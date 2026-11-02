"""DuckDB storage. All SQL is parameterized; all writes are upserts on natural keys."""

from datetime import datetime
from importlib.resources import files
from pathlib import Path

import duckdb

from loop.changelog import ChangelogEntry
from loop.cohorts import extract_cohort
from loop.issue_form import content_sha

STAFF = ("OWNER", "MEMBER", "COLLABORATOR")


def connect(path: str | Path) -> duckdb.DuckDBPyConnection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    conn.execute(files("loop").joinpath("schema.sql").read_text())
    return conn


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def upsert_issue(conn, repo: str, item: dict, fetched_at: datetime) -> None:
    labels = [lb["name"] if isinstance(lb, dict) else lb for lb in item.get("labels", [])]
    title, body = item["title"], item.get("body")
    conn.execute(
        "INSERT OR REPLACE INTO issue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [repo, item["number"], title, body, content_sha(title, body), item["state"], item.get("state_reason"),
         labels, item.get("author_association"), item.get("comments", 0),
         (item.get("reactions") or {}).get("total_count", 0), item["html_url"],
         _ts(item["created_at"]), _ts(item["updated_at"]), _ts(item.get("closed_at")), fetched_at],
    )
    c = extract_cohort(title, body, labels)
    conn.execute(
        "INSERT OR REPLACE INTO cohort VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [repo, item["number"], c.form_kind, c.platform, c.platform_source, c.os, c.terminal, c.model,
         c.version, c.regression, c.priority, c.impact],
    )


def upsert_changelog(conn, repo: str, entries: list[ChangelogEntry]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO changelog_entry VALUES (?, ?, ?, ?, ?, ?)",
        [[repo, e.version, e.position, e.change_type, e.surface, e.text] for e in entries],
    )


def upsert_releases(conn, repo: str, releases: dict[str, datetime]) -> None:
    conn.executemany("INSERT OR REPLACE INTO release VALUES (?, ?, ?)",
                     [[repo, v, t] for v, t in releases.items()])


def issues_to_triage(conn, repo: str, since: datetime, prompt_version: str, model: str, effort: str,
                     limit: int | None) -> list[dict]:
    """User-filed issues created since `since` with no triage row for their current content."""
    rows = conn.execute(
        """
        SELECT i.number, i.title, i.body, i.content_sha, i.labels,
               c.form_kind, c.platform, c.os, c.terminal, c.model, c.version, c.regression
        FROM issue i JOIN cohort c USING (repo, number)
        WHERE i.repo = ? AND i.created_at >= ?
          AND NOT list_contains(?, coalesce(i.author_association, ''))
          AND NOT EXISTS (
            SELECT 1 FROM triage t
            WHERE t.repo = i.repo AND t.number = i.number AND t.content_sha = i.content_sha
              AND t.prompt_version = ? AND t.model = ? AND t.effort = ?)
        ORDER BY i.created_at
        LIMIT ?
        """,
        [repo, since, list(STAFF), prompt_version, model, effort, limit if limit is not None else 2**31 - 1],
    )
    cols = [d[0] for d in rows.description]
    return [dict(zip(cols, r, strict=True)) for r in rows.fetchall()]


def prune_bodies(conn, repo: str, before: datetime) -> int:
    """Drop the body text of issues created before `before` whose current content is already triaged.

    The database is carried between scheduled runs as a build artifact; pruning keeps that copy free of old issue
    text. Titles, hashes, cohorts and triage rows stay, so edit detection (content_sha) and the digest still work.
    An issue edited later is re-fetched with its new body on the next ingest and re-triaged as usual.
    """
    sql_where = """
        WHERE i.repo = ? AND i.created_at < ? AND i.body IS NOT NULL
          AND EXISTS (SELECT 1 FROM triage t WHERE t.repo = i.repo AND t.number = i.number
                      AND t.content_sha = i.content_sha)"""
    n = conn.execute(f"SELECT count(*) FROM issue i {sql_where}", [repo, before]).fetchone()[0]
    conn.execute(f"UPDATE issue i SET body = NULL {sql_where}", [repo, before])
    return n


_KEYED = {"triage": "INSERT OR REPLACE", "cluster": "INSERT OR REPLACE", "llm_call": "INSERT"}


def insert_row(conn, table: str, row: dict) -> None:
    """Insert one row; tables with a natural key are upserts. Table and column names are code constants."""
    verb = _KEYED[table]
    cols = ", ".join(row)
    conn.execute(f"{verb} INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
