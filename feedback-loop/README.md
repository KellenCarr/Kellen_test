# Loop

Loop turns a product's public issue tracker into the two signals a product team acts on, generated every week with no manual reporting:

1. **What users are asking for**, ranked by impact and checked against what already shipped.
2. **Who is blocked, on what**, cut by platform (1P API, Bedrock, Vertex, Foundry) and OS.

The first target is the public tracker for Claude Code (`anthropics/claude-code`): its issue forms capture platform, OS and terminal as structured fields, and its changelog and release feed are in the repo.

Status: **week 1 of 4.** Ingest, cohort extraction, Claude triage with checks, clustering, and the weekly digest work and are tested on synthetic data. Evals, matching shipped changes to asks, and the early access program come next ([docs/design.md](docs/design.md#plan)).

## How it works

```
GitHub issues + CHANGELOG.md + feed.xml
  └─ loop ingest   → DuckDB: issue, cohort (deterministic, from issue-form fields), changelog_entry, release
  └─ loop triage   → Claude (structured output) → checks → triage rows: auto | needs_review
  └─ loop digest   → Claude clusters asks per area → out/digest-<repo>-<date>.md
```

- **Deterministic first.** Platform, OS, terminal and regression come from the issue form, not the model.
- **The model judges meaning only:** kind, owning area, a normalized one-line ask, blocked or not, severity, plus a verbatim evidence quote.
- **Checked in code:** the area must be in `config/taxonomy.yaml`, and the evidence quote must appear in the issue text. A refusal, truncation or failed check goes to `needs_review`, never silently into the digest.
- **Safe by default:** read-only against GitHub, no usernames stored, no raw issue text in the digest. Issue text is treated as untrusted input in every prompt.

## Run it

```bash
make install                      # uv venv + deps
export GITHUB_TOKEN=...           # recommended (unauthenticated: 60 req/h)
export ANTHROPIC_API_KEY=...      # or any credential the Anthropic SDK resolves
make ingest REPO=anthropics/claude-code SINCE=2026-10-19
make triage REPO=anthropics/claude-code SINCE=2026-10-19 MAX_COST=2   # stops submitting at $2
make digest REPO=anthropics/claude-code WEEK_ENDING=2026-10-26
make test                         # no network, no credentials
```

Start with a week of issues and a low `MAX_COST`, then check the `$/issue` line before widening the window. Each triage call is logged to `llm_call` with tokens, cache reads, latency and cost.

## Layout

```
config/taxonomy.yaml     product areas (hand-curated; Claude proposes new ones for review)
src/loop/github.py       REST client: paginated incremental issues, raw files, retry/backoff
src/loop/issue_form.py   issue-form parsing, log collapsing, normalization
src/loop/cohorts.py      platform / OS / terminal from form fields (text fallback)
src/loop/changelog.py    CHANGELOG.md + Atom release feed
src/loop/llm.py          one structured Claude call: fallbacks, refusal handling, usage + cost
src/loop/triage.py       triage prompt, output schema, checks, budgeted parallel run
src/loop/cluster.py      group asks one fix would resolve; validated membership
src/loop/digest.py       the weekly digest
src/loop/schema.sql      DuckDB tables
```
