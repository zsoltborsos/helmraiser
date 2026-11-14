"""Root CLI comparison with a scripted judge; no model calls or persisted output."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, compare_commands
from helmraiser.cli.compare_commands import get_judge_adapter
from helmraiser.ledger.db import Ledger
from helmraiser.pi.protocol import PiEvent, Usage
from tests.loop.test_compare import judgement, seed_pair, seed_run, seed_task
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

runner = CliRunner()


@pytest.fixture(autouse=True)
def config(monkeypatch, configured_config):
    config = configured_config
    monkeypatch.setattr(compare_commands, "load_config", lambda: config)
    return config


@pytest.fixture(autouse=True)
def adapter(monkeypatch):
    adapter = ScriptedPiAdapter(
        {
            "judge": PiScript(
                final_text=json.dumps(judgement()),
                usage=Usage(cost_usd=0.0123),
                files=[("sessions/output.jsonl", "temporary session")],
                events=[PiEvent(type="test", raw={})],
            )
        }
    )
    monkeypatch.setattr(compare_commands, "get_judge_adapter", lambda config: adapter)
    return adapter


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def ledger(data_dir):
    with Ledger(data_dir / "helmraiser.db") as ledger:
        yield ledger


@pytest.fixture
def pair(ledger, tmp_project):
    return seed_pair(ledger, tmp_project)


def invoke(data_dir, *args):
    return runner.invoke(app, ["compare", *args, "--data-dir", str(data_dir)])


def test_compare_two_runs_markdown(data_dir, pair, adapter):
    a, b = pair
    result = invoke(data_dir, a["run_id"], b["run_id"])
    assert result.exit_code == 0, result.output
    assert len(adapter.calls) == 1
    request = adapter.calls[0]
    assert request.stage == "judge" and request.tools == ()
    assert request.context_files is False
    assert request.model.render() == "openai-codex/gpt-6-astra:high"
    assert request.timeout_s == 1800 and request.system_prompt_path is None
    assert request.session_dir == request.cwd / "sessions"
    assert request.session_id == adapter.run_ids[0] + "-judge"
    assert not request.cwd.exists()  # even scripted session files are cleaned up
    assert not request.cwd.is_relative_to(data_dir)
    for forbidden in (
        "secret",
        "prov/",
        a["run_id"],
        b["run_id"],
        a["task_id"],
        b["task_id"],
        "models:",
        "isolation:",
    ):
        assert forbidden not in request.prompt
    for expected in (
        "# Comparison: A vs B",
        "impl=prov/secret-impl:high",
        "fb=prov/secret-fb:high",
        "Winner: A",
        "$0.0123",
    ):
        assert expected in result.output


@pytest.mark.parametrize("as_json", [False, True])
def test_missing_judge(config, data_dir, pair, adapter, ledger, as_json):
    config["profiles"] = {}
    a, b = pair
    before = ledger.list_runs()
    result = invoke(data_dir, a["run_id"], b["run_id"], *(["--json"] if as_json else []))
    assert result.exit_code == 2, result.output
    if as_json:
        assert json.loads(result.output) == {"error": "models_not_configured", "missing": ["judge"]}
    else:
        assert "models not configured: judge" in result.output
    assert not adapter.calls
    assert ledger.list_runs() == before


def test_compare_json_shape(data_dir, pair, adapter):
    a, b = pair
    adapter.scripts["judge"] = PiScript(final_text=json.dumps(judgement("B")))
    result = invoke(data_dir, a["run_id"][:4], b["run_id"][:4], "--json")
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert set(value) == {"judge", "runs", "verdict", "winner_run_id", "warnings"}
    assert set(value["runs"]) == {"A", "B"}
    assert value["winner_run_id"] == b["run_id"]
    assert value["runs"]["A"]["run_id"] == a["run_id"]
    assert not {"brief", "reviews", "diff"} & value["runs"]["A"].keys()
    assert value["verdict"] == judgement("B")


@pytest.mark.parametrize("select_project", [False, True])
def test_compare_variant_of(data_dir, ledger, tmp_project, tmp_path, adapter, select_project):
    projects = [tmp_project, tmp_path / "other"] if select_project else [tmp_project]
    for project in projects:
        for label in ("z", "a"):
            task = seed_task(ledger, project, label, variant="original")
            seed_run(ledger, project, task, str(project) + label + "-old", started_at=1)
            seed_run(ledger, project, task, str(project) + label + "-new", started_at=2)
    args = ["--variant-of", "original", "--json"]
    if select_project:
        ambiguous = invoke(data_dir, *args)
        assert ambiguous.exit_code == 2 and "pass --project" in ambiguous.output
        assert adapter.calls == []
        args += ["--project", str(tmp_project / ".")]
    result = invoke(data_dir, *args)
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert value["variant_of"] == "original"
    assert value["runs"]["A"]["run_id"] == str(tmp_project) + "a-new"
    assert value["runs"]["B"]["run_id"] == str(tmp_project) + "z-new"
    assert len(adapter.calls) == 1


def test_compare_uses_the_project_judge_profile(data_dir, pair, tmp_project, adapter, config):
    a, b = pair
    config["profiles"]["judge"] = {"model": "prov/config-judge", "thinking": "medium"}
    result = invoke(data_dir, a["run_id"], b["run_id"])
    assert result.exit_code == 0
    assert adapter.calls[-1].model.render() == "prov/config-judge:medium"
    (tmp_project / ".helmraiser").mkdir()
    (tmp_project / ".helmraiser/project.toml").write_text('[models]\njudge = "prov/judge-x:low"\n')
    result = invoke(data_dir, a["run_id"], b["run_id"])
    assert result.exit_code == 0, result.output
    assert adapter.calls[-1].model.render() == "prov/judge-x:low"


def test_compare_warns_when_the_briefs_differ(data_dir, pair, ledger, tmp_project, adapter):
    a, b = pair
    seed_task(ledger, tmp_project, "task-b", brief="## Goal\n\nA different brief.")
    result = invoke(data_dir, a["run_id"], b["run_id"], "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["warnings"] == ["task briefs differ; the prompt uses run A's"]
    assert "Fix it." in adapter.calls[0].prompt
    assert "A different brief." not in adapter.calls[0].prompt


@pytest.mark.parametrize(
    "mistake",
    ["none", "one", "three", "both", "project", "self", "projects", "running", "one-variant"],
)
@pytest.mark.parametrize("as_json", [False, True])
def test_compare_argument_mistakes_exit_2(
    data_dir, pair, ledger, tmp_project, adapter, mistake, as_json
):
    a, b = pair
    args = [a["run_id"], b["run_id"]]
    if mistake == "none":
        args = []
    elif mistake == "one":
        args = [a["run_id"]]
    elif mistake == "three":
        args += ["extra"]
    elif mistake == "both":
        args += ["--variant-of", "original"]
    elif mistake == "project":
        args += ["--project", str(tmp_project)]
    elif mistake == "self":
        args = [a["run_id"], a["run_id"]]
    elif mistake == "projects":
        ledger.update_run(b["run_id"], project="/other")
    elif mistake == "running":
        ledger.update_run(b["run_id"], status="running")
    elif mistake == "one-variant":
        seed_task(ledger, tmp_project, "only", variant="original")
        args = ["--variant-of", "original"]
    result = invoke(data_dir, *args, *(["--json"] if as_json else []))
    assert result.exit_code == 2, result.output
    assert adapter.calls == []
    if as_json and mistake != "three":  # Click itself rejects excess positionals
        assert "error" in json.loads(result.output)


@pytest.mark.parametrize("ambiguous", [False, True])
@pytest.mark.parametrize("as_json", [False, True])
def test_compare_unknown_and_ambiguous_runs_exit_1(
    data_dir, pair, ledger, tmp_project, adapter, ambiguous, as_json
):
    a, b = pair
    ref = "missing"
    if ambiguous:
        seed_run(ledger, tmp_project, a["task_id"], "aaaaaaaa-another")
        ref = "aaa"
    result = invoke(data_dir, ref, b["run_id"], *(["--json"] if as_json else []))
    assert result.exit_code == 1
    expected = "ambiguous run prefix" if ambiguous else "unknown run"
    assert expected in result.output
    assert adapter.calls == []
    if as_json:
        assert expected in json.loads(result.output)["error"]


@pytest.mark.parametrize("as_json", [False, True])
def test_compare_judge_failure_exit_1(data_dir, pair, adapter, as_json):
    adapter.scripts["judge"] = PiScript(ok=False, error_kind="provider", error_message="boom")
    result = invoke(data_dir, *[r["run_id"] for r in pair], *(["--json"] if as_json else []))
    assert result.exit_code == 1
    assert "judge session failed (provider): boom" in result.output
    assert len(adapter.calls) == 1
    if as_json:
        assert json.loads(result.output) == {"error": "judge session failed (provider): boom"}


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("invalid_verdict", [False, True])
def test_compare_unparsable_answer_exit_1(data_dir, pair, adapter, as_json, invalid_verdict):
    text = (
        json.dumps({**judgement(), "winner": "C"}) if invalid_verdict else "raw text " + "x" * 5000
    )
    adapter.scripts["judge"] = PiScript(final_text=text)
    result = invoke(data_dir, *[r["run_id"] for r in pair], *(["--json"] if as_json else []))
    assert result.exit_code == 1
    assert "judge answer is not a valid verdict" in result.output
    reason = "invalid winner" if invalid_verdict else "no JSON verdict"
    assert reason in result.output
    assert len(adapter.calls) == 1
    if as_json:
        assert json.loads(result.output)["raw"] == text[:4000]
    else:
        assert text[:4000] in result.output
        if len(text) > 4000:
            assert text[:4001] not in result.output


def test_compare_accepts_a_fenced_answer_with_prose(data_dir, pair, adapter):
    adapter.scripts["judge"] = PiScript(
        final_text="Here:\n```json\n" + json.dumps(judgement()) + "\n```\nDone."
    )
    result = invoke(data_dir, *[r["run_id"] for r in pair])
    assert result.exit_code == 0, result.output
    assert "Winner: A" in result.output


def test_compare_writes_nothing(data_dir, pair, ledger, adapter):
    ledger.start_iteration(pair[0]["run_id"], 1, "s", "prov/secret-impl:high")
    ledger.append_event(
        pair[0]["run_id"], "review", "review_verdict", {"verdict": "pass", "findings": []}
    )

    def snapshot():
        return {
            "runs": ledger.list_runs(),
            "tasks": ledger.list_tasks(),
            "events": [ledger.events(r["run_id"]) for r in pair],
            "iterations": [ledger.iterations(r["run_id"]) for r in pair],
            "files": sorted(str(p.relative_to(data_dir)) for p in data_dir.rglob("*")),
        }

    before = snapshot()
    result = invoke(data_dir, *[r["run_id"] for r in pair], "--json")
    assert result.exit_code == 0, result.output
    assert snapshot() == before
    assert not adapter.calls[0].cwd.exists()


@pytest.mark.parametrize(
    "args, code", [(["missing", "other"], 1), (["--variant-of", "original"], 2)]
)
def test_missing_database_is_not_created(data_dir, adapter, args, code):
    assert not data_dir.exists()
    result = invoke(data_dir, *args, "--json")
    assert result.exit_code == code
    assert "error" in json.loads(result.output)
    assert not data_dir.exists() and adapter.calls == []


def test_invalid_project_profile_is_validation_error(data_dir, pair, tmp_project, adapter):
    (tmp_project / ".helmraiser").mkdir()
    (tmp_project / ".helmraiser/project.toml").write_text('[models]\nunknown = "p/m"\n')
    result = invoke(data_dir, *[r["run_id"] for r in pair], "--json")
    assert result.exit_code == 2
    assert "error" in json.loads(result.output)
    assert adapter.calls == []


def test_default_adapter_has_no_wrapper():
    adapter = get_judge_adapter({"pi_bin": "/custom/pi"})
    assert adapter.pi_bin == "/custom/pi"
    assert adapter.argv_wrapper is None
