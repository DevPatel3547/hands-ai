# Interview guide

Questions people are likely to ask about Hands, with short answers. Every number here can be
reproduced from the repo (`pytest`, `hands demo all --sim`, `hands rules`), and every claim
matches the [Status](../README.md#status) section of the README: **the loop works on a
simulated bench; it has not run on real hardware or with a real model end to end yet.**

---

## The pitch

**What is Hands, in 30 seconds?**
An AI hardware engineer that uses a non-expert human as its hands. You give it a one-sentence
gadget idea. A model designs a circuit from the parts you own, and deterministic code checks
it against 20 electrical rules. Hands then walks you through wiring it one jumper at a time,
checks a photo before power, measures current at power-on, flashes firmware, tests the result
with instruments, and debugs it with you. The model proposes; code decides what is safe.

**Why build it?**
Anthropic's Project Fetch showed Claude roughly halving the time non-experts needed to program
an *existing* robot dog, and the write-up says designing and building *new* hardware is a
different question. Hands tests that question cheaply: about $75 of breadboard parts, with a
person instead of a fab or a robot.

**What actually works today?**
The whole loop, end to end, on a simulated bench: 178 tests, 89% branch coverage. All 7
injectable wiring mistakes are caught and fixed. The real-hardware path is implemented and
tested against fakes, but has not run on a physical board. No real model has run the loop end
to end yet; the demos use a scripted model. I'm explicit about that because the honest version
is still interesting, and an overclaimed version is worthless.

---

## Safety design

**Why deterministic gates instead of letting the LLM judge safety?**
Four reasons:

1. **Same input, same verdict.** A safety check that gives a different answer on a rerun is not
   a check.
2. **It can't be talked out of a rule.** The thing being checked is model output. If the checker
   is also a model, a confident wrong design can persuade it. Code cannot be persuaded.
3. **It is auditable and testable.** Every rule cites a datasheet number (`docs/safety.md`) and
   has regression tests. `test_every_rule_id_is_reachable` fails if any rule is never exercised.
4. **Failures are asymmetric.** A false refusal costs one more revision. A false accept could
   cost a part, a USB port, or a burned finger.

The model is still used where it adds value: proposing designs, writing firmware and test plans,
describing photos, and diagnosing failures that code cannot explain. Its output always passes
through a gate. Rule findings go back to the model as feedback, so it learns why a design was
rejected.

**Rules can be incomplete. How do you handle unknown unknowns?**
By keeping the world closed. Designs may use only 8 part kinds, and any other kind is refused,
even if someone adds it to the YAML. Designs may only touch `pico.GPn` pins; power is wired by a
fixed fixture. Limits are hard-coded ceilings that config can tighten but never loosen.
Worst-case values are used throughout (for example minimum LED Vf, which gives the highest
current). And the envelope itself is small: USB 5 V, under 500 mA, no batteries, no mains.

**What are the gates?**
0. Spec screen (before any model call). 1. Rule check, re-run inside the placer and the wiring
guide and before every physical stage, with a digest so later tampering is caught. 2. Photo
check before power. 3. Smoke test at every power-on. 4. Firmware linter. 5. Model physical fixes
must restore the approved plan. 6. Test-plan validation. Plus two invariants: no wiring change
while USB is plugged in, and the bare Pico gets safe-idle firmware before any wiring.

**What does the firmware linter check, and why AST instead of regex?**
It parses the MicroPython with Python's `ast`. Imports are allow-listed. Every `Pin(...)` must
name a literal or module-constant GPIO from the rule-checked `pin_map`, so the linter always
knows which pins are driven. A button pin can never be an output. I2C/SPI/UART must name their
pins. `eval`, `exec`, dunder access, `machine.mem32`, deep sleep and overclocking are refused.
Regex can't follow `from machine import Pin as P` or a computed pin number. Both of those got
past the first version, which is exactly why it is AST-based now.

**What was the most important thing the hardening pass found?**
That a gate that *exists* is not the same as a gate that is *enforced*. The rule check ran in
the agent, but `layout.place()` would still turn an unchecked design into wiring steps if called
directly. Some physical fixes re-powered the board without a new photo check, and a few were
even requested while powered. The fix was structural: put the invariant inside the function
that does the dangerous thing (`require_safe` inside `place()` and `build_guide()`, lint inside
`_flash()`, a powered check inside `_human_change`). The pass found more than 20 bypasses in
total, all fixed and regression-tested.

**Give me an example of a subtle rule bypass.**
A DHT22 *module* has an on-board pull-up resistor to its own VCC. If the model powers it from
5 V, that pull-up puts 5 V on the GPIO. No resistor appears in the netlist, so the original
"5 V through a resistor into a GPIO" check missed it. `overvoltage` now follows paths through
resistors, LEDs, buzzers and buttons (up to 4 hops), and through module pull-ups when the module
is on 5 V. Another: a design could wire `pico.VSYS` into its own net, which the checker didn't
treat as a rail. Now only GPIO pins may appear in a design.

---

## Catching mistakes

**How is a fault caught before power versus after power?**

*Before power*, the photo check. The vision model returns a structured description of what it
sees (sanitised JSON, never a yes/no), and `wiring.compare` diffs it against the plan. Each
discrepancy becomes a code-generated fix instruction, carried out with USB unplugged, followed by
another photo check. Mistakes the camera can see are caught here: a reversed LED, a resistor
replaced by a jumper, a wire in the neighbouring pin, swapped SDA/SCL, a servo on the wrong rail.

*After power*, the instruments. Every power-on starts with safe-idle firmware (all GPIOs inputs)
and an INA219 reading of the 3V3 rail against a budget derived from the rule checker's own
estimate (`max(50, 1.5 × estimate + 20)` mA). A short shows up as over-current or as a board that
stops answering, and the agent unplugs at once. Then the tests run. When one fails, diagnosis
goes from cheap and certain to expensive and uncertain: power fault, then I2C/sensor wiring, then
known-good diagnostic firmware plus a **signal walk** with the logic analyzer hole by hole along
the planned path until the signal disappears, and only then the model.

**Example?**
In the Steelers demo, the simulated person leaves out a resistor and leaves one jumper loose.
The photo catches the missing resistor before power. The loose jumper looks fine in a photo,
so the buzzer test fails after power. The signal walk sees the signal at hole a20 but not at
a38, which identifies jumper W15. The agent says to unplug and re-seat it, then repeats the
photo check, smoke test and reflash, and all 9 tests pass.

**Why does the pre/post split matter?**
A mistake caught before power cannot damage anything. So the share caught pre-power is one of
the headline metrics, and it is logged separately (`errors_caught_pre_power` and
`errors_caught_post_power`). Tests also force the camera to miss mistakes (`--vision-miss`) to
check that the post-power path catches them.

---

## The simulator

**How does the simulator work?**
Four layers:

1. **Physics:** a small DC nodal-analysis solver (`sim/mna.py`) with piecewise-linear diodes and
   NPN switches, and GPIOs modelled as voltage sources behind about 40 Ω. The 3V3 regulator
   collapses past 0.5 A, so a hard short becomes a brownout. It answers the questions a bench
   asks: is this LED lit, what does the shunt see, does this probe read high.
2. **As-built board:** the board is evaluated from the wires *as actually placed*, not as
   planned. An injected mistake changes that physical state, and the agent only sees what the
   instruments and the camera report. Ground truth (which faults remain) is tracked separately
   and checked at the end of each test.
3. **Runtime:** the *same* MicroPython firmware that would go to a real Pico runs against shims
   for `machine`, `time`, `dht` and `ssd1306`, with **virtual time**: a 10-second logic capture
   runs in milliseconds. Restricted builtins, an import allow-list and busy-loop detection. That
   is not a security sandbox; the linter is the real gate.
4. **Simulated person:** follows the steps, injects mistakes (7 kinds, 2 invisible to the
   camera), and adds an estimated beginner time per action, such as 30 s per jumper.

**Why build a simulator instead of waiting for hardware?**
Reproducibility. You can't make a real person leave the same jumper loose 50 times, but the
simulator can, in CI, on every commit. It tests the *agent loop* (gates, diagnosis, recovery),
which is where the bugs were. It also cost nothing while the design kept changing.

**What doesn't the simulator capture?**
Timing-accurate I2C or PWM at the electrical level, contact resistance, ESD, heat, and imperfect
vision (the simulated camera is perfect unless told to miss). Sim "time to working" is an
estimate, not a measurement.

---

## The numbers

**What are the key safety limits?**

| Limit | Value |
|-------|-------|
| GPIO level | 3.3 V, treated as not 5 V tolerant |
| Per-pin current | 12 mA max (8 mA warning) |
| Total GPIO current | 50 mA |
| 3V3(OUT) load | 300 mA |
| USB budget | 500 mA total (including ~100 mA for the Pico W itself) |
| Max supply | 5.25 V |
| Resistor dissipation | 125 mW (half of 1/4 W) |

Worked examples: an LED with 220 Ω on a GPIO at worst-case Vf 1.8 V draws
(3.3 − 1.8) / 220 ≈ 6.8 mA, which is fine. An active buzzer draws about 25 mA, so it can't go on
a pin and needs a PN2222A; 1 kΩ base resistor gives (3.3 − 0.7) / 1000 = 2.6 mA base current.
An SG90 servo's stall current is assumed to be 650 mA (the high end of reported values), so it
goes on 5 V and gets a stall warning.

**Where do those numbers come from, and are they verified?**
From the RP2040, Pico W, INA219 and part datasheets, with sources listed in `docs/safety.md`.
To be precise: the RP2040/RP2350 values and section numbers were written from memory while
building, and re-verifying them against the current PDFs is on the roadmap. They were chosen
conservatively, and the RP2350 is held to the stricter RP2040 limits.

**What do the demo results show?**
On the simulated bench with the scripted model, all three gadgets reach a working device. Rules
catch 1, 0 and 2 design errors; the photo check catches 1 mistake before power in each; the
instruments catch 1, 1 and 0 after power. Estimated times to working are 20.9, 13.8 and 11.6 min.
That shows the pipeline works. It says nothing yet about real models or real people.

---

## Risks

**What could still go wrong?**

- **Vision says "matches" when it doesn't,** *and* the fault sits where the INA219 can't see it:
  the 5 V servo rail, or a load driven straight from a GPIO. The person watching the board is the
  last backstop.
- **The parts aren't what the YAML says,** for example a 100 Ω resistor in the 220 Ω bag, or a
  clone INA219 or OLED with a different pin order. The checks are only as good as that file.
- **The person doesn't unplug when told.** Without a `uhubctl`-compatible hub, Hands can't cut
  power itself.
- **The spec screen is pattern-based,** so creative phrasing can get past it. That's acceptable
  because it isn't the only line: whatever the spec says, a design can only use the 8 allowed
  part kinds, powered from USB, and must pass the rules.
- **USB ports differ.** Most limit current, but not all. A powered hub adds protection.
- **The real bench is untested.** mpremote timing, sigrok output quirks and the on-Pico monitor
  next to a busy program are the likely first failures.
- **Designs can be safe and still not work.** That's a functional failure, not a safety one, and
  the test-and-debug loop exists for it.

**Can the model hurt someone through Hands?**
It would have to get an unsafe design past 20 deterministic rules, unsafe firmware past an AST
linter, and an unplanned wiring change past `validate_fix`, all inside a USB-only envelope. Each
of those paths has regression tests built from real bypasses found in review. The residual
risks are physical ones the software can't see, listed above and in `docs/safety.md`.

---

## The user study

**How would you run the user study?**
Following `docs/study-protocol.md`:

- **Participants:** 3-5 adults who have never built a circuit (no soldering, Arduino or EE).
  Convenience sample, recorded as a limitation. Check whether ethics/IRB review is needed first.
- **Task:** everyone builds `steelers` first, so times are comparable, with a 60-minute limit.
  `hands run steelers --real --participant P01 --camera prompt`.
- **Facilitator rules:** no help with the task. Step in only for safety (heat, smell, wiring
  while powered), and log every assist and intervention.
- **Measures:** automatic (time to working, wiring pace, mistakes caught before and after power,
  interventions, safety events, model calls) plus hand-collected (assists, confusion points,
  1-7 ratings of confidence, clarity and safety, and "what was the hardest moment?").
- **Analysis:** `hands report --format md`. Report every session, including failures. With
  n = 3-5, describe; don't claim significance.

Before any participant, the prerequisites are a successful `hands bringup` and a full real
Steelers run by me, to shake out hardware bugs so participants aren't debugging my code.

---

## What's next

**What would you do next, in order?**

1. **Hardware bring-up:** `hands bringup` and a recorded real Steelers build. Fix what the fakes
   missed.
2. **Verify every datasheet number** against the current datasheets.
3. **Model evaluation:** Claude vs. local models on the three specs plus held-out specs. Measure
   rule rejections per design, revisions to a passing design, lint rejections, and the share of
   runs that reach a working device.
4. **Pilot study** with 3-5 non-experts.
5. **More capability, rules first:** new part kinds (potentiometer, RGB LED, low-voltage relay
   module), a second INA219 on the 5 V rail, live photo-check results in the web UI.

**How does this relate to evaluating autonomous capabilities?**
Hands is a harness for measuring how well a model can get things done in the physical world
*through* an intermediary it doesn't control: planning, instructing, verifying and recovering
from errors. The same structure (model proposes, deterministic gates decide, instruments verify,
everything logged) is how I'd want to give any agent physical affordances. The bench is already
exposed over MCP, so Claude Code can drive the same lint-gated tools directly.

**What would you change if you started over?**
Write the gates as invariants inside the dangerous functions from day one, instead of checks in
the orchestrator. Most of the bypasses came from that one mistake.

---

## About the build

**How did you build it?**
I built it with Claude as an AI pair-programmer, across design discussions, code, tests, safety
review and docs. I don't have an electronics background, which is why the safety model leans so
heavily on cited datasheet numbers and conservative worst cases instead of intuition. It also
makes me the target user. The full story is in [`BUILD_LOG.md`](BUILD_LOG.md).

**Why the Pico W and MicroPython?**
About $7, USB powered, no compiler toolchain, and firmware is a text file the model can write and
`mpremote` can flash. The linter can parse it with Python's own `ast` module.

**Why a human instead of a robot arm?**
Cost, and because it's the more interesting test. An agent working through a person has to
communicate, verify and recover. It can't assume its instructions were followed.
