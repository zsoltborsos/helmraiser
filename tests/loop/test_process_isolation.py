"""``isolation: process`` through ``GovernedRun``: the pi argv is srt-wrapped, and an
abort that srt turns into a clean exit still closes the run as aborted."""

from __future__ import annotations

import dataclasses
import json
import shlex
import sys
from pathlib import Path

import pytest

from helmraiser.cli.commands import make_adapter_factory
from helmraiser.isolation.process import (
    SETTINGS_FILENAME,
    SETUP_SETTINGS_FILENAME,
    ProcessIsolation,
)
from helmraiser.loop.runner import GovernedRun
from helmraiser.task.model import EvidenceStep, VerifyStep, project_slug
from tests.conftest import FakePi
from tests.loop.conftest import FakeSleep
from tests.mocks.fake_srt import FakeSrt, unwrap_guard
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

REVIEW_PASS = 'ok\n\n```json\n{"verdict": "pass", "findings": [], "evidence_checks": []}\n```'

# Stands in for srt: logs its argv, checks the settings file, runs the command after ``--``.
FAKE_SRT = """import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_SRT_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")
assert argv[0] == "--settings" and os.path.isfile(argv[1]) and argv[2] == "--", argv
os.execvp(argv[3], argv[3:])
"""


@pytest.fixture
def fake_srt(fake_pi: FakePi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "fake_srt.py"
    source.write_text(FAKE_SRT)
    wrapper = fake_pi.bin.parent / "srt"  # same bin dir, already on PATH
    log = tmp_path / "fake_srt.log"
    wrapper.write_text(
        f"#!/bin/sh\nFAKE_SRT_LOG={shlex.quote(str(log))}"
        f' exec {shlex.quote(sys.executable)} {shlex.quote(str(source))} "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return log


def retry_run(
    make_task, config, ledger, data_dir, tmp_path, fake_pi, monkeypatch, kind, rpc=False, failures=1
):
    config["pi_rpc_implement"] = rpc
    sibling = data_dir / "runs" / "vanished sibling"
    sibling.mkdir(parents=True)
    filename = "srt-settings.json" if kind == "session" else f"srt-settings-{kind}.json"
    fake = FakeSrt(tmp_path / "retry-srt", sibling, kind=filename, failures=failures)
    config["isolation"] = {"srt": str(fake.bin), "tmpdir": str(tmp_path / "sandbox-temp")}
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    task = dataclasses.replace(
        make_task(setup="echo setup >> setup-counter"),
        isolation="process",
        verify=(VerifyStep("ok", "test -f done.txt; echo verify >> verify-counter"),),
        evidence=(
            EvidenceStep(
                "proof",
                'echo evidence >> evidence-counter; echo p > "$HELMRAISER_EVIDENCE_DIR/p.txt"',
                ("p.txt",),
                "p",
            ),
        ),
    )
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {
                    "text": "Implemented.",
                    "files": [["done.txt", "ok\n"]],
                    "cost": 0.25,
                },
                "*-review": {"text": REVIEW_PASS},
            }
        }
    )
    isolation = ProcessIsolation(config, data_dir=data_dir)
    isolation.check_available = lambda: None
    sleep = FakeSleep()
    run = GovernedRun(
        task,
        config=config,
        ledger=ledger,
        adapter_factory=make_adapter_factory(config),
        pi_version="0.87.1",
        data_dir=data_dir,
        snapshot=task.source_path.read_text(),
        auth_check=lambda model: None,
        isolation=isolation,
        sleep=sleep,
    )
    return run, fake, sibling, sleep, filename


