"""Dependency eligibility uses immutable snapshots and real local git ancestry."""

import subprocess
from pathlib import Path

import pytest

from helmraiser.ledger.dependencies import dependency_holds, dependency_relation
from helmraiser.task.loader import render_task
from tests.ledger.test_db import make_spec, run_row


@pytest.mark.parametrize(
    "dependencies, expected",
    [
        ({"a": ("b",), "b": ()}, True),
        ({"a": ("c",), "c": ("b",), "b": ()}, True),
        ({"a": ("c",), "b": ("c",), "c": ()}, False),
        ({"a": ("c",), "c": ("a",), "b": ()}, None),
        ({"a": ("unknown",), "b": ()}, None),
        ({"a": (), "b": ()}, False),
    ],
)
def test_concurrency_dependency_relation(tmp_project, dependencies, expected):
    rows = [task(tmp_project, id, deps) for id, deps in dependencies.items()]
    assert dependency_relation(rows, "a", "b") is expected
    assert dependency_relation(rows, "b", "a") is expected


def test_concurrency_malformed_snapshot(tmp_project):
    a, b = task(tmp_project, "a", ()), task(tmp_project, "b", ())
    a["snapshot"] = "---\npaths: [\n---\n"
    assert dependency_relation([a, b], "a", "b") is None
    a["snapshot"] = ""
    assert dependency_relation([a, b], "a", "b") is False


def git(project, *args):
    return subprocess.run(
        ["git", *args], cwd=project, capture_output=True, text=True, check=True
    ).stdout.strip()


def task(project, id="dependent", dependencies=("upstream",), **kwargs):
    spec = make_spec(project, id, depends_on=dependencies, **kwargs)
    return {
        "task_id": spec.task_id,
        "id": id,
        "project": str(project),
        "snapshot": render_task(spec),
        "source_path": str(spec.source_path),
        "status": spec.status,
    }


def run(upstream, status="landed", head=None, time=1):
    return {
        **run_row(f"run-{time}", upstream["task_id"], upstream["project"], status),
        "head_commit": head,
        "started_at": time,
        "delivery": "pr",
    }


def evaluate(dependent, tasks, runs=()):
    return dependency_holds([dependent, *tasks], list(runs), candidates=[dependent])


def reasons(holds):
    return [item["reason"] for hold in holds.values() for item in hold["unmet"]]


@pytest.mark.parametrize("status", ["queued", "landed"])
def test_depends_on_needs_landed_run(tmp_project, status):
    dependent = task(tmp_project)
    upstream = task(tmp_project, "upstream", (), status=status)
    assert reasons(evaluate(dependent, [upstream])) == ["not_landed"]


def test_depends_on_unknown_and_project_boundary(tmp_project, tmp_path):
    dependent = task(tmp_project)
    assert reasons(evaluate(dependent, [])) == ["unknown"]
    assert reasons(evaluate(dependent, [task(tmp_path, "upstream", ())])) == ["unknown"]
    upstream = task(tmp_project, "upstream", ())
    assert reasons(evaluate(dependent, [upstream])) == ["not_landed"]


@pytest.mark.parametrize("status", ["failed", "aborted"])
def test_depends_on_latest_failure_overrides_land(tmp_project, status):
    dependent, upstream = task(tmp_project), task(tmp_project, "upstream", ())
    head = git(tmp_project, "rev-parse", "HEAD")
    runs = [run(upstream, head=head), run(upstream, status, time=2)]
    holds = evaluate(dependent, [upstream], runs)
    assert reasons(holds) == [status]
    assert "run-2" in holds[dependent["task_id"]]["unmet"][0]["text"]
    assert not evaluate(dependent, [upstream], [*runs, run(upstream, head=head, time=3)])


def test_depends_on_missing_head(tmp_project):
    dependent, upstream = task(tmp_project), task(tmp_project, "upstream", ())
    assert reasons(evaluate(dependent, [upstream], [run(upstream)])) == ["missing_head"]


def feature(project):
    base = git(project, "branch", "--show-current")
    git(project, "checkout", "-b", "feature")
    (project / "feature.txt").write_text("feature\n")
    git(project, "add", "feature.txt")
    git(project, "commit", "-m", "feature")
    head = git(project, "rev-parse", "HEAD")
    git(project, "checkout", base)
    return base, head


