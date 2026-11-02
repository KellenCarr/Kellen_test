"""Eval: run triage on hand-labeled issues, score it against the labels and a rules-only baseline, and gate.

Every case runs `repeats` times because sampling can't be pinned on this model (temperature is not accepted),
so a single run can't tell a real change from noise. Scores are averaged over runs; `consistency` reports how
often all runs of a case agree.

The gate fails a change when the eval is incomplete, a floor is broken, the model doesn't clear the baseline by
the configured margin, or a gated metric regresses past tolerance against the last accepted report.
"""

import json
import os
import statistics
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from loop import baseline, labels
from loop.cohorts import extract_cohort
from loop.github import GitHubError
from loop.issue_form import content_sha
from loop.llm import MODEL
from loop.triage import PROMPT_VERSION, Taxonomy, triage_issue

FIELDS = labels.LABEL_FIELDS
DEFAULT_GATE = Path("evals/gate.yaml")
DEFAULT_ACCEPTED = Path("evals/accepted.json")


@dataclass
class Case:
    label: dict
    issue: dict  # same shape as store.issues_to_triage rows


@dataclass
class Run:
    pred: dict | None  # the four labeled fields; None when the call produced no usable output
    status: str | None = None
    evidence_valid: bool | None = None
    refused: bool = False
    cost_usd: float = 0.0
    latency_ms: int = 0


@dataclass
class ModelRuns:
    runs: dict[int, list[Run]] = field(default_factory=dict)
    cost_usd: float = 0.0
    stopped_on_budget: bool = False


def issue_record(item: dict) -> dict:
    """A GitHub REST issue -> the dict triage takes (same fields as store.issues_to_triage)."""
    item_labels = [lb["name"] if isinstance(lb, dict) else lb for lb in item.get("labels", [])]
    c = extract_cohort(item["title"], item.get("body"), item_labels)
    return {"number": item["number"], "title": item["title"], "body": item.get("body"),
            "content_sha": content_sha(item["title"], item.get("body")), "labels": item_labels,
            "form_kind": c.form_kind, "platform": c.platform, "os": c.os, "terminal": c.terminal,
            "model": c.model, "version": c.version, "regression": c.regression, "priority": c.priority}


def issue_from_db(conn, repo: str, number: int) -> dict | None:
    cur = conn.execute(
        """SELECT i.number, i.title, i.body, i.content_sha, i.labels, c.form_kind, c.platform, c.os, c.terminal,
                  c.model, c.version, c.regression, c.priority
           FROM issue i JOIN cohort c USING (repo, number) WHERE i.repo = ? AND i.number = ?""", [repo, number])
    row = cur.fetchone()
    if row is None or row[2] is None:  # pruned bodies can't be triaged
        return None
    return dict(zip([d[0] for d in cur.description], row, strict=True))


def load_cases(rows: list[dict], split: str, repo: str, conn=None, gh=None) -> tuple[list[Case], Counter]:
    """Labeled rows in `split` -> cases. Uses the local DB when it holds the labeled content, else GitHub.
    Issues edited since labeling are skipped (the label may no longer fit), and counted."""
    cases, skipped = [], Counter()
    for row in rows:
        if split != "all" and row["split"] != split:
            continue
        if not labels.is_labeled(row):
            skipped["unlabeled"] += 1
            continue
        issue = issue_from_db(conn, repo, row["number"]) if conn is not None else None
        if (issue is None or issue["content_sha"] != row["content_sha"]) and gh is not None:
            try:
                issue = issue_record(gh.get_issue(repo, row["number"]))
            except GitHubError:
                skipped["fetch_failed"] += 1
                continue
        if issue is None:
            skipped["missing"] += 1
        elif issue["content_sha"] != row["content_sha"]:
            skipped["edited_since_labeling"] += 1
        else:
            cases.append(Case(row, issue))
    return cases, skipped


def run_model(cases: list[Case], client, taxonomy: Taxonomy, product: str, *, effort: str, repeats: int,
              max_cost: float, workers: int) -> ModelRuns:
    """All repeats of all cases, at most `workers` in flight. Jobs are ordered repeat-major, so a budget stop
    leaves whole earlier repeats rather than some cases fully run and others not at all."""
    out = ModelRuns(runs={c.issue["number"]: [] for c in cases})
    jobs = iter([c for _ in range(repeats) for c in cases])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        in_flight = {}

        def submit_next() -> None:
            if (c := next(jobs, None)) is not None:
                in_flight[pool.submit(triage_issue, client, c.issue, taxonomy, product, effort)] = c

        for _ in range(workers):
            submit_next()
        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for fut in done:
                case = in_flight.pop(fut)
                row, r = fut.result()  # FATAL API errors propagate and end the eval
                out.cost_usd += r.cost_usd
                if row is None or row["kind"] is None:
                    run = Run(None, refused=r.stop_reason == "refusal", cost_usd=r.cost_usd, latency_ms=r.latency_ms)
                else:
                    run = Run({f: row[f] for f in FIELDS}, row["status"], row["evidence_valid"], False,
                              r.cost_usd, r.latency_ms)
                out.runs[case.issue["number"]].append(run)
                if out.cost_usd >= max_cost:
                    out.stopped_on_budget = True
                else:
                    submit_next()
    return out


