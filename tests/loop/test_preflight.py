"""``helmraiser preflight`` (loop/preflight.py): a throwaway worktree of HEAD, the project's
setup and services, and each command under the verify command sandbox settings, with hints for
known sandbox signatures. The fake srt runs the command after ``--``."""

from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from helmraiser.config import DEFAULT_CONFIG
from helmraiser.isolation.process import ProcessIsolation
from helmraiser.loop import intake, preflight, services
from helmraiser.loop.hints import (
    DENIED_HINT,
    NOT_PERMITTED_HINT,
    SOCKET_HINT,
    loopback_hint,
    sandbox_hints,
)
from helmraiser.loop.preflight import (
    PreflightError,
    create_throwaway_worktree,
    remove_throwaway_worktree,
    run_preflight,
    run_preflight_steps,
)
from helmraiser.task.loader import TaskLoadError, parse_task, render_task
from helmraiser.task.model import EvidenceStep, VerifyStep, project_slug
from helmraiser.task.project import worktree_manager
from helmraiser.task.schema import validate_task
from tests.conftest import FakeDocker
from tests.isolation.test_process_settings import REAL_SRT, needs_real_srt

# Stands in for srt: logs argv, cwd, the settings and REDIS_URL; a step behind the relay
# bootstrap is only recorded (exit 0); anything else runs.
FAKE_SRT = """import json, os, sys
argv = sys.argv[1:]
assert argv[0] == "--settings" and argv[2] == "--", argv
with open(argv[1]) as fh:
    srt_settings = json.load(fh)
entry = {"argv": argv, "cwd": os.getcwd(), "settings": srt_settings,
         "REDIS_URL": os.environ.get("REDIS_URL"), "FOO": os.environ.get("FOO"),
         "npm_config_cache": os.environ.get("npm_config_cache"),
         "PIP_CACHE_DIR": os.environ.get("PIP_CACHE_DIR"),
         "HELMRAISER_BASE_SHA": os.environ.get("HELMRAISER_BASE_SHA")}
with open(os.environ["FAKE_SRT_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\\n")
if "__sq_port" in argv[-1]:
    sys.exit(0)
os.execvp(argv[3], argv[3:])
"""

SERVICE = """
[[services]]
name = "redis"
image = "redis:7"
port = 6379
provides = { REDIS_URL = "redis://{host}:{port}/0" }
"""


@pytest.fixture
def config() -> dict:
    return copy.deepcopy(DEFAULT_CONFIG)


