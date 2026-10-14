"""Deterministic debugging helpers: signal-path walk, known-good diagnostic
firmware, and wiring-check instructions. The LLM is only consulted when these
come up empty (e.g. the hardware is fine and the firmware is wrong)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .circuit import PinRef
from .inventory import Inventory
from .layout import RAILS, ROWS_BOT, ROWS_TOP, Layout, strip_of

SAFE_IDLE_FW = """# Hands safe-idle firmware: every GPIO left as a high-impedance input.
import time
print("READY idle")
while True:
    time.sleep(1)
"""

DIAG_TOGGLE_FW = """# Hands diagnostic firmware (known good): toggle GP{n} at 2 Hz.
from machine import Pin
import time
p = Pin({n}, Pin.OUT)
print("READY diag toggle GP{n}")
while True:
    p.toggle()
    time.sleep(0.25)
"""

DIAG_SERVO_FW = """# Hands diagnostic firmware (known good): 50 Hz servo pulses on GP{n}, sweeping 45..135 deg.
from machine import Pin, PWM
import time
s = PWM(Pin({n}))
s.freq(50)
def deg(d):
    s.duty_u16(int((0.5 + 2.0 * d / 180) / 20 * 65535))
print("READY diag servo GP{n}")
while True:
    deg(45)
    time.sleep(0.6)
    deg(135)
    time.sleep(0.6)
