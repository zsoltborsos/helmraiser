"""Rebase reworks use real git conflicts, resolved only by scripted sessions."""

import os
import subprocess
from pathlib import Path

import pytest

from helmraiser.task.project import worktree_manager
from tests.loop.test_evidence_review_merge import EVIDENCE, SHOTS_MET, _git, verdict_text
from tests.loop.test_rework import hand_back_rework, resume_rework
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter


async def park_run(make_task, make_run):
    task = make_task(paths=("done.txt",), evidence=EVIDENCE)
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "task version\n")]),
            "review": PiScript(final_text=verdict_text("pass", evidence_checks=SHOTS_MET)),
        }
    )
    run, _ = make_run(task, adapter)
    parked = await run.run()
    assert parked.status == "awaiting_approval", parked.failure_reason
    return task, run.run_id, parked


def move_base(project):
    (project / "done.txt").write_text("main version\n")
    (project / "other.txt").write_text("upstream only\n")
    _git(project, "add", "done.txt", "other.txt")
    _git(project, "commit", "-m", "move base")
    return _git(project, "rev-parse", "HEAD")


def start_rebase(worktree, base):
    result = subprocess.run(
        ["git", "rebase", base], cwd=worktree, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "done.txt" in _git(worktree, "diff", "--name-only", "--diff-filter=U")
    assert "<<<<<<<" in (worktree / "done.txt").read_text()


def resolve_rebase(worktree):
    (worktree / "done.txt").write_text("main version\ntask version\n")
    _git(worktree, "add", "done.txt")
    subprocess.run(
        ["git", "rebase", "--continue"],
        cwd=worktree,
        env={**os.environ, "GIT_EDITOR": "true"},
        capture_output=True,
        text=True,
        check=True,
    )


def rebase_and_resolve(base):
    def action(worktree):
        start_rebase(worktree, base)
        resolve_rebase(worktree)

    return action


def rework_adapter(implement):
    return ScriptedPiAdapter(
        {
            "implement": implement,
            "review": PiScript(final_text=verdict_text("pass", evidence_checks=SHOTS_MET)),
        }
    )


@pytest.mark.parametrize("location", ["default", "external"])
async def test_rework_rebase_resolves_the_conflict_and_parks_again(
    make_task, make_run, ledger, data_dir, config, tmp_project, tmp_path, location
):
    project_file = tmp_project / ".helmraiser" / "project.toml"
    if location != "default":
        project_file.parent.mkdir(exist_ok=True)
        directory = tmp_path / "external"
        project_file.write_text(f'[defaults]\nworktree_dir = "{directory}"\n')
    task, run_id, parked = await park_run(make_task, make_run)
    metadata_path = Path(parked.worktree_path).parent / f"{run_id}.json"
    moved = move_base(task.project)
    base = ledger.get_run(run_id)["base_branch"]
    hand_back_rework(ledger, run_id, task, rebase=True)
    adapter = rework_adapter(PiScript(action=rebase_and_resolve(base)))
    resumed = resume_rework(run_id, adapter, config, ledger, data_dir)
    outcome = await resumed.run()

    prompt = adapter.calls_for("implement")[0].prompt
    assert prompt.startswith("## Rebase request") and f"git rebase {base}" in prompt
    assert outcome.status == "awaiting_approval", outcome.failure_reason
    assert outcome.pending_approval == "merge"
    assert ledger.pending_approval(run_id)["kind"] == "merge"
    assert all(r["passed"] for r in outcome.verify_results)
    assert outcome.evidence_results and all(r["ok"] for r in outcome.evidence_results)
    assert len(adapter.calls_for("review")) == 1
    assert outcome.base_commit == moved != parked.base_commit
    assert ledger.get_run(run_id)["base_commit"] == moved
    manager = worktree_manager(task.project)
    assert manager.metadata(run_id)["base_commit"] == moved
    assert manager._metadata_path(run_id) == metadata_path
    assert list(tmp_path.rglob(f"{run_id}.json")) == [metadata_path]
    events = ledger.events(run_id)
    assert not any(e["type"] == "path_scope_failed" for e in events)
    rebased = [e for e in events if e["type"] == "rebased"]
    assert len(rebased) == 1
    assert rebased[0]["payload"] == {
        "base_branch": base,
        "base_commit": moved,
        "previous": parked.base_commit,
    }
    assert rebased[0]["stage"] == "implement"
    assert next(e for e in events if e["type"] == "run_resume")["payload"]["from"] == "rework"
    assert "other.txt" not in resumed.rundir.diff_patch.read_text()
    _git(task.project, "merge-base", "--is-ancestor", moved, outcome.branch_name)
    manager.finalize_local(task.id, run_id)
    assert (task.project / "done.txt").read_text() == "main version\ntask version\n"


async def test_rework_rebase_checks_the_resolved_tip_when_the_base_ref_moves(
    make_task, make_run, ledger, data_dir, config, monkeypatch
):
    task, run_id, parked = await park_run(make_task, make_run)
    moved = move_base(task.project)
    base = ledger.get_run(run_id)["base_branch"]
    ref = f"refs/heads/{base}"
    hand_back_rework(ledger, run_id, task, rebase=True)
    resumed = resume_rework(run_id, rework_adapter(PiScript()), config, ledger, data_dir)
    resumed.worktree = Path(parked.worktree_path)
    resumed.base_commit = parked.base_commit
    git = resumed._git

    def rewind_after_resolution(*args):
        result = git(*args)
        if args == ("rev-parse", "--verify", ref):
            assert result.stdout.strip() == moved
            # The old base is contained in HEAD, but the resolved tip is not.
            _git(task.project, "update-ref", ref, parked.base_commit, moved)
        return result

    monkeypatch.setattr(resumed, "_git", rewind_after_resolution)
    result = resumed._rebase_result()

    assert result is not None and result.name == "rebase" and not result.passed
    assert moved[:12] in result.tail
    assert resumed.base_commit == parked.base_commit
    assert ledger.get_run(run_id)["base_commit"] == parked.base_commit
    assert worktree_manager(task.project).metadata(run_id)["base_commit"] == parked.base_commit
    assert not any(e["type"] == "rebased" for e in ledger.events(run_id))


async def test_rework_rebase_gate_feeds_back_until_the_branch_is_rebased(
    make_task, make_run, ledger, data_dir, config
):
    task, run_id, parked = await park_run(make_task, make_run)
    move_base(task.project)
    base = ledger.get_run(run_id)["base_branch"]

    def resolve_and_remove_extra(worktree):
        rebase_and_resolve(base)(worktree)
        (worktree / "extra.txt").unlink()

    hand_back_rework(ledger, run_id, task, rebase=True)
    adapter = rework_adapter(
        [
            PiScript(files=[("extra.txt", "not a rebase\n")]),
            PiScript(action=resolve_and_remove_extra),
        ]
    )
    outcome = await resume_rework(run_id, adapter, config, ledger, data_dir).run()
    first = ledger.iterations(run_id)[parked.iterations]["verify_results"]
    assert len(first) == 1 and first[0]["name"] == "rebase" and not first[0]["passed"]
    assert "does not contain" in first[0]["tail"]
    assert first[0]["tail"] in adapter.calls_for("implement")[1].prompt
    assert outcome.status == "awaiting_approval", outcome.failure_reason
    assert outcome.iterations == parked.iterations + 2


async def test_rework_rebase_never_commits_a_half_done_rebase(
    make_task, make_run, ledger, data_dir, config
):
    task, run_id, parked = await park_run(make_task, make_run)
    move_base(task.project)
    base = ledger.get_run(run_id)["base_branch"]
    hand_back_rework(ledger, run_id, task, rebase=True)
    adapter = rework_adapter(
        [PiScript(action=lambda cwd: start_rebase(cwd, base)), PiScript(action=resolve_rebase)]
    )
    outcome = await resume_rework(run_id, adapter, config, ledger, data_dir).run()
    first = ledger.iterations(run_id)[parked.iterations]["verify_results"]
    assert len(first) == 1 and first[0]["name"] == "rebase" and not first[0]["passed"]
    assert "rebase is in progress" in first[0]["tail"]
    assert outcome.status == "awaiting_approval", outcome.failure_reason
    assert "<<<<<<<" not in _git(Path(outcome.worktree_path), "log", "-p", outcome.branch_name)


async def test_rework_without_rebase_ignores_a_moved_base(
    make_task, make_run, ledger, data_dir, config
):
    task, run_id, parked = await park_run(make_task, make_run)
    move_base(task.project)
    hand_back_rework(ledger, run_id, task)
    adapter = rework_adapter(PiScript(files=[("done.txt", "task version improved\n")]))
    outcome = await resume_rework(run_id, adapter, config, ledger, data_dir).run()
    assert outcome.status == "awaiting_approval", outcome.failure_reason
    assert "## Rebase request" not in adapter.calls_for("implement")[0].prompt
    assert outcome.base_commit == parked.base_commit
    assert not any(r["name"] == "rebase" for r in outcome.verify_results)
    assert not any(e["type"] == "rebased" for e in ledger.events(run_id))


@pytest.mark.parametrize("rebase", [False, True])
async def test_reopened_pr_rejects_rewritten_published_history_in_the_loop(
    make_task, make_run, ledger, data_dir, config, rebase
):
    task, run_id, parked = await park_run(make_task, make_run)
    tree = Path(parked.worktree_path)
    published = _git(tree, "rev-parse", "HEAD")
    reset_to = move_base(task.project) if rebase else parked.base_commit
    approval = ledger.pending_approval(run_id)
    ledger.decide_approval(approval["id"], "approved")
    ledger.update_run(
        run_id, status="landed", delivery="pr", pr_url="https://example/pr/1", head_commit=published
    )
    metadata = worktree_manager(task.project).metadata(run_id)
    assert ledger.hand_back(
        run_id,
        "implement",
        rework={
            "approval_id": None,
            "note": "Fix CI without rewriting published commits",
            "findings": [],
            "iteration_allowance": 2,
            "extra_cost_usd": 2,
            "extra_minutes": 20,
            **({"rebase": True} if rebase else {}),
        },
        reopen_pr={
            **metadata,
            "head_commit": published,
            "pr_url": "https://example/pr/1",
            "worktree_recreated": False,
        },
    )

    def rewrite(cwd):
        _git(cwd, "reset", "--hard", reset_to)
        (cwd / "done.txt").write_text("rewritten task\n")
        _git(cwd, "add", "done.txt")
        _git(cwd, "commit", "-m", "rewrite published work")

    def restore(cwd):
        subprocess.run(
            ["git", "merge", "--no-commit", published], cwd=cwd, capture_output=True, check=False
        )
        (cwd / "done.txt").write_text("task version improved\n")
        _git(cwd, "add", "done.txt")
        _git(cwd, "commit", "--no-edit")

    adapter = rework_adapter([PiScript(action=rewrite), PiScript(action=restore)])
    outcome = await resume_rework(run_id, adapter, config, ledger, data_dir).run()
    first = ledger.iterations(run_id)[parked.iterations]["verify_results"]
    assert len(first) == 1 and first[0]["name"] == "rebase" and not first[0]["passed"]
    assert "published commits must not be rewritten" in first[0]["tail"].lower()
    assert published in first[0]["tail"] and "restore" in first[0]["tail"].lower()
    assert first[0]["tail"] in adapter.calls_for("implement")[1].prompt
    assert outcome.status == "awaiting_approval", outcome.failure_reason
    assert len(adapter.calls_for("review")) == 1
    assert outcome.iterations == parked.iterations + 2
    _git(tree, "merge-base", "--is-ancestor", published, "HEAD")


@pytest.mark.parametrize("unfinished", [False, True])
async def test_published_pr_merges_base_without_rewriting(
    make_task, make_run, ledger, data_dir, config, unfinished
):
    task, run_id, parked = await park_run(make_task, make_run)
    tree = Path(parked.worktree_path)
    published = _git(tree, "rev-parse", "HEAD")
    ledger.update_run(run_id, pr_url="https://example/pr/1")
    moved = move_base(task.project)
    base = ledger.get_run(run_id)["base_branch"]

    def start_merge(cwd):
        result = subprocess.run(
            ["git", "merge", "--no-edit", base], cwd=cwd, capture_output=True, check=False
        )
        assert result.returncode != 0
        assert "<<<<<<<" in (cwd / "done.txt").read_text()
        # Even a staged but unfinished merge must not be auto-committed by Helmraiser.
        (cwd / "done.txt").write_text("main version\\ntask version\\n")
        _git(cwd, "add", "done.txt")

    def complete(cwd):
        _git(cwd, "commit", "--no-edit")

    def merge(cwd):
        start_merge(cwd)
        complete(cwd)

    hand_back_rework(ledger, run_id, task, rebase=True)
    adapter = rework_adapter(
        [PiScript(action=start_merge), PiScript(action=complete)]
        if unfinished
        else PiScript(action=merge)
    )
    outcome = await resume_rework(run_id, adapter, config, ledger, data_dir).run()
    assert outcome.status == "awaiting_approval", outcome.failure_reason
    prompt = adapter.calls_for("implement")[0].prompt
    assert f"git merge --no-edit {base}" in prompt
    assert "Do not rewrite published commits or force push" in prompt
    assert f"git rebase {base}" not in prompt
    assert outcome.base_commit == moved
    assert all(r["passed"] for r in outcome.verify_results)
    assert outcome.evidence_results and all(r["ok"] for r in outcome.evidence_results)
    assert len(adapter.calls_for("review")) == 1
    _git(tree, "merge-base", "--is-ancestor", published, "HEAD")
    _git(tree, "merge-base", "--is-ancestor", moved, "HEAD")
    if unfinished:
        first = ledger.iterations(run_id)[parked.iterations]["verify_results"]
        assert first[0]["name"] == "rebase" and not first[0]["passed"]
        assert "integration is in progress" in first[0]["tail"]
        assert first[0]["tail"] in adapter.calls_for("implement")[1].prompt


async def test_rework_rebase_blocked_by_the_implementer_fails_the_run(
    make_task, make_run, ledger, data_dir, config
):
    task, run_id, _ = await park_run(make_task, make_run)
    move_base(task.project)
    hand_back_rework(ledger, run_id, task, rebase=True)
    adapter = rework_adapter(PiScript(final_text="BLOCKED: cannot resolve done.txt"))
    outcome = await resume_rework(run_id, adapter, config, ledger, data_dir).run()
    assert outcome.status == "failed"
    assert outcome.failure_reason == "blocked: cannot resolve done.txt"
