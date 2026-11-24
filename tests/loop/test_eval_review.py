"""Review replay: case validation, reconstruction, variants, sessions, resume and scoring.

Reviewer and judge sessions run the scripted ``fake_pi`` (no wrapper); nothing calls a model.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from helmraiser.ledger.db import Ledger
from helmraiser.loop import eval_review
from helmraiser.loop.eval_review import (
    VARIANTS,
    Case,
    EvalContext,
    EvalError,
    ReplayError,
    ReplayIsolation,
    build_report,
    load_cases,
    merge_sessions,
    project_skill_dirs,
    read_results,
    reconstruct,
    render_report,
    replay_prompt,
    replay_worktree,
    run_eval,
    select,
    skill_read_paths,
    variant_args,
)
from helmraiser.loop.review import build_review_prompt
from helmraiser.pi.adapter import PiJsonAdapter
from helmraiser.pi.directives import render_review_directives
from helmraiser.task.loader import parse_task
from helmraiser.task.model import ModelSpec
from helmraiser.utils.markdown import serialize_frontmatter

RUN_ID = "11111111-2222-4333-8444-555555555555"
LIVE_WORKTREE = "/live/worktrees/worktree-fix-it-" + RUN_ID


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def commit_file(project: Path, name: str, text: str) -> str:
    (project / name).write_text(text)
    git(project, "add", name)
    git(project, "commit", "-q", "-m", f"add {name}")
    return git(project, "rev-parse", "HEAD")


def label(**overrides: Any) -> dict[str, Any]:
    return {
        "summary": "the helper swallows errors",
        "file": "a.py",
        "severity_expected": "severe",
        "rule": {"source": "AGENTS.md:3", "text": "Never swallow errors.", "written": True},
        **overrides,
    }


def case_entry(case_id: str = "miss-1", **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": case_id,
        "run_id": RUN_ID,
        "review": 1,
        "bucket": "miss",
        "axis": "standards",
        "label": label(),
    }
    entry.update(overrides)
    return entry


def write_cases(path: Path, cases: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps({"name": "replay-test", "cases": cases}))
    return path


def make_case(**overrides: Any) -> Case:
    fields: dict[str, Any] = {
        "id": "miss-1",
        "run_id": RUN_ID,
        "review": 1,
        "bucket": "miss",
        "axis": "standards",
        "label": label(),
        "seed_patch": None,
    }
    fields.update(overrides)
    return Case(**fields)


class Fixture:
    def __init__(self, project: Path, data_dir: Path, ledger: Ledger, commits: list[str]):
        self.project = project
        self.data_dir = data_dir
        self.ledger = ledger
        self.commits = commits
        self.base = commits[0]
        self.run_dir = data_dir / "runs" / RUN_ID


def build_fixture(tmp_project: Path, tmp_path: Path) -> Fixture:
    """A run with two reviews: review 2 had a provider retry (one more spilled prompt)."""
    project = tmp_project
    (project / "AGENTS.md").write_text("# Rules\n\nNever swallow errors.\n")
    base = commit_file(project, "AGENTS.md", "# Rules\n\nNever swallow errors.\n")
    data_dir = tmp_path / "data"
    ledger = Ledger(data_dir / "helmraiser.db")
    text = serialize_frontmatter(
        {"id": "fix-it", "title": "Fix it", "project": str(project), "isolation": "none"},
        "## Goal\n\nAdd the helper.",
    )
    spec = parse_task(text, project / "fix-it.md", {})
    ledger.upsert_task(spec, text)
    ledger.create_run(
        {
            "run_id": RUN_ID,
            "task_id": spec.task_id,
            "project": str(project),
            "worktree_path": LIVE_WORKTREE,
            "status": "landed",
            "policy": "review",
            "pi_version": "test",
            "isolation": "process",
            "model_implement": "prov/impl:high",
            "model_review": "prov/rev:high",
            "base_commit": base,
        }
    )
    run_dir = data_dir / "runs" / RUN_ID
    (run_dir / "evidence").mkdir(parents=True)
    (run_dir / "evidence" / "out.txt").write_text("all green\n")
    ledger.append_event(RUN_ID, "helmraiser", "worktree_created", {"base": base})
    commits = [base]
    spills = 0
    for n, (name, body) in enumerate((("a.py", "x = 1\n"), ("b.py", "y = 2\n")), start=1):
        sha = commit_file(project, name, body)
        commits.append(sha)
        ledger.start_iteration(RUN_ID, n, RUN_ID + "-implement", "prov/impl:high")
        ledger.finish_iteration(
            RUN_ID, n, agent_ok=True, commit_sha=sha, verify_passed=True, verify_results=[]
        )
        ledger.append_event(RUN_ID, "helmraiser", "stage_start", {"stage": "review"}, "review")
        diff = git(project, "diff", f"{base}..{sha}") + "\n"
        prompt = build_review_prompt(
            spec,
            diff_path=run_dir / "diff.patch",
            diff_text=diff,
            verify_results=[],
            evidence_results=[],
            evidence_dir=run_dir / "evidence",
        )
        launches = 2 if n == 2 else 1
        if n == 2:
            ledger.append_event(RUN_ID, "helmraiser", "retry", {"error_kind": "provider"}, "review")
        for _ in range(launches):
            spills += 1
            suffix = "" if spills == 1 else f"-{spills}"
            (run_dir / f"prompt-review-1{suffix}.md").write_text(prompt)
        ledger.append_event(RUN_ID, "helmraiser", "review_verdict", {"verdict": "pass"}, "review")
    (run_dir / "directives-review.md").write_text(
        render_review_directives(
            run_id=RUN_ID, title="Fix it", worktree=LIVE_WORKTREE, project_notes="Use tabs."
        )
    )
    return Fixture(project, data_dir, ledger, commits)


@pytest.fixture
def fixture(tmp_project: Path, tmp_path: Path):
    built = build_fixture(tmp_project, tmp_path)
    yield built
    built.ledger.close()


def verdict_text(verdict: str, findings: list[dict[str, Any]]) -> str:
    block = json.dumps({"verdict": verdict, "findings": findings, "evidence_checks": []})
    return f"Reviewed.\n\n```json\n{block}\n```\n"


FINDING = {"severity": "minor", "file": "a.py", "line": 1, "note": "swallows errors"}


# ── case file ────────────────────────────────────────────────────────


def test_load_cases_accepts_every_bucket_and_meta(tmp_path):
    seed = tmp_path / "seed.patch"
    seed.write_text("")
    path = write_cases(
        tmp_path / "cases.json",
        [
            case_entry("miss-1", meta={"reviewed_commit": "abc", "review_prompt": "~/p.md"}),
            case_entry("found-1", bucket="found", axis="spec"),
            case_entry("under-1", bucket="undercall", axis=None, label=label(rule=None)),
            case_entry("seeded-1", bucket="seeded", seed={"patch": str(seed)}),
            case_entry("clean-1", bucket="clean", axis=None, label=None),
        ],
    )
    name, cases = load_cases(path)
    assert name == "replay-test"
    assert [c.bucket for c in cases] == ["miss", "found", "undercall", "seeded", "clean"]
    assert cases[0].meta["reviewed_commit"] == "abc" and cases[1].meta == {}
    assert cases[0].rule_written is True and cases[2].rule_written is None
    assert cases[3].seed_patch == seed


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (case_entry(bucket="regression"), "unknown bucket 'regression'"),
        (case_entry(axis="style"), "axis must be"),
        (case_entry(review=0), "review must be"),
        (case_entry(bucket="clean", axis=None), "a clean case has no label"),
        (case_entry(label=None), "label must be an object"),
        (case_entry(label=label(severity_expected="major")), "severity_expected"),
        (case_entry(label=label(rule={"written": "yes"})), "rule.written"),
        (case_entry(bucket="seeded"), "needs seed.patch"),
        (case_entry(seed={"patch": "/x.patch"}), "only a seeded case"),
        (case_entry(meta=[]), "meta must be an object"),
        (case_entry(meta={"reviewed_commit": 7}), "meta.reviewed_commit"),
        (case_entry(id="../escape"), "id must match"),
    ],
)
def test_load_cases_rejects_bad_cases(tmp_path, entry, message):
    with pytest.raises(EvalError, match=message):
        load_cases(write_cases(tmp_path / "cases.json", [entry]))


def test_load_cases_rejects_duplicates_and_bad_files(tmp_path):
    with pytest.raises(EvalError, match="duplicate case id"):
        load_cases(write_cases(tmp_path / "cases.json", [case_entry(), case_entry()]))
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(EvalError, match="not valid JSON"):
        load_cases(tmp_path / "bad.json")


def test_select_rejects_unknown_variants_and_cases():
    cases = [make_case()]
    with pytest.raises(EvalError, match="unknown variant v9"):
        select(cases, [], ["v9"])
    with pytest.raises(EvalError, match="unknown case nope"):
        select(cases, ["nope"], ["v0-baseline"])
    chosen, variants = select(cases, [], ["v1-noskills", "v0-baseline", "v1-noskills"])
    assert chosen == cases and [v.name for v in variants] == ["v1-noskills", "v0-baseline"]


# ── reconstruction ───────────────────────────────────────────────────


def test_reconstruct_maps_review_n_to_its_prompt_and_commit(fixture):
    first = reconstruct(make_case(review=1), fixture.ledger, fixture.data_dir / "runs")
    second = reconstruct(make_case(review=2), fixture.ledger, fixture.data_dir / "runs")
    assert first.prompt_file.name == "prompt-review-1.md"
    assert first.commit == fixture.commits[1] and first.base == fixture.base
    # Review 2 spilled twice (a provider retry); its first spill is prompt -2.
    assert second.prompt_file.name == "prompt-review-1-2.md"
    assert second.commit == fixture.commits[2]
    assert second.diff_check == "exact" and "b.py" in second.diff_text
    assert second.title == "Fix it"
    assert second.notes_section == "## Project notes (Helmraiser)\n\nUse tabs."


def test_reconstruct_checks_case_meta(fixture):
    runs = fixture.data_dir / "runs"
    agreeing = make_case(
        review=2,
        meta={
            "reviewed_commit": fixture.commits[2],
            "review_prompt": str(fixture.run_dir / "prompt-review-1-2.md"),
        },
    )
    assert reconstruct(agreeing, fixture.ledger, runs).commit == fixture.commits[2]
    wrong_commit = make_case(review=2, meta={"reviewed_commit": fixture.commits[1]})
    with pytest.raises(ReplayError, match="meta.reviewed_commit .* disagrees"):
        reconstruct(wrong_commit, fixture.ledger, runs)
    wrong_prompt = make_case(meta={"review_prompt": str(fixture.run_dir / "prompt-review-1-2.md")})
    with pytest.raises(ReplayError, match="meta.review_prompt .* disagrees"):
        reconstruct(wrong_prompt, fixture.ledger, runs)


def test_reconstruct_refuses_what_it_cannot_tell_apart(fixture):
    runs = fixture.data_dir / "runs"
    with pytest.raises(ReplayError, match="has 2 review"):
        reconstruct(make_case(review=3), fixture.ledger, runs)
    (fixture.run_dir / "prompt-review-1-3.md").unlink()
    with pytest.raises(ReplayError, match="2 stored review prompt"):
        reconstruct(make_case(), fixture.ledger, runs)


def test_reconstruct_needs_a_commit_that_reproduces_the_diff(fixture):
    prompt = fixture.run_dir / "prompt-review-1.md"
    prompt.write_text(prompt.read_text().replace("x = 1", "x = 9"))
    with pytest.raises(ReplayError, match="reproduces the diff"):
        reconstruct(make_case(), fixture.ledger, fixture.data_dir / "runs")


def test_only_evidence_written_by_review_n_is_copied(fixture, tmp_path):
    """A later step (a rework's evidence) rewrites evidence/: such files are withheld."""
    later = fixture.run_dir / "evidence" / "junit.xml"
    later.write_text("<testsuite tests='38'/>")
    log = fixture.run_dir / "logs" / "verify.log"
    log.parent.mkdir()
    log.write_text("old log\n")
    future = time.time() + 3600
    os.utime(later, (future, future))
    os.utime(log, (1_000_000, 1_000_000))  # written well before review 1
    replay = reconstruct(make_case(), fixture.ledger, fixture.data_dir / "runs")
    run_dir = tmp_path / "replay-run"
    copied, withheld = eval_review.prepare_run_dir(replay, run_dir, replay.diff_text)
    assert copied == ["evidence/out.txt", "logs/verify.log"]
    assert withheld == ["evidence/junit.xml"]
    assert (run_dir / "evidence" / "out.txt").read_text() == "all green\n"
    assert (run_dir / "logs" / "verify.log").is_file()
    assert not (run_dir / "evidence" / "junit.xml").exists()
    # The prompt is left as stored, even where it names a withheld file.
    stored = replay.prompt.replace(str(fixture.run_dir), str(run_dir))
    assert replay_prompt(replay, replay.diff_text, tmp_path / "wt", run_dir) == stored


def test_legacy_run_dir_and_worktree_paths_are_relocated(fixture, tmp_path):
    """Squadron-era prompts name ~/.squadron/runs/<id> and other worktree roots."""
    replay = reconstruct(make_case(), fixture.ledger, fixture.data_dir / "runs")
    run_dir, worktree = tmp_path / "run", tmp_path / "wt"
    text = "\n".join(
        [
            f"- junit.xml: `~/.squadron/runs/{RUN_ID}/evidence/x.xml`",
            f"Full output: `/home/zsolt/.squadron/runs/{RUN_ID}/logs/verify.log`",
            f"- shot.png (image, attached): @/srv/old-data/runs/{RUN_ID}/evidence/shot.png",
            f"see /mnt/data/squadron-worktrees/proj/worktree-fix-it-{RUN_ID}/src/a.py.",
            f"{LIVE_WORKTREE}/b.py and {fixture.run_dir}/diff.patch",
            f"untouched: /srv/runs/{RUN_ID}-other and runs/{RUN_ID}",
        ]
    )
    assert eval_review.relocate(text, replay, worktree, run_dir).splitlines() == [
        f"- junit.xml: `{run_dir}/evidence/x.xml`",
        f"Full output: `{run_dir}/logs/verify.log`",
        f"- shot.png (image, attached): @{run_dir}/evidence/shot.png",
        f"see {worktree}/src/a.py.",
        f"{worktree}/b.py and {run_dir}/diff.patch",
        f"untouched: /srv/runs/{RUN_ID}-other and runs/{RUN_ID}",
    ]


def test_dry_run_reports_paths_the_replay_cannot_resolve(context, fixture, fake_pi):
    prompt = fixture.run_dir / "prompt-review-1.md"
    prompt.write_text(
        prompt.read_text()
        + f"\n- out.txt: `~/.squadron/runs/{RUN_ID}/evidence/out.txt`"
        + f"\n- x.xml: `~/.squadron/runs/{RUN_ID}/evidence/x.xml`"
        + f"\nsee {LIVE_WORKTREE}/a.py, {LIVE_WORKTREE}/gone.py."
        + f"\n  at {LIVE_WORKTREE}/a.py:1:5 and {LIVE_WORKTREE}/gone.py:12"
    )
    result = run_eval(context, [make_case()], [VARIANTS["v3-twoaxis"]], dry_run=True)
    (pair,) = result["pairs"]
    assert pair["unresolved_paths"] == ["run/evidence/x.xml", "worktree/gone.py"]
    assert fake_pi.calls() == []


def test_reconstruct_needs_review_n_to_have_a_verdict(fixture):
    fixture.ledger.append_event(RUN_ID, "helmraiser", "stage_start", {"stage": "review"}, "review")
    (fixture.run_dir / "prompt-review-1-4.md").write_text("# Review: Fix it\n")
    with pytest.raises(ReplayError, match="review 3 of run .* recorded no verdict"):
        reconstruct(make_case(review=3), fixture.ledger, fixture.data_dir / "runs")


def test_seeded_worktree_reviews_the_seeded_diff(fixture, tmp_path):
    seed = tmp_path / "seed.patch"
    seed.write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n x = 1\n+SEEDED = 1\n"
    )
    case = make_case(bucket="seeded", seed_patch=seed)
    replay = reconstruct(case, fixture.ledger, fixture.data_dir / "runs")
    with replay_worktree(replay, "seed-test") as (worktree, diff):
        assert (worktree / "a.py").read_text() == "x = 1\nSEEDED = 1\n"
        assert git(worktree, "rev-parse", "HEAD") == fixture.commits[1]
        prompt = replay_prompt(replay, diff, worktree, tmp_path / "run")
        assert "+SEEDED = 1" in prompt and "+SEEDED = 1" not in replay.prompt
        assert prompt.count("## Diff") == 1
    assert not worktree.exists()
    assert str(worktree) not in git(fixture.project, "worktree", "list")


