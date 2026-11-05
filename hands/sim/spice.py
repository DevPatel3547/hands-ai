"""SPICE operating-point checks with ngspice: an independent check of the rules and of ``mna.py``.

Two netlist emitters share one set of device models (:mod:`hands.sim.spice_models`):

* :func:`analyze_design` takes a gadget :class:`~hands.circuit.Design`, merges the bench fixture
  (Pico 3V3 regulator, INA219 shunt, USB VBUS) and runs a DC operating point for every
  combination of GPIO drive state (high / low / high-impedance) and button state, at every
  tolerance corner (:data:`CORNERS`). All combinations go into **one** ngspice run as
  independent copies of the circuit, so a full corner sweep costs one process start.
* :func:`solve_board` takes the as-built :class:`~hands.sim.board.SimBoard` circuit for one
  pin state (the same topology ``mna.py`` solves) and returns an ``mna.Solution`` look-alike,
  so the simulator can use ngspice instead of its piecewise-linear solver.

ngspice is an optional system dependency (``brew install ngspice`` / ``sudo apt install
ngspice``); it runs as ``ngspice -b`` in a temporary directory with a timeout. Nothing is
imported from it, and no ngspice Python wrapper is needed.
"""

from __future__ import annotations

import itertools
import math
import os
import platform
import random
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..circuit import FIXTURE_PARTS, Design, validate_structure, with_fixture
from ..inventory import Inventory, PartType
from . import mna
from . import spice_models as M

if TYPE_CHECKING:
    from ..doctor import Check
    from .board import SimBoard

NGSPICE_ENV = "HANDS_NGSPICE"
MAX_COPIES = 4096
GPIO_V_MARGIN = 0.3  # V beyond IOVDD/GND at which a pin counts as over-driven (abs. max is 0.5 V)
LEAK_OHMS = 1e9  # every node leaks to ground so floating nodes have a DC path (like mna.G_LEAK)
REG_OHMS = 0.05  # 3V3 regulator output resistance
VBUS_OHMS = 0.1  # USB cable + connector
BUTTON_OHMS = 0.1  # closed tactile switch
SHUNT_OHMS = 0.1  # INA219 module shunt (also read from the inventory)


class SpiceUnavailable(RuntimeError):
    """ngspice is not installed (or not where ``HANDS_NGSPICE`` points)."""


class SpiceError(RuntimeError):
    """ngspice ran but did not produce a usable operating point."""


# ------------------------------------------------------------------ ngspice ---


def find_ngspice() -> str | None:
    """Path of the ngspice binary: ``$HANDS_NGSPICE`` if set, else ``ngspice`` on PATH."""
    env = os.environ.get(NGSPICE_ENV)
    if env:
        return env if Path(env).is_file() else None
    return shutil.which("ngspice")


def install_hint() -> str:
    system = platform.system()
    if system == "Darwin":
        return "brew install ngspice"
    if system == "Linux":
        return "sudo apt install ngspice  (Fedora: sudo dnf install ngspice)"
    return "install ngspice from https://ngspice.sourceforge.io/download.html and put it on PATH"


def require_ngspice() -> str:
    path = find_ngspice()
    if not path:
        raise SpiceUnavailable(f"ngspice not found; install it with: {install_hint()}")
    return path


@lru_cache(maxsize=4)
def ngspice_version(path: str) -> str:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "ngspice (unknown version)"
    m = re.search(r"ngspice-(\S+)", out)
    return f"ngspice-{m.group(1)}" if m else "ngspice"


def doctor_check() -> Check:
    """For ``hands doctor``: is the optional SPICE checker usable?"""
    from ..doctor import OK, WARN, Check

    path = find_ngspice()
    if path:
        return Check("spice", "ngspice (SPICE checks)", OK, f"{ngspice_version(path)} at {path}")
    return Check(
        "spice",
        "ngspice (SPICE checks)",
        WARN,
        "not found: `hands spice` and the SPICE soundness tests are unavailable (rules still run)",
        install_hint(),
    )


