"""The intake heuristic that flags verify/evidence steps which inspect source text."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from helmraiser.task.loader import parse_task
from helmraiser.task.model import VerifyStep
from helmraiser.task.structural import (
    _tracked_files,
    body_outside_paths_warnings,
    body_paths_outside_scope,
    large_budget_warnings,
    outside_paths_warnings,
    related_tests_outside_scope,
    related_tests_warnings,
    structural_reason,
)

STRUCTURAL = [
    (
        "node -e \"const s=require('fs').readFileSync('src/app.ts','utf8');"
        ' if(!/export function foo/.test(s)) process.exit(1)"',
        "reads src/app.ts",
    ),
    (
        "node -e \"const assert=require('node:assert');"
        " assert.match(readFileSync(path.join(root, 'src', 'x.ts'), 'utf8'), /foo/)\"",
        "reads src",
    ),
    (
        "node -e \"const ts=require('typescript');"
        " ts.createSourceFile('a.ts', code, ts.ScriptTarget.Latest)\"",
        "parses source (createSourceFile)",
    ),
    ("python -c \"import ast; ast.parse(open('lib/mod.py').read())\"", "parses source (ast.parse)"),
    (
        "python3 - <<'EOF'\nfrom pathlib import Path\n"
        "assert 'held' in Path('src/helmraiser/cli/commands.py').read_text()\nEOF",
        "reads src/helmraiser/cli/commands.py",
    ),
    ('grep -q "import { foo }" src/index.ts', "greps src/index.ts"),
    ("grep -rq useEffect src/", "greps src/"),
    ("rg -q -e foo packages/core", "greps packages/core"),
    ("cat src/x.py | grep -q def", "reads src/x.py"),
    ("npm test && test -f src/foo.test.ts", "tests that src/foo.test.ts exists"),
    ("[ -f lib/widget.py ]", "tests that lib/widget.py exists"),
    ("! grep -q console.log app/page.tsx", "greps app/page.tsx"),
]

BEHAVIOURAL = [
    "npm test",
    ".venv/bin/python -m pytest -q --no-cov tests/cli",
    "ruff check src tests && black --check src tests",
    "npx tsc --noEmit",
    "node --test src/foo.test.ts",
    "npx vitest run src/foo.test.ts",
    "grep -q ok done.txt",
    'pytest -q | tee "$HELMRAISER_EVIDENCE_DIR/out.txt" && grep -q passed "$HELMRAISER_EVIDENCE_DIR/out.txt"',
    "node -e \"const r=JSON.parse(require('fs').readFileSync("
    "process.env.HELMRAISER_EVIDENCE_DIR+'/r.json','utf8')); if(!r.ok) process.exit(1)\"",
    'test -f "$HELMRAISER_EVIDENCE_DIR/status.json"',
    "npm run build && test -f dist/index.js",
    "git diff --name-only | grep -q '^src/'",
    "helmraiser status --json | grep -q queued",
    'grep -q "src/app.ts" coverage/summary.txt',
    "grep -q 'held' README.md",
    "unbalanced 'quote",
]


@pytest.mark.parametrize(("run", "reason"), STRUCTURAL)
def test_structural_steps_are_flagged(run: str, reason: str) -> None:
    assert structural_reason(run) == reason


@pytest.mark.parametrize("run", BEHAVIOURAL)
def test_behavioural_steps_are_not_flagged(run: str) -> None:
    assert structural_reason(run) is None


@pytest.mark.parametrize(
    ("cost", "expected"),
    [
        (19.99, []),
        (
            20.0,
            [
                "budget.max_cost_usd is $20; tasks budgeted at $20 or more often fail: "
                "consider splitting this task into smaller tasks linked with depends_on"
            ],
        ),
        (
            25.0,
            [
                "budget.max_cost_usd is $25; tasks budgeted at $20 or more often fail: "
                "consider splitting this task into smaller tasks linked with depends_on"
            ],
        ),
        (
            25.5,
            [
                "budget.max_cost_usd is $25.5; tasks budgeted at $20 or more often fail: "
                "consider splitting this task into smaller tasks linked with depends_on"
            ],
        ),
    ],
)
def test_large_budget_warnings(tmp_path, cost, expected) -> None:
    spec = parse_task(
        f"---\nid: t\ntitle: T\nproject: {tmp_path}\nstatus: draft\n---\n", tmp_path / "t.md", {}
    )
    task = replace(spec, budget=replace(spec.budget, max_cost_usd=cost))
    assert large_budget_warnings(task) == expected
    assert task.budget.max_cost_usd == cost


def test_outside_paths_warnings(tmp_path) -> None:
    for name in ("tests/test_old.py", "src/story.test.ts", "docs/a.md", "Makefile"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x")
    spec = parse_task(
        f"---\nid: t\ntitle: T\nproject: {tmp_path}\nstatus: draft\n---\n", tmp_path / "t.md", {}
    )
    runs = [
        "python -m pytest -q tests/test_old.py::test_x ./src/story.test.ts",
        "make -f Makefile && cat docs/a.md missing.py $HOME/x.py /etc/x.py ../x.py",
        "npx vitest run src/story.test.ts 'unbalanced",
    ]
    steps = tuple(VerifyStep(name=f"s{n}", run=run) for n, run in enumerate(runs))
    task = replace(spec, verify=steps, paths=("docs/**",))
    assert outside_paths_warnings(task) == [
        "verify runs tests/test_old.py, which is outside paths: "
        "add it if the change needs to edit it",
        "verify runs src/story.test.ts, which is outside paths: "
        "add it if the change needs to edit it",
    ]
    assert outside_paths_warnings(replace(task, paths=None)) == []


@pytest.fixture
def body_paths_repo(tmp_path, git_init):
    repo = tmp_path / "repo"
    repo.mkdir()
    git_init(repo)
    names = (
        "src/a.py",
        "src/b.py",
        "tests/test_a.py",
        "docs/x.md",
        "docs/nested/y.md",
        ".github/ci.yml",
    )
    for name in names:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "--", *names], check=True)
    (repo / "src/untracked.py").write_text("x\n")
    return repo


def test_body_paths_mentions(body_paths_repo):
    body = """Edit `src/a.py:12` and src/b.py:12-20, docs/x.md#intro.
    **./tests/test_a.py::test_name**; .github/ci.yml.
    src/a.py:12::test_x#anchor docs/x.md src/b.py!
    ```bash
    pytest tests/test_a.py::test_x
    ```
    """
    assert body_paths_outside_scope(body, ("other.py",), body_paths_repo) == [
        ".github/ci.yml",
        "docs/x.md",
        "src/a.py",
        "src/b.py",
        "tests/test_a.py",
    ]


@pytest.mark.parametrize(
    "token",
    [
        "https://example.com/src/b.py",
        "src/missing.py",
        "src/untracked.py",
        "prose",
        "src",
        "/src/b.py",
        "-src/b.py",
        "~/src/b.py",
        "$src/b.py",
        "src/*.py",
        "src/b?.py",
        "src/${b}.py",
        "src/{b}.py",
        "x=src/b.py",
        "src/../src/b.py",
        "src//b.py",
        "docs//",
    ],
)
def test_body_paths_ignores_non_candidates(body_paths_repo, token):
    assert body_paths_outside_scope(token, ("other.py",), body_paths_repo) == []


@pytest.mark.parametrize(
    "delimiter", [" ", "\n", "`", "'", '"', "[", "]", "(", ")", "<", ">", ",", ";", "|"]
)
def test_body_paths_splitting(body_paths_repo, delimiter):
    body = delimiter.join(["src/a.py", "src/b.py"])
    assert body_paths_outside_scope(body, ("src/a.py",), body_paths_repo) == ["src/b.py"]


@pytest.mark.parametrize(
    "paths",
    [("src/a.py", "src/b.py", "tests/test_a.py", "docs/x.md"), ("src/**", "tests/", "docs/*.md")],
)
def test_body_paths_coverage(body_paths_repo, paths):
    body = "src/a.py src/b.py tests/test_a.py docs/x.md src/untracked.py"
    assert body_paths_outside_scope(body, paths, body_paths_repo) == []


def test_body_paths_directories(body_paths_repo):
    body = "docs/ docs/nested src/ ./tests/. docs//"
    assert body_paths_outside_scope(body, ("src/a.py",), body_paths_repo) == [
        "docs/",
        "docs/nested/",
        "tests/",
    ]


def test_body_paths_uses_index_for_files_and_directories(body_paths_repo):
    # A tracked file and its parents still count when absent from the worktree.
    (body_paths_repo / "docs/nested/y.md").unlink()
    (body_paths_repo / "docs/nested").rmdir()
    (body_paths_repo / "generated").mkdir()
    (body_paths_repo / "generated/output.py").write_text("x\n")

    body = "docs/nested/y.md docs/nested/ generated/ generated/output.py"
    assert body_paths_outside_scope(body, ("src/a.py",), body_paths_repo) == [
        "docs/nested/",
        "docs/nested/y.md",
    ]


@pytest.mark.parametrize("paths", [None, ()])
def test_body_paths_unrestricted(body_paths_repo, paths, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("unrestricted scope must not invoke git")

    monkeypatch.setattr("helmraiser.task.structural.subprocess.run", unexpected)
    assert body_paths_outside_scope("src/b.py docs/", paths, body_paths_repo) == []


def test_body_paths_non_repository(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/b.py").write_text("x\n")
    assert body_paths_outside_scope("src/b.py", ("src/a.py",), tmp_path) == []


@pytest.mark.parametrize(
    "failure", [None, FileNotFoundError(), subprocess.TimeoutExpired("git", 5)]
)
def test_body_paths_git_failures(monkeypatch, tmp_path, failure):
    def run(*args, **kwargs):
        if failure is not None:
            raise failure
        return subprocess.CompletedProcess(args[0], 1, stdout="src/b.py\0")

    monkeypatch.setattr("helmraiser.task.structural.subprocess.run", run)
    assert body_paths_outside_scope("src/b.py", ("src/a.py",), tmp_path) == []


def test_tracked_listing_preserves_nul_delimited_names(monkeypatch, tmp_path):
    def run(args, **kwargs):
        assert args == ["git", "-C", str(tmp_path), "ls-files", "-z"]
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "surrogateescape",
            "check": False,
            "timeout": 5,
        }
        return subprocess.CompletedProcess(args, 0, stdout="src/a file.py\0src/b.py\0")

    monkeypatch.setattr("helmraiser.task.structural.subprocess.run", run)
    assert _tracked_files(tmp_path) == {"src/a file.py", "src/b.py"}


def test_body_outside_paths_warnings(body_paths_repo: Path):
    spec = parse_task(
        f"---\nid: t\ntitle: T\nproject: {body_paths_repo}\nstatus: draft\n---\n",
        body_paths_repo / "t.md",
        {},
    )
    task = replace(spec, body="src/b.py docs/x.md src/b.py", paths=("src/a.py",))
    assert body_outside_paths_warnings(task) == [
        "body mentions files outside paths: docs/x.md, src/b.py"
        " — add them to paths if the task edits them"
    ]
    assert body_outside_paths_warnings(replace(task, paths=("src/**", "docs/"))) == []
    assert body_outside_paths_warnings(replace(task, paths=None)) == []


@pytest.fixture
def related_tests_repo(tmp_path, git_init):
    repo = tmp_path / "repo"
    repo.mkdir()
    git_init(repo)

    def create(files):
        for name, text in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        return repo

    return create


def test_related_tests_name_matches(related_tests_repo):
    tests = ["a/widget.test.tsx", "b/widget.spec.ts", "test_mod_a.py", "mod_a_test.py"]
    repo = related_tests_repo(dict.fromkeys(["src/widget.tsx", "pkg/mod_a.py", *tests], ""))
    assert related_tests_outside_scope(("src/", "pkg/"), repo) == sorted(tests)


@pytest.mark.parametrize(
    "directory", ["__tests__", "tests", "test", "e2e", "contest", "tests-extra"]
)
def test_related_tests_directory_segments(related_tests_repo, directory):
    test = f"{directory}/widget.ts"
    repo = related_tests_repo({"src/widget.ts": "", test: ""})
    expected = [] if directory in {"contest", "tests-extra"} else [test]
    assert related_tests_outside_scope(("src/widget.ts",), repo) == expected


def test_related_tests_index_and_init_keys(related_tests_repo):
    repo = related_tests_repo(
        dict.fromkeys(
            [
                "src/widgets/index.ts",
                "pkg/gadgets/__init__.py",
                "widgets.spec.ts",
                "tests/test_gadgets.py",
            ],
            "",
        )
    )
    assert related_tests_outside_scope(("src/", "pkg/"), repo) == [
        "tests/test_gadgets.py",
        "widgets.spec.ts",
    ]


@pytest.mark.parametrize("suffix", [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py"])
def test_related_tests_source_suffixes(related_tests_repo, suffix):
    repo = related_tests_repo({f"src/widget{suffix}": "", "widget.test.ts": ""})
    assert related_tests_outside_scope(("src/",), repo) == ["widget.test.ts"]


@pytest.mark.parametrize(
    "name",
    [
        "tests/README.md",
        "tests/__snapshots__/widget.test.tsx.snap",
        "tests/widget.png",
        "tests/widget.json",
        "tests/widget.test.txt",
    ],
)
def test_related_tests_ignores_unsupported_test_files(related_tests_repo, monkeypatch, name):
    repo = related_tests_repo({"src/widget.ts": "", name: "Examples from 'widget'."})
    original_open = Path.open
    opened = []

    def record_open(path, *args, **kwargs):
        opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", record_open)
    assert related_tests_outside_scope(("src/widget.ts",), repo) == []
    assert opened == []


def test_related_tests_source_selection(related_tests_repo):
    repo = related_tests_repo(
        dict.fromkeys(
            [
                "src/widget.vue",
                "src/tests/test_gadget.py",
                "widget.test.ts",
                "gadget.test.ts",
            ],
            "",
        )
    )
    (repo / "src/gadget.ts").write_text("")
    assert related_tests_outside_scope(("src/",), repo) == []


def test_related_tests_short_keys(related_tests_repo):
    repo = related_tests_repo(
        dict.fromkeys(
            [
                "src/ab.ts",
                "src/abc.ts",
                "ab.test.ts",
                "abc.test.ts",
            ],
            "",
        )
    )
    assert related_tests_outside_scope(("src/",), repo) == ["abc.test.ts"]


@pytest.mark.parametrize("declaration", ["tests/widget.test.ts", "tests/*.test.ts", "tests/"])
def test_related_tests_already_declared(related_tests_repo, declaration):
    repo = related_tests_repo({"src/widget.ts": "", "tests/widget.test.ts": ""})
    assert related_tests_outside_scope(("src/widget.ts", declaration), repo) == []


@pytest.mark.parametrize("count", [200, 201])
@pytest.mark.parametrize("stem", ["widget", "ab"])
def test_related_tests_source_cap(related_tests_repo, count, stem):
    files = dict.fromkeys([f"src/{n}/{stem}.ts" for n in range(count - 1)], "")
    files.update({"src/widget.ts": "", "widget.test.ts": ""})
    repo = related_tests_repo(files)
    assert related_tests_outside_scope(("src/",), repo) == (
        ["widget.test.ts"] if count == 200 else []
    )


@pytest.mark.parametrize("paths", [None, ()])
def test_related_tests_unrestricted(tmp_path, paths, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("unrestricted scope must not invoke git")

    monkeypatch.setattr("helmraiser.task.structural.subprocess.run", unexpected)
    assert related_tests_outside_scope(paths, tmp_path) == []


def test_related_tests_non_repository(tmp_path):
    assert related_tests_outside_scope(("src/",), tmp_path) == []
    assert related_tests_outside_scope(("src/",), tmp_path / "missing") == []


@pytest.mark.parametrize(
    "failure", [None, FileNotFoundError(), subprocess.TimeoutExpired("git", 5)]
)
def test_related_tests_git_failures(monkeypatch, tmp_path, failure):
    def run(*args, **kwargs):
        if failure is not None:
            raise failure
        return subprocess.CompletedProcess(args[0], 1, stdout="src/widget.ts\0")

    monkeypatch.setattr("helmraiser.task.structural.subprocess.run", run)
    assert related_tests_outside_scope(("src/",), tmp_path) == []


def test_related_tests_unreadable(related_tests_repo, monkeypatch):
    repo = related_tests_repo({"src/widget.ts": "", "widget.test.ts": ""})
    (repo / "widget.test.ts").unlink()
    assert related_tests_outside_scope(("src/",), repo) == []

    def unreadable(*args, **kwargs):
        raise PermissionError("unreadable")

    monkeypatch.setattr(Path, "open", unreadable)
    assert related_tests_outside_scope(("src/",), repo) == []


@pytest.mark.parametrize(
    "text",
    [
        "import { w } from '../widget';",
        'import "@/components/widget.tsx";',
        "require ( './widget.js' );",
        "import(\n'../widget'\n);",
        'jest . mock ( "./widget" );',
        "// ignored\nimport { w } from './widget';",
    ],
)
def test_related_tests_js_import_forms(related_tests_repo, text):
    repo = related_tests_repo({"src/widget.tsx": "", "tests/other.ts": text})
    assert related_tests_outside_scope(("src/",), repo) == ["tests/other.ts"]


@pytest.mark.parametrize(
    "text",
    [
        "import { w } from './widgets';",
        "import { w } from './widget/other';",
        "require(moduleName);",
        "import(`./widget`);",
        "// import { w } from './widget';",
        "/* require('./widget'); */",
        """const prose = "require('widget')";""",
        """const prose = `import { w } from 'widget'`;""",
        "const widget = 1;",
        "/* unterminated comment require('widget')",
        """const prose = "unterminated require('widget')""",
        """const prose = "unterminated require('widget')""" + "\\",
        "const prose = `unterminated require('widget')" + "\\",
    ],
)
def test_related_tests_js_comments_and_prose(related_tests_repo, text):
    repo = related_tests_repo({"src/widget.ts": "", "tests/other.ts": text})
    assert related_tests_outside_scope(("src/",), repo) == []