def test_seed_that_does_not_apply_fails_and_cleans_up(fixture, tmp_path):
    seed = tmp_path / "seed.patch"
    seed.write_text("diff --git a/zz.py b/zz.py\n--- a/zz.py\n+++ b/zz.py\n@@ -1 +1 @@\n-a\n+b\n")
    replay = reconstruct(
        make_case(bucket="seeded", seed_patch=seed), fixture.ledger, fixture.data_dir / "runs"
    )
    with pytest.raises(ReplayError, match="seed patch does not apply"):
        with replay_worktree(replay, "bad-seed"):
            pass
    assert "bad-seed" not in git(fixture.project, "worktree", "list")


# ── variants ─────────────────────────────────────────────────────────


def skill(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test\n---\n")
    return path


def install_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, ...]:
    skills = tuple(skill(tmp_path / "hub", name) for name in ("codebase-design", "tdd"))
    monkeypatch.setattr(eval_review, "HUB_SKILLS", skills)
    return skills


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return install_hub(tmp_path, monkeypatch)


def test_project_skills_are_found_up_to_the_repo_root(tmp_path):
    outer = tmp_path / "outer"
    skill(outer / ".agents" / "skills", "above-root")
    repo = outer / "repo"
    (repo / ".git").mkdir(parents=True)
    root_skill = skill(repo / ".pi" / "skills", "root-skill")
    nested = skill(repo / ".pi" / "skills" / "group", "nested")
    sub = repo / "pkg"
    sub_skill = skill(sub / ".agents" / "skills", "sub-skill")
    skill(root_skill, "inside-a-skill")  # a skill's own subfolders are not more skills
    assert project_skill_dirs(sub) == [sub_skill, nested, root_skill]