def run_baseline(cases: list[Case], areas: set[str]) -> dict[int, list[Run]]:
    return {c.issue["number"]: [Run(baseline.triage(c.issue, areas), status="auto")] for c in cases}


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def score(cases: list[Case], runs: dict[int, list[Run]]) -> tuple[dict, list[dict]]:
    """Returns (metrics, misses). A run with no usable output counts as wrong on every field."""
    all_runs = [(c, r) for c in cases for r in runs.get(c.issue["number"], [])]
    n = len(all_runs)
    m: dict = {"cases": len(cases), "runs": n}
    for f in FIELDS:
        m[f"{f}_acc"] = _rate(sum(r.pred is not None and r.pred[f] == c.label[f] for c, r in all_runs), n)
    pos = [(c, r) for c, r in all_runs if c.label["blocked"]]
    flagged = [(c, r) for c, r in all_runs if r.pred is not None and r.pred["blocked"]]
    m["blocked_recall"] = _rate(sum(r.pred is not None and r.pred["blocked"] for _, r in pos), len(pos))
    m["blocked_precision"] = _rate(sum(c.label["blocked"] for c, _ in flagged), len(flagged))
    m["evidence_valid_rate"] = _rate(sum(r.evidence_valid is True for _, r in all_runs), n)
    m["auto_rate"] = _rate(sum(r.status == "auto" for _, r in all_runs), n)
    m["refusal_rate"] = _rate(sum(r.refused for _, r in all_runs), n)
    m["failure_rate"] = _rate(sum(r.pred is None for _, r in all_runs), n)
    agree = [len({json.dumps(r.pred, sort_keys=True) for r in runs[c.issue["number"]]}) == 1
             for c in cases if runs.get(c.issue["number"])]
    m["consistency"] = _rate(sum(agree), len(agree))
    m["cost_per_issue"] = round(sum(r.cost_usd for _, r in all_runs) / n, 5) if n else None
    m["latency_p50_ms"] = int(statistics.median(r.latency_ms for _, r in all_runs)) if n else None

    misses = []
    for c in cases:
        preds = [r.pred for r in runs.get(c.issue["number"], [])]
        for f in FIELDS:
            got = Counter("(no output)" if p is None else p[f] for p in preds)
            top, count = got.most_common(1)[0] if got else ("(not run)", 0)
            if top != c.label[f]:
                misses.append({"number": c.issue["number"], "field": f, "label": c.label[f], "predicted": top,
                               "runs_agreeing": f"{count}/{len(preds)}"})
    return m, misses


