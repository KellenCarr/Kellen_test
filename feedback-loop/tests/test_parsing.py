from conftest import BEDROCK_BUG, FEATURE, FOUNDRY_TEXT, LOG_DUMP

from loop.changelog import parse_changelog, parse_release_feed
from loop.cohorts import extract_cohort
from loop.issue_form import collapse_long_code_blocks, normalize, parse_issue_form


def test_issue_form_fields_and_no_response():
    f = parse_issue_form(BEDROCK_BUG["body"])
    assert f["Platform"] == "AWS Bedrock"
    assert f["Claude Model"] == ""
    assert parse_issue_form(None) == {}
    assert parse_issue_form("free text, no headings") == {}


def test_cohort_from_form():
    c = extract_cohort(BEDROCK_BUG["title"], BEDROCK_BUG["body"], ["bug"])
    got = (c.form_kind, c.platform, c.platform_source, c.os, c.terminal)
    assert got == ("bug", "bedrock", "form", "macos", "iTerm2")
    assert c.model == "unknown"
    assert c.regression.startswith("Yes")


def test_cohort_text_fallback_and_title_kind():
    c = extract_cohort(FOUNDRY_TEXT["title"], FOUNDRY_TEXT["body"], [])
    assert (c.platform, c.platform_source, c.form_kind) == ("foundry", "text", "unknown")
    f = extract_cohort(FEATURE["title"], FEATURE["body"], [])
    assert f.form_kind == "feature_request"
    assert f.priority.startswith("Medium")


def test_form_platform_wins_over_text():
    body = "### Platform\n\nAnthropic API\n\n### What's Wrong?\n\nI used to be on Bedrock."
    assert extract_cohort("x", body, []).platform == "anthropic_api"


def test_long_code_blocks_are_collapsed_explicitly():
    text, removed = collapse_long_code_blocks(LOG_DUMP["body"])
    assert removed == 102 - 20  # 100 log lines + 2 fence lines, keep 10 + 10
    assert "lines of output omitted" in text
    assert "line 0" in text and "line 99" in text and "line 50" not in text
    short = "```\na\nb\n```"
    assert collapse_long_code_blocks(short) == (short, 0)


def test_normalize_makes_quotes_and_whitespace_comparable():
    assert normalize("It’s  \n broken") == normalize("it's broken")


CHANGELOG = """# Changelog

## 3.0.1

- Fixed a crash when pasting images
- [VSCode] Added a settings page
- Windows: Fixed paths with spaces
- Self-hosted runner: Improved restart handling
- Fix: typo in help
- Bug fixes and reliability improvements

## 3.0.0

- Added plugins
"""


def test_changelog_parsing():
    e = parse_changelog(CHANGELOG)
    assert [(x.version, x.position) for x in e][:2] == [("3.0.1", 0), ("3.0.1", 1)]
    assert [(x.change_type, x.surface) for x in e if x.version == "3.0.1"] == [
        ("fixed", None), ("added", "vscode"), ("fixed", "windows"), ("improved", "self_hosted_runner"),
        ("fixed", None), ("other", None)]
    assert e[-1].version == "3.0.0" and e[-1].change_type == "added"


FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Changelog</title>
  <entry><title>Widget v3.0.1</title><updated>2026-10-23T23:38:43Z</updated></entry>
  <entry><title>Widget v3.0.0</title><updated>2026-10-21T10:00:00Z</updated></entry>
</feed>"""


def test_release_feed():
    r = parse_release_feed(FEED)
    assert set(r) == {"3.0.1", "3.0.0"}
    assert r["3.0.1"].isoformat() == "2026-10-23T23:38:43+00:00"
