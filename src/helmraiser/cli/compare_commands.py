"""One-shot blind comparison, without persisting a judge session or verdict."""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from pathlib import Path
from typing import Any, NoReturn

import typer

from helmraiser.cli.model_setup import require_models
from helmraiser.config import load_config
from helmraiser.ledger.db import Ledger, default_data_dir
from helmraiser.loop.compare import (
    CompareError,
    UnknownRun,
    build_prompt,
    build_result,
    check_runs,
    parse_judgement,
    render_markdown,
    resolve_run,
    run_facts,
    variant_runs,
)
from helmraiser.pi.adapter import PiJsonAdapter
from helmraiser.pi.protocol import PiAdapter, PiEvent, PiRequest
from helmraiser.task.loader import profile_models

EXIT_ERROR = 1
EXIT_VALIDATION = 2


def _fail(
    message: str, *, as_json: bool, code: int = EXIT_ERROR, raw: str | None = None
) -> NoReturn:
    if as_json:
        error = {"error": message}
        if raw is not None:
            error["raw"] = raw
        typer.echo(json.dumps(error))
    else:
        typer.echo("Error: " + message)
        if raw is not None:
            typer.echo(raw)
    raise typer.Exit(code=code)


def get_judge_adapter(config: dict[str, Any]) -> PiAdapter:
    return PiJsonAdapter(pi_bin=str(config.get("pi_bin") or "pi"))


async def _noop_event(_event: PiEvent) -> None:
    return None


def compare(
    run_a: str | None = typer.Argument(None),
    run_b: str | None = typer.Argument(None),
    variant_of: str | None = typer.Option(None, "--variant-of"),
    project: Path | None = typer.Option(None, "--project"),
    as_json: bool = typer.Option(False, "--json"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
) -> None:
    """Compare two runs blind, revealing their model setups only in the output."""
    if not (
        (run_a is not None and run_b is not None and variant_of is None)
        or (run_a is None and run_b is None and variant_of is not None)
    ):
        _fail(
            "give two runs or --variant-of <task>, not both", as_json=as_json, code=EXIT_VALIDATION
        )
    if project is not None and variant_of is None:
        _fail("--project requires --variant-of", as_json=as_json, code=EXIT_VALIDATION)
    base = data_dir.expanduser() if data_dir is not None else default_data_dir()
    project_key = str(project.expanduser().resolve()) if project is not None else None
    try:
        if not (base / "helmraiser.db").is_file():
            if variant_of is not None:
                raise CompareError(f"expected two variants of {variant_of}, found 0: ")
            raise UnknownRun("unknown run: " + str(run_a))
        with Ledger(base / "helmraiser.db", read_only=True) as ledger:
            if variant_of is not None:
                a, b = variant_runs(ledger, variant_of, project_key)
            else:
                a, b = resolve_run(ledger, str(run_a)), resolve_run(ledger, str(run_b))
            check_runs(a, b)
            a, b = run_facts(ledger, a), run_facts(ledger, b)
    except UnknownRun as exc:
        _fail(str(exc), as_json=as_json)
    except CompareError as exc:
        _fail(str(exc), as_json=as_json, code=EXIT_VALIDATION)

    warnings = []
    if a["brief"] != b["brief"]:
        warnings.append("task briefs differ; the prompt uses run A's")
    prompt = build_prompt(a["brief"], a, b)
    try:
        config = load_config()
        models = profile_models(Path(a["project"]), config)
        require_models(
            {"profiles": {role: spec.render() for role, spec in models.items()}},
            ("judge",),
            as_json=as_json,
        )
        model = models["judge"]
    except ValueError as exc:
        _fail(str(exc), as_json=as_json, code=EXIT_VALIDATION)
    adapter = get_judge_adapter(config)
    run_id = str(uuid.uuid4())
    with tempfile.TemporaryDirectory(prefix="helmraiser-compare-") as tmp:
        request = PiRequest(
            prompt=prompt,
            session_id=f"{run_id}-judge",
            model=model,
            cwd=Path(tmp),
            session_dir=Path(tmp) / "sessions",
            tools=(),
            context_files=False,
            timeout_s=1800,
            stage="judge",
        )
        session = asyncio.run(adapter.run(request, _noop_event, run_id))
    if not session.ok:
        _fail(
            f"judge session failed ({session.error_kind}): {session.error_message or ''}",
            as_json=as_json,
        )
    try:
        verdict = parse_judgement(session.final_text)
    except ValueError as exc:
        _fail(
            "judge answer is not a valid verdict: " + str(exc),
            as_json=as_json,
            raw=session.final_text[:4000],
        )
    result = build_result(
        a,
        b,
        verdict,
        judge_model=model.render(),
        judge_cost=session.usage.cost_usd,
        variant_of=variant_of,
        warnings=warnings,
    )
    typer.echo(json.dumps(result) if as_json else render_markdown(result))
