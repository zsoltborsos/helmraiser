"""Blinding: until a pair has its pick, nothing that Helmraiser prints may say which
arm, models or skills a run of the pair used.

``blinding_pair`` (and ``blinded``) is the one decision whether a run's setup is
hidden. ``Blind`` does the hiding: it replaces each hidden value with ``HIDDEN``
and keeps the key, so that the shape of the output does not change.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from helmraiser.ledger.db import Ledger
from helmraiser.pi import skills
from helmraiser.task.model import ModelSpec

HIDDEN = "hidden"
RUN_MODEL_COLUMNS = ("model_plan", "model_implement", "model_fallback", "model_review")
# Helmraiser's own events that record a run's setup: every value is hidden.
SETUP_EVENTS = frozenset({"experiment_arm", "routing", "provider_held"})
# Payload keys that name a model or a provider, wherever they sit in another event.
SETUP_KEYS = frozenset({"model", "models", "provider", "skills", "thinking"})


def blinding_pair(ledger: Ledger, run_id: str) -> dict[str, Any] | None:
    """The unpicked pair that keeps the setup of run ``run_id`` hidden; None when the
    setup may be shown.

    A run is blinded while it is run X or Y of a pair without a pick. So is an
    earlier run of that pair's task: a retried pair keeps its labels and arms and
    leaves its previous runs behind, and their arms would give the labels away.
    """
    pair = ledger.pair_for_run(run_id)
    if pair is not None:
        return pair if pair["pick"] is None else None
    run = ledger.get_run(run_id)
    if run is None:
        return None
    for pair in ledger.unpicked_pairs():
        if pair["task_id"] != run["task_id"]:
            continue
        for key in ("run_x", "run_y"):
            current = ledger.get_run(str(pair[key])) if pair[key] else None
            if current is not None and float(run["started_at"]) <= float(current["started_at"]):
                return pair
    return None


def blinded(ledger: Ledger, run_id: str) -> bool:
    """Whether the setup of run ``run_id`` is hidden (``blinding_pair``)."""
    return blinding_pair(ledger, run_id) is not None


def blinded_runs(ledger: Ledger, told_apart_by: Callable[[str], str] | None = None) -> set[str]:
    """Every run whose setup is hidden now.

    With ``told_apart_by``, only those that a sum keyed by it would tell apart: the
    runs of a pair whose two runs differ in what ``told_apart_by`` makes of their
    models (``str`` for the model itself, or a function to its provider). A sum over
    two arms with the same models says nothing about either, and stays as it is.
    """
    result: set[str] = set()
    for pair in ledger.unpicked_pairs():
        if told_apart_by is not None and not _models_differ(ledger, pair, told_apart_by):
            continue
        for run in ledger.list_runs(task_id=str(pair["task_id"])):
            found = blinding_pair(ledger, str(run["run_id"]))
            if found is not None and found["id"] == pair["id"]:
                result.add(str(run["run_id"]))
    return result


def _models_differ(ledger: Ledger, pair: dict[str, Any], name: Callable[[str], str]) -> bool:
    """Whether the pair's two runs record different models, as ``name`` names them. A
    pair that lacks a run counts as differing."""
    setups = []
    for key in ("run_x", "run_y"):
        run = ledger.get_run(str(pair[key])) if pair[key] else None
        if run is None:
            return True
        setups.append([name(str(run[c])) if run[c] else None for c in RUN_MODEL_COLUMNS])
    return setups[0] != setups[1]


def hidden_setup() -> dict[str, Any]:
    """A blinded run's ``models`` and ``arm``, as ``recorded_setup`` shapes them."""
    return {
        "models": {column.removeprefix("model_"): HIDDEN for column in RUN_MODEL_COLUMNS},
        "arm": {"name": HIDDEN, "overrides": HIDDEN, "replaced": HIDDEN},
    }


def recorded_setup(ledger: Ledger, run: dict[str, Any]) -> dict[str, Any]:
    """A run's recorded setup: its models per stage, and its arm (None without one)."""
    arm = ledger.events(str(run["run_id"]), types=("experiment_arm",))
    return {
        "models": {column.removeprefix("model_"): run[column] for column in RUN_MODEL_COLUMNS},
        "arm": arm[-1]["payload"] if arm else None,
    }


def _model_names(value: Any) -> set[str]:
    """The ways one ``provider/id[:thinking]`` is written: whole, without its
    thinking level, the bare id and the provider."""
    if not value:
        return set()
    spec = ModelSpec.parse(value)
    provider, _, bare = spec.model.partition("/")
    return {spec.render(), spec.model, provider, bare} - {""}


class Blind:
    """Hides the setup of a blinded pair's runs in text and in ledger events."""

    def __init__(self, ledger: Ledger, pair: dict[str, Any], *runs: dict[str, Any]) -> None:
        names: set[str] = set()
        rows = list(runs)
        for key in ("run_x", "run_y"):
            row = ledger.get_run(str(pair[key])) if pair[key] else None
            if row is not None:
                rows.append(row)
        for row in rows:
            for column in RUN_MODEL_COLUMNS:
                names |= _model_names(row[column])
            for iteration in ledger.iterations(str(row["run_id"])):
                names |= _model_names(iteration["model"])
        experiment = ledger.experiment_by_id(int(pair["experiment_id"]))
        for overrides in (experiment["arms"] if experiment is not None else {}).values():
            for model in (overrides.get("models") or {}).values():
                names |= _model_names(model)
        patterns = [
            # A name stands alone: ``o3`` is not hidden inside ``foo3``.
            rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])"
            for name in sorted(names, key=len, reverse=True)
        ]
        # The worker's skill folders, as a path or under ``~``; a run of the
        # ``none`` arm never touches them.
        for root in (skills.SKILL_SETS_ROOT, skills.SKILLS_ROOT):
            for text in {str(root), _under_home(root)}:
                patterns.append(re.escape(text) + r"(?:/[^\s\"'`,;:)\]]*)?")
        self._pattern = re.compile("|".join(patterns))

    def text(self, value: str | None) -> str | None:
        """``value`` with model names and worker skill paths replaced."""
        return self._pattern.sub(HIDDEN, value) if value else value

    def event(self, event: dict[str, Any]) -> dict[str, Any]:
        """A ledger event that may be shown. A pi session's record keeps only its
        ``type``: its messages and tool calls name the model, show its thinking and
        read its skills."""
        payload = event["payload"]
        if event["source"] == "pi" or event["type"] in SETUP_EVENTS:
            shown = {key: value if key == "type" else HIDDEN for key, value in payload.items()}
        else:
            shown = self._walk(payload)
        return {**event, "payload": shown}

    def _walk(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: HIDDEN if key in SETUP_KEYS else self._walk(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._walk(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def _under_home(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def blind_for(ledger: Ledger, run: dict[str, Any]) -> Blind | None:
    """How to hide ``run``'s setup; None for a run whose setup may be shown."""
    pair = blinding_pair(ledger, str(run["run_id"]))
    return Blind(ledger, pair, run) if pair is not None else None


def failure_reason(ledger: Ledger, run: dict[str, Any]) -> str | None:
    """The run's failure reason as it may be shown: a blinded run's names no model."""
    reason = run["failure_reason"]
    blind = blind_for(ledger, run) if reason else None
    return blind.text(reason) if blind is not None else reason
