"""``build_model_report`` (``helmraiser report --models``) over a seeded ledger
and run directories: no pi, no network."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from helmraiser.config import DEFAULT_CONFIG, SUBSCRIPTION_TEMPLATES
from helmraiser.ledger.db import Ledger
from helmraiser.ledger.rundir import RunDir
from helmraiser.loop.model_report import (
    MODELS_COLUMNS,
    build_model_report,
    render_models_section,
    run_setup,
    setup_label,
)
from helmraiser.task.loader import parse_task

NOW = 1_700_000_000.0
SEVEN_DAYS = 7 * 24 * 3600

GO_MODEL = "opencode-go/glm-5.3-flash:medium"

TASK = """---
id: {id}
title: Fix the widget
project: /repo
status: queued
policy: review
isolation: none
verify:
  - name: marker
    run: test -f done.txt
---
## Goal

Make the widget work.
"""


def seed_task(ledger: Ledger, task_id: str = "t1") -> str:
    text = TASK.format(id=task_id)
    spec = parse_task(text, "/repo/topics/x.md", copy.deepcopy(DEFAULT_CONFIG))
    ledger.upsert_task(spec, text)
    return spec.task_id


def seed_run(
    ledger: Ledger,
    task_id: str,
    run_id: str,
    *,
    model: str,
    status: str,
    started_at: float,
    ended_at: float | None = None,
    escalated: int = 0,
    cost: float = 0.0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    failure_reason: str | None = None,
    model_plan: str | None = None,
    plan_cost: float = 0.0,
    model_fallback: str | None = None,
    model_review: str = "prov/review",
) -> str:
    return ledger.create_run(
        {
            "run_id": run_id,
            "task_id": task_id,
            "project": "/repo",
            "status": status,
            "policy": "review",
            "isolation": "none",
            "model_implement": model,
            "model_review": model_review,
            "model_fallback": model_fallback,
            "model_plan": model_plan,
            "plan_cost_usd": plan_cost,
            "pi_version": "0.87.1",
            "cost_usd": cost,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "escalated": escalated,
            "started_at": started_at,
            "ended_at": ended_at,
            "failure_reason": failure_reason,
        }
    )


def seed_iteration(
    ledger: Ledger,
    run_id: str,
    n: int,
    *,
    model: str,
    verify_passed: bool | None,
    cost: float = 0.0,
) -> None:
    ledger.start_iteration(run_id, n, f"{run_id}-impl", model)
    ledger.finish_iteration(
        run_id,
        n,
        agent_ok=True,
        commit_sha="abc",
        verify_passed=verify_passed,
        verify_results=[],
        cost_usd=cost,
    )


@pytest.fixture
def rows(ledger: Ledger, data_dir: Path, config: dict) -> list[dict]:
    """Two models, four in-window runs plus one old run:
    the opencode-go model lands a first-pass run and a failed run; ``prov/impl``
    has an escalated awaiting run (review round, severe first review) and a
    failed run, so it never landed."""
    config["subscriptions"] = {"opencode-go": copy.deepcopy(SUBSCRIPTION_TEMPLATES["opencode-go"])}
    task_id = seed_task(ledger)

    # opencode-go model: landed first-pass run with a known iteration cost.
    seed_run(
        ledger,
        task_id,
        "r-go-landed",
        model=GO_MODEL,
        status="landed",
        started_at=NOW - 7200,
        ended_at=NOW - 3600,
        cost=3.0,
        tokens_in=2000,
        tokens_out=1000,
        model_plan="prov/planner",
        plan_cost=0.25,
    )
    seed_iteration(ledger, "r-go-landed", 1, model=GO_MODEL, verify_passed=True, cost=3.0)
    # Same model: a failed run, so the group has runs, failed, ended stats.
    seed_run(
        ledger,
        task_id,
        "r-go-failed",
        model=GO_MODEL,
        status="failed",
        started_at=NOW - 5000,
        ended_at=NOW - 4400,
        cost=0.2,
        tokens_in=100,
        tokens_out=50,
    )
    # prov/impl: escalated awaiting run after a review round whose first review
    # (review-1.md) carries a severe finding; first iteration did not verify.
    seed_run(
        ledger,
        task_id,
        "r-esc-wait",
        model="prov/impl",
        status="awaiting_approval",
        started_at=NOW - 6000,
        escalated=1,
        cost=0.5,
        tokens_in=500,
        tokens_out=250,
    )
    ledger.start_iteration("r-esc-wait", 1, "r-esc-wait-impl", "prov/impl")
    ledger.finish_iteration(
        "r-esc-wait",
        1,
        agent_ok=True,
        commit_sha="abc",
        verify_passed=False,
        verify_results=[],
        cost_usd=0.4,
    )
    ledger.append_event(
        "r-esc-wait",
        "loop",
        "escalated",
        {"model": "prov/impl", "max_iterations": 2, "reason": "review"},
    )
    ledger.append_event("r-esc-wait", "review", "review_feedback", {"round": 1})
    rundir = RunDir("r-esc-wait", data_dir).create()
    rundir.review_round_md(1).write_text(
        "Look again.\n\n```json\n"
        '{"verdict": "concerns", "findings": [{"severity": "severe", '
        '"file": "src/x.py", "line": 3, "note": "wrong"}], "evidence_checks": []}\n'
        "```\n"
    )
    # prov/impl: failed run whose escalated event has no reason → "verify".
    seed_run(
        ledger,
        task_id,
        "r-impl-failed",
        model="prov/impl",
        status="failed",
        started_at=NOW - 4000,
        ended_at=NOW - 3000,
        escalated=1,
        cost=0.1,
    )
    ledger.append_event("r-impl-failed", "loop", "escalated", {"model": "prov/impl"})
    # A model that never lands in the window beyond this: old run is excluded.
    seed_run(
        ledger,
        task_id,
        "r-old",
        model="prov/impl",
        status="landed",
        started_at=NOW - 8 * 24 * 3600,
        ended_at=NOW - 8 * 24 * 3600 + 600,
        cost=9.0,
    )

    return build_model_report(ledger, data_dir, SEVEN_DAYS, config, now=NOW)


def test_every_column_per_model(rows: list[dict]) -> None:
    go, impl = rows[0], rows[1]
    assert [r["model"] for r in rows] == [GO_MODEL, "prov/impl"]

    assert go["runs"] == 2
    assert go["planned"] == 1
    assert go["landed"] == 1
    assert go["awaiting"] == 0
    assert go["failed"] == 1
    assert go["rejected"] == 0
    assert go["first_pass"] == 1
    assert go["escalated"] == 0
    assert go["escalation_reasons"] == {}
    assert go["review_rounds"] == 0
    assert go["severe_first_review"] == 0
    assert go["avg_iterations"] == pytest.approx(0.5)
    assert go["avg_minutes"] == pytest.approx(35.0)
    assert go["cost_usd"] == pytest.approx(3.2)
    assert go["plan_cost_usd"] == pytest.approx(0.25)
    assert go["cost_per_run"] == pytest.approx(1.6)
    assert go["cost_per_landed"] == pytest.approx(3.2)
    assert go["tokens_per_run"] == pytest.approx(1575.0)
    assert go["opencode_go_share_per_landed"] == pytest.approx(3.0 / 60)

    assert impl["runs"] == 2
    assert impl["planned"] == 0
    assert impl["landed"] == 0
    assert impl["awaiting"] == 1
    assert impl["failed"] == 1
    assert impl["rejected"] == 0
    assert impl["first_pass"] == 0
    assert impl["escalated"] == 2
    assert impl["escalation_reasons"] == {"review": 1, "verify": 1}
    assert impl["review_rounds"] == 1
    assert impl["severe_first_review"] == 1
    # The awaiting run never ended, so only the failed run counts here.
    assert impl["avg_iterations"] == pytest.approx(0.0)
    assert impl["avg_minutes"] == pytest.approx(16.7)
    assert impl["cost_usd"] == pytest.approx(0.6)
    assert impl["plan_cost_usd"] == pytest.approx(0.0)
    assert impl["cost_per_run"] == pytest.approx(0.3)
    assert impl["cost_per_landed"] is None
    assert impl["tokens_per_run"] == pytest.approx(375.0)
    assert impl["opencode_go_share_per_landed"] is None


def test_rejected_runs_counted_separately_from_failed(
    ledger: Ledger, data_dir: Path, config: dict
) -> None:
    """``helmraiser reject`` leaves a run ``failed`` with ``failure_reason``
    ``rejected``; those count as ``rejected``, not ``failed``. Aborted runs
    still count as ``failed``."""
    task_id = seed_task(ledger)
    seed_run(
        ledger,
        task_id,
        "r-rejected",
        model="prov/impl",
        status="failed",
        failure_reason="rejected",  # exactly what ``helmraiser reject`` writes
        started_at=NOW - 900,
        ended_at=NOW - 600,
    )
    seed_run(
        ledger,
        task_id,
        "r-failed",
        model="prov/impl",
        status="failed",
        started_at=NOW - 500,
        ended_at=NOW - 300,
    )
    seed_run(
        ledger,
        task_id,
        "r-aborted",
        model="prov/impl",
        status="aborted",
        started_at=NOW - 200,
        ended_at=NOW - 100,
    )

    rows = build_model_report(ledger, data_dir, SEVEN_DAYS, config, now=NOW)

    assert rows[0]["runs"] == 3
    assert rows[0]["failed"] == 2
    assert rows[0]["rejected"] == 1
    header = render_models_section(rows).splitlines()[2]
    assert header.index("| failed | rejected |") < header.index("| first_pass |")
    assert MODELS_COLUMNS.index("rejected") == MODELS_COLUMNS.index("failed") + 1


def test_share_includes_opencode_fallback_for_non_opencode_implement_model(
    ledger: Ledger, data_dir: Path, config: dict
) -> None:
    """The share follows iteration models, including a mixed-provider fallback."""
    config["subscriptions"] = {"opencode-go": copy.deepcopy(SUBSCRIPTION_TEMPLATES["opencode-go"])}
    task_id = seed_task(ledger)
    seed_run(
        ledger,
        task_id,
        "r-fallback",
        model="openai-codex/gpt-6-astra:high",
        status="landed",
        started_at=NOW - 600,
        ended_at=NOW - 300,
        cost=4.0,
    )
    seed_iteration(
        ledger,
        "r-fallback",
        1,
        model="openai-codex/gpt-6-astra:high",
        verify_passed=False,
        cost=1.0,
    )
    seed_iteration(
        ledger,
        "r-fallback",
        2,
        model=GO_MODEL,
        verify_passed=True,
        cost=3.0,
    )

    rows = build_model_report(ledger, data_dir, SEVEN_DAYS, config, now=NOW)

    assert rows[0]["model"] == "openai-codex/gpt-6-astra:high"
    assert rows[0]["landed"] == 1
    assert rows[0]["opencode_go_share_per_landed"] == pytest.approx(3.0 / 60)


def test_share_null_without_a_subscription(ledger, data_dir, config):
    config["subscriptions"] = {}
    task_id = seed_task(ledger)
    seed_run(
        ledger,
        task_id,
        "r-unmetered",
        model=GO_MODEL,
        status="landed",
        started_at=NOW - 600,
        ended_at=NOW - 300,
        cost=3.0,
    )
    seed_iteration(ledger, "r-unmetered", 1, model=GO_MODEL, verify_passed=True, cost=3.0)

    rows = build_model_report(ledger, data_dir, SEVEN_DAYS, config, now=NOW)

    assert rows[0]["model"] == GO_MODEL
    assert rows[0]["landed"] == 1
    assert rows[0]["cost_usd"] == 3.0
    assert rows[0]["opencode_go_share_per_landed"] is None


def test_share_null_for_a_landed_non_opencode_model(
    ledger: Ledger, data_dir: Path, config: dict
) -> None:
    """Regression: a landed model without opencode-go iterations reported a
    share of 0 instead of null."""
    task_id = seed_task(ledger)
    seed_run(
        ledger,
        task_id,
        "r-plain",
        model="openai-codex/gpt-6-astra:high",
        status="landed",
        started_at=NOW - 600,
        ended_at=NOW - 300,
        cost=1.0,
    )

    rows = build_model_report(ledger, data_dir, SEVEN_DAYS, config, now=NOW)

    assert rows[0]["model"] == "openai-codex/gpt-6-astra:high"
    assert rows[0]["landed"] == 1
    assert rows[0]["opencode_go_share_per_landed"] is None


def test_window_excludes_old_runs(ledger: Ledger, data_dir: Path, config: dict) -> None:
    task_id = seed_task(ledger)
    seed_run(
        ledger,
        task_id,
        "r-old",
        model="prov/impl",
        status="landed",
        started_at=NOW - 8 * 24 * 3600,
        ended_at=NOW - 8 * 24 * 3600 + 600,
        cost=9.0,
    )
    seed_run(
        ledger,
        task_id,
        "r-new",
        model="prov/impl",
        status="landed",
        started_at=NOW - 60,
        ended_at=NOW - 30,
        cost=1.0,
    )

    rows = build_model_report(ledger, data_dir, SEVEN_DAYS, config, now=NOW)

    assert [r["runs"] for r in rows] == [1]
    assert rows[0]["model"] == "prov/impl"
    assert rows[0]["cost_usd"] == pytest.approx(1.0)


def test_empty_window(ledger: Ledger, data_dir: Path, config: dict) -> None:
    assert build_model_report(ledger, data_dir, SEVEN_DAYS, config, now=NOW) == []


def test_by_setup_separates_runs_that_differ_in_plan(
    ledger: Ledger, data_dir: Path, config: dict
) -> None:
    task_id = seed_task(ledger)
    seed_run(
        ledger,
        task_id,
        "r-plan",
        model="prov/impl",
        status="landed",
        started_at=NOW - 600,
        ended_at=NOW - 300,
        model_plan="prov/planner",
        model_fallback="prov/fb",
    )
    seed_run(
        ledger,
        task_id,
        "r-noplan",
        model="prov/impl",
        status="landed",
        started_at=NOW - 500,
        ended_at=NOW - 200,
        model_fallback="prov/fb",
    )

    rows = build_model_report(ledger, data_dir, SEVEN_DAYS, config, now=NOW, by="setup")

    assert [row["runs"] for row in rows] == [1, 1]
    assert [row["model"] for row in rows] == ["prov/impl", "prov/impl"]
    assert rows[0]["setup"] == {
        "plan": None,
        "implement": "prov/impl",
        "fallback": "prov/fb",
        "review": "prov/review",
    }
    assert rows[1]["setup"] == {
        "plan": "prov/planner",
        "implement": "prov/impl",
        "fallback": "prov/fb",
        "review": "prov/review",
    }
    assert list(rows[0]) == ["model", "setup", *MODELS_COLUMNS[1:]]


def test_by_setup_default_grouping_unchanged_for_the_same_runs(
    ledger: Ledger, data_dir: Path, config: dict
) -> None:
    task_id = seed_task(ledger)
    seed_run(
        ledger,
        task_id,
        "r-plan",
        model="prov/impl",
        status="landed",
        started_at=NOW - 600,
        ended_at=NOW - 300,
        model_plan="prov/planner",
        model_fallback="prov/fb",
    )
    seed_run(
        ledger,
        task_id,
        "r-noplan",
        model="prov/impl",
        status="landed",
        started_at=NOW - 500,
        ended_at=NOW - 200,
        model_fallback="prov/fb",
    )

    default = build_model_report(ledger, data_dir, SEVEN_DAYS, config, now=NOW)
    explicit = build_model_report(ledger, data_dir, SEVEN_DAYS, config, now=NOW, by="model")

    (row,) = default
    assert row["runs"] == 2
    assert list(row) == list(MODELS_COLUMNS)
    assert "setup" not in row
    assert explicit == default


def test_by_setup_merges_identical_setups(ledger: Ledger, data_dir: Path, config: dict) -> None:
    task_id = seed_task(ledger)
    for run_id, cost in (("r-a", 1.25), ("r-b", 2.5)):
        seed_run(
            ledger,
            task_id,
            run_id,
            model="prov/impl",
            status="landed",
            started_at=NOW - 600,
            ended_at=NOW - 300,
            cost=cost,
            model_fallback="prov/fb",
        )
    seed_run(
        ledger,
        task_id,
        "r-other-review",
        model="prov/impl",
        status="landed",
        started_at=NOW - 500,
        ended_at=NOW - 200,
        cost=9.0,
        model_fallback="prov/fb",
        model_review="prov/other-review",
    )

    rows = build_model_report(ledger, data_dir, SEVEN_DAYS, config, now=NOW, by="setup")

    assert [row["runs"] for row in rows] == [2, 1]
    assert rows[0]["landed"] == 2
    assert rows[0]["cost_usd"] == pytest.approx(3.75)


def test_by_setup_distinguishes_a_missing_fallback(
    ledger: Ledger, data_dir: Path, config: dict
) -> None:
    task_id = seed_task(ledger)
    seed_run(
        ledger,
        task_id,
        "r-no-fb",
        model="prov/impl",
        status="landed",
        started_at=NOW - 600,
        ended_at=NOW - 300,
    )
    seed_run(
        ledger,
        task_id,
        "r-fb",
        model="prov/impl",
        status="landed",
        started_at=NOW - 500,
        ended_at=NOW - 200,
        model_fallback="prov/fb",
    )

    rows = build_model_report(ledger, data_dir, SEVEN_DAYS, config, now=NOW, by="setup")

    assert [row["runs"] for row in rows] == [1, 1]
    assert rows[0]["setup"]["fallback"] is None
    assert rows[1]["setup"]["fallback"] == "prov/fb"


def test_by_setup_rejects_an_unknown_grouping(
    ledger: Ledger, data_dir: Path, config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("the ledger must not be read for an unknown grouping")

    monkeypatch.setattr(ledger, "list_runs", fail)
    with pytest.raises(ValueError, match="by must be one of"):
        build_model_report(ledger, data_dir, SEVEN_DAYS, config, now=NOW, by="nope")


def test_run_setup_and_label() -> None:
    setup = run_setup(
        {
            "model_plan": None,
            "model_implement": "opencode-go/deepseek-v4.1-flash:medium",
            "model_fallback": "",
            "model_review": "openai-codex/gpt-6-astra:high",
        }
    )
    assert setup == {
        "plan": None,
        "implement": "opencode-go/deepseek-v4.1-flash:medium",
        "fallback": None,
        "review": "openai-codex/gpt-6-astra:high",
    }
    assert list(setup) == ["plan", "implement", "fallback", "review"]

    assert setup_label(
        {
            "plan": "prov/planner",
            "implement": "opencode-go/deepseek-v4.1-flash:medium",
            "fallback": "openai-codex/gpt-6.1-sol:high",
            "review": "openai-codex/gpt-6-astra:high",
        }
    ) == (
        "plan=prov/planner impl=opencode-go/deepseek-v4.1-flash:medium "
        "fb=openai-codex/gpt-6.1-sol:high rev=openai-codex/gpt-6-astra:high"
    )
    label = setup_label(setup)
    assert label == (
        "plan=none impl=opencode-go/deepseek-v4.1-flash:medium "
        "fb=none rev=openai-codex/gpt-6-astra:high"
    )
    assert "|" not in label and "\n" not in label


def test_setup_table_replaces_the_model_column_with_the_label(
    ledger: Ledger, data_dir: Path, config: dict
) -> None:
    task_id = seed_task(ledger)
    seed_run(
        ledger,
        task_id,
        "r-plan",
        model="prov/impl",
        status="landed",
        started_at=NOW - 600,
        ended_at=NOW - 300,
        model_plan="prov/planner",
        model_fallback="prov/fb",
    )
    seed_run(
        ledger,
        task_id,
        "r-noplan",
        model="prov/impl",
        status="landed",
        started_at=NOW - 500,
        ended_at=NOW - 200,
        model_fallback="prov/fb",
    )
    rows = build_model_report(ledger, data_dir, SEVEN_DAYS, config, now=NOW, by="setup")

    lines = render_models_section(rows, by="setup").splitlines()
    header = lines[2]
    assert header.startswith("| setup | runs | planned |")
    cells = [cell.strip() for cell in header.strip("|").split("|")]
    assert cells == ["setup", *MODELS_COLUMNS[1:]]
    assert any("plan=none impl=prov/impl fb=prov/fb rev=prov/review" in line for line in lines[3:])
    pipes = header.count("|")
    assert all(line.count("|") == pipes for line in lines if line.startswith("|"))


def test_default_table_unchanged(rows: list[dict]) -> None:
    default = render_models_section(rows)
    assert default == render_models_section(rows, by="model")
    assert default.splitlines()[2].startswith("| model | runs |")


def test_setup_table_rejects_an_unknown_grouping() -> None:
    with pytest.raises(ValueError, match="by must be one of"):
        render_models_section([], by="nope")