"""


@dataclass
class PathPoint:
    strip: str
    hole: str
    via: tuple[str, str] | None  # ("wire", "W3") | ("part", "r1") | None
    net: str
    probe: bool = True


def _used_holes(layout: Layout) -> set[str]:
    used = set(layout.pico_pins.values())
    for p in layout.placements.values():
        used |= set(p.holes.values()) | set(p.extra_holes)
    for w in layout.wires:
        used |= {w.a, w.b}
    for c in range(1, 21):
        used |= {f"{r}{c}" for r in "cdefgh"}
    return used


def probe_hole(layout: Layout, strip: str) -> str:
    if strip in RAILS or "." in strip:
        return strip
    side, col = strip[0], int(strip[1:])
    used = _used_holes(layout)
    rows = ROWS_TOP if side == "t" else ROWS_BOT
    for r in rows:
        if f"{r}{col}" not in used:
            return f"{r}{col}"
    # strip full: touch the probe to the leg/wire already in the outermost hole
    return f"{rows[0] if side == 't' else rows[-1]}{col}"


def signal_path(layout: Layout, inv: Inventory, gpio: str, target: str) -> list[PathPoint]:
    """Ordered probe points from the Pico pin to the target part, following
    jumpers and crossing resistors / transistor (base->collector)."""
    d = layout.design
    parts = d.all_parts()
    start = d.net_of(f"pico.{gpio}")
    if start is None:
        return []

    def kind(inst):
        t = parts.get(inst)
        return inv.parts[t].kind if t in inv.parts else None

    # BFS over nets through parts
    prev: dict[str, tuple[str, str, str] | None] = {start: None}  # net -> (prev_net, part, entry_pin)
    q = deque([start])
    goal = None
    while q:
        net = q.popleft()
        if any(PinRef.parse(p).inst == target for p in d.nets[net]):
            goal = net
            break
        for p in d.nets[net]:
            r = PinRef.parse(p)
            k = kind(r.inst)
            nxt = []
            if k == "resistor":
                nxt = [f"{r.inst}.{'2' if r.pin == '1' else '1'}"]
            elif k == "npn" and r.pin == "b":
                nxt = [f"{r.inst}.c"]
            for o in nxt:
                n2 = d.net_of(o)
                if n2 and n2 not in prev:
                    prev[n2] = (net, r.inst, o)
                    q.append(n2)
    if goal is None:
        return []
    chain = []  # list of (net, via_part, entry_ref)
    cur: str | None = goal
    while cur is not None:
        pv = prev[cur]
        chain.append((cur, pv[1] if pv else None, pv[2] if pv else None))
        cur = pv[0] if pv else None
    chain.reverse()

    points: list[PathPoint] = []
    for i, (net, via_part, entry_ref) in enumerate(chain):
        # strips of this net, connected by this net's wires
        wires = [w for w in layout.wires if w.net == net]
        adj: dict[str, list[tuple[str, str]]] = {}
        for w in wires:
            a, b = strip_of(w.a), strip_of(w.b)
            adj.setdefault(a, []).append((b, w.id))
            adj.setdefault(b, []).append((a, w.id))
        if i == 0:
            entry = strip_of(layout.pico_pins[f"pico.{gpio}"])
        else:
            assert entry_ref is not None
            entry = strip_of(layout.hole_of(entry_ref) or entry_ref)
        # exit = where the next part's entry pin / target pin sits
        if i + 1 < len(chain):
            nxt_part = chain[i + 1][1]
            exit_refs = [p for p in d.nets[net] if PinRef.parse(p).inst == nxt_part]
        else:
            exit_refs = [p for p in d.nets[net] if PinRef.parse(p).inst == target]
        exits = {strip_of(layout.hole_of(p) or p) for p in exit_refs}
        # BFS entry -> exit over wires
        prevs: dict[str, tuple[str, str] | None] = {entry: None}
        qq = deque([entry])
        found = entry if entry in exits else None
        while qq and not found:
            s = qq.popleft()
            for s2, wid in adj.get(s, []):
                if s2 not in prevs:
                    prevs[s2] = (s, wid)
                    if s2 in exits:
                        found = s2
                        break
                    qq.append(s2)
        seq: list[tuple[str, tuple[str, str] | None]] = []
        walk: str | None = found or entry
        while walk is not None:
            pw = prevs.get(walk)
            seq.append((walk, ("wire", pw[1]) if pw else None))
            walk = pw[0] if pw else None
        seq.reverse()
        is_base = any(PinRef.parse(p).pin == "b" and kind(PinRef.parse(p).inst) == "npn" for p in d.nets[net])
        for j, (s, via) in enumerate(seq):
            if j == 0 and i > 0 and via_part is not None:
                via = ("part", via_part)
            points.append(PathPoint(s, probe_hole(layout, s), via, net, probe=not is_base and "." not in s))
    return points


def wires_touching(layout: Layout, inst: str) -> list[str]:
    pl = layout.placements[inst]
    strips = {strip_of(h) for h in pl.holes.values()}
    out = []
    for w in layout.wires:
        if (
            strip_of(w.a) in strips
            or strip_of(w.b) in strips
            or w.a.startswith(inst + ".")
            or w.b.startswith(inst + ".")
        ):
            out.append(w.id)
    return out


def check_part_instruction(layout: Layout, inv: Inventory, inst: str) -> tuple[str, dict]:
    pl = layout.placements[inst]
    pt = inv.parts[pl.type_id]
    parts = []
    if pl.holes:
        legs = ", ".join(
            f"{p.name}{' (' + p.hint + ')' if p.hint else ''} in {pl.holes[p.name]}"
            for p in pt.pins
            if p.name in pl.holes
        )
        parts.append(f"Check {inst}: {legs}. If it is turned the wrong way, pull it out and re-insert it.")
    wids = wires_touching(layout, inst)
    wmap = {w.id: w for w in layout.wires}
    if wids:
        parts.append(
            "Then check each of its wires is pushed fully in at both ends: "
            + "; ".join(f"{w} {wmap[w].a}->{wmap[w].b}" for w in wids)
            + "."
        )
    fixes = ([{"action": "verify_part", "inst": inst}] if pl.holes else []) + [
        {"action": "verify_wire", "wire": w} for w in wids
    ]
    return " ".join(parts), {"action": "multi", "fixes": fixes}


def power_wiring_instruction(layout: Layout) -> tuple[str, dict]:
    pw = [w for w in layout.wires if w.net in ("3V3", "5V", "GND", "V3_SRC")]
    txt = (
        "With USB unplugged: look along the red and blue rail lines for any stray lead clipping or bent "
        "leg touching both. Then check every power wire lands exactly where planned: "
        + "; ".join(f"{w.id} {w.a}->{w.b}" for w in pw)
        + "."
    )
    return txt, {
        "action": "multi",
        "fixes": [{"action": "inspect_rails"}] + [{"action": "verify_wire", "wire": w.id} for w in pw],
    }
