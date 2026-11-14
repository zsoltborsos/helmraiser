"""Open-PR rework using a local origin, fake gh and subprocess fake pi."""

import json
import multiprocessing
import os
from dataclasses import replace
from pathlib import Path

import pytest

from helmraiser.cli import commands
from helmraiser.pipeline.worktree import WorktreeManager
from helmraiser.task.loader import render_task, write_task
from helmraiser.task.project import worktree_manager
from tests.cli.test_approve_reject import REVIEW_PASS, cli_env, invoke  # noqa: F401
from tests.cli.test_land import env as pr_env  # noqa: F401 -- shared strict fake gh
from tests.ledger.test_db import make_spec
from tests.loop.test_landing import clock  # noqa: F401
from tests.pipeline.test_worktree import git


@pytest.fixture
def env(pr_env):  # noqa: F811
    return pr_env


def approve(env, out):
    result = invoke("approve", out["run_id"], "--json", data_dir=env.data)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def delivered(env):
    out = env.new(paths=("done.txt",))
    result = approve(env, out)
    assert result["pr_url"] == "https://example.test/pr/1"
    assert not Path(out["worktree_path"]).exists()
    assert not worktree_manager(env.project)._metadata_path(out["run_id"]).exists()
    return out


def resume(env, out, fake_pi):
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"text": "Fixed", "files": [["done.txt", "fixed\n"]]},
                "*-review*": {"text": REVIEW_PASS},
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=env.data)
    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["run_id"] == out["run_id"]
    assert resumed["status"] == "awaiting_approval", resumed
    return resumed


def test_open_pr_reopens_and_worker_recreates_setup(env, fake_pi):
    out = delivered(env)
    run_id = out["run_id"]
    before = env.ledger.get_run(run_id)
    approvals = env.ledger.approvals(run_id)
    source = Path(env.ledger.get_task(out["task_id"])["source_path"])
    # Setup artifacts are intentionally absent from the published branch.
    exclude = env.project / ".git" / "info" / "exclude"
    with exclude.open("a") as f:
        f.write("\n.setup-marker\n")
    source.write_text(
        source.read_text()
        .replace("isolation: none\n", "isolation: none\nsetup: touch .setup-marker\n")
        .replace("run: test -f done.txt", "run: test -f done.txt && test -f .setup-marker")
    )
    note = "  Owner café\n[bold]fix the CI failure[/bold]  "
    calls_before = len(fake_pi.calls())
    result = invoke("rework", run_id[:8], "--note", note, "--json", data_dir=env.data)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["stage"] == "implement"
    assert len(fake_pi.calls()) == calls_before  # never runs pi inline
    row = env.ledger.get_run(run_id)
    assert row["status"] == "resuming" and row["head_commit"] is None
    assert row["branch_name"] == before["branch_name"]
    assert row["pr_url"] == before["pr_url"]
    assert env.ledger.approvals(run_id) == approvals
    request = env.ledger.reworks(run_id)[0]
    assert request["approval_id"] is None and request["worktree_recreated"]
    assert request["note"] == note and request["iteration_allowance"] == 2
    assert request["extra_cost_usd"] == pytest.approx(4 / 3)
    event = env.ledger.events(run_id, types=("rework_reopened",))[0]["payload"]
    assert event["pr_url"] == before["pr_url"]
    assert event["head_commit"] == before["head_commit"]
    assert event["worktree_recreated"]
    resumed = resume(env, out, fake_pi)
    assert resumed["branch_name"] == before["branch_name"]
    assert Path(resumed["worktree_path"], ".setup-marker").exists()
    for stage in ("implement", "review"):
        call = next(c for c in fake_pi.calls()[calls_before:] if stage in (c["session_id"] or ""))
        prompt = Path(call["argv"][-1][1:]).read_text()
        assert note in prompt
    assert env.ledger.pending_approval(run_id)["kind"] == "merge"
    old_head = before["head_commit"]
    new_head = git(env.project, "rev-parse", before["branch_name"])
    git(
        env.project,
        "config",
        "remote.origin.push",
        f"refs/heads/{before['branch_name']}:refs/heads/forbidden",
    )
    result = invoke("approve", run_id, data_dir=env.data)
    assert result.exit_code == 0, result.output
    assert "updated" in result.output and before["pr_url"] in result.output
    assert old_head != new_head == git(env.origin, "rev-parse", before["branch_name"])
    git(env.origin, "merge-base", "--is-ancestor", old_head, new_head)
    assert env.ledger.get_run(run_id)["head_commit"] == new_head
    assert env.ledger.get_run(run_id)["status"] == "landed"
    assert env.ledger.get_run(run_id)["pr_url"] == before["pr_url"]
    assert not Path(resumed["worktree_path"]).exists()
    assert sum(c.startswith("pr create") for c in env.calls()) == 1
    assert "refs/heads/forbidden" not in git(env.origin, "for-each-ref", "--format=%(refname)")


