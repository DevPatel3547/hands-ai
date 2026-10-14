# Contributing to Hands

Thanks for helping. Hands puts model-designed circuits in the hands of beginners, so the bar
for safety-related changes is high and explicit.

## Setup

```bash
git clone https://github.com/DevPatel3547/hands.git && cd hands
python -m venv .venv && . .venv/bin/activate
make install            # pip install -e ".[dev,mcp]"
make check              # ruff + mypy + pytest with the 85% coverage gate: what CI runs
make demo               # all three gadgets on the simulated bench
```

No hardware or API key is needed for development. The simulated bench, scripted model and
fake servers cover everything in CI.

## Ground rules

1. **Safety code is deterministic.** `safety.py`, `rules.py`, `fwlint.py` and the gates in
   `agent.py` must never call a model.
2. **Every safety change needs a regression test.** Put it in `tests/test_safety.py` (rules,
   lint, spec screen) or `tests/test_gates.py` (agent gates). `test_every_rule_id_is_reachable`
   fails if a rule ID is never exercised.
3. **Every number needs a source.** Add or update its row in `docs/safety.md`: datasheet
   name and section. If you cannot find one, use the conservative value and say so.
4. **Limits only get stricter.** `HARD_LIMITS` in `safety.py` is a ceiling. Do not add code
   paths that read limits from anywhere else.
5. **New part kinds need rules first.** Adding a kind to `DESIGN_KINDS` without rules for its
   hazards will be rejected. The simulator model, a gadget or test, and the docs follow.
6. **No new runtime dependencies** without a strong reason. The core needs only PyYAML;
   real-bench and model extras are optional.
7. **Model output is untrusted input.** Validate structure, then check it. Never put it into
   HTML or a shell command.

## Style

- `ruff format` (line length 120) and `ruff check`. `make format` fixes most issues.
- Type hints everywhere. `mypy` is strict for the safety core and the real bench.
- Messages shown to people use short sentences, one action each, with exact hole coordinates.

## Pull requests

- Describe the change and how you verified it. If you touched the real bench, say whether
  it was tested on hardware, and with which board and OS.
- Add a line to `CHANGELOG.md` under *Unreleased*.
- Hardware-path changes that were not run on a board are welcome. Mark them "untested on
  hardware".

## Reporting a safety problem

If you find a way to get Hands to produce a wiring step or power-on for an unsafe circuit,
please open an issue on [DevPatel3547/hands](https://github.com/DevPatel3547/hands/issues) titled
`safety:` with the design JSON and the command you ran, or contact the maintainer directly. These reports get top priority.
