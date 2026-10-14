"""Simulated Pico W + parts, evaluated from the *as-built* breadboard."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..inventory import Inventory
from ..layout import pico_hole, strip_of
from . import mna
from .build import Build

GPIO_R_OUT = 40.0  # ohms, approx RP2040 output impedance
PULL_R = 50e3  # internal pull-up/down
REG_R = 0.3  # 3V3 regulator output resistance (sim)
REG_LIMIT_A = 0.5  # beyond this the regulator collapses -> brownout
LOGIC_TH = 1.4  # logic analyzer threshold (V)
GPIO_TH = 1.65  # RP2040 input threshold (V)


@dataclass
class PinState:
    mode: str = "in"  # in | out | i2c | pwm
    value: int = 0
    pull: str = ""  # "" | up | down
    pwm_freq: float = 0.0
    pwm_duty: int = 0  # duty_u16

    def key(self):
        return (self.mode, self.value, self.pull, round(self.pwm_freq), self.pwm_duty)


@dataclass
class SimEnv:
    temperature_c: float = 22.4
    humidity_pct: float = 41.0
    seed: int = 0


@dataclass
class OledState:
    lines: list[str] = field(default_factory=list)


class SimBoard:
    def __init__(self, inv: Inventory, build: Build, env: SimEnv | None = None):
        self.inv = inv
        self.build = build
        self.env = env or SimEnv()
        self.rng = random.Random(self.env.seed)
        self.powered = False
        self.browned_out = False
        self.now = 0.0
        self.pins: dict[int, PinState] = {}
        self.onboard_led = 0
        self.pressed: set[str] = set()
        self.oled: dict[int, OledState] = {}
        self.servo_target: dict[str, float] = {}
        self.servo_moving_until: dict[str, float] = {}
        self.stress: list[str] = []
        self.log: list[tuple[float, tuple]] = []  # (t, state key) history for instruments
        self._topo_version = -1
        self._cache: dict = {}

    # ------------------------------------------------------------ topology
    def _find(self, x):
        p = self._parent.setdefault(x, x)
        if p != x:
            p = self._parent[x] = self._find(p)
        return p

    def _union(self, a, b):
        ra, rb = self._find(a), self._find(b)
        if ra != rb:
            self._parent[ra] = rb

    def _topology(self):
        if self._topo_version == self.build.version:
            return
        self._parent = {}
        b = self.build
        for w in b.wires:
            if w.seated:
                self._union(strip_of(w.a), strip_of(w.b))
        for s1, s2 in b.hidden_shorts:
            self._union(s1, s2)
        self._union("PICO_GND", "PICO_GND")
        if b.pico_placed:
            for num, label in self.inv.pico_header.items():
                s = strip_of(pico_hole(num))
                if label in ("GND", "AGND"):
                    self._union(s, "PICO_GND")
                elif label == "3V3":
                    self._union(s, "PICO_3V3")
                elif label == "VBUS":
                    self._union(s, "PICO_VBUS")
                elif label.startswith("GP"):
                    self._union(s, label)
        self._topo_version = b.version
        self._cache.clear()

    def node_of_hole(self, hole: str) -> str:
        self._topology()
        return self._find(strip_of(hole))

    def node_of_pin(self, inst: str, pin: str) -> str | None:
        self._topology()
        p = self.build.placements.get(inst)
        if p is None:
            return None
        if not p["holes"]:  # off-board: its connector socket is a node
            return self._find(f"{inst}.{pin}")
        h = p["holes"].get(pin)
        return self._find(strip_of(h)) if h else None

    def node_of_gpio(self, n: int) -> str:
        self._topology()
        return self._find(f"GP{n}")

    @property
    def gnd(self) -> str:
        self._topology()
        return self._find("PICO_GND")

    # ------------------------------------------------------------- state
    def state_key(self, extra_inputs: frozenset = frozenset()):
        servo_moving = tuple(sorted(i for i, t in self.servo_moving_until.items() if t > self.now))
        return (
            self.powered,
            tuple(sorted((n, s.key()) for n, s in self.pins.items())),
            tuple(sorted(self.pressed)),
            servo_moving,
            extra_inputs,
        )

    def record(self):
        self.log.append((self.now, self.state_key()))
        if len(self.log) > 20000:
            self.log = self.log[-10000:]

    def pin(self, n: int) -> PinState:
        return self.pins.setdefault(n, PinState())

    def reset_pins(self):
        self.pins.clear()
        self.onboard_led = 0
        self.oled.clear()
        self.record()

    # ------------------------------------------------------------ solving
    def _circuit(self, key) -> mna.Circuit:
        powered, pins, pressed, servo_moving, _extra = key
        pins = dict(pins)
        c = mna.Circuit(ground=self.gnd)
        F = self._find
        if powered and self.build.pico_placed:
            c.sources.append(mna.Source(F("PICO_3V3"), 3.3, REG_R, "reg3v3"))
            c.sources.append(mna.Source(F("PICO_VBUS"), 5.0, 0.2, "vbus"))
            for n, (mode, value, pull, _freq, duty) in pins.items():
                node = F(f"GP{n}")
                if mode == "out":
                    c.sources.append(mna.Source(node, 3.3 if value else 0.0, GPIO_R_OUT, f"GP{n}"))
                elif mode == "pwm":
                    c.sources.append(mna.Source(node, 3.3 * duty / 65535, GPIO_R_OUT, f"GP{n}"))
                if pull == "up":
                    c.sources.append(mna.Source(node, 3.3, PULL_R, f"pull{n}"))
                elif pull == "down":
                    c.sources.append(mna.Source(node, 0.0, PULL_R, f"pull{n}"))
        for inst, p in self.build.placements.items():
            pt = self.inv.parts[p["type"]]
            nd = lambda pin: self.node_of_pin(inst, pin) or ""  # noqa: E731  ("" = missing terminal, dropped below)
            k = pt.kind
            if k == "resistor":
                c.res.append(mna.Res(nd("1"), nd("2"), float(pt.get("ohms")), inst))
            elif k == "led":
                c.diodes.append(mna.Diode(nd("anode"), nd("cathode"), float(pt.get("vf", 2.0)), 15.0, inst))
            elif k == "buzzer":
                c.diodes.append(mna.Diode(nd("plus"), nd("minus"), 0.3, 120.0, inst))
            elif k == "npn":
                c.npns.append(mna.NPN(nd("b"), nd("c"), nd("e"), inst))
            elif k == "button":
                if inst in pressed:
                    c.res.append(mna.Res(nd("a"), nd("b"), 0.05, inst))
            elif k == "dht22":
                c.res.append(mna.Res(nd("vcc"), nd("gnd"), 2200, inst))
                c.res.append(mna.Res(nd("data"), nd("vcc"), 10e3, inst + ":pu"))
            elif k == "ssd1306":
                c.res.append(mna.Res(nd("vcc"), nd("gnd"), 220, inst))
                c.res.append(mna.Res(nd("sda"), nd("vcc"), float(pt.get("pullup_ohms", 4700)), inst + ":pu_sda"))
                c.res.append(mna.Res(nd("scl"), nd("vcc"), float(pt.get("pullup_ohms", 4700)), inst + ":pu_scl"))
            elif k == "ina219":
                c.res.append(mna.Res(nd("vcc"), nd("gnd"), 3300, inst))
                c.res.append(
                    mna.Res(nd("vin_plus"), nd("vin_minus"), float(pt.get("shunt_ohms", 0.1)), "shunt:" + inst)
                )
                c.res.append(mna.Res(nd("sda"), nd("vcc"), float(pt.get("pullup_ohms", 10e3)), inst + ":pu_sda"))
                c.res.append(mna.Res(nd("scl"), nd("vcc"), float(pt.get("pullup_ohms", 10e3)), inst + ":pu_scl"))
            elif k == "servo":
                r = 20.0 if inst in servo_moving else 500.0
                c.res.append(mna.Res(nd("vplus"), nd("gnd"), r, inst))
        # drop elements with a missing terminal
        c.res = [r for r in c.res if r.a and r.b]
        c.diodes = [d for d in c.diodes if d.a and d.k]
        c.npns = [q for q in c.npns if q.b and q.c and q.e]
        return c

    def solve(self, key=None) -> mna.Solution:
        self._topology()
        key = key or self.state_key()
        if key not in self._cache:
            self._cache[key] = mna.solve(self._circuit(key))
        return self._cache[key]

    def check_power(self) -> None:
        """Update brownout / stress flags for the current state."""
        if not self.powered:
            return
        sol = self.solve()
        i_reg = sol.source_current("reg3v3")
        if i_reg > REG_LIMIT_A:
            if not self.browned_out:
                self.stress.append(f"t={self.now:.2f}s 3V3 regulator overload ({i_reg * 1000:.0f} mA) -> brownout")
            self.browned_out = True
        for n, s in self.pins.items():
            if s.mode in ("out", "pwm"):
                i = abs(sol.source_current(f"GP{n}")) * 1000
                if i > self.inv.limits["gpio_max_ma"]:
                    msg = f"GP{n} overloaded ({i:.0f} mA)"
                    if msg not in self.stress:
                        self.stress.append(msg)

    # ------------------------------------------------------- observations
    def gpio_read(self, n: int) -> int:
        sol = self.solve()
        return int(sol.volt(self.node_of_gpio(n)) > GPIO_TH)

    def hole_voltage(self, hole: str, key=None) -> float:
        return self.solve(key).volt(self.node_of_hole(hole))

    def led_state(self, inst: str, key=None) -> float:
        """LED/buzzer current in mA."""
        return self.solve(key).diode_current(inst) * 1000

    def ina_current_ma(self, inst: str = "ina", key=None) -> float:
        return self.solve(key).res_current("shunt:" + inst) * 1000

    def ina_bus_v(self, inst: str = "ina", key=None) -> float:
        return self.solve(key).volt(self.node_of_pin(inst, "vin_minus"))

    def device_powered(self, inst: str, sol=None) -> bool:
        sol = sol or self.solve()
        pt = self.inv.parts[self.build.placements[inst]["type"]]
        vcc_pin = next(p.name for p in pt.pins if p.role == "power_in")
        gnd_pin = next(p.name for p in pt.pins if p.role == "gnd")
        v = sol.volt(self.node_of_pin(inst, vcc_pin)) - sol.volt(self.node_of_pin(inst, gnd_pin))
        return v >= float(pt.get("v_min", 3.0)) - 0.35

    def i2c_devices(self, sda: int, scl: int) -> dict[int, str]:
        if not self.powered or self.browned_out:
            return {}
        # bus idles with the Pico pins released: check the pull-ups lift the lines
        saved = {n: self.pins.get(n) for n in (sda, scl)}
        for n in (sda, scl):
            self.pins[n] = PinState(mode="i2c")
        sol = self.solve()
        for n, s in saved.items():
            if s is None:
                self.pins.pop(n, None)
            else:
                self.pins[n] = s
        n_sda, n_scl = self.node_of_gpio(sda), self.node_of_gpio(scl)
        if sol.volt(n_sda) < 2.3 or sol.volt(n_scl) < 2.3:
            return {}
        out = {}
        for inst, p in self.build.placements.items():
            pt = self.inv.parts[p["type"]]
            if "i2c_addr" not in pt.facts:
                continue
            if (
                self.node_of_pin(inst, "sda") == n_sda
                and self.node_of_pin(inst, "scl") == n_scl
                and self.device_powered(inst, sol)
            ):
                out[int(pt.get("i2c_addr"))] = inst
        return out

    def dht_read(self, gpio: int) -> tuple[float, float] | None:
        if not self.powered or self.browned_out:
            return None
        sol = self.solve()
        node = self.node_of_gpio(gpio)
        for inst, p in self.build.placements.items():
            if self.inv.parts[p["type"]].kind != "dht22":
                continue
            if self.node_of_pin(inst, "data") == node and self.device_powered(inst, sol):
                t = self.env.temperature_c + self.rng.uniform(-0.2, 0.2)
                h = self.env.humidity_pct + self.rng.uniform(-0.5, 0.5)
                return round(t, 1), round(h, 1)
        return None

    def servo_angle(self, inst: str) -> float | None:
        """Angle the horn points at, or None if it never received a valid 50 Hz command while powered."""
        return self.servo_target.get(inst)

    def update_servos(self):
        """Called after a PWM change: servos whose signal node carries a valid
        50 Hz pulse train get a new target angle."""
        if not self.powered:
            return
        for inst, p in self.build.placements.items():
            if self.inv.parts[p["type"]].kind != "servo":
                continue
            sig = self.node_of_pin(inst, "sig")
            for n, s in self.pins.items():
                if s.mode == "pwm" and self.node_of_gpio(n) == sig and 40 <= s.pwm_freq <= 60:
                    pulse_ms = s.pwm_duty / 65535 * 1000 / s.pwm_freq
                    ang = max(0.0, min(180.0, (pulse_ms - 0.5) / 2.0 * 180.0))
                    sol = self.solve()
                    v = sol.volt(self.node_of_pin(inst, "vplus")) - sol.volt(self.node_of_pin(inst, "gnd"))
                    if v < 4.0:
                        continue  # under-powered: the horn doesn't follow the command
                    prev = self.servo_target.get(inst)
                    if prev is None or abs(prev - ang) > 0.5:
                        self.servo_moving_until[inst] = self.now + 0.4
                    self.servo_target[inst] = round(ang, 1)

    def scene(self) -> dict:
        """Ground truth a camera could see right now."""
        leds, sol = {}, self.solve()
        for inst, p in self.build.placements.items():
            k = self.inv.parts[p["type"]].kind
            if k == "led":
                leds[inst] = round(sol.diode_current(inst) * 1000, 2)
        servos = {
            i: self.servo_angle(i)
            for i, p in self.build.placements.items()
            if self.inv.parts[p["type"]].kind == "servo"
        }
        oled = {}
        for inst, p in self.build.placements.items():
            pt = self.inv.parts[p["type"]]
            if pt.kind == "ssd1306":
                addr = int(pt.get("i2c_addr"))
                lit = self.powered and not self.browned_out and self.device_powered(inst, sol)
                oled[inst] = list(self.oled.get(addr, OledState()).lines) if lit else []
        return {
            "t": round(self.now, 3),
            "powered": self.powered and not self.browned_out,
            "led_ma": leds,
            "onboard_led": self.onboard_led,
            "oled": oled,
            "servo_deg": servos,
        }
