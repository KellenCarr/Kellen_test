PY := .venv/bin/python
LOOP := .venv/bin/loop
REPO ?= anthropics/claude-code
SINCE ?= $(shell date -u -d '7 days ago' +%F 2>/dev/null || date -u -v-7d +%F)
WEEK_ENDING ?= $(shell date -u +%F)
MAX_COST ?= 2
EFFORT ?= low
N ?= 150
SPLIT ?= frozen
EVAL_MAX_COST ?= 10

.PHONY: install test lint ingest triage digest sample labels-check freeze eval eval-accept review-export review-import

install:
	uv venv -q .venv
	uv pip install -q --python $(PY) -e ".[dev]"

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check .

ingest:
	$(LOOP) ingest --repo $(REPO) --since $(SINCE)

triage:
	$(LOOP) triage --repo $(REPO) --since $(SINCE) --max-cost $(MAX_COST) --effort $(EFFORT)

digest:
	$(LOOP) digest --repo $(REPO) --week-ending $(WEEK_ENDING) --effort $(EFFORT)

# Evals: label, freeze, measure, accept. See evals/CODEBOOK.md and README "Operate it".
sample:
	$(LOOP) sample --repo $(REPO) -n $(N)

labels-check:
	$(LOOP) labels-check

freeze:
	$(LOOP) labels-check --update

eval:
	$(LOOP) eval --repo $(REPO) --split $(SPLIT) --effort $(EFFORT) --max-cost $(EVAL_MAX_COST)

eval-accept:
	$(LOOP) eval --repo $(REPO) --split $(SPLIT) --effort $(EFFORT) --max-cost $(EVAL_MAX_COST) --accept

# The review loop: needs-review issues and reader flags -> labels.
review-export:
	$(LOOP) review-export --repo $(REPO)

review-import:
	$(LOOP) review-import
