# Architecture

Hands is a loop with one rule: **the model proposes, deterministic code decides, the person
acts, instruments verify.** This page maps that loop onto the code. Safety details and their
datasheet sources are in [`safety.md`](safety.md); the circuit and firmware simulation is in
[`digital-twin.md`](digital-twin.md); the threat model is in [`../SECURITY.md`](../SECURITY.md).

## The loop and its gates

```
spec ──► gate 0: spec screen (safety.screen_text) ──refuse──► stop, no model call
  │
  ▼
model: design JSON ──► circuit.Design (strict parse) ──► gate 1: rules.require_safe (20 rules)
  ▲                                                        │  error → findings back to the model
  └──────────── revise with findings ◄─────────────────────┤
                                                           ▼ pass
                                           SPICE check (sim/spice.py, optional, fails closed)
                                                           ▼
           layout.place ─► wiring.build_guide (both re-run gate 1, digest-checked)
                                                           ▼
           bare Pico gets safe-idle firmware ─► person wires, one action per step, USB unplugged
                                                           ▼
           gate 2: photo check (vision describes, wiring.compare decides) ─► fix with power off
                                                           ▼
           gate 3: power on with safe-idle firmware + INA219 current against the rule budget
                                                           ▼
model: firmware ──► gate 4: fwlint (AST) ──► flash ──► model: test plan ──► gate 6: validate
                                                           ▼
           run tests (serial, logic analyzer, INA219, camera) ──fail──► diagnose
                deterministic first: power fault → I2C/DHT wiring → known-good firmware +
                logic-probe signal walk → model last (firmware patch: linted again;
                physical fix: gate 5, may only restore the plan; then unplug, change,
                photo check, smoke test, reflash)
```

`agent.Engineer` owns the order. Every stage that touches the physical world re-asserts gate 1
through `Engineer._approved()` (the design digest must match the one that was checked), every
physical change goes through `_human_change()` (refuses while powered, resets the photo check),
and power goes through `_plug()` / `_unplug()`.

## Modules

| Layer | Modules | Calls a model? |
|---|---|---|
| Entry points | `cli.py` (argparse; optional subcommands in `commands/`), `demo.py` (wires a run together), `eval.py` (held-out spec suite) | no |
| Orchestration | `agent.py` (the loop and gates 2, 3, 5, 6), `diagnose.py` (signal walk, known-good firmware) | orchestrates |
| Safety core | `safety.py` (hard ceilings, spec screen, part allow-list), `rules.py` (20 rules), `fwlint.py` (firmware AST linter), `circuit.py` / `inventory.py` / `data/starter_kit.yaml` (netlist, parts, limits) | **never** |
| Build guidance | `layout.py` (breadboard placer), `wiring.py` (steps, diagrams, photo diff), `webui/` (phone-friendly guide, strict CSP) | no |
| Models | `llm.py` (scripted, Anthropic, OpenAI-compatible, replay, recording), `prompts.py`, `vision.py` (describe only, sanitised) | yes |
| Benches | `bench/base.py` (the `Bench` protocol), `bench/sim_bench.py`, `bench/real_bench.py` (mpremote, pyserial, sigrok, camera), `bench/mcp_server.py` | no |
| Simulation | `sim/mna.py` (fast DC solver), `sim/spice.py` + `sim/spice_models/` (ngspice, corners), `sim/board.py` (as-built breadboard), `sim/runtime.py` (CPython firmware runner, labelled), `sim/human.py` (a person who makes realistic mistakes) | no |
| Firmware | `firmware/hands_bench.py` (on-Pico INA219 sampler), `firmware/main_shim.py` | no |
| Operations | `doctor.py` (environment check, `--live` key check), `bringup.py` (first hardware check), `metrics.py` (JSONL events, study report), `log.py` (structured logging, redaction) | no |

## Model adapters

`llm.LLMClient` is one method: `complete(system, prompt, *, stage, images, max_tokens) -> str`.

- `ScriptedClient`: canned replies per stage for the three built-in gadgets. Demonstrates the
  pipeline, not model quality, and every run says so.
- `AnthropicClient`: streams every request through the official SDK. Structured stages
  (design, revision, test plan, diagnosis, vision) use a forced tool call whose input schema is
  `STAGE_SCHEMAS[stage]`. The system prompt is cached. HTTP 429/529 are retried by the SDK
  honouring `retry-after`; overloaded errors inside the SSE stream are retried by the adapter.
  Errors become one-line `LLMError`s. Token usage lands in metrics.
- `OpenAICompatClient`: stdlib-only `/chat/completions` (Ollama, LM Studio, llama.cpp, vLLM).
- `RecordingClient` / `ReplayClient`: `--record FILE` saves a real session; `--model replay:FILE`
  replays it with no network and always prints `REPLAY of <model> recorded <date>`.

Every run's `run_end` event carries `model_source` (`scripted`, `replay` or `live`), so a
report can never mix canned and real results silently.

## Testing the live path without a key

`tests/fakes/anthropic_server.py` is a local HTTP server that speaks the Messages API wire
format: JSON and Server-Sent Events (`message_start`, `content_block_*` with `text_delta` and
`input_json_delta`, `message_delta`, `message_stop`, `error`), header and body validation,
injected 429/529 with `retry-after`, slow responses and mid-stream errors. The real SDK is
pointed at it with `base_url`, so the adapter code that runs against Claude is the code under
test. `make demo-live-fake` runs the whole Steelers loop through it; `make demo-live` runs it
against Claude with your key.

## Benches

A `Bench` implements `power_on`, `power_cut`, `flash_firmware`, `read_serial`, `write_serial`,
`capture_logic`, `read_current`, `camera_snapshot` and `wait`. The agent cannot tell the
benches apart, which is what makes the simulated runs meaningful:

- `SimBench`: the as-built breadboard (including the person's mistakes) is turned into a
  circuit and solved with `mna.py` (or ngspice), and firmware runs in `sim/runtime.py`.
- `RealBench`: a Raspberry Pi Pico W over USB serial and `mpremote`, an INA219 on GP26/GP27
  read by `hands_bench.py` while the program runs, a sigrok logic analyzer and a camera.
  Implemented and tested against fakes; not yet run on a physical board.

## Data

`metrics/runs.jsonl` gets one schema-versioned event per line (`run_start`, `rule_check`,
`spice_check`, `wiring_step`, `photo_check`, `intervention`, `power_on`, `smoke_test`, `flash`,
`test`, `diagnosis`, `run_end`, ...). `hands report` turns it into a terminal table or a
markdown study report; simulated and real runs are kept in separate sections.
