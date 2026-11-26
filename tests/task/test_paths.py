"""Path declarations retain owner spelling and reject unsafe patterns."""

from dataclasses import replace
from pathlib import Path

import pytest

from helmraiser.task.loader import TaskLoadError, parse_task, render_task, spec_to_frontmatter
from helmraiser.task.paths import (
    add_paths_to_frontmatter,
    matches_paths,
    parse_paths,
    paths_overlap,
    srt_protected_errors,
)
from helmraiser.task.schema import validate_task
from helmraiser.utils.markdown import serialize_frontmatter


@pytest.mark.parametrize(
    "left,right,expected",
    [
        (("src/a/**",), ("src/b/**",), False),
        (("src/",), ("src/a.py",), True),
        (("**/*.py",), ("docs/a",), True),
        (("src/file*.py",), ("src/file*.txt",), True),
        (("./src//a/",), ("src/a/**",), True),
        (("src/[a]",), ("src/a",), False),
        (("src/{a,b}",), ("src/a",), False),
        (("src/a", "docs/**"), ("docs/a",), True),
        (("src/?/a",), ("src/x/b",), True),
    ],
)
def test_conservative_overlap(left, right, expected):
    assert paths_overlap(left, right) is expected
    assert paths_overlap(right, left) is expected


def task(value=None, *, declared=True):
    fm = {"id": "scope", "title": "Scope", "project": "/tmp/project"}
    if declared:
        fm["paths"] = value
    return parse_task(serialize_frontmatter(fm, "Body"), Path("task.md"), {})


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("src/a.py", "src/a.py", True),
        ("src/a.py", "src/A.py", False),
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/deep/a.py", False),
        ("src/file?.py", "src/file1.py", True),
        ("src/file?.py", "src/file12.py", False),
        ("src/**/*.py", "src/a.py", True),
        ("src/**/*.py", "src/x/a.py", True),
        ("src/**/*.py", "src/x/y/a.py", True),
        ("**/**/a.py", "a.py", True),
        ("**/**/a.py", "x/y/a.py", True),
        ("src/", "src/a", True),
        ("src/", "src/deep/a", True),
        ("src/", "src", False),
        ("src/", "src-other/a", False),
        ("./src//*.py", "./src/a.py", True),
        ("*", ".hidden", True),
        ("[ab].py", "a.py", False),
        ("[ab].py", "[ab].py", True),
        ("{a,b}!", "{a,b}!", True),
        ("a?b", "a\nb", True),
    ],
)
def test_matching(pattern, path, expected):
    assert matches_paths(path, (pattern,)) is expected


def test_multiple_patterns_and_rename_endpoints():
    patterns = ("src/", "docs/*.md")
    assert matches_paths("docs/readme.md", patterns)
    assert matches_paths("src/renamed.py", patterns)
    assert not matches_paths("outside/old.py", patterns)
    assert not matches_paths("anything", ())


def test_omission():
    spec = task(declared=False)
    assert spec.paths is None
    assert "paths" not in spec_to_frontmatter(spec)


def test_roundtrip():
    patterns = ["./src//**/*.py", "docs/", "src/file?.py", "docs/"]
    spec = task(patterns)
    assert spec.paths == tuple(patterns)
    assert "paths" not in spec.extra
    assert parse_task(render_task(spec), Path("task.md"), {}).paths == tuple(patterns)
    assert validate_task(spec) == []


@pytest.mark.parametrize("value", [None, [], "src/*", [1], [""], ["  "], ["/src"], ["a/../b"]])
def test_invalid(value):
    with pytest.raises(ValueError, match="paths"):
        parse_paths(value)
    with pytest.raises(TaskLoadError, match="paths"):
        task(value)


@pytest.mark.parametrize("paths", [(), ("",), ("/src",), ("src/../docs",)])
def test_direct_spec_invalid(paths):
    assert any("paths" in e for e in validate_task(replace(task(declared=False), paths=paths)))


