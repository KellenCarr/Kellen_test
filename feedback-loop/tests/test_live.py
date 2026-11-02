"""Live smoke test against the Claude API. Run with: pytest -m live (needs Anthropic credentials)."""

import pytest
from conftest import BEDROCK_BUG, FEATURE

from loop.cohorts import extract_cohort
from loop.issue_form import content_sha
from loop.llm import make_client
from loop.triage import Taxonomy, triage_issue

pytestmark = pytest.mark.live


def as_candidate(item: dict) -> dict:
    labels = [lb["name"] for lb in item["labels"]]
    c = extract_cohort(item["title"], item["body"], labels)
    return {"number": item["number"], "title": item["title"], "body": item["body"], "labels": labels,
            "content_sha": content_sha(item["title"], item["body"]), "platform": c.platform, "os": c.os,
            "terminal": c.terminal, "model": c.model, "regression": c.regression}


def test_triage_two_issues_schema_valid_and_cached():
    client, tax = make_client(), Taxonomy.load()
    first_row, first = triage_issue(client, as_candidate(BEDROCK_BUG), tax, "claude-code", "low")
    second_row, second = triage_issue(client, as_candidate(FEATURE), tax, "claude-code", "low")
    for row, r in ((first_row, first), (second_row, second)):
        assert row is not None, r.error
        assert row["stop_reason"] == "end_turn" and row["kind"] is not None, row
    assert first_row["blocked"] is True and first_row["area"] in tax.areas
    assert second_row["kind"] == "feature_request"
    # identical system prompt -> the second call reads it from cache
    assert second.usage["cache_read_tokens"] > 0, second.usage