def test_variant_args(hub, tmp_path):
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    own = skill(worktree / ".agents" / "skills", "own")
    assert variant_args(VARIANTS["v0-baseline"], worktree) == ((), ())
    assert variant_args(VARIANTS["v1-noskills"], worktree) == (("--no-skills",), ())
    assert variant_args(VARIANTS["v3-twoaxis"], worktree) == (("--no-skills",), ())
    for name in ("v2-skills", "v4-twoaxis-skills"):
        args, read = variant_args(VARIANTS[name], worktree)
        assert args == (
            "--no-skills",
            *("--skill", str(hub[0])),
            *("--skill", str(hub[1])),
            *("--skill", str(own)),
        )
        assert read == (hub[0], hub[1], own)


def test_skill_paths_include_symlink_targets(tmp_path):
    target = tmp_path / "elsewhere" / "refs"
    target.mkdir(parents=True)
    linked = skill(tmp_path / "skills", "linked")
    (linked / "refs").symlink_to(target)
    assert skill_read_paths([linked]) == (linked.resolve(), target.resolve())


def test_missing_hub_skill_fails_the_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_review, "HUB_SKILLS", (tmp_path / "gone",))
    with pytest.raises(ReplayError, match="skill not found"):
        variant_args(VARIANTS["v2-skills"], tmp_path)