@pytest.mark.parametrize(
    "kind,rpc",
    [
        ("setup", False),
        ("verify", False),
        ("evidence", False),
        ("session", False),
        ("session", True),
    ],
)
async def test_governed_sandbox_retry_is_durable(
    make_task, config, ledger, data_dir, tmp_path, fake_pi, monkeypatch, kind, rpc
):
    run, fake, sibling, sleep, filename = retry_run(
        make_task, config, ledger, data_dir, tmp_path, fake_pi, monkeypatch, kind, rpc
    )
    outcome = await run.run()
    assert outcome.status == "awaiting_approval", outcome
    calls = [c for c in fake.calls() if c["kind"] == filename]
    # Sessions also include one later review launch.
    assert len(calls) == (3 if kind == "session" else 2)
    assert str(sibling) in calls[0]["settings"]["filesystem"]["denyRead"]
    assert str(sibling) not in calls[1]["settings"]["filesystem"]["denyRead"]
    expected = {"kind": kind, "vanished_path": str(sibling), "attempt": 2}
    events = ledger.events(run.run_id)
    assert [e["payload"] for e in events if e["type"] == "sandbox_retry"] == [expected]
    local = [json.loads(line) for line in run.rundir.events_jsonl.read_text().splitlines()]
    [event] = [e for e in local if e["event_type"] == "sandbox_retry"]
    assert event["payload"] == expected
    assert not any(e["type"] == "retry" for e in events)
    assert sleep.calls == []
    worktree = Path(outcome.worktree_path)
    for stage in ("setup", "verify", "evidence"):
        assert (worktree / f"{stage}-counter").read_text() == f"{stage}\n"
    assert (
        len(
            [
                c
                for c in fake_pi.calls()
                if c["session_id"] and c["session_id"].endswith("-implement")
            ]
        )
        == 1
    )
    assert not list(run.isolation.tmpdir.glob("*.marker"))


@pytest.mark.parametrize("rpc", [False, True])
async def test_exhausted_pi_start_is_terminal(
    make_task, config, ledger, data_dir, tmp_path, fake_pi, monkeypatch, rpc
):
    run, fake, sibling, sleep, filename = retry_run(
        make_task,
        config,
        ledger,
        data_dir,
        tmp_path,
        fake_pi,
        monkeypatch,
        "session",
        rpc,
        failures=20,
    )
    outcome = await run.run()
    assert outcome.status == "failed"
    assert "sandbox" in outcome.failure_reason and str(sibling) in outcome.failure_reason
    assert len([c for c in fake.calls() if c["kind"] == filename]) == 2
    assert [c for c in fake_pi.calls() if c["session_id"]] == []
    assert sleep.calls == []
    events = ledger.events(run.run_id)
    assert len([e for e in events if e["type"] == "sandbox_retry"]) == 1
    assert not any(e["type"] in ("retry", "escalated", "no_changes") for e in events)
    [iteration] = ledger.iterations(run.run_id)
    assert iteration["agent_ok"] == 0 and iteration["ended_at"] is not None
    assert run.state.get_process_lease(run.run_id) is None


