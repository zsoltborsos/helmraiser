"""Review replay: re-run the review stage of past runs under reviewer variants and score them.

A case file (JSON, kept outside the repository) names past reviews and what each one
should have found. For each case the replay rebuilds what the live reviewer had: the
stored review prompt (``prompt-review-1[-<n>].md`` in the run dir), the stored review
directives, and a detached worktree of the project at the reviewed commit, verified by
regenerating the diff the stored prompt carries. A ``seeded`` case applies a patch on top
and swaps the regenerated diff into the prompt. Each variant then runs one reviewer
session (or, for the two-axis variants, a Spec and then a Standards session) under the
scout's read-only process isolation, and a blind judge session decides whether the
findings name the labelled defect. Results go to ``<out>/results.jsonl``, one line per
(case, variant), so a stopped evaluation resumes where it left off.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from helmraiser.code_snapshot import without_snapshot
from helmraiser.isolation.process import ProcessIsolation, _abs, _within
from helmraiser.ledger.db import Ledger
from helmraiser.loop.compare import _json_candidates
from helmraiser.loop.preflight import remove_throwaway_worktree
from helmraiser.loop.review import diff_block, parse_verdict
from helmraiser.loop.runner import REVIEW_TOOLS
from helmraiser.pi.adapter import PiJsonAdapter
from helmraiser.pi.protocol import PiAdapter, PiEvent, PiRequest
from helmraiser.task.model import ModelSpec
from helmraiser.task.project import worktree_manager

BUCKETS: tuple[str, ...] = ("miss", "found", "undercall", "seeded", "clean")
POSITIVE_BUCKETS: tuple[str, ...] = ("found", "miss", "undercall", "seeded")
AXES: tuple[str, ...] = ("spec", "standards")
SEVERITIES: tuple[str, ...] = ("severe", "minor")
VERDICT_RANK = {"pass": 0, "concerns": 1, "uncertain": 2}
CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PROMPT_FILE = re.compile(r"^prompt-review-1(?:-(\d+))?\.md$")
NOTES_HEADING = "## Project notes (Helmraiser)"
REVIEW_TIMEOUT_S = 1800
JUDGE_TIMEOUT_S = 1800

SKILLS_HUB = Path("/mnt/data/projects/personal/agent-skills-hub/skills/matt")
HUB_SKILLS: tuple[Path, ...] = tuple(
    SKILLS_HUB / name
    for name in ("codebase-design", "tdd", "writing-for-agents", "domain-modeling")
)
PROJECT_SKILL_ROOTS: tuple[str, ...] = (".pi/skills", ".agents/skills")


class EvalError(ValueError):
    """The case file, a variant or a case selection is invalid; nothing ran."""


class ReplayError(RuntimeError):
    """One case cannot be replayed faithfully (recorded as an error row)."""


@dataclass(frozen=True)
class Variant:
    name: str
    two_axis: bool
    no_skills: bool
    skills: bool


VARIANTS: dict[str, Variant] = {
    v.name: v
    for v in (
        Variant("v0-baseline", two_axis=False, no_skills=False, skills=False),
        Variant("v1-noskills", two_axis=False, no_skills=True, skills=False),
        Variant("v2-skills", two_axis=False, no_skills=True, skills=True),
        Variant("v3-twoaxis", two_axis=True, no_skills=True, skills=False),
        Variant("v4-twoaxis-skills", two_axis=True, no_skills=True, skills=True),
    )
}


# ── directives for the two-axis variants ─────────────────────────────

SPEC_AXIS = """
## Spec axis

A separate reviewer checks the project's coding standards; your axis is the task. Answer
these three questions about the diff, and quote the task or acceptance line each finding
rests on:

1. Which requirements of the task are missing or only partly implemented?
2. What behaviour does the diff add that the task did not ask for?
3. Which requirements look implemented, but are implemented wrongly?
"""

# The smell baseline and its two binding rules are copied from the code-review skill of
# mattpocock/skills (https://github.com/mattpocock/skills), under this licence:
MIT_NOTICE = """\
MIT License

Copyright (c) 2026 Matt Pocock

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""

SMELL_BASELINE = """\
On top of whatever the repo documents, the Standards axis always carries the **smell
baseline** below: a fixed set of Fowler code smells (_Refactoring_, ch.3) that applies
even when a repo documents nothing. Two rules bind it:

- **The repo overrides.** A documented repo standard always wins; where it endorses
  something the baseline would flag, suppress the smell.
- **Always a judgement call.** Each smell is a labelled heuristic ("possible Feature
  Envy"), never a hard violation. Like any standard here, skip anything tooling already
  enforces.

Each smell reads *what it is* → *how to fix*; match it against the diff:

- **Mysterious Name**: a function, variable, or type whose name doesn't reveal what it does or holds. → rename it; if no honest name comes, the design's murky.
- **Duplicated Code**: the same logic shape appears in more than one hunk or file in the change. → extract the shared shape, call it from both.
- **Feature Envy**: a method that reaches into another object's data more than its own. → move the method onto the data it envies.
- **Data Clumps**: the same few fields or params keep travelling together (a type wanting to be born). → bundle them into one type, pass that.
- **Primitive Obsession**: a primitive or string standing in for a domain concept that deserves its own type. → give the concept its own small type.
- **Repeated Switches**: the same `switch`/`if`-cascade on the same type recurs across the change. → replace with polymorphism, or one map both sites share.
- **Shotgun Surgery**: one logical change forces scattered edits across many files in the diff. → gather what changes together into one module.
- **Divergent Change**: one file or module is edited for several unrelated reasons. → split so each module changes for one reason.
- **Speculative Generality**: abstraction, parameters, or hooks added for needs the spec doesn't have. → delete it; inline back until a real need shows.
- **Message Chains**: long `a.b().c().d()` navigation the caller shouldn't depend on. → hide the walk behind one method on the first object.
- **Middle Man**: a class or function that mostly just delegates onward. → cut it, call the real target direct.
- **Refused Bequest**: a subclass or implementer that ignores or overrides most of what it inherits. → drop the inheritance, use composition."""

