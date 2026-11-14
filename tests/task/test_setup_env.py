"""``setup`` / ``env`` in the task schema and ``.helmraiser/project.toml`` defaults."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from helmraiser.config import DEFAULT_CONFIG, save_config
from helmraiser.ledger.db import default_data_dir
from helmraiser.task.loader import TaskLoadError, load_task, parse_task, render_task, write_task
from helmraiser.task.project import (
    DEFAULT_SETUP_ALLOWED_DOMAINS,
    ProjectDefaults,
    SetupConfig,
    effective_env,
    effective_run_limit,
    effective_setup,
    expand_env,
    load_project_defaults,
    setup_write_paths,
    worktree_manager,
)
from helmraiser.task.schema import validate_task

TASK = """---
id: t
title: T
project: {project}
setup: python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev]"
env:
  PYTHONPATH: $HELMRAISER_WORKTREE/src
  WIDGET_MODE: test
verify:
  - name: tests
    run: .venv/bin/python -m pytest -q
---
body
"""


def write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "task.md"
    path.write_text(content, encoding="utf-8")
    return path


class TestSchema:
    def test_load_setup_and_env(self, tmp_path: Path):
        spec = load_task(write(tmp_path, TASK.format(project=tmp_path)), DEFAULT_CONFIG)
        assert spec.setup == 'python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev]"'
        assert spec.env == {"PYTHONPATH": "$HELMRAISER_WORKTREE/src", "WIDGET_MODE": "test"}
        assert "setup" not in spec.extra and "env" not in spec.extra
        assert validate_task(spec) == []

    def test_defaults_when_absent(self, tmp_path: Path):
        spec = load_task(write(tmp_path, f"---\nid: t\ntitle: T\nproject: {tmp_path}\n---\n"), {})
        assert spec.setup is None and spec.env == {}
        assert "setup:" not in render_task(spec) and "env:" not in render_task(spec)

    def test_round_trip(self, tmp_path: Path):
        spec = load_task(write(tmp_path, TASK.format(project=tmp_path)), DEFAULT_CONFIG)
        out = tmp_path / "out.md"
        write_task(spec, out)
        text = out.read_text()
        assert (
            "setup: python3 -m venv .venv" in text
            and "PYTHONPATH: $HELMRAISER_WORKTREE/src" in text
        )
        assert load_task(out, DEFAULT_CONFIG) == replace(spec, source_path=out.resolve())

    def test_env_values_are_strings(self, tmp_path: Path):
        content = f"---\nid: t\ntitle: T\nproject: {tmp_path}\nenv:\n  PORT: 8080\n---\n"
        assert load_task(write(tmp_path, content), {}).env == {"PORT": "8080"}

    def test_env_must_be_a_mapping(self, tmp_path: Path):
        with pytest.raises(TaskLoadError, match="env must be a mapping"):
            parse_task(f"---\nid: t\ntitle: T\nproject: {tmp_path}\nenv: [A]\n---\n", tmp_path, {})

    def test_invalid_env_key_is_a_validation_error(self, tmp_path: Path):
        spec = load_task(write(tmp_path, TASK.format(project=tmp_path)), DEFAULT_CONFIG)
        bad = replace(spec, env={"NOT-VALID": "x"})
        assert any("NOT-VALID" in e for e in validate_task(bad))


class TestProjectDefaults:
    @pytest.mark.parametrize("global_limit, override", [(1, 2), (3, 2), (2, 4)])
    def test_run_limit_override(self, tmp_path, global_limit, override):
        config = {"max_runs_per_project": global_limit}
        assert effective_run_limit(tmp_path, {}) == 1
        assert effective_run_limit(tmp_path, config) == global_limit
        (tmp_path / ".helmraiser").mkdir()
        (tmp_path / ".helmraiser/project.toml").write_text(f"max_runs_per_project = {override}\n")
        assert effective_run_limit(tmp_path, config) == override

    @pytest.mark.parametrize("value", ["true", "false", '"2"', "2.0", "0", "-1"])
    def test_invalid_run_limit(self, tmp_path, value):
        (tmp_path / ".helmraiser").mkdir()
        (tmp_path / ".helmraiser/project.toml").write_text(f"max_runs_per_project = {value}\n")
        with pytest.raises(ValueError, match=r"project.toml: max_runs_per_project"):
            load_project_defaults(tmp_path)

    def test_missing_file(self, tmp_path: Path):
        assert load_project_defaults(tmp_path) == ProjectDefaults()

    def test_loads_setup_and_env(self, tmp_path: Path):
        (tmp_path / ".helmraiser").mkdir()
        (tmp_path / ".helmraiser" / "project.toml").write_text(
            '[defaults]\nsetup = "make venv"\n\n[defaults.env]\nA = "1"\nB = "$HELMRAISER_WORKTREE/b"\n'
        )
        defaults = load_project_defaults(tmp_path)
        assert defaults == ProjectDefaults(
            setup="make venv", env={"A": "1", "B": "$HELMRAISER_WORKTREE/b"}
        )

    def test_setup_config_defaults_and_explicit_empty_domains(self, tmp_path: Path):
        assert load_project_defaults(tmp_path).setup_config == SetupConfig(
            allowed_domains=DEFAULT_SETUP_ALLOWED_DOMAINS
        )
        (tmp_path / ".helmraiser").mkdir()
        (tmp_path / ".helmraiser" / "project.toml").write_text(
            '[setup]\nwrite = ["~/.cache/pip", "$CACHE", "cache", "cache"]\n'
            "allowed_domains = []\n"
        )
        setup = load_project_defaults(tmp_path).setup_config
        assert setup.allowed_domains == ()
        assert setup_write_paths(
            setup, tmp_path, {"HOME": "/home/test", "CACHE": "/srv/cache/.pnpm-store"}
        ) == (
            Path("/home/test/.cache/pip"),
            Path("/srv/cache/.pnpm-store"),
            (tmp_path / "cache").resolve(),
        )

    def test_setup_host_requires_approval(self, tmp_path: Path):
        (tmp_path / ".helmraiser").mkdir()
        path = tmp_path / ".helmraiser" / "project.toml"
        path.write_text('[setup]\nsandbox = "host"\n')
        with pytest.raises(ValueError, match="requires defaults.require_approval"):
            load_project_defaults(tmp_path)
        path.write_text('[defaults]\nrequire_approval = true\n[setup]\nsandbox = "host"\n')
        assert load_project_defaults(tmp_path).setup_config.sandbox == "host"

    @pytest.mark.parametrize(
        "content",
        [
            "setup = false\n",
            '[setup]\nwrite = "x"\n',
            '[setup]\nwrite = [""]\n',
            "[setup]\nallowed_domains = [1]\n",
            '[defaults]\nrequire_approval = true\n[setup]\nsandbox = "srt"\n',
        ],
    )
    def test_setup_config_rejects_invalid_values(self, tmp_path: Path, content: str):
        (tmp_path / ".helmraiser").mkdir()
        (tmp_path / ".helmraiser" / "project.toml").write_text(content)
        with pytest.raises(ValueError, match="setup"):
            load_project_defaults(tmp_path)

    def test_loads_guardrails(self, tmp_path: Path):
        (tmp_path / ".helmraiser").mkdir()
        (tmp_path / ".helmraiser" / "project.toml").write_text(
            '[defaults]\nrequire_approval = true\n\n[sandbox]\ndeny_read = ["secrets", "~/private"]\n'
        )

        defaults = load_project_defaults(tmp_path)

        assert defaults.require_approval is True
        assert defaults.deny_read == ("secrets", "~/private")

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            ('[defaults]\nrequire_approval = "yes"\n', "require_approval"),
            ('[sandbox]\ndeny_read = "secrets"\n', "deny_read"),
            ("[sandbox]\ndeny_read = [1]\n", "deny_read"),
            ("sandbox = false\n", r"\[sandbox\]"),
        ],
    )
    def test_guardrails_reject_wrong_types(
        self, tmp_path: Path, content: str, message: str
    ) -> None:
        (tmp_path / ".helmraiser").mkdir()
        path = tmp_path / ".helmraiser" / "project.toml"
        path.write_text(content)

        with pytest.raises(ValueError, match=rf"project\.toml.*{message}"):
            load_project_defaults(tmp_path)

    def test_loads_base(self, tmp_path: Path):
        (tmp_path / ".helmraiser").mkdir()
        (tmp_path / ".helmraiser" / "project.toml").write_text('[defaults]\nbase = "programme/x"\n')

        assert load_project_defaults(tmp_path).base == "programme/x"

    def test_worktree_dir_is_parsed_and_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".helmraiser").mkdir()
        (tmp_path / ".helmraiser" / "project.toml").write_text(
            '[defaults]\nworktree_dir = "$HOME/helmraiser-worktrees"\n'
        )
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))

        defaults = load_project_defaults(tmp_path)
        manager = worktree_manager(tmp_path)

        assert defaults.worktree_dir == "$HOME/helmraiser-worktrees"
        assert manager.path_for("topic", "run").parent == home / "helmraiser-worktrees"

    def test_relative_worktree_dir_is_resolved_from_project(self, tmp_path: Path):
        (tmp_path / ".helmraiser").mkdir()
        (tmp_path / ".helmraiser" / "project.toml").write_text(
            '[defaults]\nworktree_dir = "../run-worktrees"\n'
        )

        manager = worktree_manager(tmp_path)

        assert manager.path_for("topic", "run").parent == tmp_path.parent / "run-worktrees"

    def test_unset_worktree_dir_uses_default(self, tmp_path: Path):
        manager = worktree_manager(tmp_path)
        suffix = hashlib.sha256(str(tmp_path.resolve()).encode("utf-8")).hexdigest()[:12]
        expected = default_data_dir() / "worktrees" / f"{tmp_path.name}-{suffix}"
        assert manager.path_for("topic", "run").parent == expected
        assert manager._metadata_path("run") == expected / "run.json"
        assert worktree_manager(tmp_path).path_for("topic", "run").parent == expected
        assert not expected.exists()

    def test_same_basename_repositories_have_distinct_directories(self, tmp_path: Path):
        first, second = tmp_path / "a" / "repo", tmp_path / "b" / "repo"
        a = worktree_manager(first).path_for("topic", "run")
        b = worktree_manager(second).path_for("topic", "run")
        assert a.parent != b.parent
        assert a.parent.name.startswith("repo-")
        assert b.parent.name.startswith("repo-")

    def test_resolved_repository_aliases_share_directory(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(repo, target_is_directory=True)
        expected = worktree_manager(repo).path_for("topic", "run")
        assert worktree_manager(alias).path_for("topic", "run") == expected
        assert worktree_manager(repo / ".." / "repo").path_for("topic", "run") == expected

    @pytest.mark.parametrize("root", ["absolute", "env", "tilde", "relative"])
    def test_global_root_expansion(self, tmp_path: Path, monkeypatch, root: str):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("ROOT", str(tmp_path / "roots"))
        monkeypatch.chdir(tmp_path)
        configured = {
            "absolute": str(tmp_path / "roots"),
            "env": "$ROOT",
            "tilde": "~/roots",
            "relative": "roots",
        }[root]
        save_config({"worktree_root": configured})
        path = worktree_manager(tmp_path / "repo").path_for("topic", "run")
        assert path.parent.parent == tmp_path / "roots"
        assert not path.parent.exists()

    def test_global_root_is_read_on_each_call(self, tmp_path: Path):
        repo = tmp_path / "repo"
        save_config({"worktree_root": str(tmp_path / "first")})
        first = worktree_manager(repo).path_for("topic", "run")
        save_config({"worktree_root": str(tmp_path / "second")})
        second = worktree_manager(repo).path_for("topic", "run")
        assert first.parent.name == second.parent.name
        assert first.parent.parent == tmp_path / "first"
        assert second.parent.parent == tmp_path / "second"

    @pytest.mark.parametrize("root", [False, 42, [], {}, "", "  "])
    def test_invalid_global_root(self, tmp_path: Path, root):
        save_config({"worktree_root": root})
        with pytest.raises(ValueError, match="worktree_root must be a non-empty string or null"):
            worktree_manager(tmp_path)

    @pytest.mark.parametrize("root", [False, "/unused"])
    def test_project_override_bypasses_global_root(self, tmp_path: Path, root):
        save_config({"worktree_root": root})
        (tmp_path / ".helmraiser").mkdir()
        (tmp_path / ".helmraiser" / "project.toml").write_text(
            '[defaults]\nworktree_dir = "../explicit"\n'
        )
        path = worktree_manager(tmp_path).path_for("topic", "run")
        assert path.parent == tmp_path.parent / "explicit"
        assert not path.parent.exists()

    def test_invalid_toml(self, tmp_path: Path):
        (tmp_path / ".helmraiser").mkdir()
        (tmp_path / ".helmraiser" / "project.toml").write_text("[defaults\n")
        with pytest.raises(ValueError, match="project.toml"):
            load_project_defaults(tmp_path)

    def test_task_values_win_env_merges(self, tmp_path: Path):
        spec = load_task(write(tmp_path, TASK.format(project=tmp_path)), DEFAULT_CONFIG)
        defaults = ProjectDefaults(setup="make venv", env={"WIDGET_MODE": "prod", "EXTRA": "e"})
        assert effective_setup(spec, defaults) == spec.setup
        assert effective_env(spec, defaults) == {
            "WIDGET_MODE": "test",
            "EXTRA": "e",
            "PYTHONPATH": "$HELMRAISER_WORKTREE/src",
        }
        bare = replace(spec, setup=None, env={})
        assert effective_setup(bare, defaults) == "make venv"
        assert effective_env(bare, defaults) == defaults.env

    def test_expand_env(self):
        base = {"HELMRAISER_WORKTREE": "/wt", "HELMRAISER_RUN_ID": "r1"}
        env = {
            "A": "$HELMRAISER_WORKTREE/src",
            "B": "${HELMRAISER_RUN_ID}-x",
            "C": "$UNKNOWN",
            "D": "plain",
        }
        assert expand_env(env, base) == {"A": "/wt/src", "B": "r1-x", "C": "$UNKNOWN", "D": "plain"}