@pytest.mark.parametrize(
    "pattern,protected",
    [
        # a protected directory, itself or beneath it
        (".claude/agents/**", ".claude/agents"),
        (".claude/agents/", ".claude/agents"),
        (".claude/agents/foo.md", ".claude/agents"),
        (".claude/commands/*.md", ".claude/commands"),
        (".vscode/settings.json", ".vscode"),
        ("./.idea//**", ".idea"),
        # a protected file
        (".mcp.json", ".mcp.json"),
        (".bashrc", ".bashrc"),
        (".gitmodules", ".gitmodules"),
        # nested, with the name as a segment at any depth
        ("sub/.vscode/settings.json", ".vscode"),
        ("a/b/c/.claude/agents/x.md", ".claude/agents"),
        ("packages/app/.mcp.json", ".mcp.json"),
        ("**/.idea/**", ".idea"),
        ("docs/**/.zshrc", ".zshrc"),
        # git's own files: srt keeps a worktree's .git whole, a repository's hooks and config
        (".git/hooks/pre-commit", ".git"),
        ("sub/.git/config", ".git"),
        # srt compares names in lower case
        (".Claude/Agents/x.md", ".claude/agents"),
    ],
)
def test_srt_protected_paths_are_refused(pattern, protected):
    (error,) = srt_protected_errors((pattern,))
    assert repr(pattern) in error and protected in error
    assert "never lets a run write" in error and "outside Helmraiser" in error
    assert validate_task(task([pattern])) == [error]


@pytest.mark.parametrize(
    "pattern",
    [
        "**",
        "src/**",
        "src/app.py",
        "docs/",
        ".claude/**",
        ".claude/skills/**",
        ".claude/settings.json",
        ".claude/agent/x.md",
        ".github/workflows/ci.yml",
        ".gitignore",
        ".gitattributes",
        "foo.mcp.json",
        "src/.mcp.jsonx",
        ".v*/settings.json",
        "**/.claude*/**",
        "agents/.claude/x.md",
        "*",
    ],
)
def test_ordinary_paths_and_wildcards_are_not_refused(pattern):
    assert srt_protected_errors((pattern,)) == []
    assert validate_task(task([pattern])) == []


def test_each_protected_pattern_gets_its_own_error():
    errors = srt_protected_errors(("src/**", ".mcp.json", "docs/", ".idea/x"))
    assert len(errors) == 2
    assert "'.mcp.json'" in errors[0] and "'.idea/x'" in errors[1]


def test_isolation_none_is_not_sandboxed_so_nothing_is_refused():
    spec = replace(task([".claude/agents/**"]), isolation="none")
    assert validate_task(spec) == []
    assert len(validate_task(replace(spec, isolation="process"))) == 1


def test_task_paths_verify_name_is_reserved():
    from helmraiser.task.model import VerifyStep

    spec = replace(task(["src/"]), verify=[VerifyStep(name="task-paths", run="true")])
    assert any("reserved" in e for e in validate_task(spec))


BODY = "\n## Goal\n\nKeep: [x]\n- not a path\n"


@pytest.mark.parametrize(
    "before,after",
    [
        (
            "---\nid: t\npaths: [done.txt, 'src/**']  # owner\npolicy: review\n---" + BODY,
            "---\nid: t\npaths: [done.txt, 'src/**', docs/a.md, \"*.md\"]  # owner\n"
            "policy: review\n---" + BODY,
        ),
        (
            "---\nid: t\npaths:\n  - done.txt\n  - 'src/**'\npolicy: review\n---" + BODY,
            "---\nid: t\npaths:\n  - done.txt\n  - 'src/**'\n  - docs/a.md\n  - \"*.md\"\n"
            "policy: review\n---" + BODY,
        ),
        (
            "---\nid: t\npolicy: review\npaths:\n- done.txt\n---" + BODY,
            '---\nid: t\npolicy: review\npaths:\n- done.txt\n- docs/a.md\n- "*.md"\n---' + BODY,
        ),
    ],
)
def test_add_paths_to_frontmatter_is_a_minimal_edit(before, after):
    assert add_paths_to_frontmatter(before, ["docs/a.md", "*.md"]) == after


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter\n",
        "---\nid: t\n---\npaths: [a]\n",
        "---\nid: t\npaths: done.txt\n---\n",
        "---\nid: t\npaths:\npolicy: review\n---\n",
    ],
)
def test_add_paths_to_frontmatter_refuses_without_a_list(text):
    with pytest.raises(ValueError):
        add_paths_to_frontmatter(text, ["a.md"])
