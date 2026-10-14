"""A simulated beginner who follows instructions -- and makes injected mistakes."""

from __future__ import annotations

from ..human import EST_SECONDS
from ..layout import Layout, strip_of
from ..wiring import Step
from .build import FAULT_KINDS, Build, BuiltWire, FaultRecord


class SimHuman:
    def __init__(self, bench, faults: list[str] | None = None, log=None):
        self.layout: Layout | None = None
        self.build: Build = bench.board.build
        self.bench = bench
        self.pending = list(faults or [])
        for f in self.pending:
            if f not in FAULT_KINDS:
                raise ValueError(f"unknown fault {f!r}; choose from {sorted(FAULT_KINDS)}")
        self.est_seconds = 0.0
        self.transcript: list[str] = []
        self.log = log or (lambda s: None)
        self.injected: list[FaultRecord] = []
        self.skipped: list[str] = []

    @property
    def L(self) -> Layout:
        assert self.layout is not None, "begin_build() first"
        return self.layout

    def begin_build(self, layout: Layout) -> None:
        self.layout = layout
        self.build.layout = layout

    # ------------------------------------------------------------- helpers
    def _note(self, s: str):
        self.transcript.append(s)
        self.log(s)

    def _take(self, kind: str) -> bool:
        if kind in self.pending:
            self.pending.remove(kind)
            return True
        return False

    def _fault(self, rec: FaultRecord):
        self.build.faults.append(rec)
        self.injected.append(rec)
        self._note(f"      (sim human mistake: {FAULT_KINDS[rec.kind]} -- {rec.detail})")

    def _kind(self, inst: str) -> str:
        inv = self.bench.board.inv
        return inv.parts[self.L.placements[inst].type_id].kind

    def _led_resistor(self) -> str | None:
        d = self.L.design
        for inst, _t in d.parts.items():
            if self._kind(inst) != "resistor":
                continue
            for pin in ("1", "2"):
                net = d.net_of(f"{inst}.{pin}")
                if net and any(p.endswith(".anode") and self._kind(p.split(".")[0]) == "led" for p in d.nets[net]):
                    return inst
        return None

    def _is_gpio_signal_wire(self, w) -> bool:
        pico_strips = {strip_of(h) for k, h in self.L.pico_pins.items() if k.startswith("pico.GP")}
        nets_i2c = {n for n in self.L.design.nets if "SDA" in n or "SCL" in n}
        return (strip_of(w.a) in pico_strips or strip_of(w.b) in pico_strips) and w.net not in nets_i2c

    # ---------------------------------------------------------- wiring steps
    def perform_step(self, step: Step) -> None:
        self.est_seconds += EST_SECONDS.get(step.kind, 30)
        b = self.build
        if step.kind == "place_pico":
            b.pico_placed = True
        elif step.kind == "insert_part":
            inst = step.ref
            pl = self.L.placements[inst]
            holes = dict(pl.holes)
            kind = self._kind(inst)
            if kind == "led" and self._take("reversed_led"):
                planned = dict(holes)
                holes["anode"], holes["cathode"] = holes["cathode"], holes["anode"]
                self._fault(FaultRecord("reversed_led", inst, f"{inst} legs swapped", {"holes": planned}))
            if kind == "resistor" and inst == self._led_resistor() and self._take("missing_resistor"):
                wid = f"X{len(b.wires) + 1}"
                b.wires.append(BuiltWire(wid, holes["1"], holes["2"]))
                self._fault(
                    FaultRecord(
                        "missing_resistor",
                        inst,
                        f"jumper {holes['1']}-{holes['2']} instead of {inst}",
                        {"holes": dict(holes), "hide_wires": [wid]},
                    )
                )
            else:
                b.placements[inst] = {"type": pl.type_id, "holes": holes, "extra": list(pl.extra_holes)}
        elif step.kind == "jumper":
            w = next(x for x in self.L.wires if x.id == step.ref)
            bw = BuiltWire(w.id, w.a, w.b)
            if self._is_gpio_signal_wire(w) and self._take("wrong_pin"):
                # shift the Pico-side end one pin over
                pico_strips = {strip_of(h) for h in self.L.pico_pins.values()}
                end = "a" if strip_of(w.a) in pico_strips else "b"
                h = getattr(bw, end)
                col = int(h[1:])
                new = f"{h[0]}{col - 1 if col > 1 else col + 1}"
                setattr(bw, end, new)
                self._fault(
                    FaultRecord(
                        "wrong_pin",
                        w.id,
                        f"{w.id} end in {new} instead of {h}",
                        {"wire": (w.id, w.a, w.b), "wires": [(w.id, w.a, w.b)]},
                    )
                )
            elif self._is_gpio_signal_wire(w) and self._take("loose_wire"):
                bw.seated = False
                self._fault(FaultRecord("loose_wire", w.id, f"{w.id} not pushed in fully", {}))
            elif w.net.endswith("SDA") and "INA" not in w.net and "swapped_i2c" in self.pending:
                pass  # handled when the SCL wire arrives
            elif w.net.endswith("SCL") and "INA" not in w.net and self._take("swapped_i2c"):
                sda = next(
                    (
                        x
                        for x in b.wires
                        if any(lw.id == x.id and lw.net.endswith("SDA") and "INA" not in lw.net for lw in self.L.wires)
                    ),
                    None,
                )
                if sda:
                    pico = {strip_of(h) for h in self.L.pico_pins.values()}
                    ea = "a" if strip_of(sda.a) in pico else "b"
                    eb = "a" if strip_of(bw.a) in pico else "b"
                    pa, pb = getattr(sda, ea), getattr(bw, eb)
                    setattr(sda, ea, pb)
                    setattr(bw, eb, pa)
                    lw_sda = next(x for x in self.L.wires if x.id == sda.id)
                    self._fault(
                        FaultRecord(
                            "swapped_i2c",
                            w.id,
                            f"{sda.id}/{w.id} swapped at the Pico",
                            {"wires": [(lw_sda.id, lw_sda.a, lw_sda.b), (w.id, w.a, w.b)]},
                        )
                    )
            elif any(e.endswith(".vplus") for e in (w.a, w.b)) and self._take("servo_wrong_rail"):
                end = "a" if w.a.startswith("B+") else "b"
                setattr(bw, end, "T+" + getattr(bw, end)[2:])
                self._fault(
                    FaultRecord(
                        "servo_wrong_rail",
                        w.id,
                        f"{w.id} on T+ (3V3) instead of B+ (5V)",
                        {"wire": (w.id, w.a, w.b), "wires": [(w.id, w.a, w.b)]},
                    )
                )
            b.wires.append(bw)
        elif step.kind == "safety" and step.n > 0 and self._take("hidden_short"):
            b.hidden_shorts.append(("T+", "T-"))
            self._fault(FaultRecord("hidden_short", "T+/T-", "clipped lead bridging T+ and T-", {}))
        b.touch()

    def finish_wiring(self):
        for k in self.pending:
            self.skipped.append(k)
        self.pending.clear()

    # ----------------------------------------------------------- instructions
    def do(self, instruction: str, fix: dict | None = None) -> None:
        self.est_seconds += EST_SECONDS["fix"]
        self._note(f"      human: OK -- {instruction}")
        if fix:
            self._apply(fix)
        self.build.touch()
        for f in self.build.refresh_faults():
            self._note(f"      (sim: fault '{f.kind}' on {f.target} is now fixed)")

    def _apply(self, fix: dict) -> None:
        a = fix.get("action")
        b, L = self.build, self.L
        if a == "multi":
            for f in fix.get("fixes", []):
                self._apply(f)
            return
        plan_w = {w.id: w for w in L.wires}
        if a in ("reverse_part", "move_part", "insert_part", "verify_part"):
            inst = fix["inst"]
            pl = L.placements[inst]
            b.placements[inst] = {"type": pl.type_id, "holes": dict(pl.holes), "extra": list(pl.extra_holes)}
            if a in ("insert_part", "verify_part"):
                # anything sitting where the part belongs (e.g. a stand-in jumper) comes out
                strips = {strip_of(h) for h in pl.holes.values()}
                b.wires = [w for w in b.wires if w.id in plan_w or {strip_of(w.a), strip_of(w.b)} != strips]
        elif a in ("move_wire", "add_wire", "verify_wire", "reseat_wire"):
            wid = fix["wire"]
            pw = plan_w[wid]
            cur = b.wire(wid)
            if cur is None:
                b.wires.append(BuiltWire(wid, pw.a, pw.b))
            else:
                cur.a, cur.b, cur.seated = pw.a, pw.b, True
        elif a == "remove_wire":
            sa, sb = strip_of(fix["a"]), strip_of(fix["b"])
            b.wires = [w for w in b.wires if {strip_of(w.a), strip_of(w.b)} != {sa, sb}]
        elif a == "inspect_rails":
            b.hidden_shorts.clear()

    def clip_probe(self, channel: str, hole: str) -> None:
        self.est_seconds += EST_SECONDS["probe"]

    def press(self, inst: str, hold_s: float) -> None:
        self.est_seconds += EST_SECONDS["press"]
        board = self.bench.board
        board.pressed.add(inst)
        board.record()
        self.bench.wait(hold_s)
        board.pressed.discard(inst)
        board.record()

    def plug_usb(self) -> None:
        self.est_seconds += EST_SECONDS["power"]
        self.bench.power_on()

    def unplug_usb(self, reason: str) -> None:
        self.est_seconds += EST_SECONDS["power"]
        self._note("      " + self.bench.power_cut(reason))

    def say(self, msg: str) -> None:
        self.log(msg)
