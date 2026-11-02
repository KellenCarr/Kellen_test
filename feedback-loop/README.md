# Loop

Loop turns a product's public issue tracker into the two signals a product team acts on, generated every week with no manual reporting:

1. **What users are asking for**, ranked by impact and checked against what already shipped.
2. **Who is blocked, on what**, cut by platform (1P API, Bedrock, Vertex, Foundry) and OS.

The first target is the public tracker for Claude Code (`anthropics/claude-code`): its issue forms capture platform, OS and terminal as structured fields, and its changelog and release feed are in the repo.

Status: **built to run unattended; not yet run on real issues.** Ingest, triage with checks, clustering, the digest, the eval harness and gate, publishing, the heartbeat and the review loop are built and tested on synthetic data. What's left needs keys and human judgment: the first live run, 150 hand labels, and turning on the schedule. See [Operate it](#operate-it). Matching shipped changes to asks comes next ([docs/design.md](docs/design.md#plan)).

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

## Operate it

Production here means: four weekly digests in a row published with nobody touching them, an eval gate on every
prompt or model change, and reader flags turning into eval cases. Setup, once:

1. **Secret:** `ANTHROPIC_API_KEY` in Settings > Secrets and variables > Actions. Never in a file in the repo.
2. **Variables** (same page, Variables tab):
   | Variable | Value |
   |---|---|
   | `LOOP_SITE_URL` | The Pages URL, e.g. `https://<user>.github.io/<repo>` |
   | `LOOP_MAX_COST` | Weekly triage budget in USD (default 5). Set it from the first run's $/call |
   | `LOOP_EVAL_MAX_COST` | Budget per eval-gate run in USD (default 10) |
   | `LOOP_ENABLED` | `true` turns on the weekly schedule and the heartbeat. Leave it unset until step 5 |
   | `LOOP_SOURCE_REPO`, `LOOP_FEEDBACK_REPO` | Optional. Default to `anthropics/claude-code` and this repo |
3. **Pages:** Settings > Pages > Source: **GitHub Actions**. Create a `wrong-item` label for reader flags.
4. **First run, by hand:** Actions > *feedback-loop weekly* > Run workflow with `max_cost` = 2. Read the pipeline
   health section of the run summary: issues opened vs triaged, review reasons, and $/call. Set `LOOP_MAX_COST`
   to cover a normal week with headroom.
5. **Turn it on:** set `LOOP_ENABLED` to `true`. The heartbeat fails if a week passes with no successful run.
   GitHub emails failed scheduled runs to whoever last edited the cron line; enable Actions notifications in the
   GitHub mobile app to get them on your phone.

Each week, about 15 minutes:

- `make review-export` (with `GITHUB_TOKEN` and `LOOP_FEEDBACK_REPO=<owner/repo>` set, so reader flags are
  pulled), fill in the queue, set
  `"confirmed": true`, `make review-import`, commit `evals/`. Promote taxonomy proposals into `config/taxonomy.yaml`
  when the digest's taxonomy-fit line shows the same proposal more than once.

The eval, once (needs a local database: run `make ingest SINCE=<a month back>`, or download the `loop-db`
artifact from a weekly run into `data/`):

1. `make sample` adds 150 unlabeled, stratified issues to `evals/labels.jsonl`. Label them using
   [evals/CODEBOOK.md](evals/CODEBOOK.md), before looking at what the model said.
2. When the frozen labels are final: `make freeze`, then commit `labels.jsonl` and `frozen.sha256` together.
3. Measure the noise floor: run `make eval` twice on unchanged code and compare. Set `regression.tolerance` in
   `evals/gate.yaml` above the gap, and set the floors from what you see.
4. `make eval-accept` records the accepted baseline (`evals/accepted.json`). Commit it.

From then on, the *feedback-loop eval gate* workflow runs the frozen eval on every PR that touches the prompt,
triage code, LLM client, taxonomy or labels, and fails the PR when the gate fails. Iterate on `SPLIT=tune`; the
frozen set is for the gate only.

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
src/loop/labels.py       hand labels: validation, frozen/tune split, frozen-set hash, stratified sampling
src/loop/baseline.py     rules-only triage, the bar the model has to clear
src/loop/evaluate.py     eval runs (repeats), scoring, the gate, the report
src/loop/publish.py      digest -> docs/ site with index and RSS
src/loop/review.py       review queue: needs-review issues and reader flags -> labels
evals/                   gate.yaml, CODEBOOK.md, labels.jsonl, frozen.sha256, accepted.json
docs/                    the published site (Pages): design doc, digests, index, feed
```

Workflows live in the repo's `.github/workflows/`: `feedback-loop-ci` (lint, tests), `feedback-loop-weekly` (the
pipeline and Pages deploy), `feedback-loop-heartbeat` (missed-run alarm) and `feedback-loop-eval` (the gate).
The weekly run carries the database between runs as a 90-day artifact. Before saving it, `loop prune` drops the
body text of triaged issues older than 14 days.
