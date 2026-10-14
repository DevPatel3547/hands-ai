# Build log

How Hands got from an idea to its current state, in order. It records the decisions, the
tradeoffs, the mistakes found along the way, and what is still unverified. Version dates
match [`CHANGELOG.md`](../CHANGELOG.md).

**Where things stand today:** the full loop works on a simulated bench (178 tests, 89% branch
coverage). It has not been run on physical hardware or with a real model end to end, and no
user-study sessions have been run.

---

## 1. The original idea: closed-loop AI hardware design

The starting question came from Anthropic's
[Project Fetch](https://www.anthropic.com/research/project-fetch-robot-dog). It showed that
Claude helps non-experts *control existing hardware*, and it named designing and building *new*
hardware as a separate, harder question.

The first version of the idea was a fully closed loop:

```
model designs a board -> board is fabricated -> instruments test it -> model reads results -> redesign
```

That is the most direct test of "can a model design hardware", and it would have made a striking
demo. It did not survive contact with the budget:

- **Every iteration costs real money.** Each design revision means ordering boards and parts
  again. A design loop that is *supposed* to fail and retry gets expensive fast.
- **Every iteration takes days.** Fabrication and shipping turn a feedback loop into a
  calendar. You cannot iterate on an agent's debugging behavior when each round takes a week.
- **Failures are permanent.** A bad board is scrap. There is no "unplug and move the wire".
- **It needs an expert somewhere.** Someone has to assemble, probe and judge the boards. For a
  builder with no electronics background, that expert does not exist.

## 2. The redesign: a ~$75 breadboard kit, with a person as the hands

The redesign kept the loop and replaced the expensive step. Fabrication became a **breadboard**,
and the fab plus robot became **a non-expert person** following instructions.

```
model designs a circuit -> code checks it -> a person wires it -> instruments test it -> model debugs
```

Why this is a better project, not just a cheaper one:

- **Iteration takes minutes and costs nothing.** A jumper moves in seconds. A wrong part is not
  scrap.
- **The person is the interesting part.** An agent acting through a human has to plan, give
  instructions a beginner can follow, and recover from that person's mistakes. That tests a
  capability Project Fetch did not cover.
- **It is measurable.** Time to a working device, mistakes caught before versus after power,
  interventions and unsafe events can all be logged automatically.

Key choices made at this stage:

| Decision | Why | Tradeoff |
|----------|-----|----------|
| **Raspberry Pi Pico W + MicroPython** | ~$7, USB powered, no compiler toolchain, firmware is a text file flashed with `mpremote`. | MicroPython is slower and less precise than C. That is fine for these gadgets. |
| **USB 5 V only, under 500 mA** | Removes mains, batteries and high current entirely. The worst realistic failure is a warm part or a USB port that cuts out. | Rules out motors, relays, LED strips and anything battery powered. |
| **A closed list of part kinds** (LED, resistor, buzzer, NPN, button, DHT22, SSD1306, servo) | Every kind gets explicit rules. An unknown part cannot slip through as "something the checker ignores". | Small designs only. A new part needs rules before it is allowed. |
| **Instruments on the bench**: INA219 current sensor, cheap logic analyzer, camera | The agent needs to *verify* rather than trust: current at power-on, signals on specific holes, a photo before power. | Adds roughly $15-45 depending on options, and the INA219 usually needs its header soldered. The logic analyzer and webcam are optional. |
| **Trust nobody** | The person can misplace a wire. The model can propose something unsafe. Both are checked. | More stages, more code, and slower runs. |

The shopping list came out at about $70-85 with a phone camera, or $85-110 with a webcam
([`SHOPPING_LIST.md`](../SHOPPING_LIST.md)).

## 3. v0.1 prototype: the loop on a simulated bench (2026-10-11)

There was no hardware yet, so the first build was a complete loop against a **simulated
bench**. This was a deliberate choice, not only a stopgap. A simulator makes the agent loop
deterministic and testable in CI, and it lets you inject the same human mistake over and over,
which you cannot do reliably with real hands.

What v0.1 had:

- **Netlist model and rule checker** (17 rule IDs at the time), driven by part facts in
  `hands/data/starter_kit.yaml`.
- **Breadboard placer and wiring guide**: one physical action per step, with exact hole
  coordinates.
- **Photo check**: a vision layer describes what it sees, and code diffs that description
  against the plan.
- **Firmware, test plan, instrumented tests and diagnosis**, including a deterministic
  signal-path walk with known-good diagnostic firmware.
- **The simulator**, in four parts:
  - a small **DC nodal-analysis solver** (`sim/mna.py`) with piecewise-linear diodes and NPN
    switches, which answers "is this LED lit, how much current flows through the shunt, is this
    pin overloaded";
  - an **as-built board** (`sim/build.py`, `sim/board.py`) that is evaluated from the wires as
    they were *actually* placed, not as planned. A fault changes the physical state, and the
    agent only sees what the instruments and the camera report;
  - a **MicroPython runtime** (`sim/runtime.py`) that runs the same firmware the real Pico would
    run, with stand-ins for `machine`, `time`, `dht` and `ssd1306`, and **virtual time**, so a
    10-second logic capture takes milliseconds;
  - a **simulated person** (`sim/human.py`) who follows the steps, makes injected mistakes, and
    adds an estimated beginner time per action.
- **Seven injectable mistakes**: `reversed_led`, `missing_resistor`, `wrong_pin`, `loose_wire`,
  `hidden_short`, `swapped_i2c` and `servo_wrong_rail`. Two of them (`loose_wire`,
  `hidden_short`) are invisible to the camera on purpose, so the post-power instruments have to
  catch them. All seven were caught and fixed in tests.
- **Three gadgets**: the Steelers score light, a temperature display and a servo pointer.
- **62 tests.**

The Steelers demo was scripted to show the whole story in one run. The model's first design puts
a buzzer straight on a GPIO (~25 mA, over the 12 mA limit). The rule check rejects it, and the
model redesigns with a transistor. The simulated person then leaves out a resistor (caught in
the photo before power) and leaves a jumper loose (caught by the signal walk after power).

## 4. Hardening pass: attacking the project's own gates (v0.2.0, 2026-10-12)

With the loop working, the next step was to assume the model is adversarial, or simply wrong
in creative ways, and look for any path from model output to an unsafe physical action. That
review found **more than 20 distinct bypasses**. `docs/safety.md` groups them into 14
categories, and every one now has a regression test.

### The big lesson: a gate that exists is not a gate that is enforced

Several checks existed but were not on every path:

- **The rule check was not a hard gate everywhere.** It ran in the agent, but `layout.place()`
  and `wiring.build_guide()` would happily turn an unchecked design into wiring steps. The fix
  was to move `rules.require_safe` *inside* those functions, and to re-assert it with a digest
  check before every physical stage, so a design edited after checking is caught.
- **Power was not gated on the photo check** on every path. Now USB power requires the latest
  wiring to have passed, and any physical change resets that.
- **Re-power without checks.** Fixes found by the signal walk or by the I2C/DHT checks plugged
  USB back in with no photo check or smoke test, and some of those changes were requested
  *while powered*. Now every physical fix runs the same sequence: unplug, change, photo check,
  smoke test, reflash. `_human_change` refuses outright while USB is plugged in.
- **The old program problem.** A real Pico keeps its last `main.py`. An old program could drive
  pins into the new circuit the moment USB is connected. Now the bare Pico gets safe-idle
  firmware before the first wiring step.

The pattern behind all four: **put the invariant in the function that does the dangerous thing,
not in its caller.**

### Rule-checker bypasses

- **Pin aliasing through Pico power and control pins.** A design could wire `pico.VSYS` or
  `pico.VBUS` into its own net, which the checker did not treat as a rail, or tie `pico.RUN` or
  `pico.3V3_EN` to GND. Now only `pico.GPn` may appear in a design.
- **Reserved fixture nets.** A net named `V3_SRC` was silently overwritten when the fixture was
  merged in, and `V3_SRC` is the *unmeasured* side of the current sensor. A net named `INA_SDA`
  joined the instrument's own bus. Both names are now reserved.
- **Power-alias net names.** `Gnd`, `VCC` and `+5V` were treated as ordinary signals the checker
  could not reason about. They are now refused.
- **Unknown part kinds.** Any kind added to the YAML was accepted, and the rules ignored it. Kinds
  are now an allow-list, and fixture or tool parts cannot be used as design parts.
- **Loosened limits.** Setting `gpio_max_ma: 100` in the YAML made the checker accept it. Limits
  are now clamped to hard-coded ceilings (`HARD_LIMITS`), so the YAML can only tighten them.
- **A button across the supplies** passed every rule. Pressing it would short USB. It is now
  `shorted_part`.
- **5 V into a GPIO through something other than a resistor**: through an LED, a buzzer, a
  button, or a DHT22 module's on-board pull-up when the module is powered from 5 V. `overvoltage`
  now follows paths through any of these.
- **An NPN collector straight to a supply**, with nothing to limit current. `transistor` now
  refuses it.
- **A wired GPIO missing from `pin_map`**, which the firmware linter therefore could not check.
- **Loosely typed design JSON.** Parsing is now strict.

New rules added: `shorted_part`, `led_current` and `resistor_power` (17 to 20 rule IDs).
`overvoltage`, `transistor` and `rail_budget` were extended, the last one with a total USB
budget. Worst-case LED current now uses minimum Vf.

### Firmware linter gaps

Each of these got past the v0.1 linter: `Pin(n)` with a computed `n`; `from machine import Pin
as P`; `machine.mem32` raw register writes; `I2C(1)` with default pins the design never checked;
`getattr(x, "__dict__")`. The linter now requires literal or module-constant pins from the
checked `pin_map`, never lets a button pin be an output, and refuses raw registers, deep sleep,
overclocking, `eval`/`exec`, dunder access, star imports and bus peripherals on default pins.

### Model-output paths

- **The model's physical fixes.** A model diagnosis could tell the person to make *any* wiring
  change. Now `validate_fix` allows only actions that restore the approved plan (re-seat a
  planned wire, re-check a placed part, inspect the rails), and the instruction text is
  generated by code.
- **Diagnostic toggling.** The signal walk would toggle whatever GPIO the model's test plan named,
  including a button pin. Test plans are now validated (`validate_test_plan`).
- **MCP flashing.** The MCP `flash_firmware` tool flashed anything. It now requires a rule-checked
  design and lint-clean code, and a `check_design` tool was added.
- **Web UI injection.** Vision-model text went into the page through `innerHTML`. It is now
  inserted as text only, behind a strict CSP, with upload checks and an access token whenever
  the UI is reachable beyond localhost.

### Also added in this pass

- A **spec screen** (`safety.screen_text`) that refuses mains, batteries, >5 V, >500 mA, motors,
  relays, heaters and lasers before any model call. It is deliberately conservative: a false
  refusal costs a rephrase, and a false accept could cost a fire.
- Crash fixes: the placer raised a `TypeError` when the breadboard was full; `extract_json`
  could return non-object JSON; a bad model regex could raise `re.error` mid-run; malformed
  model fix dicts could crash the simulated person.

## 5. Real-hardware readiness (v0.2.0)

Next came everything needed to run on a real kit. All of it is unit-tested against fakes, and
none of it has touched a board yet.

- **`hands run <gadget|spec> --real`**: the full loop with a person at the terminal.
- **`hands doctor`**: checks Python, pyserial, mpremote, the Pico's port, MicroPython, the INA219,
  sigrok, the camera and any model, and prints the exact fix for each missing piece.
- **`hands bringup`**: a guided first-time check, one part at a time (on-board LED, INA219
  fixture, logic-analyzer self-test, camera). It is meant to show a faulty part before you build
  a whole gadget on top of it.
- **Port discovery** by Raspberry Pi's USB vendor ID (0x2E8A) on macOS, Linux and Windows, with
  BOOTSEL detection. **mpremote retries** with backoff on transient errors. **sigrok-cli** presence
  and device checks with actionable errors. **Camera** through OpenCV, fswebcam, imagesnap, or a
  phone photo (`--camera prompt`).
- **An on-Pico current monitor** (`firmware/hands_bench.py` plus a two-line `main.py` shim). It
  samples the INA219 on its own SoftI2C bus every 20 ms while the gadget's program runs, so
  current can be measured *while the buzzer sounds* without stopping the program. There is an
  `mpremote exec` fallback.
- **Free model providers.** A standard-library OpenAI-compatible adapter (Ollama, LM Studio,
  llama.cpp, vLLM) with JSON mode, timeouts and retries, alongside the Anthropic SDK adapter.
  When no vision model is available, `ConsoleVision` asks the person yes/no questions instead.
  The goal was that nobody needs to pay for anything to try Hands.

## 6. Web UI (v0.2.0)

The terminal works, but a beginner holding a jumper wire needs the instructions next to the
breadboard. The web UI is a single accessible page served by the standard library (no
JavaScript framework, no new dependency), usable from a phone on the same Wi-Fi:

- a diagram for each step, a progress bar, phone photo upload and a tick-box self-check list;
- a large red **"do not plug in"** panel that stays up until every step is done and the photo
  check has passed;
- security: binds to localhost by default, requires a random token beyond localhost, uses a
  strict CSP, checks upload type and size, and never inserts model text as HTML.

## 7. Tooling and study support (v0.2.0)

- **Metrics:** schema-versioned JSONL events with ISO timestamps, participant ids,
  interventions, and power on/cut. `hands report --format md` produces a study report.
- **Study protocol:** [`docs/study-protocol.md`](study-protocol.md), a 3-5 person non-expert
  pilot with facilitator rules and a consent template.
- **Packaging and CI:** a `hands` entry point, ruff, mypy (strict for the safety core), pytest
  with an 85% coverage gate, and GitHub Actions on Python 3.11/3.12/3.13 plus macOS and Windows,
  which also runs the sim demos and builds a study report. Plus a Dockerfile for the sim demo
  and a Makefile.
- **`test_every_rule_id_is_reachable`** fails if any rule ID is never exercised by a test.
- Test count went from **62 to 178**, with 89% branch coverage.

## 8. Documentation pass (2026-10-12)

README rewrite with an honest status section, this build log, an interview/design Q&A
([`INTERVIEW_GUIDE.md`](INTERVIEW_GUIDE.md)), repository URLs, and a stricter `.gitignore`.
A verification-status note was added to `docs/safety.md`.

---

## Decisions and tradeoffs, summarised

| Decision | Alternative considered | Why this one |
|----------|------------------------|--------------|
| Deterministic rule checker as the safety authority | Ask the model to self-review for safety | Same input, same verdict; auditable and testable; cannot be argued out of a rule. The model still gets the findings as feedback. |
| Vision *describes*, code *decides* | Ask the vision model "does it match?" | A yes/no from a model is unauditable. A structured description diffed by code gives specific, fixable discrepancies. |
| Deterministic diagnosis first, model last | Send every failure to the model | Power faults, I2C checks and the signal walk are cheap and certain. The model is used for what code cannot do, such as spotting a firmware bug. |
| Simulator before hardware | Wait for the kit | Reproducible fault injection, CI coverage of the whole agent loop, and no hardware cost while the design is still changing. |
| Model physical fixes may only restore the plan | Let the model propose new wiring | New wiring would skip the rule check. Restoring the plan keeps gate 1 meaningful. |
| Hard-coded ceilings; YAML can only tighten | Fully configurable limits | A config typo should never loosen safety. |
| Score feed through a host-side bridge | Pico W polls the web itself | The scoreboard JSON is large compared with the Pico's free RAM, and the bridge keeps Wi-Fi credentials off the device. |
| Free scripted model by default | Require an API key | Anyone can run the full demo for free. The cost is that the default demo shows the pipeline, not model quality. |

## What is still unverified

- **Physical hardware.** Nothing has run on a real Pico yet. Expect fixes around mpremote timing,
  sigrok CSV quirks, and how the on-Pico monitor's soft timer behaves next to a busy program.
- **Datasheet numbers.** The RP2040/RP2350 pad-drive and total-current limits (12 mA per pin,
  50 mA total), the Pico W 3V3 budget, and the section numbers cited in `docs/safety.md` were
  written **from memory** during development. They are conservative, but each needs to be
  checked against the current datasheet. The RP2350 (Pico 2 W) total-current figure in
  particular is assumed to match the RP2040.
- **Part figures that vary by vendor.** SG90 stall current (650 mA assumed, the high end of
  reported values), buzzer current (25 mA assumed), and clone pin orders for the INA219 and OLED.
- **Real models.** The Claude and Ollama adapters are tested only against a fake server. No real
  model has run the loop end to end, so there are no numbers yet on design rejection rates, lint
  rejections, or success rates.
- **Vision accuracy on real photos.** The simulated camera is perfect unless told to miss. A
  real vision model reading a real breadboard photo is untested.
- **Time-to-working estimates.** Sim times are per-action estimates (for example 30 s per jumper,
  45 s per part), not measurements.
- **The ESPN feed.** The endpoint is unofficial and undocumented. It may change or disappear.
- **Pico 2 W.** Same pinout and the stricter RP2040 limits are applied, but it has never been
  tested.
