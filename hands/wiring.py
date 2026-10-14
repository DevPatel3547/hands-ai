"""Wiring guide: one physical action per step, with coordinates and diagrams,
plus the deterministic comparison used by the photo check."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from .inventory import Inventory
from .layout import NCOLS, RAILS, ROWS_BOT, ROWS_TOP, Layout, pico_hole, strip_of
from .rules import CheckReport, Finding, UnsafeDesignError, require_safe

RAIL_DESC = {
    "T+": "top red (+) rail = 3V3",
    "T-": "top blue (-) rail = GND",
    "B+": "bottom red (+) rail = 5V",
    "B-": "bottom blue (-) rail = GND",
}


@dataclass
class Step:
    n: int
    kind: str  # safety | place_pico | insert_part | jumper
    title: str
    detail: str
    holes: list[str] = field(default_factory=list)
    ref: str = ""  # part instance or wire id
    done: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Guide:
    layout: Layout
    steps: list[Step]

    def text(self, diagrams: bool = True) -> str:
        out = []
        for s in self.steps:
            out.append(f"Step {s.n}: {s.title}\n  {s.detail}")
            if diagrams and s.kind != "safety":
                out.append(ascii_diagram(self.layout, s.n, self.steps))
        return "\n".join(out)


def describe_hole(h: str) -> str:
    for r in RAILS:
        if h.startswith(r):
            return f"any free hole in the {RAIL_DESC[r]} near column {h[len(r) :]}"
    if "." in h:
        inst, pin = h.split(".", 1)
        return f"the {pin} socket of {inst}'s connector"
    return h


def build_guide(layout: Layout, inv: Inventory) -> Guide:
    """One physical action per step. Re-verifies the safety gate: a layout that
    did not come from a rule-checked design gets no wiring steps."""
    if layout.gadget is None:
        raise UnsafeDesignError(
            CheckReport([Finding("structure", "error", "layout has no rule-checked design")], {}, {})
        )
    require_safe(layout.gadget, inv)
    steps: list[Step] = []

    def add(kind, title, detail, holes=(), ref=""):
        steps.append(Step(len(steps), kind, title, detail, list(holes), ref))

    add(
        "safety",
        "Unplug the Pico's USB cable",
        "Everything is wired with power OFF. Low voltage only (USB 5 V). Never connect anything to mains. "
        "If your breadboard's rail lines have a gap in the middle, add a short jumper across each gap.",
    )
    add(
        "place_pico",
        "Place the Pico W on the breadboard",
        f"USB port hanging off the LEFT end. Pin 1 (GP0) goes in {pico_hole(1)}, pin 20 (GP15) in {pico_hole(20)}, "
        f"pin 40 (VBUS) in {pico_hole(40)}, pin 21 (GP16) in {pico_hole(21)}. It straddles the centre channel; "
        f"press down evenly until the header is fully seated.",
        [pico_hole(1), pico_hole(20), pico_hole(21), pico_hole(40)],
        "pico",
    )

    placed_wires: set[str] = set()
    wire_by_inst: dict[str, list] = {}
    for w in layout.wires:
        for end in (w.a, w.b):
            for inst, pl in layout.placements.items():
                if (
                    end in pl.holes.values()
                    or strip_of(end) in {strip_of(h) for h in pl.holes.values()}
                    or end.startswith(inst + ".")
                ):
                    wire_by_inst.setdefault(inst, []).append(w)

    def wire_step(w):
        if w.id in placed_wires:
            return
        placed_wires.add(w.id)
        add(
            "jumper",
            f"Jumper {w.id} ({w.color} if you have it): {_short(w.a)} -> {_short(w.b)}",
            f"Connect {describe_hole(w.a)} to {describe_hole(w.b)}. (net {w.net})",
            [w.a, w.b],
            w.id,
        )

    # GND/power wires that come straight from the Pico first
    for w in layout.wires:
        if any(
            h in layout.pico_pins.values() or strip_of(h) in {strip_of(p) for p in layout.pico_pins.values()}
            for h in (w.a,)
        ) and w.net in ("GND",):
            wire_step(w)

    for inst in layout.order:
        pl = layout.placements[inst]
        pt = inv.parts[pl.type_id]
        if pl.holes:
            legs = []
            for p in pt.pins:
                h = pl.holes.get(p.name)
                if h:
                    hint = f" [{p.hint}]" if p.hint else ""
                    legs.append(f"{p.name}{hint} -> {h}")
            extra = ""
            if pl.extra_holes:
                extra = f" Its other two legs go in {' and '.join(pl.extra_holes)} (button straddles the channel)."
            facing = " Module faces outward, away from the centre." if pt.mount == "header" else ""
            add(
                "insert_part",
                f"Insert {inst} ({pt.name})",
                "; ".join(legs) + "." + extra + facing,
                list(pl.holes.values()) + pl.extra_holes,
                inst,
            )
        else:
            wires_desc = ", ".join(f"{p.name} = {p.hint}" for p in pt.pins)
            add(
                "insert_part",
                f"Get {inst} ready ({pt.name}) next to the board",
                f"It is not plugged into the breadboard; jumpers push into its connector sockets ({wires_desc}).",
                [],
                inst,
            )
        for w in wire_by_inst.get(inst, []):
            wire_step(w)
    for w in layout.wires:
        wire_step(w)
    add(
        "safety",
        "Final look-over, then photo check",
        "Check no bare leads touch each other. Then take a photo from directly above (whole board visible).",
    )
    return Guide(layout, steps)


def _short(h: str) -> str:
    return h


# ------------------------------------------------------------ diagrams ---


def _occupancy(layout: Layout, upto: int, steps: list[Step]):
    marks: dict[str, str] = {}
    legend: dict[str, str] = {}
    letters = iter("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    done = steps[: upto + 1]
    if any(s.kind == "place_pico" for s in done):
        for c in range(1, 21):
            marks[f"c{c}"] = "P"
            marks[f"h{c}"] = "P"
            for r in "defg":
                marks[f"{r}{c}"] = "#"
        legend["P"] = "Pico pins (#=body)"
    for s in done:
        if s.kind == "insert_part":
            ch = next(letters)
            legend[ch] = s.ref
            for h in s.holes:
                marks[h] = ch
        elif s.kind == "jumper":
            for h in s.holes:
                if h[0] in ROWS_TOP + ROWS_BOT and h[1:].isdigit():
                    marks[h] = "o"
    cur = steps[upto] if upto < len(steps) else None
    if cur is not None and cur.kind in ("jumper", "insert_part"):
        for h in cur.holes:
            if h[0] in ROWS_TOP + ROWS_BOT and h[1:].isdigit():
                marks[h] = "@"
    return marks, legend, cur


def ascii_diagram(layout: Layout, upto: int, steps: list[Step]) -> str:
    marks, legend, cur = _occupancy(layout, upto, steps)
    ncol = NCOLS
    hdr1 = "     " + "".join(str(c // 10) if c % 10 == 0 else " " for c in range(1, ncol + 1))
    hdr2 = "     " + "".join(str(c % 10) for c in range(1, ncol + 1))
    lines = [hdr1, hdr2]
    rail_hits: dict[str, set[int]] = {r: set() for r in RAILS}
    for s in steps[: upto + 1]:
        if s.kind == "jumper":
            for h in s.holes:
                for r in RAILS:
                    if h.startswith(r):
                        rail_hits[r].add(int(h[len(r) :]))
    cur_rails = set()
    if cur is not None and cur.kind == "jumper":
        cur_rails = {h for h in cur.holes if any(h.startswith(r) for r in RAILS)}

    def rail_line(r, sym):
        row = []
        for c in range(1, ncol + 1):
            h = f"{r}{c}"
            row.append("@" if h in cur_rails else "o" if c in rail_hits[r] else sym)
        return f"{r:>3}  " + "".join(row)

    lines.append(rail_line("T+", "+"))
    lines.append(rail_line("T-", "-"))
    for r in ROWS_TOP:
        lines.append(f"{r:>3}  " + "".join(marks.get(f"{r}{c}", ".") for c in range(1, ncol + 1)))
    lines.append("     " + "~" * ncol)
    for r in ROWS_BOT:
        lines.append(f"{r:>3}  " + "".join(marks.get(f"{r}{c}", ".") for c in range(1, ncol + 1)))
    lines.append(rail_line("B+", "+"))
    lines.append(rail_line("B-", "-"))
    leg = "  ".join(f"{k}={v}" for k, v in legend.items())
    lines.append(f"     legend: @=this step  o=jumper end  {leg}")
    return "\n".join(lines)


def svg_diagram(layout: Layout, upto: int, steps: list[Step]) -> str:
    _marks, legend, cur = _occupancy(layout, upto, steps)
    px, x0, y0 = 14, 40, 30
    rows = ["T+", "T-"] + list(ROWS_TOP) + ["~"] + list(ROWS_BOT) + ["B+", "B-"]
    ys = {r: y0 + i * px for i, r in enumerate(rows)}
    w, h = x0 + NCOLS * px + 20, y0 + len(rows) * px + 30
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" class="bb">']
    out.append(
        f'<rect x="{x0 - 10}" y="{y0 - 10}" width="{NCOLS * px + 10}" height="{len(rows) * px + 6}" '
        f'rx="6" class="board"/>'
    )

    def xy(hole):
        for r in RAILS:
            if hole.startswith(r):
                return x0 + (int(hole[len(r) :]) - 1) * px, ys[r]
        return x0 + (int(hole[1:]) - 1) * px, ys[hole[0]]

    for r in rows:
        if r == "~":
            continue
        cls = "rp" if r.endswith("+") else "rn" if r.endswith("-") else "hole"
        out.append(f'<text x="{x0 - 28}" y="{ys[r] + 4}" class="lbl">{r}</text>')
        for c in range(1, NCOLS + 1):
            out.append(f'<circle cx="{x0 + (c - 1) * px}" cy="{ys[r]}" r="2.2" class="{cls}"/>')
    for c in (1, 10, 20, 30, 40, 50, 60):
        out.append(f'<text x="{x0 + (c - 1) * px - 4}" y="{y0 - 14}" class="lbl">{c}</text>')
    if "P" in legend:
        out.append(
            f'<rect x="{x0 - 7}" y="{ys["c"] - 6}" width="{20 * px}" height="{ys["h"] - ys["c"] + 12}" '
            f'class="pico"/><text x="{x0 + 8 * px}" y="{(ys["e"] + ys["f"]) // 2 + 4}" class="plbl">Pico W</text>'
        )
    done = steps[: upto + 1]
    for s in done:
        hl = s is cur
        if s.kind == "insert_part" and s.holes:
            pts = [xy(hh) for hh in s.holes]
            for (ax, ay), (bx, by) in zip(pts, pts[1:], strict=False):
                out.append(f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" class="part{" cur" if hl else ""}"/>')
            for ax, ay in pts:
                out.append(f'<circle cx="{ax}" cy="{ay}" r="4" class="leg{" cur" if hl else ""}"/>')
            ax, ay = pts[0]
            out.append(f'<text x="{ax + 5}" y="{ay - 6}" class="plbl">{escape(s.ref)}</text>')
        elif s.kind == "jumper":
            ends = [hh for hh in s.holes if "." not in hh]
            if len(ends) == 2:
                (ax, ay), (bx, by) = xy(ends[0]), xy(ends[1])
                mx, my = (ax + bx) / 2, min(ay, by) - 18
                out.append(f'<path d="M{ax},{ay} Q{mx},{my} {bx},{by}" class="wire{" cur" if hl else ""}"/>')
            elif ends:
                ax, ay = xy(ends[0])
                out.append(f'<path d="M{ax},{ay} l30,-20" class="wire{" cur" if hl else ""}"/>')
                off = next(hh for hh in s.holes if "." in hh)
                out.append(f'<text x="{ax + 32}" y="{ay - 22}" class="plbl">{escape(off)}</text>')
    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------- photo check ---


@dataclass
class Discrepancy:
    kind: str  # part_missing | part_reversed | part_misplaced | wire_missing | wire_misplaced | wire_extra
    ref: str
    message: str
    instruction: str
    fix: dict = field(default_factory=dict)


def expected_observation(layout: Layout) -> dict:
    return {
        "placements": {k: dict(p.holes) for k, p in layout.placements.items() if p.holes},
        "wires": [[w.a, w.b] for w in layout.wires],
    }


def compare(layout: Layout, observed: dict) -> list[Discrepancy]:
    """Compare what the camera saw against the plan, at the electrical (strip)
    level -- a lead one hole over in the same 5-hole strip is not an error."""
    out: list[Discrepancy] = []
    obs_pl: dict[str, dict[str, str]] = observed.get("placements", {})
    obs_w = [tuple(w) for w in observed.get("wires", [])]
    used_obs: set[int] = set()
    for inst, pl in layout.placements.items():
        if not pl.holes:
            continue
        got = obs_pl.get(inst)
        if not got:
            out.append(
                Discrepancy(
                    "part_missing",
                    inst,
                    f"{inst} is not on the board",
                    f"Insert {inst} as in its step: " + ", ".join(f"{p} -> {h}" for p, h in pl.holes.items()),
                    {"action": "insert_part", "inst": inst},
                )
            )
            continue
        exp = {p: strip_of(h) for p, h in pl.holes.items()}
        obs = {p: strip_of(h) for p, h in got.items()}
        if exp == obs:
            continue
        pins = list(exp)
        if len(pins) == 2 and obs.get(pins[0]) == exp[pins[1]] and obs.get(pins[1]) == exp[pins[0]]:
            out.append(
                Discrepancy(
                    "part_reversed",
                    inst,
                    f"{inst} is in backwards",
                    f"Pull {inst} out and turn it around: {pins[0]} must be in "
                    f"{pl.holes[pins[0]]}, {pins[1]} in {pl.holes[pins[1]]}.",
                    {"action": "reverse_part", "inst": inst},
                )
            )
        else:
            bad = [p for p in pins if obs.get(p) != exp[p]]
            out.append(
                Discrepancy(
                    "part_misplaced",
                    inst,
                    f"{inst} leg(s) {bad} in the wrong strip",
                    "Move " + ", ".join(f"{inst}.{p} from {got.get(p)} to {pl.holes[p]}" for p in bad),
                    {"action": "move_part", "inst": inst},
                )
            )
    for w in layout.wires:
        key = w.key()
        hit = next((i for i, ow in enumerate(obs_w) if i not in used_obs and frozenset(map(strip_of, ow)) == key), None)
        if hit is not None:
            used_obs.add(hit)
            continue
        # one end matches -> misplaced end
        part = None
        for i, ow in enumerate(obs_w):
            if i in used_obs:
                continue
            ss = list(map(strip_of, ow))
            for good, other in ((w.a, w.b), (w.b, w.a)):
                if strip_of(good) in ss:
                    j = ss.index(strip_of(good))
                    wrong = ow[1 - j]
                    if strip_of(wrong) != strip_of(other):
                        part = (i, good, other, wrong)
                        break
            if part:
                break
        if part:
            i, good, other, wrong = part
            used_obs.add(i)
            out.append(
                Discrepancy(
                    "wire_misplaced",
                    w.id,
                    f"{w.id}: end at {wrong} should be at {other}",
                    f"Wire {w.id} (the one from {good}): move its other end from {wrong} to {other}.",
                    {"action": "move_wire", "wire": w.id, "from": wrong, "to": other},
                )
            )
        else:
            out.append(
                Discrepancy(
                    "wire_missing",
                    w.id,
                    f"{w.id} ({w.a} -> {w.b}) is missing",
                    f"Add jumper {w.id}: {describe_hole(w.a)} to {describe_hole(w.b)}.",
                    {"action": "add_wire", "wire": w.id},
                )
            )
    # a stray jumper exactly where a missing part belongs is the same mistake ("used a wire instead")
    for d in out:
        if d.kind != "part_missing":
            continue
        strips = {strip_of(h) for h in layout.placements[d.ref].holes.values()}
        for i, ow in enumerate(obs_w):
            if i not in used_obs and {strip_of(ow[0]), strip_of(ow[1])} == strips:
                used_obs.add(i)
                d.message = f"{d.ref} is missing and a jumper ({ow[0]}-{ow[1]}) is in its place"
                d.instruction = (
                    f"Remove the jumper between {ow[0]} and {ow[1]}, then "
                    + d.instruction[0].lower()
                    + d.instruction[1:]
                )
    # two jumpers with their ends exchanged (classic SDA/SCL swap) -> one mistake, one instruction
    mis = [d for d in out if d.kind == "wire_misplaced"]
    for i, d1 in enumerate(mis):
        for d2 in mis[i + 1 :]:
            if (
                d1 in out
                and d2 in out
                and strip_of(d1.fix["from"]) == strip_of(d2.fix["to"])
                and strip_of(d2.fix["from"]) == strip_of(d1.fix["to"])
            ):
                out.remove(d2)
                d1.kind, d1.ref = "wires_swapped", f"{d1.ref}+{d2.ref}"
                d1.message = f"{d1.ref.replace('+', ' and ')} are swapped ({d1.fix['from']} <-> {d2.fix['from']})"
                d1.instruction = (
                    f"Swap the ends of {d1.fix['wire']} and {d2.fix['wire']} that sit in "
                    f"{d1.fix['from']} and {d2.fix['from']}."
                )
                d1.fix = {"action": "multi", "fixes": [d1.fix, d2.fix]}
    for i, ow in enumerate(obs_w):
        if i not in used_obs:
            out.append(
                Discrepancy(
                    "wire_extra",
                    f"{ow[0]}-{ow[1]}",
                    f"unexpected wire {ow[0]} -> {ow[1]}",
                    f"Remove the wire between {ow[0]} and {ow[1]}.",
                    {"action": "remove_wire", "a": ow[0], "b": ow[1]},
                )
            )
    return out
