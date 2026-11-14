"""Fixtures for outer-loop tests: a scripted pi, a temp ledger, a real git worktree."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from helmraiser.ledger.db import Ledger
from helmraiser.loop.runner import GovernedRun
from helmraiser.pi.protocol import PiAdapter, PiEvent
from helmraiser.task.loader import parse_task
from helmraiser.task.model import TaskSpec

TASK_HEADER = {
    "id": "fix-widget",
    "title": "Fix the widget",
    "status": "queued",
    "isolation": "none",
}
TASK_BODY = "## Goal\n\nMake the widget work.\n"


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def assistant_message_end(cost: float, text: str = "working") -> PiEvent:
    return PiEvent(
        "message_end",
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "stopReason": "stop",
                "usage": {"input": 10, "output": 5, "cost": {"total": cost}},
            },
        },
    )


@pytest.fixture(autouse=True)
def runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """status.json goes to a temp XDG_RUNTIME_DIR, never the real one."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return runtime


@pytest.fixture
def config(configured_config: dict[str, Any]) -> dict[str, Any]:
    return configured_config


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def ledger(data_dir: Path) -> Ledger:
    return Ledger(data_dir / "helmraiser.db")


@pytest.fixture
def make_task(tmp_project: Path, config: dict[str, Any], ledger: Ledger):
    """Build a queued TaskSpec in ``tmp_project`` and register it in the ledger."""

    def _make(
        verify: list[tuple[str, str]] | None = None,
        *,
        task_id: str = "fix-widget",
        max_cost_usd: float = 5.0,
        max_minutes: int = 90,
        max_iterations: int = 4,
        fallback: bool = True,
        policy: str = "review",
        evidence: list[dict[str, Any]] | None = None,
        setup: str | None = None,
        env: dict[str, str] | None = None,
        delivery: str | None = None,
        base: str | None = None,
        plan: bool = False,
        paths: tuple[str, ...] | None = None,
    ) -> TaskSpec:
        steps = verify or [("marker", "test -f done.txt")]
        models = {
            "implement": "openai-codex/gpt-6.1-sol:medium",
            "review": "openai-codex/gpt-6-astra:high",
        }
        if fallback:
            models["fallback"] = "openai-codex/gpt-6-astra:high"
        frontmatter: dict[str, Any] = {
            **TASK_HEADER,
            "id": task_id,
            "project": str(tmp_project),
            "policy": policy,
            "budget": {
                "max_cost_usd": max_cost_usd,
                "max_minutes": max_minutes,
                "max_iterations": max_iterations,
            },
            "models": models,
            "verify": [{"name": name, "run": run} for name, run in steps],
        }
        if paths is not None:
            frontmatter["paths"] = list(paths)
        if plan:
            frontmatter["plan"] = True
        if evidence:
            frontmatter["evidence"] = evidence
        if setup:
            frontmatter["setup"] = setup
        if env:
            frontmatter["env"] = env
        if delivery:
            frontmatter["delivery"] = delivery
        if base:
            frontmatter["base"] = base
        text = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n" + TASK_BODY
        # Outside the repo: an untracked task file would make the primary checkout
        # dirty and ``finalize_local`` refuse.
        path = tmp_project.parent / "tasks" / f"{task_id}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        spec = parse_task(text, path, config)
        if not fallback:
            spec.models.pop("fallback", None)
        ledger.upsert_task(spec, text)
        return spec

    return _make


@pytest.fixture
def make_run(config: dict[str, Any], ledger: Ledger, data_dir: Path):
    """GovernedRun wired to an in-process adapter; auth always ready; no real sleep."""

    def _make(
        task: TaskSpec, adapter: PiAdapter, **overrides: Any
    ) -> tuple[GovernedRun, FakeSleep]:
        sleep = FakeSleep()
        auth: Callable[[Any], str | None] = overrides.pop("auth_check", lambda model: None)
        run = GovernedRun(
            task,
            config=config,
            ledger=ledger,
            adapter_factory=lambda state, isolation: adapter,
            pi_version="0.87.1",
            data_dir=data_dir,
            snapshot=task.source_path.read_text(encoding="utf-8"),
            auth_check=auth,
            sleep=sleep,
            **overrides,
        )
        return run, sleep

    return _make
