"""``helmraiser eval review|report`` with the scripted pi; nothing calls a model."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, eval_commands
from helmraiser.pi.adapter import PiJsonAdapter
from tests.loop.test_eval_review import build_fixture, case_entry, script, write_cases

runner = CliRunner()


@pytest.fixture(autouse=True)
def wiring(monkeypatch, configured_config, fake_pi):
    monkeypatch.setattr(eval_commands, "load_config", lambda: configured_config)
    seen = []

    def review_adapter(config, project, extra, data_dir):
        seen.append((project, extra, data_dir))
        return PiJsonAdapter(pi_bin=str(fake_pi.bin))

    monkeypatch.setattr(eval_commands, "get_review_adapter", review_adapter)
    monkeypatch.setattr(
        eval_commands, "get_judge_adapter", lambda config: PiJsonAdapter(pi_bin=str(fake_pi.bin))
    )
    return seen


def invoke(*args):
    return runner.invoke(app, ["eval", *args])


@pytest.fixture
def fixture(tmp_project, tmp_path):
    built = build_fixture(tmp_project, tmp_path)
    yield built
    built.ledger.close()


@pytest.fixture
def cases_file(tmp_path):
    (tmp_path / "cases").mkdir()
    return write_cases(
        tmp_path / "cases" / "cases.json",
        [case_entry(), case_entry("clean-1", bucket="clean", axis=None, label=None)],
    )


def test_review_dry_run_json(fixture, cases_file, fake_pi, wiring):
    result = invoke(
        "review",
        str(cases_file),
        "--variant",
        "v0-baseline",
        "--case",
        "miss-1",
        "--dry-run",
        "--json",
        "--data-dir",
        str(fixture.data_dir),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True and fake_pi.calls() == []
    (pair,) = payload["pairs"]
    assert pair["case"] == "miss-1" and pair["sessions"][0]["argv"][0] == str(fake_pi.bin)
    assert pair["reviewed_commit"] == fixture.commits[1]
    assert wiring[0][2] == fixture.data_dir
    assert not (cases_file.parent / "out" / "results.jsonl").exists()


def test_review_then_report(fixture, cases_file, fake_pi, tmp_path):
    script(fake_pi, cost=0.25)
    out = tmp_path / "results"
    args = ["--variant", "v3-twoaxis", "--out", str(out), "--data-dir", str(fixture.data_dir)]
    result = invoke("review", str(cases_file), *args)
    assert result.exit_code == 0, result.output
    assert "miss-1 v3-twoaxis: concerns, found" in result.output
    assert "clean-1 v3-twoaxis: concerns, 2 finding(s), 1 severe" in result.output
    again = invoke("review", str(cases_file), *args, "--json")
    assert again.exit_code == 0 and json.loads(again.output)["pairs"] == []
    report = invoke("report", str(out), "--json")
    assert report.exit_code == 0, report.output
    payload = json.loads(report.output)
    variant = payload["variants"]["v3-twoaxis"]
    assert variant["recall"]["miss"]["found"] == 1
    assert variant["clean"]["severe"] == 1
    assert (out / "report.md").read_text().startswith("# Review replay report")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--variant", "v9"], "unknown variant v9"),
        (["--variant", "v0-baseline", "--case", "nope"], "unknown case nope"),
        (["--variant", "v0-baseline", "--max-cost", "-1"], "must not be negative"),
    ],
)
def test_review_validation_exits_2(fixture, cases_file, args, message):
    result = invoke("review", str(cases_file), *args, "--json", "--data-dir", str(fixture.data_dir))
    assert result.exit_code == 2
    assert message in json.loads(result.output)["error"]


def test_review_unknown_run_exits_2(fixture, tmp_path):
    cases = write_cases(tmp_path / "c.json", [case_entry(run_id="nope")])
    result = invoke(
        "review",
        str(cases),
        "--variant",
        "v0-baseline",
        "--json",
        "--data-dir",
        str(fixture.data_dir),
    )
    assert result.exit_code == 2 and "unknown run nope" in result.output


def test_review_error_pair_exits_1(fixture, tmp_path, fake_pi):
    cases = write_cases(tmp_path / "c.json", [case_entry(review=5)])
    result = invoke(
        "review", str(cases), "--variant", "v0-baseline", "--data-dir", str(fixture.data_dir)
    )
    assert result.exit_code == 1
    assert "miss-1 v0-baseline: error: run" in result.output


def test_report_without_results_exits_2(tmp_path):
    result = invoke("report", str(tmp_path), "--json")
    assert result.exit_code == 2 and "no results" in result.output
