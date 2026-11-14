"""Landing mechanics use real local git and fake GitHub responses only."""

import subprocess

import pytest

from helmraiser.loop import landing
from tests.pipeline.test_worktree import git


@pytest.fixture
def remote(tmp_project, tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_project, "remote", "add", "origin", str(origin))
    git(tmp_project, "push", "origin", "main")
    git(tmp_project, "checkout", "-b", "run")
    (tmp_project / "result.txt").write_text("result")
    git(tmp_project, "add", "result.txt")
    git(tmp_project, "commit", "-m", "result")
    git(tmp_project, "checkout", "main")
    landing.push_branch(tmp_project, "run")
    return tmp_project, origin


def test_sync_and_push(remote):
    project, origin = remote
    landing.preflight(project, "run", "main")
    head = landing.sync_head(project, "run", "main", "id")
    landing.push_base(project, "main", head)
    assert git(origin, "rev-parse", "main") == head
    assert git(project, "rev-parse", "main") != head
    assert set(git(origin, "for-each-ref", "--format=%(refname)").splitlines()) == {
        "refs/heads/main",
        "refs/heads/run",
    }


@pytest.mark.parametrize("target", ["branch", "base"])
def test_landing_push_does_not_follow_tags(remote, target):
    project, origin = remote
    git(project, "config", "push.followTags", "true")
    git(project, "checkout", "run")
    (project / "result.txt").write_text("new head")
    git(project, "commit", "-am", "new head")
    head = git(project, "rev-parse", "HEAD")
    git(project, "tag", "-a", "unpublished", "-m", "Keep this tag local", head)
    assert git(origin, "for-each-ref", "refs/tags") == ""

    if target == "branch":
        landing.push_branch(project, "run")
        assert git(origin, "rev-parse", "run") == head
    else:
        landing.push_base(project, "main", head)
        assert git(origin, "rev-parse", "main") == head

    assert git(project, "rev-parse", "unpublished^{commit}") == head
    assert set(git(origin, "for-each-ref", "--format=%(refname)").splitlines()) == {
        "refs/heads/main",
        "refs/heads/run",
    }


@pytest.mark.parametrize("destination", ["main", "forbidden"])
def test_approval_push_ignores_configured_destination(remote, destination):
    project, origin = remote
    before = git(origin, "rev-parse", "main")
    git(project, "checkout", "run")
    (project / "result.txt").write_text("new head")
    git(project, "commit", "-am", "new head")
    head = git(project, "rev-parse", "HEAD")
    git(project, "config", "remote.origin.push", f"refs/heads/run:refs/heads/{destination}")

    landing.push_branch(project, "run")

    assert git(origin, "rev-parse", "main") == before
    assert git(origin, "rev-parse", "run") == head
    assert git(project, "rev-parse", "run@{upstream}") == head
    assert git(project, "config", "branch.run.merge") == "refs/heads/run"
    assert set(git(origin, "for-each-ref", "--format=%(refname)").splitlines()) == {
        "refs/heads/main",
        "refs/heads/run",
    }


@pytest.mark.parametrize("kind", ["remote", "merge", "multiple"])
def test_preflight_incompatible_upstream(remote, kind):
    project, _ = remote
    git(project, "config", "branch.main.remote", "elsewhere" if kind == "remote" else "origin")
    git(
        project,
        "config",
        "branch.main.merge",
        "refs/heads/other" if kind == "merge" else "refs/heads/main",
    )
    if kind == "multiple":
        git(project, "config", "--add", "branch.main.merge", "refs/heads/other")
    with pytest.raises(RuntimeError, match="requires origin/main"):
        landing.preflight(project, "run", "main")


def test_sync_origin_ahead(remote):
    project, _ = remote
    (project / "README.md").write_text("moved")
    git(project, "commit", "-am", "moved")
    git(project, "push", "origin", "main")
    with pytest.raises(RuntimeError, match="helmraiser rework id --rebase"):
        landing.sync_head(project, "run", "main", "id")