def test_depends_on_pr_local_catchup_and_squash(tmp_project):
    base, head = feature(tmp_project)
    dependent, upstream = task(tmp_project), task(tmp_project, "upstream", ())
    runs = [run(upstream, head=head)]
    assert reasons(evaluate(dependent, [upstream], runs)) == ["not_in_base"]
    # Equivalent content is not ancestry.
    git(tmp_project, "merge", "--squash", "feature")
    git(tmp_project, "commit", "-m", "squash")
    assert reasons(evaluate(dependent, [upstream], runs)) == ["not_in_base"]
    # Simulated remote base advances, but local base is still stale.
    git(tmp_project, "checkout", "-b", "remote-base")
    git(tmp_project, "merge", "--no-ff", "feature", "-m", "merge PR")
    merge = git(tmp_project, "rev-parse", "HEAD")
    git(tmp_project, "checkout", base)
    assert reasons(evaluate(dependent, [upstream], runs)) == ["not_in_base"]
    git(tmp_project, "merge", "--ff-only", merge)
    assert not evaluate(dependent, [upstream], runs)


def test_depends_on_explicit_default_and_detached_base(tmp_project):
    base, head = feature(tmp_project)
    upstream = task(tmp_project, "upstream", ())
    runs = [run(upstream, head=head)]
    dependent = task(tmp_project, base=base)
    git(tmp_project, "checkout", "feature")
    assert reasons(evaluate(dependent, [upstream], runs)) == ["not_in_base"]
    assert not evaluate(task(tmp_project), [upstream], runs)
    git(tmp_project, "checkout", "--detach")
    assert reasons(evaluate(task(tmp_project), [upstream], runs)) == ["base_unavailable"]
    assert not evaluate(task(tmp_project, base="feature"), [upstream], runs)
    (tmp_project / ".helmraiser").mkdir(exist_ok=True)
    (tmp_project / ".helmraiser/project.toml").write_text('[defaults]\nbase = "feature"\n')
    assert not evaluate(task(tmp_project), [upstream], runs)


@pytest.mark.parametrize(
    "edges",
    [
        {"upstream": ("dependent",)},
        {"upstream": ("middle",), "middle": ("upstream",)},
        {"upstream": ("middle",), "middle": ("last",), "last": ("upstream",)},
    ],
)
def test_depends_on_cycles(tmp_project, edges):
    dependent = task(tmp_project)
    holds = evaluate(dependent, [task(tmp_project, id, deps) for id, deps in edges.items()])
    assert reasons(holds) == ["cycle"]
    assert " -> " in holds[dependent["task_id"]]["unmet"][0]["text"]


def test_depends_on_diamond_and_multiple_unmet(tmp_project):
    dependent = task(tmp_project, dependencies=("left", "right", "unknown", "left"))
    tasks = [
        task(tmp_project, "left", ("shared",)),
        task(tmp_project, "right", ("shared",)),
        task(tmp_project, "shared", ()),
    ]
    holds = evaluate(dependent, tasks, [run(tasks[0], head=git(tmp_project, "rev-parse", "HEAD"))])
    assert reasons(holds) == ["not_landed", "unknown"]
    assert [item["id"] for item in holds[dependent["task_id"]]["unmet"]] == ["right", "unknown"]


def test_depends_on_snapshot_not_source(tmp_project):
    dependent = task(tmp_project)
    Path(dependent["source_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(dependent["source_path"]).write_text(render_task(make_spec(tmp_project)))
    assert reasons(evaluate(dependent, [])) == ["unknown"]


@pytest.mark.parametrize(
    "snapshot", ["---\ndepends_on: null\n---\n", "---\ndepends_on: [Bad]\n---\n"]
)
def test_depends_on_invalid_snapshot(tmp_project, snapshot):
    dependent = task(tmp_project)
    dependent["snapshot"] = snapshot
    assert reasons(evaluate(dependent, [])) == ["invalid"]


def test_depends_on_git_errors(tmp_project, monkeypatch):
    dependent, upstream = task(tmp_project), task(tmp_project, "upstream", ())
    assert reasons(evaluate(dependent, [upstream], [run(upstream, head="0" * 40)])) == ["git_error"]
    assert reasons(
        evaluate(task(tmp_project, base="missing"), [upstream], [run(upstream, head="abc")])
    ) == ["base_unavailable"]

    def broken(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr("helmraiser.ledger.dependencies.subprocess.run", broken)
    assert reasons(evaluate(dependent, [upstream], [run(upstream, head="abc")])) == [
        "base_unavailable"
    ]


def test_depends_on_legacy_snapshots(tmp_project):
    for snapshot in ("", "snapshot"):
        dependent = task(tmp_project)
        dependent["snapshot"] = snapshot
        assert not evaluate(dependent, [])