@pytest.mark.parametrize("rpc", [False, True])
async def test_process_isolation_wraps_every_pi_spawn_in_srt(
    make_task, config, ledger, data_dir, fake_pi: FakePi, fake_srt: Path, tmp_project: Path, rpc
):
    config["pi_rpc_implement"] = rpc
    task = dataclasses.replace(make_task(policy="auto"), isolation="process")
    project_file = tmp_project / ".helmraiser" / "project.toml"
    project_file.parent.mkdir(exist_ok=True)
    project_file.write_text('[sandbox]\ndeny_read = ["secrets", "~/private"]\n')
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"text": "Implemented.", "files": [["done.txt", "ok\n"]]},
                "*-review": {"text": REVIEW_PASS},
            }
        }
    )
    isolation = ProcessIsolation(config)
    isolation.check_available = lambda: None  # type: ignore[method-assign]
    run = GovernedRun(
        task,
        config=config,
        ledger=ledger,
        adapter_factory=make_adapter_factory(config),
        pi_version="0.87.1",
        data_dir=data_dir,
        snapshot=task.source_path.read_text(encoding="utf-8"),
        auth_check=lambda model: None,
        isolation=isolation,
    )
    outcome = await run.run()

    assert outcome.status == "landed", outcome
    assert ledger.get_run(run.run_id)["isolation"] == "process"
    settings = data_dir / "runs" / run.run_id / SETTINGS_FILENAME
    srt_calls = [json.loads(line) for line in fake_srt.read_text().splitlines()]
    srt_calls = [[*argv[:3], *unwrap_guard(argv[3:])] for argv in srt_calls]
    pi_calls = [c for c in fake_pi.calls() if c["session_id"]]
    assert [c["session_id"] for c in pi_calls] == [
        f"{run.run_id}-implement",
        f"{run.run_id}-review",
    ]
    implement, review = pi_calls
    assert implement["argv"][:2] == ["--mode", "rpc" if rpc else "json"]
    assert review["argv"][:2] == ["--mode", "json"]
    if rpc:
        command = json.loads(implement["stdin"])
        assert command["type"] == command["id"] == "prompt"
        assert "Make the widget work." in command["message"]
        assert "--" not in implement["argv"]
        assert not list(settings.parent.glob("prompt-implement-*.md"))
    else:
        assert implement["stdin"] == ""
        assert implement["argv"][-1].startswith("@")
        assert list(settings.parent.glob("prompt-implement-*.md"))
    assert review["stdin"] == "" and review["argv"][-1].startswith("@")
    assert list(settings.parent.glob("prompt-review-*.md"))
    verify_settings = settings.parent / "srt-settings-verify.json"
    assert [argv[:4] for argv in srt_calls if argv[3] != "pi"] == [
        ["--settings", str(verify_settings), "--", "/bin/bash"]
    ]
    srt_calls = [argv for argv in srt_calls if argv[3] == "pi"]
    assert len(srt_calls) == 2
    for srt_argv, pi_call in zip(srt_calls, pi_calls, strict=True):
        assert srt_argv[:4] == ["--settings", str(settings), "--", "pi"]
        assert srt_argv[4:] == pi_call["argv"]
        assert pi_call["cwd"] == outcome.worktree_path
    srt_settings = json.loads(settings.read_text())["filesystem"]
    sessions = settings.parent / "sessions"
    # The /tmp root depends on whether tmp_path is under /tmp (it is not in verify
    # sandboxes); tests/isolation/test_process_settings.py covers that rule.
    roots = [root for root in srt_settings["allowWrite"] if root != "/tmp"]
    assert roots[:2] == [outcome.worktree_path, str(sessions)]
    assert str(settings.parent) not in srt_settings["allowWrite"]
    assert roots[2] == str((tmp_project / ".git" / "objects").resolve())
    assert str(tmp_project / "secrets") in srt_settings["denyRead"]
    assert str(Path(outcome.worktree_path) / "secrets") in srt_settings["denyRead"]
    assert str(fake_srt.parent / "home" / "private") in srt_settings["denyRead"]


async def test_recorded_base_sha_reaches_verify_and_tracks_the_current_base(
    make_task, make_run, tmp_project: Path
):
    task = make_task(
        verify=[("base", 'test "$HELMRAISER_BASE_SHA" = "$(git rev-parse main)"')],
        setup='test "$HELMRAISER_BASE_SHA" = "$(git rev-parse HEAD)"',
    )
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval", outcome
    assert run._command_env()["HELMRAISER_BASE_SHA"] == run.base_commit
    # Rework adopts a new base: later steps must not keep a cached environment.
    run.base_commit = "f" * 40
    assert run._step_env()["HELMRAISER_BASE_SHA"] == "f" * 40


async def test_setup_uses_its_own_sandbox_settings(
    make_task, make_run, ledger, tmp_project: Path, fake_srt: Path, tmp_path: Path
):
    project_file = tmp_project / ".helmraiser" / "project.toml"
    project_file.parent.mkdir(exist_ok=True)
    cache = tmp_path / "cache"
    project_file.write_text(
        f'[setup]\nwrite = ["{cache}"]\nallowed_domains = ["registry.npmjs.org"]\n'
    )
    task = dataclasses.replace(make_task(setup="echo ready > setup.txt"), isolation="process")
    isolation = ProcessIsolation({})
    isolation.check_available = lambda: None  # type: ignore[method-assign]
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter, isolation=isolation)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval"
    calls = [json.loads(line) for line in fake_srt.read_text().splitlines()]
    setup_file = run.rundir.path / SETUP_SETTINGS_FILENAME
    assert calls[0][:4] == ["--settings", str(setup_file), "--", "/bin/bash"]
    srt_settings = json.loads(setup_file.read_text())
    owned = run.rundir.path.parent.parent / "setup-cache" / project_slug(tmp_project)
    assert srt_settings["filesystem"]["allowWrite"] == [
        outcome.worktree_path,
        "/tmp",
        str(cache),
        str(owned),
    ]
    assert srt_settings["network"]["allowedDomains"] == ["registry.npmjs.org"]
    assert (Path(outcome.worktree_path) / "setup.txt").is_file()
    events = ledger.events(run.run_id)
    assert [e["payload"]["sandbox"] for e in events if e["type"] == "setup_start"] == ["srt"]
    assert outcome.setup_sandbox == "srt"