def test_build_argv_per_variant(hub, tmp_path):
    adapter = PiJsonAdapter(pi_bin="pi")
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    argvs = {}
    for name, variant in VARIANTS.items():
        extra, _ = variant_args(variant, worktree)
        plan = eval_review.SessionPlan("review", "directives", "prompt", extra)
        req = eval_review._request(
            plan,
            model=ModelSpec("prov/rev", "high"),
            worktree=worktree,
            run_dir=run_dir,
            session_id="s-review",
            timeout_s=60,
        )
        argvs[name] = adapter.build_argv(req)
    baseline = argvs["v0-baseline"]
    assert baseline[baseline.index("--tools") + 1] == "read,grep,find,ls"
    assert "--no-context-files" in baseline and "--no-skills" not in baseline
    assert "--skill" not in baseline
    assert "--append-system-prompt" in baseline
    skills = argvs["v2-skills"]
    assert skills.index("--no-skills") < skills.index("--skill") < skills.index("--")
    assert skills[skills.index("--") + 1] == "prompt"


def test_replay_isolation_opens_skill_paths_but_not_denials(tmp_path, tmp_project):
    other = tmp_path / "projects" / "hub" / "skills" / "tdd"
    other.mkdir(parents=True)
    denied = tmp_path / "private" / "skill"
    denied.mkdir(parents=True)
    config = {
        "isolation": {
            "projects_root": [str(tmp_path / "projects")],
            "deny_read": [str(tmp_path / "private")],
            "tmpdir": str(tmp_path / "srt-tmp"),
        }
    }
    run_dir = tmp_path / "run"
    plain = ReplayIsolation(config).settings(tmp_project, run_dir)
    isolation = ReplayIsolation(config, (other.resolve(), denied.resolve()))
    settings = isolation.settings(tmp_project, run_dir)
    assert isolation.readonly is True
    allow = settings["filesystem"]["allowRead"]
    assert str(other.resolve()) in allow and str(denied.resolve()) not in allow
    assert str(other.resolve()) not in plain["filesystem"]["allowRead"]
    assert str(tmp_project) not in settings["filesystem"]["allowWrite"]


