"""Rules-only triage with no LLM: the bar the model has to clear to justify its cost.

Kind comes from the form the reporter chose; area and blocked from keyword rules; severity from blocked plus a
few keywords. Deliberately simple: if this scores close to the model, the model isn't earning its keep.
"""

import re

FORM_KIND = {"bug": "bug", "feature_request": "feature_request", "model_behavior": "model_behavior",
             "docs": "docs", "integration": "bug"}

# First match wins, so more specific areas come before broad ones.
AREA_RULES: list[tuple[str, str]] = [
    ("third_party_platforms", r"\b(bedrock|vertex|foundry|litellm|gateway|proxy)\b"),
    ("ide_jetbrains", r"\b(jetbrains|intellij|pycharm|webstorm|goland|rider)\b"),
    ("ide_vscode", r"\b(vs ?code|vscode|cursor|windsurf|extension)\b"),
    ("desktop_web_cloud", r"\b(desktop app|claude\.ai/code|cloud session|remote control|web app)\b"),
    ("integrations", r"\b(slack|claude tag|code review)\b"),
    ("git_github", r"\b(git|github|worktree|pull request|pr)\b"),
    ("mcp", r"\b(mcp|elicitation)\b"),
    ("hooks", r"\bhooks?\b"),
    ("plugins_skills", r"\b(plugins?|skills?|slash commands?|marketplace)\b"),
    ("subagents", r"\b(subagents?|sub-agents?|agent teams?|background tasks?)\b"),
    ("permissions_sandbox", r"\b(permissions?|sandbox|auto mode|allowlist|trust)\b"),
    ("context_memory", r"\b(context window|compact(ion)?|claude\.md|memory|resume)\b"),
    ("sdk_headless", r"\b(sdk|headless|-p\b|print mode|non-interactive|ci)\b"),
    ("auth_limits", r"\b(login|log in|logged out|oauth|api key|auth\w*|rate limit|usage limit|billing|plan)\b"),
    ("install_update", r"\b(install\w*|update|upgrade|npm|homebrew|brew|winget|settings\.json)\b"),
    ("performance", r"\b(slow|latency|memory leak|cpu|freez\w*|hang\w*|crash\w*)\b"),
    ("model_behavior", r"\b(ignor\w+ (my )?instructions|hallucinat\w*|lazy|refus\w+)\b"),
    ("builtin_tools", r"\b(bash tool|edit tool|read tool|web ?fetch|web ?search|pdf|image)\b"),
    ("tui", r"\b(terminal|tui|render\w*|scroll\w*|keybinding\w*|paste|status ?line|theme)\b"),
    ("docs", r"\b(docs?|documentation)\b"),
]
_AREA_RX = [(area, re.compile(rx, re.I)) for area, rx in AREA_RULES]

_BLOCKED = re.compile(
    r"\b(can'?t|cannot|unable to|won'?t|doesn'?t|fails? to|stopped working|not working)\b[^.\n]{0,40}"
    r"\b(start|launch|open|run|log ?in|sign ?in|authenticate|install|use)\b"
    r"|\b(data loss|lost (all )?(my )?(work|changes|files)|deleted my|unusable|completely broken)\b",
    re.I)
_WORKAROUND = re.compile(r"\bwork-?around\b", re.I)
_CRITICAL = re.compile(r"\b(data loss|deleted my|security|leak\w* (my )?(key|token|secret)s?)\b", re.I)
_HIGH = re.compile(r"\b(crash\w*|hang\w*|freez\w*|broken|regression)\b", re.I)
_QUESTION = re.compile(r"^(how|is there|can i|what|why)\b|\?\s*$", re.I)
_FEATURE = re.compile(r"\b(support|add|allow|option to|ability to|feature)\b", re.I)


def triage(issue: dict, areas: set[str]) -> dict:
    title, body = issue["title"], issue.get("body") or ""
    text = f"{title}\n{body}"

    kind = FORM_KIND.get(issue.get("form_kind") or "")
    if kind is None:
        kind = "question" if _QUESTION.search(title) else "feature_request" if _FEATURE.search(title) else "bug"

    area = next((a for a, rx in _AREA_RX if a in areas and rx.search(text)), "other")
    blocked = kind in ("bug", "model_behavior") and bool(_BLOCKED.search(text)) and not _WORKAROUND.search(text)

    if _CRITICAL.search(text) or blocked:
        severity = "critical"
    elif kind in ("feature_request", "docs", "question"):
        severity = "medium" if (issue.get("priority") or "").lower().startswith("high") else "low"
    elif _HIGH.search(text):
        severity = "high"
    else:
        severity = "medium"
    return {"kind": kind, "area": area, "blocked": blocked, "severity": severity}
