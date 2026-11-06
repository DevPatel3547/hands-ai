"""Property test: the rule checker is never less strict than SPICE.

Hypothesis builds random designs from the starter kit out of the circuit "motifs" a beginner or a
model would actually write (LED branches with 0-2 resistors in series or parallel, pull resistors,
buttons, transistor-switched buzzers and LEDs, modules on either rail, resistors between pins,
...), with GPIOs drawn from a small pool so that loads pile up on shared pins. For every design
``hands.rules.check_design`` accepts, ngspice runs every GPIO-drive/button state at every
tolerance corner, and no hard limit may be exceeded anywhere.

Every counterexample this test found was fixed in ``hands/rules.py`` and pinned as a regression
test in ``tests/test_spice.py`` (see docs/digital-twin.md, "Findings").

Knobs: ``HANDS_SOUNDNESS_EXAMPLES`` (default 300) and ``HANDS_SOUNDNESS_RANDOM=1`` (explore new
examples instead of the fixed, reproducible CI sequence).
"""

from __future__ import annotations

import os
from collections import defaultdict

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings, target  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from hands.circuit import Design  # noqa: E402
from hands.inventory import load_inventory  # noqa: E402
from hands.rules import check_design  # noqa: E402
from hands.sim.spice import analyze_design, find_ngspice  # noqa: E402

pytestmark = pytest.mark.skipif(
    find_ngspice() is None, reason="ngspice not installed (brew install ngspice / sudo apt install ngspice)"
)

INV = load_inventory()
EXAMPLES = int(os.environ.get("HANDS_SOUNDNESS_EXAMPLES", "300"))
RANDOM = os.environ.get("HANDS_SOUNDNESS_RANDOM") == "1"
GPIO_POOL = ("GP0", "GP1", "GP2", "GP3", "GP14", "GP15", "GP16")
I2C_PAIRS = (("GP4", "GP5"), ("GP0", "GP1"), ("GP2", "GP3"), ("GP5", "GP4"))  # last one is swapped
RES = ("resistor_220", "resistor_1k", "resistor_10k")
LEDS = ("led_red", "led_yellow", "led_green")
STATS: dict[str, int] = defaultdict(int)


class _B:
    """Accumulates a design: part instances, nets, and the GPIO nets they share."""

    def __init__(self) -> None:
        self.parts: dict[str, str] = {}
        self.nets: dict[str, list[str]] = defaultdict(list)
        self.k = 0

    def part(self, t: str) -> str:
        self.k += 1
        name = f"{t.split('_')[0][:3]}{self.k}"
        self.parts[name] = t
        return name

    def net(self) -> str:
        self.k += 1
        return f"N{self.k}"

    def at(self, where: str, ref: str) -> None:
        """Attach ``ref`` to a rail ('3V3', '5V', 'GND'), a GPIO ('GP15') or an internal net."""
        if where.startswith("GP"):
            name = f"G_{where}"
            if f"pico.{where}" not in self.nets[name]:
                self.nets[name].append(f"pico.{where}")
            self.nets[name].append(ref)
        else:
            self.nets[where].append(ref)

    def design(self) -> Design:
        pin_map = {}
        for pins in self.nets.values():
            for p in pins:
                if p.startswith("pico.GP"):
                    pin_map[f"P_{p[5:]}"] = p[5:]
        return Design.from_dict({"name": "hyp", "parts": self.parts, "nets": dict(self.nets), "pin_map": pin_map})


rail = st.sampled_from(("3V3", "5V"))
res = st.sampled_from(RES)


def _resistors(b: _B, draw: st.DrawFn, a: str, z: str, how: str) -> None:
    """Connect ``a`` to ``z`` with resistors: one, two in series, two in parallel, or a bare jumper."""
    if how == "one":
        r = b.part(draw(res))
        b.at(a, f"{r}.1")
        b.at(z, f"{r}.2")
    elif how == "series":
        r1, r2, mid = b.part(draw(res)), b.part(draw(res)), b.net()
        b.at(a, f"{r1}.1")
        b.at(mid, f"{r1}.2")
        b.at(mid, f"{r2}.1")
        b.at(z, f"{r2}.2")
    elif how == "parallel":
        for _ in range(2):
            r = b.part(draw(res))
            b.at(a, f"{r}.1")
            b.at(z, f"{r}.2")