@pytest.mark.parametrize("failure", ["refused", "exception", "interrupt"])
def test_failed_hand_back_discards_fresh_tree_and_next_rework_runs_setup(
    env, fake_pi, monkeypatch, failure
):
    out = delivered(env)
    run_id = out["run_id"]
    source = Path(env.ledger.get_task(out["task_id"])["source_path"])
    with (env.project / ".git/info/exclude").open("a") as f:
        f.write("\n.setup-marker\n")
    source.write_text(
        source.read_text()
        .replace("isolation: none\n", "isolation: none\nsetup: touch .setup-marker\n")
        .replace("run: test -f done.txt", "run: test -f done.txt && test -f .setup-marker")
    )

    def fail_hand_back(*args, **kwargs):
        if failure == "refused":
            return False
        if failure == "interrupt":
            raise KeyboardInterrupt()
        raise RuntimeError("interrupted hand-back")

    with monkeypatch.context() as patch:
        patch.setattr(commands.Ledger, "hand_back", fail_hand_back)
        result = invoke("rework", run_id, "--note", "fix", "--json", data_dir=env.data)
    assert result.exit_code != 0, result.output
    assert env.ledger.get_run(run_id)["status"] == "landed"
    assert not env.ledger.reworks(run_id)
    assert not Path(out["worktree_path"]).exists()
    assert not worktree_manager(env.project)._metadata_path(run_id).exists()
    result = invoke("rework", run_id, "--note", "fix again", "--json", data_dir=env.data)
    assert result.exit_code == 0, result.output
    assert env.ledger.reworks(run_id)[0]["worktree_recreated"]
    resumed = resume(env, out, fake_pi)
    assert Path(resumed["worktree_path"], ".setup-marker").is_file()


@pytest.mark.parametrize("committed", [False, True])
def test_failed_hand_back_preserves_existing_or_committed_tree(env, monkeypatch, committed):
    out = delivered(env)
    run_id = out["run_id"]
    if not committed:
        row = env.ledger.get_run(run_id)
        worktree_manager(env.project).restore_pr(
            "fix-widget",
            run_id,
            branch_name=row["branch_name"],
            base_branch=row["base_branch"],
            worktree_path=row["worktree_path"],
        )
    hand_back = commands.Ledger.hand_back

    def fail(ledger, *args, **kwargs):
        if committed:
            assert hand_back(ledger, *args, **kwargs)
            raise RuntimeError("failed after the transition committed")
        return False

    monkeypatch.setattr(commands.Ledger, "hand_back", fail)
    result = invoke("rework", run_id, "--note", "fix", "--json", data_dir=env.data)
    assert result.exit_code != 0
    assert Path(out["worktree_path"]).is_dir()
    assert env.ledger.get_run(run_id)["status"] == ("resuming" if committed else "landed")


