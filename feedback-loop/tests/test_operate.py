"""Labels and sampling, publishing, pruning, the review queue, and the CLI's label check."""

import json
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime

import pytest
from conftest import REPO, form, issue

from loop import labels, review, store
from loop.cli import main
from loop.digest import build_digest
from loop.publish import publish

AREAS = {"auth_limits", "install_update", "other"}
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


def labeled(number, split="frozen", **f):
    return {"number": number, "content_sha": f"sha{number}", "split": split, "source": "sample", "kind": "bug",
            "area": "auth_limits", "blocked": False, "severity": "low", **f}


def test_split_is_deterministic_and_about_a_third_frozen():
    splits = [labels.split_for(n) for n in range(1, 601)]
    assert splits == [labels.split_for(n) for n in range(1, 601)]
    assert 160 < splits.count("frozen") < 240


def test_frozen_hash_tracks_only_labeled_frozen_rows(tmp_path):
    rows = [labeled(1), labeled(2, split="tune"), {**labeled(3), "kind": None}]
    h = labels.frozen_hash(rows)
    assert labels.frozen_hash([*rows[:1], {**rows[1], "severity": "high"}, rows[2]]) == h  # tune change
    assert labels.frozen_hash([{**rows[0], "severity": "high"}, *rows[1:]]) != h          # frozen relabel
    hp = tmp_path / "frozen.sha256"
    assert labels.check(rows, hp).hash_ok is None
    hp.write_text(h + "\n")
    r = labels.check(rows, hp)
    assert r.hash_ok is True and r.labeled == {"frozen": 1, "tune": 1} and r.unlabeled == 1


def test_sample_is_stratified_and_skips_staff_and_labeled(conn):
    for n in range(200, 230):
        store.upsert_issue(conn, REPO, issue(n, f"[FEATURE] idea {n}", form(**{"Problem Statement": "x"}),
                                             labels=["enhancement"]), NOW)
    rows = labels.sample(conn, REPO, 12, exclude={101})
    numbers = {r["number"] for r in rows}
    assert len(rows) == 12 and 101 not in numbers and 104 not in numbers  # 104 is staff-filed
    assert numbers & {102, 103, 106}  # small strata still get picked (at least 5 or all of them)
    assert all(r["kind"] is None and r["split"] == labels.split_for(r["number"]) for r in rows)
    assert rows == labels.sample(conn, REPO, 12, exclude={101})


def test_publish_builds_index_and_feed(tmp_path):
    site = tmp_path / "docs"
    publish("# Loop digest: a/b\n\nweek one", site, date(2026, 10, 26), site_url="https://x.github.io/loop/")
    publish("# Loop digest: a/b\n\nweek two", site, date(2026, 11, 2), site_url="https://x.github.io/loop/")
    publish("# Loop digest: a/b\n\nweek two, re-run", site, date(2026, 11, 2), site_url="https://x.github.io/loop/")
    index = (site / "index.md").read_text()
    assert index.index("2026-11-02") < index.index("2026-10-26")
    assert "week two, re-run" in (site / "digests" / "2026-11-02.md").read_text()
    items = ET.fromstring((site / "feed.xml").read_text()).findall("./channel/item")
    assert [i.findtext("link") for i in items] == ["https://x.github.io/loop/digests/2026-11-02.html",
                                                    "https://x.github.io/loop/digests/2026-10-26.html"]


def test_prune_drops_old_triaged_bodies_only(conn):
    add_triage(conn, 101)
    store.upsert_issue(conn, REPO, issue(300, "Recent", "recent body", created="2026-10-25T00:00:00Z"), NOW)
    add_triage(conn, 300)
    n = store.prune_bodies(conn, REPO, datetime(2026, 10, 24, tzinfo=UTC))
    bodies = dict(conn.execute("SELECT number, body FROM issue WHERE number IN (101, 102, 300)").fetchall())
    assert n == 1 and bodies[101] is None          # old and triaged
    assert bodies[102] is not None                 # old but never triaged
    assert bodies[300] == "recent body"            # triaged but recent


FLAG = {"html_url": "https://github.com/me/loop/issues/7", "body": form(**{
    "Digest week": "2026-10-26", "Digest row": "Fix login", "Issue number": "#102",
    "What is wrong": "Blocked (the user is or isn't actually blocked)", "What it should be": "not blocked"})}


def test_review_queue_round_trip(conn, tmp_path):
    add_triage(conn, 101, status="needs_review", review_reason="evidence_not_found", blocked=True)
    add_triage(conn, 103)
    flag = review.parse_flag(FLAG)
    assert flag["number"] == 102 and flag["correct"] == "not blocked"
    assert review.parse_flag({"body": form(**{"Issue number": "soon"})}) is None

    queue = review.build_queue(conn, REPO, [flag], labeled={103}, existing=set())
    assert [(q["number"], q["source"]) for q in queue] == [(101, "review"), (102, "reader_flag")]
    assert queue[0]["model_said"]["blocked"] is True and "reader flag" in queue[1]["why"][0]

    queue[0].update(kind="bug", area="auth_limits", blocked=False, severity="medium", confirmed=True)
    lp = tmp_path / "labels.jsonl"
    imported, remaining = review.import_confirmed(queue, lp, AREAS)
    assert [r["number"] for r in imported] == [101] and [q["number"] for q in remaining] == [102]
    assert labels.load(lp, AREAS)[0]["split"] == "tune"

    remaining[0]["confirmed"] = True  # confirmed but blank
    with pytest.raises(labels.LabelError, match="blank label fields"):
        review.import_confirmed(remaining, lp, AREAS)


def test_digest_flag_links_and_untriaged_count(conn):
    add_triage(conn, 101, blocked=True)
    md = build_digest(conn, REPO, date(2026, 10, 26), feedback_repo="me/loop")
    assert "https://github.com/me/loop/issues/new?template=wrong-item.yml&digest=2026-10-26" in md
    assert "**not triaged: 3**" in md  # 102, 103, 106 were opened in the window but never triaged
    assert "Taxonomy fit: 0 of 1" in md


def test_labels_check_cli_fails_on_unrecorded_frozen_change(tmp_path):
    lp, hp = tmp_path / "labels.jsonl", tmp_path / "frozen.sha256"
    tax = tmp_path / "taxonomy.yaml"
    tax.write_text("version: 1\nareas:\n  - id: auth_limits\n    covers: Login\n")
    lp.write_text(json.dumps(labeled(1)) + "\n")
    args = ["labels-check", "--labels", str(lp), "--frozen-hash", str(hp), "--taxonomy", str(tax)]
    main([*args, "--update"])
    main(args)  # unchanged: passes
    lp.write_text(json.dumps({**labeled(1), "severity": "high"}) + "\n")
    with pytest.raises(SystemExit, match="frozen labels changed"):
        main(args)