@pytest.mark.parametrize("before_limit", [True, False])
def test_related_tests_bounded_read(related_tests_repo, monkeypatch, before_limit):
    statement = "import './widget';\n"
    text = statement + " " * (256 * 1024) if before_limit else " " * (256 * 1024) + statement
    repo = related_tests_repo({"src/widget.ts": "", "tests/other.ts": text})
    original_open = Path.open
    reads = []

    class BoundedReader:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def __init__(self, stream):
            self.stream = stream

        def read(self, size):
            reads.append(size)
            return self.stream.read(size)

    def open_file(path, *args, **kwargs):
        return BoundedReader(original_open(path, *args, **kwargs))

    monkeypatch.setattr(Path, "open", open_file)
    expected = ["tests/other.ts"] if before_limit else []
    assert related_tests_outside_scope(("src/",), repo) == expected
    assert reads == [262144]


def test_related_tests_tolerant_decoding(related_tests_repo):
    repo = related_tests_repo({"src/widget.ts": "", "tests/other.ts": ""})
    (repo / "tests/other.ts").write_bytes(b"// \xff\nimport './widget';")
    assert related_tests_outside_scope(("src/",), repo) == ["tests/other.ts"]


@pytest.mark.parametrize(
    "text",
    [
        "import mod_a\n",
        "import pkg.mod_a\n",
        "import pkg.mod_a as alias\n",
        "import json, pkg.mod_a as alias, os\n",
        "from pkg.mod_a import X\n",
        "from .mod_a import X\n",
        "from pkg.mod_a import (\n    X,\n    Y,\n)\n",
        "def test_it():\n    import pkg.mod_a\n",
        "import json; import pkg.mod_a\n",
        "import pkg.mod_a\n'''unterminated trailing string",
        "import pkg.mod_a\ndef broken(:\n",
    ],
)
def test_related_tests_python_import_forms(related_tests_repo, text):
    repo = related_tests_repo({"pkg/mod_a.py": "", "tests/test_other.py": text})
    assert related_tests_outside_scope(("pkg/",), repo) == ["tests/test_other.py"]