@pytest.mark.parametrize("first_rebase", [False, True])
def test_concurrent_reopening_serializes_restore_cleanup_and_hand_back(
    env, fake_pi, monkeypatch, first_rebase
):
    out = delivered(env)
    run_id = out["run_id"]
    approvals = env.ledger.approvals(run_id)
    source = Path(env.ledger.get_task(out["task_id"])["source_path"])
    with (env.project / ".git/info/exclude").open("a") as f:
        f.write("\n.setup-marker\n")
    source.write_text(
        source.read_text()
        .replace("isolation: none\n", "isolation: none\nsetup: touch .setup-marker\n")
        .replace("run: test -f done.txt", "run: test -f done.txt && test -f .setup-marker")
    )
    assert "setup: touch .setup-marker" in source.read_text()
    # Separate processes exercise real flock/SQLite and avoid CliRunner's global
    # stdout redirection being shared by threads. Fork inherits the fake gh/config.
    context = multiprocessing.get_context("fork")
    first_restored = context.Event()
    release_first = context.Event()
    second_waiting = context.Event()
    second_acquired = context.Event()
    restore = WorktreeManager.restore_pr
    flock = commands.fcntl.flock

    def paused_restore(manager, *args, **kwargs):
        metadata = restore(manager, *args, **kwargs)
        if multiprocessing.current_process().name == "first-rework":
            assert metadata["worktree_recreated"]
            first_restored.set()
            assert release_first.wait(15), "first rework was not released"
        return metadata

    def observed_flock(fd, operation):
        second = (
            multiprocessing.current_process().name == "second-rework"
            and operation == commands.fcntl.LOCK_EX
            and Path(os.readlink(f"/proc/self/fd/{fd}")).name == "rework.lock"
        )
        if second:
            second_waiting.set()
        result = flock(fd, operation)
        if second:
            second_acquired.set()
        return result

    monkeypatch.setattr(WorktreeManager, "restore_pr", paused_restore)
    monkeypatch.setattr(commands.fcntl, "flock", observed_flock)

    def request(connection, args):
        result = invoke("rework", run_id[:8], *args, "--json", data_dir=env.data)
        connection.send((result.exit_code, result.output))
        connection.close()

    first_result, first_send = context.Pipe(duplex=False)
    second_result, second_send = context.Pipe(duplex=False)
    first = context.Process(
        name="first-rework",
        target=request,
        args=(first_send, ["--rebase"] if first_rebase else ["--note", "first fix"]),
    )
    second = context.Process(
        name="second-rework",
        target=request,
        args=(second_send, ["--note", "second fix"]),
    )
    first.start()
    try:
        assert first_restored.wait(10), "first request never restored the tree"
        second.start()
        assert second_waiting.wait(10), "second request never attempted the per-run lock"
        assert not second_acquired.is_set()
        assert not second_result.poll()
    finally:
        release_first.set()
        first.join(30)
        if second.pid is not None:
            second.join(30)
    assert first.exitcode == second.exitcode == 0
    assert first_result.poll() and second_result.poll()
    first_code, first_output = first_result.recv()
    second_code, second_output = second_result.recv()
    assert first_code == (2 if first_rebase else 0), first_output
    assert second_code == (0 if first_rebase else 2), second_output
    if first_rebase:
        assert "nothing to rebase" in first_output
    else:
        assert "resuming" in second_output  # eligibility was re-read under the lock
    requests = env.ledger.reworks(run_id)
    assert len(requests) == 1 and requests[0]["worktree_recreated"]
    assert requests[0]["note"] == ("second fix" if first_rebase else "first fix")
    assert len(env.ledger.events(run_id, types=("rework_reopened",))) == 1
    assert env.ledger.approvals(run_id) == approvals
    assert env.ledger.get_run(run_id)["status"] == "resuming"
    assert Path(out["worktree_path"]).is_dir()
    assert "setup: touch .setup-marker" in env.ledger.get_task(out["task_id"])["snapshot"]
    resumed = resume(env, out, fake_pi)
    assert Path(resumed["worktree_path"], ".setup-marker").is_file()


@pytest.mark.parametrize(
    "problem,code,message",
    [
        ("MERGED", 2, "MERGED"),
        ("CLOSED", 2, "CLOSED"),
        ("non-pr", 2, "no delivered PR"),
        ("no-url", 2, "no delivered PR"),
        ("gh-failure", 1, "offline"),
        ("invalid", 1, "invalid JSON"),
        ("UNKNOWN", 1, "unexpected pull request state"),
    ],
)
def test_reopening_refused_without_transition(env, problem, code, message):
    out = delivered(env)
    run_id = out["run_id"]
    if problem == "non-pr":
        env.ledger.update_run(run_id, delivery="merge")
    elif problem == "no-url":
        env.ledger.update_run(run_id, pr_url=None)
    elif problem == "gh-failure":
        env.gh.write_text("#!/bin/sh\necho offline >&2\nexit 1\n")
    else:
        env.state.write_text(problem)
    before = env.ledger.get_run(run_id), env.ledger.approvals(run_id)
    result = invoke("rework", run_id, "--note", "fix", "--json", data_dir=env.data)
    assert result.exit_code == code, result.output
    assert message in result.output
    assert before == (env.ledger.get_run(run_id), env.ledger.approvals(run_id))
    assert env.ledger.reworks(run_id) == []
    assert not Path(out["worktree_path"]).exists()


