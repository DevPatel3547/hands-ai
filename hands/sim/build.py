"""The *as-built* physical state of the breadboard (what the human actually did),
which can differ from the plan. Faults are injected here."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from ..layout import Layout, strip_of

# fault kinds and whether a top-down photo can, in principle, reveal them
FAULT_KINDS = {
    "reversed_led": "LED inserted backwards",
    "missing_resistor": "series resistor left out and replaced with a jumper",
    "wrong_pin": "a jumper plugged into the neighbouring Pico pin",
    "loose_wire": "a jumper that looks inserted but isn't making contact",
    "hidden_short": "stray lead clipping bridging the 3V3 and GND rails under the board edge",
    "swapped_i2c": "SDA and SCL jumpers swapped",
    "servo_wrong_rail": "servo red wire plugged into the 3V3 rail instead of 5V",
}
INVISIBLE = {"loose_wire", "hidden_short"}


@dataclass
class BuiltWire:
    id: str
    a: str
    b: str
    seated: bool = True


@dataclass
class FaultRecord:
    kind: str
    target: str
    detail: str
    planned: dict = field(default_factory=dict)


@dataclass
class Build:
    layout: Layout | None = None
    pico_placed: bool = False
    placements: dict[str, dict] = field(default_factory=dict)  # inst -> {"type", "holes", "extra"}
    wires: list[BuiltWire] = field(default_factory=list)
    hidden_shorts: list[tuple[str, str]] = field(default_factory=list)
    faults: list[FaultRecord] = field(default_factory=list)
    version: int = 0

    def touch(self) -> None:
        self.version += 1

    def wire(self, wid: str) -> BuiltWire | None:
        return next((w for w in self.wires if w.id == wid), None)

    # ---- what a camera would report ------------------------------------
    def observe(self, miss: set[str] | None = None) -> dict:
        """Top-down view. Invisible faults (and any kinds in ``miss``) are
        reported as if they were built to plan."""
        miss = set(miss or ()) | INVISIBLE
        pl = {k: dict(v["holes"]) for k, v in self.placements.items() if v["holes"]}
        wires = {w.id: [w.a, w.b] for w in self.wires}
        for f in self.faults:
            if f.kind not in miss:
                continue
            if "holes" in f.planned:
                pl[f.target] = dict(f.planned["holes"])
            for wid, a, b in f.planned.get("wires", []):
                wires[wid] = [a, b]
            for wid in f.planned.get("hide_wires", []):
                wires.pop(wid, None)
        return {"pico_placed": self.pico_placed, "placements": pl, "wires": list(wires.values())}

    # ---- fix bookkeeping -----------------------------------------------
    def refresh_faults(self) -> list[FaultRecord]:
        """Drop fault records whose effect has been undone. Returns fixed ones."""
        fixed: list[FaultRecord] = []
        keep: list[FaultRecord] = []
        for f in self.faults:
            ok = False
            if f.kind in ("reversed_led", "missing_resistor"):
                cur = self.placements.get(f.target)
                extra_gone = all(self.wire(w) is None for w in f.planned.get("hide_wires", []))
                ok = cur is not None and _same_strips(cur["holes"], f.planned["holes"]) and extra_gone
            elif f.kind in ("wrong_pin", "swapped_i2c", "servo_wrong_rail"):
                ok = True
                for wid, a, b in f.planned.get("wires", [f.planned.get("wire")]):
                    w = self.wire(wid)
                    ok &= w is not None and {strip_of(w.a), strip_of(w.b)} == {strip_of(a), strip_of(b)}
            elif f.kind == "loose_wire":
                w = self.wire(f.target)
                ok = w is not None and w.seated
            elif f.kind == "hidden_short":
                ok = not self.hidden_shorts
            (fixed if ok else keep).append(f)
        self.faults = keep
        return fixed


def _same_strips(a: dict, b: dict) -> bool:
    return {k: strip_of(v) for k, v in a.items()} == {k: strip_of(v) for k, v in b.items()}


def build_to_plan(layout: Layout) -> Build:
    """A perfect build (used by tests and as the base for fault injection)."""
    b = Build(layout, pico_placed=True)
    for inst, p in layout.placements.items():
        b.placements[inst] = {"type": p.type_id, "holes": dict(p.holes), "extra": list(p.extra_holes)}
    b.wires = [BuiltWire(w.id, w.a, w.b) for w in layout.wires]
    return b


def clone(b: Build) -> Build:
    return copy.deepcopy(b)