@pytest.mark.parametrize(
    "text",
    [
        "# from pkg.mod_a import X\n",
        "'import pkg.mod_a'\n",
        "'''from pkg.mod_a import X\nimport mod_a\n'''\n",
        "import mod_abc\n",
        "import pkg.mod_a.other\n",
        "from other import mod_a\n",
        "import other as mod_a\n",
    ],
)
def test_related_tests_python_comments_and_prose(related_tests_repo, text):
    repo = related_tests_repo({"pkg/mod_a.py": "", "tests/test_other.py": text})
    assert related_tests_outside_scope(("pkg/",), repo) == []


def test_related_tests_warnings(related_tests_repo):
    repo = related_tests_repo({"src/widget.ts": "", "widget.test.ts": ""})
    spec = parse_task(
        f"---\nid: t\ntitle: T\nproject: {repo}\nstatus: draft\npaths: [src/widget.ts]\n---\n",
        repo / "t.md",
        {},
    )
    assert related_tests_warnings(spec) == [
        "tests outside paths exercise in-scope files: widget.test.ts"
        " — add them to paths if the change may need to update them"
    ]
    assert spec.paths == ("src/widget.ts",)
    assert related_tests_warnings(replace(spec, paths=None)) == []
    assert related_tests_warnings(replace(spec, paths=())) == []
    assert related_tests_warnings(replace(spec, paths=("missing.ts",))) == []


