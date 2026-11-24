"""Helmraiser configuration management.

Stores model profiles, budgets and pi settings in ~/.helmraiser/config.json.
"""

import copy
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from helmraiser.task.model import TaskSpec

CONFIG_DIR = Path(os.environ.get("HELMRAISER_CONFIG_DIR", str(Path.home() / ".helmraiser")))
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_PROFILES: dict[str, dict[str, str]] = {
    "intake": {"thinking": "high"},
    "plan": {"thinking": "high"},
    "implement": {"thinking": "high"},
    "fallback": {"thinking": "xhigh"},
    "review": {"thinking": "high"},
    "scout": {"thinking": "high"},
    "judge": {"thinking": "high"},
}

DEFAULT_BUDGET: dict[str, Any] = {
    "max_cost_usd": 5.0,
    "max_minutes": 90,
    "max_iterations": 4,
}

# OpenCode Go monthly dollar limits per model id (without provider or ``:thinking``).
# ``deepseek-v4.1-flash`` was $60 during its promotion until 2026-09-27; the table
# uses the lasting $15.
OPENCODE_MONTHLY_LIMITS: dict[str, float] = {
    "glm-5.3-flash": 60,
    "glm-5.3": 15,
    "glm-5.2": 60,
    "glm-5.1": 60,
    "kimi-k3": 15,
    "kimi-k2.7-code": 60,
    "kimi-k2.6": 60,
    "longcat-2.0": 60,
    "mimo-v2.6-flash": 60,
    "mimo-v2.6-pro": 15,
    "mimo-v2.5": 60,
    "mimo-v2.5-pro": 15,
    "minimax-m3": 60,
    "minimax-m2.7": 60,
    "minimax-m2.5": 60,
    "muse-spark-1.3-contributor": 60,
    "muse-spark-1.2-contributor": 60,
    "qwen3.8-max": 15,
    "qwen3.8-flash": 30,
    "qwen3.7-max": 30,
    "qwen3.7-plus": 60,
    "qwen3.6-plus": 60,
    "deepseek-v4.1-flash": 15,
    "deepseek-v4-pro": 15,
    "deepseek-v4-flash": 30,
    "deepseek-v4-flash-vision-exp": 15,
    "hy4-preview": 30,
    "hy3": 60,
    "grok-4.7": 15,
    "grok-4.6": 15,
    "gpt-6-luna": 15,
    "gpt-5.6-luna": 15,
}

SUBSCRIPTION_TEMPLATES: dict[str, dict[str, Any]] = {
    "opencode-go": {
        "monthly_limit_usd": dict(OPENCODE_MONTHLY_LIMITS),
        "default_monthly_limit_usd": 15,
        "window_share": {"5h": 0.2, "week": 0.5, "month": 1.0},
        # Assumed Go spend of one run when deciding whether it may start (max_cost_usd
        # covers the whole run, mostly codex review and fallback).
        "projection_usd": 1.0,
        "week_reset": {"weekday": 0, "hour_utc": 0},
        "month_reset": {"day": 17, "hour_utc": 17},
        "warn_at": 0.8,
        "stop_at": None,
        "live": "opencode-go-api",
    },
    # Live-only: no limits table, so Codex usage comes from the Omarchy
    # collector when available and there is no dollar estimate.
    "openai-codex": {
        "live": "omarchy-codex",
        "warn_at": 0.8,
        "stop_at": None,
    },
}

DEFAULT_ROUTING: dict[str, Any] = {
    "target": 0.95,
    "margin": 0.02,
    "ramp_hours": 24,
    "scout": {
        "enabled": True,
        "focus_projects": [],
        "paused_projects": [],
        "max_pending_drafts": 3,
        "max_drafts_per_run": 3,
        "cooldown_hours": 24,
        "model": None,
    },
    "plan": {"enabled": False},
    "implement": {"enabled": False, "codex_headroom": 0.15},
}