@st.composite
def designs(draw: st.DrawFn) -> Design:
    b = _B()
    # 1-3 GPIOs per design (plus an I2C pair at most), so the state space stays exhaustive
    gpio = st.sampled_from(draw(st.lists(st.sampled_from(GPIO_POOL), min_size=1, max_size=3, unique=True)))
    for _ in range(draw(st.integers(1, 5))):
        m = draw(
            st.sampled_from(
                (
                    "led",
                    "led",
                    "led",
                    "pull",
                    "button",
                    "npn_buzzer",
                    "npn_led",
                    "buzzer_direct",
                    "pin_pin",
                    "oled",
                    "dht",
                    "servo",
                    "rail_res",
                )
            )
        )
        if m == "led":
            src = draw(st.one_of(gpio, rail))
            sink = draw(st.one_of(st.just("GND"), gpio))
            led = b.part(draw(st.sampled_from(LEDS)))
            how = draw(st.sampled_from(("one", "one", "series", "parallel", "none")))
            side = draw(st.sampled_from(("anode", "cathode")))
            mid = b.net()
            if how == "none":
                b.at(src, f"{led}.anode")
                b.at(sink, f"{led}.cathode")
            elif side == "anode":
                _resistors(b, draw, src, mid, how)
                b.at(mid, f"{led}.anode")
                b.at(sink, f"{led}.cathode")
            else:
                b.at(src, f"{led}.anode")
                b.at(mid, f"{led}.cathode")
                _resistors(b, draw, mid, sink, how)
        elif m == "pull":
            r = b.part(draw(res))
            b.at(draw(gpio), f"{r}.1")
            b.at(draw(st.sampled_from(("3V3", "GND", "5V"))), f"{r}.2")
        elif m == "button" and list(b.parts.values()).count("button") < 2:
            sw = b.part("button")
            g = draw(gpio)
            other = draw(st.sampled_from(("GND", "3V3")))
            if draw(st.booleans()):
                b.at(g, f"{sw}.a")
            else:
                mid = b.net()
                _resistors(b, draw, g, mid, "one")
                b.at(mid, f"{sw}.a")
            b.at(other, f"{sw}.b")
        elif m in ("npn_buzzer", "npn_led"):
            q = b.part("npn")
            g, base, coll = draw(gpio), b.net(), b.net()
            if draw(st.integers(0, 5)):
                _resistors(b, draw, g, base, draw(st.sampled_from(("one", "one", "parallel"))))
                b.at(base, f"{q}.b")
            else:
                b.at(g, f"{q}.b")
            b.at("GND", f"{q}.e")
            b.at(coll, f"{q}.c")
            top = draw(rail)
            if m == "npn_buzzer":
                bz = b.part("buzzer_active")
                b.at(top, f"{bz}.plus")
                b.at(coll, f"{bz}.minus")
            else:
                led, mid = b.part(draw(st.sampled_from(LEDS))), b.net()
                b.at(top, f"{led}.anode")
                b.at(mid, f"{led}.cathode")
                _resistors(b, draw, mid, coll, draw(st.sampled_from(("one", "parallel"))))
        elif m == "buzzer_direct":
            bz = b.part("buzzer_active")
            b.at(draw(gpio), f"{bz}.plus")
            b.at("GND", f"{bz}.minus")
        elif m == "pin_pin":
            g1, g2 = draw(gpio), draw(gpio)
            if g1 != g2:
                _resistors(b, draw, g1, g2, "one")
        elif m == "oled" and "oled" not in b.parts.values():
            o = b.part("oled")
            sda, scl = draw(st.sampled_from(I2C_PAIRS))
            b.at(sda, f"{o}.sda")
            b.at(scl, f"{o}.scl")
            b.at(draw(rail), f"{o}.vcc")
            b.at("GND", f"{o}.gnd")
        elif m == "dht" and "dht22" not in b.parts.values():
            d = b.part("dht22")
            g = draw(gpio)
            b.at(g, f"{d}.data")
            b.at(draw(rail), f"{d}.vcc")
            b.at("GND", f"{d}.gnd")
            if draw(st.booleans()):
                r = b.part("resistor_10k")
                b.at(g, f"{r}.1")
                b.at("3V3", f"{r}.2")
        elif m == "servo" and "servo" not in b.parts.values():
            s = b.part("servo")
            b.at(draw(gpio), f"{s}.sig")
            b.at(draw(rail), f"{s}.vplus")
            b.at("GND", f"{s}.gnd")
        elif m == "rail_res":
            _resistors(b, draw, draw(rail), "GND", draw(st.sampled_from(("one", "parallel"))))
    return b.design()


@settings(
    max_examples=EXAMPLES,
    deadline=None,
    derandomize=not RANDOM,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(designs())
def test_rules_never_less_strict_than_spice(d: Design) -> None:
    STATS["examples"] += 1
    rep = check_design(d, INV)
    if not rep.ok:
        return
    STATS["accepted"] += 1
    lim = INV.limits
    # steer the search toward designs the rules accept close to their limits
    if rep.gpio_current_ma:
        target(max(rep.gpio_current_ma.values()) / lim["gpio_max_ma"], label="gpio share of limit")
        target(sum(rep.gpio_current_ma.values()) / lim["gpio_total_max_ma"], label="gpio total share")
    target(rep.rail_current_ma.get("usb_total", 0.0) / lim["vbus_max_ma"], label="usb share")
    sp = analyze_design(d, INV, max_copies=8192)
    assert sp.exhaustive, f"state space was sampled ({sp.states_run}/{sp.states_total}); bound the generator"
    assert sp.ok, (
        "rules accepted a design that SPICE says exceeds a hard limit:\n"
        + rep.render()
        + "\n"
        + sp.render()
        + "\n"
        + repr(d.to_dict())
    )


def test_soundness_run_was_not_vacuous() -> None:
    """The property only means something if the rules accepted a fair share of the random designs."""
    if not STATS["examples"]:
        pytest.skip("run together with test_rules_never_less_strict_than_spice")
    assert STATS["accepted"] >= 0.15 * STATS["examples"], dict(STATS)
