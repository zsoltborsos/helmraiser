"""``eval review`` and ``eval report``: replay past review stages under reviewer variants."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn

import typer

from helmraiser.cli.model_setup import require_models
from helmraiser.config import load_config
from helmraiser.ledger.db import Ledger, default_data_dir
from helmraiser.loop.eval_review import (
    EvalContext,
    EvalError,
    ReplayIsolation,
    build_report,
    load_cases,
    read_results,
    render_report,
    run_eval,
    select,
)
from helmraiser.pi.adapter import PiJsonAdapter
from helmraiser.pi.protocol import PiAdapter
from helmraiser.task.loader import profile_models
from helmraiser.task.model import ModelSpec
from helmraiser.task.project import load_project_defaults

EXIT_ERROR = 1
EXIT_VALIDATION = 2

eval_app = typer.Typer(help="Replay past review stages under reviewer variants and score them")


def _fail(message: str, *, as_json: bool, code: int = EXIT_ERROR) -> NoReturn:
    typer.echo(json.dumps({"error": message}) if as_json else f"Error: {message}")
    raise typer.Exit(code=code)


def get_review_adapter(
    config: dict[str, Any], project: Path, extra_read: tuple[Path, ...], data_dir: Path
) -> PiJsonAdapter:
    """A reviewer adapter under the scout's read-only sandbox, plus skill read carve-outs."""
    isolation = ReplayIsolation(config, extra_read, data_dir=data_dir)
    isolation.set_project_deny_read(load_project_defaults(project).deny_read, project)
    reason = isolation.check_available()
    if reason is not None:
        raise EvalError(f"process isolation is unavailable: {reason}")
    return PiJsonAdapter(pi_bin=str(config.get("pi_bin") or "pi"), argv_wrapper=isolation.wrap)


def get_judge_adapter(config: dict[str, Any]) -> PiAdapter:
    return PiJsonAdapter(pi_bin=str(config.get("pi_bin") or "pi"))


def _summary_line(row: dict[str, Any]) -> str:
    head = f"{row['case']} {row['variant']}"
    if row.get("status") == "error":
        return f"{head}: error: {row['error']}"
    if "verdict" not in row:  # dry run
        sessions = ", ".join(f"{s['axis']} {s['prompt_bytes']}B" for s in row["sessions"])
        return f"{head}: commit {row['reviewed_commit'][:12]} diff {row['diff_check']}: {sessions}"
    judge = row.get("judge")
    if row["bucket"] == "clean":
        score = f"{row['clean']['findings']} finding(s), {row['clean']['severe']} severe"
    elif judge is None:
        score = f"unjudged ({row.get('judge_error')})"
    else:
        score = "found" if judge["found"] else "not found"
    cost = row.get("cost_usd", 0) + row.get("judge_cost_usd", 0)
    return f"{head}: {row['verdict']}, {score}, ${cost:.4f}, {row['seconds']:.0f}s"


@eval_app.command("review")
def eval_review(
    cases_file: Path = typer.Argument(..., help="Case file (JSON)"),
    variant: list[str] = typer.Option(..., "--variant", help="Variant name; repeatable"),
    case: list[str] = typer.Option([], "--case", help="Only this case id; repeatable"),
    out: Path | None = typer.Option(None, "--out", help="Results dir (default: <cases dir>/out)"),
    max_cost: float | None = typer.Option(
        None, "--max-cost", help="Stop before a pair would take this run's spend past USD"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Build everything, call no model"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Replay the review of each case under each variant; append to <out>/results.jsonl."""
    cases_file = cases_file.expanduser()
    out_dir = (out.expanduser() if out is not None else cases_file.parent / "out").resolve()
    base = data_dir.expanduser() if data_dir is not None else default_data_dir()
    try:
        _name, cases = load_cases(cases_file)
        chosen, variants = select(cases, case, variant)
        if max_cost is not None and max_cost < 0:
            raise EvalError("--max-cost must not be negative")
        if not (base / "helmraiser.db").is_file():
            raise EvalError(f"no ledger in {base}")
    except EvalError as exc:
        _fail(str(exc), as_json=as_json, code=EXIT_VALIDATION)
    config = load_config()
    with Ledger(base / "helmraiser.db", read_only=True) as ledger:
        projects: dict[str, Path] = {}
        for item in chosen:
            run = ledger.get_run(item.run_id)
            if run is None:
                _fail(f"case {item.id}: unknown run {item.run_id}", as_json=as_json, code=2)
            projects[item.run_id] = Path(run["project"])
        models: dict[Path, dict[str, ModelSpec]] = {}
        for project in dict.fromkeys(projects.values()):
            try:
                models[project] = profile_models(project, config)
            except ValueError as exc:
                _fail(str(exc), as_json=as_json, code=EXIT_VALIDATION)
            profiles = {role: spec.render() for role, spec in models[project].items()}
            require_models({"profiles": profiles}, ("review", "judge"), as_json=as_json)
        ctx = EvalContext(
            out=out_dir,
            ledger=ledger,
            runs_dir=base / "runs",
            models=models.__getitem__,
            review_adapter=lambda project, extra: get_review_adapter(config, project, extra, base),
            judge_adapter=get_judge_adapter(config),
            timeout_s=max(1800, int(config.get("review_min_minutes", 10)) * 60),
        )
        progress = (lambda _row: None) if as_json else (lambda row: typer.echo(_summary_line(row)))
        try:
            result = run_eval(
                ctx, chosen, variants, dry_run=dry_run, max_cost=max_cost, progress=progress
            )
        except EvalError as exc:
            _fail(str(exc), as_json=as_json)
    errors = sum(1 for row in result["pairs"] if row.get("status") == "error")
    if as_json:
        typer.echo(json.dumps(result))
    else:
        for item in result["skipped_done"]:
            typer.echo(f"{item['case']} {item['variant']}: already done")
        if result["stopped"]:
            typer.echo(f"stopped: {result['stopped']} (spent ${result['spent_usd']:.4f})")
        if not dry_run:
            typer.echo(f"results: {result['results']}")
    if errors:
        raise typer.Exit(code=EXIT_ERROR)


@eval_app.command("report")
def eval_report(
    out: Path = typer.Argument(..., help="Results dir of `eval review`"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Score <out>/results.jsonl per variant and write <out>/report.md."""
    out = out.expanduser()
    try:
        rows = read_results(out / "results.jsonl")
    except ValueError as exc:
        _fail(f"results.jsonl is not valid JSON lines: {exc}", as_json=as_json, code=2)
    if not rows:
        _fail(f"no results in {out}", as_json=as_json, code=EXIT_VALIDATION)
    report = build_report(rows)
    path = out / "report.md"
    path.write_text(render_report(report), encoding="utf-8")
    report["report"] = str(path)
    typer.echo(json.dumps(report) if as_json else path.read_text(encoding="utf-8"))