async def test_setup_failure_names_the_command_through_srt(
    make_task, make_run, ledger, tmp_project: Path, fake_srt: Path, tmp_path: Path
):
    project_file = tmp_project / ".helmraiser" / "project.toml"
    project_file.parent.mkdir(exist_ok=True)
    cache = tmp_path / "cache"
    project_file.write_text(
        f'[setup]\nwrite = ["{cache}"]\nallowed_domains = ["registry.npmjs.org"]\n'
    )
    setup = "printf 'noise\\n'; sh -c 'echo real-error >&2; exit 3'"
    task = dataclasses.replace(make_task(setup=setup), isolation="process")
    isolation = ProcessIsolation({})
    isolation.check_available = lambda: None  # type: ignore[method-assign]
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter, isolation=isolation)

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == (
        "setup: exit 3 (last failed command: sh -c 'echo real-error >&2; exit 3') — real-error"
    )
    event = next(e for e in ledger.events(run.run_id) if e["type"] == "setup_result")
    assert event["payload"]["exit_code"] == 3
    assert event["payload"]["sandbox"] == "srt"
    assert "noise" in event["payload"]["tail"] and "real-error" in event["payload"]["tail"]
    assert "HELMRAISER_SETUP_RECORD" not in event["payload"]["tail"]
    assert adapter.calls == []  # failed before any implement/review session
    assert not any(e["type"] == "sandbox_retry" for e in ledger.events(run.run_id))
    calls = [json.loads(line) for line in fake_srt.read_text().splitlines()]
    setup_file = run.rundir.path / SETUP_SETTINGS_FILENAME
    assert calls[0][:4] == ["--settings", str(setup_file), "--", "/bin/bash"]
    srt_settings = json.loads(setup_file.read_text())
    owned = run.rundir.path.parent.parent / "setup-cache" / project_slug(tmp_project)
    assert srt_settings["filesystem"]["allowWrite"] == [
        outcome.worktree_path,
        "/tmp",
        str(cache),
        str(owned),
    ]
    assert srt_settings["network"]["allowedDomains"] == ["registry.npmjs.org"]
    summary = run.rundir.summary_md.read_text(encoding="utf-8")
    assert "real-error" in summary and "HELMRAISER_SETUP_RECORD" not in summary
    assert not (Path(outcome.worktree_path) / f".helmraiser-setup-{run.run_id}.rec").exists()


async def test_setup_with_none_isolation_remains_direct(make_task, make_run, ledger):
    task = dataclasses.replace(make_task(setup="echo direct > setup.txt"), isolation="none")
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.setup_sandbox == "none"
    assert outcome.worktree_path is not None
    assert (Path(outcome.worktree_path) / "setup.txt").read_text() == "direct\n"
    assert not (run.rundir.path / SETUP_SETTINGS_FILENAME).exists()
    assert not any(e["type"] == "sandbox_retry" for e in ledger.events(run.run_id))


async def test_host_setup_is_marked_unconfined(
    make_task, make_run, ledger, tmp_project: Path, fake_srt: Path
):
    project_file = tmp_project / ".helmraiser" / "project.toml"
    project_file.parent.mkdir(exist_ok=True)
    project_file.write_text('[defaults]\nrequire_approval = true\n[setup]\nsandbox = "host"\n')
    task = dataclasses.replace(make_task(setup="echo host > setup.txt"), isolation="process")
    isolation = ProcessIsolation({})
    isolation.check_available = lambda: None  # type: ignore[method-assign]
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter, isolation=isolation)

    outcome = await run.run()

    assert outcome.setup_sandbox == "host"
    assert not any(e["type"] == "sandbox_retry" for e in ledger.events(run.run_id))
    assert SETUP_SETTINGS_FILENAME not in fake_srt.read_text()
    assert [e["payload"] for e in ledger.events(run.run_id) if e["type"] == "step_unconfined"] == [
        {"kind": "setup", "name": "setup", "note": "ran unconfined (sandbox: host)"}
    ]
    assert "- setup: ran unconfined (sandbox: host)" in run.rundir.summary_md.read_text()


