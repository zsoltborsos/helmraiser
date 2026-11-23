"""End-to-end owner landing with a local bare origin and a file-scripted gh."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from helmraiser import leeboard
from helmraiser.cli import app
from helmraiser.ledger.db import Ledger
from helmraiser.ledger.rundir import RunDir
from helmraiser.loop import landing
from helmraiser.storage.state import StateManager
from helmraiser.task.project import worktree_manager
from tests.cli.test_approve_reject import REVIEW_PASS, cli_env, invoke, queue_and_run  # noqa: F401
from tests.loop.test_landing import clock  # noqa: F401
from tests.pipeline.test_worktree import git

runner = CliRunner()
STEP_NAMES = ["preflight", "leeboard", "approve", "ci", "sync", "push", "local", "confirm"]

GH = r"""#!/bin/sh
printf '%s\n' "$*" >> "$LAND_GH_LOG"
case "$1 $2" in
  "pr create") echo 'https://example.test/pr/1' ;;
  "pr checks")
    checks="$LAND_CHECKS"
    head=$(cat "$LAND_VISIBLE_HEAD" 2>/dev/null || git --git-dir="$LAND_ORIGIN" rev-parse "refs/heads/$LAND_BRANCH")
    if [ -f "$LAND_CHECKS_BY_HEAD/$head" ]; then checks="$LAND_CHECKS_BY_HEAD/$head"; fi
    IFS='|' read -r code text < "$checks"
    if [ "$(wc -l < "$checks")" -gt 1 ]; then
      tail -n +2 "$checks" > "$checks.next"
      mv "$checks.next" "$checks"
    fi
    echo "$text" >&2
    exit "$code" ;;
  "pr view")
    if [ "$5" = headRefOid ]; then
      if [ -s "$LAND_HEADS" ]; then
        head=$(head -n 1 "$LAND_HEADS")
        if [ "$(wc -l < "$LAND_HEADS")" -gt 1 ]; then
          tail -n +2 "$LAND_HEADS" > "$LAND_HEADS.next"
          mv "$LAND_HEADS.next" "$LAND_HEADS"
        fi
      else
        head=$(git --git-dir="$LAND_ORIGIN" rev-parse "refs/heads/$LAND_BRANCH")
      fi
      printf '%s\n' "$head" > "$LAND_VISIBLE_HEAD"
      printf '{"headRefOid":"%s"}\n' "$head"
      exit 0
    fi
    state=$(cat "$LAND_STATE")
    if [ "$state" = auto ]; then
      if git --git-dir="$LAND_ORIGIN" merge-base --is-ancestor "refs/heads/$LAND_BRANCH" refs/heads/main; then
        state=MERGED
      else
        state=OPEN
      fi
    fi
    if [ "$state" = invalid ]; then echo nope; else printf '{"state":"%s"}\n' "$state"; fi ;;
  *) echo "unexpected gh: $*" >&2; exit 64 ;;
