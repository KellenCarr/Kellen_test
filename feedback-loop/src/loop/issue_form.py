"""Parse GitHub issue-form bodies and prepare issue text for the model."""

import hashlib
import re
import unicodedata

NO_RESPONSE = "_No response_"
_HEADING = re.compile(r"^### (.+?)[ \t]*$", re.M)
_FENCE = re.compile(r"^```.*?^```[ \t]*$", re.M | re.S)
_MAX_BLOCK_LINES = 40
_KEEP_LINES = 10


def parse_issue_form(body: str | None) -> dict[str, str]:
    """GitHub renders issue forms as '### <label>' blocks. Returns {label: value}; empty answers -> ''."""
    if not body:
        return {}
    parts = _HEADING.split(body)
    fields = {}
    for label, value in zip(parts[1::2], parts[2::2], strict=True):
        v = value.strip()
        fields[label.strip()] = "" if v == NO_RESPONSE else v
    return fields


def collapse_long_code_blocks(body: str | None) -> tuple[str, int]:
    """Collapse fenced blocks over 40 lines (pasted logs) to their first and last 10 lines.

    Returns (text, lines_removed). The marker left in the text tells the model what was cut, and the
    caller stores lines_removed, so the truncation is explicit rather than silent.
    """
    if not body:
        return "", 0
    removed = 0

    def _collapse(m: re.Match) -> str:
        nonlocal removed
        lines = m.group(0).split("\n")
        if len(lines) <= _MAX_BLOCK_LINES:
            return m.group(0)
        cut = len(lines) - 2 * _KEEP_LINES
        removed += cut
        return "\n".join([*lines[:_KEEP_LINES], f"[... {cut} lines of output omitted ...]", *lines[-_KEEP_LINES:]])

    return _FENCE.sub(_collapse, body), removed


def normalize(text: str) -> str:
    """Normalization shared by the evidence check: NFKC, straight quotes, collapsed whitespace."""
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", t).strip().lower()


def content_sha(title: str, body: str | None) -> str:
    return hashlib.sha256(f"{title}\n{body or ''}".encode()).hexdigest()