@pytest.mark.parametrize("problem", ["CLOSED", "MERGED", "stale-base", "non-ff"])
def test_second_approval_refusal_keeps_worktree_and_pending_approval(env, fake_pi, problem):
    out = delivered(env)
    run_id = out["run_id"]
    result = invoke("rework", run_id, "--note", "fix", "--json", data_dir=env.data)
    assert result.exit_code == 0, result.output
    resume(env, out, fake_pi)
    pending = env.ledger.pending_approval(run_id)
    branch = out["branch_name"]
    remote_before = git(env.origin, "rev-parse", branch)
    if problem in ("CLOSED", "MERGED"):
        env.state.write_text(problem)
    elif problem == "stale-base":
        git(env.project, "commit", "--allow-empty", "-m", "base moved after rework")
    else:
        # Simulate another author advancing the remote without touching the run tree.
        other = env.project.parent / "author"
        git(env.project.parent, "clone", str(env.origin), str(other))
        git(other, "config", "user.email", "other@example.test")
        git(other, "config", "user.name", "Other")
        git(other, "checkout", branch)
        git(other, "commit", "--allow-empty", "-m", "remote only")
        git(other, "push", "origin", branch)
        remote_before = git(env.origin, "rev-parse", branch)
    result = invoke("approve", run_id, "--json", data_dir=env.data)
    assert result.exit_code == 1, result.output
    assert (
        f"helmraiser rework {run_id} --rebase"
        if problem == "stale-base"
        else "rejected" if problem == "non-ff" else problem
    ) in result.output
    assert env.ledger.get_run(run_id)["status"] == "awaiting_approval"
    assert env.ledger.pending_approval(run_id) == pending
    assert Path(out["worktree_path"]).exists()
    assert git(env.origin, "rev-parse", branch) == remote_before
    assert sum(c.startswith("pr create") for c in env.calls()) == 1


def test_dependents_held_until_the_new_delivered_head_is_in_base(env, fake_pi):
    out = delivered(env)
    run_id = out["run_id"]
    env.state.write_text("OPEN")
    dependent = replace(
        make_spec(env.project, "dependent", depends_on=("fix-widget",)),
        source_path=env.project.parent / "dependent.md",
    )
    write_task(dependent, dependent.source_path)
    env.ledger.upsert_task(dependent, render_task(dependent))
    git(env.project, "merge", "--ff-only", out["branch_name"])
    assert env.ledger.next_queued()["task_id"] == dependent.task_id
    previous = env.ledger.get_run(run_id)["head_commit"]
    result = invoke("rework", run_id, "--note", "fix", "--json", data_dir=env.data)
    assert result.exit_code == 0, result.output
    assert env.ledger.get_run(run_id)["head_commit"] is None
    for resumed in (False, True):
        if resumed:
            resume(env, out, fake_pi)
        held = env.ledger.held_tasks()[dependent.task_id]
        assert held["reason"] == "depends_on"
        assert held["unmet"][0]["reason"] == "not_landed"
        assert env.ledger.next_queued() is None
    approve(env, out)
    new_head = env.ledger.get_run(run_id)["head_commit"]
    assert new_head and new_head != previous
    held = env.ledger.held_tasks()[dependent.task_id]
    assert held["reason"] == "depends_on" and held["unmet"][0]["reason"] == "not_in_base"
    git(env.project, "merge", "--ff-only", out["branch_name"])
    assert env.ledger.next_queued()["task_id"] == dependent.task_id


def test_reopening_validates_task_before_restoration(env):
    out = delivered(env)
    source = Path(env.ledger.get_task(out["task_id"])["source_path"])
    source.write_text("not a task")
    result = invoke("rework", out["run_id"], "--note", "fix", "--json", data_dir=env.data)
    assert result.exit_code == 2, result.output
    assert not Path(out["worktree_path"]).exists()
    assert env.ledger.reworks(out["run_id"]) == []


@pytest.mark.parametrize("moved", [False, True])
def test_open_pr_rebase_admission(env, moved):
    out = delivered(env)
    if moved:
        (env.project / "upstream.txt").write_text("new base\n")
        git(env.project, "add", "upstream.txt")
        git(env.project, "commit", "-m", "move base")
    result = invoke("rework", out["run_id"], "--rebase", "--json", data_dir=env.data)
    assert result.exit_code == (0 if moved else 2), result.output
    if moved:
        assert env.ledger.reworks(out["run_id"])[0]["rebase"]
    else:
        assert "nothing to rebase" in result.output
        assert not env.ledger.reworks(out["run_id"])
        assert not Path(out["worktree_path"]).exists()
        # A later accepted request must still know setup is needed.
        result = invoke("rework", out["run_id"], "--note", "fix", "--json", data_dir=env.data)
        assert result.exit_code == 0, result.output
        assert env.ledger.reworks(out["run_id"])[0]["worktree_recreated"]