# ── two-axis merge ───────────────────────────────────────────────────


def session(axis: str, verdict: str, findings: list[dict[str, Any]], parsed: bool = True):
    return {"axis": axis, "verdict": verdict, "findings": findings, "parse_ok": parsed}


def test_merge_tags_axes_keeps_order_and_takes_the_worse_verdict():
    spec = session("spec", "pass", [{**FINDING, "axis": "bogus"}])
    standards = session("standards", "concerns", [{**FINDING, "severity": "severe"}])
    verdict, findings, parsed = merge_sessions([spec, standards])
    assert verdict == "concerns" and parsed is True
    assert [(f["axis"], f["severity"]) for f in findings] == [
        ("spec", "minor"),
        ("standards", "severe"),
    ]
    assert merge_sessions([session("spec", "uncertain", [], False), standards])[0] == "uncertain"
    assert merge_sessions([session("spec", "uncertain", [], False), standards])[2] is False


def test_two_axis_sessions(fixture, tmp_path):
    replay = reconstruct(make_case(), fixture.ledger, fixture.data_dir / "runs")
    worktree = tmp_path / "replay-wt"
    plans = eval_review.variant_sessions(
        VARIANTS["v3-twoaxis"],
        replay,
        prompt="P",
        worktree=worktree,
        run_dir=tmp_path / "run",
        extra_args=("--no-skills",),
    )
    assert [p.axis for p in plans] == ["spec", "standards"]
    spec, standards = plans
    assert LIVE_WORKTREE not in spec.directives and str(worktree) in spec.directives
    assert "independent review" in spec.directives and "## Spec axis" in spec.directives
    assert "standards review" in standards.directives and str(worktree) in standards.directives
    assert "Permission is hereby granted" in standards.directives
    assert "Refused Bequest" in standards.directives
    assert "## Project notes (Helmraiser)\n\nUse tabs." in standards.directives
    assert spec.prompt == standards.prompt == "P"


