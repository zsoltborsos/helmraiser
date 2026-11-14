"""Owner-declared repository path scope."""

import json
import re
from functools import cache
from typing import Any


def parse_paths(value: Any) -> tuple[str, ...]:
    """Validate declarations without changing their spelling or order."""
    if not isinstance(value, list) or not value:
        raise ValueError(f"paths must be a non-empty list of relative patterns: {value!r}")
    for pattern in value:
        if (
            not isinstance(pattern, str)
            or not pattern.strip()
            or pattern.startswith("/")
            or ".." in pattern.split("/")
        ):
            raise ValueError(f"paths contains an invalid relative pattern: {pattern!r}")
    return tuple(value)


def _segments(pattern: str) -> tuple[str, ...]:
    segments = tuple(p for p in pattern.split("/") if p not in ("", "."))
    return segments + ("?*", "**") if pattern.endswith("/") else segments


def paths_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Conservatively compare literal segment prefixes before any wildcard."""

    def prefix(pattern: str) -> tuple[str, ...]:
        result = []
        for segment in _segments(pattern):
            if "*" in segment or "?" in segment:
                break
            result.append(segment)
        return tuple(result)

    for a in map(prefix, left):
        for b in map(prefix, right):
            length = min(len(a), len(b))
            if a[:length] == b[:length]:
                return True
    return False


def matches_paths(path: str, patterns: tuple[str, ...]) -> bool:
    """Match POSIX paths with segment-local wildcards and whole-segment globstars.

    Dots and empty separator segments are normalized only for matching. All
    punctuation except ``*`` and ``?`` is literal. A trailing slash requires a
    descendant, not a file with the directory's name.
    """
    parts = tuple(p for p in path.split("/") if p not in ("", "."))
    for pattern in patterns:
        segments = _segments(pattern)

        @cache
        def match(i: int, j: int, segments: tuple[str, ...] = segments) -> bool:
            if j == len(segments):
                return i == len(parts)
            segment = segments[j]
            if segment == "**":
                return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
            if i == len(parts):
                return False
            regex = "".join(
                ".*" if c == "*" else "." if c == "?" else re.escape(c) for c in segment
            )
            return re.fullmatch(regex, parts[i], flags=re.DOTALL) is not None and match(
                i + 1, j + 1
            )

        if match(0, 0):
            return True
    return False


_PLAIN_PATH = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*")


def add_paths_to_frontmatter(text: str, new: list[str]) -> str:
    """Append ``new`` to the frontmatter ``paths:`` list with a minimal text edit.

    Handles a one-line flow list (``paths: [a, b]``) and a block list (``- a`` lines);
    the rest of the file is left byte for byte. Raises ``ValueError`` without either.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ValueError("the task file has no frontmatter")
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "---"), None)
    key = next((i for i in range(1, end or 0) if re.match(r"paths:(\s|$)", lines[i])), None)
    if end is None or key is None:
        raise ValueError("the frontmatter has no paths: list")
    scalars = [p if _PLAIN_PATH.fullmatch(p) else json.dumps(p) for p in new]
    line = lines[key].rstrip("\r\n")
    newline = lines[key][len(line) :] or "\n"
    flow = re.fullmatch(r"paths:(\s*)\[(.*)\](\s*(?:#.*)?)", line)
    if flow is not None:
        items = flow.group(2).rstrip()
        inner = ", ".join(([items] if items.strip() else []) + scalars)
        lines[key] = f"paths:{flow.group(1)}[{inner}]{flow.group(3)}{newline}"
        return "".join(lines)
    rest = line[len("paths:") :].strip()
    if rest and not rest.startswith("#"):
        raise ValueError("the frontmatter paths: value is not a list")
    prefix: str | None = None
    last = key
    for i in range(key + 1, end):
        item = re.match(r"(\s*-\s+)\S", lines[i])
        if item is not None and prefix is None:
            prefix = item.group(1)
        if item is None and not re.match(r"\s+\S", lines[i]):
            break
        last = i
    if prefix is None:
        raise ValueError("the frontmatter paths: list has no items")
    tail = lines[last] if lines[last].endswith(("\n", "\r")) else lines[last] + newline
    lines[last : last + 1] = [tail, *(f"{prefix}{s}{newline}" for s in scalars)]
    return "".join(lines)