def run_ngspice(netlist: str, *, timeout: float = 60.0, ngspice: str | None = None) -> dict[str, float]:
    """Run one ``.op`` netlist and return every saved vector (``v(node)``, ``i(vsource)``)."""
    exe = ngspice or require_ngspice()
    with tempfile.TemporaryDirectory(prefix="hands-spice-") as td:
        cir, raw = Path(td, "circuit.cir"), Path(td, "out.raw")
        cir.write_text(netlist, encoding="ascii")
        env = dict(os.environ, SPICE_ASCIIRAWFILE="1")
        try:
            proc = subprocess.run(
                [exe, "-b", "-r", str(raw), str(cir)],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=td,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise SpiceError(f"ngspice timed out after {timeout:g} s") from e
        if not raw.exists():
            tail = " | ".join((proc.stdout + proc.stderr).strip().splitlines()[-6:])
            raise SpiceError(f"ngspice produced no operating point (exit {proc.returncode}): {tail}")
        vectors = parse_raw(raw.read_text(encoding="ascii", errors="replace"))
    if not vectors:
        raise SpiceError("ngspice operating point was empty")
    return vectors


def parse_raw(text: str) -> dict[str, float]:
    """Parse an ASCII SPICE rawfile holding one operating point."""
    lines = text.splitlines()
    names: list[str] = []
    out: dict[str, float] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("No. Variables:"):
            n = int(line.split(":")[1])
            names = []
        elif line.startswith("Variables:"):
            for j in range(1, n + 1):
                parts = lines[i + j].split()
                names.append(parts[1].lower())
            i += n
        elif line.startswith("Values:"):
            vals: list[float] = []
            for raw_line in lines[i + 1 :]:
                tok = raw_line.split()
                if not tok:
                    continue
                vals.append(float(tok[-1]))
                if len(vals) == len(names):
                    break
            out.update(zip(names, vals, strict=True))
            break
        i += 1
    return out


# ------------------------------------------------------------------ corners ---


@dataclass(frozen=True)
class Corner:
    name: str
    r_scale: float  # every resistor value is multiplied by this
    led_vf: str  # "min" | "typ" | "max": which 20 mA forward voltage the LED model is fitted to
    v3v3: float  # 3V3 regulator output (= RP2040 IOVDD)
    vbus: float  # USB VBUS
    pad_ohms: float  # RP2040 GPIO driver resistance
    about: str = ""


NOMINAL = Corner("nom", 1.0, "typ", 3.3, 5.0, M.PAD_NOMINAL_OHMS, "typical values")
CORNERS: tuple[Corner, ...] = (
    NOMINAL,
    *(
        Corner(
            f"hot_{v3:.1f}_{vb:.2f}",
            1 - M.R_TOL,
            "min",
            v3,
            vb,
            M.PAD_IDEAL_OHMS,
            f"max current: R -{M.R_TOL:.0%}, LED Vf min, ideal GPIO pad, 3V3={v3:g} V, VBUS={vb:g} V",
        )
        for v3 in M.V3V3_RANGE
        for vb in M.VBUS_RANGE
    ),
    Corner(
        "cold",
        1 + M.R_TOL,
        "max",
        M.V3V3_RANGE[0],
        M.VBUS_RANGE[0],
        M.PAD_WEAK_OHMS,
        f"min current: R +{M.R_TOL:.0%}, LED Vf max, weak GPIO pad, low supplies",
    ),
)


def corners_named(spec: str | Sequence[str] | None) -> tuple[Corner, ...]:
    """``None``/``"nom"`` -> nominal only; ``"all"`` -> every corner; or a list of names."""
    if spec is None or spec == "nom":
        return (NOMINAL,)
    if spec == "all":
        return CORNERS
    wanted = [spec] if isinstance(spec, str) else list(spec)
    by = {c.name: c for c in CORNERS}
    bad = [w for w in wanted if w not in by]
    if bad:
        raise ValueError(f"unknown corner(s) {bad}; choose from {sorted(by)} or 'all'")
    return tuple(by[w] for w in wanted)


# ------------------------------------------------------------------ results ---


@dataclass
class Metric:
    key: str
    label: str
    unit: str
    limit: float | None = None
    lo: float = math.inf
    hi: float = -math.inf
    nom: float | None = None  # largest value seen at the nominal corner
    at: str = ""  # corner and state where ``hi`` happened
    rule: float | None = None  # the rule checker's estimate, when it makes one

    def add(self, value: float, corner: Corner, state: str) -> None:
        if value < self.lo:
            self.lo = value
        if value > self.hi:
            self.hi, self.at = value, f"{corner.name} [{state}]"
        if corner.name == NOMINAL.name and (self.nom is None or value > self.nom):
            self.nom = value

    @property
    def over(self) -> bool:
        return self.limit is not None and self.hi > self.limit + 1e-9

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "unit": self.unit,
            "min": round(self.lo, 4),
            "max": round(self.hi, 4),
            "nominal": None if self.nom is None else round(self.nom, 4),
            "limit": self.limit,
            "rule_estimate": self.rule,
            "worst_at": self.at,
        }


@dataclass
class SpiceFinding:
    rule: str
    severity: str  # "error" (a hard limit is exceeded) | "warning"
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.rule}: {self.message}"