class _AbortDuringSession(ScriptedPiAdapter):
    """The session ends the way srt reports an abort: cut short, exit 0."""

    state = None

    async def run(self, req, on_event, run_id):  # type: ignore[no-untyped-def]
        self.state.request_abort(run_id)
        return await super().run(req, on_event, run_id)


@pytest.mark.parametrize("rpc", [False, True])
@pytest.mark.parametrize("error_kind", ["protocol", "sandbox"])
async def test_cut_short_session_after_abort_is_aborted_not_retried(
    make_task, make_run, ledger, config, rpc, error_kind
):
    config["pi_rpc_implement"] = rpc
    task = make_task()
    adapter = _AbortDuringSession(
        {"implement": PiScript(ok=False, error_kind=error_kind, error_message="no agent_settled")}
    )
    run, sleep = make_run(task, adapter)
    adapter.state = run.state
    outcome = await run.run()

    assert (outcome.status, outcome.failure_reason) == ("aborted", "aborted: no agent_settled")
    assert len(adapter.calls_for("implement")) == 1 and sleep.calls == []
    assert adapter.calls_for("implement")[0].rpc is rpc
    assert ledger.get_run(run.run_id)["status"] == "aborted"


@pytest.mark.parametrize("level", ["process", "none"])
async def test_deny_read_on_a_tracked_file_warns_and_the_run_goes_on(
    make_task, make_run, ledger, tmp_project: Path, level: str, fake_srt: Path
):
    task = dataclasses.replace(make_task(), isolation=level)
    project_file = tmp_project / ".helmraiser" / "project.toml"
    project_file.parent.mkdir(exist_ok=True)
    project_file.write_text('[sandbox]\ndeny_read = ["README.md", "untracked", "~/private"]\n')
    overrides = {}
    if level == "process":
        isolation = ProcessIsolation({})
        isolation.check_available = lambda: None  # type: ignore[method-assign]
        overrides["isolation"] = isolation
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter, **overrides)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval", outcome
    events = [e for e in ledger.events(run.run_id) if e["type"] == "sandbox_tracked_deny"]
    summary = run.rundir.summary_md.read_text()
    if level == "none":  # nothing is denied without srt
        assert outcome.warnings == [] and events == [] and "warning" not in summary
        return
    (warning,) = outcome.warnings
    assert "'README.md' matches 1 tracked file(s) (README.md)" in warning
    assert events[0]["payload"] == {"entries": {"README.md": ["README.md"]}}
    assert f"- warning: {warning}" in summary


