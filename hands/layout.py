"""Deterministic breadboard placer.

Turns a (fixture-merged) logical design into physical positions on an
830-point breadboard: which hole every part leg goes in, and which jumper
wires connect what. Coordinates:

* ``a1`` .. ``j63`` -- terminal holes. Rows a-e in one column are one
  connected *strip* (``t<col>``); rows f-j are another (``b<col>``).
* ``T+12`` / ``T-12`` / ``B+12`` / ``B-12`` -- a hole in the top/bottom power
  rail near column 12. Rails: T+ = 3V3 (measured), T- = GND, B+ = 5V, B- = GND.
* ``servo1.sig`` -- an off-board connector (jumper plugs into the part).

The Pico straddles the centre channel at columns 1-20: header pin 1 (GP0) in
h1 ... pin 20 (GP15) in h20; pin 21 (GP16) in c20 ... pin 40 (VBUS) in c1.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .circuit import Design, PinRef, with_fixture
from .inventory import Inventory
from .rules import CheckReport, require_safe

ROWS_TOP = "abcde"
ROWS_BOT = "fghij"
NCOLS = 63
RAILS = ("T+", "T-", "B+", "B-")
RAIL_OF_NET = {"3V3": ("T+", "T+"), "5V": ("B+", "B+"), "GND": ("T-", "B-")}  # (top, bottom)
WIRE_COLORS = {"GND": "black", "3V3": "red", "5V": "orange", "V3_SRC": "red"}
MAX_REACH = 5
SPAN = {"resistor": 4, "led": 2, "buzzer": 3}


def strip_of(hole: str) -> str:
    """Electrical strip id of a hole: 't24', 'b24', or a rail name."""
    for r in RAILS:
        if hole.startswith(r):
            return r
    if "." in hole:
        return hole  # off-board connector is its own node
    row, col = hole[0], int(hole[1:])
    return f"t{col}" if row in ROWS_TOP else f"b{col}"


def pico_hole(pin_number: int) -> str:
    return f"h{pin_number}" if pin_number <= 20 else f"c{41 - pin_number}"


@dataclass
class Placement:
    inst: str
    type_id: str
    holes: dict[str, str]  # pin name -> hole (empty for off-board parts)
    extra_holes: list[str] = field(default_factory=list)  # legs not used electrically (button)


@dataclass
class Wire:
    id: str
    a: str
    b: str
    net: str
    color: str = "any"

    def key(self) -> frozenset:
        return frozenset((strip_of(self.a), strip_of(self.b)))


@dataclass
class Layout:
    design: Design  # fixture-merged
    placements: dict[str, Placement]
    wires: list[Wire]
    pico_pins: dict[str, str]  # 'pico.GP15' -> hole (GND: one entry per side used)
    order: list[str]  # instance placement order (fixture first)
    gadget: Design | None = None  # the rule-checked gadget design this layout was made from
    report: CheckReport | None = None  # the passing rule-check report (the safety gate's receipt)

    def hole_of(self, ref: str) -> str | None:
        r = PinRef.parse(ref)
        if r.inst == "pico":
            return self.pico_pins.get(ref)
        p = self.placements.get(r.inst)
        if p is None:
            return None
        return p.holes.get(r.pin, ref if not p.holes else None)

    def to_dict(self) -> dict:
        return {
            "placements": {
                k: {"type": p.type_id, "holes": p.holes, "extra_holes": p.extra_holes}
                for k, p in self.placements.items()
            },
            "wires": [w.__dict__ for w in self.wires],
            "pico_pins": self.pico_pins,
        }


class LayoutError(RuntimeError):
    """The design does not fit on one breadboard."""


class _Board:
    def __init__(self):
        self.used: set[str] = set()
        # column 21 left empty as clearance next to the Pico
        self.free_cols = {"t": set(range(22, NCOLS + 1)), "b": set(range(22, NCOLS + 1))}
        self.rail_used: dict[str, set[int]] = {r: set() for r in RAILS}
        self.rail_blocked: dict[str, set[int]] = {r: set() for r in RAILS}

    def take_col(self, side: str, col: int) -> None:
        self.free_cols[side].discard(col)

    def free_hole(self, strip: str, prefer: str | None = None) -> str:
        if strip in RAILS:
            raise ValueError("use rail_hole for rails")
        side, col = strip[0], int(strip[1:])
        rows = ROWS_TOP[::-1] if side == "t" else ROWS_BOT  # fill from the channel outward
        if prefer:
            rows = prefer + "".join(r for r in rows if r not in prefer)
        for r in rows:
            h = f"{r}{col}"
            if h not in self.used:
                self.used.add(h)
                return h
        raise RuntimeError(f"strip {strip} is full")

    def has_free(self, strip: str) -> bool:
        side, col = strip[0], int(strip[1:])
        rows = ROWS_TOP if side == "t" else ROWS_BOT
        return any(f"{r}{col}" not in self.used for r in rows)

    def rail_hole(self, rail: str, near: int) -> str:
        for d in range(NCOLS):
            for c in (near + d, near - d):
                if 1 <= c <= NCOLS and c not in self.rail_used[rail] and c not in self.rail_blocked[rail]:
                    self.rail_used[rail].add(c)
                    return f"{rail}{c}"
        raise RuntimeError(f"rail {rail} full")

    def near_free_col(self, side: str, around: int, avoid: int, span: int) -> int | None:
        """Free column within reach of ``avoid`` whose columns in between are
        also free (so the part body doesn't sit on top of another part)."""
        free = self.free_cols[side]
        for c in sorted(free, key=lambda c: (abs(abs(c - avoid) - span), c)):
            if c == avoid or not (1 <= abs(c - avoid) <= MAX_REACH):
                continue
            lo, hi = sorted((c, avoid))
            if all(x in free or x == 21 for x in range(lo + 1, hi)):  # 21 = empty clearance column
                return c
        return None

    def block_range(self, side: str, c1: int, c2: int, margin: int = 0) -> None:
        lo, hi = sorted((c1, c2))
        for x in range(lo - margin, hi + margin + 1):
            self.free_cols[side].discard(x)

    def next_free_col(self, side: str, n: int = 1) -> int | None:
        cols = sorted(self.free_cols[side])
        for c in cols:
            if all((c + i) in self.free_cols[side] for i in range(n)):
                return c
        return None


def place(design: Design, inv: Inventory) -> Layout:
    """Place a gadget design on the breadboard.

    Safety gate: the design must pass the deterministic rule check first
    (:func:`hands.rules.require_safe` raises otherwise), so no wiring can ever be
    generated for an unchecked or failing design.
    """
    report = require_safe(design, inv)
    full = with_fixture(design)
    b = _Board()
    placements: dict[str, Placement] = {}
    home: dict[str, str] = {}  # net -> strip (or rail)
    member_strips: dict[str, list[tuple[str, str]]] = {}  # net -> [(strip, hole_hint)]
    offboard: dict[str, list[str]] = {}  # net -> off-board endpoints
    net_of = {p: n for n, ps in full.nets.items() for p in ps}
    pico_pins: dict[str, str] = {}

    # Pico: each GPIO used gets its header hole; its strip can act as the net home.
    for ref in net_of:
        r = PinRef.parse(ref)
        if r.inst != "pico" or r.pin == "GND":
            continue
        hole = pico_hole(inv.pico_pin_number(r.pin))
        b.used.add(hole)
        pico_pins[ref] = hole
    for c in range(1, 21):
        b.used.add(f"c{c}")
        b.used.add(f"h{c}")
        for row in "defg":
            b.used.add(f"{row}{c}")

    def add_member(net: str, strip: str):
        if net in RAIL_OF_NET:
            member_strips.setdefault(net, []).append((strip, ""))
            return
        if net not in home:
            home[net] = strip
        elif home[net] != strip:
            member_strips.setdefault(net, []).append((strip, ""))

    order = ["pico", "ina"] + [i for i in full.parts if i not in ("pico", "ina")]
    parts = full.all_parts()

    # 1) header modules (incl. fixture INA219): consecutive columns, row a (top) or j (bottom)
    for inst in order:
        if inst == "pico":
            continue
        pt = inv.parts[parts[inst]]
        if pt.mount != "header":
            continue
        n = len(pt.pins)
        side, row = "t", "a"
        col = b.next_free_col("t", n + 1)
        if col is None or col + n - 1 > NCOLS:
            side, row = "b", "j"
            col = b.next_free_col("b", n + 1)
        if col is None:
            raise LayoutError(f"breadboard is full: no room for {inst}")
        holes = {}
        for i, p in enumerate(pt.pins):
            c = col + i
            h = f"{row}{c}"
            b.used.add(h)
            b.take_col(side, c)
            holes[p.name] = h
            # module body overhangs the rail next to it
            for rail in ("T+", "T-") if side == "t" else ("B+", "B-"):
                b.rail_blocked[rail].add(c)
        b.take_col(side, col + n)  # spacer column
        placements[inst] = Placement(inst, pt.id, holes)

    # 2) transistors (3 consecutive cols, row e) and buttons (straddle channel)
    for inst in order:
        if inst in ("pico",) or inst in placements:
            continue
        pt = inv.parts[parts[inst]]
        if pt.mount == "discrete3":
            col = b.next_free_col("t", 3)
            if col is None:
                raise LayoutError(f"breadboard is full: no room for {inst}")
            holes = {}
            for i, p in enumerate(pt.pins):
                h = f"e{col + i}"
                b.used.add(h)
                b.take_col("t", col + i)
                holes[p.name] = h
            placements[inst] = Placement(inst, pt.id, holes)
        elif pt.mount == "button4":
            col = None
            for c in sorted(b.free_cols["t"]):
                if all(x in b.free_cols[s] for x in (c, c + 2) for s in ("t", "b")):
                    col = c
                    break
            if col is None:
                raise RuntimeError("no room for button")
            for s in ("t", "b"):
                for x in (col, col + 1, col + 2):
                    b.take_col(s, x)
            holes = {"a": f"e{col}", "b": f"f{col + 2}"}
            extra = [f"e{col + 2}", f"f{col}"]
            for h in list(holes.values()) + extra:
                b.used.add(h)
            placements[inst] = Placement(inst, pt.id, holes, extra)

    # establish homes from modules/transistors/buttons
    for inst, pl in placements.items():
        for pin, h in pl.holes.items():
            net = net_of.get(f"{inst}.{pin}")
            if net:
                add_member(net, strip_of(h))

    # Pico strips can be homes for nets that have no other home yet
    for ref, hole in pico_pins.items():
        net = net_of[ref]
        if net not in RAIL_OF_NET and net not in home:
            home[net] = strip_of(hole)

    # 3) two-lead discretes
    def strip_ok(s: str) -> bool:
        return s not in RAILS and "." not in s and b.has_free(s)

    for inst in order:
        if inst == "pico" or inst in placements:
            continue
        pt = inv.parts[parts[inst]]
        if pt.mount != "discrete2":
            continue
        pins = [p.name for p in pt.pins]
        nets = [net_of.get(f"{inst}.{p}") for p in pins]
        span = SPAN.get(pt.kind, 3)
        homes = [home.get(n) if n and n not in RAIL_OF_NET else None for n in nets]
        hs: list[str | None] = [h if h and strip_ok(h) else None for h in homes]
        h0, h1 = hs
        chosen: list[str]
        if h0 and h1 and h0[0] == h1[0] and 1 <= abs(int(h0[1:]) - int(h1[1:])) <= MAX_REACH:
            chosen = [h0, h1]
        elif h0 or h1:
            k = 0 if h0 else 1
            hk = h0 or h1
            assert hk is not None
            side, c0 = hk[0], int(hk[1:])
            cfree = b.near_free_col(side, c0, c0, span)
            if cfree is None:
                chosen = _fresh_pair(b, span)  # home strip gets a jumper instead
            else:
                b.take_col(side, cfree)
                other = f"{side}{cfree}"
                chosen = [hk, other] if k == 0 else [other, hk]
        else:
            chosen = _fresh_pair(b, span)
        if chosen[0][0] == chosen[1][0]:
            b.block_range(chosen[0][0], int(chosen[0][1:]), int(chosen[1][1:]), margin=1 if pt.kind == "buzzer" else 0)
        dholes: dict[str, str] = {}
        for pname, strip in zip(pins, chosen, strict=True):
            dholes[pname] = b.free_hole(strip)
        placements[inst] = Placement(inst, pt.id, dholes)
        for pname, netname in zip(pins, nets, strict=True):
            if netname:
                add_member(netname, strip_of(dholes[pname]))

    # off-board parts (servo): endpoints are the connector sockets
    for inst in order:
        if inst == "pico" or inst in placements:
            continue
        pt = inv.parts[parts[inst]]
        if pt.mount == "offboard":
            placements[inst] = Placement(inst, pt.id, {})
            for ps in pt.pins:
                onet = net_of.get(f"{inst}.{ps.name}")
                if onet:
                    offboard.setdefault(onet, []).append(f"{inst}.{ps.name}")

    # 4) jumpers
    wires: list[Wire] = []

    def new_wire(a: str, bb: str, net: str):
        wires.append(Wire(f"W{len(wires) + 1}", a, bb, net, WIRE_COLORS.get(net, _signal_color(len(wires)))))

    def rail_for(net: str, side: str) -> str:
        top, bot = RAIL_OF_NET[net]
        return top if side == "t" else bot

    # Pico power pins first
    rails_needed: set[str] = set()
    for net, mem in member_strips.items():
        if net in RAIL_OF_NET:
            for s, _ in mem:
                rails_needed.add(rail_for(net, s[0]))
    for ref in list(pico_pins):
        net = net_of[ref]
        if net in RAIL_OF_NET:  # e.g. VBUS -> 5V rail
            rails_needed.add(RAIL_OF_NET[net][1] if net == "5V" else RAIL_OF_NET[net][0])
    rails_needed.add("T-")
    gnd_pins = {"T-": 38, "B-": 3}
    for rail in ("T-", "B-"):
        if rail in rails_needed:
            gpin = gnd_pins[rail]
            hole = pico_hole(gpin)
            pico_pins[f"pico.GND@{gpin}"] = hole
            b.used.add(hole)
            fh = b.free_hole(strip_of(hole))
            new_wire(fh, b.rail_hole(rail, int(hole[1:])), "GND")
    if "pico.GND" in net_of:
        pico_pins["pico.GND"] = pico_pins.get("pico.GND@38", pico_hole(38))

    # Pico signal/power pins -> their net home
    for ref, hole in sorted(pico_pins.items(), key=lambda kv: kv[0]):
        if ref.startswith("pico.GND"):
            continue
        net = net_of[ref]
        s = strip_of(hole)
        if net in RAIL_OF_NET:
            rail = "B+" if net == "5V" else rail_for(net, s[0])
            new_wire(b.free_hole(s), b.rail_hole(rail, int(hole[1:])), net)
        elif home.get(net) != s:
            new_wire(b.free_hole(s), b.free_hole(home[net]), net)

    # part strips -> home / rail
    for net, mem in member_strips.items():
        for s, _ in mem:
            if net in RAIL_OF_NET:
                new_wire(b.free_hole(s), b.rail_hole(rail_for(net, s[0]), int(s[1:])), net)
            else:
                new_wire(b.free_hole(s), b.free_hole(home[net]), net)

    # off-board connectors
    for net, eps in offboard.items():
        for ep in eps:
            if net in RAIL_OF_NET:
                rail = "B+" if net == "5V" else "B-"
                rails_needed.add(rail)
                new_wire(b.rail_hole(rail, 60), ep, net)
            elif net in home:
                new_wire(b.free_hole(home[net]), ep, net)
            else:
                raise RuntimeError(f"net {net} has nowhere to connect {ep}")
    # bottom GND rail fed if an off-board part needed it but no Pico GND wire yet
    if "B-" in rails_needed and "pico.GND@3" not in pico_pins:
        hole = pico_hole(3)
        b.used.add(hole)
        pico_pins["pico.GND@3"] = hole
        wires.insert(1, Wire("W0", b.free_hole(strip_of(hole)), b.rail_hole("B-", 3), "GND", "black"))
        for i, w in enumerate(wires):
            w.id = f"W{i + 1}"

    return Layout(full, placements, wires, pico_pins, [o for o in order if o != "pico"], design, report)


def _fresh_pair(b: _Board, span: int) -> list[str]:
    for side in ("t", "b"):
        c = b.next_free_col(side, span + 1)
        if c is not None and c + span <= NCOLS:
            b.take_col(side, c)
            b.take_col(side, c + span)
            return [f"{side}{c}", f"{side}{c + span}"]
    raise RuntimeError("breadboard is full")


def _signal_color(i: int) -> str:
    return ["yellow", "green", "blue", "white", "purple", "gray"][i % 6]
