"""Synthetic fixtures shaped like GitHub REST issues rendered from issue forms. No real issue content."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from loop import store

REPO = "acme/widget"


def form(**fields: str) -> str:
    return "\n\n".join(f"### {label}\n\n{value or '_No response_'}" for label, value in fields.items())


def issue(number: int, title: str, body: str, labels=(), created="2026-10-20T10:00:00Z", reactions=0,
          comments=0, association="NONE", pr=False) -> dict:
    item = {
        "number": number, "title": title, "body": body, "state": "open", "state_reason": None,
        "labels": [{"name": lb} for lb in labels], "author_association": association, "comments": comments,
        "reactions": {"total_count": reactions}, "html_url": f"https://github.com/{REPO}/issues/{number}",
        "created_at": created, "updated_at": created, "closed_at": None,
    }
    if pr:
        item["pull_request"] = {"url": "x"}
    return item


BEDROCK_BUG = issue(
    101, "[BUG] Login fails with Bedrock credentials",
    form(**{"What's Wrong?": "Startup exits with an authentication error every time, so I cannot use the tool.",
            "What Should Happen?": "It should start.", "Platform": "AWS Bedrock", "Operating System": "macOS",
            "Terminal/Shell": "iTerm2", "Is this a regression?": "Yes, this worked in a previous version",
            "Claude Code Version": "9.9.9", "Claude Model": ""}),
    labels=["bug"], reactions=4, comments=2)
FEATURE = issue(
    102, "[FEATURE] Per-project default model",
    form(**{"Problem Statement": "I switch models in every project by hand.",
            "Priority": "Medium - Would be very helpful",
            "Feature Category": "Configuration and settings"}),
    labels=["enhancement"], reactions=10)
FOUNDRY_TEXT = issue(103, "Crash on start with Azure Foundry endpoint", "Using Microsoft Foundry, it crashes.")
STAFF = issue(104, "Internal tracking issue", "tracking", association="MEMBER")
PULL = issue(105, "A pull request", "diff", pr=True)
LOG_DUMP = issue(106, "[BUG] Hang", "It hangs.\n\n```\n" + "\n".join(f"line {i}" for i in range(100)) + "\n```",
                 labels=["bug"])


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    now = datetime(2026, 10, 26, tzinfo=UTC)
    for item in (BEDROCK_BUG, FEATURE, FOUNDRY_TEXT, STAFF, LOG_DUMP):
        store.upsert_issue(c, REPO, item, now)
    return c


class FakeMessages:
    """Stands in for client.beta.messages: returns queued responses and records requests."""

    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def fake_client(*responses):
    return SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(responses)))


def response(text: str | None = None, stop_reason="end_turn", category=None, model="claude-opus-5-5"):
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, model=model,
        stop_details=SimpleNamespace(category=category) if stop_reason == "refusal" else None,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=900,
                              cache_creation_input_tokens=0),
        _request_id="req_test",
    )
