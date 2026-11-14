"""No-run base synchronization uses local origins and immutable ledger snapshots."""

from dataclasses import replace

import pytest

from helmraiser.cli import base_sync
from helmraiser.cli.base_sync import HeldBaseSync, base_sync_text
from helmraiser.ledger.db import Ledger, ProjectScope
from helmraiser.task.loader import render_task
from tests.ledger.test_db import make_spec, run_row
from tests.ledger.test_depends_on import feature, git


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "helmraiser.db")


def seed_pr_dependency_chain(ledger, project, *, base=None, delivery="pr"):
    """Merge upstream in a writer, leaving the known delivered head off local main."""
    origin = project.parent / f"{project.name}-origin.git"
    git(project, "branch", "-M", "main")
    git(project, "init", "--bare", str(origin))
    git(project, "remote", "add", "origin", str(origin))
    git(project, "push", "-u", "origin", "main")
    _, head = feature(project)
    git(project, "push", "origin", "feature:dependency")
    git(project, "branch", "-D", "feature")
    writer = project.parent / f"{project.name}-writer"
    git(project, "clone", "--branch", "main", str(origin), str(writer))
    git(writer, "config", "user.email", "test@example.com")
    git(writer, "config", "user.name", "Test")
    git(writer, "merge", "--no-ff", "origin/dependency", "-m", "merge PR")
    git(writer, "push", "origin", "main")
    upstream = make_spec(project, "upstream", status="landed", delivery="pr")
    dependent = make_spec(
        project, "dependent", depends_on=("upstream",), delivery=delivery, base=base
    )
    for spec in (upstream, dependent):
        ledger.upsert_task(spec, render_task(spec))
    ledger.create_run(
        {
            **run_row(f"dependency-{project.name}", upstream.task_id, str(project), "landed"),
            "head_commit": head,
        }
    )
    assert ledger.held_tasks()[dependent.task_id]["unmet"][0]["reason"] == "not_in_base"
    return dependent, origin


@pytest.mark.parametrize("base", [None, "main", "default"])
def test_real_sync_releases_hold(ledger, tmp_project, base):
    if base == "default":
        (tmp_project / ".helmraiser").mkdir(exist_ok=True)
        (tmp_project / ".helmraiser/project.toml").write_text('[defaults]\nbase = "main"\n')
        base = None
    dependent, _ = seed_pr_dependency_chain(ledger, tmp_project, base=base)
    outcomes = HeldBaseSync().check(ledger, ProjectScope.of())
    assert outcomes[0]["status"] == "synced"
    assert outcomes[0]["base_branch"] == "main"
    assert outcomes[0]["project"] == str(tmp_project)
    assert ledger.next_queued()["task_id"] == dependent.task_id
    assert len(ledger.list_runs()) == 1  # no synchronization run


def stub_sync(monkeypatch, result=None, error=None):
    calls = []

    class Manager:
        def sync_base(self, base):
            calls.append(base)
            if error:
                raise error
            return result or {"status": "skipped", "reason": "no_upstream", "base_branch": base}

    monkeypatch.setattr(base_sync, "worktree_manager", lambda project: Manager())
    return calls


@pytest.mark.parametrize("interval", [300, 12])
def test_cooldown_aliases_and_independent_bases(ledger, tmp_project, monkeypatch, interval):
    dependent, _ = seed_pr_dependency_chain(ledger, tmp_project)
    git(tmp_project, "branch", "other")
    for id, base in [("explicit", "main"), ("other", "other")]:
        spec = replace(dependent, id=id, base=base)
        ledger.upsert_task(spec, render_task(spec))
    calls = stub_sync(monkeypatch)
    now = [0.0]
    sync = HeldBaseSync(interval, clock=lambda: now[0])
    assert len(sync.check(ledger, ProjectScope.of())) == 2
    assert calls == ["main", "other"]
    now[0] = interval - 0.01
    assert sync.check(ledger, ProjectScope.of()) == []
    now[0] = interval
    assert len(sync.check(ledger, ProjectScope.of())) == 2
    assert calls == ["main", "other"] * 2


@pytest.mark.parametrize(
    "hold",
    [
        {},
        {"reason": "depends_on", "unmet": []},
        {"reason": "depends_on", "unmet": [{"reason": "not_in_base"}, {"reason": "unknown"}]},
        *[
            {"reason": "depends_on", "unmet": [{"reason": reason}]}
            for reason in [
                "unknown",
                "not_landed",
                "failed",
                "aborted",
                "missing_head",
                "cycle",
                "invalid",
                "base_unavailable",
                "git_error",
            ]
        ],
        {"reason": "depends_on", "run_id": "holder", "unmet": [{"reason": "not_in_base"}]},
    ],
)
def test_ineligible_holds_never_sync(ledger, tmp_project, monkeypatch, hold):
    dependent, _ = seed_pr_dependency_chain(ledger, tmp_project)
    monkeypatch.setattr(ledger, "held_tasks", lambda **kwargs: {dependent.task_id: hold})
    calls = stub_sync(monkeypatch)
    assert HeldBaseSync().check(ledger, ProjectScope.of()) == []
    assert calls == []


