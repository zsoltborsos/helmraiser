"""``helmraiser eval review|report`` with the scripted pi; nothing calls a model."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, eval_commands
from helmraiser.pi.adapter import PiJsonAdapter
from tests.loop.test_eval_review import build_fixture, case_entry, script, skill, write_cases

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


def test_review_repeat_and_review_model(fixture, cases_file, fake_pi, tmp_path):
    result = invoke(
        "review",
        str(cases_file),
        "--variant",
        "v1-noskills",
        "--case",
        "miss-1",
        "--repeat",
        "2",
        "--review-model",
        "prov/strong:xhigh",
        "--dry-run",
        "--json",
        "--data-dir",
        str(fixture.data_dir),
    )
    assert result.exit_code == 0, result.output
    pairs = json.loads(result.output)["pairs"]
    assert [(p["variant"], p["rep"]) for p in pairs] == [
        ("v1-noskills@prov/strong:xhigh", 1),
        ("v1-noskills@prov/strong:xhigh", 2),
    ]
    argv = pairs[0]["sessions"][0]["argv"]
    assert argv[argv.index("--model") + 1] == "prov/strong:xhigh"
    assert pairs[0]["model"] == "prov/strong:xhigh"
    assert pairs[0]["judge_model"] == "openai-codex/gpt-6-astra:high"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--repeat", "0"], "--repeat must be at least 1"),
        (["--review-model", "no-provider"], "--review-model must be provider/id"),
    ],
)
def test_review_repeat_and_model_validation(fixture, cases_file, args, message):
    result = invoke(
        "review",
        str(cases_file),
        "--variant",
        "v0-baseline",
        *args,
        "--json",
        "--data-dir",
        str(fixture.data_dir),
    )
    assert result.exit_code == 2 and message in json.loads(result.output)["error"]


def skill_args(argv):
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--skill"]


@pytest.mark.parametrize("name", ["v2-skills", "v4-twoaxis-skills"])
def test_review_skill_variant_without_skill_exits_2(fixture, cases_file, name):
    result = invoke(
        "review", str(cases_file), "--variant", name, "--data-dir", str(fixture.data_dir)
    )
    assert result.exit_code == 2
    assert f"Error: variant {name} needs --skill" in result.output


def test_review_skill_without_skill_md_exits_2(fixture, cases_file, tmp_path, fake_pi):
    bare = tmp_path / "bare"
    bare.mkdir()
    result = invoke(
        "review",
        str(cases_file),
        "--variant",
        "v2-skills",
        "--skill",
        str(skill(tmp_path / "skills", "tdd")),
        "--skill",
        str(bare),
        "--json",
        "--data-dir",
        str(fixture.data_dir),
    )
    assert result.exit_code == 2
    assert json.loads(result.output)["error"] == f"--skill {bare.resolve()} has no SKILL.md"
    assert fake_pi.calls() == []


def test_review_skill_is_ignored_by_variants_without_skills(fixture, cases_file, tmp_path, fake_pi):
    result = invoke(
        "review",
        str(cases_file),
        "--variant",
        "v1-noskills",
        "--case",
        "miss-1",
        "--skill",
        str(tmp_path / "gone"),
        "--dry-run",
        "--json",
        "--data-dir",
        str(fixture.data_dir),
    )
    assert result.exit_code == 0, result.output
    (pair,) = json.loads(result.output)["pairs"]
    assert skill_args(pair["sessions"][0]["argv"]) == []


def test_review_skill_dirs_are_made_absolute_and_passed_in_order(
    fixture, cases_file, tmp_path, monkeypatch, wiring
):
    home = tmp_path / "home"
    first = skill(home / "skills", "codebase-design").resolve()
    second = skill(tmp_path / "rel" / "skills", "tdd").resolve()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    result = invoke(
        "review",
        str(cases_file),
        "--variant",
        "v2-skills",
        "--case",
        "miss-1",
        "--skill",
        "~/skills/codebase-design",
        "--skill",
        "rel/skills/tdd",
        "--dry-run",
        "--json",
        "--data-dir",
        str(fixture.data_dir),
    )
    assert result.exit_code == 0, result.output
    (pair,) = json.loads(result.output)["pairs"]
    assert skill_args(pair["sessions"][0]["argv"]) == [str(first), str(second)]
    assert wiring[0][1] == (first, second)


def test_held_out_case_file_gets_its_own_out_dir_and_report(fixture, fake_pi, tmp_path):
    script(fake_pi)
    held_out = tmp_path / "held-out"
    held_out.mkdir()
    cases = write_cases(held_out / "cases.json", [case_entry("miss-h1")])
    result = invoke(
        "review", str(cases), "--variant", "v0-baseline", "--data-dir", str(fixture.data_dir)
    )
    assert result.exit_code == 0, result.output
    report = invoke("report", str(held_out / "out"), "--json")
    assert report.exit_code == 0, report.output
    assert json.loads(report.output)["variants"]["v0-baseline"]["pairs"] == 1
    assert (held_out / "out" / "report.md").is_file()
