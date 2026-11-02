"""Hand labels for the eval: loading, validation, the frozen/tune split, and sampling issues to label.

`evals/labels.jsonl` holds one row per issue: its number, the content_sha it was labeled against, the split,
where the case came from, and the four labeled fields. No issue text is stored; the eval fetches the issue and
skips it if its content changed since labeling. Rows with any label field still null are "unlabeled" and are
ignored by the eval.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from loop import store

LABEL_FIELDS = ("kind", "area", "blocked", "severity")
KINDS = ("bug", "feature_request", "model_behavior", "docs", "question", "other")
SEVERITIES = ("critical", "high", "medium", "low")
SPLITS = ("frozen", "tune")
SOURCES = ("sample", "review", "reader_flag")
DEFAULT_LABELS = Path("evals/labels.jsonl")
DEFAULT_FROZEN_HASH = Path("evals/frozen.sha256")


class LabelError(ValueError):
    pass


def split_for(number: int) -> str:
    """Deterministic, so re-sampling never moves an issue between splits: about 1 in 3 is frozen."""
    return "frozen" if int(hashlib.sha256(f"loop-eval:{number}".encode()).hexdigest(), 16) % 3 == 0 else "tune"


def is_labeled(row: dict) -> bool:
    return all(row.get(f) is not None for f in LABEL_FIELDS)


def validate_row(row: dict, areas: set[str], where: str) -> None:
    for key in ("number", "content_sha", "split", "source"):
        if key not in row:
            raise LabelError(f"{where}: missing '{key}'")
    if row["split"] not in SPLITS:
        raise LabelError(f"{where}: split must be one of {SPLITS}")
    if row["source"] not in SOURCES:
        raise LabelError(f"{where}: source must be one of {SOURCES}")
    if row["source"] != "sample" and row["split"] == "frozen":
        raise LabelError(f"{where}: only sampled cases can be frozen; review and reader cases go to 'tune'")
    if not is_labeled(row):
        return
    if row["kind"] not in KINDS:
        raise LabelError(f"{where}: kind '{row['kind']}' not in {KINDS}")
    if row["area"] not in areas:
        raise LabelError(f"{where}: area '{row['area']}' is not in the taxonomy")
    if not isinstance(row["blocked"], bool):
        raise LabelError(f"{where}: blocked must be true or false")
    if row["severity"] not in SEVERITIES:
        raise LabelError(f"{where}: severity '{row['severity']}' not in {SEVERITIES}")


def load(path: Path, areas: set[str]) -> list[dict]:
    if not path.exists():
        return []
    rows, seen = [], set()
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        where = f"{path}:{i}"
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise LabelError(f"{where}: invalid JSON ({e.msg})") from e
        validate_row(row, areas, where)
        if row["number"] in seen:
            raise LabelError(f"{where}: issue {row['number']} is labeled twice")
        seen.add(row["number"])
        rows.append(row)
    return rows


def append(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def frozen_hash(rows: list[dict]) -> str:
    """Hash of the labeled frozen cases. CI compares it to evals/frozen.sha256, so a change to the frozen set
    (relabeling, adding, removing) has to be committed on purpose and shows up in review."""
    frozen = sorted((r for r in rows if r["split"] == "frozen" and is_labeled(r)), key=lambda r: r["number"])
    canon = [{k: r[k] for k in ("number", "content_sha", *LABEL_FIELDS)} for r in frozen]
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()


@dataclass
class CheckResult:
    total: int
    labeled: dict[str, int]
    unlabeled: int
    hash_ok: bool | None  # None when there is no committed hash yet
    current_hash: str


def check(rows: list[dict], hash_path: Path) -> CheckResult:
    labeled = {s: sum(1 for r in rows if r["split"] == s and is_labeled(r)) for s in SPLITS}
    current = frozen_hash(rows)
    committed = hash_path.read_text().strip() if hash_path.exists() else None
    return CheckResult(len(rows), labeled, sum(not is_labeled(r) for r in rows),
                       None if committed is None else committed == current, current)


def sample(conn, repo: str, n: int, exclude: set[int]) -> list[dict]:
    """Pick n user-filed issues to label, stratified by the form the reporter chose so bugs, feature requests and
    free-text issues all show up. Order inside a stratum is a hash of the number: deterministic, not by date."""
    rows = conn.execute(
        """
        SELECT i.number, i.content_sha, i.html_url, c.form_kind
        FROM issue i JOIN cohort c USING (repo, number)
        WHERE i.repo = ? AND i.body IS NOT NULL AND NOT list_contains(?, coalesce(i.author_association, ''))
        """,
        [repo, list(store.STAFF)],
    ).fetchall()
    strata: dict[str, list] = {}
    for number, sha, url, kind in rows:
        if number not in exclude:
            strata.setdefault(kind, []).append((number, sha, url))
    for items in strata.values():
        items.sort(key=lambda t: hashlib.sha256(str(t[0]).encode()).hexdigest())
    total = sum(len(v) for v in strata.values())
    if total == 0:
        return []
    # Proportional allocation with at least 5 per stratum (or all of it, if smaller), then trim to n.
    quota = {k: min(len(v), max(5, round(n * len(v) / total))) for k, v in strata.items()}
    picked = [t for k, v in strata.items() for t in v[:quota[k]]]
    picked.sort(key=lambda t: hashlib.sha256(str(t[0]).encode()).hexdigest())
    return [{"number": num, "content_sha": sha, "split": split_for(num), "source": "sample",
             **dict.fromkeys(LABEL_FIELDS), "note": "", "url": url} for num, sha, url in picked[:n]]
