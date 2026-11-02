"""The review queue: issues the checks routed to a human, plus reader flags from the published digest.

`loop review-export` writes a JSONL queue with the model's guess and the reader's note for each item and blank
label fields. You fill in the labels and set "confirmed": true. `loop review-import` moves confirmed rows into
evals/labels.jsonl as `tune` cases (never `frozen`, so the frozen set only changes on purpose) and keeps the rest
in the queue. That is the loop: what the pipeline gets wrong becomes eval data.
"""

import json
from pathlib import Path

from loop import labels
from loop.issue_form import parse_issue_form

FLAG_LABEL = "wrong-item"
# Field labels in .github/ISSUE_TEMPLATE/wrong-item.yml, as GitHub renders them into the issue body.
FLAG_FIELDS = {"digest": "Digest week", "item": "Digest row", "issue": "Issue number",
               "field": "What is wrong", "correct": "What it should be"}


def parse_flag(item: dict) -> dict | None:
    """A reader-flag issue -> {number, field, correct, digest, item, flag_url}; None if it names no issue."""
    f = parse_issue_form(item.get("body"))
    raw = f.get(FLAG_FIELDS["issue"], "").strip().lstrip("#")
    if not raw.isdigit():
        return None
    return {"number": int(raw), "field": f.get(FLAG_FIELDS["field"], ""),
            "correct": f.get(FLAG_FIELDS["correct"], ""), "digest": f.get(FLAG_FIELDS["digest"], ""),
            "item": f.get(FLAG_FIELDS["item"], ""), "flag_url": item.get("html_url")}


def needs_review(conn, repo: str) -> list[dict]:
    """Latest triage per issue, for issues whose current content landed in needs_review."""
    cur = conn.execute(
        """
        SELECT i.number, i.content_sha, i.html_url, t.review_reason, t.kind, t.area, t.blocked, t.severity
        FROM issue i
        JOIN triage t ON t.repo = i.repo AND t.number = i.number AND t.content_sha = i.content_sha
        WHERE i.repo = ?
        QUALIFY row_number() OVER (PARTITION BY i.number ORDER BY t.created_at DESC) = 1
        """, [repo])
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    return [r for r in rows if r["review_reason"]]


def build_queue(conn, repo: str, flags: list[dict], labeled: set[int], existing: set[int]) -> list[dict]:
    """New queue rows for needs-review issues and reader flags not yet labeled or already queued."""
    model = {r["number"]: r for r in needs_review(conn, repo)}
    reasons: dict[int, list[str]] = {n: [f"needs_review: {r['review_reason']}"] for n, r in model.items()}
    for fl in flags:
        reasons.setdefault(fl["number"], []).append(
            f"reader flag: {fl['field']} should be '{fl['correct']}' ({fl['flag_url']})")
    out = []
    for number in sorted(reasons):
        if number in labeled or number in existing:
            continue
        info = conn.execute("SELECT content_sha, html_url FROM issue WHERE repo = ? AND number = ?",
                            [repo, number]).fetchone()
        if info is None:
            continue  # flagged an issue we never ingested; nothing to label against
        m = model.get(number) or {}
        out.append({"number": number, "content_sha": info[0], "url": info[1],
                    "source": "review" if number in model else "reader_flag", "why": reasons[number],
                    "model_said": {f: m.get(f) for f in labels.LABEL_FIELDS} if m else None,
                    **dict.fromkeys(labels.LABEL_FIELDS), "note": "", "confirmed": False})
    return out


def read_queue(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_queue(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def import_confirmed(queue: list[dict], labels_path: Path, areas: set[str]) -> tuple[list[dict], list[dict]]:
    """Validate confirmed rows and append them to the labels file as tune cases.
    Returns (imported, remaining queue). Raises LabelError on a confirmed row with a bad label."""
    imported, remaining = [], []
    for i, q in enumerate(queue, 1):
        if not q.get("confirmed"):
            remaining.append(q)
            continue
        row = {"number": q["number"], "content_sha": q["content_sha"], "split": "tune", "source": q["source"],
               **{f: q.get(f) for f in labels.LABEL_FIELDS}, "note": q.get("note", ""), "url": q.get("url")}
        if not labels.is_labeled(row):
            raise labels.LabelError(f"queue row {i} (#{q['number']}) is confirmed but has blank label fields")
        labels.validate_row(row, areas, f"queue row {i}")
        imported.append(row)
    labels.append(labels_path, imported)
    return imported, remaining
