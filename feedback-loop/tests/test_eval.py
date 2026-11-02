import json
from pathlib import Path

import pytest
from conftest import BEDROCK_BUG, FEATURE, REPO, fake_client, response

from loop import baseline, evaluate, labels
from loop.evaluate import Case, Run
from loop.triage import Taxonomy

TAX = Taxonomy({"auth_limits": "Login", "install_update": "Install", "third_party_platforms": "Bedrock",
                "other": "Anything else"})
GATE = evaluate.load_gate(Path(__file__).resolve().parents[1] / "evals" / "gate.yaml")


def label(conn, number, split="frozen", **f):
    sha = conn.execute("SELECT content_sha FROM issue WHERE number = ?", [number]).fetchone()[0]
    base = {"kind": "bug", "area": "auth_limits", "blocked": True, "severity": "critical"}
    return {"number": number, "content_sha": sha, "split": split, "source": "sample", **base, **f, "note": ""}


def triage_json(**overrides) -> str:
    base = {"kind": "bug", "area": "auth_limits", "proposed_area": None, "ask": "Fix Bedrock login at startup",
            "blocked": True, "blocked_reason": "cannot start", "severity": "critical",
            "evidence_quote": "Startup exits with an authentication error every time", "confidence": "high"}
    return json.dumps({**base, **overrides})


FEATURE_JSON = dict(kind="feature_request", area="install_update", blocked=False, blocked_reason=None,
                    severity="low", evidence_quote="I switch models in every project by hand.")


def test_load_cases_uses_db_and_skips_edited_or_unlabeled(conn):
    rows = [label(conn, 101), {**label(conn, 102), "content_sha": "stale"},
            {**label(conn, 103), "kind": None}, label(conn, 106, split="tune")]
    cases, skipped = evaluate.load_cases(rows, "frozen", REPO, conn=conn)
    assert [c.issue["number"] for c in cases] == [101]
    assert cases[0].issue["platform"] == "bedrock"
    assert skipped == {"edited_since_labeling": 1, "unlabeled": 1}


def test_load_cases_falls_back_to_github_when_not_in_db():
    class FakeGH:
        def get_issue(self, repo, number):
            assert (repo, number) == (REPO, 102)
            return FEATURE

    row = {"number": 102, "content_sha": evaluate.issue_record(FEATURE)["content_sha"], "split": "frozen",
           "source": "sample", "kind": "feature_request", "area": "install_update", "blocked": False,
           "severity": "low"}
    cases, skipped = evaluate.load_cases([row], "frozen", REPO, conn=None, gh=FakeGH())
    assert [c.issue["number"] for c in cases] == [102] and not skipped


def case(number, **lab):
    base = {"kind": "bug", "area": "auth_limits", "blocked": True, "severity": "critical"}
    return Case({"number": number, **base, **lab}, {"number": number})


def pred(**p):
    base = {"kind": "bug", "area": "auth_limits", "blocked": True, "severity": "critical"}
    return {**base, **p}


def test_score_counts_no_output_as_wrong_and_reports_misses():
    cases = [case(1), case(2, kind="feature_request", area="install_update", blocked=False, severity="low")]
    runs = {
        1: [Run(pred(), "auto", True, cost_usd=0.01), Run(None, refused=True, cost_usd=0.0)],
        2: [Run(pred(kind="feature_request", area="install_update", blocked=True, severity="low"), "auto", True,
                cost_usd=0.01)] * 2,
    }
    m, misses = evaluate.score(cases, runs)
    assert m["runs"] == 4 and m["kind_acc"] == 0.75 and m["area_acc"] == 0.75
    assert m["blocked_recall"] == 0.5          # case 1: one run said blocked, one refused
    assert m["blocked_precision"] == 0.3333    # 1 of 3 blocked=true predictions is right
    assert m["refusal_rate"] == 0.25 and m["failure_rate"] == 0.25
    assert m["consistency"] == 0.5             # case 2's runs agree, case 1's don't
    assert m["cost_per_issue"] == 0.0075
    assert {"number": 2, "field": "blocked", "label": False, "predicted": True, "runs_agreeing": "2/2"} in misses


GOOD = {"cases": 40, "kind_acc": 0.9, "area_acc": 0.8, "severity_acc": 0.7, "blocked_recall": 0.95,
        "evidence_valid_rate": 0.99, "refusal_rate": 0.0, "failure_rate": 0.0, "cost_per_issue": 0.02}
BASE = {"kind_acc": 0.7, "area_acc": 0.5}


def test_gate_passes_a_good_run():
    assert evaluate.gate(GOOD, BASE, GATE, complete=True, accepted=None, split="frozen") == []


