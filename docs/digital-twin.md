# Digital twin

Hands checks a gadget two independent ways before a person wires anything:

1. **The rule checker** (`hands/rules.py`) is the gate. It is plain worst-case arithmetic over the netlist,
   fast and deterministic, and nothing can be wired or powered unless it passes.
2. **SPICE** (`hands spice`, `hands/sim/spice.py`) is the independent check that the rules are strict enough.
   It runs [ngspice](https://ngspice.sourceforge.io/) DC operating points of the same design with real device
   models, at every tolerance corner, for every state the GPIO pins and buttons can be in.

A property test (`tests/test_rules_soundness.py`) ties the two together: Hypothesis generates random designs from
the starter kit, and **for every design the rules accept, SPICE must show every hard limit respected at every
corner**. When this test was first run it found five real holes in the rules. They are fixed and pinned as
regression tests ([Findings](#findings)).

## `hands spice`

```
brew install ngspice            # macOS (Linux: sudo apt install ngspice)
hands spice steelers --corners  # gadget id, or a design .json file
```

```
SPICE check (ngspice-42): steelers-desk-light: 6 corner(s) x all 36 pin/button state(s) = 216 operating points in 0.6 s
  quantity                              nominal  min     max     limit  rules say  worst case at
  GP15 current (mA)                     5.30     0.00    7.84    12     7.84       hot_3.4_4.75 [GP14=D GP15=H GP16=Z]
  GP16 current (mA)                     2.43     0.00    2.76    12     2.95       hot_3.4_4.75 [GP14=D GP15=L GP16=H]
  all GPIO current (mA)                 8.45     0.60    11.38   50     11.58      ...
  led1 LED current (mA)                 5.30     0.00    7.84    20     -          ...
  q1 collector current (mA)             24.51    0.00    25.34   600    -          ...
  total USB draw (mA)                   126.37   101.20  131.77  500    133.30     ...
SPICE verdict: PASS: every quantity stays inside its hard limit
```

Exit code 0 = pass, 1 = a hard limit is exceeded, 2 = ngspice is not installed (one-line install hint).
`--json` prints the same report as JSON (`"source": "ngspice"`). `--version N` checks a gadget's N-th scripted
design (`hands spice steelers --version 1` shows the buzzer-on-a-GPIO design failing: 18.8 mA on GP16).

### What one run does

* The gadget design is merged with the bench fixture (Pico 3V3 regulator, INA219 shunt and its I2C pull-ups,
  USB VBUS), exactly as the rules see it.
* **States.** Every GPIO in the design is driven high, low or left as an input; a GPIO wired to a button can only
  be an input with its pull-up or pull-down (the firmware linter, `hands/fwlint.py`, refuses firmware that makes
  such a pin an output, and SPICE relies on that same contract). Every button is pressed or released. The bench's
  I2C pins (GP26/GP27) are held low, their worst case. All combinations run; if there were ever more than 4096
  copies, a deterministic sample is taken and the report says "sampled".
* **Corners.** `nom` (typical values), four `hot` corners (resistors -5 %, LED Vf at its minimum, an ideal GPIO pad,
  3V3 at 3.2 or 3.4 V, VBUS at 4.75 or 5.25 V) and `cold` (resistors +5 %, LED Vf at its maximum, weak pad, low
  supplies).
* Every state x corner is an independent copy of the circuit in **one** netlist, so a full sweep is one ngspice
  process: 216 operating points for the hero gadget in well under a second of ngspice time.
* Every node has a 1 Gohm leak to ground (as `mna.py` does) so floating nets have a DC solution.

### Device models (`hands/sim/spice_models/__init__.py`)

| Part | Model | Source |
|------|-------|--------|
| PN2222A | Gummel-Poon `2N2222A` (Is=14.34f, Bf=255.9, Vaf=74.03, ...) | Fairchild model in LTspice `standard.bjt` (ltwiki.org mirror); PN2222A is the TO-92 package of the same die |
| 5 mm LEDs | Shockley diode, N=2, Rs=7.5 ohm; Is fitted so Vf(20 mA) = the inventory's `vf` / `vf_min` / 2.5 V max | Vf max: Kingbright L-57IID datasheet (HE red, 2.5 V max at 20 mA), assumed for yellow/green. N and Rs are **assumptions** that give the usual ~0.3 V drop from 20 mA to 1 mA |
| Active buzzer | 0.3 V diode + 120 ohm, i.e. 25 mA at 3.3 V | the inventory's `current_ma`; a DC average of an oscillator + transducer (same as `mna.py`) |
| RP2040 pad | driver resistance to IOVDD/GND + ESD clamp diodes to IOVDD and GND | RP2040 datasheet 5.5.3 bounds the driver from above only (VOH >= IOVDD - 0.62 V at the set drive), so hot corners use an ideal pad |
| Modules | supply current = inventory `current_ma` (servo: `current_moving_ma`) above `v_min`, linear below; on-board pull-ups from `pullup_ohms` (DHT22: 4.7 kohm assumed) | inventory |
| Supplies | 3V3 3.2-3.4 V (assumed +/-3 %), VBUS 4.75-5.25 V (USB 2.0) | |

Nothing here was measured on the kit's own parts. These are published or typical models with the assumptions
labelled; the point is an *independent* second opinion with real device physics, not a calibrated twin.

## The soundness property

`tests/test_rules_soundness.py` builds random designs out of the circuit motifs a beginner or a model would write:
LED branches with zero, one, two-in-series or two-in-parallel resistors on either side, sourced from a GPIO or a
rail and sunk to GND or a GPIO; pull resistors to 3V3/5V/GND; buttons (direct or through a resistor) to GND or
3V3; transistor-switched buzzers and LEDs; buzzers straight on a pin; resistors between two pins; resistors across
a rail; the OLED on valid, invalid and swapped I2C pins; the DHT22; the servo on either rail. GPIOs come from a
small pool, so loads pile up on shared pins. Hypothesis `target()` steers the search toward designs the rules
accept close to their limits.

For every design `check_design` accepts, SPICE must show at every corner and every state: each GPIO <=
`gpio_max_ma` (12 mA), all GPIOs <= `gpio_total_max_ma` (50 mA), no GPIO more than 0.3 V above IOVDD or below GND,
every LED <= its `i_max_ma`, every NPN collector <= `ic_max_ma`, every resistor <= `resistor_max_w`, the 3V3
regulator <= 300 mA and total USB draw <= 500 mA.

* CI runs 300 fixed (derandomized) examples: about 25 s on a 2-vCPU box.
* `HANDS_SOUNDNESS_RANDOM=1 HANDS_SOUNDNESS_EXAMPLES=4000 pytest tests/test_rules_soundness.py` explores new
  designs. After the fixes below, a 4000-design random run (874 accepted by the rules) found no counterexample.
* A second test fails the run if the rules accepted fewer than 15 % of the generated designs, so the property can't
  pass vacuously.

### Findings

Each of these was a design the original rules **accepted** while SPICE showed a hard limit exceeded. Each is now
rejected and pinned in `tests/test_spice.py` (`FINDINGS`, `test_led_vf_curve_finding`).

| # | Design | Rules said | SPICE said | Fix in `rules.py` |
|---|--------|-----------|------------|-------------------|
| 1 | 220 ohm between two GPIOs (one high, one low) | 0 mA on both pins | 16.3 mA on each (limit 12) | a resistor to another GPIO counts as contention: IOVDD / R |
| 2 | 220 ohm from 5V to GND | 114 mW (nominal 5.0 V) | 131.8 mW at 5.25 V, R -5 % (limit 125) | every rail at its tolerance high, resistors at -5 % |
| 3 | LED with two 220 ohm in parallel, or resistors on both legs | never current-checked | 31 mA (limit 20) | LED current follows series chains and parallel banks; anything else **fails closed** ("cannot bound this LED's current") |
| 4 | one GPIO with a button to 3V3 and another button to GND | pass | 9.7 A (3V3 shorted when both are pressed) | buttons that can join two supplies are a `shorted_part` error |
| 5 | GPIO + 220 ohm + yellow LED | 6.36 mA (constant Vf_min) | 7.84 mA at the hot corner | an LED's Vf falls at low current; the rules now use the same LED curve as SPICE (`spice_models.led_current`) |

What changed for the rules overall: every current estimate is now a worst case over the same envelope as the SPICE
hot corners (rails at their highs, resistors -5 %, LED curve fitted to Vf_min, Vbe 0.6 V, module pull-ups, the
bench's INA219 lines, the 3V3 regulator's efficiency at the worst supply ratio). Where a part's surroundings are
not a structure the rules understand, the estimate falls back to the envelope of every voltage the net can reach,
so the rules are stricter there, never looser. The three gadgets still pass. The rules' numbers now meet or exceed
SPICE's maxima (for the hero, GP15: rules 7.84 mA, SPICE 7.84 mA; total USB: rules 133.3 mA, SPICE 131.8 mA).

## mna.py vs SPICE on the as-built board

The in-process simulator (`hands/sim/board.py` + `mna.py`, a piecewise-linear DC solver) is what the demo and the
Engineer loop use, because it is about 70 times faster than starting ngspice per operating point (1.3 ms vs
92 ms per operating point, measured on a shared 2-vCPU Linux box). Its LED model is now the straight line through the
SPICE LED curve at 5 mA and 20 mA (`spice_models.led_pwl`).

`tests/test_spice.py::test_mna_agrees_with_spice_as_built` builds each gadget as a simulated person would, with no
fault and then with each of the 7 injectable faults (where they apply), and compares the two solvers on every
output-level/button combination. Agreement over all 146 operating points:

| Quantity | Tolerance asserted | Worst disagreement measured |
|----------|--------------------|-----------------------------|
| LED / buzzer current | 0.35 mA + 5 % | 0.40 mA (1.4 %, the buzzer through the transistor) |
| INA219 shunt current | 1.0 mA + 5 % | 0.05 mA |
| GPIO node voltage | 0.08 V | 0.016 V |

One deliberate difference: with the `hidden_short` fault (3V3 shorted to GND), both solvers agree the regulator is
overloaded (that is what makes the simulated board brown out), but past that point SPICE's pad clamps follow the
collapsed IOVDD and `mna.py`'s pin sources don't, so pin voltages are not compared in that state.

`SimBoard(..., solver="spice")` / `SimBench(..., solver="spice")` use ngspice for every operating point instead
(cached per pin state like the default solver).