esac
"""


@pytest.fixture
def env(tmp_project, tmp_path, fake_pi, monkeypatch, clock, cli_env):  # noqa: F811
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_project, "remote", "add", "origin", str(origin))
    git(tmp_project, "push", "origin", "main")
    bin_dir = tmp_path / "gh-bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(GH)
    gh.chmod(0o755)
    checks = tmp_path / "checks"
    checks.write_text("0|passed\n")
    state = tmp_path / "state"
    state.write_text("auto")
    heads = tmp_path / "heads"
    visible_head = tmp_path / "visible-head"
    checks_by_head = tmp_path / "checks-by-head"
    checks_by_head.mkdir()
    log = tmp_path / "gh.log"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    for key, value in {
        "LAND_GH_LOG": log,
        "LAND_CHECKS": checks,
        "LAND_STATE": state,
        "LAND_ORIGIN": origin,
        "LAND_HEADS": heads,
        "LAND_VISIBLE_HEAD": visible_head,
        "LAND_CHECKS_BY_HEAD": checks_by_head,
    }.items():
        monkeypatch.setenv(key, str(value))
    data = tmp_path / "data"
    ledger = Ledger(data / "helmraiser.db")

    def new(**kwargs):
        outcome = queue_and_run(tmp_project, data, fake_pi, delivery="pr", **kwargs)
        monkeypatch.setenv("LAND_BRANCH", outcome["branch_name"])
        return outcome

    def land(outcome, *args, json_output=True):
        return runner.invoke(
            app,
            [
                "land",
                outcome["run_id"],
                "--interval",
                "0",
                "--timeout",
                "30",
                "--data-dir",
                str(data),
                *(["--json"] if json_output else []),
                *args,
            ],
        )

    return SimpleNamespace(
        project=tmp_project,
        origin=origin,
        data=data,
        ledger=ledger,
        new=new,
        land=land,
        checks=checks,
        state=state,
        heads=heads,
        visible_head=visible_head,
        checks_by_head=checks_by_head,
        log=log,
        gh=gh,
        calls=lambda: log.read_text().splitlines() if log.exists() else [],
    )


@pytest.mark.parametrize(
    "mode", ["failed", "pending", "timeout", "absent", "no_ci", "no_ci_failed", "registers"]
)
def test_checks_and_recovery(env, monkeypatch, mode):
    out = env.new()
    before = git(env.origin, "rev-parse", "main")
    responses = {
        "failed": "1|test failed\n",
        "pending": "8|pending\n8|pending\n0|passed\n",
        "timeout": "8|pending\n",
        "absent": "1|No Checks Reported\n",
        "no_ci": "1|no checks reported\n",
        "no_ci_failed": "1|test failed\n",
        "registers": "1|no checks reported\n8|pending\n0|passed\n",
    }
    env.checks.write_text(responses[mode])
    monkeypatch.setattr(landing, "NO_CHECKS_GRACE", 0.5)
    extra = ["--no-ci"] if mode.startswith("no_ci") else []
    if mode == "timeout":
        extra += ["--timeout", "1"]
    result = env.land(out, *extra)
    if mode in ("pending", "no_ci", "registers"):
        assert result.exit_code == 0, result.output
        return
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["steps"][-1]["name"] == "ci"
    assert (
        "--no-ci" if mode == "absent" else "after 1 s" if mode == "timeout" else "test failed"
    ) in payload["error"]
    assert git(env.origin, "rev-parse", "main") == git(env.project, "rev-parse", "main") == before
    assert env.ledger.get_run(out["run_id"])["status"] == "landed"
    env.checks.write_text("0|passed\n")
    result = env.land(out)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["steps"][2]["status"] == "skipped"
    assert sum(c.startswith("pr create") for c in env.calls()) == 1


@pytest.mark.parametrize("approve_first", [False, True])
def test_land_reworked_run_uses_original_pr(env, fake_pi, approve_first):
    out = env.new()
    run_id = out["run_id"]
    result = invoke("approve", run_id, "--json", data_dir=env.data)
    assert result.exit_code == 0, result.output
    url = json.loads(result.output)["pr_url"]
    old = env.ledger.get_run(run_id)["head_commit"]
    result = invoke("rework", run_id, "--note", "fix CI", "--json", data_dir=env.data)
    assert result.exit_code == 0, result.output
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"text": "Fixed", "files": [["done.txt", "fixed\\n"]]},
                "*-review*": {"text": REVIEW_PASS},
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=env.data)
    assert result.exit_code == 0, result.output
    assert env.ledger.get_run(run_id)["status"] == "awaiting_approval"
    new = git(env.project, "rev-parse", out["branch_name"])
    assert new != old
    if approve_first:
        result = invoke("approve", run_id, "--json", data_dir=env.data)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["pr_url"] == url
    result = env.land(out)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["landed"] and payload["pr_url"] == url and payload["head"] == new
    assert payload["steps"][2]["status"] == ("skipped" if approve_first else "ok")
    assert f"pr checks {url}" in env.calls()
    assert sum(c.startswith("pr create") for c in env.calls()) == 1
    assert git(env.origin, "rev-parse", "main") == new
    git(env.origin, "merge-base", "--is-ancestor", old, new)
    assert env.ledger.get_run(run_id)["head_commit"] == new


@pytest.mark.parametrize("approve_first", [False, True])
@pytest.mark.parametrize(
    "old_checks,new_checks,lands",
    [
        ("0|old green\n", "8|new pending\n", False),
        ("0|old green\n", "1|new red\n", False),
        ("1|old red\n", "0|new green\n", True),
    ],
)
def test_land_waits_for_updated_pr_head_before_reading_checks(
    env, fake_pi, approve_first, old_checks, new_checks, lands
):
    out = env.new()
    run_id = out["run_id"]
    result = invoke("approve", run_id, "--json", data_dir=env.data)
    assert result.exit_code == 0, result.output
    old_head = env.ledger.get_run(run_id)["head_commit"]
    base_before = git(env.origin, "rev-parse", "main")
    result = invoke("rework", run_id, "--note", "fix CI", "--json", data_dir=env.data)
    assert result.exit_code == 0, result.output
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
    new_head = git(env.project, "rev-parse", out["branch_name"])
    assert new_head != old_head
    if approve_first:
        result = invoke("approve", run_id, "--json", data_dir=env.data)
        assert result.exit_code == 0, result.output
    # GitHub still exposes the previous head and its checks after the push.
    env.visible_head.write_text(old_head)
    env.heads.write_text(f"{old_head}\n{old_head}\n{new_head}\n")
    (env.checks_by_head / old_head).write_text(old_checks)
    (env.checks_by_head / new_head).write_text(new_checks)
    calls_before = len(env.calls())
    result = env.land(out, "--timeout", "3")
    assert result.exit_code == (0 if lands else 1), result.output
    payload = json.loads(result.output)
    assert git(env.origin, "rev-parse", "main") == (new_head if lands else base_before)
    if not lands:
        assert payload["steps"][-1]["name"] == "ci"
        assert ("pending after 3 s" if "pending" in new_checks else "new red") in payload["error"]
    calls = env.calls()[calls_before:]
    head_reads = [i for i, call in enumerate(calls) if call.endswith("--json headRefOid")]
    assert len(head_reads) == 3
    assert head_reads[-1] < next(i for i, call in enumerate(calls) if call.startswith("pr checks"))
    if not approve_first:
        assert payload["steps"][2]["detail"] == "updated https://example.test/pr/1"


@pytest.mark.parametrize("destination", ["main", "forbidden"])
def test_approval_push_mapping_cannot_bypass_ci(env, destination):
    out = env.new()
    branch = out["branch_name"]
    before = git(env.origin, "rev-parse", "main")
    git(
        env.project,
        "config",
        "remote.origin.push",
        f"refs/heads/{branch}:refs/heads/{destination}",
    )
    env.checks.write_text("1|test failed\n")

    result = env.land(out)

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["steps"][-1]["name"] == "ci"
    assert git(env.origin, "rev-parse", "main") == before
    head = git(env.project, "rev-parse", branch)
    assert head != before
    assert git(env.origin, "rev-parse", branch) == head
    assert set(git(env.origin, "for-each-ref", "--format=%(refname)").splitlines()) == {
        "refs/heads/main",
        f"refs/heads/{branch}",
    }
    assert env.ledger.get_run(out["run_id"])["status"] == "landed"
    assert [call.split()[:2] for call in env.calls()] == [
        ["pr", "create"],
        ["pr", "view"],
        ["pr", "checks"],
    ]


def test_origin_ahead_refuses(env):
    out = env.new()
    (env.project / "README.md").write_text("origin ahead")
    git(env.project, "commit", "-am", "origin ahead")
    git(env.project, "push", "origin", "main")
    git(env.project, "reset", "--hard", "HEAD^")
    before = git(env.origin, "rev-parse", "main")
    result = env.land(out)
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["steps"][-1]["name"] == "sync"
    assert f"helmraiser rework {out['run_id']} --rebase" in payload["error"]
    assert "update local main from origin" in payload["error"]
    assert git(env.origin, "rev-parse", "main") == before


def _advance_origin(env, tmp_path):
    writer = tmp_path / "writer"
    git(tmp_path, "clone", "--branch", "main", str(env.origin), str(writer))
    git(writer, "config", "user.name", "Test")
    git(writer, "config", "user.email", "test@example.com")
    (writer / "upstream.txt").write_text("landed elsewhere\n")
    git(writer, "add", "upstream.txt")
    git(writer, "commit", "-m", "upstream change")
    git(writer, "push", "origin", "main")
    return git(writer, "rev-parse", "HEAD")


@pytest.mark.parametrize("as_json", [False, True])
def test_stale_base_other_worktree_refuses_at_approval(env, tmp_path, as_json):
    out = env.new()
    run_id = out["run_id"]
    git(env.project, "branch", "--set-upstream-to=origin/main", "main")
    git(env.project, "checkout", "-b", "scratch")
    side = tmp_path / "side"
    git(env.project, "worktree", "add", str(side), "main")
    old = git(env.project, "rev-parse", "main")
    _advance_origin(env, tmp_path)
    before = env.ledger.get_run(run_id)
    events = env.ledger.events(run_id)
    approvals = env.ledger.approvals(run_id)
    manager = worktree_manager(env.project)
    metadata = manager._metadata_path(run_id).read_bytes()
    summary = RunDir(run_id, env.data).summary_md.read_bytes()
    with StateManager(env.project / ".helmraiser" / "state.db") as state:
        project_run = state.get_run(run_id)

    result = env.land(out, json_output=as_json)

    assert result.exit_code == 1, result.output
    if as_json:
        payload = json.loads(result.stdout)
        assert [s["name"] for s in payload["steps"]] == STEP_NAMES[:3]
        assert payload["steps"][-1]["status"] == "failed"
        error = payload["error"]
    else:
        error = result.output
        assert "approve" in error
    assert all(word in error for word in ("finalize_refused", "main", "origin", "fetch"))
    row = env.ledger.get_run(run_id)
    assert row["status"] == env.ledger.get_task(row["task_id"])["status"] == "awaiting_approval"
    assert row["ended_at"] == before["ended_at"] and row["pr_url"] is None
    assert {k: v for k, v in row.items() if k != "failure_reason"} == {
        k: v for k, v in before.items() if k != "failure_reason"
    }
    assert env.ledger.approvals(run_id) == approvals
    assert env.ledger.pending_approval(run_id)["decision"] == "pending"
    assert not any(e["type"] == "delivered" for e in env.ledger.events(run_id))
    assert env.ledger.events(run_id) == events
    with StateManager(env.project / ".helmraiser" / "state.db") as state:
        assert state.get_run(run_id) == project_run
    assert git(env.project, "rev-parse", out["branch_name"]) == out["head_commit"]
    assert git(env.project, "rev-parse", "main") == old
    for checkout in (env.project, side):
        assert git(checkout, "rev-parse", "HEAD") == old
        assert git(checkout, "status", "--porcelain", "--untracked-files=no") == ""
        assert (checkout / "README.md").read_text() == "# Test Project\n"
        assert not (checkout / "upstream.txt").exists()
    assert Path(out["worktree_path"]).is_dir()
    assert git(Path(out["worktree_path"]), "rev-parse", "HEAD") == out["head_commit"]
    assert manager._metadata_path(run_id).read_bytes() == metadata
    assert RunDir(run_id, env.data).summary_md.read_bytes() == summary
    assert git(env.origin, "for-each-ref", f"refs/heads/{out['branch_name']}") == ""
    assert env.calls() == []


def test_stale_unchecked_base_synchronizes_and_lands(env, tmp_path):
    out = env.new()
    run_id = out["run_id"]
    git(env.project, "branch", "--set-upstream-to=origin/main", "main")
    git(env.project, "checkout", "-b", "scratch")
    old = git(env.project, "rev-parse", "HEAD")
    new = _advance_origin(env, tmp_path)
    since = env.ledger.events(run_id)[-1]["id"]

    result = env.land(out)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["landed"] is True
    assert [s["name"] for s in payload["steps"]] == STEP_NAMES
    assert [s["status"] for s in payload["steps"]] == ["ok"] * 6 + ["warn", "ok"]
    assert payload["local"] == "skipped"
    assert "not_checked_out" in payload["steps"][6]["detail"]
    head = payload["head"]
    assert git(env.project, "rev-parse", "main") == new
    assert git(env.project, "rev-parse", f"{head}^") == new
    assert git(env.origin, "rev-parse", "main") == head
    assert git(env.origin, "rev-parse", out["branch_name"]) == head
    assert env.ledger.get_run(run_id)["status"] == "landed"
    events = env.ledger.events(run_id, since_id=since)
    (sync,) = [e for e in events if e["type"] == "base_sync"]
    (delivered,) = [e for e in events if e["type"] == "delivered"]
    assert sync["id"] < delivered["id"]
    assert sync["payload"]["status"] == "synced"
    assert sync["payload"]["reason"] == "fast_forward"
    assert sync["payload"]["from_commit"] == old
    assert sync["payload"]["to_commit"] == sync["payload"]["upstream_commit"] == new
    assert sum(c.startswith("pr create") for c in env.calls()) == 1
    assert git(env.project, "symbolic-ref", "--short", "HEAD") == "scratch"
    assert git(env.project, "rev-parse", "HEAD") == old
    assert git(env.project, "status", "--porcelain", "--untracked-files=no") == ""
    assert (env.project / "README.md").read_text() == "# Test Project\n"
    assert not (env.project / "upstream.txt").exists()


def test_remote_branch_mismatch(env):
    out = env.new()
    env.checks.write_text("1|failed\n")
    assert env.land(out).exit_code == 1  # Open the PR, without merging.
    head = git(env.project, "rev-parse", out["branch_name"])
    before = git(env.origin, "rev-parse", "main")
    git(env.origin, "update-ref", f"refs/heads/{out['branch_name']}", before)
    env.heads.write_text(f"{head}\n")  # GitHub CI metadata still shows the approved head.
    env.state.write_text("OPEN")
    env.checks.write_text("0|passed\n")
    result = env.land(out)
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["steps"][-1]["name"] == "sync"
    assert head in payload["error"] and before in payload["error"]
    assert git(env.origin, "rev-parse", "main") == before


def test_racing_push_rejected(env, monkeypatch):
    out = env.new()
    real_push = landing.push_base
    competitor = []

    def push(project, base, head):
        (project / "README.md").write_text("competitor")
        git(project, "commit", "-am", "competitor")
        git(project, "push", "origin", "main")
        competitor.append(git(project, "rev-parse", "main"))
        real_push(project, base, head)

    monkeypatch.setattr(landing, "push_base", push)
    result = env.land(out)
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["steps"][-1]["name"] == "push"
    assert "rejected" in payload["error"]
    assert git(env.origin, "rev-parse", "main") == competitor[0]
    assert not any(c.endswith("--json state") for c in env.calls())


@pytest.mark.parametrize(
    "condition,reason",
    [
        ("branch", "not_checked_out"),
        ("dirty", "dirty_checkout"),
        ("worktree", "other_worktree"),
        ("raises", "git_failed"),
    ],
)
def test_local_warnings_do_not_fail(env, tmp_path, monkeypatch, condition, reason):
    from helmraiser.pipeline.worktree import WorktreeManager

    out = env.new()
    before = git(env.project, "rev-parse", "main")
    if condition in ("branch", "worktree"):
        git(env.project, "checkout", "-b", "scratch")
        if condition == "worktree":
            git(env.project, "worktree", "add", str(tmp_path / "other"), "main")
    elif condition == "dirty":
        (env.project / "README.md").write_text("dirty")
    else:

        def fail(*a):
            raise RuntimeError("local failed")

        monkeypatch.setattr(WorktreeManager, "fast_forward_base", fail)
    result = env.land(out)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    local = payload["steps"][6]
    assert local["status"] == "warn" and reason in local["detail"]
    assert "git merge --ff-only" in local["detail"]
    assert git(env.project, "rev-parse", "main") == before
    assert git(env.origin, "rev-parse", "main") == payload["head"]
    if condition == "dirty":
        assert (env.project / "README.md").read_text() == "dirty"


def test_confirm_open_then_safe_rerun(env):
    out = env.new()
    env.state.write_text("OPEN")
    result = env.land(out)
    assert result.exit_code == 1, result.output
    assert "pushed, but the pull request still shows OPEN" in json.loads(result.stdout)["error"]
    head = git(env.project, "rev-parse", out["branch_name"])
    assert git(env.origin, "rev-parse", "main") == head
    env.state.write_text("auto")
    assert env.land(out).exit_code == 0
    assert sum(c.startswith("pr create") for c in env.calls()) == 1


def test_approved_closed_pr_stops_at_approve(env):
    out = env.new()
    env.checks.write_text("1|failed\n")
    assert env.land(out).exit_code == 1
    env.state.write_text("CLOSED")
    before_calls = len(env.calls())
    result = env.land(out)
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["steps"][-1]["name"] == "approve"
    assert "closed without merging" in result.output
    assert env.calls()[before_calls:] == ["pr view https://example.test/pr/1 --json state"]


def test_rebase_refusal_keeps_merge_approval(env):
    out = env.new(marker="README.md", content="run change\n")
    (env.project / "README.md").write_text("base change\n")
    git(env.project, "commit", "-am", "base change")
    result = env.land(out)
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["steps"][-1]["name"] == "approve"
    assert payload["error"].startswith("finalize_refused: rebase onto moved HEAD failed")
    assert f"helmraiser rework {out['run_id']} --rebase" in payload["error"]
    assert env.ledger.get_run(out["run_id"])["status"] == "awaiting_approval"
    assert env.ledger.pending_approval(out["run_id"])["decision"] == "pending"
    assert not Path(
        git(Path(out["worktree_path"]), "rev-parse", "--git-path", "rebase-merge")
    ).exists()
    assert env.calls() == []


@pytest.mark.parametrize("case", ["gh", "origin", "branch_name", "base_branch", "upstream"])
def test_preflight_refuses_before_approval(env, monkeypatch, case):
    out = env.new()
    if case == "gh":
        monkeypatch.setattr(landing.shutil, "which", lambda _: None)
    elif case == "origin":
        git(env.project, "remote", "remove", "origin")
    elif case == "upstream":
        git(env.project, "config", "branch.main.remote", "elsewhere")
    else:
        env.ledger.update_run(out["run_id"], **{case: None})
    result = env.land(out)
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["steps"][-1]["name"] == "preflight"
    assert env.ledger.get_run(out["run_id"])["status"] == "awaiting_approval"
    assert env.calls() == []


def test_invalid_gh_json(env):
    out = env.new()
    env.state.write_text("invalid")
    result = env.land(out)
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["steps"][-1]["name"] == "confirm"
    assert "invalid JSON" in result.output


def test_clean_json_and_merged_rerun(env):
    out = env.new()
    result = env.land({**out, "run_id": out["run_id"][:12]})
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    head = git(env.project, "rev-parse", out["branch_name"])
    assert payload == {
        "landed": True,
        "run_id": out["run_id"],
        "task_id": out["task_id"],
        "pr_url": "https://example.test/pr/1",
        "base_branch": "main",
        "branch_name": out["branch_name"],
        "head": head,
        "local": "updated",
        "steps": payload["steps"],
    }
    assert [s["name"] for s in payload["steps"]] == STEP_NAMES
    assert all(s["status"] == "ok" for s in payload["steps"])
    assert git(env.origin, "rev-parse", "main") == git(env.project, "rev-parse", "main") == head
    assert not Path(out["worktree_path"]).exists()
    row = env.ledger.get_run(out["run_id"])
    assert (
        row["status"] == "landed"
        and row["head_commit"] == head
        and row["pr_url"] == payload["pr_url"]
    )
    assert [line.split()[:2] for line in env.calls()] == [
        ["pr", "create"],
        ["pr", "view"],
        ["pr", "checks"],
        ["pr", "view"],
    ]
    # An externally merged PR must not refresh even an intentionally lagging local base.
    git(env.project, "reset", "--hard", "HEAD^")
    lagging = git(env.project, "rev-parse", "main")
    result = env.land(out)
    assert result.exit_code == 0, result.output
    assert all(s["status"] == "skipped" for s in json.loads(result.stdout)["steps"][2:])
    assert git(env.project, "rev-parse", "main") == lagging
    assert sum(c.startswith("pr create") for c in env.calls()) == 1


def test_human_progress_before_ci(env, monkeypatch):
    from helmraiser.cli import land_commands

    out = env.new()
    lines = []
    real_echo = land_commands.typer.echo

    def echo(text, **kw):
        lines.append(text)
        return real_echo(text, **kw)

    monkeypatch.setattr(land_commands.typer, "echo", echo)
    real_wait = landing.wait_for_checks

    def wait(*a, **kw):
        assert any(line.startswith("approve: ok: opened ") for line in lines)
        return real_wait(*a, **kw)

    monkeypatch.setattr(landing, "wait_for_checks", wait)
    result = env.land(out, json_output=False)
    assert result.exit_code == 0, result.output
    assert "confirm: ok: pull request MERGED" in result.output
    assert "https://example.test/pr/1\n" in result.output
    assert "\nopened " not in result.output


@pytest.mark.parametrize("blocks", ["push", "merge"])
def test_holds_before_approval(env, fake_lb, blocks):
    out = env.new()
    before = git(env.origin, "rev-parse", "main")
    holds = [
        {
            "id": "hold-1",
            "title": "Tunnel open",
            "release": "close tunnel " + "x" * 400,
            "until": "tomorrow",
        }
    ]
    fake_lb.write_script({f"check#{blocks}": {"exit": 3, "stdout": json.dumps(holds)}})
    result = env.land(out)
    assert result.exit_code == 3, result.output
    payload = json.loads(result.stdout)
    assert payload["holds"] == holds and "x" * 400 in payload["error"]
    assert payload["steps"][-1]["status"] == "blocked"
    assert env.calls() == []
    assert env.ledger.get_run(out["run_id"])["status"] == "awaiting_approval"
    assert env.ledger.pending_approval(out["run_id"])["decision"] == "pending"
    assert git(env.origin, "rev-parse", "main") == before


@pytest.mark.parametrize("missing", [False, True])
def test_lb_unavailable_warns_and_continues(env, fake_lb, monkeypatch, missing):
    out = env.new()
    if missing:
        monkeypatch.setattr(leeboard, "find_lb", lambda: None)
    else:
        fake_lb.write_script({"check": {"exit": 6, "stderr": "broken"}})
    result = env.land(out)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["steps"][1]["status"] == ("ok" if missing else "warn")


@pytest.mark.parametrize("case", ["merge", "plan", "scope", "none", "failed", "landed"])
def test_ineligible_changes_nothing(env, case):
    out = env.new()
    run_id = out["run_id"]
    if case == "merge":
        env.ledger.update_run(run_id, delivery="merge")
    elif case in ("plan", "scope", "none"):
        env.ledger.decide_approval(env.ledger.pending_approval(run_id)["id"], "approved")
        if case != "none":
            env.ledger.record_approval(run_id, case, "pending")
    else:
        env.ledger.update_run(run_id, status=case)
    before = env.ledger.get_run(run_id)
    result = env.land(out)
    assert result.exit_code == 2, result.output
    assert case in json.loads(result.stdout)["error"]
    assert env.ledger.get_run(run_id) == before
    assert env.calls() == []
