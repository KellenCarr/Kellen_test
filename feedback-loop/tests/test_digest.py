import json
from datetime import UTC, date, datetime

from conftest import REPO, fake_client, response

from loop import store
from loop.changelog import parse_changelog
from loop.digest import build_digest

NOW = datetime(2026, 10, 26, tzinfo=UTC)


def add_triage(conn, number, **t):
    sha = conn.execute("SELECT content_sha FROM issue WHERE number = ?", [number]).fetchone()[0]
    row = {"repo": REPO, "number": number, "content_sha": sha, "prompt_version": "triage_v1",
           "model": "claude-opus-5-5", "effort": "low", "kind": "bug", "area": "auth_limits", "proposed_area": None,
           "ask": "Fix login", "blocked": False, "blocked_reason": None, "severity": "medium",
           "evidence_quote": "x", "evidence_valid": True, "confidence": "high", "lines_omitted": 0,
           "status": "auto", "review_reason": None, "stop_reason": "end_turn", "refusal_category": None,
           "served_model": "claude-opus-5-5", "created_at": NOW}
    store.insert_row(conn, "triage", {**row, **t})


def seed(conn):
    add_triage(conn, 101, ask="Fix Bedrock login at startup", blocked=True)
    add_triage(conn, 102, kind="feature_request", area="install_update", ask="Support a per-project default model")
    add_triage(conn, 103, ask="Fix Bedrock login at startup", status="needs_review", review_reason="evidence_not_found")
    add_triage(conn, 106, area=None, kind=None, ask=None, status="needs_review", review_reason="refusal")
    store.upsert_changelog(conn, REPO, parse_changelog("## 3.0.1\n\n- Added plugin search\n- Fixed a crash\n"))
    store.upsert_releases(conn, REPO, {"3.0.1": datetime(2026, 10, 23, tzinfo=UTC),
                                       "2.9.0": datetime(2026, 9, 1, tzinfo=UTC)})


def test_digest_without_clustering(conn):
    seed(conn)
    md = build_digest(conn, REPO, date(2026, 10, 26))
    asks = md.split("## What users are asking for")[1].split("##")[0]
    problems = md.split("## What is hurting users")[1].split("##")[0]
    assert "Support a per-project default model" in asks and "| 11 |" in asks  # 1 + 10 reactions
    # 101 and 103 share an exact ask -> one row; 103 awaits review -> dagger
    assert problems.count("Fix Bedrock login at startup") == 1 and "†" in problems
    assert "| 2 |" in problems
    blocked = md.split("## Who is blocked")[1].split("##")[0]
    assert "| auth_limits | bedrock | macos | 1 |" in blocked
    shipped = md.split("## What shipped")[1].split("##")[0]
    assert "1 releases (3.0.1 to 3.0.1)" in shipped and "- Added plugin search" in shipped
    health = md.split("## Pipeline health")[1]
    assert "4 (2 auto, 2 awaiting review)" in health and "refusal 1" in health
    assert "Startup exits" not in md  # no raw issue text in the digest


def test_digest_with_clustering_records_clusters_and_cost(conn):
    seed(conn)
    add_triage(conn, 103, ask="Fix sign-in failure with Bedrock", status="auto")  # different wording, same problem
    clustered = json.dumps({"clusters": [{"label": "Fix Bedrock authentication at startup", "numbers": [101, 103]}]})
    client = fake_client(response(clustered))  # one multi-item group (problems:auth_limits); others are singletons
    md = build_digest(conn, REPO, date(2026, 10, 26), client=client)
    assert "Fix Bedrock authentication at startup" in md
    n = conn.execute("SELECT count(*) FROM cluster WHERE label = 'Fix Bedrock authentication at startup'").fetchone()
    assert n[0] == 1
    assert conn.execute("SELECT count(*) FROM llm_call WHERE purpose = 'cluster'").fetchone()[0] == 1