STANDARDS_TEMPLATE = """\
# Helmraiser run {run_id} — standards review

You are reviewing the implementation of task "{title}" in a git worktree at
{worktree}. This is a read-only review: never modify, create or delete files,
never run git commands that change state. You did not write this code.

A separate reviewer judges whether the change does what the task asked. Your axis is
whether the change follows this project's own documented standards:

1. Find the project's standards: AGENTS.md and CLAUDE.md at the root and in the
   directories the diff touches, CONTRIBUTING* and CODING_STANDARDS* files, the documents
   these point to about how code is written, and the project notes below if present.
2. Check every hunk of the diff against them. For each breach, cite the file and the rule
   in the finding's note (for example `AGENTS.md:42`, quoting the rule).
3. Check the diff against the smell baseline below.

Severity: only a cited breach of a documented standard may be "severe". A baseline smell
is always "minor". Skip anything the project's linters, formatters or type checkers
already enforce. Do not judge whether the task is done and do not assess evidence.

Answer with a fenced JSON block as the LAST thing in your message:
{{"verdict": "pass" | "concerns" | "uncertain",
 "findings": [{{"severity": "severe"|"minor", "file": "...", "line": 0, "note": "..."}}],
 "evidence_checks": []}}
Rules: "pass" only if no severe findings. Leave evidence_checks empty: the evidence
instructions in the prompt are for the other reviewer. Never modify files.

## Smell baseline

The smell baseline is copied from the code-review skill of mattpocock/skills
(https://github.com/mattpocock/skills):

```text
{notice}
```

{baseline}
{project_notes}"""


def render_standards_directives(
    *, run_id: str, title: str, worktree: Path | str, notes_section: str | None
) -> str:
    return STANDARDS_TEMPLATE.format(
        run_id=run_id,
        title=title,
        worktree=worktree,
        notice=MIT_NOTICE,
        baseline=SMELL_BASELINE,
        project_notes=f"\n{notes_section}\n" if notes_section else "",
    )


# ── case file ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Case:
    id: str
    run_id: str
    review: int
    bucket: str
    axis: str | None
    label: dict[str, Any] | None
    seed_patch: Path | None
    # Optional ``meta``: what the case author recorded. ``reviewed_commit`` and
    # ``review_prompt`` are preferred, then checked against the reconstruction.
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def rule_written(self) -> bool | None:
        rule = (self.label or {}).get("rule")
        return rule.get("written") if isinstance(rule, dict) else None


