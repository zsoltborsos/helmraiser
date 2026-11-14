"""Spot verify/evidence commands that inspect source files instead of running the code.

Intake uses ``structural_reason`` to add a note for such a proposed step; it never drops
the step. The rules are deliberately narrow (few false positives): a command counts only
when it greps, reads, parses or tests the existence of a path that looks like source
(under ``src/``, ``lib/``, ``app/``, … or with a source-file suffix). Paths under
``$HELMRAISER_EVIDENCE_DIR``, build output and plain text files do not count.

``outside_paths_warnings`` notes the project files a verify step names that the task's
``paths`` do not cover: an implementer cannot change them, so such a run often ends in a
scope request.

``body_paths_outside_scope`` finds tracked files and directories named in the whole
body (fenced blocks included, URLs excluded) that ``paths`` does not cover.
``body_outside_paths_warnings`` lists them in one advisory intake note, without changing
the declared paths. There is no body note when ``paths`` is omitted or empty, the project
is not a git repository, or listing tracked files fails.

``related_tests_warnings`` advises about tracked tests outside ``paths`` named after
or directly importing in-scope JS/TS or Python sources. It never expands scope.
"""

from __future__ import annotations

import io
import re
import shlex
import subprocess
import tokenize
from pathlib import Path, PurePosixPath

from helmraiser.task.model import TaskSpec
from helmraiser.task.paths import matches_paths, paths_overlap

SOURCE_ROOTS = frozenset(
    {"src", "lib", "app", "apps", "packages", "pkg", "internal", "cmd", "source"}
)
NOT_SOURCE_ROOTS = frozenset(
    {"dist", "build", "out", "coverage", "node_modules", ".venv", "venv", "target", "tmp"}
)
SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".rb",
        ".c",
        ".h",
        ".cpp",
        ".swift",
        ".vue",
        ".svelte",
        ".qml",
    }
)

_RELATED_SOURCE_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py"})
_TEST_DIRECTORIES = frozenset({"__tests__", "tests", "test", "e2e"})
# Consume comments and strings whole: import-looking prose must not become code.
_JS_TOKENS = re.compile(
    r"(?P<comment>//[^\n]*|/\*[\s\S]*?(?:\*/|$))"
    r"|(?P<string>'(?:\\[\s\S]|[^'\\])*(?:'|\\?$)|\"(?:\\[\s\S]|[^\"\\])*(?:\"|\\?$))"
    r"|(?P<template>`(?:\\[\s\S]|[^`\\])*(?:`|\\?$))"
    r"|(?P<word>[\w$]+)|(?P<punct>[^\s])"
)

_PARSERS = re.compile(r"\b(createSourceFile|ast\.parse)\s*\(")
_READ_CALL = re.compile(r"\b(?:readFileSync|readFile|open)\s*\(([^)]*)")
_PATH_READ = re.compile(r"\bPath\(([^)]*)\)\s*\.\s*read_(?:text|bytes)\b")
_LITERAL = re.compile(r"""['"]([^'"]+)['"]""")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_GREPS = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack"})
_READERS = frozenset({"cat", "head", "tail", "wc", "sed", "awk"})
_TESTS = frozenset({"test", "[", "[["})
_EXISTS_FLAGS = frozenset({"-f", "-e", "-s", "-d", "-r"})
_PREFIXES = frozenset({"!", "if", "then", "elif", "else", "do", "while", "until", "time"})


def is_source_path(path: str) -> bool:
    path = path.strip()
    if not path or "$" in path or "HELMRAISER_EVIDENCE_DIR" in path or path.startswith("/"):
        return False
    parts = [part for part in path.removeprefix("./").split("/") if part]
    if not parts or parts[0] in NOT_SOURCE_ROOTS:
        return False
    return parts[0] in SOURCE_ROOTS or PurePosixPath(path).suffix in SOURCE_SUFFIXES


def _script_reason(run: str) -> str | None:
    parser = _PARSERS.search(run)
    if parser:
        return f"parses source ({parser.group(1)})"
    for match in (*_READ_CALL.finditer(run), *_PATH_READ.finditer(run)):
        args = match.group(1)
        if "HELMRAISER_EVIDENCE_DIR" in args:
            continue
        for literal in _LITERAL.findall(args):
            if is_source_path(literal):
                return f"reads {literal}"
    return None


