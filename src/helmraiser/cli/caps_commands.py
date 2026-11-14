"""Inspect and control subscription usage warnings and ``stop_at`` enforcement."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from helmraiser.config import load_config
from helmraiser.ledger.db import Ledger, default_data_dir, default_db_path
from helmraiser.loop.spend import WINDOWS, caps_payload, subscription_warnings

console = Console()
caps_app = typer.Typer(help="Inspect and control subscription usage")


def _ledger(data_dir: Path | None) -> Ledger:
    base = data_dir.expanduser() if data_dir is not None else default_data_dir()
    return Ledger(default_db_path(base))


def _percent(fraction: Any) -> str:
    return f"{float(fraction):.0%}"


def _usage_text(values: dict[str, Any]) -> str:
    """Effective usage with the estimate kept, e.g. ``7% (est 13%)``."""
    effective = _percent(values["fraction"])
    if values.get("source") == "live":
        estimate = values.get("estimate_fraction")
        return f"{effective} (est {_percent(estimate)})" if estimate is not None else effective
    return f"{effective} (est)"


def _resets_text(resets_at: Any) -> str:
    if resets_at is None:
        return "unknown"
    return datetime.fromtimestamp(float(resets_at), tz=UTC).strftime("%a %H:%M UTC")


def _show(payload: dict[str, Any], config: dict[str, Any], as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(payload))
        return
    state = "on" if payload["enforced"] else "off"
    typer.echo(f"caps enforcement: {state} ({payload['source']})")
    table = Table(title="Subscription usage", box=None, padding=(0, 1), collapse_padding=True)
    table.add_column("provider")
    table.add_column("window")
    table.add_column("usage")
    table.add_column("elapsed")
    table.add_column("headroom")
    table.add_column("spend")
    table.add_column("resets")
    table.add_column("warn")
    table.add_column("stop")
    subs = config.get("subscriptions") or {}
    for name, windows in payload["usage"].items():
        sub = subs.get(name) or {}
        stop_at = sub.get("stop_at")
        for window in ("5h", "week", "month"):
            values = windows.get(window)
            if values is None:
                continue
            routing = (
                (payload.get("routing") or {}).get("headroom", {}).get(name, {}).get(window, {})
            )
            elapsed = routing.get("elapsed")
            headroom = routing.get("headroom")
            table.add_row(
                name,
                window,
                _usage_text(values),
                _percent(elapsed) if elapsed is not None else "-",
                f"{float(headroom) * 100:+.0f} pts" if headroom is not None else "unknown",
                f"${values['spend_usd']:.4f}",
                _resets_text(values["resets_at"]),
                _percent(values["warn_at"]) if values.get("warn_at") is not None else "-",
                _percent(stop_at) if stop_at is not None else "-",
            )
    console.print(table)
    spend = payload["spend"]
    spendable = [name for name in spend if name not in payload["usage"]]
    for name in sorted(spendable):
        typer.echo(
            f"{name} spend: "
            + " ".join(f"${spend[name][window]:.4f}/{window}" for window in WINDOWS)
        )
    for line in subscription_warnings(config, payload["usage"]):
        typer.echo(line)


@caps_app.callback(invoke_without_command=True)
def caps_default(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    refresh: bool = typer.Option(False, "--refresh", help="Force a live usage refresh"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Show subscription usage and dollar spend."""
    if ctx.invoked_subcommand is None:
        config = load_config()
        ledger = _ledger(data_dir)
        _show(caps_payload(ledger, config, time.time(), refresh=refresh), config, as_json)


def _set_switch(value: str | None, as_json: bool, data_dir: Path | None) -> None:
    config = load_config()
    ledger = _ledger(data_dir)
    ledger.set_caps_switch(value)
    _show(caps_payload(ledger, config, time.time()), config, as_json)


@caps_app.command("on")
def caps_on(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Enable cap enforcement until reset."""
    _set_switch("on", as_json, data_dir)


@caps_app.command("off")
def caps_off(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Disable cap enforcement until reset."""
    _set_switch("off", as_json, data_dir)


@caps_app.command("reset")
def caps_reset(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Clear the switch and use configured enforcement."""
    _set_switch(None, as_json, data_dir)