async def test_verify_and_evidence_run_in_srt_and_a_host_step_is_reported(
    make_task, make_run, ledger, tmp_project: Path, fake_srt: Path
):
    """Under process isolation verify and evidence go through srt with the command sandbox settings;
    a ``sandbox: host`` step (require_approval project) runs outside it, and the events,
    summary.md and the reviewer's prompt say it ran unconfined."""
    project_file = tmp_project / ".helmraiser" / "project.toml"
    project_file.parent.mkdir(exist_ok=True)
    project_file.write_text("[defaults]\nrequire_approval = true\n")
    task = dataclasses.replace(
        make_task(),
        isolation="process",
        verify=(
            VerifyStep("inside", "test -f done.txt"),
            VerifyStep("outside", "echo host-step", sandbox="host"),
        ),
        evidence=(
            EvidenceStep("proof", 'echo p > "$HELMRAISER_EVIDENCE_DIR/p.txt"', ("p.txt",), "p"),
            EvidenceStep(
                "shots",
                'echo s > "$HELMRAISER_EVIDENCE_DIR/s.txt"',
                ("s.txt",),
                "s",
                sandbox="host",
            ),
        ),
    )
    isolation = ProcessIsolation({})
    isolation.check_available = lambda: None  # type: ignore[method-assign]
    adapter = ScriptedPiAdapter(
        {"implement": PiScript(files=[("done.txt", "ok\n")]), "review": PiScript(final_text="?")}
    )
    run, _ = make_run(task, adapter, isolation=isolation)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval", outcome
    run_dir = run.rundir.path
    srt_commands = [json.loads(line)[-1] for line in fake_srt.read_text().splitlines()]
    assert srt_commands == ["test -f done.txt", 'echo p > "$HELMRAISER_EVIDENCE_DIR/p.txt"']
    for kind in ("verify", "evidence"):
        srt_settings = json.loads((run_dir / f"srt-settings-{kind}.json").read_text())
        assert srt_settings["filesystem"]["allowWrite"] == [
            outcome.worktree_path,
            str(run_dir / "evidence"),
            "/tmp",
        ]
    events = ledger.events(run.run_id)
    (verify_event,) = [e for e in events if e["type"] == "verify_result"]
    assert [r["sandbox"] for r in verify_event["payload"]["results"]] == ["srt", "host"]
    assert [e["payload"] for e in events if e["type"] == "step_unconfined"] == [
        {"kind": "verify", "name": "outside", "note": "ran unconfined (sandbox: host)"},
        {"kind": "evidence", "name": "shots", "note": "ran unconfined (sandbox: host)"},
    ]
    lines = run.rundir.summary_md.read_text().splitlines()
    unconfined = [line.split(":")[0] for line in lines if "ran unconfined (sandbox: host)" in line]
    assert unconfined == ["- outside", "- shots"]
    assert any(line.startswith("- inside: pass in") for line in lines)
    prompt = adapter.calls_for("review")[0].prompt
    assert "### outside: pass in " in prompt
    assert "s; ran unconfined (sandbox: host)\n```" in prompt
    assert "### shots: collected; ran unconfined (sandbox: host)\n" in prompt
    assert "### proof: collected\n" in prompt
    assert prompt.count("ran unconfined") == 2


@pytest.mark.parametrize(
    "level,host,has_setup",
    [
        ("process", False, True),
        ("process", True, True),
        ("none", False, True),
        ("process", False, False),
    ],
)
async def test_cache_environment_is_setup_only(
    make_task, make_run, tmp_project, fake_srt, monkeypatch, level, host, has_setup
):
    from helmraiser.loop import runner

    monkeypatch.setenv("PIP_CACHE_DIR", "/worker/pip")
    monkeypatch.delenv("npm_config_cache", raising=False)
    project_file = tmp_project / ".helmraiser" / "project.toml"
    project_file.parent.mkdir(exist_ok=True)
    project_file.write_text(
        '[defaults]\nrequire_approval = true\n[defaults.env]\nPIP_CACHE_DIR = "/project/pip"\n'
        + ('[setup]\nsandbox = "host"\n' if host else "")
    )
    task = dataclasses.replace(
        make_task(setup="echo ready" if has_setup else None),
        isolation=level,
        env={"PIP_CACHE_DIR": "/task/pip"},
    )
    iso = ProcessIsolation({})
    iso.check_available = lambda: None
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter, **({"isolation": iso} if level == "process" else {}))
    captured = []
    original = runner.run_shell

    def record(script, cwd, timeout, env, wrap=None):
        before = [run._command_env(), run._step_env(), run._session_env()]
        captured.append(env.copy())
        result = original(script, cwd, timeout, env, wrap)
        assert before == [run._command_env(), run._step_env(), run._session_env()]
        return result

    monkeypatch.setattr(runner, "run_shell", record)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    cache = run.rundir.path.parent.parent / "setup-cache" / project_slug(tmp_project)
    sandboxed = level == "process" and not host and has_setup
    assert cache.exists() == sandboxed
    if has_setup:
        assert captured[0]["PIP_CACHE_DIR"] == "/task/pip"
        assert captured[0].get("npm_config_cache") == (str(cache / "npm") if sandboxed else None)
    for env in (run._command_env(), run._step_env(), run._session_env()):
        assert "npm_config_cache" not in env
    assert run._command_env()["PIP_CACHE_DIR"] == "/task/pip"
    for call in adapter.calls:
        assert "npm_config_cache" not in call.env