def _optional_str(value: Any, where: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise EvalError(f"{where} must be a string or null")
    return value


def _label(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EvalError(f"{where}: label must be an object")
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise EvalError(f"{where}: label.summary must be a non-empty string")
    _optional_str(raw.get("file"), f"{where}: label.file")
    if raw.get("severity_expected") not in (*SEVERITIES, None):
        raise EvalError(f"{where}: label.severity_expected must be severe, minor or null")
    rule = raw.get("rule")
    if rule is not None:
        if not isinstance(rule, dict):
            raise EvalError(f"{where}: label.rule must be an object or null")
        _optional_str(rule.get("source"), f"{where}: label.rule.source")
        _optional_str(rule.get("text"), f"{where}: label.rule.text")
        if not isinstance(rule.get("written"), bool):
            raise EvalError(f"{where}: label.rule.written must be true or false")
    return raw


def _case(raw: Any, index: int) -> Case:
    where = f"cases[{index}]"
    if not isinstance(raw, dict):
        raise EvalError(f"{where} must be an object")
    case_id = raw.get("id")
    if not isinstance(case_id, str) or not CASE_ID.match(case_id):
        raise EvalError(f"{where}: id must match {CASE_ID.pattern}")
    where = f"case {case_id}"
    run_id = raw.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise EvalError(f"{where}: run_id must be a non-empty string")
    review = raw.get("review")
    if type(review) is not int or review < 1:
        raise EvalError(f"{where}: review must be an integer from 1")
    bucket = raw.get("bucket")
    if bucket not in BUCKETS:
        raise EvalError(f"{where}: unknown bucket {bucket!r} (expected {', '.join(BUCKETS)})")
    axis = raw.get("axis")
    if axis not in (*AXES, None):
        raise EvalError(f"{where}: axis must be spec, standards or null")
    label = raw.get("label")
    if bucket == "clean":
        if label is not None:
            raise EvalError(f"{where}: a clean case has no label")
    else:
        label = _label(label, where)
    seed = raw.get("seed")
    seed_patch: Path | None = None
    if bucket == "seeded":
        patch = seed.get("patch") if isinstance(seed, dict) else None
        if not isinstance(patch, str) or not Path(patch).is_absolute():
            raise EvalError(f"{where}: a seeded case needs seed.patch, an absolute path")
        seed_patch = Path(patch)
        if not seed_patch.is_file():
            raise EvalError(f"{where}: seed patch not found: {patch}")
    elif seed is not None:
        raise EvalError(f"{where}: only a seeded case has a seed")
    meta = raw.get("meta")
    meta = {} if meta is None else meta
    if not isinstance(meta, dict):
        raise EvalError(f"{where}: meta must be an object")
    for key in ("reviewed_commit", "review_prompt", "base_commit"):
        _optional_str(meta.get(key), f"{where}: meta.{key}")
    return Case(case_id, run_id, review, bucket, axis, label, seed_patch, meta)


def load_cases(path: Path) -> tuple[str, list[Case]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvalError(f"cannot read case file {path}: {exc}") from exc
    except ValueError as exc:
        raise EvalError(f"case file is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EvalError("case file must be a JSON object")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise EvalError("case file: name must be a non-empty string")
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise EvalError("case file: cases must be a non-empty list")
    cases = [_case(raw, index) for index, raw in enumerate(raw_cases)]
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise EvalError(f"duplicate case id {case.id}")
        seen.add(case.id)
    return name, cases


def select(
    cases: list[Case], ids: list[str], variants: list[str]
) -> tuple[list[Case], list[Variant]]:
    unknown = [name for name in variants if name not in VARIANTS]
    if unknown or not variants:
        raise EvalError(
            f"unknown variant {', '.join(unknown) or '(none given)'}; "
            f"known: {', '.join(VARIANTS)}"
        )
    known = {case.id for case in cases}
    missing = [case_id for case_id in ids if case_id not in known]
    if missing:
        raise EvalError(f"unknown case {', '.join(missing)}")
    chosen = [case for case in cases if not ids or case.id in ids]
    return chosen, [VARIANTS[name] for name in dict.fromkeys(variants)]


# ── reconstruction ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Replay:
    """What the live reviewer had for one case, before the worktree exists."""

    case: Case
    project: Path
    run_dir: Path
    live_worktree: str | None
    base: str
    commit: str
    prompt: str
    directives: str
    prompt_file: Path
    diff_text: str
    diff_check: str  # "exact" (inline diff matched) | "size" (out-of-line diff, size only)

    @property
    def title(self) -> str:
        first = self.prompt.split("\n", 1)[0]
        return first.removeprefix("# Review: ").strip()

    @property
    def notes_section(self) -> str | None:
        at = self.directives.find(NOTES_HEADING)
        return self.directives[at:].strip() if at >= 0 else None


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=without_snapshot(os.environ),
    )


def _diff_section(diff_text: str, diff_path: Path) -> str:
    return f"## Diff\n\n{diff_block(diff_text, diff_path)}\n\n## Verify results"


def review_prompt_file(
    ledger: Ledger, run_id: str, run_dir: Path, review: int
) -> tuple[Path, float]:
    """The stored prompt of review ``review`` and when that review stage started.

    Review sessions all spill with attempt 1, so review prompts are numbered in launch
    order (``prompt-review-1.md``, ``-2``, ...); a provider retry inside a review spills
    once more. Review stage starts and review retries in the ledger give the order.
    """
    starts: list[tuple[float, int]] = []
    launches = 0
    for event in ledger.events(run_id, types=("stage_start", "retry")):
        if event["type"] == "stage_start" and event["payload"].get("stage") == "review":
            starts.append((float(event["ts"]), launches))
            launches += 1
        elif event["type"] == "retry" and event["stage"] == "review" and starts:
            launches += 1
    if review > len(starts):
        raise ReplayError(
            f"run {run_id} has {len(starts)} review(s); case asks for review {review}"
        )
    numbered = []
    for path in run_dir.glob("prompt-review-1*.md"):
        match = PROMPT_FILE.match(path.name)
        if match:
            numbered.append((int(match.group(1) or 1), path))
    files = [path for _, path in sorted(numbered)]
    if len(files) != launches:
        raise ReplayError(
            f"{len(files)} stored review prompt(s) for {launches} review session(s) in "
            f"{run_dir}; cannot tell which is review {review}"
        )
    ts, index = starts[review - 1]
    return files[index], ts


def _candidates(
    ledger: Ledger, run: dict[str, Any], started: float, meta: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Commits (most likely first) and bases the reviewed diff may have been taken between;
    the case's recorded ``meta`` commits come first."""
    iterations = sorted(
        (i for i in ledger.iterations(run["run_id"]) if i["commit_sha"]),
        key=lambda i: i["ended_at"] or i["started_at"],
    )
    before = [i["commit_sha"] for i in iterations if (i["ended_at"] or 0) <= started]
    commits = [
        meta.get("reviewed_commit"),
        *reversed(before),
        run.get("head_commit"),
        *(i["commit_sha"] for i in iterations),
    ]
    bases = [meta.get("base_commit"), run.get("base_commit")]
    for event in ledger.events(run["run_id"], types=("worktree_created", "rebased")):
        payload = event["payload"]
        bases += [payload.get("base"), payload.get("previous"), payload.get("base_commit")]
    return (
        [c for c in dict.fromkeys(commits) if isinstance(c, str) and c],
        [b for b in dict.fromkeys(bases) if isinstance(b, str) and b],
    )


def reconstruct(case: Case, ledger: Ledger, runs_dir: Path) -> Replay:
    run = ledger.get_run(case.run_id)
    if run is None:
        raise ReplayError(f"unknown run {case.run_id}")
    project = Path(run["project"])
    if not project.is_dir():
        raise ReplayError(f"project of run {case.run_id} no longer exists: {project}")
    run_dir = runs_dir / case.run_id
    prompt_file, started = review_prompt_file(ledger, case.run_id, run_dir, case.review)
    recorded = case.meta.get("review_prompt")
    if recorded and Path(recorded).expanduser().resolve() != prompt_file.resolve():
        raise ReplayError(
            f"meta.review_prompt {recorded} disagrees with the ledger: review {case.review} "
            f"of run {case.run_id} is {prompt_file}"
        )
    prompt = prompt_file.read_text(encoding="utf-8")
    if not prompt.startswith("# Review: "):
        raise ReplayError(f"{prompt_file} is not a review prompt")
    directives_file = run_dir / "directives-review.md"
    if not directives_file.is_file():
        raise ReplayError(f"no stored review directives: {directives_file}")
    commits, bases = _candidates(ledger, run, started, case.meta)
    present = [
        c for c in commits if _git(project, "cat-file", "-e", f"{c}^{{commit}}").returncode == 0
    ]
    if not present:
        raise ReplayError(
            f"none of the {len(commits)} recorded commit(s) of run {case.run_id} is still in "
            f"{project}"
        )
    tried = 0
    for commit in present:
        for base in bases:
            diff = _git(project, "diff", f"{base}..{commit}")
            if diff.returncode != 0:
                continue
            tried += 1
            if _diff_section(diff.stdout, run_dir / "diff.patch") in prompt:
                recorded = case.meta.get("reviewed_commit")
                if recorded and recorded != commit:
                    raise ReplayError(
                        f"meta.reviewed_commit {recorded} disagrees with the ledger: commit "
                        f"{commit} reproduces the diff of review {case.review}"
                    )
                inline = diff_block(diff.stdout, run_dir / "diff.patch").startswith("```diff")
                return Replay(
                    case=case,
                    project=project,
                    run_dir=run_dir,
                    live_worktree=run.get("worktree_path"),
                    base=base,
                    commit=commit,
                    prompt=prompt,
                    directives=directives_file.read_text(encoding="utf-8"),
                    prompt_file=prompt_file,
                    diff_text=diff.stdout,
                    diff_check="exact" if inline else "size",
                )
    raise ReplayError(
        f"no recorded commit of run {case.run_id} reproduces the diff in {prompt_file.name} "
        f"({tried} commit/base pair(s) tried in {project})"
    )


@contextmanager
def replay_worktree(replay: Replay, eval_id: str) -> Iterator[tuple[Path, str]]:
    """A detached worktree at the reviewed commit (seed applied) and the diff to review."""
    path = worktree_manager(replay.project).path_for("eval", eval_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    added = _git(replay.project, "worktree", "add", "--detach", str(path), replay.commit)
    if added.returncode != 0:
        raise ReplayError(f"worktree: {(added.stderr or added.stdout).strip()}")
    try:
        diff = replay.diff_text
        seed = replay.case.seed_patch
        if seed is not None:
            applied = _git(path, "apply", "--index", str(seed))
            if applied.returncode != 0:
                raise ReplayError(f"seed patch does not apply: {applied.stderr.strip()}")
            diff = _git(path, "diff", "--cached", replay.base).stdout
        yield path, diff
    finally:
        remove_throwaway_worktree(replay.project, path)


def replay_prompt(replay: Replay, diff_text: str, worktree: Path, run_dir: Path) -> str:
    """The stored prompt, with a seeded diff swapped in and paths moved to the replay's."""
    prompt = replay.prompt
    if diff_text != replay.diff_text:
        live_diff = replay.run_dir / "diff.patch"
        prompt = prompt.replace(
            _diff_section(replay.diff_text, live_diff), _diff_section(diff_text, live_diff), 1
        )
    return relocate(prompt, replay, worktree, run_dir)


def relocate(text: str, replay: Replay, worktree: Path, run_dir: Path) -> str:
    """Point the live run dir and worktree paths at the replay's copies."""
    text = text.replace(str(replay.run_dir), str(run_dir))
    if replay.live_worktree:
        text = text.replace(replay.live_worktree, str(worktree))
    return text


def prepare_run_dir(replay: Replay, run_dir: Path, diff_text: str) -> None:
    """The replay's run dir: copies of the run's evidence and logs, and the reviewed diff."""
    run_dir.mkdir(parents=True, exist_ok=True)
    for name in ("evidence", "logs"):
        source = replay.run_dir / name
        if source.is_dir():
            shutil.copytree(source, run_dir / name, symlinks=True)
    (run_dir / "evidence").mkdir(exist_ok=True)
    (run_dir / "diff.patch").write_text(diff_text, encoding="utf-8")


# ── variants ─────────────────────────────────────────────────────────


def project_skill_dirs(start: Path) -> list[Path]:
    """Skill directories (holding ``SKILL.md``) in ``.pi/skills`` and ``.agents/skills``
    from ``start`` up to its repository root, nearest first."""
    found: list[Path] = []
    current = start
    while True:
        for rel in PROJECT_SKILL_ROOTS:
            root = current / rel
            if not root.is_dir():
                continue
            seen: set[str] = set()
            for folder, dirs, files in os.walk(root, followlinks=True):
                real = os.path.realpath(folder)
                if real in seen:
                    dirs[:] = []
                    continue
                seen.add(real)
                if "SKILL.md" in files:
                    found.append(Path(folder))
                    dirs[:] = []
                dirs.sort()
        if (current / ".git").exists() or current.parent == current:
            return found
        current = current.parent


def skill_dirs(worktree: Path) -> list[Path]:
    missing = [path for path in HUB_SKILLS if not (path / "SKILL.md").is_file()]
    if missing:
        raise ReplayError(f"skill not found: {missing[0]}")
    return [*HUB_SKILLS, *project_skill_dirs(worktree)]


def skill_read_paths(dirs: list[Path]) -> tuple[Path, ...]:
    """What reading ``dirs`` needs: each directory and every symlink target inside it."""
    paths: list[Path] = []
    for directory in dirs:
        paths.append(directory.resolve())
        for folder, subdirs, files in os.walk(directory):
            for name in [*subdirs, *files]:
                entry = Path(folder) / name
                if entry.is_symlink() and entry.exists():
                    paths.append(entry.resolve())
    return tuple(dict.fromkeys(paths))


def variant_args(variant: Variant, worktree: Path) -> tuple[tuple[str, ...], tuple[Path, ...]]:
    """The variant's extra pi flags and the paths they need readable in the sandbox."""
    if not variant.no_skills:
        return (), ()
    if not variant.skills:
        return ("--no-skills",), ()
    dirs = skill_dirs(worktree)
    args: list[str] = ["--no-skills"]
    for directory in dirs:
        args += ["--skill", str(directory)]
    return tuple(args), skill_read_paths(dirs)


@dataclass(frozen=True)
class SessionPlan:
    axis: str  # "review" for a single reviewer, else "spec" / "standards"
    directives: str
    prompt: str
    extra_args: tuple[str, ...]


def variant_sessions(
    variant: Variant,
    replay: Replay,
    *,
    prompt: str,
    worktree: Path,
    run_dir: Path,
    extra_args: tuple[str, ...],
) -> list[SessionPlan]:
    """The reviewer sessions of ``variant``, in the order they run."""
    directives = relocate(replay.directives, replay, worktree, run_dir)
    if not variant.two_axis:
        return [SessionPlan("review", directives, prompt, extra_args)]
    standards = render_standards_directives(
        run_id=replay.case.run_id,
        title=replay.title,
        worktree=worktree,
        notes_section=replay.notes_section,
    )
    return [
        SessionPlan("spec", directives.rstrip("\n") + "\n" + SPEC_AXIS, prompt, extra_args),
        SessionPlan("standards", standards, prompt, extra_args),
    ]


def merge_sessions(sessions: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], bool]:
    """Verdict (the worst), findings tagged with their session's axis in session order, and
    whether every verdict parsed. Findings are never reranked across axes."""
    verdict = max((s["verdict"] for s in sessions), key=VERDICT_RANK.__getitem__)
    findings = [{**finding, "axis": s["axis"]} for s in sessions for finding in s["findings"]]
    return verdict, findings, all(s["parse_ok"] for s in sessions)


# ── isolation ────────────────────────────────────────────────────────


class ReplayIsolation(ProcessIsolation):
    """The scout's read-only session sandbox plus ``allowRead`` carve-outs for skill paths
    that another-project denial hides. Credentials and configured denials stay closed."""

    def __init__(
        self,
        config: dict[str, Any],
        extra_read: tuple[Path, ...] = (),
        *,
        data_dir: Path | None = None,
    ) -> None:
        super().__init__(config, readonly=True, data_dir=data_dir)
        self.extra_read = extra_read

    def settings(
        self,
        worktree: Path,
        run_dir: Path,
        *,
        command: bool = False,
        service_ports: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        settings = super().settings(worktree, run_dir, command=command, service_ports=service_ports)
        if command or not self.extra_read:
            return settings
        _, fixed, _ = self._read_denials(Path(_abs(worktree)), Path(_abs(run_dir)))
        allow = settings["filesystem"]["allowRead"]
        for path in self.extra_read:
            if str(path) not in allow and not any(_within(path, f.resolve()) for f in fixed):
                allow.append(str(path))
        return settings


# ── sessions ─────────────────────────────────────────────────────────

ReviewAdapterFactory = Callable[[Path, tuple[Path, ...]], PiJsonAdapter]


async def _noop_event(_event: PiEvent) -> None:
    return None


def _request(
    plan: SessionPlan,
    *,
    model: ModelSpec,
    worktree: Path,
    run_dir: Path,
    session_id: str,
    timeout_s: int,
) -> PiRequest:
    directives = run_dir / f"directives-review-{plan.axis}.md"
    directives.write_text(plan.directives, encoding="utf-8")
    return PiRequest(
        prompt=plan.prompt,
        session_id=session_id,
        model=model,
        cwd=worktree,
        session_dir=run_dir / "sessions",
        system_prompt_path=directives,
        tools=REVIEW_TOOLS,
        context_files=False,
        timeout_s=timeout_s,
        env={"HELMRAISER_WORKTREE": str(worktree)},
        stage="review",
        extra_args=plan.extra_args,
    )


def run_review_session(adapter: PiAdapter, req: PiRequest, run_id: str) -> dict[str, Any]:
    """One reviewer session, its verdict parsed as the live review parses it."""
    started = time.monotonic()
    res = asyncio.run(adapter.run(req, _noop_event, run_id))
    seconds = round(time.monotonic() - started, 3)
    verdict = parse_verdict(res.final_text)
    if res.error_kind is not None:
        verdict.verdict = "uncertain"
    return {
        "verdict": verdict.verdict,
        "findings": list(verdict.findings),
        "parse_ok": verdict.parsed,
        "parse_error": verdict.error,
        "error_kind": res.error_kind,
        "error_message": res.error_message,
        "cost_usd": res.usage.cost_usd,
        "tokens_in": res.usage.tokens_in,
        "tokens_out": res.usage.tokens_out,
        "seconds": seconds,
        "text": res.final_text,
    }


JUDGE_PROMPT = """\
You are scoring a code review against one known defect in the reviewed change.

## Known defect

{defect}

## Review findings

{findings}

Decide whether any finding identifies this defect: the same problem, not merely the same
file or area. Reply with the JSON object only:
{{"found": true|false, "matched_finding": <index>|null, "severity_given": "severe"|"minor"|null,
 "axis": "<axis of the matched finding>"|null, "note": "<one sentence>"}}"""


def build_judge_prompt(label: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    rule = label.get("rule") or {}
    defect = [
        f"Summary: {label['summary']}",
        f"File: {label.get('file') or '(not given)'}",
        f"Expected severity: {label.get('severity_expected') or '(not given)'}",
    ]
    if rule.get("source") or rule.get("text"):
        defect.append(f"Rule: {rule.get('source') or ''} {rule.get('text') or ''}".rstrip())
    rows = [
        f"[{i}] ({f.get('axis')}) {f.get('severity')} {f.get('file')}:{f.get('line')}: "
        f"{f.get('note')}"
        for i, f in enumerate(findings)
    ]
    return JUDGE_PROMPT.format(defect="\n".join(defect), findings="\n".join(rows))


def parse_judge(text: str, findings: list[dict[str, Any]]) -> dict[str, Any]:
    """The judge's answer. Severity and axis come from the matched finding itself."""
    for candidate in _json_candidates(text):
        if not isinstance(candidate, dict) or "found" not in candidate:
            continue
        found = candidate["found"]
        if not isinstance(found, bool):
            raise ValueError("judge answer: found must be true or false")
        matched = candidate.get("matched_finding")
        if not found:
            matched = None
        elif type(matched) is not int or not 0 <= matched < len(findings):
            raise ValueError("judge answer: matched_finding must index a finding")
        finding = findings[matched] if matched is not None else {}
        severity = finding.get("severity")
        return {
            "found": found,
            "matched_finding": matched,
            "severity_given": severity if severity in SEVERITIES else None,
            "axis": finding.get("axis"),
            "note": str(candidate.get("note") or ""),
        }
    raise ValueError("judge answer contains no JSON verdict")


def run_judge(
    adapter: PiAdapter, model: ModelSpec, label: dict[str, Any], findings: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None, float]:
    """(judgement, error, cost). No findings means not found, without a session."""
    if not findings:
        return (
            {
                "found": False,
                "matched_finding": None,
                "severity_given": None,
                "axis": None,
                "note": "no findings",
            },
            None,
            0.0,
        )
    run_id = str(uuid.uuid4())
    with tempfile.TemporaryDirectory(prefix="helmraiser-eval-judge-") as tmp:
        req = PiRequest(
            prompt=build_judge_prompt(label, findings),
            session_id=f"{run_id}-judge",
            model=model,
            cwd=Path(tmp),
            session_dir=Path(tmp) / "sessions",
            tools=(),
            context_files=False,
            timeout_s=JUDGE_TIMEOUT_S,
            stage="judge",
        )
        res = asyncio.run(adapter.run(req, _noop_event, run_id))
    cost = res.usage.cost_usd
    if not res.ok:
        return None, f"judge session failed ({res.error_kind}): {res.error_message or ''}", cost
    try:
        return parse_judge(res.final_text, findings), None, cost
    except ValueError as exc:
        return None, str(exc), cost


# ── the evaluation ───────────────────────────────────────────────────

STOP_ERRORS = frozenset({"quota", "contract"})


def read_results(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _pair_cost(row: dict[str, Any]) -> float:
    return float(row.get("cost_usd") or 0) + float(row.get("judge_cost_usd") or 0)


def _estimate(rows: list[dict[str, Any]], variant: str) -> float:
    costs = [_pair_cost(r) for r in rows if r.get("status") == "ok" and r["variant"] == variant]
    costs = costs or [_pair_cost(r) for r in rows if r.get("status") == "ok"]
    return statistics.fmean(costs) if costs else 0.0


@dataclass
class EvalContext:
    out: Path
    ledger: Ledger
    runs_dir: Path
    models: Callable[[Path], dict[str, ModelSpec]]
    review_adapter: ReviewAdapterFactory
    judge_adapter: PiAdapter
    timeout_s: int = REVIEW_TIMEOUT_S
    temp_root: Path | None = None


def _base_row(case: Case, variant: Variant) -> dict[str, Any]:
    return {
        "case": case.id,
        "variant": variant.name,
        "run_id": case.run_id,
        "review": case.review,
        "bucket": case.bucket,
        "label_axis": case.axis,
        "rule_written": case.rule_written,
        "severity_expected": (case.label or {}).get("severity_expected"),
    }


def _run_pair(
    ctx: EvalContext,
    replay: Replay,
    variant: Variant,
    worktree: Path,
    diff_text: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    case = replay.case
    row = _base_row(case, variant)
    row.update(
        reviewed_commit=replay.commit,
        base_commit=replay.base,
        diff_check=replay.diff_check,
        seeded=case.seed_patch is not None,
    )
    pair_id = str(uuid.uuid4())
    models = ctx.models(replay.project)
    row["model"] = models["review"].render()
    with tempfile.TemporaryDirectory(prefix="helmraiser-eval-", dir=ctx.temp_root) as tmp:
        run_dir = Path(tmp) / "run"
        prepare_run_dir(replay, run_dir, diff_text)
        extra_args, read_paths = variant_args(variant, worktree)
        prompt = replay_prompt(replay, diff_text, worktree, run_dir)
        plans = variant_sessions(
            variant,
            replay,
            prompt=prompt,
            worktree=worktree,
            run_dir=run_dir,
            extra_args=extra_args,
        )
        adapter = ctx.review_adapter(replay.project, read_paths)
        requests = [
            _request(
                plan,
                model=models["review"],
                worktree=worktree,
                run_dir=run_dir,
                session_id=f"{pair_id}-review" + ("" if plan.axis == "review" else f"-{plan.axis}"),
                timeout_s=ctx.timeout_s,
            )
            for plan in plans
        ]
        if dry_run:
            row["worktree"] = str(worktree)
            row["sessions"] = [
                {
                    "axis": plan.axis,
                    "argv": list(adapter.build_argv(req)),
                    "prompt_bytes": len(plan.prompt.encode("utf-8")),
                    "directives_bytes": len(plan.directives.encode("utf-8")),
                }
                for plan, req in zip(plans, requests, strict=True)
            ]
            row["judge_model"] = models["judge"].render()
            return row
        sessions = []
        for plan, req in zip(plans, requests, strict=True):
            session = run_review_session(adapter, req, pair_id)
            session["axis"] = plan.axis
            (run_dir / f"review-{plan.axis}.md").write_text(session.pop("text"), encoding="utf-8")
            sessions.append(session)
            if session["error_kind"] in STOP_ERRORS:
                break
        dest = ctx.out / "pairs" / case.id / variant.name / pair_id
        shutil.copytree(run_dir, dest, ignore=shutil.ignore_patterns("evidence", "logs"))
    row["artifacts"] = str(dest)
    row["sessions"] = sessions
    row["cost_usd"] = sum(s["cost_usd"] for s in sessions)
    row["tokens_in"] = sum(s["tokens_in"] for s in sessions)
    row["tokens_out"] = sum(s["tokens_out"] for s in sessions)
    row["seconds"] = round(sum(s["seconds"] for s in sessions), 3)
    failed = [s for s in sessions if s["error_kind"] is not None]
    if failed:
        first = failed[0]
        row.update(
            status="error",
            error=f"{first['axis']} session failed ({first['error_kind']}): "
            f"{first['error_message'] or ''}".strip(),
            error_kind=first["error_kind"],
        )
        return row
    verdict, findings, parse_ok = merge_sessions(sessions)
    row.update(status="ok", verdict=verdict, findings=findings, parse_ok=parse_ok)
    if case.bucket == "clean":
        row["clean"] = {
            "findings": len(findings),
            "severe": sum(1 for f in findings if f.get("severity") == "severe"),
        }
        return row
    assert case.label is not None
    judgement, error, cost = run_judge(ctx.judge_adapter, models["judge"], case.label, findings)
    row.update(judge=judgement, judge_error=error, judge_cost_usd=cost)
    row["judge_model"] = models["judge"].render()
    return row


def run_eval(
    ctx: EvalContext,
    cases: list[Case],
    variants: list[Variant],
    *,
    dry_run: bool = False,
    max_cost: float | None = None,
    progress: Callable[[dict[str, Any]], None] = lambda _row: None,
) -> dict[str, Any]:
    """Replay every (case, variant) pair not already ``ok`` in ``<out>/results.jsonl``."""
    results = ctx.out / "results.jsonl"
    previous = read_results(results)
    done = {(r["case"], r["variant"]) for r in previous if r.get("status") == "ok"}
    history = list(previous)
    pairs: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    spent = 0.0
    stopped: str | None = None

    def record(row: dict[str, Any]) -> None:
        pairs.append(row)
        progress(row)
        if dry_run:
            return
        history.append(row)
        ctx.out.mkdir(parents=True, exist_ok=True)
        with results.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    for case in cases:
        todo = [v for v in variants if (case.id, v.name) not in done]
        skipped += [{"case": case.id, "variant": v.name} for v in variants if v not in todo]
        if not todo or stopped:
            continue
        try:
            replay = reconstruct(case, ctx.ledger, ctx.runs_dir)
        except ReplayError as exc:
            for variant in todo:
                record({**_base_row(case, variant), "status": "error", "error": str(exc)})
            continue
        try:
            with replay_worktree(replay, str(uuid.uuid4())) as (worktree, diff_text):
                for variant in todo:
                    if not dry_run and max_cost is not None:
                        if spent + _estimate(history, variant.name) > max_cost:
                            stopped = "max_cost"
                            break
                    try:
                        row = _run_pair(ctx, replay, variant, worktree, diff_text, dry_run=dry_run)
                    except ReplayError as exc:
                        row = {**_base_row(case, variant), "status": "error", "error": str(exc)}
                    spent += _pair_cost(row)
                    record(row)
                    if row.get("error_kind") in STOP_ERRORS:
                        stopped = str(row["error_kind"])
                        break
        except ReplayError as exc:
            for variant in todo:
                if not any(p["case"] == case.id and p["variant"] == variant.name for p in pairs):
                    record({**_base_row(case, variant), "status": "error", "error": str(exc)})
    return {
        "out": str(ctx.out),
        "results": str(results),
        "dry_run": dry_run,
        "pairs": pairs,
        "skipped_done": skipped,
        "spent_usd": spent,
        "stopped": stopped,
    }


# ── report ───────────────────────────────────────────────────────────


def _recall(rows: list[dict[str, Any]]) -> dict[str, Any]:
    judged = [r for r in rows if r.get("judge") is not None]
    found = sum(1 for r in judged if r["judge"]["found"])
    return {
        "found": found,
        "judged": len(judged),
        "unjudged": len(rows) - len(judged),
        "recall": round(found / len(judged), 3) if judged else None,
    }


def _severity(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"severe": 0, "minor": 0, "unknown": 0}
    for row in rows:
        judge = row.get("judge")
        if judge and judge["found"]:
            counts[judge["severity_given"] or "unknown"] += 1
    return counts


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-variant scores from the latest row of each (case, variant) pair."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        latest[(row["case"], row["variant"])] = row
    names = [name for name in VARIANTS if any(v == name for _, v in latest)]
    names += sorted({v for _, v in latest} - set(names))
    variants: dict[str, Any] = {}
    for name in names:
        mine = [r for (_, v), r in latest.items() if v == name]
        ok = [r for r in mine if r.get("status") == "ok"]
        clean = [r for r in ok if r["bucket"] == "clean"]
        standards = [r for r in ok if r["label_axis"] == "standards" and r["bucket"] != "clean"]
        costs = [_pair_cost(r) for r in ok]
        seconds = [float(r.get("seconds") or 0) for r in ok]
        variants[name] = {
            "pairs": len(mine),
            "ok": len(ok),
            "errors": len(mine) - len(ok),
            "recall": {
                bucket: _recall([r for r in ok if r["bucket"] == bucket])
                for bucket in POSITIVE_BUCKETS
            },
            "severity_called": {
                bucket: _severity([r for r in ok if r["bucket"] == bucket])
                for bucket in ("undercall", "miss")
            },
            "standards": {
                "written": _recall([r for r in standards if r.get("rule_written") is True]),
                "unwritten": _recall([r for r in standards if r.get("rule_written") is not True]),
            },
            "clean": {
                "cases": len(clean),
                "findings": sum(r["clean"]["findings"] for r in clean),
                "severe": sum(r["clean"]["severe"] for r in clean),
                "cases_with_severe": sum(1 for r in clean if r["clean"]["severe"]),
            },
            "cost_usd": {"total": round(sum(costs), 4), "median": _median(costs)},
            "seconds": {"total": round(sum(seconds), 3), "median": _median(seconds)},
        }
    return {"pairs": len(latest), "variants": variants}


def _fraction(score: dict[str, Any]) -> str:
    if not score["judged"]:
        return "-"
    text = f"{score['found']}/{score['judged']} ({score['recall']:.0%})"
    return text + (f", {score['unjudged']} unjudged" if score["unjudged"] else "")


def render_report(report: dict[str, Any], name: str | None = None) -> str:
    variants = report["variants"]
    heads = list(variants)
    lines = [f"# Review replay report{f': {name}' if name else ''}", ""]
    if not heads:
        return "\n".join([*lines, "No results yet.", ""])

    def table(title: str, rows: list[tuple[str, list[str]]], note: str = "") -> None:
        lines.extend([f"## {title}", ""])
        if note:
            lines.extend([note, ""])
        lines.append("| | " + " | ".join(heads) + " |")
        lines.append("|---|" + "---|" * len(heads))
        lines.extend(f"| {label} | " + " | ".join(cells) + " |" for label, cells in rows)
        lines.append("")

    v = [variants[h] for h in heads]
    table(
        "Recall by bucket",
        [(b, [_fraction(x["recall"][b]) for x in v]) for b in POSITIVE_BUCKETS],
        "`found` cases were caught live: a regression check, not a gain.",
    )
    table(
        "Severity called when found",
        [
            (b, ["{severe} severe, {minor} minor".format(**x["severity_called"][b]) for x in v])
            for b in ("undercall", "miss")
        ],
    )
    table(
        "Standards cases",
        [
            ("rule written", [_fraction(x["standards"]["written"]) for x in v]),
            ("rule unwritten", [_fraction(x["standards"]["unwritten"]) for x in v]),
        ],
    )
    table(
        "Clean cases (false-alarm proxy)",
        [
            ("cases", [str(x["clean"]["cases"]) for x in v]),
            ("findings", [str(x["clean"]["findings"]) for x in v]),
            ("severe findings", [str(x["clean"]["severe"]) for x in v]),
            ("cases with a severe finding", [str(x["clean"]["cases_with_severe"]) for x in v]),
        ],
    )
    table(
        "Cost and time",
        [
            ("pairs ok / errors", [f"{x['ok']} / {x['errors']}" for x in v]),
            ("total cost (USD)", [f"{x['cost_usd']['total']:.4f}" for x in v]),
            ("median cost (USD)", [_num(x["cost_usd"]["median"]) for x in v]),
            ("total seconds", [f"{x['seconds']['total']:.0f}" for x in v]),
            ("median seconds", [_num(x["seconds"]["median"]) for x in v]),
        ],
    )
    return "\n".join(lines)


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"
