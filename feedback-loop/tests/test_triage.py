import json
from datetime import UTC, datetime

import anthropic
import httpx
import pytest
from conftest import REPO, fake_client, response

from loop import store
from loop.cluster import validate
from loop.triage import Taxonomy, check, output_schema, run_triage, system_prompt, user_message

TAX = Taxonomy({"auth_limits": "Login", "install_update": "Install", "performance": "Speed"})
SINCE = datetime(2026, 10, 1, tzinfo=UTC)


def triage_json(**overrides) -> str:
    base = {"kind": "bug", "area": "auth_limits", "proposed_area": None, "ask": "Fix Bedrock login at startup",
            "blocked": True, "blocked_reason": "cannot start", "severity": "critical",
            "evidence_quote": "Startup exits with an authentication error every time", "confidence": "high"}
    return json.dumps({**base, **overrides})


def run(conn, *responses, **kw):
    client = fake_client(*responses)
    s = run_triage(conn, client, REPO, SINCE, effort="low", limit=kw.get("limit"), max_cost=kw.get("max_cost", 10),
                   workers=1, run_id="t", taxonomy=TAX)
    return s, client


def rows(conn):
    cur = conn.execute(
        "SELECT number, status, review_reason, area, blocked, evidence_valid FROM triage ORDER BY number")
    return cur.fetchall()


def test_schema_constrains_area_to_taxonomy():
    s = output_schema(TAX)
    assert s["properties"]["area"]["enum"] == ["auth_limits", "install_update", "performance", "new"]
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"])


def test_prompt_and_message():
    sp = system_prompt(TAX, "widget")
    assert "- auth_limits: Login" in sp and "{areas}" not in sp and "untrusted" in sp
    msg, source, omitted = user_message({"number": 1, "title": "T", "body": "B", "labels": ["bug"],
                                         "platform": "bedrock", "os": "unknown"})
    assert "<form_fields>platform=bedrock</form_fields>" in msg and source == "T\nB" and omitted == 0


def test_request_shape(conn):
    _, client = run(conn, response(triage_json()), limit=1)
    req = client.beta.messages.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["output_config"]["effort"] == "low"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in req and "tool_choice" not in req


def test_staff_issues_skipped_and_auto_status(conn):
    # candidates: 101, 102, 103, 106 (104 is staff-filed); answers in created order (all same time -> by number)
    answers = [response(triage_json(evidence_quote="Startup exits")) for _ in range(4)]
    s, _ = run(conn, *answers)
    assert s.candidates == 4 and s.triaged == 4
    statuses = {n: (st, reason) for n, st, reason, *_ in rows(conn)}
    assert statuses[101] == ("auto", None)
    assert statuses[102] == ("needs_review", "evidence_not_found")  # quote isn't in issue 102's text
    assert 104 not in statuses


def test_refusal_and_max_tokens_route_to_review(conn):
    s, _ = run(conn, response(stop_reason="refusal", category="bio"), response(stop_reason="max_tokens"), limit=2)
    r = rows(conn)
    assert [(x[1], x[2]) for x in r] == [("needs_review", "refusal"), ("needs_review", "max_tokens")]
    cat = conn.execute("SELECT refusal_category FROM triage WHERE review_reason='refusal'").fetchone()[0]
    assert cat == "bio" and s.needs_review == 2


def test_transient_error_leaves_issue_untriaged_for_next_run(conn):
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    s, _ = run(conn, err, limit=1)
    assert s.failed == 1 and rows(conn) == []
    logged = conn.execute("SELECT error FROM llm_call").fetchone()[0]
    assert logged.startswith("APIConnectionError")
    s2, _ = run(conn, response(triage_json()), limit=1)  # retried on the next run
    assert s2.triaged == 1


def test_bad_request_aborts_the_run(conn):
    req = httpx.Request("POST", "https://api.anthropic.com")
    err = anthropic.BadRequestError("bad", response=httpx.Response(400, request=req), body=None)
    with pytest.raises(anthropic.BadRequestError):
        run(conn, err, limit=1)


def test_budget_stops_submission(conn):
    # each fake call costs ~ (1000*4 + 200*20 + 900*0.2)/1e6 = $0.00818
    s, _ = run(conn, *[response(triage_json()) for _ in range(4)], max_cost=0.01)
    assert s.stopped_on_budget and s.triaged == 2


def test_rerun_is_idempotent_and_retriages_edited_issues(conn):
    run(conn, *[response(triage_json()) for _ in range(4)])
    s, _ = run(conn)
    assert s.candidates == 0
    item = conn.execute("SELECT number FROM issue WHERE number = 101").fetchone()
    assert item
    from conftest import BEDROCK_BUG
    store.upsert_issue(conn, REPO, {**BEDROCK_BUG, "body": BEDROCK_BUG["body"] + "\nedit"}, datetime.now(UTC))
    s, _ = run(conn, response(triage_json()))
    assert s.candidates == 1


def test_check_rules():
    src = "Login fails. Startup exits with an authentication error."
    assert check(json.loads(triage_json(evidence_quote="startup  exits")), TAX, src)[1] is None
    new_area = json.loads(triage_json(area="new", proposed_area="x", evidence_quote="Login fails"))
    assert check(new_area, TAX, src)[1] == "new_area"
    assert check(json.loads(triage_json(evidence_quote="")), TAX, src)[1] == "evidence_not_found"
    assert check({"kind": "bug"}, TAX, src)[1] == "schema"
    assert check(None, TAX, src)[1] == "no_output"


def test_cluster_validation_keeps_every_number_once():
    items = {1: "a", 2: "b", 3: "c"}
    raw = {"clusters": [{"label": "A", "numbers": [1, 2, 99]}, {"label": "B", "numbers": [2]}]}
    assert validate(raw, items) == [("A", [1, 2]), ("c", [3])]
    assert validate(None, items) == [("a", [1]), ("b", [2]), ("c", [3])]
