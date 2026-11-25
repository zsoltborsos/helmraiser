"""``helmraiser pick``: decide a pair, deliver the chosen arm and reveal the setups."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn

import typer

from helmraiser.cli import commands
from helmraiser.cli.monitor import _resolve_wait_ref
from helmraiser.loop import pairs

_CHOICES = {"x": "X", "y": "Y", "tie": "tie"}


def pick(
    task: str = typer.Argument(
        ..., help="Task id (short id or <project-key>:<id>), or a run of the pair"
    ),
    choice: str = typer.Argument(..., metavar="X|Y|tie", help="The better arm, or tie"),
    reason: str = typer.Option(
        ...,
        "--reason",
        help="Why: recorded with the pick and as the winner's approval note; - reads stdin",
    ),
    judge: str = typer.Option(
        ..., "--judge", help="Who judged: harness/agent (model, effort); recorded with the pick"
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Decide a pair whose arms both await merge approval or are over: record the pick,
    reject the other arm (branch kept), deliver the picked arm as ``approve`` does, and
    reveal which arm each label was. ``tie`` delivers the cheaper arm, then the one
    with fewer iterations, then X."""

    def refuse(message: str, code: int = commands.EXIT_ERROR) -> NoReturn:
        commands._fail(message, as_json=as_json, code=code)

    picked = _CHOICES.get(choice.lower())
    if picked is None:
        refuse(f"pick X, Y or tie, not {choice}", commands.EXIT_VALIDATION)
    reason = commands._resolve_note(reason, as_json=as_json) or ""
    if not reason.strip() or not judge.strip():
        refuse("--reason and --judge must not be blank", commands.EXIT_VALIDATION)
    config = commands.load_config()
    base = commands._data_dir(data_dir)
    ledger = commands._ledger(data_dir)
    kind, ref = _resolve_wait_ref(ledger, task, as_json=as_json, error_code=commands.EXIT_ERROR)
    pair = ledger.pair_for_task(ref) if kind == "task" else ledger.pair_for_run(ref)
    if pair is None:
        refuse(f"{kind} {ref} has no pair to pick")
    task_id = str(pair["task_id"])
    if pair["pick"] is not None:
        refuse(
            f"the pair of task {task_id} is already picked "
            f"({pair['pick']}, decided by {pair['decided_by']})"
        )
    states = pairs.arms(ledger, pair)
    if states is None:
        refuse(f"the pair of task {task_id} has no runs")
    try:
        winner = pairs.delivered_label(ledger, states, picked)
    except pairs.PickRefused as exc:
        refuse(str(exc))
    loser = pairs.other(winner)
    # Both arms are loaded before anything is recorded: a task that no longer loads
    # refuses the pick whole.
    winner_row, winner_task = commands._awaiting_run(
        ledger, states[winner].run_id, config, as_json=as_json
    )
    loser_run = (
        commands._awaiting_run(ledger, states[loser].run_id, config, as_json=as_json)
        if states[loser].awaiting_merge
        else None
    )
    # The pick is recorded first. It is set once, so a second pick stops here; and
    # from here on both arms are ordinary runs, so that a delivery that is refused
    # below can be repeated with ``approve``.
    decided_by = "tie" if picked == "tie" else "judged"
    if not pairs.record_pick(
        ledger, pair, picked, decided_by=decided_by, reason=reason, judge=judge
    ):
        refuse(f"the pair of task {task_id} was picked just now")
    # The loser is rejected before the winner is approved: in that order the task's
    # status follows the winner to landed.
    lost: dict[str, Any] = {
        "label": loser,
        "run_id": states[loser].run_id,
        "status": states[loser].run["status"],
        "branch_name": states[loser].run["branch_name"],
    }
    if loser_run is not None:
        rejected = commands._reject_run(ledger, *loser_run, f"pair pick: {loser} lost", base)
        lost.update(
            status=rejected["status"],
            worktree_removed=rejected["worktree_removed"],
            worktree_error=rejected["worktree_error"],
        )
    won: dict[str, Any] = {
        "label": winner,
        "run_id": states[winner].run_id,
        "status": "landed",
        "branch_name": winner_row["branch_name"],
    }
    error: str | None = None
    try:
        pr_url, head = commands._approve_merge(
            ledger,
            winner_row,
            winner_task,
            ledger.pending_approval(states[winner].run_id),
            reason,
            base,
        )
        won.update({"pr_url": pr_url} if pr_url else {"head": head})
    except commands.MergeApprovalRefused as exc:
        error = str(exc)
        won["status"] = "awaiting_approval"
    experiment = ledger.experiment_by_id(int(pair["experiment_id"]))
    payload: dict[str, Any] = {
        "picked": picked,
        "task_id": task_id,
        "decided_by": decided_by,
        "reason": reason,
        "judge": judge,
        "delivered": winner if error is None else None,
        "winner": won,
        "loser": lost,
        "reveal": pairs.reveal(ledger, pair),
        "experiment": (
            {key: experiment[key] for key in ("name", "status", "end_reason")}
            if experiment is not None
            else None
        ),
    }
    if error is not None:
        payload["error"] = error
    hint = commands._eligible_hint(ledger, winner_task.project, data_dir, config)
    if hint:
        payload["eligible"] = hint
    if as_json:
        typer.echo(json.dumps(payload))
    else:
        _print_pick(payload)
        commands._print_eligible(hint)
    if error is not None:
        raise typer.Exit(code=commands.EXIT_ERROR)


def _print_pick(payload: dict[str, Any]) -> None:
    won, lost = payload["winner"], payload["loser"]
    what = f"tie ({won['label']} is delivered)" if payload["picked"] == "tie" else payload["picked"]
    typer.echo(f"picked {what} for {payload['task_id']}, judged by {payload['judge']}")
    if "worktree_removed" in lost:
        typer.echo(f"  {lost['label']} rejected: run {lost['run_id']}")
        typer.echo(f"  branch kept: {lost['branch_name']}")
        if not lost["worktree_removed"]:
            typer.echo(f"  worktree not removed: {lost['worktree_error']}")
    else:
        typer.echo(f"  {lost['label']} was already {lost['status']}: run {lost['run_id']}")
    if "error" in payload:
        typer.echo(f"  {won['label']} not delivered: {payload['error']}")
        typer.echo(
            "  the pick is recorded and the run awaits merge approval as any run: "
            f"helmraiser approve {won['run_id']}"
        )
    elif "pr_url" in won:
        typer.echo(f"  {won['label']} delivered: run {won['run_id']} → {won['pr_url']}")
    else:
        typer.echo(f"  {won['label']} landed: run {won['run_id']} → {won['head'][:12]}")
    for label, arm in payload["reveal"].items():
        typer.echo(
            f"  {label} was arm {arm['arm']}: {json.dumps(arm['overrides'], sort_keys=True)}"
        )
    experiment = payload["experiment"]
    if experiment is not None and experiment["status"] != "active":
        typer.echo(f"  experiment {experiment['name']} ended: {experiment['end_reason']}")