@pytest.mark.parametrize("missing", [True, False])
def test_sync_remote_head_mismatch(remote, missing):
    project, origin = remote
    before = git(origin, "rev-parse", "main")
    local = git(project, "rev-parse", "run")
    if missing:
        git(origin, "update-ref", "-d", "refs/heads/run")
    else:
        git(origin, "update-ref", "refs/heads/run", before)
    with pytest.raises(RuntimeError) as caught:
        landing.sync_head(project, "run", "main", "id")
    assert local in str(caught.value)
    assert ("missing" if missing else before) in str(caught.value)


def test_push_rejected_after_sync(remote):
    project, origin = remote
    head = landing.sync_head(project, "run", "main", "id")
    (project / "README.md").write_text("competitor")
    git(project, "commit", "-am", "competitor")
    git(project, "push", "origin", "main")
    before = git(origin, "rev-parse", "main")
    with pytest.raises(RuntimeError, match="rejected"):
        landing.push_base(project, "main", head)
    assert git(origin, "rev-parse", "main") == before


@pytest.mark.parametrize(
    "output,expected", [("not json", "invalid JSON"), ("{}", "unexpected"), ("[]", "unexpected")]
)
def test_invalid_pr_state(tmp_path, monkeypatch, output, expected):
    monkeypatch.setattr(landing, "_checked", lambda *a: output)
    with pytest.raises(RuntimeError, match=expected):
        landing.pr_state(tmp_path, "url")


def test_bounded_subprocess(tmp_path, monkeypatch):
    def run(argv, **kwargs):
        assert kwargs["timeout"] == 60
        assert kwargs["shell"] is False
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        raise subprocess.TimeoutExpired(argv, 60)

    monkeypatch.setattr(landing.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="timed out"):
        landing.push_base(tmp_path, "main", "sha")


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0.0
        sleeps = []

        def tick(self):
            self.now += 0.1
            return self.now

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(landing, "monotonic", clock.tick)
    monkeypatch.setattr(landing, "sleep", clock.sleep)
    return clock


def checks(monkeypatch, responses):
    answers = iter(responses)
    calls = []

    def run(argv, cwd, **kwargs):
        calls.append(argv)
        assert 0 < kwargs["timeout"] <= landing.COMMAND_TIMEOUT
        code, text = next(answers)
        return subprocess.CompletedProcess(argv, code, "", text)

    monkeypatch.setattr(landing, "_run", run)
    return calls


@pytest.mark.parametrize("no_ci", [False, True])
def test_expected_head_never_visible_times_out_without_checking_ci(
    tmp_path, monkeypatch, clock, no_ci
):
    calls = []

    def run(argv, cwd, **kwargs):
        calls.append(argv)
        assert 0 < kwargs["timeout"] <= 1
        return subprocess.CompletedProcess(argv, 0, '{"headRefOid":"old"}', "")

    monkeypatch.setattr(landing, "_run", run)
    with pytest.raises(RuntimeError, match="head has not reached new.*after 1 s"):
        landing.wait_for_checks(
            tmp_path, "url", "id", expected_head="new", timeout=1, interval=0, no_ci=no_ci
        )
    assert calls and all(
        argv == ["gh", "pr", "view", "url", "--json", "headRefOid"] for argv in calls
    )


@pytest.mark.parametrize(
    "output,code,message",
    [
        ("not json", 0, "invalid JSON"),
        ("{}", 0, "unexpected pull request head"),
        ('{"headRefOid":false}', 0, "unexpected pull request head"),
        ("offline", 1, "offline"),
    ],
)
def test_head_query_errors_are_reported(tmp_path, monkeypatch, clock, output, code, message):
    monkeypatch.setattr(
        landing, "_run", lambda argv, *a, **kw: subprocess.CompletedProcess(argv, code, output, "")
    )
    with pytest.raises(RuntimeError, match=message):
        landing.wait_for_checks(tmp_path, "url", "id", expected_head="new", interval=0)