def load_gate(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def gate(metrics: dict, base: dict, cfg: dict, *, complete: bool, accepted: dict | None, split: str) -> list[str]:
    fails = []
    if not complete:
        fails.append("eval is incomplete: the budget ran out before every case ran every repeat")
    if metrics["cases"] < cfg["min_cases"]:
        fails.append(f"only {metrics['cases']} cases; the gate needs at least {cfg['min_cases']}")
    for k, floor in cfg.get("floors", {}).items():
        v = metrics.get(k)
        if v is None or v < floor:
            fails.append(f"{k} {v} is below the floor {floor}")
    for k, ceiling in cfg.get("ceilings", {}).items():
        v = metrics.get(k)
        if v is not None and v > ceiling:
            fails.append(f"{k} {v} is above the ceiling {ceiling}")
    bb = cfg.get("beat_baseline", {})
    for k in bb.get("metrics", []):
        v, b = metrics.get(k), base.get(k)
        if v is None or b is None or v - b < bb["margin"]:
            fails.append(f"{k} {v} does not beat the rules baseline {b} by {bb['margin']}")
    reg = cfg.get("regression", {})
    if accepted and accepted.get("split") == split:
        old = accepted["metrics"]
        for k in reg.get("metrics", []):
            v, o = metrics.get(k), old.get(k)
            if o is not None and (v is None or v < o - reg["tolerance"]):
                fails.append(f"{k} regressed: {v} vs accepted {o} (tolerance {reg['tolerance']})")
        oc, nc = old.get("cost_per_issue"), metrics.get("cost_per_issue")
        if oc and nc and nc > oc * (1 + reg.get("max_cost_increase", 0.25)):
            fails.append(f"cost_per_issue rose to ${nc:.4f} from accepted ${oc:.4f}")
    return fails


def run_eval(rows: list[dict], *, repo: str, split: str, client, taxonomy: Taxonomy, effort: str, repeats: int,
             max_cost: float, workers: int, gate_cfg: dict, accepted: dict | None, conn=None, gh=None) -> dict:
    cases, skipped = load_cases(rows, split, repo, conn=conn, gh=gh)
    areas = set(taxonomy.areas)
    product = repo.split("/")[-1]
    model = run_model(cases, client, taxonomy, product, effort=effort, repeats=repeats, max_cost=max_cost,
                      workers=workers)
    complete = not model.stopped_on_budget and all(len(v) == repeats for v in model.runs.values())
    metrics, misses = score(cases, model.runs)
    base, _ = score(cases, run_baseline(cases, areas))
    failures = gate(metrics, base, gate_cfg, complete=complete, accepted=accepted, split=split)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "repo": repo, "split": split, "repeats": repeats, "model": MODEL, "effort": effort,
        "prompt_version": PROMPT_VERSION, "git_sha": os.environ.get("GITHUB_SHA"),
        "frozen_labels_hash": labels.frozen_hash(rows),
        "skipped": dict(skipped), "complete": complete, "stopped_on_budget": model.stopped_on_budget,
        "total_cost_usd": round(model.cost_usd, 4),
        "metrics": metrics, "baseline": base, "misses": misses,
        "gate": {"passed": not failures, "failures": failures,
                 "compared_to_accepted": bool(accepted and accepted.get("split") == split)},
    }


def accepted_record(report: dict) -> dict:
    keys = ("generated_at", "split", "repeats", "model", "effort", "prompt_version", "git_sha", "frozen_labels_hash",
            "metrics")
    return {k: report[k] for k in keys}


ROWS = [("kind_acc", "Kind accuracy"), ("area_acc", "Area accuracy"), ("severity_acc", "Severity accuracy"),
        ("blocked_acc", "Blocked accuracy"), ("blocked_recall", "Blocked recall"),
        ("blocked_precision", "Blocked precision"), ("evidence_valid_rate", "Evidence quote found"),
        ("auto_rate", "Passed all checks"), ("refusal_rate", "Refusals"), ("failure_rate", "No usable output"),
        ("consistency", "All runs agree"), ("cost_per_issue", "$ per issue"), ("latency_p50_ms", "p50 latency (ms)")]


def render_markdown(report: dict) -> str:
    m, b = report["metrics"], report["baseline"]
    verdict = "PASSED" if report["gate"]["passed"] else "FAILED"
    out = [f"# Loop eval: {report['split']} split, gate {verdict}", "",
           f"{m['cases']} cases x {report['repeats']} runs on `{report['model']}` (effort `{report['effort']}`, "
           f"prompt `{report['prompt_version']}`). Spend ${report['total_cost_usd']:.2f}.", ""]
    if report["skipped"]:
        out += ["Skipped: " + ", ".join(f"{k} {v}" for k, v in sorted(report["skipped"].items())) + ".", ""]
    out += ["| Metric | Model | Rules baseline |", "|---|---|---|"]
    for k, name in ROWS:
        bv = b.get(k) if k not in ("cost_per_issue", "latency_p50_ms", "evidence_valid_rate", "auto_rate",
                                   "refusal_rate", "failure_rate", "consistency") else None
        out.append(f"| {name} | {_fmt(m.get(k))} | {_fmt(bv)} |")
    out.append("")
    if report["gate"]["failures"]:
        out += ["## Gate failures", "", *[f"- {f}" for f in report["gate"]["failures"]], ""]
    if report["misses"]:
        out += ["## Misses (majority prediction differs from the label)", "",
                "| Issue | Field | Label | Predicted | Runs agreeing |", "|---|---|---|---|---|"]
        out += [f"| #{x['number']} | {x['field']} | {x['label']} | {x['predicted']} | {x['runs_agreeing']} |"
                for x in report["misses"]]
        out.append("")
    return "\n".join(out)


def _fmt(v) -> str:
    if v is None:
        return "-"
    return f"{v:.4f}" if isinstance(v, float) else str(v)
