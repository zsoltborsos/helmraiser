"""Scout draft preparation: a draft the owner changed is no longer Helmraiser's to rewrite."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from helmraiser.ledger.db import Ledger
from helmraiser.loop.intake import IntakeOutcome
from helmraiser.loop.scout import ScoutOutcome, drop_draft, execute_scout
from helmraiser.task.loader import parse_task, write_task
from helmraiser.task.model import TaskSpec, VerifyStep
from helmraiser.utils.markdown import parse_frontmatter
from tests.cli.test_scout_drafts import candidate
from tests.cli.test_scout_requests import config_for
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

STOPPED = "preparation stopped: the draft changed after Helmraiser last wrote it"
DROP_REASON = "not wanted"

Owner = Callable[[Ledger, str], None]


def drop(ledger: Ledger, task_id: str) -> None:
    drop_draft(ledger, ledger.get_task(task_id) or {}, DROP_REASON)


def queue(ledger: Ledger, task_id: str) -> None:
    """What ``helmraiser intake`` does to a draft: rewrite the file and the ledger task."""
    path = Path((ledger.get_task(task_id) or {})["source_path"])
    spec = replace(parse_task(path.read_text(encoding="utf-8"), path, {}), status="queued")
    write_task(spec, path)
    ledger.upsert_task(spec, path.read_text(encoding="utf-8"))


@dataclass
class Prepared:
    ledger: Ledger
    outcome: ScoutOutcome
    intakes: int
    preflights: int

    @property
    def task_id(self) -> str:
        return self.outcome.written_task_ids[0]

    @property
    def path(self) -> Path:
        return Path(self.outcome.written_paths[0])

    @property
    def frontmatter(self) -> dict:
        return parse_frontmatter(self.path.read_text(encoding="utf-8"))["frontmatter"]

    @property
    def verify_names(self) -> list[str]:
        return [step["name"] for step in self.frontmatter.get("verify") or []]

    @property
    def notes(self) -> list[str]:
        return self.ledger.get_scout_draft(self.task_id)["intake_notes"]


def prepare(
    tmp_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    owner: Owner | None = None,
    when: tuple[str, int] = ("never", 0),
) -> Prepared:
    """Scout one candidate whose acceptance check fails on the first attempt.

    ``owner`` acts on the draft during the ``when`` step ("intake" or "preflight", call number).
    Each intake proposes a verify step named ``check-<call number>``.
    """
    ledger = Ledger(tmp_path / "ledger.db")
    adapter = ScriptedPiAdapter({"scout": PiScript(final_text=json.dumps([candidate()]))})
    monkeypatch.setattr("helmraiser.loop.scout._leeboard_brief", lambda repo: None)
    counts = {"intake": 0, "preflight": 0}

    def owner_acts(step: str) -> None:
        counts[step] += 1
        if owner is not None and when == (step, counts[step]):
            owner(ledger, ledger.list_tasks()[0]["task_id"])

    def intake(spec: TaskSpec, *, feedback: str | None = None) -> IntakeOutcome:
        owner_acts("intake")
        name = f"check-{counts['intake']}"
        step = VerifyStep(name, "true")
        return IntakeOutcome(replace(spec, verify=(step,)), {"verify": [name]}, [], [])

    def preflight(repo: Path, steps: list[VerifyStep], **kwargs: object) -> dict:
        owner_acts("preflight")
        command = {
            "name": steps[0].name,
            "command": steps[0].run,
            "exit_code": 1,
            "passed": False,
            "seconds": 0.1,
            "output": "boom",
            "tail": "boom",
            "hints": [],
        }
        return {"commands": [command], "error": None}

    monkeypatch.setattr("helmraiser.loop.scout.run_preflight_steps", preflight)
    outcome = execute_scout(
        tmp_project,
        ledger,
        config_for(tmp_project),
        tmp_path / "data",
        adapter,
        intake,
        force=True,
    )
    assert outcome.outcome == "succeeded", outcome.error
    return Prepared(ledger, outcome, counts["intake"], counts["preflight"])


def test_an_unchanged_draft_gets_both_attempts(tmp_project, tmp_path, monkeypatch):
    result = prepare(tmp_project, tmp_path, monkeypatch)

    assert (result.intakes, result.preflights) == (2, 2)
    assert result.verify_names == ["check-2"]
    assert result.frontmatter["status"] == "draft"
    assert result.ledger.get_task(result.task_id)["status"] == "draft"
    assert result.ledger.get_task(result.task_id)["snapshot"] == result.path.read_text()
    assert STOPPED not in result.notes


# (step the owner acts during, call number) -> (intake calls, preflight calls, verify names kept)
MOMENTS = {
    "during the first intake": (("intake", 1), (1, 0), []),
    "between the attempts": (("preflight", 1), (1, 1), ["check-1"]),
    "during the retry intake": (("intake", 2), (2, 1), ["check-1"]),
}


@pytest.mark.parametrize("moment", MOMENTS)
def test_a_dropped_draft_stays_dropped(tmp_project, tmp_path, monkeypatch, moment):
    when, (intakes, preflights), verify_names = MOMENTS[moment]

    result = prepare(tmp_project, tmp_path, monkeypatch, owner=drop, when=when)

    assert result.frontmatter["status"] == "dropped"
    assert result.frontmatter["drop_reason"] == DROP_REASON
    assert result.verify_names == verify_names
    task = result.ledger.get_task(result.task_id)
    assert task["status"] == "dropped"
    assert task["snapshot"] == result.path.read_text()
    assert STOPPED in result.notes
    assert STOPPED in Path(result.outcome.report_path).read_text()
    assert (result.intakes, result.preflights) == (intakes, preflights)


@pytest.mark.parametrize("moment", MOMENTS)
def test_a_queued_draft_stays_queued(tmp_project, tmp_path, monkeypatch, moment):
    when, (intakes, preflights), verify_names = MOMENTS[moment]

    result = prepare(tmp_project, tmp_path, monkeypatch, owner=queue, when=when)

    assert result.frontmatter["status"] == "queued"
    assert result.verify_names == verify_names
    task = result.ledger.get_task(result.task_id)
    assert task["status"] == "queued"
    assert task["snapshot"] == result.path.read_text()
    assert STOPPED in result.notes
    assert (result.intakes, result.preflights) == (intakes, preflights)


def test_a_draft_edited_by_hand_is_not_overwritten(tmp_project, tmp_path, monkeypatch):
    def edit(ledger: Ledger, task_id: str) -> None:
        path = Path(ledger.get_task(task_id)["source_path"])
        path.write_text(path.read_text() + "\nOwner's own words.\n", encoding="utf-8")

    result = prepare(tmp_project, tmp_path, monkeypatch, owner=edit, when=("intake", 2))

    assert "Owner's own words." in result.path.read_text()
    assert result.verify_names == ["check-1"]
    assert result.frontmatter["status"] == "draft"
    assert STOPPED in result.notes


def test_a_task_queued_in_the_ledger_alone_is_not_overwritten(tmp_project, tmp_path, monkeypatch):
    def queue_in_ledger(ledger: Ledger, task_id: str) -> None:
        assert ledger.set_task_status(task_id, "queued")

    result = prepare(tmp_project, tmp_path, monkeypatch, owner=queue_in_ledger, when=("intake", 2))

    assert result.ledger.get_task(result.task_id)["status"] == "queued"
    assert result.verify_names == ["check-1"]
    assert STOPPED in result.notes