@pytest.mark.parametrize(("change", "complete", "expected"), [
    ({}, False, "incomplete"),
    ({"cases": 10}, True, "only 10 cases"),
    ({"blocked_recall": 0.8}, True, "blocked_recall 0.8 is below the floor"),
    ({"blocked_recall": None}, True, "blocked_recall None is below the floor"),
    ({"refusal_rate": 0.05}, True, "refusal_rate 0.05 is above the ceiling"),
    ({"area_acc": 0.55}, True, "area_acc 0.55 does not beat the rules baseline"),
])
def test_gate_failures(change, complete, expected):
    fails = evaluate.gate({**GOOD, **change}, BASE, GATE, complete=complete, accepted=None, split="frozen")
    assert any(expected in f for f in fails), fails


def test_gate_regression_against_accepted_only_for_same_split():
    accepted = {"split": "frozen", "metrics": {**GOOD, "area_acc": 0.9, "cost_per_issue": 0.01}}
    fails = evaluate.gate(GOOD, BASE, GATE, complete=True, accepted=accepted, split="frozen")
    assert any("area_acc regressed" in f for f in fails)
    assert any("cost_per_issue rose" in f for f in fails)
    assert evaluate.gate(GOOD, BASE, GATE, complete=True, accepted=accepted, split="tune") == []


def test_run_eval_end_to_end(conn):
    rows = [label(conn, 101), label(conn, 102, kind="feature_request", area="install_update", blocked=False,
                                    severity="low")]
    # repeat-major order: 101, 102, 101, 102
    client = fake_client(response(triage_json()), response(triage_json(**FEATURE_JSON)),
                         response(triage_json()), response(triage_json(**FEATURE_JSON)))
    report = evaluate.run_eval(rows, repo=REPO, split="frozen", client=client, taxonomy=TAX, effort="low",
                               repeats=2, max_cost=10, workers=1, gate_cfg=GATE, accepted=None, conn=conn)
    m = report["metrics"]
    assert report["complete"] and m["cases"] == 2 and m["runs"] == 4
    assert m["kind_acc"] == 1.0 and m["blocked_recall"] == 1.0 and m["evidence_valid_rate"] == 1.0
    assert report["baseline"]["cases"] == 2
    assert any("only 2 cases" in f for f in report["gate"]["failures"])  # below min_cases
    md = evaluate.render_markdown(report)
    assert "gate FAILED" in md and "| Kind accuracy | 1.0000 |" in md
    assert evaluate.accepted_record(report)["metrics"] == m


def test_run_eval_budget_stop_is_incomplete(conn):
    rows = [label(conn, 101), label(conn, 102)]
    client = fake_client(response(triage_json()))
    report = evaluate.run_eval(rows, repo=REPO, split="frozen", client=client, taxonomy=TAX, effort="low",
                               repeats=3, max_cost=0.0001, workers=1, gate_cfg=GATE, accepted=None, conn=conn)
    assert report["stopped_on_budget"] and not report["complete"]
    assert any("incomplete" in f for f in report["gate"]["failures"])


def test_baseline_rules():
    areas = set(TAX.areas)
    issue = evaluate.issue_record(BEDROCK_BUG)
    assert baseline.triage(issue, areas) == {"kind": "bug", "area": "third_party_platforms", "blocked": True,
                                             "severity": "critical"}
    feat = baseline.triage(evaluate.issue_record(FEATURE), areas)
    assert feat["kind"] == "feature_request" and feat["blocked"] is False and feat["severity"] == "low"
    q = baseline.triage({"title": "How do I change the model?", "body": "", "form_kind": "unknown"}, areas)
    assert q["kind"] == "question" and q["area"] == "other"


def test_labels_loader_rejects_bad_rows(tmp_path):
    areas = set(TAX.areas)
    p = tmp_path / "labels.jsonl"
    good = {"number": 1, "content_sha": "a", "split": "frozen", "source": "sample", "kind": "bug",
            "area": "auth_limits", "blocked": False, "severity": "low"}
    for bad, msg in [({**good, "area": "nope"}, "not in the taxonomy"),
                     ({**good, "source": "review"}, "only sampled cases can be frozen"),
                     ({**good, "blocked": "no"}, "blocked must be true or false")]:
        p.write_text(json.dumps(bad) + "\n")
        with pytest.raises(labels.LabelError, match=msg):
            labels.load(p, areas)
    p.write_text(json.dumps(good) + "\n" + json.dumps(good) + "\n")
    with pytest.raises(labels.LabelError, match="labeled twice"):
        labels.load(p, areas)
