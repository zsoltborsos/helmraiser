"""PiJsonAdapter argv construction and session ids (spec §3.1)."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from helmraiser.pi.adapter import (
    LARGE_PROMPT_BYTES,
    PiJsonAdapter,
    check_auth,
    pi_version,
    session_id_for,
)
from helmraiser.pi.protocol import PiRequest
from helmraiser.task.model import ModelSpec

RUN_ID = "3f2c1a9e-6b7d-4c1e-9a2b-1f0e8d7c6b5a"


class _RecordingEngine:
    """Stands in for ExecutionEngine: records the argv, spawns nothing."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    async def run_stdin(
        self,
        cmd: list[str],
        input: str,  # noqa: A002 - matches ExecutionEngine.run_stdin
        timeout: int,
        cwd: str,
        on_line=None,
        run_id=None,
        capture_stdout: bool = True,
        env=None,
    ):
        self.argvs.append(list(cmd))
        return SimpleNamespace(returncode=0, timed_out=False, killed=False, stderr="", stdout="")


async def _run(adapter: PiJsonAdapter, req) -> list[str]:
    """Drive ``adapter.run`` with the recording engine; return the argv spawned."""

    async def on_event(event) -> None:
        pass

    await adapter.run(req, on_event, RUN_ID)
    return engine_argv(adapter)


def engine_argv(adapter: PiJsonAdapter) -> list[str]:
    engine = adapter.engine
    assert isinstance(engine, _RecordingEngine)
    return engine.argvs[-1]


def _request(tmp_path: Path, **overrides) -> PiRequest:
    run_dir = tmp_path / "runs" / RUN_ID
    fields = {
        "prompt": "Begin.",
        "session_id": f"{RUN_ID}-implement",
        "model": ModelSpec("openai-codex/gpt-6.1-sol", "medium"),
        "cwd": tmp_path / "worktree",
        "session_dir": run_dir / "sessions",
        "system_prompt_path": run_dir / "directives.md",
    }
    fields.update(overrides)
    return PiRequest(**fields)


