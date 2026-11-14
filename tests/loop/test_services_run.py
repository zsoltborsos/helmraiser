"""``[[services]]`` through ``GovernedRun`` with the fake docker: services start before
implement, verify and evidence get what they provide (under srt behind the relays, with
the literal ports in the command sandbox settings only), and every way a run ends removes them."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from helmraiser.isolation.process import SETTINGS_FILENAME, ProcessIsolation
from helmraiser.loop import services
from helmraiser.task.model import EvidenceStep, VerifyStep
from tests.conftest import FakeDocker
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

REVIEW_PASS = 'ok\n\n```json\n{"verdict": "pass", "findings": [], "evidence_checks": []}\n```'
PROJECT = """
[[services]]
name = "postgres"
image = "postgres:16"
env = { POSTGRES_PASSWORD = "helmraiser", POSTGRES_DB = "$SERVICE_TEST_DB" }
port = 5432
ready_timeout = 1
provides = { DATABASE_URL = "postgresql://postgres:helmraiser@{host}:{port}/app" }

[[services]]
name = "qdrant"
image = "qdrant/qdrant:latest"
port = 6333
provides = { QDRANT_URL = "http://{host}:{port}" }
"""


@pytest.fixture
def project(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_project / ".helmraiser" / "project.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(PROJECT)
    monkeypatch.setenv("SERVICE_TEST_DB", "app")
    return tmp_project


@pytest.fixture
def ready(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    probed: list[int] = []

    def probe(port: int) -> bool:
        probed.append(port)
        return True

    monkeypatch.setattr(services, "port_ready", probe)
    return probed


def host_ports(fake_docker: FakeDocker) -> dict[str, int]:
    ports: dict[str, int] = {}
    for call in fake_docker.calls():
        argv = call["argv"]
        if argv[0] == "run":
            name = argv[argv.index("--name") + 1].rsplit("-", 1)[1]
            ports[name] = int(argv[argv.index("-p") + 1].split(":")[1])
    return ports


def event_types(ledger: Any, run_id: str) -> list[str]:
    return [e["type"] for e in ledger.events(run_id)]


async def test_services_serve_verify_and_evidence_and_are_removed_after(
    make_task, make_run, ledger, project: Path, fake_docker: FakeDocker, ready: list[int]
):
    task = make_task(
        verify=[("db", 'test -f done.txt && echo "$DATABASE_URL" > "$HELMRAISER_EVIDENCE_DIR/v"')],
        evidence=[
            {
                "name": "urls",
                "run": 'echo "$QDRANT_URL" > "$HELMRAISER_EVIDENCE_DIR/e.txt"',
                "artifacts": ["e.txt"],
                "criteria": "c",
            }
        ],
    )
    adapter = ScriptedPiAdapter(
        {"implement": PiScript(files=[("done.txt", "ok\n")]), "review": PiScript(final_text="?")}
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval", outcome
    ports = host_ports(fake_docker)
    assert sorted(ready) == sorted(ports.values())
    evidence = run.rundir.evidence
    assert (evidence / "v").read_text() == (
        f"postgresql://postgres:helmraiser@127.0.0.1:{ports['postgres']}/app\n"
    )
    assert (evidence / "e.txt").read_text() == f"http://127.0.0.1:{ports['qdrant']}\n"
    postgres_run, *_ = [c for c in fake_docker.calls() if c["argv"][0] == "run"]
    assert postgres_run["env"] == {"POSTGRES_PASSWORD": "helmraiser", "POSTGRES_DB": "app"}
    # pi sessions get neither the services' variables nor their ports; the directive says so.
    for call in adapter.calls:
        assert "DATABASE_URL" not in call.env and "QDRANT_URL" not in call.env
    directives = " ".join(run.rundir.directives_md.read_text().split())
    assert (
        "only the verify and evidence steps get DATABASE_URL, QDRANT_URL, for containers"
    ) in directives
    assert fake_docker.containers() == {}
    types = event_types(ledger, run.run_id)
    assert types.index("service_ready") < types.index("iteration_start")
    assert types.count("service_start") == 2 and types.count("service_ready") == 2
    assert types.index("service_stop") > types.index("evidence_result")
    (stop,) = [e["payload"] for e in ledger.events(run.run_id) if e["type"] == "service_stop"]
    assert len(stop["removed"]) == 2 and stop["error"] is None
    assert [s["name"] for s in outcome.services] == ["postgres", "qdrant"]
    summary = run.rundir.summary_md.read_text()
    assert (
        f"- service postgres: `postgres:16` on 127.0.0.1:{ports['postgres']}"
        " (container port 5432; provides DATABASE_URL)"
    ) in summary
    assert "helmraiser@" not in summary


async def test_simultaneous_siblings_have_isolated_services_and_verify(
    make_task, make_run, config, project, fake_docker, ready, monkeypatch
):
    config["max_runs_per_project"] = 2
    ports = iter([41001, 41002, 41003, 41004])
    monkeypatch.setattr(services, "free_port", lambda: next(ports))
    arrived = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]

    class BarrierAdapter(ScriptedPiAdapter):
        def __init__(self, index):
            super().__init__(
                {
                    "implement": PiScript(files=[(f"{index}/done.txt", "ok\n")]),
                    "review": PiScript(final_text=REVIEW_PASS),
                }
            )
            self.index = index

        async def run(self, req, on_event, run_id):
            if req.stage == "implement":
                arrived[self.index].set()
                await asyncio.wait_for(release[self.index].wait(), timeout=10)
            return await super().run(req, on_event, run_id)

    runs = []
    for i in range(2):
        task = make_task(
            task_id=f"sibling-{i}",
            paths=(f"{i}/**",),
            verify=[
                (
                    "urls",
                    f'test -f {i}/done.txt && echo "$DATABASE_URL" > "$HELMRAISER_EVIDENCE_DIR/v"',
                )
            ],
            evidence=[
                {
                    "name": "urls",
                    "run": 'echo "$QDRANT_URL" > "$HELMRAISER_EVIDENCE_DIR/e.txt"',
                    "artifacts": ["e.txt"],
                    "criteria": "own URL",
                }
            ],
        )
        run, _ = make_run(task, BarrierAdapter(i))
        runs.append(run)
    pending = [asyncio.create_task(run.run()) for run in runs]
    try:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in arrived)), timeout=10)
        first, second = runs
        assert first.run_id != second.run_id and first.worktree != second.worktree
        names = {
            f"helmraiser-{run.run_id[:8]}-{svc}" for run in runs for svc in ("postgres", "qdrant")
        }
        assert {c["name"] for c in fake_docker.containers().values()} == names
        assert {s.host_port for run in runs for s in run.services} == {41001, 41002, 41003, 41004}
        for run in runs:
            assert {s.container for s in run.services} == {
                f"helmraiser-{run.run_id[:8]}-{svc}" for svc in ("postgres", "qdrant")
            }
        release[0].set()
        outcome = await asyncio.wait_for(pending[0], timeout=10)
        assert outcome.status == "awaiting_approval", outcome
        assert {c["name"] for c in fake_docker.containers().values()} == {
            s.container for s in second.services
        }
        assert not pending[1].done()
        release[1].set()
        outcome = await asyncio.wait_for(pending[1], timeout=10)
        assert outcome.status == "awaiting_approval", outcome
        for run in runs:
            provided = {key: value for s in run.services for key, value in s.provides.items()}
            assert (run.rundir.evidence / "v").read_text().strip() == provided["DATABASE_URL"]
            assert (run.rundir.evidence / "e.txt").read_text().strip() == provided["QDRANT_URL"]
        assert fake_docker.containers() == {}
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*pending, return_exceptions=True)


async def test_a_service_that_is_not_ready_fails_the_run_before_implement(
    make_task, make_run, ledger, project: Path, fake_docker: FakeDocker, monkeypatch
):
    monkeypatch.setattr(services, "port_ready", lambda port: False)
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(make_task(), adapter)

    outcome = await run.run()

    assert (outcome.status, outcome.failure_reason) == (
        "failed",
        "services: postgres not ready after 1s",
    )
    assert adapter.calls_for("implement") == []
    assert fake_docker.containers() == {}  # removed by label, although it never got ready
    types = event_types(ledger, run.run_id)
    assert "service_failed" in types and "service_stop" in types
    assert "- failure: services: postgres not ready after 1s" in run.rundir.summary_md.read_text()


async def test_a_failed_docker_run_fails_the_run_and_cleans_up(
    make_task, make_run, project: Path, fake_docker: FakeDocker, ready: list[int]
):
    fake_docker.write_script({"run_fail": {"qdrant": "Unable to find image"}})
    adapter = ScriptedPiAdapter()
    run, _ = make_run(make_task(), adapter)

    outcome = await run.run()

    assert outcome.failure_reason == "services: qdrant: docker run failed: Unable to find image"
    assert fake_docker.containers() == {}


async def test_a_failed_run_removes_its_services(
    make_task, make_run, project: Path, fake_docker: FakeDocker, ready: list[int]
):
    task = make_task(verify=[("never", "false")], max_iterations=1, fallback=False)
    run, _ = make_run(task, ScriptedPiAdapter({"implement": PiScript(files=[("x", "x\n")])}))

    outcome = await run.run()

    assert outcome.status == "failed", outcome
    assert len(host_ports(fake_docker)) == 2 and fake_docker.containers() == {}


class _AbortDuringSession(ScriptedPiAdapter):
    state = None

    async def run(self, req, on_event, run_id):  # type: ignore[no-untyped-def]
        self.state.request_abort(run_id)
        return await super().run(req, on_event, run_id)


async def test_an_aborted_run_removes_its_services(
    make_task, make_run, project: Path, fake_docker: FakeDocker, ready: list[int]
):
    adapter = _AbortDuringSession(
        {"implement": PiScript(ok=False, error_kind="protocol", error_message="cut short")}
    )
    run, _ = make_run(make_task(), adapter)
    adapter.state = run.state

    outcome = await run.run()

    assert outcome.status == "aborted", outcome
    assert len(host_ports(fake_docker)) == 2 and fake_docker.containers() == {}


class _Raises(ScriptedPiAdapter):
    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self.exc = exc

    async def run(self, req, on_event, run_id):  # type: ignore[no-untyped-def]
        raise self.exc


async def test_an_infrastructure_error_removes_the_services(
    make_task, make_run, project: Path, fake_docker: FakeDocker, ready: list[int]
):
    run, _ = make_run(make_task(), _Raises(RuntimeError("adapter broke")))

    outcome = await run.run()

    assert outcome.failure_reason == "error: RuntimeError: adapter broke"
    assert fake_docker.containers() == {}


async def test_a_cancelled_run_still_removes_the_services(
    make_task, make_run, project: Path, fake_docker: FakeDocker, ready: list[int]
):
    run, _ = make_run(make_task(), _Raises(asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await run.run()

    assert len(host_ports(fake_docker)) == 2 and fake_docker.containers() == {}


async def test_a_cleanup_failure_is_a_warning(
    make_task, make_run, ledger, project: Path, fake_docker: FakeDocker, ready: list[int]
):
    adapter = ScriptedPiAdapter(
        {"implement": PiScript(files=[("done.txt", "ok\n")]), "review": PiScript(final_text="?")}
    )
    run, _ = make_run(make_task(), adapter)

    async def fail_rm_on_review(*args: Any, **kwargs: Any) -> Any:
        fake_docker.write_script({"rm_fail": "daemon busy"})
        return await ScriptedPiAdapter.run(adapter, *args, **kwargs)

    adapter.run = fail_rm_on_review  # type: ignore[method-assign]
    outcome = await run.run()

    assert outcome.status == "awaiting_approval"
    assert outcome.warnings == [
        "services: cleanup failed (daemon busy); `helmraiser work` removes them later"
    ]
    assert len(fake_docker.containers()) == 2


async def test_the_plan_stage_starts_no_services(
    make_task, make_run, project: Path, fake_docker: FakeDocker, ready: list[int]
):
    task = make_task(policy="approve-plan-first")
    run, _ = make_run(task, ScriptedPiAdapter({"plan": PiScript(final_text="## Plan\n\n1. x")}))

    outcome = await run.run()

    assert outcome.status == "awaiting_approval" and outcome.pending_approval == "plan"
    assert fake_docker.calls() == []


# Stands in for srt: records its argv; runs pi, and bash steps that are not behind the
# relay bootstrap; a bootstrapped step is only recorded (exit 0).
FAKE_SRT = """import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_SRT_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")
if "__sq_port" in argv[-1]:
    sys.exit(0)