# ── run_eval with the scripted pi ────────────────────────────────────


@pytest.fixture
def context(fixture, fake_pi, tmp_path, hub):
    calls: list[tuple[Path, tuple[Path, ...]]] = []

    def review_adapter(project: Path, extra: tuple[Path, ...]) -> PiJsonAdapter:
        calls.append((project, extra))
        return PiJsonAdapter(pi_bin=str(fake_pi.bin))

    models = {"review": ModelSpec("prov/rev", "high"), "judge": ModelSpec("prov/judge", "high")}
    ctx = EvalContext(
        out=tmp_path / "out",
        ledger=fixture.ledger,
        runs_dir=fixture.data_dir / "runs",
        models=lambda _project: models,
        review_adapter=review_adapter,
        judge_adapter=PiJsonAdapter(pi_bin=str(fake_pi.bin)),
        timeout_s=60,
    )
    ctx.adapter_calls = calls  # type: ignore[attr-defined]
    return ctx


def script(fake_pi, cost: float = 0.0) -> None:
    judge = {"found": True, "matched_finding": 1, "severity_given": "minor", "axis": "x"}
    fake_pi.write_script(
        {
            "sessions": {
                "*-review": {"text": verdict_text("pass", []), "cost": cost},
                "*-review-spec": {"text": verdict_text("pass", [FINDING]), "cost": cost},
                "*-review-standards": {
                    "text": verdict_text("concerns", [{**FINDING, "severity": "severe"}]),
                    "cost": cost,
                },
                "*-judge": {"text": json.dumps({**judge, "note": "same defect"}), "cost": 0.5},
            }
        }
    )


def sessions_of(fake_pi) -> list[str]:
    return [call["session_id"].split("-", 5)[-1] for call in fake_pi.calls()]


def test_run_eval_two_axis_runs_sequentially_and_judges(context, fixture, fake_pi):
    script(fake_pi, cost=1.0)
    result = run_eval(context, [make_case()], [VARIANTS["v3-twoaxis"]])
    assert result["stopped"] is None
    assert sessions_of(fake_pi) == ["review-spec", "review-standards", "judge"]
    spec_call, standards_call = fake_pi.calls()[:2]
    assert "--no-skills" in spec_call["argv"] and "--skill" not in spec_call["argv"]
    assert spec_call["cwd"] != str(fixture.project)
    (row,) = read_results(context.out / "results.jsonl")
    assert row == result["pairs"][0]
    assert row["status"] == "ok" and row["verdict"] == "concerns" and row["parse_ok"] is True
    assert [(f["axis"], f["severity"]) for f in row["findings"]] == [
        ("spec", "minor"),
        ("standards", "severe"),
    ]
    assert row["judge"] == {
        "found": True,
        "matched_finding": 1,
        "severity_given": "severe",
        "axis": "standards",
        "note": "same defect",
    }
    assert row["cost_usd"] == 2.0 and row["judge_cost_usd"] == 0.5
    assert row["reviewed_commit"] == fixture.commits[1] and row["model"] == "prov/rev:high"
    assert [s["axis"] for s in row["sessions"]] == ["spec", "standards"]
    artifacts = Path(row["artifacts"])
    assert "concerns" in (artifacts / "review-standards.md").read_text()
    assert (artifacts / "directives-review-standards.md").is_file()
    assert not (artifacts / "evidence").exists()
    worktrees = git(fixture.project, "worktree", "list")
    assert "worktree-eval-" not in worktrees