class TestBuildArgv:
    def test_rpc_defaults_off(self, tmp_path):
        assert _request(tmp_path).rpc is False

    @pytest.mark.parametrize("wrapped", [False, True])
    @pytest.mark.parametrize("tools", [None, (), ("read", "ls")])
    @pytest.mark.parametrize("system_prompt", [False, True])
    def test_rpc_preserves_flags_but_omits_prompt(self, tmp_path, wrapped, tools, system_prompt):
        wrapper_calls = []

        def wrapper(argv, cwd, run_dir):
            wrapper_calls.append((argv, cwd, run_dir))
            return ["srt", *argv]

        req = _request(
            tmp_path,
            tools=tools,
            context_files=False,
            prompt="é\n" * LARGE_PROMPT_BYTES,
            system_prompt_path=tmp_path / "system.md" if system_prompt else None,
            model=ModelSpec("provider/model", "high"),
        )
        adapter = PiJsonAdapter(argv_wrapper=wrapper if wrapped else None)
        json_argv = adapter.build_argv(req)
        rpc_argv = adapter.build_argv(replace(req, rpc=True), tmp_path / "ignored.md")
        expected = json_argv[:-2]
        expected[expected.index("--mode") + 1] = "rpc"
        assert rpc_argv == expected
        assert "--" not in rpc_argv
        assert not any(arg.startswith("@") for arg in rpc_argv)
        assert req.prompt not in rpc_argv
        assert not req.run_dir.exists()
        if wrapped:
            assert len(wrapper_calls) == 2
            assert wrapper_calls[-1] == (rpc_argv[1:], req.cwd, req.run_dir)

    def test_implement_stage_exact_argv(self, tmp_path: Path):
        req = _request(tmp_path)
        argv = PiJsonAdapter().build_argv(req)
        run_dir = tmp_path / "runs" / RUN_ID
        assert argv == [
            "pi",
            "--mode",
            "json",
            "--session-id",
            f"{RUN_ID}-implement",
            "--session-dir",
            str(run_dir / "sessions"),
            "--model",
            "openai-codex/gpt-6.1-sol:medium",
            "--append-system-prompt",
            str(run_dir / "directives.md"),
            "--no-extensions",
            "--no-prompt-templates",
            "--no-themes",
            "--",
            "Begin.",
        ]

    def test_review_stage_adds_tools_and_no_context_files(self, tmp_path: Path):
        req = _request(
            tmp_path,
            session_id=f"{RUN_ID}-review",
            model=ModelSpec("openai-codex/gpt-6-astra", "high"),
            tools=("read", "grep", "find", "ls"),
            context_files=False,
            stage="review",
        )
        argv = PiJsonAdapter(pi_bin="/opt/pi/bin/pi").build_argv(req)
        assert argv[0] == "/opt/pi/bin/pi"
        assert argv[argv.index("--tools") + 1] == "read,grep,find,ls"
        assert "--no-context-files" in argv
        # Flags precede the prompt separator; prompt is the trailing positional.
        assert argv.index("--no-context-files") < argv.index("--")
        assert argv[-2:] == ["--", "Begin."]

    def test_empty_tools_adds_no_tools_flag(self, tmp_path: Path):
        argv = PiJsonAdapter().build_argv(_request(tmp_path, tools=()))
        assert argv.index("--no-tools") < argv.index("--")
        assert "--tools" not in argv

    def test_no_tools_setting_adds_neither_flag(self, tmp_path: Path):
        argv = PiJsonAdapter().build_argv(_request(tmp_path, tools=None))
        assert "--no-tools" not in argv
        assert "--tools" not in argv

    def test_model_without_thinking_has_no_suffix(self, tmp_path: Path):
        req = _request(tmp_path, model=ModelSpec("opencode-go/deepseek-v4.1-flash"))
        argv = PiJsonAdapter().build_argv(req)
        assert argv[argv.index("--model") + 1] == "opencode-go/deepseek-v4.1-flash"

    def test_no_system_prompt_omits_append_flag(self, tmp_path: Path):
        argv = PiJsonAdapter().build_argv(_request(tmp_path, system_prompt_path=None))
        assert "--append-system-prompt" not in argv

    def test_prompt_starting_with_dash_is_after_separator(self, tmp_path: Path):
        argv = PiJsonAdapter().build_argv(_request(tmp_path, prompt="--not-a-flag"))
        assert argv[-2:] == ["--", "--not-a-flag"]

    async def test_wrapped_session_always_spills_prompt_even_when_short(self, tmp_path: Path):
        # A 10-byte prompt: even the shortest prompt goes through a file when a
        # wrapper is set, because quoting can multiply the whole command size.
        prompt = "Begin now."  # 10 bytes
        wrapper_calls: list[list[str]] = []

        def wrapper(argv: list[str], cwd: Path, run_dir: Path) -> list[str]:
            wrapper_calls.append(argv)
            return ["srt", *argv]

        adapter = PiJsonAdapter(argv_wrapper=wrapper, engine=_RecordingEngine())
        argv = await _run(adapter, _request(tmp_path, prompt=prompt))
        spill = tmp_path / "runs" / RUN_ID / "prompt-implement-1.md"
        assert argv[-1] == f"@{spill}"
        assert spill.read_text(encoding="utf-8") == prompt
        assert len(prompt.encode("utf-8")) == 10
        assert argv == ["srt", *wrapper_calls[0][:-1], f"@{spill}"]  # wrapped, prompt spilled

    async def test_build_argv_writes_no_prompt_file(self, tmp_path: Path):
        # dry_run_plan builds the argv for display and must leave no run
        # directory behind, even for sandboxed sessions.
        adapter = PiJsonAdapter(argv_wrapper=lambda argv, cwd, run_dir: ["srt", *argv])
        argv = adapter.build_argv(_request(tmp_path, prompt="Begin now."))
        assert argv[-1].startswith("@")
        assert not (tmp_path / "runs" / RUN_ID).exists()

    async def test_two_prompts_same_stage_and_attempt_keep_distinct_files(self, tmp_path: Path):
        # Review rounds run with the same session id and attempt 1; two prompts
        # must not overwrite each other's spill file.
        adapter = PiJsonAdapter(engine=_RecordingEngine())
        run_dir = tmp_path / "runs" / RUN_ID
        first = await _run(
            adapter, _request(tmp_path, prompt="first " + "x" * LARGE_PROMPT_BYTES, stage="review")
        )
        second = await _run(
            adapter,
            _request(tmp_path, prompt="second " + "y" * LARGE_PROMPT_BYTES, stage="review"),
        )
        assert first[-1] == f"@{run_dir / 'prompt-review-1.md'}"
        assert second[-1] == f"@{run_dir / 'prompt-review-1-2.md'}"
        assert (run_dir / "prompt-review-1.md").read_text(encoding="utf-8").startswith("first ")
        assert (run_dir / "prompt-review-1-2.md").read_text(encoding="utf-8").startswith("second ")

    def test_large_prompt_is_spilled_to_file(self, tmp_path: Path):
        prompt = "x" * (LARGE_PROMPT_BYTES + 1)
        req = _request(tmp_path, prompt=prompt, attempt=2)
        argv = PiJsonAdapter(engine=_RecordingEngine()).build_argv(req)
        spill = tmp_path / "runs" / RUN_ID / "prompt-implement-2.md"
        assert argv[-1] == f"@{spill}"

    async def test_large_prompt_content_is_written_when_the_session_runs(self, tmp_path: Path):
        prompt = "x" * (LARGE_PROMPT_BYTES + 1)
        adapter = PiJsonAdapter(engine=_RecordingEngine())
        argv = await _run(adapter, _request(tmp_path, prompt=prompt, attempt=2))
        spill = tmp_path / "runs" / RUN_ID / "prompt-implement-2.md"
        assert argv[-1] == f"@{spill}"
        assert spill.read_text(encoding="utf-8") == prompt

    def test_prompt_at_limit_stays_inline(self, tmp_path: Path):
        prompt = "y" * LARGE_PROMPT_BYTES
        argv = PiJsonAdapter().build_argv(_request(tmp_path, prompt=prompt))
        assert argv[-1] == prompt
        assert not (tmp_path / "runs" / RUN_ID / "prompt-implement-1.md").exists()

    def test_large_prompt_measured_in_bytes_not_chars(self, tmp_path: Path):
        prompt = "é" * (LARGE_PROMPT_BYTES // 2 + 1)  # 2 bytes each in UTF-8
        argv = PiJsonAdapter().build_argv(_request(tmp_path, prompt=prompt))
        assert argv[-1].startswith("@")


class TestEnvironment:
    def test_adds_run_stage_and_evidence_dir(self, tmp_path: Path):
        req = _request(tmp_path, env={"EXTRA": "1"}, stage="review")
        env = PiJsonAdapter().environment(req, RUN_ID)
        assert {k: env[k] for k in env if k.startswith("HELMRAISER_") or k == "EXTRA"} == {
            "HELMRAISER_RUN_ID": RUN_ID,
            "HELMRAISER_STAGE": "review",
            "HELMRAISER_EVIDENCE_DIR": str(tmp_path / "runs" / RUN_ID / "evidence"),
            "EXTRA": "1",
        }
        assert "PI_OFFLINE" not in env

    def test_is_the_allowlist_not_the_worker_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s")
        monkeypatch.setenv("SOME_TOOL_SETTING", "x")
        monkeypatch.setenv("PATH", "/tools:/usr/bin")
        monkeypatch.setenv("LC_TIME", "C")
        req = _request(tmp_path, env={"SOME_TOOL_SETTING": "opted-in"})
        env = PiJsonAdapter().environment(req, RUN_ID)
        assert env["PATH"] == os.environ["PATH"] and env["LC_TIME"] == "C"
        assert "SSH_AUTH_SOCK" not in env and "CLAUDE_CODE_SESSION_ID" not in env
        assert env["SOME_TOOL_SETTING"] == "opted-in"

    @pytest.mark.parametrize("explicit", [False, True])
    def test_worker_interpreter_is_stripped_unless_explicitly_overridden(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
    ):
        monkeypatch.setattr(sys, "prefix", "/interpreter")
        monkeypatch.setattr(sys, "base_prefix", "/system")
        monkeypatch.setattr(sys, "executable", "/interpreter/bin/python")
        monkeypatch.setenv("VIRTUAL_ENV", "/activated")
        monkeypatch.setenv("VIRTUAL_ENV_PROMPT", "worker")
        monkeypatch.setenv("PYTHONHOME", "/python-home")
        monkeypatch.setenv("PATH", "/activated/bin:/tools:/interpreter/bin:/usr/bin:/activated/bin")
        overrides = (
            {"PATH": "/activated/bin:/project/bin", "VIRTUAL_ENV": "/project"} if explicit else {}
        )
        env = PiJsonAdapter().environment(_request(tmp_path, env=overrides, stage="review"), RUN_ID)
        if explicit:
            assert env["PATH"] == overrides["PATH"]
            assert env["VIRTUAL_ENV"] == overrides["VIRTUAL_ENV"]
        else:
            assert env["PATH"] == "/tools:/usr/bin"
            assert "VIRTUAL_ENV" not in env
        assert "VIRTUAL_ENV_PROMPT" not in env and "PYTHONHOME" not in env
        assert env["HELMRAISER_RUN_ID"] == RUN_ID
        assert env["HELMRAISER_STAGE"] == "review"
        assert env["HELMRAISER_EVIDENCE_DIR"] == str(tmp_path / "runs" / RUN_ID / "evidence")

    def test_version_and_auth_checks_get_the_session_allowlist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        dumps = tmp_path / "env"
        dumps.mkdir()
        pi = tmp_path / "pi"
        pi.write_text(
            f'#!/bin/sh\nenv > {dumps}/"$1"\n'
            'if [ "$1" = --version ]; then echo 0.87.1; else echo \'{"status":"ready"}\'; fi\n'
        )
        pi.chmod(0o755)
        monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
        monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "t")
        monkeypatch.setenv("SOME_TOOL_SETTING", "x")

        assert pi_version(str(pi)) == "0.87.1"
        assert check_auth(ModelSpec.parse("openai-codex/gpt-6-astra:high"), str(pi)) is None

        for name in ("--version", "auth"):
            names = {line.split("=", 1)[0] for line in (dumps / name).read_text().splitlines()}
            assert "PATH" in names
            assert not names & {"SSH_AUTH_SOCK", "CLAUDE_CODE_MESSAGING_TOKEN", "SOME_TOOL_SETTING"}


class TestSessionId:
    def test_derivation(self):
        assert session_id_for(RUN_ID, "implement") == f"{RUN_ID}-implement"
        assert session_id_for(RUN_ID, "implement", 1) == f"{RUN_ID}-implement"
        assert session_id_for(RUN_ID, "implement", 2) == f"{RUN_ID}-implement-2"
        assert session_id_for(RUN_ID, "review") == f"{RUN_ID}-review"

    @pytest.mark.parametrize("stage,attempt", [("implement", 1), ("implement", 2), ("review", 1)])
    def test_satisfies_pi_constraints(self, stage: str, attempt: int):
        # cli.md: letters, digits, '.', '_', '-'; must start and end alphanumeric.
        sid = session_id_for(RUN_ID, stage, attempt)
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]", sid)