DEFAULT_CONFIG: dict[str, Any] = {
    # Spec §0: model profiles, per-run budgets, pi invocation.
    "profiles": {name: dict(spec) for name, spec in DEFAULT_PROFILES.items()},
    "budget": dict(DEFAULT_BUDGET),
    "subscriptions": {},
    "routing": copy.deepcopy(DEFAULT_ROUTING),
    "intake": {"avoid_models": []},
    "cleanup": {"failed_after_days": 7},
    "verify_tail_lines": 120,
    "verify_excerpt_lines": 300,
    "verify_excerpt_chars": 30000,
    "review_feedback_rounds": 1,
    "first_model_time_share": 0.6,
    "plan_time_share": 0.25,
    "review_min_minutes": 10,
    "notify": True,
    # The long-running worker (``work --watch``, ``worker start``): how many run
    # children it runs at once, how often it re-checks the queue when idle, and the projects
    # that ``work`` without ``--project`` leaves alone; and whether it restarts itself
    # once a commit changes Helmraiser's own code.
    "max_parallel_runs": 2,
    "max_runs_per_project": 1,
    "worker_poll_seconds": 30,
    # Dependency-held PR base attempts have a cooldown independent of polling.
    "base_sync_interval_seconds": 300,
    "worker_exclude_projects": [],
    "worker_restart_on_code_change": True,
    "pi_bin": "pi",
    "pi_rpc_implement": False,
    # Computed at runtime from default_data_dir() when unset.
    "worktree_root": None,
}


def missing_models(
    config: dict[str, Any], roles: Iterable[str], task: "TaskSpec | None" = None
) -> list[str]:
    """Return requested roles without an explicit model, in request order."""
    from helmraiser.task.loader import resolve_models

    models = resolve_models(None, config)
    if task is not None:
        models.update(task.models)
    scout = config.get("routing", {}).get("scout", {}).get("model")
    return [
        role
        for role in roles
        if (
            not isinstance(scout, str) or not scout.strip()
            if role == "routing.scout.model"
            else role not in models or not models[role].model.strip()
        )
    ]


def _fill_missing(defaults: dict[str, Any], values: dict[str, Any]) -> None:
    """Recursively copy missing dictionary defaults into ``values``."""
    for key, default in defaults.items():
        if key not in values:
            values[key] = copy.deepcopy(default)
        elif isinstance(default, dict) and isinstance(values[key], dict):
            _fill_missing(default, values[key])


def validate_run_limit(value: object) -> int:
    """Validate the opt-in project concurrency limit."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_runs_per_project must be a positive integer")
    return value


def load_config() -> dict[str, Any]:
    """Load configuration from ~/.helmraiser/config.json.

    Returns the full config dict, creating it with defaults if it doesn't exist
    or is corrupted.
    """
    if not CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        save_config(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)

    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
    except (json.JSONDecodeError, ValueError):
        # Recover from corrupted file
        save_config(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)

    # Fill in default values for missing keys
    for key, value in DEFAULT_CONFIG.items():
        if key not in data:
            data[key] = copy.deepcopy(value)
        elif key in ("profiles", "budget") and isinstance(data[key], dict):
            for sub_key, sub_val in value.items():
                data[key].setdefault(sub_key, sub_val)
        elif key == "subscriptions" and isinstance(data[key], dict):
            for sub_name, existing in data[key].items():
                template = SUBSCRIPTION_TEMPLATES.get(sub_name)
                if isinstance(existing, dict) and template is not None:
                    for sub_key, sub_val in template.items():
                        if sub_key not in existing:
                            existing[sub_key] = copy.deepcopy(sub_val)
        elif key in ("routing", "intake", "cleanup") and isinstance(data[key], dict):
            _fill_missing(value, data[key])

    intake = data["intake"]
    avoid_models = intake.get("avoid_models") if isinstance(intake, dict) else None
    if not isinstance(avoid_models, list) or not all(
        isinstance(model, str) for model in avoid_models
    ):
        raise ValueError("intake.avoid_models must be a list of strings")

    cleanup = data["cleanup"]
    days = cleanup.get("failed_after_days") if isinstance(cleanup, dict) else None
    if not isinstance(cleanup, dict) or (
        days is not None and (isinstance(days, bool) or not isinstance(days, int) or days <= 0)
    ):
        raise ValueError("cleanup.failed_after_days must be a positive integer or null")

    validate_run_limit(data["max_runs_per_project"])
    return cast("dict[str, Any]", data)


def save_config(config: dict[str, Any]) -> None:
    """Save configuration to ~/.helmraiser/config.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2, default=str)