@pytest.fixture
def srt_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "srt-bin"
    bin_dir.mkdir()
    source = tmp_path / "fake_srt.py"
    source.write_text(FAKE_SRT)
    log = tmp_path / "fake_srt.log"
    wrapper = bin_dir / "srt"
    wrapper.write_text(
        f"#!/bin/sh\nFAKE_SRT_LOG={shlex.quote(str(log))}"
        f' exec {shlex.quote(sys.executable)} {shlex.quote(str(source))} "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ProcessIsolation, "check_available", lambda self: None)
    return log


def srt_calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def project_toml(project: Path, text: str) -> None:
    path = project / ".helmraiser" / "project.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def assert_cleaned_up(project: Path, data_dir: Path, payload_or_worktree: dict | Path) -> None:
    worktree = (
        Path(payload_or_worktree["worktree"])
        if isinstance(payload_or_worktree, dict)
        else payload_or_worktree
    )
    assert not worktree.exists()
    assert git(project, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert git(project, "branch", "--format=%(refname:short)") == "main"
    assert not (data_dir / "runs").exists() or list((data_dir / "runs").iterdir()) == []
    assert not (data_dir / "helmraiser.db").exists()


def masked_hint(name: str) -> str:
    return (
        f"srt masks {name} in writable roots (it reads as /dev/null there): "
        f"scope the command to the changed paths or have the tool ignore {name}; "
        "a tool that only warns about it and exits 0 did not fail because of it"
    )


class TestHints:
    @pytest.mark.parametrize(
        ("output", "hints"),
        [
            ("all 12 tests passed", []),
            (
                "Error: listen EPERM: operation not permitted /tmp/tsx-1000/2519.pipe",
                [SOCKET_HINT],
            ),
            ("connect EPERM /run/user/1000/bus.sock", [SOCKET_HINT]),
            (
                "Error: connect ECONNREFUSED 127.0.0.1:5432",
                [
                    "nothing listens on 127.0.0.1:5432 inside the sandbox: only the ports of "
                    "[[services]] in .helmraiser/project.toml are reachable; declare the service "
                    "and use the variable it provides"
                ],
            ),
            (
                "EACCES: permission denied, mkdir '/home/u/.cache/ms-playwright'",
                [DENIED_HINT],
            ),
            (
                ".mcp.json internalError/io  Permission denied (os error 13)",
                [masked_hint(".mcp.json")],
            ),
            (
                # Biome: the diagnostic header names the path, the message follows.
                "Checked 212 files in 180ms. No fixes applied.\n"
                "./.mcp.json internalError/fs \u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\n"
                "  \u26a0 Permission denied (os error 13)\n",
                [masked_hint(".mcp.json")],
            ),
            (
                "error: failed to read config\n  --> /work/repo/.vscode/settings.json\n"
                "  = is not a regular file\n",
                [masked_hint(".vscode")],
            ),
            # A path line followed by an unrelated denial names the masked file; a
            # non-masked path keeps the generic hint, and "not a regular file" alone none.
            (
                "src/app.ts internalError/fs\n  Permission denied (os error 13)",
                [DENIED_HINT],
            ),
            ("src/app.ts is not a regular file", []),
            ("bwrap: Operation not permitted", [NOT_PERMITTED_HINT]),
            (
                "listen EPERM: operation not permitted x.sock\nlisten EPERM again y.sock",
                [SOCKET_HINT],
            ),
        ],
    )
    def test_known_signatures(self, output: str, hints: list[str]) -> None:
        assert sandbox_hints(output) == hints


class TestThrowawayWorktree:
    def test_detached_head_configured_location_and_cleanup(
        self, tmp_project: Path, tmp_path: Path
    ) -> None:
        tree_dir = tmp_path / "detached"
        project_toml(tmp_project, f'[defaults]\nworktree_dir = "{tree_dir}"\n')
        primary_head = git(tmp_project, "rev-parse", "HEAD")

        worktree, head = create_throwaway_worktree(tmp_project, "scout", "run-id")

        assert head == primary_head
        assert worktree.parent == tree_dir.resolve()
        assert git(worktree, "rev-parse", "HEAD") == primary_head
        assert (
            subprocess.run(
                ["git", "symbolic-ref", "-q", "HEAD"], cwd=worktree, check=False
            ).returncode
            != 0
        )
        assert not list(tree_dir.glob("*.json"))
        assert git(tmp_project, "branch", "--format=%(refname:short)") == "main"

        assert remove_throwaway_worktree(tmp_project, worktree) is None
        assert not worktree.exists()
        assert git(tmp_project, "worktree", "list", "--porcelain").count("worktree ") == 1


class TestRedOnBaseTaskFields:
    @pytest.mark.parametrize("kind", ["verify", "evidence"])
    @pytest.mark.parametrize("value", [None, "true", "false"])
    def test_marker_round_trip(self, tmp_path: Path, kind: str, value: str | None):
        marker = f"    red_on_base: {value}\n" if value is not None else ""
        content = f"---\nid: marker\n{kind}:\n  - name: check\n    run: false\n{marker}---\nGoal\n"
        spec = parse_task(content, tmp_path / "task.md", {})
        assert getattr(spec, kind)[0].red_on_base is (value == "true")
        rendered = render_task(spec)
        assert ("red_on_base:" in rendered) is (value == "true")
        loaded = parse_task(rendered, tmp_path / "task.md", {})
        assert getattr(loaded, kind)[0].red_on_base is (value == "true")

    @pytest.mark.parametrize("kind", ["verify", "evidence"])
    @pytest.mark.parametrize("name", ["check", None])
    @pytest.mark.parametrize("value", ['"true"', "0", "1", "null", "[]", "{}"])
    def test_invalid_marker(self, tmp_path: Path, kind: str, name: str | None, value: str):
        named = f"    name: {name}\n" if name else ""
        content = f"---\nid: marker\n{kind}:\n  - run: false\n{named}    red_on_base: {value}\n---\nGoal\n"
        with pytest.raises(
            TaskLoadError, match=f"{kind} step '{name or kind + '-1'}': red_on_base"
        ):
            parse_task(content, tmp_path / "task.md", {})

    @pytest.mark.parametrize("kind", ["verify", "evidence"])
    @pytest.mark.parametrize("value", [True, False, "true", 0, 1, None, [], {}])
    def test_constructed_marker_boolean(self, tmp_path: Path, kind: str, value: object):
        spec = parse_task(
            f"---\nid: marker\ntitle: Marker\nproject: {tmp_path}\n---\nGoal\n",
            tmp_path / "task.md",
            {},
        )
        base = (
            VerifyStep("check", "false")
            if kind == "verify"
            else EvidenceStep("check", "false", (), "")
        )
        step = replace(base, red_on_base=value)
        errors = validate_task(replace(spec, **{kind: (step,)}))
        if isinstance(value, bool):
            assert errors == []
        else:
            assert f"{kind} step 'check': red_on_base must be a boolean" in errors


class TestIntakeAcceptanceCheck:
    @pytest.mark.parametrize("kind", ["verify", "evidence"])
    @pytest.mark.parametrize(
        "code,hints,expected",
        [(1, [], "expected red"), (0, [], "fail"), (1, ["sandbox hint"], "sandbox")],
    )
    @pytest.mark.parametrize("tail", ["", "original output"])
    def test_declared_red(
        self, tmp_project, tmp_path, monkeypatch, kind, code, hints, expected, tail
    ):
        spec = self.spec(tmp_project)
        spec = replace(spec, **{kind: (replace(getattr(spec, kind)[0], red_on_base=True),)})
        payload = {"commands": []}

        def run(repo, steps, **kwargs):
            marked = next(step for step in steps if step.red_on_base)
            payload["commands"] = [
                {
                    "name": marked.name,
                    "red_on_base": marked.red_on_base,
                    "exit_code": code,
                    "hints": hints,
                    "tail": tail,
                }
            ]
            return payload

        monkeypatch.setattr(intake, "run_preflight_steps", run)
        result = intake.check_acceptance(spec, config={}, data_dir=tmp_path)
        step = result["steps"][0]
        assert step["class"] == expected
        assert payload["commands"][0]["tail"] == tail
        if code == 0:
            assert step["tail"].startswith(tail)
            assert "declared red on base but passed on the unchanged code" in step["tail"]
        else:
            assert step["tail"] == tail

    def test_duplicate_names_keep_markers(self, tmp_project, tmp_path, config, srt_log):
        payload = preflight.run_preflight_steps(
            tmp_project,
            [VerifyStep("same", "true", red_on_base=True), VerifyStep("same", "true")],
            config=config,
            data_dir=tmp_path / "data",
        )
        first, second = payload["commands"]
        assert first["red_on_base"] is True
        assert "red_on_base" not in second
        assert payload["ok"] and first["passed"] and first["exit_code"] == 0
        result = preflight.normalize_scout_preflight(tmp_project, payload, ())
        assert [step["class"] for step in result["steps"]] == ["fail", "green"]

    def spec(self, project, *, paths=(), body=""):
        return replace(
            parse_task(
                f"---\nid: check\ntitle: Check\nproject: {project}\n---\n{body}",
                project / "task.md",
                {},
            ),
            paths=paths,
            verify=(VerifyStep("verify", "echo verify", 7, "host"),),
            evidence=(EvidenceStep("evidence", "echo evidence", (), "works", 19, "host"),),
        )

    def test_order_timeouts_and_sandbox(self, tmp_project, tmp_path, monkeypatch):
        spec = self.spec(tmp_project)
        calls = []

        def run(repo, steps, **kwargs):
            calls.append((repo, steps, kwargs))
            return {"commands": []}

        monkeypatch.setattr(intake, "run_preflight_steps", run)
        assert intake.check_acceptance(spec, config={}, data_dir=tmp_path)["summary"] == "green"
        assert calls == [
            (
                tmp_project,
                [
                    VerifyStep("verify", "echo verify", 7, "host"),
                    VerifyStep("evidence", "echo evidence", 19, "host"),
                ],
                {"config": {}, "data_dir": tmp_path},
            )
        ]

    @pytest.mark.parametrize(
        ("paths", "body", "target", "classification"),
        [
            (("tests/new.py",), "", "tests/new.py", "expected red"),
            ((), "Create `./tests/new.py:12`.", "tests/new.py", "expected red"),
            ((), "Create [tests/new.py::test_it](#test).", "tests/new.py", "expected red"),
            ((), "", "tests/new.py", "fail"),
            (("README.md",), "", "README.md", "fail"),
            (("tests/", "tests/*.py"), "", "tests/new.py", "fail"),
            (
                (),
                "Avoid /tests/new.py https://host/tests/new.py ../tests/new.py "
                "~/tests/new.py $ROOT/tests/new.py tests/*.py",
                "tests/new.py",
                "fail",
            ),
        ],
    )
    def test_classification_uses_full_output_and_only_task_mentions(
        self, tmp_project, tmp_path, monkeypatch, paths, body, target, classification
    ):
        spec = self.spec(tmp_project, paths=paths, body=body)
        # A command target alone is not a task promise to create that file.
        spec = replace(spec, verify=(VerifyStep("verify", f"pytest {target}"),))
        monkeypatch.setattr(
            intake,
            "run_preflight_steps",
            lambda *args, **kwargs: {
                "commands": [
                    {
                        "name": "verify",
                        "exit_code": 1,
                        "seconds": 0.2,
                        "output": f"ERROR: file or directory not found: {target}\nlater output",
                        "tail": "later output",
                        "hints": [],
                        "command": "private",
                    }
                ]
            },
        )
        result = intake.check_acceptance(spec, config={}, data_dir=tmp_path)
        assert result["steps"] == [
            {
                "name": "verify",
                "exit_code": 1,
                "seconds": 0.2,
                "class": classification,
                "hint": None,
                "tail": "later output",
            }
        ]

    def test_preflight_error(self, tmp_project, tmp_path, monkeypatch):
        def unavailable(*args, **kwargs):
            raise PreflightError("no sandbox")

        monkeypatch.setattr(intake, "run_preflight_steps", unavailable)
        assert intake.check_acceptance(self.spec(tmp_project), config={}, data_dir=tmp_path) == {
            "summary": "fail(1)",
            "error": "no sandbox",
            "steps": [],
        }

    def test_executes_in_throwaway_worktree(self, tmp_project, tmp_path, config, srt_log):
        spec = replace(
            self.spec(tmp_project),
            verify=(VerifyStep("verify", "echo verify"),),
            evidence=(EvidenceStep("evidence", "echo evidence", (), "works"),),
        )
        result = intake.check_acceptance(spec, config=config, data_dir=tmp_path / "data")
        assert result["summary"] == "green"
        assert [step["tail"] for step in result["steps"]] == ["verify", "evidence"]
        calls = srt_calls(srt_log)
        assert len(calls) == 2
        assert_cleaned_up(tmp_project, tmp_path / "data", Path(calls[0]["cwd"]))


class TestRunPreflight:
    @pytest.mark.parametrize("configured", [False, True])
    def test_temporary_run_directory(
        self, tmp_project, tmp_path, config, srt_log, monkeypatch, configured
    ):
        root = tmp_path / "temporary"
        root.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))
        if configured:
            config.setdefault("isolation", {})["tmpdir"] = str(root)
        else:
            config.setdefault("isolation", {}).pop("tmpdir", None)
        payload = run_preflight(
            tmp_project,
            ['test -d "$HELMRAISER_EVIDENCE_DIR" && echo "$HELMRAISER_EVIDENCE_DIR"'],
            config=config,
            data_dir=tmp_path / "data",
        )
        folder = root / f"helmraiser-preflight-{payload['run_id']}"
        assert payload["ok"]
        assert payload["commands"][0]["tail"] == str(folder / "evidence")
        assert Path(srt_calls(srt_log)[0]["argv"][1]).parent == folder
        assert not folder.exists()
        assert root.exists()
        assert not (tmp_path / "data" / "runs").exists()
        assert preflight.RunDir("ordinary", tmp_path).path == tmp_path / "runs" / "ordinary"

    def test_named_steps_preserve_names_timeouts_and_full_output(
        self,
        tmp_project: Path,
        tmp_path: Path,
        config: dict,
        srt_log: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        original = preflight.run_step
        seen: list[tuple[str, int]] = []

        def recording(step, *args, **kwargs):
            seen.append((step.name, step.timeout))
            return original(step, *args, **kwargs)

        monkeypatch.setattr(preflight, "run_step", recording)
        steps = (
            VerifyStep("quick", "printf 'first line\\ncomplete output'", 7),
            VerifyStep("slow", "printf 'earlier line\\nsecond output'", 19),
        )

        payload = run_preflight_steps(
            tmp_project, steps, config=config, data_dir=tmp_path / "data", tail=1
        )

        assert seen == [("quick", 7), ("slow", 19)]
        assert [item["name"] for item in payload["commands"]] == ["quick", "slow"]
        # tail=1 shows only the last line; the step payload keeps the whole output.
        assert [item["output"] for item in payload["commands"]] == [
            "first line\ncomplete output",
            "earlier line\nsecond output",
        ]

    def test_setup_env_and_commands_under_the_verify_sandbox_settings(
        self, tmp_project: Path, tmp_path: Path, config: dict, srt_log: Path
    ):
        project_toml(
            tmp_project,
            '[defaults]\nsetup = "echo made > setup.txt"\n\n[defaults.env]\nFOO = "bar"\n',
        )
        data_dir = tmp_path / "data"
        head = git(tmp_project, "rev-parse", "HEAD")

        payload = run_preflight(
            tmp_project,
            [
                'test -f setup.txt && echo "FOO=$FOO in $HELMRAISER_WORKTREE"',
                "echo 'Error: listen EPERM: operation not permitted /tmp/tsx-1/2.pipe' >&2; exit 1",
            ],
            config=config,
            data_dir=data_dir,
        )

        assert payload["ok"] is False and payload["error"] is None
        assert payload["head"] == head
        assert payload["setup"]["ok"] is True and payload["setup"]["exit_code"] == 0
        assert payload["setup"]["sandbox"] == "srt"
        first, second = payload["commands"]
        assert first["exit_code"] == 0 and first["sandbox"] == "srt"
        assert f"FOO=bar in {payload['worktree']}" in first["tail"]
        assert first["hints"] == []
        assert second["exit_code"] == 1 and second["hints"] == [SOCKET_HINT]
        calls = srt_calls(srt_log)
        assert [call["cwd"] for call in calls] == [payload["worktree"]] * 3
        assert [Path(call["argv"][1]).name for call in calls] == [
            "srt-settings-setup.json",
            "srt-settings-verify.json",
            "srt-settings-verify.json",
        ]
        assert calls[0]["FOO"] == "bar"
        assert [call["HELMRAISER_BASE_SHA"] for call in calls] == [head] * 3
        srt_settings = calls[0]["settings"]["filesystem"]
        assert srt_settings["allowWrite"][0] == payload["worktree"]
        assert (
            Path(payload["worktree"]).parent
            == worktree_manager(tmp_project).path_for("preflight", "unused").parent
        )
        assert_cleaned_up(tmp_project, data_dir, payload)

    def test_worktree_dir_is_honoured(
        self, tmp_project: Path, tmp_path: Path, config: dict, srt_log: Path
    ):
        project_toml(tmp_project, f'[defaults]\nworktree_dir = "{tmp_path / "trees"}"\n')

        payload = run_preflight(tmp_project, ["true"], config=config, data_dir=tmp_path / "data")

        assert payload["ok"] is True
        assert Path(payload["worktree"]).parent == (tmp_path / "trees").resolve()
        assert_cleaned_up(tmp_project, tmp_path / "data", payload)

    def test_services_start_as_for_a_run_and_are_removed(
        self,
        tmp_project: Path,
        tmp_path: Path,
        config: dict,
        srt_log: Path,
        fake_docker: FakeDocker,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(services, "port_ready", lambda port: True)
        project_toml(tmp_project, SERVICE)

        payload = run_preflight(
            tmp_project, ["redis-cli -u $REDIS_URL ping"], config=config, data_dir=tmp_path / "d"
        )

        assert payload["ok"] is True, payload
        (service,) = payload["services"]
        assert service["name"] == "redis" and service["provides"] == ["REDIS_URL"]
        (call,) = srt_calls(srt_log)
        port = service["host_port"]
        assert call["REDIS_URL"] == f"redis://127.0.0.1:{port}/0"
        assert f"127.0.0.1:{port}" in call["settings"]["network"]["allowedDomains"]
        assert f"for __sq_port in {port}; do" in call["argv"][-1]
        assert fake_docker.containers() == {}
        assert_cleaned_up(tmp_project, tmp_path / "d", payload)

    def test_a_refused_port_gets_the_hint_unless_a_service_provides_it(
        self,
        tmp_project: Path,
        tmp_path: Path,
        config: dict,
        srt_log: Path,
        fake_docker: FakeDocker,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # The fake srt does not run relayed commands: feed the hints the refusals directly.
        monkeypatch.setattr(services, "port_ready", lambda port: True)
        refused = (
            "Error: connect ECONNREFUSED 127.0.0.1:56379\n"
            "Error: connect ECONNREFUSED 127.0.0.1:56380\n"
        )
        monkeypatch.setattr(
            preflight, "sandbox_hints", lambda _output, ports: sandbox_hints(refused, ports)
        )
        project_toml(tmp_project, SERVICE + "host_port = 56379\n")

        payload = run_preflight(tmp_project, ["true"], config=config, data_dir=tmp_path / "d")

        (command,) = payload["commands"]
        assert command["hints"] == [loopback_hint("127.0.0.1:56380", sandboxed=True)]

    def test_failed_setup_stops_before_the_commands(
        self, tmp_project: Path, tmp_path: Path, config: dict, srt_log: Path
    ):
        project_toml(tmp_project, '[defaults]\nsetup = "echo no registry; exit 3"\n')

        payload = run_preflight(tmp_project, ["true"], config=config, data_dir=tmp_path / "d")

        assert payload["ok"] is False
        assert payload["error"] == "setup: exit 3"
        assert payload["setup"]["exit_code"] == 3 and "no registry" in payload["setup"]["tail"]
        assert payload["commands"] == []
        settings = Path(srt_calls(srt_log)[0]["argv"][1])
        assert settings.name == "srt-settings-setup.json"
        assert not settings.parent.exists()
        assert_cleaned_up(tmp_project, tmp_path / "d", payload)

    def test_host_setup_is_marked_and_verify_stays_in_srt(
        self, tmp_project: Path, tmp_path: Path, config: dict, srt_log: Path
    ):
        project_toml(
            tmp_project,
            '[defaults]\nsetup = "echo host > setup.txt"\nrequire_approval = true\n'
            '[setup]\nsandbox = "host"\n',
        )

        payload = run_preflight(
            tmp_project, ["test -f setup.txt"], config=config, data_dir=tmp_path / "d"
        )

        assert payload["ok"] is True and payload["setup"]["sandbox"] == "host"
        calls = srt_calls(srt_log)
        assert len(calls) == 1
        assert Path(calls[0]["argv"][1]).name == "srt-settings-verify.json"

    def test_a_service_that_fails_to_start_is_removed(
        self,
        tmp_project: Path,
        tmp_path: Path,
        config: dict,
        srt_log: Path,
        fake_docker: FakeDocker,
    ):
        project_toml(tmp_project, SERVICE)
        fake_docker.write_script({"run_fail": {"redis": "pull access denied"}})

        payload = run_preflight(tmp_project, ["true"], config=config, data_dir=tmp_path / "d")

        assert payload["ok"] is False
        assert payload["error"] == "services: redis: docker run failed: pull access denied"
        assert payload["commands"] == []
        assert fake_docker.containers() == {}
        assert_cleaned_up(tmp_project, tmp_path / "d", payload)

    def test_an_interrupt_still_cleans_up(
        self,
        tmp_project: Path,
        tmp_path: Path,
        config: dict,
        srt_log: Path,
        fake_docker: FakeDocker,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(services, "port_ready", lambda port: True)
        project_toml(tmp_project, SERVICE)
        seen: list[Path] = []

        def interrupted(step, cwd, *args, **kwargs):
            seen.append(cwd)
            assert len(fake_docker.containers()) == 1
            run_path = Path(args[2]["HELMRAISER_EVIDENCE_DIR"]).parent
            seen.append(run_path)
            assert run_path.exists()
            raise KeyboardInterrupt

        monkeypatch.setattr(preflight, "run_step", interrupted)

        with pytest.raises(KeyboardInterrupt):
            run_preflight(tmp_project, ["true"], config=config, data_dir=tmp_path / "d")

        assert fake_docker.containers() == {}
        assert_cleaned_up(tmp_project, tmp_path / "d", seen[0])
        assert not seen[1].exists()

    def test_unavailable_sandbox_is_refused_before_a_worktree_exists(
        self, tmp_project: Path, tmp_path: Path, config: dict, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(ProcessIsolation, "check_available", lambda self: "srt not found")

        with pytest.raises(PreflightError, match="process isolation is unavailable: srt not"):
            run_preflight(tmp_project, ["true"], config=config, data_dir=tmp_path / "d")

        assert not (tmp_project / ".helmraiser" / "worktrees").exists()

    def test_not_a_git_repository(self, tmp_path: Path, config: dict, srt_log: Path):
        with pytest.raises(PreflightError, match="not a git repository"):
            run_preflight(tmp_path, ["true"], config=config, data_dir=tmp_path / "d")


@needs_real_srt
def test_real_srt_runs_the_commands_and_flags_a_blocked_socket(
    tmp_project: Path, tmp_path: Path, config: dict, monkeypatch: pytest.MonkeyPatch
):
    """The real srt: a command sees the sandbox, a Unix socket is refused with a hint,
    and the worktree is gone afterwards. ``~`` is a temp dir."""
    assert REAL_SRT is not None
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", f"{Path(REAL_SRT).parent}{os.pathsep}{os.environ['PATH']}")
    python = shlex.quote(sys.executable)
    socket_step = (
        f"{python} -c 'import socket; socket.socket(socket.AF_UNIX).bind(\"/tmp/sq.sock\")'"
    )

    payload = run_preflight(
        tmp_project,
        ['test "$SANDBOX_RUNTIME" = 1 && echo inside', socket_step],
        config=config,
        data_dir=tmp_path / "data",
    )

    inside, socket_result = payload["commands"]
    assert inside["exit_code"] == 0 and inside["tail"] == "inside", inside
    assert socket_result["exit_code"] != 0
    assert "Operation not permitted" in socket_result["tail"]
    assert socket_result["hints"] == [NOT_PERMITTED_HINT]
    assert_cleaned_up(tmp_project, tmp_path / "data", payload)


@pytest.mark.parametrize("configured,host", [(False, False), (True, False), (False, True)])
def test_preflight_persistent_setup_cache(
    tmp_project, config, tmp_path, srt_log, monkeypatch, configured, host
):
    monkeypatch.delenv("npm_config_cache", raising=False)
    monkeypatch.delenv("PIP_CACHE_DIR", raising=False)
    data = tmp_path / "real-data"
    data.mkdir()
    (data / "ledger.db").touch()
    project_toml(
        tmp_project,
        "[defaults]\nsetup = 'echo kept > \"$npm_config_cache/kept\"'\nrequire_approval = true\n"
        '[defaults.env]\nPIP_CACHE_DIR = "/project/pip"\n[setup]\nwrite = ["$PIP_CACHE_DIR"]\nallowed_domains = []\n'
        + ('sandbox = "host"\n' if host else ""),
    )
    if host:
        project_toml(
            tmp_project,
            '[defaults]\nsetup = "true"\nrequire_approval = true\n[setup]\nsandbox = "host"\n',
        )
    if configured:
        config.setdefault("isolation", {})["tmpdir"] = str(tmp_path / "temporary")
    payload = run_preflight_steps(
        tmp_project, [VerifyStep("ok", "true")], config=config, data_dir=data
    )
    assert payload["ok"]
    cache = data / "setup-cache" / project_slug(tmp_project)
    assert cache.exists() == (not host)
    calls = srt_calls(srt_log)
    if not host:
        setup, verify = calls
        assert setup["npm_config_cache"] == str(cache / "npm")
        assert setup["PIP_CACHE_DIR"] == "/project/pip"
        assert setup["settings"]["filesystem"]["allowWrite"] == [
            payload["worktree"],
            "/tmp",
            "/project/pip",
            str(cache),
            *([str(tmp_path / "temporary")] if configured else []),
        ]
        denied = setup["settings"]["filesystem"]["denyRead"]
        assert not any(cache.is_relative_to(Path(p)) for p in denied)
        assert any((data / "ledger.db").is_relative_to(Path(p)) for p in denied)
        assert setup["settings"]["network"]["allowedDomains"] == []
        assert (cache / "npm" / "kept").read_text() == "kept\n"
        assert verify["npm_config_cache"] is None
        assert verify["PIP_CACHE_DIR"] == "/project/pip"
        for denied in (cache, data / "ledger.db"):
            assert any(
                denied.is_relative_to(Path(p)) for p in verify["settings"]["filesystem"]["denyRead"]
            )
    assert not (data / "helmraiser.db").exists()
    assert not Path(payload["worktree"]).exists()
