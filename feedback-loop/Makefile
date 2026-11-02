PY := .venv/bin/python
LOOP := .venv/bin/loop
REPO ?= anthropics/claude-code
SINCE ?= $(shell date -u -d '7 days ago' +%F 2>/dev/null || date -u -v-7d +%F)
WEEK_ENDING ?= $(shell date -u +%F)
MAX_COST ?= 2
EFFORT ?= low

.PHONY: install test lint ingest triage digest

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
