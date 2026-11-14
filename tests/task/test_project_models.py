"""``[models]`` in ``.helmraiser/project.toml``: parsing and model resolution."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from helmraiser.task.loader import (
    load_task,
    parse_task,
    profile_models,
    write_task,
)
from helmraiser.task.model import ModelSpec
from helmraiser.task.project import ProjectDefaults, load_project_defaults
from helmraiser.task.schema import validate_task
from helmraiser.utils.markdown import parse_frontmatter, serialize_frontmatter


def write(project: Path, text: str) -> None:
    path = project / ".helmraiser" / "project.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_models_are_parsed_in_order_and_stripped(tmp_path: Path):
    write(
        tmp_path,
        '[models]\nimplement = "  opencode-go/deepseek-v4.1-flash:medium  "\n'
        'fallback = "openai-codex/gpt-6.1-sol:high"\n',
    )

    models = load_project_defaults(tmp_path).models

    assert models == {
        "implement": "opencode-go/deepseek-v4.1-flash:medium",
        "fallback": "openai-codex/gpt-6.1-sol:high",
    }
    assert list(models) == ["implement", "fallback"]


def test_missing_project_file_uses_defaults(tmp_path: Path):
    defaults = load_project_defaults(tmp_path)

    assert defaults.models == {}
    assert defaults == ProjectDefaults()


def test_project_without_models_uses_defaults(tmp_path: Path):
    write(tmp_path, "[defaults]\n")

    assert load_project_defaults(tmp_path).models == {}


def test_unknown_model_key_names_file_and_key(tmp_path: Path):
    write(tmp_path, '[models]\nimplment = "x/y"\n')

    with pytest.raises(ValueError, match=r"models: unknown key\(s\) implment") as exc:
        load_project_defaults(tmp_path)

    assert str(tmp_path / ".helmraiser" / "project.toml") in str(exc.value)


def test_unknown_model_keys_are_sorted(tmp_path: Path):
    write(tmp_path, '[models]\nzebra = "x/y"\napple = "y/z"\n')

    with pytest.raises(ValueError, match=r"models: unknown key\(s\) apple, zebra"):
        load_project_defaults(tmp_path)


@pytest.mark.parametrize(
    "text,message",
    [
        ('models = "x"\n', "[models] must be a table"),
        ("[models]\nimplement = 3\n", "models.implement must be a non-empty string"),
        ('[models]\nimplement = "  "\n', "models.implement must be a non-empty string"),
    ],
)
def test_invalid_models(tmp_path: Path, text: str, message: str):
    write(tmp_path, text)

    with pytest.raises(ValueError) as exc:
        load_project_defaults(tmp_path)

    assert str(tmp_path / ".helmraiser" / "project.toml") in str(exc.value)
    assert message in str(exc.value)


@pytest.fixture
def config() -> dict:
    return {
        "profiles": {
            "intake": {"model": "p/intake", "thinking": "high"},
            "plan": {"model": "p/planner", "thinking": "high"},
            "implement": {"model": "p/impl", "thinking": "medium"},
            "fallback": {"model": "p/fallback", "thinking": "high"},
            "review": {"model": "p/review", "thinking": "high"},
            "scout": {"model": "p/scout", "thinking": "low"},
        },
        "budget": {"max_cost_usd": 7.0, "max_minutes": 45, "max_iterations": 3},
    }


def task_text(project: Path, models: dict[str, str] | None = None) -> str:
    frontmatter: dict = {
        "id": "project-models",
        "title": "Project models",
        "project": str(project),
        "status": "draft",
        "policy": "review",
        "verify": [{"name": "ok", "run": "true"}],
    }
    if models is not None:
        frontmatter["models"] = models
    return serialize_frontmatter(frontmatter, "## Goal\n\nDo the thing.")


def test_task_project_config_precedence(tmp_path: Path, config: dict):
    write(
        tmp_path,
        '[models]\nimplement = "proj/implement:low"\nfallback = "proj/fallback:low"\n',
    )

    spec = parse_task(
        task_text(tmp_path, {"implement": "task/implement:medium"}),
        tmp_path / "task.md",
        config,
    )

    assert spec.models["implement"] == ModelSpec("task/implement", "medium")
    assert spec.models["fallback"] == ModelSpec("proj/fallback", "low")
    assert spec.models["review"] == ModelSpec("p/review", "high")
    assert spec.explicit_models == frozenset({"implement"})


def test_project_without_models_preserves_resolution(tmp_path: Path, config: dict):
    before = parse_task(task_text(tmp_path), tmp_path / "task.md", config)

    write(tmp_path, "[defaults]\n")
    after = parse_task(task_text(tmp_path), tmp_path / "task.md", config)

    assert after.models == before.models
    assert after.explicit_models == before.explicit_models == frozenset()


def test_project_models_do_not_become_explicit(tmp_path: Path, config: dict):
    write(
        tmp_path,
        '[models]\nimplement = "proj/implement:low"\nfallback = "proj/fallback:low"\n',
    )

    spec = parse_task(task_text(tmp_path), tmp_path / "task.md", config)

    assert spec.models["implement"] == ModelSpec("proj/implement", "low")
    assert spec.models["fallback"] == ModelSpec("proj/fallback", "low")
    assert spec.explicit_models == frozenset()


@pytest.mark.parametrize(
    "models",
    [None, {"implement": "task/implement:high"}],
)
def test_project_defaults_round_trip_without_becoming_explicit(
    tmp_path: Path, config: dict, models: dict[str, str] | None
):
    project_path = tmp_path / "project"
    project_path.mkdir()
    write(
        project_path,
        '[models]\nimplement = "proj/implement:low"\nfallback = "proj/fallback:low"\n',
    )
    path = tmp_path / "task.md"
    spec = parse_task(task_text(project_path, models), path, config)
    write_task(spec, path)

    written = parse_frontmatter(path.read_text())["frontmatter"]
    if models is None:
        assert "models" not in written
    else:
        assert written["models"] == models

    reloaded = load_task(path, config)
    assert reloaded.models == spec.models
    assert reloaded.explicit_models == spec.explicit_models


def test_nonabsolute_project_still_parses(tmp_path: Path, config: dict):
    spec = parse_task(task_text(Path("relative-project")), tmp_path / "task.md", config)

    assert spec.models["implement"] == ModelSpec("p/impl", "medium")


def test_absolute_project_without_project_file_still_parses(tmp_path: Path, config: dict):
    spec = parse_task(task_text(tmp_path), tmp_path / "task.md", config)

    assert spec.models["implement"] == ModelSpec("p/impl", "medium")
    assert spec.explicit_models == frozenset()


def test_profile_models_uses_project_defaults(tmp_path: Path, config: dict):
    write(tmp_path, '[models]\nimplement = "proj/implement:low"\n')

    profiles = profile_models(tmp_path, config)

    assert profiles["implement"] == ModelSpec("proj/implement", "low")
    assert profiles["fallback"] == ModelSpec("p/fallback", "high")
    assert profiles["review"] == ModelSpec("p/review", "high")


def test_project_value_replaces_thinking(tmp_path: Path, config: dict):
    write(tmp_path, '[models]\nimplement = "project/implement"\n')

    spec = parse_task(task_text(tmp_path), tmp_path / "task.md", config)

    assert spec.models["implement"].thinking is None


def test_effective_reviewer_must_differ(tmp_project: Path, config: dict):
    write(tmp_project, '[models]\nimplement = "p/review:high"\n')
    path = tmp_project / "task.md"

    spec = parse_task(task_text(tmp_project), path, config)
    errors = validate_task(replace(spec, status="queued"))
    assert any("reviewer must differ from implementer" in error for error in errors)

    with_review = parse_task(task_text(tmp_project, {"review": "p/other"}), path, config)
    errors = validate_task(replace(with_review, status="queued"))
    assert errors == []
