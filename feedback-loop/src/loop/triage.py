"""Claude triage of issues, with deterministic checks after the model responds."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from loop import store
from loop.issue_form import collapse_long_code_blocks, normalize
from loop.llm import MODEL, llm_call_row, structured_call

PROMPT_VERSION = "triage_v1"
DEFAULT_TAXONOMY = Path(__file__).resolve().parents[2] / "config" / "taxonomy.yaml"


class Triage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["bug", "feature_request", "model_behavior", "docs", "question", "other"]
    area: str
    proposed_area: str | None
    ask: str
    blocked: bool
    blocked_reason: str | None
    severity: Literal["critical", "high", "medium", "low"]
    evidence_quote: str
    confidence: Literal["high", "low"]


@dataclass(frozen=True)
class Taxonomy:
    areas: dict[str, str]  # id -> what it covers

    @classmethod
    def load(cls, path: Path = DEFAULT_TAXONOMY) -> "Taxonomy":
        doc = yaml.safe_load(path.read_text())
        return cls({a["id"]: a["covers"] for a in doc["areas"]})


def output_schema(taxonomy: Taxonomy) -> dict:
    schema = Triage.model_json_schema()
    schema["properties"]["area"]["enum"] = [*taxonomy.areas, "new"]
    return schema


def system_prompt(taxonomy: Taxonomy, product: str) -> str:
    areas = "\n".join(f"- {k}: {v}" for k, v in taxonomy.areas.items())
    template = files("loop").joinpath(f"prompts/{PROMPT_VERSION}.md").read_text()
    return template.replace("{product}", product).replace("{areas}", areas)


def user_message(issue: dict) -> tuple[str, str, int]:
    """Returns (message, the text the evidence quote is checked against, log lines omitted)."""
    body, omitted = collapse_long_code_blocks(issue["body"])
    fields = "; ".join(f"{k}={issue[k]}" for k in ("platform", "os", "terminal", "model", "regression")
                       if issue.get(k) and issue[k] != "unknown")
    msg = (f'<issue number="{issue["number"]}">\n<labels>{", ".join(issue["labels"])}</labels>\n'
           f"<form_fields>{fields}</form_fields>\n<title>{issue['title']}</title>\n<body>\n{body}\n</body>\n</issue>")
    return msg, f"{issue['title']}\n{body}", omitted


def check(data: dict | None, taxonomy: Taxonomy, source_text: str) -> tuple[Triage | None, str | None, bool | None]:
    """Deterministic checks. Returns (triage, review_reason or None, evidence_valid)."""
    if data is None:
        return None, "no_output", None
    try:
        t = Triage.model_validate(data)
    except ValidationError:
        return None, "schema", None
    if t.area != "new" and t.area not in taxonomy.areas:
        return t, "unknown_area", None
    evidence_valid = bool(t.evidence_quote.strip()) and normalize(t.evidence_quote) in normalize(source_text)
    if not evidence_valid:
        return t, "evidence_not_found", False
    if t.area == "new":
        return t, "new_area", True
    return t, None, True


def triage_issue(client, issue: dict, taxonomy: Taxonomy, product: str, effort: str):
    msg, source_text, omitted = user_message(issue)
    r = structured_call(client, system=system_prompt(taxonomy, product), user=msg,
                        schema=output_schema(taxonomy), effort=effort)
    if r.error and r.data is None and r.stop_reason is None:
        return None, r  # transport/API failure: no row, so the next run retries it
    if r.stop_reason == "refusal":
        t, reason, evidence_valid = None, "refusal", None
    elif r.stop_reason != "end_turn":
        t, reason, evidence_valid = None, r.stop_reason or "no_output", None
    else:
        t, reason, evidence_valid = check(r.data, taxonomy, source_text)
    row = {
        "number": issue["number"], "content_sha": issue["content_sha"], "prompt_version": PROMPT_VERSION,
        "model": MODEL, "effort": effort,
        **(t.model_dump() if t else dict.fromkeys(Triage.model_fields)),
        "evidence_valid": evidence_valid, "lines_omitted": omitted,
        "status": "needs_review" if reason else "auto", "review_reason": reason,
        "stop_reason": r.stop_reason, "refusal_category": r.refusal_category, "served_model": r.served_model,
    }
    return row, r


@dataclass
class RunSummary:
    candidates: int = 0
    triaged: int = 0
    needs_review: int = 0
    failed: int = 0
    cost_usd: float = 0.0
    stopped_on_budget: bool = False


def run_triage(conn, client, repo: str, since: datetime, *, effort: str, limit: int | None, max_cost: float,
               workers: int, run_id: str, taxonomy: Taxonomy | None = None) -> RunSummary:
    """Triage untriaged issues with at most `workers` calls in flight. Once spend reaches max_cost no new
    call starts; the ones already in flight finish and are recorded, so overshoot is bounded by `workers`."""
    taxonomy = taxonomy or Taxonomy.load()
    product = repo.split("/")[-1]
    issues = store.issues_to_triage(conn, repo, since, PROMPT_VERSION, MODEL, effort, limit)
    s = RunSummary(candidates=len(issues))
    queue = iter(issues)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        in_flight = {}

        def submit_next() -> None:
            if (i := next(queue, None)) is not None:
                in_flight[pool.submit(triage_issue, client, i, taxonomy, product, effort)] = i

        for _ in range(workers):
            submit_next()
        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for fut in done:
                issue = in_flight.pop(fut)
                row, r = fut.result()  # FATAL API errors propagate and end the run
                now = datetime.now(UTC)
                store.insert_row(conn, "llm_call", llm_call_row(run_id, "triage", str(issue["number"]), r, now))
                s.cost_usd += r.cost_usd
                if row is None:
                    s.failed += 1
                else:
                    store.insert_row(conn, "triage", {"repo": repo, **row, "created_at": now})
                    s.triaged += 1
                    s.needs_review += row["status"] == "needs_review"
                if s.cost_usd >= max_cost:
                    s.stopped_on_budget = True
                else:
                    submit_next()
    return s