@pytest.mark.parametrize(
    "exclusion",
    [
        "paused",
        "scope",
        "merge",
        "running",
        "awaiting_approval",
        "resuming",
        "planning",
        "reviewing",
    ],
)
def test_safety_exclusions(ledger, tmp_project, monkeypatch, exclusion):
    dependent, _ = seed_pr_dependency_chain(
        ledger, tmp_project, delivery="merge" if exclusion == "merge" else "pr"
    )
    scope = ProjectScope.of(exclude=[tmp_project]) if exclusion == "scope" else ProjectScope.of()
    if exclusion == "paused":
        ledger.pause_queue("test")
    elif exclusion not in {"scope", "merge"}:
        ledger.create_run(run_row("holder", dependent.task_id, str(tmp_project), exclusion))
    calls = stub_sync(monkeypatch)
    assert HeldBaseSync().check(ledger, scope) == []
    assert calls == []


def test_rechecks_pause_and_runs_without_consuming_cooldown(ledger, tmp_project, monkeypatch):
    dependent, _ = seed_pr_dependency_chain(ledger, tmp_project)
    holds = ledger.held_tasks()
    monkeypatch.setattr(ledger, "held_tasks", lambda **kwargs: holds)
    calls = stub_sync(monkeypatch)
    sync = HeldBaseSync(clock=lambda: 0)
    ledger.create_run(run_row("holder", dependent.task_id, str(tmp_project), "awaiting_approval"))
    assert sync.check(ledger, ProjectScope.of()) == []
    ledger.finish_run("holder", "failed")
    assert len(sync.check(ledger, ProjectScope.of())) == 1
    assert calls == ["main"]
    paused = iter([None, {"reason": "test"}])
    monkeypatch.setattr(ledger, "is_paused", lambda: next(paused))
    assert HeldBaseSync().check(ledger, ProjectScope.of()) == []


@pytest.mark.parametrize("raised", [False, True])
def test_failure_is_visible_rate_limited_and_other_project_continues(
    ledger, tmp_project, tmp_path, git_init, monkeypatch, raised
):
    _, origin = seed_pr_dependency_chain(ledger, tmp_project)
    other = tmp_path / "other"
    other.mkdir()
    git_init(other)
    seed_pr_dependency_chain(ledger, other)
    git(tmp_project, "remote", "set-url", "origin", str(origin / "missing"))
    if raised:
        real = base_sync.worktree_manager

        def manager(project):
            if project == tmp_project:
                raise RuntimeError("unexpected manager error")
            return real(project)

        monkeypatch.setattr(base_sync, "worktree_manager", manager)
    now = [0.0]
    sync = HeldBaseSync(clock=lambda: now[0])
    outcomes = sync.check(ledger, ProjectScope.of())
    assert [o["status"] for o in outcomes] == ["failed", "synced"]
    assert "detail" in outcomes[0]
    text = base_sync_text(outcomes[0])
    assert str(tmp_project) in text and "main" in text and "failed" in text
    assert outcomes[0]["detail"] in text
    assert sync.check(ledger, ProjectScope.of()) == []
    now[0] = 300
    assert len(sync.check(ledger, ProjectScope.of())) == 1


@pytest.mark.parametrize("failure", ["parse", "base"])
def test_candidate_errors_are_rate_limited(ledger, tmp_project, monkeypatch, failure):
    dependent, _ = seed_pr_dependency_chain(ledger, tmp_project)
    holds = ledger.held_tasks()
    monkeypatch.setattr(ledger, "held_tasks", lambda **kwargs: holds)
    if failure == "parse":

        def parse(*args):
            raise ValueError("bad snapshot")

        monkeypatch.setattr(base_sync, "parse_task", parse)
    else:
        git(tmp_project, "checkout", "--detach")
    calls = stub_sync(monkeypatch)
    sync = HeldBaseSync(clock=lambda: 0)
    result = sync.check(ledger, ProjectScope.of())
    assert result[0]["status"] == "failed"
    assert result[0]["base_branch"] is None
    assert sync.check(ledger, ProjectScope.of()) == []
    assert calls == []
    assert ledger.get_task(dependent.task_id)["status"] == "queued"