def test_run_eval_skill_variant_passes_skills_and_read_paths(context, fake_pi, hub):
    script(fake_pi)
    run_eval(context, [make_case()], [VARIANTS["v2-skills"]])
    (call,) = [c for c in fake_pi.calls() if c["session_id"].endswith("-review")]
    assert call["argv"].count("--skill") == 2 and str(hub[0]) in call["argv"]
    assert context.adapter_calls[-1][1] == (hub[0].resolve(), hub[1].resolve())


def test_run_eval_resumes_without_redoing_finished_pairs(context, fake_pi):
    script(fake_pi)
    cases = [make_case(), make_case(id="clean-1", bucket="clean", axis=None, label=None)]
    variants = [VARIANTS["v0-baseline"], VARIANTS["v1-noskills"]]
    run_eval(context, cases[:1], variants[:1])
    before = len(fake_pi.calls())
    result = run_eval(context, cases, variants)
    assert {(s["case"], s["variant"]) for s in result["skipped_done"]} == {
        ("miss-1", "v0-baseline")
    }
    assert [(p["case"], p["variant"]) for p in result["pairs"]] == [
        ("miss-1", "v1-noskills"),
        ("clean-1", "v0-baseline"),
        ("clean-1", "v1-noskills"),
    ]
    # Three reviews; no findings means no judge session.
    assert len(fake_pi.calls()) - before == 3
    assert len(read_results(context.out / "results.jsonl")) == 4
    clean = result["pairs"][1]
    assert clean["clean"] == {"findings": 0, "severe": 0} and "judge" not in clean


def test_run_eval_retries_error_pairs_on_resume(context, fixture, fake_pi):
    script(fake_pi)
    missing = make_case(review=9)
    first = run_eval(context, [missing], [VARIANTS["v0-baseline"]])
    assert first["pairs"][0]["status"] == "error" and "has 2 review" in first["pairs"][0]["error"]
    second = run_eval(context, [missing], [VARIANTS["v0-baseline"]])
    assert len(second["pairs"]) == 1 and fake_pi.calls() == []


def test_run_eval_stops_before_a_pair_that_would_pass_max_cost(context, fake_pi):
    script(fake_pi, cost=1.0)
    cases = [make_case(), make_case(id="miss-2", review=2)]
    result = run_eval(context, cases, [VARIANTS["v0-baseline"]], max_cost=1.5)
    assert result["stopped"] == "max_cost"
    assert [p["case"] for p in result["pairs"]] == ["miss-1"]
    assert result["spent_usd"] == 1.0  # no findings: no judge
    resumed = run_eval(context, cases, [VARIANTS["v0-baseline"]], max_cost=1.5)
    assert [p["case"] for p in resumed["pairs"]] == ["miss-2"]


def test_quota_error_stops_the_evaluation(context, fake_pi):
    fake_pi.write_script(
        {
            "sessions": {
                "*-review": {
                    "text": "",
                    "stop_reason": "error",
                    "error_message": "429 usage limit reached",
                }
            }
        }
    )
    result = run_eval(context, [make_case(), make_case(id="miss-2")], [VARIANTS["v0-baseline"]])
    (row,) = result["pairs"]
    assert row["status"] == "error" and row["error_kind"] == "quota"
    assert result["stopped"] == "quota" and len(fake_pi.calls()) == 1


def test_dry_run_builds_everything_without_model_calls(context, fixture, fake_pi):
    script(fake_pi)
    variants = [VARIANTS["v0-baseline"], VARIANTS["v4-twoaxis-skills"]]
    result = run_eval(context, [make_case()], variants, dry_run=True)
    assert fake_pi.calls() == []
    assert not (context.out / "results.jsonl").exists()
    baseline, two_axis = result["pairs"]
    assert baseline["reviewed_commit"] == fixture.commits[1]
    assert [s["axis"] for s in baseline["sessions"]] == ["review"]
    assert [s["axis"] for s in two_axis["sessions"]] == ["spec", "standards"]
    argv = two_axis["sessions"][1]["argv"]
    assert "--no-skills" in argv and argv.count("--skill") == 2
    assert two_axis["judge_model"] == "prov/judge:high"
    assert baseline["evidence_files_copied"] == ["evidence/out.txt"]
    assert baseline["evidence_files_withheld"] == []
    assert baseline["unresolved_paths"] == two_axis["unresolved_paths"] == []
    assert not Path(baseline["worktree"]).exists()


