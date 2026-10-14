"""``hands bringup``: a guided first-time hardware check, before building any gadget.

1. Find the Pico and read its MicroPython version.
2. Blink the on-board LED (nothing wired yet) and ask the person whether they saw it.
3. Wire *only* the bench fixture (Pico + INA219), with USB unplugged, one jumper at a time;
   then power on with safe-idle firmware and check the INA219 reads a sane idle current
   and a ~3.3 V bus voltage.
4. Logic-analyzer self-test: clip channel D0 next to GP15, run a known-good 2 Hz toggle,
   and check the analyzer sees the edges.
5. Optional camera snapshot.

Every step reports PASS / FAIL / SKIP with a concrete next action, and the result is logged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .bench.base import BenchError
from .circuit import Design
from .diagnose import DIAG_TOGGLE_FW, SAFE_IDLE_FW, probe_hole
from .fwlint import lint_firmware
from .inventory import Inventory, load_inventory
from .layout import pico_hole, place, strip_of
from .metrics import Metrics
from .wiring import build_guide

PROBE_GPIO = 15
IDLE_MAX_MA = 15.0
BUS_V_RANGE = (3.1, 3.45)


@dataclass
class StepResult:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""
    fix: str = ""


def fixture_guide(inv: Inventory) -> Any:
    """Wiring guide for the bench fixture alone (an empty gadget, still rule-checked)."""
    return build_guide(place(Design("bringup-fixture", {}, {}, {}), inv), inv)


def run_bringup(
    bench: Any,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
    inv: Inventory | None = None,
    metrics: Metrics | None = None,
    skip: set[str] | None = None,
) -> list[StepResult]:
    inv = inv or load_inventory()
    skip = skip or set()
    results: list[StepResult] = []

    def yes(q: str) -> bool:
        for _ in range(3):
            a = ask(q + " [y/n] ").strip().lower()
            if a[:1] in ("y", "n"):
                return a.startswith("y")
        return False

    def record(r: StepResult) -> StepResult:
        results.append(r)
        say(f"   [{r.status}] {r.name}" + (f": {r.detail}" if r.detail else ""))
        if r.status == "FAIL" and r.fix:
            say(f"          next: {r.fix}")
        if metrics:
            metrics.event("bringup_step", step=r.name, status=r.status, detail=r.detail[:200])
        return r

    say("== Hands bring-up. Low voltage only: everything runs from the computer's USB port.")
    say("   Start with the Pico NOT on the breadboard and nothing else connected.")
    ask("   Plug the Pico into the computer with the micro-USB cable, then press Enter. ")

    # 1. find + identify
    try:
        info = bench.device_info()
        ok = info.split()[0:1] == ["micropython"]
        r = record(
            StepResult(
                "Pico + MicroPython",
                "PASS" if ok else "FAIL",
                f"{bench.port}: {info}",
                "" if ok else "install MicroPython: https://micropython.org/download/RPI_PICO_W/",
            )
        )
    except BenchError as e:
        r = record(StepResult("Pico + MicroPython", "FAIL", str(e)[:300], "run `hands doctor` for details"))
    if r.status == "FAIL":
        return results

    # 2. on-board LED
    if "led" not in skip:
        try:
            bench.blink_onboard_led(8)
            seen = yes("   Did the small LED next to the USB socket blink a few times?")
            record(
                StepResult(
                    "on-board LED blink",
                    "PASS" if seen else "FAIL",
                    "",
                    ""
                    if seen
                    else "the board runs code but the LED didn't blink: on a Pico W the LED is "
                    "driven by the wireless chip; check you have a Pico W/WH, not a Pico",
                )
            )
        except BenchError as e:
            record(StepResult("on-board LED blink", "FAIL", str(e)[:200], "replug the Pico and retry"))
    # park safe-idle firmware (with the current helper) before any wiring
    try:
        bench.flash_firmware(SAFE_IDLE_FW)
    except BenchError as e:
        record(StepResult("load safe-idle firmware", "FAIL", str(e)[:200], "run `hands doctor`"))
        return results
    say("!! " + bench.power_cut("unplug before wiring"))
    ask("   Unplug the USB cable now, then press Enter. ")

    # 3. INA219 fixture
    if "ina" in skip:
        record(StepResult("INA219 current sensor", "SKIP"))
    else:
        guide = fixture_guide(inv)
        say(f"== Wire the bench fixture ({len(guide.steps)} steps). USB stays unplugged.")
        for s in guide.steps:
            ask(f"   Step {s.n}: {s.title}\n      {s.detail}\n      [Enter when done] ")
        ask("   Check nothing touches across the red and blue rails. Plug USB back in, then press Enter. ")
        bench.power_on()
        try:
            bench.flash_firmware(SAFE_IDLE_FW)
            rd = bench.read_current(1.0)
            sane = rd.max_ma <= IDLE_MAX_MA and BUS_V_RANGE[0] <= rd.bus_v <= BUS_V_RANGE[1]
            record(
                StepResult(
                    "INA219 current sensor",
                    "PASS" if sane else "FAIL",
                    f"idle {rd.avg_ma:.1f} mA (max {rd.max_ma:.1f}), bus {rd.bus_v:.2f} V",
                    ""
                    if sane
                    else "UNPLUG NOW. Then check wires W2/W7 (3V3 to VIN+), VIN- to the red "
                    "rail, and that no lead bridges the rails",
                )
            )
            if not sane:
                say("!! " + bench.power_cut("INA219 reading out of range"))
                return results
        except BenchError as e:
            record(
                StepResult(
                    "INA219 current sensor",
                    "FAIL",
                    str(e)[:200],
                    "UNPLUG, then check the INA219's VCC/GND/SCL/SDA pins match the labels on YOUR board "
                    "(edit hands/data/starter_kit.yaml if the order differs)",
                )
            )
            say("!! " + bench.power_cut("INA219 not answering"))
            return results

    # 4. logic analyzer
    if "logic" in skip:
        record(StepResult("logic analyzer self-test", "SKIP"))
    else:
        layout = place(Design("bringup-fixture", {}, {}, {}), inv)
        hole = probe_hole(layout, strip_of(pico_hole(inv.pico_pin_number(f"GP{PROBE_GPIO}"))))
        fw = DIAG_TOGGLE_FW.format(n=PROBE_GPIO)
        assert lint_firmware(fw, {"PROBE": f"GP{PROBE_GPIO}"}) == []
        ask(
            f"   Clip the logic analyzer's GND lead to a jumper in the blue (-) rail, and channel D0 to a jumper "
            f"pushed into hole {hole} (next to GP{PROBE_GPIO}). Plug the analyzer into the computer, press Enter. "
        )
        try:
            bench.flash_firmware(fw)
            cap = bench.capture_logic({"D0": hole}, 2.0).channels.get("D0")
            edges = cap.edges if cap else 0
            ok = edges >= 4
            record(
                StepResult(
                    "logic analyzer self-test",
                    "PASS" if ok else "FAIL",
                    f"{edges} edges in 2 s on D0",
                    "" if ok else f"check D0 is in {hole} and GND on the blue rail; run `sigrok-cli --scan`",
                )
            )
        except BenchError as e:
            record(StepResult("logic analyzer self-test", "FAIL", str(e)[:200], "run `hands doctor`"))
        finally:
            try:
                bench.flash_firmware(SAFE_IDLE_FW)
            except BenchError:
                pass

    # 5. camera
    if "camera" in skip:
        record(StepResult("camera", "SKIP"))
    else:
        try:
            snap = bench.camera_snapshot()
            record(StepResult("camera", "PASS", f"photo saved to {snap.path}"))
        except BenchError as e:
            record(StepResult("camera", "FAIL", str(e)[:200], "or use --camera prompt to give phone photos"))

    say("!! " + bench.power_cut("bring-up finished"))
    fails = [r for r in results if r.status == "FAIL"]
    say(
        "== Bring-up "
        + (
            "PASSED: you're ready to build a gadget (`hands run steelers --real`)."
            if not fails
            else f"found {len(fails)} problem(s); fix them and run `hands bringup` again."
        )
    )
    return results
