# Loop: design

Loop answers two questions every week from a product's public issue tracker, with no manual reporting:

1. What are users asking for, ranked by impact, and has any of it already shipped?
2. Who is blocked, on what, and on which platform?

It is built as a Product Ops system: the tool is one part, and the program around it (taxonomy ownership, review queue, early access, retros) is the other.

## Decisions

| Decision | Choice | Why | Tradeoff |
|---|---|---|---|
| Source | `anthropics/claude-code` public issues, `CHANGELOG.md`, `feed.xml` | Real, high-volume feedback on a product with structured issue forms; the reader can judge the output instantly | Public issues are not enterprise feedback: no revenue weighting, and noisier cohorts |
| Cohorts | Deterministic, from issue-form fields (Platform, OS, Terminal); text match only as a fallback | The form already holds the fact; asking a model for it adds error and cost | Free-text issues get `unknown` more often |
| Model's job | Kind, owning area, normalized ask, blocked, severity, evidence quote | These need judgment | Needs evals (week 2) |
| Grouping | Claude assigns an area from a curated taxonomy, then clusters asks within an area | Evaluable, explainable, and stays on one provider (no embeddings API needed) | Duplicates that span two areas aren't merged |
| Storage | DuckDB file | Anyone can run `make test` and the pipeline without an account | Ports to Delta/Databricks by swapping `store.py`; the SQL is plain |
| Model | `claude-opus-5-5`, effort `low` by default, set explicitly | Triage is classification-shaped; the effort sweep in week 2 decides | — |

## Pipeline

1. **`loop ingest`** pulls issues updated since the last run (paginated, with retries and rate-limit backoff), then upserts `issue` and `cohort`. It also loads the changelog (`changelog_entry`, tagged with change type and surface) and release dates from the Atom feed (`release`). Every stage is safe to re-run: all writes are upserts on natural keys.
2. **`loop triage`** handles user-filed issues (staff excluded) with no triage row for their current `content_sha`. An edited issue is re-triaged; an unchanged one never is. Each call:
   - uses structured output (`output_config.format`) with `area` constrained to the taxonomy enum;
   - has server-side fallbacks on (`fallbacks: "default"`), and checks `stop_reason` before reading;
   - caches the system prompt (taxonomy and definitions);
   - collapses pasted logs over 40 lines to their first and last 10 lines, recording the count in `lines_omitted`;
   - runs with at most `--workers` calls in flight; no new call starts once spend reaches `--max-cost`.
3. **Checks in code, after the model responds.** Any failure sends the issue to `needs_review` with a `review_reason`:
   - refusal or `max_tokens` → no output trusted (`refusal_category` logged);
   - schema violation → `schema`;
   - an area not in the taxonomy → `unknown_area`; `area = "new"` → `new_area`, so a human promotes it into the taxonomy;
   - the evidence quote not found in the normalized issue text → `evidence_not_found`. This is the hallucination guard;
   - transport or API errors after SDK retries → no row is written, so the next run retries. A bad request or bad credentials ends the run instead of failing every issue.
4. **`loop digest`** builds the digest from stored data only:
   - Claude clusters asks within each area (cluster membership is validated: input numbers only, each exactly once);
   - clusters are ranked by an impact proxy (1 + reactions + 0.5 × comments per issue);
   - blocked issues are grouped by (area, platform, OS);
   - shipped work comes from the release window;
   - pipeline health covers review reasons and spend.
   Items awaiting review appear with a † rather than being dropped (recall over polish).

## Safety and privacy

- Read-only against GitHub. Loop drafts; it never comments, labels or closes.
- No author logins are stored. The digest shows Claude's normalized asks plus issue links, never raw issue text.
- Every prompt states that issue text is untrusted and that instructions inside it are ignored. Structured output limits what an injected instruction could do: the worst case is a wrong classification, which the checks and review queue catch.

## Metrics (process as product)

| User | What they need | Metric |
|---|---|---|
| PM / eng lead | A ranked list they trust | Precision of the top-10 clusters (reviewed sample); share of digest items they act on |
| Research / model team | Model-behavior regressions early | Model-behavior issues per release; time from release to first cluster |
| Field / Support | To hear back | Loop-closure rate: share of top asks with a matched shipped change and a drafted reply (week 3) |
| Ops (me) | Low upkeep | `needs_review` rate, $/issue, minutes of manual work per digest (target: 0) |

## Plan

| Week | Build | Done when |
|---|---|---|
| 1 | Ingest, cohorts, taxonomy seed, triage with checks, clustering, digest | Code and tests done; first live digest (needs API keys) reviewed by hand |
| 2 | Label 150 issues (`evals/labels.jsonl`, codebook); `run_eval.py` with 3 repeats; keyword-rule baseline; effort sweep (low/medium) | Eval table: kind accuracy, area accuracy, blocked recall (gate), evidence validity, $/issue |
| 3 | Close the loop: match changelog entries to clusters with a quote check on both sides, plus drafted replies; a model-launch lookback (the 2026-10-21 sampling-parameter removal, or the next model launch) | Precision gate on "fixed by" matches (a false "fixed" is the costly error) |
| 4 | Package as a Claude Code skill; EAP brief (the question first, scaffolding, success criteria); recruit 5–8 testers | EAP opens |
| 6 | EAP retro and v2 | Retro published, with what changed because of it |

## Known limits

- **Impact is a proxy.** Internally, ARR, seats or a strategic-account flag would plug into `impact()`. The public version can't have them.
- **Clusters are per run,** not yet stable week to week. Next step: pass last week's labels in as existing clusters so the same ask keeps its identity and trends become visible.
- **Clustering chunks groups over 200 asks** and doesn't merge across chunks.
- **`feed.xml` only covers the last ~20 releases,** enough for a weekly window. Backfilling dates for week 3 needs the Releases API or git history.
