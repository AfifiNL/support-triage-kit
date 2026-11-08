# Everything here runs offline and costs nothing, except `record-live`, which calls a live model
# on purpose. `make check` is exactly what CI runs.
UV  ?= uv
RUN := $(UV) run --all-extras
CLI := $(RUN) support-triage-kit
OFFLINE  := TRIAGE_PROVIDER=mock
CASSETTE := evals/fixtures/live-triage.jsonl
REPLAY   := TRIAGE_PROVIDER=replay TRIAGE_CASSETTE=$(CASSETTE)
# The live configuration `record-live` records. Needs Gemini credentials (Vertex AI: ADC and
# GOOGLE_CLOUD_PROJECT) in the environment, and costs money: at most LIVE_CALLS calls.
LIVE_CALLS ?= 70
LIVE ?= TRIAGE_PROVIDER=gemini TRIAGE_GEMINI_VERTEX=true TRIAGE_GEMINI_MODEL=gemini-3.6-flash \
        TRIAGE_GEMINI_LOCATION=eu TRIAGE_GEMINI_THINKING_LEVEL=low \
        TRIAGE_MAX_LIVE_CALLS=$(LIVE_CALLS)

.PHONY: help install lint fmt typecheck test eval eval-baseline eval-replay eval-replay-baseline \
        workflows record-live demo serve check

help:            ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

install:         ## create .venv with all extras and dev tools (locked)
	$(UV) sync --all-extras

lint:            ## ruff lint + format check
	$(RUN) ruff check src tests
	$(RUN) ruff format --check src tests

fmt:             ## apply ruff formatting and safe fixes
	$(RUN) ruff format src tests
	$(RUN) ruff check --fix src tests

typecheck:       ## mypy (strict)
	$(RUN) mypy

test:            ## pytest (offline)
	$(OFFLINE) $(RUN) pytest

eval:            ## eval gate with the offline mock model: exits 1 on a floor breach or a drop
	$(OFFLINE) $(CLI) eval --gate evals/gate.json --baseline evals/baseline.json --out reports/mock

eval-baseline:   ## re-record evals/baseline.json (review the diff before committing)
	$(OFFLINE) $(CLI) eval --update-baseline --baseline evals/baseline.json --out reports/mock

eval-replay:     ## replay the recorded live run offline; fails on any change or a drop
	$(REPLAY) $(CLI) eval --gate evals/gate_replay.json --baseline evals/baseline_replay.json \
	  --out reports/replay

eval-replay-baseline: ## re-record evals/baseline_replay.json from the cassette
	$(REPLAY) $(CLI) eval --update-baseline --baseline evals/baseline_replay.json --out reports/replay

record-live:     ## call the live model for every ticket and record the cassette (costs money)
	$(LIVE) TRIAGE_RECORD_CASSETTE=$(CASSETTE) $(CLI) eval --workers 4 --out reports/record

demo:            ## offline end-to-end demo: signed webhook -> review -> outbox
	$(OFFLINE) $(CLI) demo

serve:           ## HTTP API on http://127.0.0.1:8000 (set TRIAGE_WEBHOOK_SECRET, TRIAGE_REVIEW_TOKEN)
	$(CLI) serve

check: lint typecheck test eval eval-replay  ## everything CI runs
