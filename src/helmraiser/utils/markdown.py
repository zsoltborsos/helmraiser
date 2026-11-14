"""Parse and serialize markdown with YAML frontmatter."""

import yaml

# libyaml parses ~9x faster; status parses every task snapshot on each call.
_SafeLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def parse_frontmatter(content: str) -> dict:
    """Parse markdown with YAML frontmatter.

    Uses a line-oriented parser so that ``---`` separators that appear
    inside the body (e.g. in Markdown horizontal rules) are not mistaken
    for the closing frontmatter delimiter.

    Args:
        content: Markdown content

    Returns:
        Dict with ``frontmatter`` (dict), ``body`` (str) and ``error`` (str | None).
        If no frontmatter is found, returns empty frontmatter and the full content as body.
        ``error`` is set when the frontmatter block exists but is not valid YAML or is
        not a mapping.
    """
    if not content.startswith("---"):
        return {"frontmatter": {}, "body": content, "error": None}

    try:
        lines = content.splitlines(keepends=True)
        # Skip the opening "---" line (index 0) and find the next "---\n"
        closing = None
        for i, line in enumerate(lines[1:], start=1):
            if line.rstrip("\r\n") == "---":
                closing = i
                break

        if closing is None:
            return {"frontmatter": {}, "body": content, "error": None}

        yaml_text = "".join(lines[1:closing])
        body_text = "".join(lines[closing + 1 :]).strip()

        try:
            frontmatter = yaml.load(yaml_text, Loader=_SafeLoader)
        except yaml.YAMLError as exc:
            problem = getattr(exc, "problem", None) or str(exc)
            mark = getattr(exc, "problem_mark", None)
            if mark is not None:
                # mark.line is 0-based within yaml_text; the opening "---" and the
                # 1-based file count add 2.
                error = f"invalid YAML frontmatter: {problem} at line {mark.line + 2}"
            else:
                error = f"invalid YAML frontmatter: {problem}"
            return {"frontmatter": {}, "body": content, "error": error}
        if not isinstance(frontmatter, dict):
            return {
                "frontmatter": {},
                "body": body_text,
                "error": "frontmatter must be a mapping",
            }

        return {"frontmatter": frontmatter, "body": body_text, "error": None}
    except Exception:
        return {"frontmatter": {}, "body": content, "error": None}


def serialize_frontmatter(frontmatter: dict, body: str) -> str:
    """Serialize frontmatter and body to markdown.

    Args:
        frontmatter: Frontmatter dict
        body: Body text

    Returns:
        Markdown string with YAML frontmatter
    """
    yaml_str = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True)
    return f"---\n{yaml_str}---\n\n{body}"