def _segments(line: str) -> list[list[str]]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token and all(ch in lexer.punctuation_chars for ch in token):
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _command_reason(words: list[str]) -> str | None:
    while words and (words[0] in _PREFIXES or _ASSIGNMENT.match(words[0])):
        words = words[1:]
    if not words:
        return None
    command, args = PurePosixPath(words[0]).name, words[1:]
    if command in _GREPS:
        paths: list[str] = []
        pattern_given = skip = False
        for arg in args:
            if skip:
                skip = False
            elif arg in ("-e", "-f", "--regexp"):
                pattern_given = skip = True
            elif arg.startswith("-"):
                continue
            elif not pattern_given:
                pattern_given = True
            else:
                paths.append(arg)
        source = next((path for path in paths if is_source_path(path)), None)
        return f"greps {source}" if source else None
    if command in _READERS:
        source = next(
            (arg for arg in args if not arg.startswith("-") and is_source_path(arg)), None
        )
        return f"reads {source}" if source else None
    if command in _TESTS:
        for flag, path in zip(args, args[1:], strict=False):
            if flag in _EXISTS_FLAGS and is_source_path(path):
                return f"tests that {path} exists"
    return None


def outside_paths_warnings(task: TaskSpec) -> list[str]:
    """A warning for each existing repo file a verify step names outside ``paths``."""
    if task.paths is None:
        return []
    named: list[str] = []
    for step in task.verify:
        for line in step.run.splitlines():
            try:
                segments = _segments(line)
            except ValueError:
                continue
            for word in (w for words in segments for w in words):
                path = word.split("::")[0].removeprefix("./")
                if (
                    path.startswith(("/", "-"))
                    or "$" in path
                    or ".." in path.split("/")
                    or not PurePosixPath(path).suffix
                    or not (task.project / path).is_file()
                    or matches_paths(path, task.paths)
                ):
                    continue
                named.append(path)
    return [
        f"verify runs {path}, which is outside paths: add it if the change needs to edit it"
        for path in dict.fromkeys(named)
    ]


