"""Efficiency view (``helmraiser report --models``): landing, escalation, cost and
review findings, from the ledger and the run directories, grouped by implement
model (the default) or by the run's whole model setup."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from helmraiser.ledger.db import Ledger
from helmraiser.ledger.rundir import RunDir
from helmraiser.loop.blinding import HIDDEN, blinded_runs
from helmraiser.loop.review import parse_verdict
from helmraiser.loop.spend import model_limit, provider

OPENCODE_GO = "opencode-go"

MODELS_COLUMNS: tuple[str, ...] = (
    "model",
    "runs",
    "planned",
    "landed",
    "awaiting",
    "failed",
    "rejected",
    "first_pass",
    "escalated",
    "escalation_reasons",
    "review_rounds",
    "severe_first_review",
    "avg_iterations",
    "avg_minutes",
    "cost_usd",
    "plan_cost_usd",
    "cost_per_run",
    "cost_per_landed",
    "tokens_per_run",
    "opencode_go_share_per_landed",
)

REPORT_GROUPINGS: tuple[str, ...] = ("model", "setup")

_MONEY_COLUMNS = frozenset({"cost_usd", "plan_cost_usd", "cost_per_run", "cost_per_landed"})


def _first_review_path(rundir: RunDir) -> Path | None:
    """The run's first review: ``review-1.md`` when review rounds rewrote
    ``review.md``, else ``review.md`` itself."""
    first = rundir.review_round_md(1)
    if first.is_file():
        return first
    return rundir.review_md if rundir.review_md.is_file() else None


def _mean(values: list[float], digits: int) -> float | None:
    return round(sum(values) / len(values), digits) if values else None


def run_setup(run: dict[str, Any]) -> dict[str, str | None]:
    """The run's recorded model setup: plan (None without a plan stage),
    implement, configured fallback (None when the task had none) and review."""
    return {
        "plan": run["model_plan"] or None,
        "implement": str(run["model_implement"]),
        "fallback": run["model_fallback"] or None,
        "review": str(run["model_review"]),
    }


def setup_label(setup: dict[str, str | None]) -> str:
    """One pipe-free line naming a setup for a markdown table cell."""
    return (
        f"plan={setup['plan'] or 'none'} impl={setup['implement']} "
        f"fb={setup['fallback'] or 'none'} rev={setup['review']}"
    )


def _check_grouping(by: str) -> None:
    if by not in REPORT_GROUPINGS:
        raise ValueError(f"by must be one of {', '.join(REPORT_GROUPINGS)}")


def build_model_report(
    ledger: Ledger,
    data_dir: Path,
    since_seconds: float,
    config: dict[str, Any],
    now: float | None = None,
    by: str = "model",
) -> list[dict[str, Any]]:
    """One row per implement model (``by="model"``) or per whole model setup
    (``by="setup"``) among runs started in the window, sorted by run count:
    landing, escalation, review findings and cost efficiency."""
    _check_grouping(by)
    now = now if now is not None else time.time()
    cutoff = now - since_seconds
    # The runs of unpicked pairs whose arms differ in a model are one group,
    # ``hidden``: a row per model or setup would say which arm each of them ran.
    hidden = blinded_runs(ledger, str)

    def shown_setup(run: dict[str, Any]) -> dict[str, str | None]:
        setup = run_setup(run)
        return dict.fromkeys(setup, HIDDEN) if run["run_id"] in hidden else setup

    groups: dict[tuple[str | None, ...], list[dict[str, Any]]] = {}
    for run in ledger.list_runs(since=cutoff):
        setup = shown_setup(run)
        key = tuple(setup.values()) if by == "setup" else (setup["implement"],)
        groups.setdefault(key, []).append(run)
    rows = []
    for runs in groups.values():
        setup = shown_setup(runs[0])
        rows.append(
            _model_row(
                ledger,
                data_dir,
                str(setup["implement"]),
                runs,
                config,
                setup=setup if by == "setup" else None,
            )
        )
    rows.sort(
        key=lambda row: (
            -int(row["runs"]),
            str(row["model"]),
            setup_label(row["setup"]) if "setup" in row else "",
        )
    )
    return rows


def _model_row(
    ledger: Ledger,
    data_dir: Path,
    model: str,
    runs: list[dict[str, Any]],
    config: dict[str, Any],
    setup: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    planned = sum(run["model_plan"] is not None for run in runs)
    landed = sum(str(run["status"]) == "landed" for run in runs)
    awaiting = sum(str(run["status"]) == "awaiting_approval" for run in runs)
    # ``helmraiser reject`` leaves a run ``failed`` with ``failure_reason``
    # ``rejected``; deliberate rejections are not model failures.
    failed = sum(
        str(run["status"]) in ("failed", "aborted")
        and str(run["failure_reason"] or "") != "rejected"
        for run in runs
    )
    rejected = sum(
        str(run["status"]) == "failed" and str(run["failure_reason"] or "") == "rejected"
        for run in runs
    )
    first_pass = 0
    escalated = 0
    reasons: dict[str, int] = {}
    review_rounds = 0
    severe_first_review = 0
    ended_iterations: list[int] = []
    ended_minutes: list[float] = []
    cost = 0.0
    plan_cost = 0.0
    tokens = 0
    opencode_share = 0.0
    used_opencode_go = False
    sub_config: dict[str, Any] = dict((config.get("subscriptions") or {}).get(OPENCODE_GO) or {})

    for run in runs:
        run_id = str(run["run_id"])
        iterations = ledger.iterations(run_id)
        if iterations and iterations[0]["verify_passed"]:
            first_pass += 1
        if run["escalated"]:
            escalated += 1
        events = ledger.events(run_id, types=("review_feedback", "escalated"))
        if any(event["type"] == "review_feedback" for event in events):
            review_rounds += 1
        for event in events:
            if event["type"] != "escalated":
                continue
            reason = str(event["payload"].get("reason") or "verify")
            reasons[reason] = reasons.get(reason, 0) + 1
        path = _first_review_path(RunDir(run_id, data_dir))
        if path is not None and parse_verdict(path.read_text(encoding="utf-8")).severe:
            severe_first_review += 1
        if run["ended_at"]:
            ended_iterations.append(len(iterations))
            ended_minutes.append((float(run["ended_at"]) - float(run["started_at"])) / 60)
        cost += float(run["cost_usd"] or 0.0)
        plan_cost += float(run["plan_cost_usd"] or 0.0)
        tokens += int(run["tokens_in"] or 0) + int(run["tokens_out"] or 0)
        for iteration in iterations:
            iteration_model = str(iteration["model"] or "")
            if provider(iteration_model) != OPENCODE_GO:
                continue
            used_opencode_go = True
            opencode_share += float(iteration["cost_usd"] or 0.0) / model_limit(
                sub_config, iteration_model
            )

    return {
        "model": model,
        **({"setup": setup} if setup is not None else {}),
        "runs": len(runs),
        "planned": planned,
        "landed": landed,
        "awaiting": awaiting,
        "failed": failed,
        "rejected": rejected,
        "first_pass": first_pass,
        "escalated": escalated,
        "escalation_reasons": dict(sorted(reasons.items())),
        "review_rounds": review_rounds,
        "severe_first_review": severe_first_review,
        "avg_iterations": _mean([float(n) for n in ended_iterations], 2),
        "avg_minutes": _mean(ended_minutes, 1),
        "cost_usd": round(cost, 4),
        "plan_cost_usd": round(plan_cost, 4),
        "cost_per_run": round(cost / len(runs), 4),
        "cost_per_landed": round(cost / landed, 4) if landed else None,
        "tokens_per_run": round(tokens / len(runs), 1),
        "opencode_go_share_per_landed": (
            round(opencode_share / landed, 4)
            if landed and sub_config and used_opencode_go
            else None
        ),
    }


def _cell(key: str, value: Any) -> str:
    if key == "setup":
        return setup_label(value)
    if value is None:
        return "-"
    if key == "opencode_go_share_per_landed":
        return f"{float(value) * 100:.4g}%"
    if key == "escalation_reasons":
        return ", ".join(f"{reason}={count}" for reason, count in value.items()) or "-"
    if key in _MONEY_COLUMNS:
        return f"{float(value):.4f}"
    return str(value)


def render_models_section(models: list[dict[str, Any]], by: str = "model") -> str:
    """The ``# Models`` markdown table. ``by="setup"`` replaces the first
    ``model`` column with the setup label; every other column keeps its order."""
    _check_grouping(by)
    columns = ("setup", *MODELS_COLUMNS[1:]) if by == "setup" else MODELS_COLUMNS
    out = [
        "# Models",
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in models:
        out.append("| " + " | ".join(_cell(key, row.get(key)) for key in columns) + " |")
    out.append("")
    return "\n".join(out) + "\n"