@dataclass
class SpiceReport:
    name: str
    engine: str
    corners: list[str]
    states_total: int
    states_run: int
    copies: int
    metrics: dict[str, Metric]
    findings: list[SpiceFinding] = field(default_factory=list)
    seconds: float = 0.0
    source: str = "ngspice"

    @property
    def violations(self) -> list[SpiceFinding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def exhaustive(self) -> bool:
        return self.states_run == self.states_total

    def render(self) -> str:
        cov = "all" if self.exhaustive else f"{self.states_run} sampled of"
        head = (
            f"SPICE check ({self.engine}): {self.name}: {len(self.corners)} corner(s) x {cov} "
            f"{self.states_total} pin/button state(s) = {self.copies} operating points in {self.seconds:.2f} s"
        )
        rows = [("quantity", "nominal", "min", "max", "limit", "rules say", "worst case at")]
        for m in self.metrics.values():
            rows.append(
                (
                    f"{m.label} ({m.unit})",
                    "-" if m.nom is None else f"{m.nom:.2f}",
                    f"{m.lo:.2f}",
                    f"{m.hi:.2f}" + (" !" if m.over else ""),
                    "-" if m.limit is None else f"{m.limit:g}",
                    "-" if m.rule is None else f"{m.rule:.2f}",
                    m.at,
                )
            )
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        table = ["  " + "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows]
        verdict = "PASS: every quantity stays inside its hard limit" if self.ok else "FAIL"
        out = [head, *table, f"SPICE verdict: {verdict}"]
        out += ["  " + str(f) for f in self.findings]
        return "\n".join(out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "engine": self.engine,
            "corners": self.corners,
            "states_total": self.states_total,
            "states_run": self.states_run,
            "copies": self.copies,
            "seconds": round(self.seconds, 3),
            "ok": self.ok,
            "metrics": {k: m.to_dict() for k, m in self.metrics.items()},
            "findings": [f.__dict__ for f in self.findings],
        }


# ------------------------------------------------------------- the netlist ---


class _Net:
    """Netlist text builder. Every copy of the circuit gets its own node/element prefix."""

    def __init__(self, title: str):
        self.lines: list[str] = [f"* {title}".replace("\n", " ")[:120]]
        self.models: dict[str, str] = {}
        self.nodes: set[str] = set()

    def el(self, line: str, *nodes: str) -> None:
        self.lines.append(line)
        self.nodes.update(n for n in nodes if n != "0")

    def model(self, name: str, text: str) -> None:
        self.models.setdefault(name, text)

    def text(self) -> str:
        leaks = [f"rlk_{n} {n} 0 {LEAK_OHMS:g}" for n in sorted(self.nodes)]
        return "\n".join([*self.lines, *leaks, *self.models.values(), ".op", ".end", ""])


def _led_vf20(pt: PartType, which: str) -> float:
    typ = float(pt.get("vf", 2.0))
    if which == "min":
        return float(pt.get("vf_min", typ))
    if which == "max":
        return max(M.LED_VF_MAX, typ)
    return typ


def _led_model_name(net: _Net, pt: PartType, which: str) -> str:
    vf = _led_vf20(pt, which)
    name = f"dled_{pt.id}_{which}".lower()
    net.model(name, M.led_model(name, vf))
    return name


def _load(net: _Net, name: str, a: str, b: str, amps: float, v_full: float) -> None:
    """Module supply current: ``amps`` once ``V(a,b) >= v_full``, linear below, zero if reversed."""
    v_full = max(v_full, 0.5)
    net.el(f"b{name} {a} {b} I={amps:.6g}*min(1,max(0,V({a},{b}))/{v_full:.4g})", a, b)


# --------------------------------------------------------- design analysis ---


@dataclass
class _Probe:
    metric: str
    kind: str  # i (ammeter), v (node), vd (node pair), p (resistor power)
    a: str
    b: str = "0"
    scale: float = 1.0
    ohms: float = 0.0


@dataclass
class _Copy:
    corner: Corner
    state: str
    probes: list[_Probe]
    gpio_amm: dict[str, str]  # GPIO label -> ammeter name
    gpio_node: dict[str, str]  # GPIO label -> pin node
    iovdd: str
    reg: str
    vbus: str | None
    nodes: dict[str, str] = field(default_factory=dict)  # net name -> node


def _design_states(gadget: Design, full: Design, inv: Inventory) -> tuple[list[str], list[str]]:
    gpios = sorted(gadget.gpio_nets(), key=lambda g: int(g[2:]))
    buttons = sorted(i for i, t in full.all_parts().items() if t in inv.parts and inv.parts[t].kind == "button")
    return gpios, buttons


def _emit_design_copy(
    net: _Net,
    ci: int,
    full: Design,
    inv: Inventory,
    corner: Corner,
    drive: dict[str, str],
    pressed: frozenset[str],
    fixture_gpios: dict[str, str],
) -> _Copy:
    p = f"c{ci}"
    node_of_net: dict[str, str] = {}
    for k, name in enumerate(sorted(full.nets)):
        node_of_net[name] = "0" if name == "GND" else f"{p}n{k}"

    def node(ref: str) -> str:
        n = full.net_of(ref)
        return node_of_net[n] if n is not None else f"{p}x_{ref.replace('.', '_')}"

    parts = full.all_parts()
    probes: list[_Probe] = []
    state = " ".join([f"{g}={s}" for g, s in drive.items()] + [f"{b}=pressed" for b in sorted(pressed)]) or "-"

    # --- power: the Pico's 3V3 regulator (IOVDD) and USB VBUS
    iovdd = node("pico.3V3")
    reg = f"vreg{p}"
    net.el(f"{reg} {p}reg 0 DC {corner.v3v3:g}", f"{p}reg")
    net.el(f"rreg{p} {p}reg {iovdd} {REG_OHMS:g}", f"{p}reg", iovdd)
    vbus_src: str | None = None
    if full.net_of("pico.VBUS") is not None:
        vbus_src = f"vbus{p}"
        net.el(f"{vbus_src} {p}vb 0 DC {corner.vbus:g}", f"{p}vb")
        net.el(f"rvb{p} {p}vb {node('pico.VBUS')} {VBUS_OHMS:g}", f"{p}vb", node("pico.VBUS"))

    # --- GPIO pads
    gpio_amm: dict[str, str] = {}
    gpio_node: dict[str, str] = {}
    net.model("dclamp", M.CLAMP)
    for label, st in {**drive, **fixture_gpios}.items():
        pin = node(f"pico.{label}")
        pad = f"{p}pad{label}"
        amm = f"vam{p}{label}".lower()
        net.el(f"{amm} {pin} {pad} 0", pin, pad)
        if st == "H":
            net.el(f"rpad{p}{label} {pad} {iovdd} {corner.pad_ohms:g}", pad, iovdd)
        elif st == "L":
            net.el(f"rpad{p}{label} {pad} 0 {corner.pad_ohms:g}", pad)
        elif st == "U":
            net.el(f"rpad{p}{label} {pad} {iovdd} {M.PULL_OHMS:g}", pad, iovdd)
        elif st == "D":
            net.el(f"rpad{p}{label} {pad} 0 {M.PULL_OHMS:g}", pad)
        net.el(f"dclh{p}{label} {pad} {iovdd} dclamp", pad, iovdd)
        net.el(f"dcll{p}{label} 0 {pad} dclamp", pad)
        gpio_amm[label], gpio_node[label] = amm, pin

    # --- parts
    for inst, t in parts.items():
        if inst == "pico":
            continue
        pt = inv.parts[t]
        k = pt.kind
        e = f"{p}{inst}".lower()
        if k == "resistor":
            ohms = float(pt.get("ohms", 0)) * corner.r_scale
            a, b = node(f"{inst}.1"), node(f"{inst}.2")
            net.el(f"r{e} {a} {b} {ohms:.6g}", a, b)
            probes.append(_Probe(f"res:{inst}", "p", a, b, ohms=ohms))
        elif k == "led":
            a, c = node(f"{inst}.anode"), node(f"{inst}.cathode")
            mid = f"{e}_a"
            net.el(f"vam{e} {a} {mid} 0", a, mid)
            net.el(f"d{e} {mid} {c} {_led_model_name(net, pt, corner.led_vf)}", mid, c)
            probes.append(_Probe(f"led:{inst}", "i", f"vam{e}"))
        elif k == "buzzer":
            a, c = node(f"{inst}.plus"), node(f"{inst}.minus")
            mid = f"{e}_a"
            net.model("dbuzzer", M.BUZZER)
            net.el(f"vam{e} {a} {mid} 0", a, mid)
            net.el(f"d{e} {mid} {c} dbuzzer", mid, c)
            probes.append(_Probe(f"buzzer:{inst}", "i", f"vam{e}"))
        elif k == "npn":
            b, c, em = node(f"{inst}.b"), node(f"{inst}.c"), node(f"{inst}.e")
            xb, xc = f"{e}_b", f"{e}_c"
            net.model("qpn2222a", M.PN2222A)
            net.el(f"vamc{e} {c} {xc} 0", c, xc)
            net.el(f"vamb{e} {b} {xb} 0", b, xb)
            net.el(f"q{e} {xc} {xb} {em} qpn2222a", xc, xb, em)
            probes.append(_Probe(f"npn_ic:{inst}", "i", f"vamc{e}"))
            probes.append(_Probe(f"npn_ib:{inst}", "i", f"vamb{e}"))
        elif k == "button":
            if inst in pressed:
                a, b = node(f"{inst}.a"), node(f"{inst}.b")
                net.el(f"r{e} {a} {b} {BUTTON_OHMS:g}", a, b)
        elif k in ("dht22", "ssd1306", "ina219", "servo"):
            vcc_pin = next(x.name for x in pt.pins if x.role == "power_in")
            gnd_pin = next(x.name for x in pt.pins if x.role == "gnd")
            vcc, gnd = node(f"{inst}.{vcc_pin}"), node(f"{inst}.{gnd_pin}")
            amps = float(pt.get("current_moving_ma" if k == "servo" else "current_ma", 0)) / 1000
            mid = f"{e}_v"
            net.el(f"vam{e} {vcc} {mid} 0", vcc, mid)
            _load(net, e, mid, gnd, amps, float(pt.get("v_min", 3.0)))
            probes.append(_Probe(f"load:{inst}", "i", f"vam{e}"))
            if pt.get("pullup_onboard") or k == "ina219":
                pu = float(pt.get("pullup_ohms", M.DHT22_PULLUP_OHMS)) * corner.r_scale
                for x in pt.pins:
                    if x.role in ("signal", "i2c_sda", "i2c_scl"):
                        s = node(f"{inst}.{x.name}")
                        net.el(f"rpu{e}{x.name} {s} {vcc} {pu:.6g}", s, vcc)
            if k == "ina219":
                vp, vm = node(f"{inst}.vin_plus"), node(f"{inst}.vin_minus")
                mid2 = f"{e}_sh"
                ohms = float(pt.get("shunt_ohms", SHUNT_OHMS))
                net.el(f"vamsh{e} {vp} {mid2} 0", vp, mid2)
                net.el(f"rsh{e} {mid2} {vm} {ohms:g}", mid2, vm)
                probes.append(_Probe("rail:3v3", "i", f"vamsh{e}"))
        # mcu/breadboard/jumpers: nothing electrical
    return _Copy(corner, state, probes, gpio_amm, gpio_node, iovdd, reg, vbus_src, node_of_net)


def _metric_defs(gadget: Design, full: Design, inv: Inventory, gpios: list[str]) -> dict[str, Metric]:
    lim = inv.limits
    ms: dict[str, Metric] = {}
    for g in gpios:
        ms[f"gpio:{g}"] = Metric(f"gpio:{g}", f"{g} current", "mA", lim["gpio_max_ma"])
    ms["gpio_total"] = Metric("gpio_total", "all GPIO current", "mA", lim["gpio_total_max_ma"])
    for g in gpios:
        ms[f"gpio_v:{g}"] = Metric(f"gpio_v:{g}", f"{g} voltage above IOVDD", "V", GPIO_V_MARGIN)
    for inst, t in full.all_parts().items():
        if inst in FIXTURE_PARTS or t not in inv.parts:
            continue
        pt = inv.parts[t]
        if pt.kind == "led":
            ms[f"led:{inst}"] = Metric(f"led:{inst}", f"{inst} LED current", "mA", float(pt.get("i_max_ma", 20)))
        elif pt.kind == "buzzer":
            ms[f"buzzer:{inst}"] = Metric(f"buzzer:{inst}", f"{inst} buzzer current", "mA")
        elif pt.kind == "npn":
            ms[f"npn_ic:{inst}"] = Metric(
                f"npn_ic:{inst}", f"{inst} collector current", "mA", float(pt.get("ic_max_ma", 600))
            )
            ms[f"npn_ib:{inst}"] = Metric(f"npn_ib:{inst}", f"{inst} base current", "mA")
        elif pt.kind == "resistor":
            ms[f"res:{inst}"] = Metric(f"res:{inst}", f"{inst} power", "mW", lim["resistor_max_w"] * 1000)
        elif pt.kind in ("dht22", "ssd1306", "servo"):
            ms[f"load:{inst}"] = Metric(f"load:{inst}", f"{inst} supply current", "mA")
            ms[f"supply:{inst}"] = Metric(f"supply:{inst}", f"{inst} supply voltage", "V")
    ms["rail:3v3"] = Metric("rail:3v3", "3V3 rail (INA219 shunt)", "mA")
    ms["rail:reg"] = Metric("rail:reg", "3V3 regulator load (incl. GPIO)", "mA", lim["rail_3v3_max_ma"])
    ms["rail:5v"] = Metric("rail:5v", "5V (VBUS) rail", "mA")
    ms["rail:usb"] = Metric("rail:usb", "total USB draw", "mA", lim["vbus_max_ma"])
    return ms


def gpio_state_options(gadget: Design, inv: Inventory) -> dict[str, str]:
    """Drive states each GPIO can be in: ``H`` high, ``L`` low, ``Z`` input without pull,
    ``U``/``D`` input with the internal pull-up/-down. The firmware linter (``hands/fwlint.py``)
    guarantees that a GPIO wired directly to a button is never an output, so those pins are only
    ever inputs; SPICE relies on the same contract (``docs/digital-twin.md``)."""
    from ..fwlint import button_gpios

    buttons = button_gpios(gadget, inv)
    out = {}
    for g in sorted(gadget.gpio_nets(), key=lambda x: int(x[2:])):
        out[g] = "UD" if int(g[2:]) in buttons else "HLZ"
    return out


def _enumerate_states(
    options: dict[str, str], buttons: list[str], n_corners: int, max_copies: int, seed: int
) -> tuple[list[tuple[dict[str, str], frozenset[str]]], int]:
    radix = [len(o) for o in options.values()] + [2] * len(buttons)
    total = math.prod(radix)
    per_corner = max(1, max_copies // max(n_corners, 1))

    def make(idx: int) -> tuple[dict[str, str], frozenset[str]]:
        drive: dict[str, str] = {}
        for g, opts in options.items():
            idx, r = divmod(idx, len(opts))
            drive[g] = opts[r]
        pressed = []
        for b in buttons:
            idx, r = divmod(idx, 2)
            if r:
                pressed.append(b)
        return drive, frozenset(pressed)

    if total <= per_corner:
        return [make(i) for i in range(total)], total
    # Too many to run exhaustively: a deterministic random sample (reported as "sampled").
    rng = random.Random(seed)
    picks = sorted(rng.sample(range(total), per_corner))
    return [make(i) for i in picks], total


def analyze_design(
    design: Design,
    inv: Inventory,
    *,
    corners: str | Sequence[str] | None = "all",
    max_copies: int = MAX_COPIES,
    ngspice: str | None = None,
    timeout: float = 120.0,
) -> SpiceReport:
    """SPICE operating points of ``design`` (a gadget design; the fixture is merged in) at every
    tolerance corner and every GPIO-drive/button state. Hard-limit excursions become ``error``
    findings, exactly the limits the rule checker enforces (``hands/safety.py``)."""
    from ..rules import check_design, design_digest

    exe = ngspice or require_ngspice()
    if design.fixture:
        raise ValueError("pass the gadget design, not a fixture-merged one")
    errs = validate_structure(design, inv, gadget=True)
    if errs:
        raise ValueError("design is not structurally valid: " + "; ".join(errs[:3]))
    t0 = time.perf_counter()
    full = with_fixture(design)
    cs = corners_named(corners)
    _gpios, buttons = _design_states(design, full, inv)
    options = gpio_state_options(design, inv)
    gpios = list(options)
    fixture_gpios = {"GP26": "L", "GP27": "L"}  # the bench's open-drain I2C lines, worst case: driven low
    states, total = _enumerate_states(options, buttons, len(cs), max_copies, int(design_digest(design), 16) % 2**31)
    net = _Net(f"hands design {design.name}")
    copies: list[_Copy] = []
    for corner in cs:
        for drive, pressed in states:
            copies.append(_emit_design_copy(net, len(copies), full, inv, corner, drive, pressed, fixture_gpios))
    vec = run_ngspice(net.text(), timeout=timeout, ngspice=exe)
    metrics = _metric_defs(design, full, inv, gpios)
    rep = check_design(design, inv)
    for g in gpios:
        if g in rep.gpio_current_ma:
            metrics[f"gpio:{g}"].rule = rep.gpio_current_ma[g]
    metrics["gpio_total"].rule = round(sum(rep.gpio_current_ma.values()), 2) if rep.gpio_current_ma else None
    if rep.rail_current_ma:
        metrics["rail:usb"].rule = rep.rail_current_ma.get("usb_total")
        metrics["rail:5v"].rule = rep.rail_current_ma.get("5V")
    pico_self = inv.limits.get("pico_self_ma", 100.0)
    for cp in copies:
        _collect(cp, vec, metrics, full, inv, pico_self, all_gpios=gpios)
    findings = _findings(metrics, full, inv)
    return SpiceReport(
        design.name,
        ngspice_version(exe),
        [c.name for c in cs],
        total,
        len(states),
        len(copies),
        metrics,
        findings,
        time.perf_counter() - t0,
    )


def _v(vec: dict[str, float], node: str) -> float:
    return 0.0 if node == "0" else vec.get(f"v({node})".lower(), 0.0)


def _i(vec: dict[str, float], src: str) -> float:
    return vec.get(f"i({src})".lower(), 0.0)


def _collect(
    cp: _Copy,
    vec: dict[str, float],
    ms: dict[str, Metric],
    full: Design,
    inv: Inventory,
    pico_self_ma: float,
    *,
    all_gpios: list[str],
) -> None:
    c, st = cp.corner, cp.state
    total = 0.0
    v_io = _v(vec, cp.iovdd)
    for g, amm in cp.gpio_amm.items():
        ma = abs(_i(vec, amm)) * 1000
        total += ma
        if g in all_gpios:
            ms[f"gpio:{g}"].add(ma, c, st)
            vpin = _v(vec, cp.gpio_node[g])
            ms[f"gpio_v:{g}"].add(max(vpin - v_io, -vpin), c, st)
    ms["gpio_total"].add(total, c, st)
    for pr in cp.probes:
        if pr.kind == "i":
            val = _i(vec, pr.a) * 1000
        elif pr.kind == "p":
            dv = _v(vec, pr.a) - _v(vec, pr.b)
            val = dv * dv / pr.ohms * 1000 if pr.ohms > 0 else 0.0
        else:  # pragma: no cover - no other probe kinds are emitted
            continue
        if pr.metric in ms:
            ms[pr.metric].add(val, c, st)
    for inst, t in full.all_parts().items():
        pt = inv.parts.get(t)
        if pt is None or inst in FIXTURE_PARTS or pt.kind not in ("dht22", "ssd1306", "servo"):
            continue
        vcc_pin = next(x.name for x in pt.pins if x.role == "power_in")
        gnd_pin = next(x.name for x in pt.pins if x.role == "gnd")
        a, b = full.net_of(f"{inst}.{vcc_pin}"), full.net_of(f"{inst}.{gnd_pin}")
        if a is None or b is None:
            continue
        ms[f"supply:{inst}"].add(_v(vec, cp.nodes[a]) - _v(vec, cp.nodes[b]), c, st)
    i_reg = -_i(vec, cp.reg) * 1000
    i_vbus = -_i(vec, cp.vbus) * 1000 if cp.vbus else 0.0
    ms["rail:reg"].add(i_reg, c, st)
    ms["rail:5v"].add(i_vbus, c, st)
    usb = i_vbus + i_reg * c.v3v3 / (c.vbus * M.REG_EFFICIENCY) + pico_self_ma
    ms["rail:usb"].add(usb, c, st)


def _findings(ms: dict[str, Metric], full: Design, inv: Inventory) -> list[SpiceFinding]:
    out: list[SpiceFinding] = []
    for m in ms.values():
        if m.over:
            out.append(
                SpiceFinding(
                    m.key.split(":")[0],
                    "error",
                    f"{m.label} reaches {m.hi:.2f} {m.unit} (limit {m.limit:g}) at {m.at}",
                )
            )
    for g in [k[5:] for k in ms if k.startswith("gpio:")]:
        m = ms[f"gpio:{g}"]
        warn = inv.limits["gpio_warn_ma"]
        if not m.over and m.hi > warn:
            out.append(SpiceFinding("gpio", "warning", f"{g} reaches {m.hi:.2f} mA (comfortable: {warn:g} mA)"))
    lim = inv.limits
    for inst, t in full.all_parts().items():
        pt = inv.parts.get(t)
        if pt is None or inst in FIXTURE_PARTS:
            continue
        sm = ms.get(f"supply:{inst}")
        vmin = float(pt.get("v_min", 0))
        if sm is not None and sm.lo < vmin - 1e-6 and sm.hi > 0.5:
            out.append(
                SpiceFinding(
                    "supply",
                    "warning",
                    f"{inst} supply dips to {sm.lo:.2f} V (part minimum {vmin:g} V) at a low-supply corner",
                )
            )
        if pt.kind == "servo" and "rail:usb" in ms:
            extra = float(pt.get("current_stall_ma", 650)) - float(pt.get("current_moving_ma", 250))
            if ms["rail:usb"].hi + extra > lim["vbus_max_ma"]:
                out.append(
                    SpiceFinding(
                        "rail",
                        "warning",
                        f"{inst} at stall would take the USB draw to ~{ms['rail:usb'].hi + extra:.0f} mA "
                        f"(> {lim['vbus_max_ma']:g} mA); same policy as the rules: a warning, not a block",
                    )
                )
    return out


# ------------------------------------------------------- as-built board path ---


class SpiceSolution(mna.Solution):
    """An ``mna.Solution`` whose voltages and device currents come from ngspice."""

    def __init__(self, v: dict[str, float], circuit: mna.Circuit, currents: dict[str, float]):
        super().__init__(v, circuit)
        self.currents = currents

    def diode_current(self, tag: str) -> float:
        return max(0.0, self.currents.get(f"d:{tag}", 0.0))

    def npn_ic(self, tag: str) -> float:
        return self.currents.get(f"q:{tag}", 0.0)


def board_netlist(
    ckt: mna.Circuit, board: SimBoard, corner: Corner = NOMINAL
) -> tuple[str, dict[str, str], dict[str, str]]:
    """SPICE text for one as-built pin state, plus node and ammeter name maps."""
    net = _Net("hands as-built board")
    names: dict[str, str] = {}
    gnd = ckt.ground

    def nd(n: str) -> str:
        if n == gnd:
            return "0"
        if n not in names:
            names[n] = f"n{len(names)}"
        return names[n]

    amm: dict[str, str] = {}
    for k, s in enumerate(ckt.sources):
        a = nd(s.node)
        net.el(f"vs{k} s{k} 0 DC {s.v:.6g}", f"s{k}")
        net.el(f"rs{k} s{k} {a} {s.r:.6g}", f"s{k}", a)
    for k, r in enumerate(ckt.res):
        a, b = nd(r.a), nd(r.b)
        net.el(f"r{k} {a} {b} {r.r * (corner.r_scale if _is_resistor(board, r.tag) else 1.0):.6g}", a, b)
    for k, d in enumerate(ckt.diodes):
        a, c = nd(d.a), nd(d.k)
        pt = _ptype(board, d.tag)
        if pt is not None and pt.kind == "led":
            model = _led_model_name(net, pt, corner.led_vf)
        else:
            net.model("dbuzzer", M.BUZZER)
            model = "dbuzzer"
        net.el(f"vad{k} {a} da{k} 0", a, f"da{k}")
        net.el(f"d{k} da{k} {c} {model}", f"da{k}", c)
        amm[f"d:{d.tag}"] = f"vad{k}"
    net.model("qpn2222a", M.PN2222A)
    for k, q in enumerate(ckt.npns):
        b, c, e = nd(q.b), nd(q.c), nd(q.e)
        net.el(f"vaq{k} {c} qc{k} 0", c, f"qc{k}")
        net.el(f"q{k} qc{k} {b} {e} qpn2222a", f"qc{k}", b, e)
        amm[f"q:{q.tag}"] = f"vaq{k}"
    # ESD clamps of every GPIO that drives or reads a node, to IOVDD (the 3V3 regulator node)
    io = next((s.node for s in ckt.sources if s.tag == "reg3v3"), None)
    if io is not None:
        net.model("dclamp", M.CLAMP)
        for n in sorted({s.node for s in ckt.sources if s.tag.startswith("GP")}):
            a, v = nd(n), nd(io)
            net.el(f"dch_{a} {a} {v} dclamp", a, v)
            net.el(f"dcl_{a} 0 {a} dclamp", a)
    return net.text(), names, amm


def _ptype(board: SimBoard, tag: str) -> PartType | None:
    p = board.build.placements.get(tag.split(":")[0])
    return board.inv.parts.get(p["type"]) if p else None


def _is_resistor(board: SimBoard, tag: str) -> bool:
    pt = _ptype(board, tag)
    return pt is not None and pt.kind == "resistor" and ":" not in tag


def solve_board(board: SimBoard, key: Any = None, *, ngspice: str | None = None) -> SpiceSolution:
    """Solve the as-built circuit of ``board`` (one pin state) with ngspice."""
    ckt = board._circuit(key or board.state_key())
    text, names, amm = board_netlist(ckt, board)
    if not ckt.sources:  # unpowered: nothing to solve
        return SpiceSolution({ckt.ground: 0.0}, ckt, {})
    vec = run_ngspice(text, ngspice=ngspice)
    v = {n: vec.get(f"v({nn})", 0.0) for n, nn in names.items()}
    v[ckt.ground] = 0.0
    currents = {k: vec.get(f"i({a})", 0.0) for k, a in amm.items()}
    return SpiceSolution(v, ckt, currents)


def iter_rule_ids(findings: Iterable[SpiceFinding]) -> list[str]:
    return sorted({f.rule for f in findings if f.severity == "error"})


__all__ = [
    "CORNERS",
    "NOMINAL",
    "Corner",
    "SpiceError",
    "SpiceFinding",
    "SpiceReport",
    "SpiceSolution",
    "SpiceUnavailable",
    "analyze_design",
    "board_netlist",
    "doctor_check",
    "find_ngspice",
    "install_hint",
    "run_ngspice",
    "solve_board",
]
