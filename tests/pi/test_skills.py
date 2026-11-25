"""Per-stage worker skill folders (``pi/skills.py``) and their sandbox carve-outs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helmraiser.isolation.process import SETTINGS_FILENAME, ProcessIsolation
from helmraiser.pi import skills
from helmraiser.pi.adapter import PiJsonAdapter
from helmraiser.pi.protocol import PiRequest
from helmraiser.pi.skills import (
    argv_skill_dirs,
    skill_read_paths,
    stage_skill_args,
)
from helmraiser.task.model import ModelSpec


def skill(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test\n---\n")
    return path


def flags(*dirs: Path) -> tuple[str, ...]:
    return ("--no-skills", *(arg for d in dirs for arg in ("--skill", str(d))))


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    (path / ".git").mkdir(parents=True)
    return path


def test_no_stage_folder_leaves_pi_discovery(worktree: Path):
    skill(worktree / ".pi" / "skills", "own")
    assert not skills.SKILLS_ROOT.exists()
    assert stage_skill_args("implement", worktree) == ()


def test_empty_stage_folder_means_project_skills_only(worktree: Path):
    (skills.SKILLS_ROOT / "review").mkdir(parents=True)
    assert stage_skill_args("review", worktree) == ("--no-skills",)
    own = skill(worktree / ".agents" / "skills", "own")
    assert stage_skill_args("review", worktree) == flags(own)


def test_stage_skills_sorted_with_symlinks_then_project_skills(tmp_path: Path, worktree: Path):
    folder = skills.SKILLS_ROOT / "plan"
    beta = skill(folder, "beta")
    hub = skill(tmp_path / "hub" / "skills", "alpha")
    (folder / "alpha").symlink_to(hub)
    (folder / "notes").mkdir()  # no SKILL.md: not a skill
    (folder / "README.md").write_text("not a skill\n")
    (folder / "dangling").symlink_to(tmp_path / "gone")
    pi_own = skill(worktree / ".pi" / "skills", "pi-own")
    agents_own = skill(worktree / ".agents" / "skills", "agents-own")
    assert stage_skill_args("plan", worktree) == flags(folder / "alpha", beta, pi_own, agents_own)
    # Other stages keep pi's discovery.
    assert stage_skill_args("implement", worktree) == ()


def test_dry_run_finds_project_skills_in_the_checkout(tmp_path: Path):
    checkout = tmp_path / "project"
    (checkout / ".git").mkdir(parents=True)
    skill(checkout / ".pi" / "skills", "own")
    stage = skill(skills.SKILLS_ROOT / "implement", "tdd")
    planned = tmp_path / "worktrees" / "not-yet"
    assert stage_skill_args("implement", planned, checkout=checkout) == flags(
        stage, planned / ".pi" / "skills" / "own"
    )


@pytest.mark.parametrize("project", [True, False])
def test_none_ignores_worker_skills(worktree, project):
    skill(skills.SKILLS_ROOT / "implement", "worker")
    own = [skill(worktree / ".pi" / "skills", "own")] if project else []
    assert stage_skill_args("implement", worktree, skills="none") == flags(*own)


def test_discover_ignores_explicit_skills(worktree):
    skill(skills.SKILLS_ROOT / "plan", "worker")
    skill(worktree / ".agents" / "skills", "own")
    assert stage_skill_args("plan", worktree, skills="discover") == ()


def test_named_set_sorted_with_symlinks_then_project(worktree, tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "SKILL_SETS_ROOT", tmp_path / "sets")
    folder = skills.SKILL_SETS_ROOT / "test" / "plan"
    beta = skill(folder, "beta")
    alpha = skill(tmp_path / "hub", "alpha")
    (folder / "alpha").symlink_to(alpha)
    (folder / "notes").mkdir()
    skill(skills.SKILLS_ROOT / "plan", "excluded")
    own = skill(worktree / ".pi" / "skills", "own")
    assert stage_skill_args("plan", worktree, skills="test") == flags(folder / "alpha", beta, own)


@pytest.mark.parametrize("project", [True, False])
def test_missing_named_stage_never_discovers(worktree, tmp_path, monkeypatch, project):
    monkeypatch.setattr(skills, "SKILL_SETS_ROOT", tmp_path / "sets")
    (skills.SKILL_SETS_ROOT / "test").mkdir(parents=True)
    skill(skills.SKILLS_ROOT / "review", "excluded")
    own = [skill(worktree / ".agents" / "skills", "own")] if project else []
    assert stage_skill_args("review", worktree, skills="test") == flags(*own)


@pytest.mark.parametrize("mode", ["none", "test"])
def test_project_only_modes_remap_checkout(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(skills, "SKILL_SETS_ROOT", tmp_path / "sets")
    checkout = tmp_path / "project"
    (checkout / ".git").mkdir(parents=True)
    own = skill(checkout / ".pi" / "skills", "own")
    planned = tmp_path / "not-yet"
    assert stage_skill_args("review", planned, checkout=checkout, skills=mode) == flags(
        planned / own.relative_to(checkout)
    )


@pytest.mark.parametrize("folder", [True, False])
def test_explicit_default_matches_existing_behavior(worktree, folder):
    if folder:
        skill(skills.SKILLS_ROOT / "implement", "worker")
    skill(worktree / ".pi" / "skills", "own")
    assert stage_skill_args("implement", worktree, skills="default") == stage_skill_args(
        "implement", worktree
    )


def test_argv_skill_dirs_stop_at_the_prompt():
    argv = ["pi", "--skill", "/a", "--no-skills", "--skill", "/b", "--", "--skill", "/c"]
    assert argv_skill_dirs(argv) == [Path("/a"), Path("/b")]
    assert argv_skill_dirs(["pi", "--mode", "json", "--", "prompt"]) == []


def test_read_paths_follow_a_symlinked_skill(tmp_path: Path):
    hub = skill(tmp_path / "hub" / "skills", "tdd")
    refs = tmp_path / "hub" / "refs"
    refs.mkdir()
    (hub / "refs").symlink_to(refs)
    linked = tmp_path / "stage" / "tdd"
    linked.parent.mkdir()
    linked.symlink_to(hub)
    assert skill_read_paths([linked]) == (hub.resolve(), refs.resolve())


def test_session_sandbox_opens_stage_skills_inside_other_projects(
    tmp_path: Path, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LEEBOARD_HOME", raising=False)
    projects = tmp_path / "projects"
    hub = skill(projects / "hub" / "skills", "tdd")
    refs = projects / "hub" / "refs"
    refs.mkdir()
    (hub / "refs").symlink_to(refs)
    (hub / "keys").symlink_to(home / ".ssh")  # a fixed denial stays closed
    folder = skills.SKILLS_ROOT / "implement"
    folder.mkdir(parents=True)
    (folder / "tdd").symlink_to(hub)
    run_dir = tmp_path / "data" / "runs" / "r"
    req = PiRequest(
        prompt="go",
        session_id="r-implement",
        model=ModelSpec("p/m", "high"),
        cwd=tmp_project,
        session_dir=run_dir / "sessions",
        extra_args=stage_skill_args("implement", tmp_project),
    )
    isolation = ProcessIsolation(
        {"isolation": {"projects_root": [str(projects)], "tmpdir": str(tmp_path / "srt-tmp")}}
    )

    argv = PiJsonAdapter(argv_wrapper=isolation.wrap).build_argv(req)

    assert argv[argv.index("--skill") + 1] == str(folder / "tdd")
    fs = json.loads((run_dir / SETTINGS_FILENAME).read_text())["filesystem"]
    assert str(projects / "hub") in fs["denyRead"]
    assert str(hub.resolve()) in fs["allowRead"]
    assert str(refs.resolve()) in fs["allowRead"]
    assert str((home / ".ssh").resolve()) not in fs["allowRead"]
    # Without the stage folder the session gets no carve-out.
    plain = tmp_path / "data" / "runs" / "plain"
    PiJsonAdapter(argv_wrapper=isolation.wrap).build_argv(
        PiRequest(
            prompt="go",
            session_id="p-implement",
            model=ModelSpec("p/m", "high"),
            cwd=tmp_project,
            session_dir=plain / "sessions",
        )
    )
    assert json.loads((plain / SETTINGS_FILENAME).read_text())["filesystem"]["allowRead"] == []
