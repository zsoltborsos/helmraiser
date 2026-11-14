"""Tests for markdown frontmatter parsing and serialization."""

from helmraiser.utils.markdown import parse_frontmatter, serialize_frontmatter


class TestParseFrontmatter:
    """Test parse_frontmatter."""

    def test_parses_frontmatter_and_body(self):
        content = "---\ntitle: Test\nstatus: draft\n---\n\nThis is the body."
        result = parse_frontmatter(content)
        assert result["frontmatter"]["title"] == "Test"
        assert result["frontmatter"]["status"] == "draft"
        assert "This is the body." in result["body"]

    def test_returns_empty_frontmatter_when_no_delimiter(self):
        content = "Just plain markdown with no frontmatter."
        result = parse_frontmatter(content)
        assert result["frontmatter"] == {}
        assert result["body"] == content

    def test_returns_empty_frontmatter_when_no_closing_delimiter(self):
        content = "---\ntitle: Test\n\nNo closing delimiter"
        result = parse_frontmatter(content)
        assert result["frontmatter"] == {}
        assert result["body"] == content

    def test_handles_malformed_yaml(self):
        content = "---\n[invalid yaml: {\n---\n\nBody"
        result = parse_frontmatter(content)
        assert result["frontmatter"] == {}
        assert "Body" in result["body"]

    def test_error_reports_line_for_bad_yaml(self):
        content = "---\nid: bad\ntitle: a: b\n---\nbody\n"
        result = parse_frontmatter(content)
        assert result["frontmatter"] == {}
        assert result["error"] is not None
        assert "invalid YAML frontmatter" in result["error"]
        assert "at line 3" in result["error"]

    def test_error_for_non_mapping_frontmatter(self):
        content = "---\n- just a list\n---\n\nBody"
        result = parse_frontmatter(content)
        assert result["error"] == "frontmatter must be a mapping"

    def test_error_is_none_for_valid_frontmatter(self):
        content = "---\ntitle: Test\n---\n\nBody"
        result = parse_frontmatter(content)
        assert result["error"] is None

    def test_error_is_none_when_no_frontmatter(self):
        result = parse_frontmatter("Just plain markdown.")
        assert result["error"] is None

    def test_handles_non_dict_yaml(self):
        content = "---\n- just a list\n---\n\nBody"
        result = parse_frontmatter(content)
        assert result["frontmatter"] == {}
        assert "Body" in result["body"]

    def test_dashes_in_body_not_mistaken_for_delimiter(self):
        content = "---\ntitle: Test\n---\n\nSome text\n\n---\n\nMore text"
        result = parse_frontmatter(content)
        assert result["frontmatter"]["title"] == "Test"
        assert "---" in result["body"]
        assert "More text" in result["body"]

    def test_body_is_stripped(self):
        content = "---\ntitle: Test\n---\n\n  Body with spaces  \n\n"
        result = parse_frontmatter(content)
        assert result["body"] == "Body with spaces"


class TestSerializeFrontmatter:
    """Test serialize_frontmatter."""

    def test_produces_valid_markdown(self):
        fm = {"title": "Test", "status": "draft"}
        body = "This is the body."
        result = serialize_frontmatter(fm, body)
        assert result.startswith("---\n")
        assert "title: Test" in result
        assert "status: draft" in result
        assert "This is the body." in result

    def test_round_trips_with_parse(self):
        fm = {"title": "Round Trip", "status": "draft", "priority": "medium"}
        body = "This is the body."
        serialized = serialize_frontmatter(fm, body)
        parsed = parse_frontmatter(serialized)
        assert parsed["frontmatter"]["title"] == "Round Trip"
        assert parsed["frontmatter"]["status"] == "draft"
        assert parsed["frontmatter"]["priority"] == "medium"
        assert "This is the body." in parsed["body"]

    def test_handles_unicode(self):
        fm = {"title": "Tëst wíth ünïcödé"}
        body = "Bodÿ"
        result = serialize_frontmatter(fm, body)
        parsed = parse_frontmatter(result)
        assert parsed["frontmatter"]["title"] == "Tëst wíth ünïcödé"

    def test_empty_body(self):
        fm = {"title": "Test"}
        result = serialize_frontmatter(fm, "")
        parsed = parse_frontmatter(result)
        assert parsed["frontmatter"]["title"] == "Test"

    def test_empty_frontmatter(self):
        result = serialize_frontmatter({}, "Body")
        parsed = parse_frontmatter(result)
        assert parsed["frontmatter"] == {}
        assert "Body" in parsed["body"]
