# Common tasks. `make help` lists them.
PY ?= python3

.PHONY: help install lint format typecheck test test-emu cov check demo demo-live demo-live-fake eval report web doctor docker clean

help:  ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-10s %s\n", $$1, $$2}'

install:  ## editable install with dev tools
	$(PY) -m pip install -e ".[dev,mcp,llm,vision]"

lint:  ## ruff lint + format check
	ruff check hands tests scripts
	ruff format --check hands tests scripts

format:  ## auto-format
	ruff format hands tests scripts
	ruff check --fix hands tests scripts

typecheck:  ## mypy (strict for the safety core)
	mypy

test:  ## run the tests (fast suite)
	$(PY) -m pytest

test-emu:  ## firmware on the RP2040 emulator (needs Node.js + `hands emu fetch`)
	$(PY) -m pytest -m emu

cov:  ## tests with coverage (fails under 85%)
	$(PY) -m pytest --cov=hands --cov-report=term-missing:skip-covered

check: lint typecheck cov  ## everything CI runs

demo:  ## all three gadgets on the simulated bench (scripted model)
	hands demo all --sim

REPLAYS ?= replays
STAMP := $(shell date +%Y%m%d)

demo-live:  ## REAL Claude run of all three gadgets (needs ANTHROPIC_API_KEY); records replays/ + eval
	@test -n "$$ANTHROPIC_API_KEY" || { echo "ANTHROPIC_API_KEY is not set: export it in your shell first (never commit it)."; exit 2; }
	hands doctor --live
	@mkdir -p $(REPLAYS)
	hands demo all --sim --model anthropic --record $(REPLAYS)/claude-$(STAMP).jsonl
	hands eval --model anthropic --out $(REPLAYS)/eval-claude-$(STAMP).json
	@echo "Replay later with: hands demo steelers --sim --model replay:$(REPLAYS)/claude-$(STAMP)-steelers.jsonl"

demo-live-fake:  ## the live Claude code path against a local fake Messages API (no key, no network)
	$(PY) scripts/demo_live_fake.py steelers

eval:  ## held-out spec suite against the fake API (plumbing check; real numbers need `make demo-live`)
	$(PY) -m pytest tests/test_eval.py -q

report:  ## markdown study report from metrics/runs.jsonl
	hands report --format md --out study-report.md

web:  ## wiring guide web UI for the Steelers gadget
	hands guide steelers --web

doctor:  ## what's missing on this computer
	hands doctor

docker:  ## build and run the sim demo image
	docker build -t hands-sim .
	docker run --rm hands-sim

clean:  ## remove caches and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