os.execvp(argv[3], argv[3:])
"""


async def test_under_srt_steps_run_behind_relays_with_the_literal_ports_allowed(
    make_task,
    make_run,
    project: Path,
    fake_docker: FakeDocker,
    ready: list[int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "fake_srt.py"
    source.write_text(FAKE_SRT)
    log = tmp_path / "fake_srt.log"
    wrapper = fake_docker.bin.parent / "srt"
    wrapper.write_text(
        f"#!/bin/sh\nFAKE_SRT_LOG={shlex.quote(str(log))}"
        f' exec {shlex.quote(sys.executable)} {shlex.quote(str(source))} "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    task = dataclasses.replace(
        make_task(),
        isolation="process",
        verify=(VerifyStep("db", "pytest -q tests/db"),),
        evidence=(EvidenceStep("proof", "echo p", ("p.txt",), "p"),),
    )
    isolation = ProcessIsolation({})
    isolation.check_available = lambda: None  # type: ignore[method-assign]
    adapter = ScriptedPiAdapter(
        {"implement": PiScript(files=[("done.txt", "ok\n")]), "review": PiScript(final_text="?")}
    )
    run, _ = make_run(task, adapter, isolation=isolation)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval", outcome
    ports = host_ports(fake_docker)
    literal = [f"127.0.0.1:{ports['postgres']}", f"127.0.0.1:{ports['qdrant']}"]
    run_dir = run.rundir.path
    for kind in ("verify", "evidence"):
        srt_settings = json.loads((run_dir / f"srt-settings-{kind}.json").read_text())
        assert srt_settings["network"]["allowedDomains"][-2:] == literal
        assert "allowAllUnixSockets" not in json.dumps(srt_settings)
    # The pi session's settings for the same run never has them.
    isolation.wrap(["pi"], Path(outcome.worktree_path), run_dir)
    pi_settings = json.loads((run_dir / SETTINGS_FILENAME).read_text())
    assert not any(d.startswith("127.0.0.1:") for d in pi_settings["network"]["allowedDomains"])
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    steps = [argv for argv in calls if argv[3] == "/bin/bash"]
    assert len(steps) == 2
    for argv, command in zip(steps, ["pytest -q tests/db", "echo p"], strict=True):
        from tests.mocks.fake_srt import unwrap_guard

        payload = unwrap_guard(argv[3:])
        assert payload[:4] == ["/bin/bash", "-o", "pipefail", "-c"]
        script = payload[4]
        assert f"for __sq_port in {ports['postgres']} {ports['qdrant']}; do" in script
        assert f"/bin/bash -o pipefail -c {shlex.quote(command)}" in script


BLOCKED_ON_SERVICES = "BLOCKED: cannot run backend-suite: DATABASE_URL is not set"


async def test_blocked_with_committed_work_goes_to_verify_when_the_run_has_services(
    make_task, make_run, ledger, project: Path, fake_docker: FakeDocker, ready: list[int]
):
    task = make_task(verify=[("db", 'test -f done.txt && test -n "$DATABASE_URL"')])
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(
                files=[("done.txt", "ok\n")], commit=True, final_text=BLOCKED_ON_SERVICES
            ),
            "review": PiScript(final_text=REVIEW_PASS),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval", outcome
    (event,) = [e for e in ledger.events(run.run_id) if e["type"] == "blocked_to_verify"]
    assert event["payload"] == {
        "n": 1,
        "reason": "cannot run backend-suite: DATABASE_URL is not set",
    }


async def test_blocked_work_that_fails_verify_fails_as_blocked_without_another_round(
    make_task, make_run, project: Path, fake_docker: FakeDocker, ready: list[int]
):
    task = make_task(verify=[("never", "false")])
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(
                files=[("done.txt", "ok\n")], commit=True, final_text=BLOCKED_ON_SERVICES
            )
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert (outcome.status, outcome.failure_reason) == (
        "failed",
        "blocked: cannot run backend-suite: DATABASE_URL is not set",
    )
    assert len(adapter.calls_for("implement")) == 1


async def test_blocked_with_uncommitted_work_still_fails_with_services(
    make_task, make_run, project: Path, fake_docker: FakeDocker, ready: list[int]
):
    adapter = ScriptedPiAdapter(
        {"implement": PiScript(files=[("done.txt", "ok\n")], final_text=BLOCKED_ON_SERVICES)}
    )
    run, _ = make_run(make_task(), adapter)

    outcome = await run.run()

    assert outcome.failure_reason == "blocked: cannot run backend-suite: DATABASE_URL is not set"
    assert len(adapter.calls) == 1


async def test_the_directive_says_not_to_run_or_block_on_service_steps(
    make_task, make_run, project: Path, fake_docker: FakeDocker, ready: list[int]
):
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(make_task(), adapter)

    await run.run()

    directives = " ".join(run.rundir.directives_md.read_text().split())
    assert (
        "Do not run verify steps that need them or answer `BLOCKED:` over them: commit and stop."
    ) in directives
