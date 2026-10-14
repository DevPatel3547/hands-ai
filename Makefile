# Common tasks. `make help` lists them.
PY ?= python3

.PHONY: help install lint format typecheck test cov check demo report web doctor docker clean

help:  ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-10s %s\n", $$1, $$2}'

install:  ## editable install with dev tools
	$(PY) -m pip install -e ".[dev,mcp]"

lint:  ## ruff lint + format check
	ruff check hands tests
	ruff format --check hands tests

format:  ## auto-format
	ruff format hands tests
	ruff check --fix hands tests

typecheck:  ## mypy (strict for the safety core)
	mypy

test:  ## run the tests
	$(PY) -m pytest

cov:  ## tests with coverage (fails under 85%)
	$(PY) -m pytest --cov=hands --cov-report=term-missing:skip-covered

check: lint typecheck cov  ## everything CI runs

demo:  ## all three gadgets on the simulated bench
	hands demo all --sim

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