def _tracked_files(repo: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode:
        return set()
    return set(result.stdout.split("\0")) - {""}


def body_paths_outside_scope(body: str, paths: tuple[str, ...] | None, repo: Path) -> list[str]:
    """Sorted tracked files and directories mentioned in the body outside ``paths``."""
    if not paths:
        return []
    files = _tracked_files(repo)
    directories = {
        str(parent)
        for name in files
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    }
    outside: set[str] = set()
    for token in re.split(r"[\s`\"'\[\]()<> ,;|]+", body):
        if "://" in token:
            continue
        token = token.lstrip("*").rstrip("*.,:;!?")
        token = token.split("#", 1)[0].split("::", 1)[0]
        token = re.sub(r":\d+(?:-\d+)?$", "", token).removeprefix("./")
        if (
            not token
            or token.startswith(("/", "-", "~", "$"))
            or any(char in token for char in "*?${}=")
            or ("/" not in token and not PurePosixPath(token).suffix)
        ):
            continue
        directory = token.removesuffix("/")
        if any(segment in ("", "..") for segment in directory.split("/")):
            continue
        if token in files:
            if not matches_paths(token, paths):
                outside.add(token)
        elif directory in directories and not paths_overlap((directory + "/",), paths):
            outside.add(directory + "/")
    return sorted(outside)


def body_outside_paths_warnings(task: TaskSpec) -> list[str]:
    """One advisory note listing the body's tracked mentions outside scope."""
    outside = body_paths_outside_scope(task.body, task.paths, task.project)
    if not outside:
        return []
    return [
        f"body mentions files outside paths: {', '.join(outside)}"
        " — add them to paths if the task edits them"
    ]


def related_tests_warnings(task: TaskSpec) -> list[str]:
    """One bounded advisory about related tests, without expanding declared paths."""
    outside = related_tests_outside_scope(task.paths, task.project)
    if not outside:
        return []
    names = ", ".join(outside[:10])
    if len(outside) > 10:
        names += f" and {len(outside) - 10} more"
    return [
        f"tests outside paths exercise in-scope files: {names}"
        " — add them to paths if the change may need to update them"
    ]


def _is_test_file(path: str) -> bool:
    file = PurePosixPath(path)
    return (
        bool(_TEST_DIRECTORIES.intersection(file.parts[:-1]))
        or ".test." in file.name
        or ".spec." in file.name
        or (
            file.suffix == ".py"
            and (file.name.startswith("test_") or file.name.endswith("_test.py"))
        )
    )


def _source_key(path: str) -> str:
    file = PurePosixPath(path)
    return file.parent.name if file.stem in {"index", "__init__"} else file.stem


def _test_name_key(path: str) -> str:
    stem = PurePosixPath(path).stem
    stem = re.split(r"\.(?:test|spec)(?:\.|$)", stem, maxsplit=1)[0]
    return stem.removeprefix("test_").removesuffix("_test")


def _module_key(specifier: str) -> str:
    return PurePosixPath(specifier.rsplit("/", 1)[-1]).stem


def _js_import_keys(text: str) -> set[str]:
    tokens = [
        (match.lastgroup, match.group())
        for match in _JS_TOKENS.finditer(text)
        if match.lastgroup != "comment"
    ]
    keys: set[str] = set()
    for i, (kind, value) in enumerate(tokens):
        if kind != "word":
            continue
        tail = tokens[i + 1 : i + 5]
        literal = None
        if value in {"from", "import"} and tail and tail[0][0] == "string":
            literal = tail[0][1]
        elif value in {"require", "import"} and len(tail) >= 2:
            if tail[0][1] == "(" and tail[1][0] == "string":
                literal = tail[1][1]
        elif value == "jest" and len(tail) == 4:
            if [token[1] for token in tail[:3]] == [".", "mock", "("] and tail[3][0] == "string":
                literal = tail[3][1]
        if literal and len(literal) >= 2 and literal[-1] == literal[0]:
            keys.add(_module_key(literal[1:-1]))
    return keys


def _python_statement_import_keys(statement: list[tokenize.TokenInfo]) -> set[str]:
    for i, token in enumerate(statement):
        if token.type != tokenize.NAME:
            continue
        if token.string == "from":
            module: list[str] = []
            for part in statement[i + 1 :]:
                if part.string == "import":
                    return {module[-1]} if module else set()
                if part.type == tokenize.NAME:
                    module.append(part.string)
            return set()
        if token.string == "import":
            keys: set[str] = set()
            module = []
            alias = False
            for part in statement[i + 1 :]:
                if part.string == ",":
                    if module:
                        keys.add(module[-1])
                    module = []
                    alias = False
                elif part.string == "as":
                    alias = True
                elif part.type == tokenize.NAME and not alias:
                    module.append(part.string)
            if module:
                keys.add(module[-1])
            return keys
    return set()


def _python_import_keys(text: str) -> set[str]:
    keys: set[str] = set()
    statement: list[tokenize.TokenInfo] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.NEWLINE or (
                token.type == tokenize.OP and token.string == ";"
            ):
                keys.update(_python_statement_import_keys(statement))
                statement = []
            elif token.type not in {
                tokenize.COMMENT,
                tokenize.NL,
                tokenize.INDENT,
                tokenize.DEDENT,
            }:
                statement.append(token)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # The bounded read may end in an incomplete statement. Keep earlier imports.
        pass
    return keys


def related_tests_outside_scope(paths: tuple[str, ...] | None, repo: Path) -> list[str]:
    """Sorted tracked tests directly related to declared JS/TS or Python sources."""
    if not paths:
        return []
    files = _tracked_files(repo)
    sources = [
        path
        for path in files
        if not _is_test_file(path)
        and PurePosixPath(path).suffix in _RELATED_SOURCE_SUFFIXES
        and matches_paths(path, paths)
    ]
    if len(sources) > 200:
        return []
    keys = {key for path in sources if len(key := _source_key(path)) >= 3}
    if not keys:
        return []
    related = []
    for path in files:
        if (
            PurePosixPath(path).suffix not in _RELATED_SOURCE_SUFFIXES
            or not _is_test_file(path)
            or matches_paths(path, paths)
        ):
            continue
        try:
            with (repo / path).open("rb") as stream:
                text = stream.read(256 * 1024).decode("utf-8", errors="replace")
        except OSError:
            continue
        imported = (
            _python_import_keys(text)
            if PurePosixPath(path).suffix == ".py"
            else _js_import_keys(text)
        )
        if _test_name_key(path) in keys or keys.intersection(imported):
            related.append(path)
    return sorted(related)


def structural_reason(run: str) -> str | None:
    """Why ``run`` looks like a check of source text or file existence, or ``None``."""
    reason = _script_reason(run)
    if reason:
        return reason
    for line in run.splitlines():
        try:
            segments = _segments(line)
        except ValueError:  # unbalanced quotes: not a line we can judge
            continue
        for words in segments:
            reason = _command_reason(words)
            if reason:
                return reason
    return None
