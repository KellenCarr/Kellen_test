"""Deterministic cohort extraction from issue-form fields, labels and title prefixes.

Who is affected (platform, OS, terminal) comes from structured fields wherever the form captured
it; text matching is only a fallback. The model is never asked for facts the form already holds.
"""

import re
from dataclasses import dataclass

from loop.issue_form import parse_issue_form

PLATFORMS = {
    "Anthropic API": "anthropic_api",
    "AWS Bedrock": "bedrock",
    "Google Vertex AI": "vertex",
}
_PLATFORM_TEXT = [
    ("foundry", re.compile(r"\b(microsoft|azure)?\s*foundry\b", re.I)),
    ("bedrock", re.compile(r"\bbedrock\b", re.I)),
    ("vertex", re.compile(r"\bvertex\b", re.I)),
]
OS = {
    "macOS": "macos",
    "Windows": "windows",
    "Ubuntu/Debian Linux": "linux",
    "Other Linux": "linux",
}
LABEL_KIND = {
    "bug": "bug",
    "enhancement": "feature_request",
    "model": "model_behavior",
    "documentation": "docs",
    "github-integration": "integration",
}
TITLE_KIND = {
    "[BUG]": "bug",
    "[FEATURE]": "feature_request",
    "[MODEL]": "model_behavior",
    "[DOCS]": "docs",
    "[GitHub integration]": "integration",
}


@dataclass(frozen=True)
class Cohort:
    form_kind: str  # template the reporter chose; 'unknown' when no label or title prefix says
    platform: str
    platform_source: str  # form | text | none
    os: str
    terminal: str
    model: str
    version: str
    regression: str
    priority: str  # feature form only
    impact: str  # model-behavior form only


def _first(fields: dict[str, str], *labels: str) -> str:
    return next((fields[label] for label in labels if fields.get(label)), "")


def extract_cohort(title: str, body: str | None, labels: list[str]) -> Cohort:
    fields = parse_issue_form(body)

    form_kind = next((LABEL_KIND[lb] for lb in labels if lb in LABEL_KIND), "")
    if not form_kind:
        form_kind = next((k for prefix, k in TITLE_KIND.items() if title.startswith(prefix)), "unknown")

    platform, source = PLATFORMS.get(fields.get("Platform", ""), ""), "form"
    if not platform:
        hay = f"{title}\n{body or ''}"
        platform = next((p for p, rx in _PLATFORM_TEXT if rx.search(hay)), "")
        source = "text" if platform else "none"
    if not platform:
        platform = "other" if fields.get("Platform") == "Other" else "unknown"

    return Cohort(
        form_kind=form_kind,
        platform=platform,
        platform_source=source,
        os=OS.get(fields.get("Operating System", ""), fields.get("Operating System", "").lower() or "unknown"),
        terminal=_first(fields, "Terminal/Shell") or "unknown",
        model=_first(fields, "Claude Model") or "unknown",
        version=_first(fields, "Claude Code Version"),
        regression=_first(fields, "Is this a regression?"),
        priority=_first(fields, "Priority"),
        impact=_first(fields, "Impact"),
    )
