"""Deterministic electrical safety / sanity checker.

Nothing in here asks an LLM. Every rule is plain code over the netlist and the
inventory facts, so the same design always gets the same verdict.

The checker is a **hard gate**: :func:`require_safe` raises
:class:`UnsafeDesignError` for any design with an ``error`` finding, and the
breadboard placer (:func:`hands.layout.place`), the wiring guide and the
engineer's power-on stage all call it. There is no code path from a design to
a wiring step or to USB power that skips it.

Every limit comes from :func:`hands.safety.effective_limits` (YAML values
clamped to hard ceilings). Rule numbers and their datasheet sources are listed
in ``docs/safety.md``.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from .circuit import (
    FIXTURE_PARTS,
    RAIL_NETS,
    RESERVED_GPIO,
    Design,
    PinRef,
    gpio_number,
    validate_structure,
    with_fixture,
)
from .inventory import Inventory, PartType

V_GPIO = 3.3
POWER_PINS = {"pico.GND": 0.0, "pico.AGND": 0.0, "pico.3V3": 3.3, "pico.VBUS": 5.0, "pico.VSYS": 5.0}
LED_DIRECT_MA = 40.0  # stand-in for "unlimited" when an LED/base has no resistor
TWO_PIN_KINDS = ("led", "resistor", "buzzer", "button")

#: Every rule id the checker can emit, in the order they run.
RULE_IDS = (
    "structure",
    "inventory",
    "pin_conflict",
    "power_short",
    "gpio_short",
    "shorted_part",
    "unconnected_pin",
    "led_resistor",
    "led_polarity",
    "led_current",
    "gpio_current",
    "transistor",
    "buzzer_polarity",
    "servo_power",
    "module_power",
    "i2c",
    "dht",
    "overvoltage",
    "resistor_power",
    "rail_budget",
)


@dataclass
class Finding:
    rule: str
    severity: str  # "error" | "warning"
    message: str
    refs: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.rule}: {self.message}"


@dataclass
class CheckReport:
    findings: list[Finding]
    gpio_current_ma: dict[str, float]
    rail_current_ma: dict[str, float]
    digest: str = ""

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        if not self.findings:
            return "Rule check: PASS (no findings)"
        head = "Rule check: " + ("PASS with warnings" if self.ok else f"FAIL ({len(self.errors)} error(s))")
        return "\n".join([head] + ["  " + str(f) for f in self.findings])


class UnsafeDesignError(ValueError):
    """A design failed the deterministic rule check. Nothing may be wired or powered."""

    def __init__(self, report: CheckReport):
        self.report = report
        super().__init__("design fails the safety rules:\n" + report.render())


def design_digest(design: Design) -> str:
    """Stable fingerprint of a gadget design (what the rule check approved)."""
    blob = json.dumps({"parts": design.parts, "nets": design.nets, "pin_map": design.pin_map}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def require_safe(design: Design, inv: Inventory) -> CheckReport:
    """The hard gate. Returns the passing report or raises :class:`UnsafeDesignError`."""
    if design.fixture:
        raise UnsafeDesignError(
            CheckReport([Finding("structure", "error", "pass the gadget design, not a fixture-merged one")], {}, {})
        )
    rep = check_design(design, inv)
    if not rep.ok:
        raise UnsafeDesignError(rep)
    return rep


# ----------------------------------------------------------------- context ---

Range = tuple[float, float]


class _Ctx:
    def __init__(self, design: Design, inv: Inventory):
        self.d = design
        self.inv = inv
        self.lim = inv.limits
        self.parts = design.all_parts()
        self.net_of: dict[str, str] = {}
        for net, pins in design.nets.items():
            for p in pins:
                self.net_of.setdefault(p, net)

    def kind(self, inst: str) -> str | None:
        t = self.parts.get(inst)
        return self.inv.parts[t].kind if t in self.inv.parts else None

    def ptype(self, inst: str) -> PartType | None:
        return self.inv.parts.get(self.parts.get(inst, ""))

    def fact(self, inst: str, key: str, default: float) -> float:
        pt = self.ptype(inst)
        return float(pt.get(key, default)) if pt else default

    def pins(self, net: str | None) -> list[str]:
        return self.d.nets.get(net, []) if net else []

    def gpios(self, net: str | None) -> list[str]:
        return [PinRef.parse(p).pin for p in self.pins(net) if p.startswith("pico.GP")]

    def voltage(self, net: str | None) -> float | None:
        """Fixed voltage of a power net, None for signal nets, NaN for a supply short."""
        if net is None:
            return None
        vs = {POWER_PINS[p] for p in self.pins(net) if p in POWER_PINS}
        if net == "GND":
            vs.add(0.0)
        elif net in ("3V3", "V3_SRC"):
            vs.add(3.3)
        elif net == "5V":
            vs.add(5.0)
        if len(vs) == 1:
            return vs.pop()
        if len(vs) > 1:
            return float("nan")
        return None

    def vrange(self, net: str | None) -> Range | None:
        """Voltage range a net can take: fixed rails, GPIO 0..3.3 V, else unknown."""
        v = self.voltage(net)
        if v is not None and v == v:
            return (v, v)
        if self.gpios(net):
            return (0.0, V_GPIO)
        return None

    def is_gnd(self, net: str | None) -> bool:
        return self.voltage(net) == 0.0

    def other_pin(self, ref: str) -> str | None:
        """For a 2-terminal part, the pin on the other end."""
        r = PinRef.parse(ref)
        t = self.ptype(r.inst)
        if not t or len(t.pins) != 2:
            return None
        other = [p.name for p in t.pins if p.name != r.pin]
        return f"{r.inst}.{other[0]}" if other else None

    def far_net(self, ref: str) -> str | None:
        o = self.other_pin(ref)
        return self.net_of.get(o) if o else None

    def across_resistor(self, net: str) -> list[tuple[str, float]]:
        """Nets reachable from ``net`` through exactly one resistor, with ohms."""
        out = []
        for p in self.pins(net):
            r = PinRef.parse(p)
            if self.kind(r.inst) == "resistor":
                on = self.far_net(p)
                if on:
                    out.append((on, self.fact(r.inst, "ohms", 0.0)))
        return out

    def side_class(self, net: str | None) -> str:
        """Classify what a net is driven by: gnd / supply / gpio / passive / none."""
        if net is None:
            return "none"
        v = self.voltage(net)
        if v == 0.0:
            return "gnd"
        if v is not None:
            return "supply"
        if self.gpios(net):
            return "gpio"
        return "passive"

    def led_pins(self, inst: str) -> tuple[str | None, str | None]:
        return self.net_of.get(f"{inst}.anode"), self.net_of.get(f"{inst}.cathode")


def check_design(design: Design, inv: Inventory) -> CheckReport:
    """Run every rule. ``design`` is the gadget-only design; the bench fixture is merged in."""
    findings: list[Finding] = []
    gadget = design
    full = with_fixture(design)

    for e in validate_structure(gadget, inv, gadget=True):
        findings.append(Finding("structure", "error", e))
    if any(f.rule == "structure" for f in findings):
        return CheckReport(findings, {}, {}, design_digest(gadget))
    # Duplicated pins after the fixture merge = conflict with a bench-reserved pin
    for e in validate_structure(full, inv, gadget=False):
        if "appears on two nets" in e:
            findings.append(Finding("pin_conflict", "error", e + " (pin is reserved by the bench fixture)"))

    c = _Ctx(full, inv)
    findings += _inventory_qty(gadget, inv)
    findings += _pin_rules(gadget, c, inv)
    findings += _power_shorts(c)
    findings += _gpio_shorts(c)
    findings += _shorted_parts(c)
    findings += _floating(c, inv)
    findings += _led_rules(c)
    findings += _led_current(c)
    gpio_ma, f_cur = _gpio_current(c)
    findings += f_cur
    findings += _transistor_rules(c)
    findings += _buzzer_rules(c)
    findings += _servo_rules(c)
    findings += _module_power(c)
    findings += _i2c_rules(c, inv)
    findings += _dht_rules(c)
    findings += _overvoltage(c)
    findings += _resistor_power(c)
    rail_ma, f_rail = _rail_budget(c, gpio_ma)
    findings += f_rail
    seen, uniq = set(), []
    for f in findings:
        k = (f.rule, f.message)
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return CheckReport(uniq, gpio_ma, rail_ma, design_digest(gadget))


# ------------------------------------------------------------------- rules ---


def _inventory_qty(d: Design, inv: Inventory) -> list[Finding]:
    out = []
    counts = Counter(d.parts.values())
    for t, n in counts.items():
        if t in FIXTURE_PARTS.values():
            n += 1  # the fixture already uses one
        if t in inv.parts and n > inv.parts[t].qty:
            out.append(Finding("inventory", "error", f"design needs {n} x {t} but inventory has {inv.parts[t].qty}"))
    return out


def _pin_rules(gadget: Design, c: _Ctx, inv: Inventory) -> list[Finding]:
    out = []
    used = gadget.gpio_nets()
    for gp in used:
        if gp in RESERVED_GPIO:
            out.append(Finding("pin_conflict", "error", f"{gp} is reserved for the {RESERVED_GPIO[gp]}", [gp]))
        if gpio_number(gp) in inv.pico_internal_gpio:
            out.append(Finding("pin_conflict", "error", f"{gp} is internal on the Pico W (not on the header)", [gp]))
    seen: dict[str, str] = {}
    for role, gp in gadget.pin_map.items():
        if gp in seen:
            out.append(Finding("pin_conflict", "error", f"pin_map assigns {gp} to both {seen[gp]} and {role}", [gp]))
        seen[gp] = role
        if gp not in used:
            out.append(
                Finding("pin_conflict", "error", f"pin_map[{role}] = {gp} but {gp} is not connected to anything", [gp])
            )
    for gp in used:
        if gp not in gadget.pin_map.values():
            out.append(
                Finding(
                    "pin_conflict",
                    "error",
                    f"{gp} is wired but missing from pin_map; the firmware linter could not check it",
                    [gp],
                )
            )
    return out


def _power_shorts(c: _Ctx) -> list[Finding]:
    out = []
    for net in c.d.nets:
        v = c.voltage(net)
        if v is not None and v != v:  # NaN
            out.append(
                Finding("power_short", "error", f"net {net} ties different supplies/GND together (dead short)", [net])
            )
    return out


def _gpio_shorts(c: _Ctx) -> list[Finding]:
    out = []
    for net in c.d.nets:
        g = c.gpios(net)
        if not g:
            continue
        v = c.voltage(net)
        if v is not None:
            what = "GND" if v == 0 else f"{v:g} V supply"
            out.append(
                Finding(
                    "gpio_short",
                    "error",
                    f"{', '.join(g)} is wired directly to {what} on net {net}: driving the pin would short it",
                    g,
                )
            )
        if len(g) > 1:
            out.append(
                Finding("gpio_short", "error", f"{', '.join(g)} are tied together on net {net} (output contention)", g)
            )
    return out


def _shorted_parts(c: _Ctx) -> list[Finding]:
    """Two-terminal parts with both legs on one net, and buttons that would short two supplies."""
    out = []
    for inst in c.parts:
        k = c.kind(inst)
        if k not in TWO_PIN_KINDS:
            continue
        pt = c.ptype(inst)
        assert pt is not None
        a, b = (c.net_of.get(f"{inst}.{p.name}") for p in pt.pins)
        if a is not None and a == b:
            out.append(
                Finding("shorted_part", "error", f"both legs of {inst} are on net {a}: it is shorted out", [inst])
            )
            continue
        if k == "button":
            va, vb = c.voltage(a), c.voltage(b)
            if va is not None and vb is not None and va == va and vb == vb and va != vb:
                out.append(
                    Finding(
                        "shorted_part",
                        "error",
                        f"{inst} connects {a} and {b}: pressing it would short the supply. "
                        f"Wire buttons between a GPIO and GND.",
                        [inst],
                    )
                )
    return out


def _floating(c: _Ctx, inv: Inventory) -> list[Finding]:
    out = []
    for inst, t in c.parts.items():
        if inst in FIXTURE_PARTS:
            continue
        pt = inv.parts[t]
        for p in pt.pins:
            ref = f"{inst}.{p.name}"
            if ref not in c.net_of:
                out.append(Finding("unconnected_pin", "error", f"{ref} ({p.role}) is not connected", [ref]))
            elif len(c.pins(c.net_of[ref])) < 2:
                out.append(
                    Finding(
                        "unconnected_pin",
                        "error",
                        f"{ref} is alone on net {c.net_of[ref]} (connects to nothing)",
                        [ref],
                    )
                )
    return out


def _isolated_series(c: _Ctx, inst: str, net: str) -> bool:
    """Only this part plus resistor pins on the net -> a resistor is in series."""
    others = [p for p in c.pins(net) if not p.startswith(inst + ".")]
    return bool(others) and all(c.kind(PinRef.parse(p).inst) == "resistor" for p in others)


def _led_rules(c: _Ctx) -> list[Finding]:
    out = []
    for inst in c.parts:
        if c.kind(inst) != "led":
            continue
        a, k = c.led_pins(inst)
        if a is None or k is None:
            continue
        if not (_isolated_series(c, inst, a) or _isolated_series(c, inst, k)):
            out.append(
                Finding(
                    "led_resistor",
                    "error",
                    f"{inst} has no series resistor: nothing limits its current (anode net {a}, cathode net {k})",
                    [inst],
                )
            )

        def eff(net: str) -> str:
            cls = c.side_class(net)
            if cls == "passive":
                for n2, _ in c.across_resistor(net):
                    cls2 = c.side_class(n2)
                    if cls2 != "passive":
                        return cls2
            return cls

        ea, ek = eff(a), eff(k)
        if ea == "gnd" and ek in ("gpio", "supply"):
            out.append(
                Finding(
                    "led_polarity",
                    "error",
                    f"{inst} is backwards: anode goes toward GND and cathode toward {ek}. Swap its legs.",
                    [inst],
                )
            )
    return out


def _series_r(c: _Ctx, inst: str, net: str) -> tuple[float, Range | None] | None:
    """If ``net`` holds only ``inst`` and one resistor: (ohms, voltage range of the resistor's far end)."""
    if not _isolated_series(c, inst, net):
        return None
    rs = [p for p in c.pins(net) if not p.startswith(inst + ".")]
    if len(rs) != 1:
        return None
    r = PinRef.parse(rs[0]).inst
    return c.fact(r, "ohms", 0.0), c.vrange(c.far_net(rs[0]))


def led_worst_ma(c: _Ctx, inst: str) -> float | None:
    """Worst-case LED current (minimum Vf), or None if the circuit around it is not a simple series path."""
    a, k = c.led_pins(inst)
    vf = c.fact(inst, "vf_min", c.fact(inst, "vf", 2.0))
    ra, rk = c.vrange(a), c.vrange(k)
    sa, sk = (_series_r(c, inst, a) if a else None), (_series_r(c, inst, k) if k else None)
    if sa and rk:
        ohms, far = sa
        if far and ohms > 0:
            return max(far[1] - rk[0] - vf, 0.0) / ohms * 1000
    if sk and ra:
        ohms, far = sk
        if far and ohms > 0:
            return max(ra[1] - far[0] - vf, 0.0) / ohms * 1000
    return None


def _led_current(c: _Ctx) -> list[Finding]:
    out = []
    for inst in c.parts:
        if c.kind(inst) != "led":
            continue
        i = led_worst_ma(c, inst)
        imax = c.fact(inst, "i_max_ma", 20.0)
        if i is not None and i > imax:
            out.append(
                Finding(
                    "led_current",
                    "error",
                    f"{inst} would carry ~{i:.1f} mA (> its {imax:g} mA rating). Use a bigger series resistor.",
                    [inst],
                )
            )
    return out


def _gpio_load_ma(c: _Ctx, net: str) -> tuple[float, list[str]]:
    """Estimated worst-case source/sink current of the GPIO on ``net``."""
    total, why = 0.0, []
    for p in c.pins(net):
        r = PinRef.parse(p)
        if r.inst == "pico":
            continue
        k = c.kind(r.inst)
        if k == "resistor":
            ohms = c.fact(r.inst, "ohms", 0.0)
            on = c.far_net(p)
            if on is None or ohms <= 0:
                continue
            v = c.voltage(on)
            if v is not None and v == v:
                i = (V_GPIO if v == 0 else v) / ohms * 1000  # pin driven to the opposite level
                total += i
                why.append(f"{r.inst} to {'GND' if v == 0 else on} ~{i:.1f} mA")
                continue
            for q in c.pins(on):
                qr = PinRef.parse(q)
                qk = c.kind(qr.inst)
                if qk == "led":
                    vf = c.fact(qr.inst, "vf_min", c.fact(qr.inst, "vf", 2.0))
                    other_v = c.voltage(c.far_net(q))
                    if qr.pin == "anode":  # pin sources current into the LED
                        i = max(V_GPIO - vf - (other_v if other_v is not None else 0.0), 0) / ohms * 1000
                    else:  # pin sinks current from an LED whose anode sits on a rail
                        i = max((other_v if other_v is not None else V_GPIO) - vf, 0) / ohms * 1000
                    total += i
                    why.append(f"{r.inst}+{qr.inst} ~{i:.1f} mA")
                elif qk == "npn" and qr.pin == "b":
                    i = (V_GPIO - c.fact(qr.inst, "vbe", 0.7)) / ohms * 1000
                    total += i
                    why.append(f"{qr.inst} base via {r.inst} ~{i:.1f} mA")
                elif qk == "button":
                    ov = c.voltage(c.far_net(q))
                    if ov is not None and ov == ov:
                        i = (V_GPIO if ov == 0 else ov) / ohms * 1000
                        total += i
                        why.append(f"{r.inst} via pressed {qr.inst} ~{i:.1f} mA")
        elif k == "led":
            total += LED_DIRECT_MA
            why.append(f"{r.inst} directly on pin (unlimited, ~40+ mA)")
        elif k == "buzzer":
            i = c.fact(r.inst, "current_ma", 25.0)
            total += i
            why.append(f"{r.inst} buzzer directly on pin ~{i:.0f} mA")
        elif k == "npn" and r.pin == "b":
            total += LED_DIRECT_MA
            why.append(f"{r.inst} base with no resistor (unlimited)")
    return total, why


def _gpio_current(c: _Ctx) -> tuple[dict[str, float], list[Finding]]:
    out: list[Finding] = []
    per: dict[str, float] = {}
    warn, mx, tot = c.lim["gpio_warn_ma"], c.lim["gpio_max_ma"], c.lim["gpio_total_max_ma"]
    for net in c.d.nets:
        for g in c.gpios(net):
            ma, why = _gpio_load_ma(c, net)
            per[g] = round(ma, 2)
            if ma > mx:
                out.append(
                    Finding(
                        "gpio_current",
                        "error",
                        f"{g} would carry ~{ma:.1f} mA (> {mx:g} mA max): {'; '.join(why)}. "
                        f"Use a transistor or a bigger resistor.",
                        [g],
                    )
                )
            elif ma > warn:
                out.append(
                    Finding(
                        "gpio_current", "warning", f"{g} carries ~{ma:.1f} mA (above the comfortable {warn:g} mA)", [g]
                    )
                )
    s = sum(per.values())
    if s > tot:
        out.append(Finding("gpio_current", "error", f"total GPIO current ~{s:.1f} mA exceeds {tot:g} mA"))
    return per, out


def _collector_load_ma(c: _Ctx, coll: str) -> float:
    total = 0.0
    for p in c.pins(coll):
        r = PinRef.parse(p)
        k = c.kind(r.inst)
        if k == "buzzer":
            total += c.fact(r.inst, "current_ma", 25.0)
        elif k == "led":
            total += led_worst_ma(c, r.inst) or c.fact(r.inst, "i_max_ma", 20.0)
        elif k == "resistor":
            v = c.voltage(c.far_net(p))
            ohms = c.fact(r.inst, "ohms", 0.0)
            if v and ohms > 0:
                total += v / ohms * 1000
    return total


def _transistor_rules(c: _Ctx) -> list[Finding]:
    out = []
    for inst in c.parts:
        if c.kind(inst) != "npn":
            continue
        e, b, col = (c.net_of.get(f"{inst}.{p}") for p in ("e", "b", "c"))
        if e and not c.is_gnd(e):
            out.append(
                Finding(
                    "transistor", "error", f"{inst} emitter must go to GND for a low-side switch (is on {e})", [inst]
                )
            )
        if b and c.gpios(b):
            out.append(
                Finding(
                    "transistor",
                    "error",
                    f"{inst} base is wired straight to {c.gpios(b)[0]}; it needs a ~1 kohm resistor",
                    [inst],
                )
            )
        if b and c.voltage(b) is not None:
            out.append(Finding("transistor", "error", f"{inst} base is wired straight to a power net ({b})", [inst]))
        if col and c.voltage(col) is not None:
            out.append(
                Finding(
                    "transistor",
                    "error",
                    f"{inst} collector is wired straight to {col}: when it switches on nothing limits the current. "
                    f"Put the load (buzzer) between the supply and the collector.",
                    [inst],
                )
            )
            continue
        if not col:
            continue
        load = _collector_load_ma(c, col)
        icmax = c.fact(inst, "ic_max_ma", 600.0)
        if load > icmax:
            out.append(Finding("transistor", "error", f"{inst} would switch ~{load:.0f} mA (> {icmax:g} mA)", [inst]))
        # base drive: want forced beta <= 10 for a hard-saturated switch
        rb = [(n2, ohms) for n2, ohms in c.across_resistor(b) if c.gpios(n2)] if b else []
        if rb and load > 0:
            ib = (V_GPIO - c.fact(inst, "vbe", 0.7)) / rb[0][1] * 1000
            if ib < load / 10:
                out.append(
                    Finding(
                        "transistor",
                        "warning",
                        f"{inst} base current ~{ib:.2f} mA may not fully switch ~{load:.0f} mA "
                        f"(aim for at least {load / 10:.1f} mA; use a smaller base resistor, e.g. 1 kohm)",
                        [inst],
                    )
                )
    return out


def _buzzer_rules(c: _Ctx) -> list[Finding]:
    out = []
    for inst in c.parts:
        if c.kind(inst) != "buzzer":
            continue
        plus, minus = c.net_of.get(f"{inst}.plus"), c.net_of.get(f"{inst}.minus")
        if plus and c.is_gnd(plus):
            out.append(Finding("buzzer_polarity", "error", f"{inst} '+' leg is on GND; it is backwards", [inst]))
        if minus and c.voltage(minus) not in (None, 0.0):
            out.append(
                Finding("buzzer_polarity", "error", f"{inst} '-' leg is on a supply rail; it is backwards", [inst])
            )
    return out


def _servo_rules(c: _Ctx) -> list[Finding]:
    out = []
    for inst in c.parts:
        if c.kind(inst) != "servo":
            continue
        vp = c.net_of.get(f"{inst}.vplus")
        v = c.voltage(vp)
        if v == 3.3:
            out.append(
                Finding(
                    "servo_power",
                    "error",
                    f"{inst} is powered from 3V3: an SG90 needs ~5 V and can pull 250-650 mA, "
                    f"which overloads the Pico's 3V3 regulator. Put its red wire on the 5V (VBUS) rail.",
                    [inst],
                )
            )
        elif v != 5.0 and vp is not None:
            out.append(
                Finding("servo_power", "error", f"{inst} red wire must go to the 5V (VBUS) rail (is on {vp})", [inst])
            )
        g = c.net_of.get(f"{inst}.gnd")
        if g and not c.is_gnd(g):
            out.append(Finding("servo_power", "error", f"{inst} brown wire must share GND with the Pico", [inst]))
        s = c.net_of.get(f"{inst}.sig")
        if s and not c.gpios(s):
            out.append(Finding("servo_power", "error", f"{inst} signal wire is not on a GPIO", [inst]))
    return out


def _module_power(c: _Ctx) -> list[Finding]:
    out = []
    vmax_usb = c.lim["supply_max_v"]
    for inst in c.parts:
        pt = c.ptype(inst)
        if pt is None or inst in FIXTURE_PARTS:
            continue
        if float(pt.get("v_min", 0)) > vmax_usb:
            out.append(
                Finding(
                    "module_power",
                    "error",
                    f"{inst} needs at least {pt.get('v_min')} V; Hands never goes above USB 5 V",
                    [inst],
                )
            )
        for p in pt.pins:
            ref = f"{inst}.{p.name}"
            net = c.net_of.get(ref)
            if net is None:
                continue
            v = c.voltage(net)
            if p.role == "power_in":
                if v is None or v == 0.0:
                    out.append(
                        Finding("module_power", "error", f"{ref} must connect to 3V3 or 5V (is on {net})", [ref])
                    )
                elif v == v:
                    lo, hi = float(pt.get("v_min", 0)), float(pt.get("v_max", 99))
                    if not (lo <= v <= hi):
                        out.append(
                            Finding(
                                "module_power",
                                "error",
                                f"{ref} on {v:g} V is outside the part's {lo:g}-{hi:g} V range",
                                [ref],
                            )
                        )
            if p.role == "gnd" and v != 0.0:
                out.append(Finding("module_power", "error", f"{ref} must connect to GND (is on {net})", [ref]))
    return out


def _i2c_rules(c: _Ctx, inv: Inventory) -> list[Finding]:
    out = []
    buses: dict[tuple[str, str, str], list[str]] = {}
    for inst in c.parts:
        pt = c.ptype(inst)
        if pt is None or "i2c_addr" not in pt.facts or inst in FIXTURE_PARTS:
            continue
        sda_ref = next((f"{inst}.{p.name}" for p in pt.pins if p.role == "i2c_sda"), None)
        scl_ref = next((f"{inst}.{p.name}" for p in pt.pins if p.role == "i2c_scl"), None)
        sda = c.net_of.get(sda_ref) if sda_ref else None
        scl = c.net_of.get(scl_ref) if scl_ref else None
        if sda is None or scl is None:
            continue
        gs, gc = c.gpios(sda), c.gpios(scl)
        if len(gs) != 1 or len(gc) != 1:
            out.append(Finding("i2c", "error", f"{inst} SDA/SCL must each go to exactly one Pico GPIO", [inst]))
            continue
        ns, nc = gpio_number(gs[0]), gpio_number(gc[0])
        ctrl = None
        for name, m in inv.pico_i2c.items():
            if ns in m["sda"] and nc in m["scl"]:
                ctrl = name
        if ctrl is None:
            swapped = any(nc in m["sda"] and ns in m["scl"] for m in inv.pico_i2c.values())
            hint = " (SDA and SCL look swapped)" if swapped else ""
            out.append(
                Finding(
                    "i2c",
                    "error",
                    f"{inst}: {gs[0]} (SDA) + {gc[0]} (SCL) is not a valid I2C pin pair on one controller{hint}",
                    [inst],
                )
            )
            continue
        buses.setdefault((ctrl, sda, scl), []).append(inst)
    ctrl_count = Counter(k[0] for k in buses)
    for ctrl, n in ctrl_count.items():
        if n > 1:
            out.append(Finding("i2c", "error", f"{ctrl} is used with two different pin pairs"))
    for (ctrl, sda, scl), insts in buses.items():
        addrs = Counter(int(c.fact(i, "i2c_addr", 0)) for i in insts)
        for a, n in addrs.items():
            if n > 1:
                out.append(Finding("i2c", "error", f"two devices at address 0x{a:02X} on {ctrl}", insts))
        has_pu = any((c.ptype(i) or PartType("", "", "", 0, "")).get("pullup_onboard") for i in insts)
        for line_net in (sda, scl):
            if not has_pu and not any(c.voltage(n2) == 3.3 for n2, _ in c.across_resistor(line_net)):
                out.append(Finding("i2c", "error", f"{ctrl} line {line_net} has no pull-up resistor to 3V3", insts))
                break
    return out


def _dht_rules(c: _Ctx) -> list[Finding]:
    out = []
    for inst in c.parts:
        if c.kind(inst) != "dht22":
            continue
        data = c.net_of.get(f"{inst}.data")
        if data and not c.gpios(data):
            out.append(Finding("dht", "error", f"{inst} data pin is not on a GPIO", [inst]))
        pt = c.ptype(inst)
        if data and pt is not None and not pt.get("pullup_onboard"):
            if not any(c.voltage(n2) == 3.3 for n2, _ in c.across_resistor(data)):
                out.append(Finding("dht", "error", f"{inst} data line needs a 10 kohm pull-up to 3V3", [inst]))
    return out


def _overvoltage(c: _Ctx) -> list[Finding]:
    """No path may put 5 V on a GPIO: not through a chain of passive parts (resistor, LED, buzzer,
    button), and not through a module's on-board pull-ups."""
    out = []
    for net in c.d.nets:
        g = c.gpios(net)
        if not g:
            continue
        # breadth-first over two-terminal parts; fixed-voltage nets other than 5 V stop the search
        frontier, seen_nets, via = [net], {net}, {net: ""}
        for _depth in range(4):
            nxt = []
            for n in frontier:
                for p in c.pins(n):
                    r = PinRef.parse(p)
                    if c.kind(r.inst) not in TWO_PIN_KINDS:
                        continue
                    n2 = c.far_net(p)
                    if not n2 or n2 in seen_nets:
                        continue
                    seen_nets.add(n2)
                    via[n2] = (via[n] + " -> " if via[n] else "") + r.inst
                    v2 = c.voltage(n2)
                    if v2 == 5.0:
                        out.append(
                            Finding(
                                "overvoltage",
                                "error",
                                f"{g[0]} is connected to 5 V through {via[n2]}; GPIO max is 3.3 V (not 5 V tolerant)",
                                g,
                            )
                        )
                    elif v2 is None and not c.gpios(n2):
                        nxt.append(n2)
            frontier = nxt
    for inst in c.parts:
        pt = c.ptype(inst)
        if pt is None or inst in FIXTURE_PARTS or not pt.get("pullup_onboard"):
            continue
        vcc = next((c.net_of.get(f"{inst}.{p.name}") for p in pt.pins if p.role == "power_in"), None)
        if c.voltage(vcc) != 5.0:
            continue
        sig_gpios = [
            gp
            for p in pt.pins
            if p.role in ("signal", "i2c_sda", "i2c_scl")
            for gp in c.gpios(c.net_of.get(f"{inst}.{p.name}"))
        ]
        if sig_gpios:
            out.append(
                Finding(
                    "overvoltage",
                    "error",
                    f"{inst} is powered from 5V and has on-board pull-ups to VCC: that puts 5 V on "
                    f"{'/'.join(sig_gpios)} (not 5 V tolerant). Power it from 3V3.",
                    [inst],
                )
            )
    return out


def _resistor_power(c: _Ctx) -> list[Finding]:
    out = []
    pmax = c.lim["resistor_max_w"]
    for inst in c.parts:
        if c.kind(inst) != "resistor":
            continue
        ohms = c.fact(inst, "ohms", 0.0)
        if ohms <= 0:
            out.append(Finding("resistor_power", "error", f"{inst} has no valid resistance in the inventory", [inst]))
            continue
        ra, rb = c.vrange(c.net_of.get(f"{inst}.1")), c.vrange(c.net_of.get(f"{inst}.2"))
        if ra and rb:
            dv = max(abs(ra[1] - rb[0]), abs(rb[1] - ra[0]))
            p = dv * dv / ohms
            if p > pmax:
                out.append(
                    Finding(
                        "resistor_power",
                        "error",
                        f"{inst} would dissipate ~{p * 1000:.0f} mW (> {pmax * 1000:.0f} mW)",
                        [inst],
                    )
                )
    return out


def _rail_budget(c: _Ctx, gpio_ma: dict[str, float]) -> tuple[dict[str, float], list[Finding]]:
    out: list[Finding] = []
    rails = {"3V3": 0.0, "5V": 0.0}
    counted_leds: set[str] = set()
    for inst in c.parts:
        pt = c.ptype(inst)
        if pt is None or inst in FIXTURE_PARTS:
            continue
        if pt.kind == "led":
            a, _k = c.led_pins(inst)
            src = c.voltage(a)
            if src is None:  # anode through a resistor to a rail?
                sr = _series_r(c, inst, a) if a else None
                far = sr[1] if sr else None
                src = far[0] if far and far[0] == far[1] and far[0] > 0 else None
            if src in (3.3, 5.0):
                rails["3V3" if src == 3.3 else "5V"] += led_worst_ma(c, inst) or 0.0
                counted_leds.add(inst)
            continue
        for p in pt.pins:
            if p.role in ("power_in", "anode"):
                v = c.voltage(c.net_of.get(f"{inst}.{p.name}"))
                key = "3V3" if v == 3.3 else "5V" if v == 5.0 else None
                if key is None:
                    continue
                if pt.kind == "servo":
                    rails[key] += float(pt.get("current_moving_ma", 250))
                else:
                    rails[key] += float(pt.get("current_ma", 0))
    for inst in c.parts:  # resistors straight from a rail to GND (pull-ups, dividers)
        if c.kind(inst) != "resistor":
            continue
        va, vb = c.voltage(c.net_of.get(f"{inst}.1")), c.voltage(c.net_of.get(f"{inst}.2"))
        if va is not None and vb is not None and va == va and vb == vb and va != vb:
            hi = max(va, vb)
            rails["3V3" if hi == 3.3 else "5V"] += abs(va - vb) / c.fact(inst, "ohms", 1e9) * 1000
    gpio_total = sum(gpio_ma.values())
    lim3 = c.lim["rail_3v3_max_ma"]
    if rails["3V3"] + gpio_total > lim3:
        out.append(
            Finding(
                "rail_budget",
                "error",
                f"3V3 regulator load ~{rails['3V3'] + gpio_total:.0f} mA (rail {rails['3V3']:.0f} + GPIO "
                f"{gpio_total:.0f}) exceeds {lim3:g} mA",
            )
        )
    # Everything ultimately comes from the USB port: 5 V rail + the 3V3 regulator's input + the Pico itself.
    vbus_total = rails["5V"] + (rails["3V3"] + gpio_total) * 3.3 / (5.0 * 0.85) + c.lim["pico_self_ma"]
    if vbus_total > c.lim["vbus_max_ma"]:
        out.append(
            Finding(
                "rail_budget",
                "error",
                f"total USB draw ~{vbus_total:.0f} mA exceeds the {c.lim['vbus_max_ma']:g} mA a USB 2.0 port must supply",
            )
        )
    for inst in c.parts:
        if c.kind(inst) == "servo" and c.voltage(c.net_of.get(f"{inst}.vplus")) == 5.0:
            stall = c.fact(inst, "current_stall_ma", 650.0)
            if stall + c.lim["pico_self_ma"] > c.lim["vbus_max_ma"]:
                out.append(
                    Finding(
                        "rail_budget",
                        "warning",
                        f"{inst} stall current (up to ~{stall:g} mA) can exceed a USB port's 500 mA; keep the horn "
                        f"free to move, don't hold it, and unplug if the Pico keeps resetting",
                    )
                )
    rails = {k: round(v, 1) for k, v in rails.items()}
    rails["usb_total"] = round(vbus_total, 1)
    return rails, out


def render_findings(fs: Iterable[Finding]) -> str:
    return "\n".join(str(f) for f in fs)


__all__ = [
    "RAIL_NETS",
    "RULE_IDS",
    "CheckReport",
    "Finding",
    "UnsafeDesignError",
    "check_design",
    "design_digest",
    "require_safe",
]