def test_head_wait_and_checks_share_deadline(tmp_path, monkeypatch, clock):
    calls = []

    def run(argv, cwd, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        if "view" in argv:
            clock.sleep(0.5)
            return subprocess.CompletedProcess(argv, 0, '{"headRefOid":"new"}', "")
        return subprocess.CompletedProcess(argv, 8, "pending", "")

    monkeypatch.setattr(landing, "_run", run)
    with pytest.raises(RuntimeError, match="checks still pending after 1 s"):
        landing.wait_for_checks(tmp_path, "url", "id", expected_head="new", timeout=1, interval=0)
    assert calls[0][0][1:3] == ["pr", "view"]
    assert calls[1][0][1:3] == ["pr", "checks"] and calls[1][1] < 0.5


def test_pending_then_pass(tmp_path, monkeypatch, clock):
    calls = checks(monkeypatch, [(8, "pending"), (8, "pending"), (0, "passed")])
    assert landing.wait_for_checks(tmp_path, "url", "id", interval=0) == "passed"
    assert calls == [["gh", "pr", "checks", "url"]] * 3


@pytest.mark.parametrize("no_ci", [False, True])
def test_existing_failed_checks_never_skipped(tmp_path, monkeypatch, clock, no_ci):
    checks(monkeypatch, [(1, "test failed")])
    with pytest.raises(RuntimeError, match="test failed"):
        landing.wait_for_checks(tmp_path, "url", "id", no_ci=no_ci)


def test_timeout(tmp_path, monkeypatch, clock):
    calls = checks(monkeypatch, [(8, "pending")] * 20)
    with pytest.raises(
        RuntimeError, match="checks still pending after 1 s; run helmraiser land id again"
    ):
        landing.wait_for_checks(tmp_path, "url", "id", timeout=1, interval=0)
    assert calls


def test_zero_timeout_does_not_poll(tmp_path, monkeypatch, clock):
    calls = checks(monkeypatch, [])
    with pytest.raises(RuntimeError, match="after 0 s"):
        landing.wait_for_checks(tmp_path, "url", "id", timeout=0)
    assert not calls


@pytest.mark.parametrize("no_ci", [False, True])
def test_no_checks_grace(tmp_path, monkeypatch, clock, no_ci):
    monkeypatch.setattr(landing, "NO_CHECKS_GRACE", 1)
    checks(monkeypatch, [(1, "No Checks Reported")] * 20)
    if no_ci:
        assert "--no-ci" in landing.wait_for_checks(tmp_path, "url", "id", no_ci=True)
    else:
        with pytest.raises(RuntimeError, match="use --no-ci"):
            landing.wait_for_checks(tmp_path, "url", "id", interval=0)


def test_no_checks_overall_deadline(tmp_path, monkeypatch, clock):
    checks(monkeypatch, [(1, "no checks reported")] * 20)
    with pytest.raises(RuntimeError, match="after 1 s"):
        landing.wait_for_checks(tmp_path, "url", "id", timeout=1, interval=0)


def test_ci_registers_during_grace(tmp_path, monkeypatch, clock):
    checks(monkeypatch, [(1, "no checks reported"), (8, "pending"), (0, "passed")])
    assert landing.wait_for_checks(tmp_path, "url", "id", interval=0) == "passed"


@pytest.mark.parametrize(
    "states,error",
    [
        (["OPEN", "MERGED"], None),
        (["OPEN"] * 10, "still shows OPEN"),
        (["CLOSED"], "closed without merging"),
    ],
)
def test_confirm(tmp_path, monkeypatch, clock, states, error):
    answers = iter(states)
    monkeypatch.setattr(landing, "pr_state", lambda *a: next(answers))
    if error:
        with pytest.raises(RuntimeError, match=error):
            landing.confirm_merged(tmp_path, "url")
    else:
        landing.confirm_merged(tmp_path, "url")
    assert len(clock.sleeps) == len(states) - 1


@pytest.fixture
def operation(monkeypatch, tmp_path):
    from helmraiser.logbook import HoldCheck

    calls = []
    for name in ("preflight", "push_base", "confirm_merged"):
        monkeypatch.setattr(landing, name, lambda *a, _name=name: calls.append(_name))
    monkeypatch.setattr(landing, "check_hold", lambda *a, **kw: HoldCheck("clear"))
    monkeypatch.setattr(landing, "wait_for_checks", lambda *a, **kw: "passed")
    monkeypatch.setattr(landing, "sync_head", lambda *a: "sha")
    monkeypatch.setattr(landing, "pr_state", lambda *a: "OPEN")

    class Manager:
        def fast_forward_base(self, *a):
            calls.append("local")
            return {"status": "updated", "reason": "fast_forward"}

    row = {
        "run_id": "id",
        "task_id": "task",
        "status": "awaiting_approval",
        "branch_name": "run",
        "base_branch": "main",
    }

    def approve():
        calls.append("approve")
        return "url", "sha"

    def run(**kw):
        return landing.land_run(row, tmp_path, Manager(), approve, **kw)

    return row, calls, run


def test_ordered_operation(operation):
    row, calls, run = operation
    progress = []
    result = run(progress=progress.append)
    assert result.exit_code == 0
    assert [s["name"] for s in progress] == [
        "preflight",
        "logbook",
        "approve",
        "ci",
        "sync",
        "push",
        "local",
        "confirm",
    ]
    assert result.payload["steps"] == progress
    assert result.payload["head"] == "sha" and result.payload["local"] == "updated"
    assert calls == ["preflight", "approve", "push_base", "local", "confirm_merged"]


@pytest.mark.parametrize(
    "name,step",
    [
        ("preflight", "preflight"),
        ("wait_for_checks", "ci"),
        ("sync_head", "sync"),
        ("push_base", "push"),
        ("confirm_merged", "confirm"),
    ],
)
def test_first_failure(operation, monkeypatch, name, step):
    _, calls, run = operation

    def fail(*a, **kw):
        raise RuntimeError("refused")

    monkeypatch.setattr(landing, name, fail)
    result = run()
    assert result.exit_code == 1
    assert result.payload["steps"][-1] == {"name": step, "status": "failed", "detail": "refused"}
    if step != "confirm":
        assert "confirm_merged" not in calls


@pytest.mark.parametrize("state,code", [("OPEN", 0), ("MERGED", 0), ("CLOSED", 1)])
def test_resumed_operation(operation, monkeypatch, state, code):
    row, calls, run = operation
    row.update(status="landed", pr_url="url", head_commit="recorded")
    monkeypatch.setattr(landing, "pr_state", lambda *a: state)
    result = run()
    assert result.exit_code == code
    assert "approve" not in calls
    if state == "MERGED":
        assert calls == ["preflight"]
        assert result.payload["head"] == "recorded"
        assert all(s["status"] == "skipped" for s in result.payload["steps"][2:])
    elif state == "CLOSED":
        assert result.payload["steps"][-1]["name"] == "approve"


@pytest.mark.parametrize("blocked", ["push", "merge"])
def test_operation_hold(operation, monkeypatch, blocked):
    from helmraiser.logbook import HoldCheck

    _, calls, run = operation
    tags = []

    def check(project, blocks):
        tags.append(blocks)
        return HoldCheck("blocked", text="hold") if blocks == blocked else HoldCheck("clear")

    monkeypatch.setattr(landing, "check_hold", check)
    result = run()
    assert result.exit_code == 3
    assert result.payload["steps"][-1]["status"] == "blocked"
    assert calls == ["preflight"]
    assert tags == (["push"] if blocked == "push" else ["push", "merge"])


def test_operation_unavailable_warns(operation, monkeypatch):
    from helmraiser.logbook import HoldCheck

    monkeypatch.setattr(
        landing, "check_hold", lambda *a, **kw: HoldCheck("unavailable", detail="broken")
    )
    result = operation[2]()
    assert result.exit_code == 0
    assert result.payload["steps"][1]["status"] == "warn"
    assert "push: broken; merge: broken" in result.payload["steps"][1]["detail"]


def test_os_error(tmp_path, monkeypatch):
    def run(*a, **kw):
        raise OSError("unavailable")

    monkeypatch.setattr(landing.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="unavailable"):
        landing.pr_state(tmp_path, "url")
