"""Read-only comparisons over temporary ledgers and real commit-to-commit diffs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from helmraiser.loop import compare
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
from helmraiser.loop.model_report import setup_label
from helmraiser.task.loader import parse_task
from helmraiser.utils.markdown import serialize_frontmatter


def judgement(winner="A") -> dict:
    return {
        "winner": winner,
        "confidence": "high",
        "scores": {label: dict.fromkeys(compare.CRITERIA, 4) for label in ("A", "B")},
        "decisive": "The edge case is covered.",
        "defects": {"A": [], "B": ["Missing edge case."]},
        "merge_advice": "Merge after checking the edge case.",
    }


def seed_task(ledger, project, task_id="task-a", *, variant=None, brief="## Goal\n\nFix it."):
    fm = {
        "id": task_id,
        "title": "Fix it",
        "project": str(project),
        "status": "queued",
        "isolation": "none",
        "models": {"implement": "prov/secret-impl:high"},
    }
    if variant is not None:
        fm["variant_of"] = variant
    text = serialize_frontmatter(fm, brief)
    spec = parse_task(text, Path(project) / (task_id + ".md"), {})
    ledger.upsert_task(spec, text)
    return spec.task_id


def seed_run(ledger, project, task_id, run_id, **overrides):
    ledger.create_run(
        {
            "run_id": run_id,
            "task_id": task_id,
            "project": str(project),
            "status": "landed",
            "policy": "review",
            "pi_version": "test",
            "isolation": "none",
            "model_implement": "prov/secret-impl:high",
            "model_fallback": "prov/secret-fb:high",
            "model_review": "prov/secret-rev:high",
            "model_plan": "prov/secret-plan:high",
            "started_at": 1000,
            "ended_at": 1123,
            "cost_usd": 0.75,
            "escalated": 0,
            "base_commit": None,
            "head_commit": None,
            **overrides,
        }
    )
    return ledger.get_run(run_id)


def git(project, *args):
    return (
        subprocess.run(["git", *args], cwd=project, capture_output=True, check=True)
        .stdout.decode()
        .strip()
    )


def seed_pair(ledger, project, *, variants=False):
    base = git(project, "rev-parse", "HEAD")
    runs = []
    for label in ("a", "b"):
        git(project, "checkout", "-b", "secret-branch-" + label, base)
        (project / (label + ".txt")).write_text("unique addition " + label + "\n")
        git(project, "add", label + ".txt")
        git(project, "commit", "-m", "Add a file")
        head = git(project, "rev-parse", "HEAD")
        task = seed_task(ledger, project, "task-" + label, variant="original" if variants else None)
        runs.append(
            seed_run(ledger, project, task, label * 8 + "-run", base_commit=base, head_commit=head)
        )
    git(project, "checkout", "main")
    return tuple(runs)


@pytest.fixture
def pair(ledger, tmp_project):
    return seed_pair(ledger, tmp_project)


@pytest.mark.parametrize(
    "format", ["bare", "json", "fence", "prose", "objects", "unrelated", "two"]
)
def test_parse_judgement_accepts_the_formats(format):
    answer = json.dumps(judgement())
    text = {
        "bare": answer,
        "json": "```json\n" + answer + "\n```",
        "fence": "```\n" + answer + "\n```",
        "prose": "Here is the answer:\n" + answer + "\nThat is all.",
        "objects": 'Some {} and {"x": 1} before ' + answer,
        "unrelated": '```json\n{"x": 1}\n```\n' + answer,
        "two": "```json\n" + json.dumps(judgement("B")) + "\n```\n```json\n" + answer + "\n```",
    }[format]
    assert parse_judgement(text) == judgement()


@pytest.mark.parametrize(
    "bad",
    [
        "empty",
        "prose",
        "winner",
        "confidence",
        "scores",
        0,
        6,
        3.5,
        True,
        "missing-B",
        "defects",
        "comma",
        "decisive",
        "merge_advice",
        "extra-score",
        "nested-type",
        "defect-item",
    ],
)
def test_parse_judgement_rejects(bad):
    value = judgement()
    if type(bad) in (int, float, bool):
        value["scores"]["A"]["tests"] = bad
    elif bad == "winner":
        value["winner"] = "C"
    elif bad == "confidence":
        value["confidence"] = "certain"
    elif bad == "scores":
        del value["scores"]
    elif bad == "missing-B":
        del value["scores"]["B"]
    elif bad == "defects":
        value["defects"]["A"] = "not a list"
    elif bad in ("decisive", "merge_advice"):
        value[bad] = "  "
    elif bad == "extra-score":
        value["scores"]["A"]["extra"] = 4
    elif bad == "nested-type":
        value["scores"]["A"] = None
    elif bad == "defect-item":
        value["defects"]["A"] = [3]
    text = json.dumps(value)
    if bad == "empty":
        text = ""
    elif bad == "prose":
        text = "No answer today."
    elif bad == "comma":
        text = text[:-1] + ",}"
    with pytest.raises(ValueError, match="judge answer"):
        parse_judgement(text)


def test_parse_judgement_returns_only_the_verdict_keys():
    value = {**judgement(), "extra": "ignored"}
    assert parse_judgement(json.dumps(value)) == judgement()
    value["winner"] = []
    with pytest.raises(ValueError, match="invalid winner"):
        parse_judgement(json.dumps(value) + json.dumps(judgement()))


def test_prompt_is_blind(ledger, pair):
    a, b = (run_facts(ledger, r) for r in pair)
    prompt = build_prompt(a["brief"], a, b)
    forbidden = ["secret", "prov/", "secret-branch-a", "secret-branch-b", "isolation:", "models:"]
    for facts in (a, b):
        forbidden += [facts["run_id"], facts["task_id"], setup_label(facts["setup"])]
    assert all(word not in prompt for word in forbidden)


def test_prompt_labels_follow_the_order_given(ledger, pair):
    facts = [run_facts(ledger, r) for r in pair]
    for a, b in (facts, facts[::-1]):
        prompt = build_prompt(a["brief"], a, b)
        block_a, block_b = prompt.split("## Implementation A")[1].split("## Implementation B")
        assert a["diff"] in block_a and b["diff"] in block_b
        assert b["diff"] not in block_a and a["diff"] not in block_b
        assert "+unique addition" in block_a and "+unique addition" in block_b


def test_prompt_keeps_braces_and_the_brief_once(ledger, pair):
    a, b = (run_facts(ledger, r) for r in pair)
    brief = "## Goal\n\nPreserve {a}, {task} and {}."
    prompt = build_prompt(brief, a, b)
    assert prompt.count(brief) == 1
    assert prompt.endswith("Judge A against B as instructed. Reply with the JSON object only.")


def test_prompt_contains_outcomes_and_reviews(ledger, pair):
    a, b = pair
    ledger.start_iteration(a["run_id"], 1, "session", "prov/secret-impl:high")
    for note in ("first note", "second note"):
        ledger.append_event(
            a["run_id"],
            "review",
            "review_verdict",
            {
                "verdict": "concerns",
                "findings": [{"note": note}],
                "summary": "Look again",
                "evidence_checks": ["must not be included"],
            },
        )
    ledger.append_event(a["run_id"], "review", "review_verdict", {"verdict": "pass"})
    a = run_facts(
        ledger, {**a, "status": "failed", "failure_reason": "verify failed", "escalated": 1}
    )
    b = run_facts(ledger, b)
    prompt = build_prompt(a["brief"], a, b)
    assert "failed (verify failed); iterations: 1; escalated: True" in prompt
    assert '"verdict": "concerns"' in prompt
    assert prompt.index("first note") < prompt.index("second note")
    assert "Look again" in prompt and "evidence_checks" not in prompt
    assert a["reviews"][-1] == {"verdict": "pass"}


def test_long_diff_and_reviews_are_cut(ledger, pair, monkeypatch):
    monkeypatch.setattr(
        compare.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b"x" * 90_000),
    )
    a = run_facts(ledger, pair[0])
    assert a["diff"] == "x" * 80_000 + "\n[diff cut after 80000 characters]\n"
    a["reviews"] = [{"summary": "y" * 13_000}]
    prompt = build_prompt(a["brief"], a, a)
    reviews = prompt.split("- automated review rounds (oldest first):\n```json\n")[1].split(
        "\n```"
    )[0]
    assert reviews == json.dumps(a["reviews"], indent=1)[:12_000]


@pytest.mark.parametrize("commits", [(None, None), (None, "HEAD"), ("HEAD", "does-not-exist")])
def test_run_without_commits_has_no_diff(ledger, pair, commits):
    a = run_facts(ledger, {**pair[0], "base_commit": commits[0], "head_commit": commits[1]})
    assert a["diff"] == ""
    assert "(no diff)" in build_prompt(a["brief"], a, a)


@pytest.mark.parametrize("error", [OSError("no git"), subprocess.TimeoutExpired("git", 60)])
def test_diff_failure_is_no_diff(ledger, pair, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(compare.subprocess, "run", fail)
    assert run_facts(ledger, pair[0])["diff"] == ""


def test_run_facts_fields(ledger, pair):
    run = pair[0]
    ledger.start_iteration(run["run_id"], 1, "session", "p/m")
    facts = run_facts(ledger, {**run, "escalated": 1})
    assert facts["iterations"] == 1 and facts["escalated"] is True
    assert facts["minutes"] == 2.0 and facts["cost_usd"] == 0.75
    assert facts["brief"] == "## Goal\n\nFix it."
    assert facts["setup"] == {
        "plan": "prov/secret-plan:high",
        "implement": "prov/secret-impl:high",
        "fallback": "prov/secret-fb:high",
        "review": "prov/secret-rev:high",
    }
    assert run_facts(ledger, {**run, "ended_at": None})["minutes"] is None
    assert run_facts(ledger, {**run, "started_at": -60, "ended_at": 0})["minutes"] == 1.0
    assert run_facts(ledger, {**run, "task_id": "missing"})["brief"] == ""


def test_resolve_run_prefix_unknown_and_ambiguous(ledger, pair, tmp_project):
    a, b = pair
    assert resolve_run(ledger, a["run_id"]) == a
    assert resolve_run(ledger, "aaa") == a
    with pytest.raises(UnknownRun, match="unknown run: missing"):
        resolve_run(ledger, "missing")
    other = seed_run(ledger, tmp_project, a["task_id"], "aaaaaaaa-another")
    with pytest.raises(UnknownRun, match="ambiguous run prefix aaa") as exc:
        resolve_run(ledger, "aaa")
    assert other["run_id"] in str(exc.value) and a["run_id"] in str(exc.value)
    assert resolve_run(ledger, a["run_id"]) == a


def test_check_runs_rules(pair):
    a, b = pair
    for left, right, message in (
        (a, a, "cannot compare a run with itself"),
        (a, {**b, "project": "/elsewhere"}, "different projects"),
        (a, {**b, "status": "running"}, "bbbbbbbb is still running"),
    ):
        with pytest.raises(CompareError, match=message):
            check_runs(left, right)
    for status in ("landed", "failed", "aborted", "awaiting_approval"):
        check_runs({**a, "status": status}, b)


def test_variant_runs_pick_the_latest_run_of_each_variant(ledger, tmp_project):
    b = seed_task(ledger, tmp_project, "b", variant="original")
    a = seed_task(ledger, tmp_project, "a", variant="original")
    seed_run(ledger, tmp_project, a, "a-old", started_at=1)
    latest_a = seed_run(ledger, tmp_project, a, "a-new", started_at=2)
    seed_run(ledger, tmp_project, b, "b-old", started_at=2)
    latest_b = seed_run(ledger, tmp_project, b, "b-z", started_at=2)
    assert variant_runs(ledger, "original", None) == (latest_a, latest_b)


@pytest.mark.parametrize("count", [0, 1, 3])
def test_variant_runs_refuse_the_wrong_number_of_variants(ledger, tmp_project, count):
    seed_task(ledger, tmp_project, "unrelated", variant="other")
    ids = ["task-" + str(n) for n in range(count)]
    for task in ids:
        seed_task(ledger, tmp_project, task, variant="original")
    with pytest.raises(
        CompareError, match=f"expected two variants of original, found {count}"
    ) as exc:
        variant_runs(ledger, "original", None)
    assert all(task in str(exc.value) for task in ids)


def test_variant_without_run(ledger, tmp_project):
    seed_task(ledger, tmp_project, "a", variant="original")
    seed_task(ledger, tmp_project, "b", variant="original")
    with pytest.raises(CompareError, match="variant a has no run"):
        variant_runs(ledger, "original", None)


def test_variant_runs_project_selection(ledger, tmp_project, tmp_path):
    for project in (tmp_project, tmp_path / "elsewhere"):
        for task_id in ("a", "b"):
            task = seed_task(ledger, project, task_id, variant="original")
            seed_run(ledger, project, task, str(project) + task_id)
    with pytest.raises(CompareError, match="several projects: .*pass --project"):
        variant_runs(ledger, "original", None)
    a, b = variant_runs(ledger, "original", str(tmp_project))
    assert a["project"] == b["project"] == str(tmp_project)
    latest = seed_run(
        ledger, tmp_project, a["task_id"], "latest", started_at=2000, status="running"
    )
    a, b = variant_runs(ledger, "original", str(tmp_project))
    assert a == latest
    with pytest.raises(CompareError, match="still running"):
        check_runs(a, b)


@pytest.mark.parametrize("winner", ["A", "B", "tie"])
def test_build_result_and_winner_run_id(ledger, pair, winner):
    a, b = (run_facts(ledger, r) for r in pair)
    result = build_result(
        a,
        b,
        judgement(winner),
        judge_model="p/judge:high",
        judge_cost=0.02,
        variant_of=None,
        warnings=[],
    )
    assert result["winner_run_id"] == {"A": a["run_id"], "B": b["run_id"], "tie": None}[winner]
    assert "variant_of" not in result
    assert result["judge"] == {"model": "p/judge:high", "cost_usd": 0.02}
    assert not {"brief", "diff", "reviews"} & result["runs"]["A"].keys()
    assert "brief" in a and "diff" in b  # inputs are not mutated
    assert (
        build_result(a, b, judgement(), judge_model="j", judge_cost=0, variant_of="t", warnings=[])[
            "variant_of"
        ]
        == "t"
    )


def test_render_markdown(ledger, pair):
    a, b = (run_facts(ledger, r) for r in pair)
    result = build_result(
        a,
        b,
        judgement(),
        judge_model="p/judge:high",
        judge_cost=0.02,
        variant_of="original",
        warnings=["briefs differ"],
    )
    text = render_markdown(result)
    for expected in (
        "# Comparison: A vs B",
        "Variants of original",
        "p/judge:high",
        "$0.0200",
        a["run_id"][:8],
        b["run_id"][:8],
        setup_label(a["setup"]),
        "Winner: A",
        "Confidence: high",
        "| correctness | 4 | 4 |",
        "Missing edge case.",
        "- warning: briefs differ",
        "| minutes | 2.0 | 2.0 |",
    ):
        assert expected in text
    setup_row = next(line for line in text.splitlines() if line.startswith("| setup |"))
    assert setup_row.count("|") == 4
    result["verdict"] = judgement("tie")
    assert "Winner: tie" in render_markdown(result)
