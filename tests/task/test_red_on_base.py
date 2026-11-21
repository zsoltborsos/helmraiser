"""Strict opt-in base-red markers round-trip for both step kinds."""

from pathlib import Path

import pytest

from helmraiser.task.loader import TaskLoadError, parse_task, render_task


@pytest.mark.parametrize("kind", ["verify", "evidence"])
@pytest.mark.parametrize("value", [None, "true", "false"])
def test_marker_round_trip(tmp_path: Path, kind: str, value: str | None):
    marker = f"    red_on_base: {value}\n" if value is not None else ""
    content = f"---\nid: marker\n{kind}:\n  - name: check\n    run: false\n{marker}---\nGoal\n"
    spec = parse_task(content, tmp_path / "task.md", {})
    assert getattr(spec, kind)[0].red_on_base is (value == "true")
    rendered = render_task(spec)
    assert ("red_on_base:" in rendered) is (value == "true")
    loaded = parse_task(rendered, tmp_path / "task.md", {})
    assert getattr(loaded, kind)[0].red_on_base is (value == "true")


@pytest.mark.parametrize("kind", ["verify", "evidence"])
@pytest.mark.parametrize("name", ["check", None])
@pytest.mark.parametrize("value", ['"true"', "0", "1", "null", "[]", "{}"])
def test_invalid_marker(tmp_path: Path, kind: str, name: str | None, value: str):
    named = f"    name: {name}\n" if name else ""
    content = (
        f"---\nid: marker\n{kind}:\n  - run: false\n{named}    red_on_base: {value}\n---\nGoal\n"
    )
    with pytest.raises(TaskLoadError, match=f"{kind} step '{name or kind + '-1'}': red_on_base"):
        parse_task(content, tmp_path / "task.md", {})