# ── report ───────────────────────────────────────────────────────────


def row(case: str, variant: str, bucket: str, **extra: Any) -> dict[str, Any]:
    return {
        "case": case,
        "variant": variant,
        "bucket": bucket,
        "label_axis": extra.pop("label_axis", "spec"),
        "rule_written": extra.pop("rule_written", None),
        "status": extra.pop("status", "ok"),
        "cost_usd": extra.pop("cost_usd", 1.0),
        "judge_cost_usd": extra.pop("judge_cost_usd", 0.0),
        "seconds": extra.pop("seconds", 10.0),
        **extra,
    }


def judged(found: bool, severity: str | None = None) -> dict[str, Any]:
    return {"found": found, "matched_finding": 0 if found else None, "severity_given": severity}


def test_report_bucket_arithmetic(tmp_path):
    rows = [
        row("m1", "v0-baseline", "miss", judge=judged(False), status="error"),
        row("m1", "v0-baseline", "miss", judge=judged(True, "minor")),  # the retry wins
        row("m2", "v0-baseline", "miss", judge=judged(False)),
        row("m3", "v0-baseline", "miss", judge=None, judge_error="bad answer"),
        row("u1", "v0-baseline", "undercall", judge=judged(True, "severe")),
        row("f1", "v0-baseline", "found", judge=judged(True, "severe")),
        row(
            "s1",
            "v0-baseline",
            "seeded",
            label_axis="standards",
            rule_written=True,
            judge=judged(True, "severe"),
        ),
        row(
            "s2",
            "v0-baseline",
            "miss",
            label_axis="standards",
            rule_written=False,
            judge=judged(False),
        ),
        row("c1", "v0-baseline", "clean", clean={"findings": 3, "severe": 1}, cost_usd=3.0),
        row("c2", "v0-baseline", "clean", clean={"findings": 0, "severe": 0}),
        row("m1", "v3-twoaxis", "miss", status="error", error="quota"),
    ]
    report = build_report(rows)
    base = report["variants"]["v0-baseline"]
    assert report["pairs"] == 10
    assert base["ok"] == 9 and base["errors"] == 0
    assert base["recall"]["miss"] == {"found": 1, "judged": 3, "unjudged": 1, "recall": 0.333}
    assert base["recall"]["undercall"]["recall"] == 1.0
    assert base["recall"]["found"]["found"] == 1 and base["recall"]["seeded"]["found"] == 1
    assert base["severity_called"]["miss"] == {"severe": 0, "minor": 1, "unknown": 0}
    assert base["severity_called"]["undercall"] == {"severe": 1, "minor": 0, "unknown": 0}
    assert base["standards"]["written"] == {"found": 1, "judged": 1, "unjudged": 0, "recall": 1.0}
    assert base["standards"]["unwritten"]["found"] == 0
    assert base["clean"] == {"cases": 2, "findings": 3, "severe": 1, "cases_with_severe": 1}
    assert base["cost_usd"] == {"total": 11.0, "median": 1.0}
    assert base["seconds"]["median"] == 10.0
    assert report["variants"]["v3-twoaxis"]["errors"] == 1
    text = render_report(report)
    assert "| miss | 1/3 (33%), 1 unjudged | - |" in text
    assert "## Clean cases (false-alarm proxy)" in text and "v3-twoaxis" in text
    assert report["evidence_withheld"] == {} and "## Evidence withheld\n" in text


def test_report_counts_withheld_evidence_per_case():
    rows = [
        row("m1", "v0-baseline", "miss", judge=judged(False), evidence_files_withheld=["a", "b"]),
        row("m1", "v1-noskills", "miss", judge=judged(False), evidence_files_withheld=["a", "b"]),
        row(
            "c1",
            "v0-baseline",
            "clean",
            clean={"findings": 0, "severe": 0},
            evidence_files_withheld=[],
        ),
    ]
    report = build_report(rows)
    assert report["evidence_withheld"] == {"m1": 2}
    assert "| m1 | 2 |" in render_report(report)