@pytest.mark.parametrize("count", [10, 11, 13, 20, 21])
def test_related_tests_warnings_limit(tmp_path, monkeypatch, count):
    names = [f"widget-{n:02}.test.ts" for n in range(count)]
    monkeypatch.setattr(
        "helmraiser.task.structural.related_tests_outside_scope", lambda *args: names
    )
    spec = parse_task(
        f"---\nid: t\ntitle: T\nproject: {tmp_path}\nstatus: draft\n---\n",
        tmp_path / "t.md",
        {},
    )
    remainder = f" and {count - 10} more" if count > 10 else ""
    expected = [
        f"tests outside paths exercise in-scope files: {', '.join(names[:10])}{remainder}"
        + (f" ({count} total; none automatically added: limit is 20)" if count > 20 else "")
        + " — add them to paths if the change may need to update them"
    ]
    assert related_tests_warnings(spec) == expected
    monkeypatch.setattr(
        "helmraiser.task.structural.related_tests_outside_scope",
        lambda *args: pytest.fail("must reuse supplied candidates"),
    )
    assert related_tests_warnings(spec, names) == expected


def test_related_tests_python_init_import(related_tests_repo):
    repo = related_tests_repo(
        {
            "pkg/widgets/__init__.py": "",
            "tests/test_other.py": "from pkg.widgets import X\n",
        }
    )
    assert related_tests_outside_scope(("pkg/",), repo) == ["tests/test_other.py"]
