"""Circuit representation: a logical netlist of part pins and Pico pins.

A design (usually produced by the LLM) is JSON like::

    {
      "name": "steelers-light",
      "parts": {"r1": "resistor_220", "led1": "led_yellow"},
      "nets": {
        "LIGHT":   ["pico.GP15", "r1.1"],
        "LIGHT_A": ["r1.2", "led1.anode"],
        "GND":     ["led1.cathode"]
      },
      "pin_map": {"LIGHT": "GP15"}
    }

Three net names are reserved for the breadboard power rails: ``3V3`` (measured
3.3 V rail, fed through the INA219), ``5V`` (USB VBUS) and ``GND``. The bench
fixture (Pico + INA219 + rails) is added by :func:`with_fixture`, so the LLM
never wires the Pico power pins itself.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .inventory import Inventory
from .safety import DESIGN_KINDS

RAIL_NETS = ("3V3", "5V", "GND")
FIXTURE_PARTS = {"pico": "pico", "ina": "ina219"}
#: Net names the bench fixture owns. A gadget design must never use them: ``V3_SRC`` is the
#: *unmeasured* 3.3 V (before the INA219 shunt) and the INA nets are the instrument's I2C bus.
FIXTURE_NETS = ("V3_SRC", "INA_SDA", "INA_SCL")
RESERVED_GPIO = {"GP26": "bench INA219 I2C1 SDA", "GP27": "bench INA219 I2C1 SCL"}
INA_I2C = {"bus": 1, "sda": 26, "scl": 27, "addr": 0x40}

#: Names that look like a power net but are not one of the three real rails. Wiring a part to
#: "Gnd" or "VCC" would silently create a *signal* net the checker cannot reason about.
POWER_ALIASES = {
    "gnd",
    "ground",
    "0v",
    "vss",
    "agnd",
    "vcc",
    "vdd",
    "vin",
    "v+",
    "3v3",
    "3.3v",
    "3v",
    "+3v3",
    "+3.3v",
    "5v",
    "+5v",
    "5v0",
    "vbus",
    "vsys",
    "usb",
    "power",
    "pwr",
    "v3_src",
    "3v3_out",
}
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,23}$")
_NET = re.compile(r"^[A-Za-z0-9_+\-]{1,32}$")
_GPIO = re.compile(r"^GP([0-9]{1,2})$")
MAX_PARTS, MAX_NETS, MAX_PINS_PER_NET = 40, 80, 40


class DesignError(ValueError):
    pass


@dataclass(frozen=True)
class PinRef:
    inst: str
    pin: str

    @classmethod
    def parse(cls, s: str) -> PinRef:
        if "." not in s:
            raise DesignError(f"bad pin reference {s!r}; expected 'part.pin'")
        inst, pin = s.split(".", 1)
        return cls(inst, pin)

    def __str__(self) -> str:
        return f"{self.inst}.{self.pin}"


@dataclass
class Design:
    name: str
    parts: dict[str, str]
    nets: dict[str, list[str]]
    pin_map: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    fixture: bool = False

    # ---- construction -------------------------------------------------
    @classmethod
    def from_dict(cls, d: Any) -> Design:
        """Strictly typed parse of model output. Anything odd is a :class:`DesignError`."""
        if not isinstance(d, dict):
            raise DesignError("design must be a JSON object")
        parts, nets, pin_map = d.get("parts"), d.get("nets"), d.get("pin_map", {})
        if pin_map is None:
            pin_map = {}
        if not isinstance(parts, dict) or not isinstance(nets, dict) or not isinstance(pin_map, dict):
            raise DesignError("design JSON needs object fields 'parts', 'nets' (and optional 'pin_map')")
        for k, v in nets.items():
            if not isinstance(v, list) or not all(isinstance(p, str) for p in v):
                raise DesignError(f"net {k!r} must be a list of 'part.pin' strings")
        for k, v in list(parts.items()) + list(pin_map.items()):
            if not isinstance(v, str):
                raise DesignError(f"{k!r} must map to a string, got {type(v).__name__}")
        return cls(
            name=str(d.get("name", "design"))[:64],
            parts={str(k): v for k, v in parts.items()},
            nets={str(k): list(v) for k, v in nets.items()},
            pin_map={str(k): v for k, v in pin_map.items()},
            notes=str(d.get("notes", ""))[:2000],
        )

    @classmethod
    def from_json(cls, text: str) -> Design:
        return cls.from_dict(extract_json(text))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "parts": self.parts, "nets": self.nets, "pin_map": self.pin_map, "notes": self.notes}

    # ---- queries ------------------------------------------------------
    def net_of(self, ref: str) -> str | None:
        for net, pins in self.nets.items():
            if ref in pins:
                return net
        return None

    def pins_of(self, inst: str) -> list[str]:
        return [p for pins in self.nets.values() for p in pins if p.startswith(inst + ".")]

    def part_type(self, inst: str) -> str:
        if inst in self.parts:
            return self.parts[inst]
        if inst in FIXTURE_PARTS:
            return FIXTURE_PARTS[inst]
        raise DesignError(f"unknown part instance {inst!r}")

    def gpio_nets(self) -> dict[str, str]:
        """GPIO label -> net name."""
        out = {}
        for net, pins in self.nets.items():
            for p in pins:
                r = PinRef.parse(p)
                if r.inst == "pico" and r.pin.startswith("GP"):
                    out[r.pin] = net
        return out

    def instances_of_kind(self, inv: Inventory, kind: str) -> list[str]:
        return [i for i, t in self.all_parts().items() if t in inv.parts and inv.parts[t].kind == kind]

    def all_parts(self) -> dict[str, str]:
        d = dict(self.parts)
        if self.fixture:
            d = {**FIXTURE_PARTS, **d}
        return d


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of an LLM reply (tolerates ```json fences)."""
    text = text.strip()
    if "```" in text:
        chunks = text.split("```")
        for c in chunks[1::2]:
            c = c.strip()
            if c.startswith("json"):
                c = c[4:]
            try:
                obj = json.loads(c)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise DesignError("no JSON object found in model output")
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise DesignError(f"model output is not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise DesignError("model output is not a JSON object")
    return obj


def validate_structure(design: Design, inv: Inventory, *, gadget: bool = True) -> list[str]:
    """Structural problems (bad references, aliasing, unsupported parts).

    ``gadget=True`` applies the rules for a model-written design: only header
    GPIOs of the Pico may be referenced (power and control pins are wired by
    the bench fixture), fixture net names are off limits, and every part must
    be of a kind the rule checker understands.
    """
    errs: list[str] = []
    seen: dict[str, str] = {}
    parts = design.all_parts()
    if len(design.parts) > MAX_PARTS or len(design.nets) > MAX_NETS:
        errs.append(f"design too large (max {MAX_PARTS} parts, {MAX_NETS} nets)")
        return errs
    for inst, t in design.parts.items():
        if inst in FIXTURE_PARTS:
            errs.append(f"instance name {inst!r} is reserved for the bench fixture")
        elif not _NAME.match(inst):
            errs.append(f"instance name {inst!r} must be a letter followed by letters/digits/_ (max 24)")
        if t not in inv.parts:
            errs.append(f"{inst}: part type {t!r} is not in the inventory")
        elif gadget and inv.parts[t].kind not in DESIGN_KINDS:
            errs.append(
                f"{inst}: part type {t!r} (kind {inv.parts[t].kind!r}) cannot be used in a design; "
                f"the safety rules only cover {sorted(DESIGN_KINDS)}"
            )
    for net, pins in design.nets.items():
        if not _NET.match(net):
            errs.append(f"net name {net!r} must be 1-32 letters, digits, '_', '+' or '-'")
        if gadget and net in FIXTURE_NETS:
            errs.append(f"net name {net!r} is reserved for the bench fixture (use 3V3 / 5V / GND)")
        if gadget and net not in RAIL_NETS and net.lower() in POWER_ALIASES:
            errs.append(f"net name {net!r} looks like a power rail; power nets must be named exactly 3V3, 5V or GND")
        if len(pins) > MAX_PINS_PER_NET:
            errs.append(f"net {net} has too many pins")
        for p in pins:
            try:
                r = PinRef.parse(p)
            except DesignError as e:
                errs.append(str(e))
                continue
            if p in seen and seen[p] != net:
                errs.append(f"{p} appears on two nets ({seen[p]} and {net})")
            seen[p] = net
            if r.inst == "pico":
                if r.pin not in set(inv.pico_header.values()):
                    errs.append(f"{p}: the Pico has no pin labelled {r.pin}")
                elif gadget and not _GPIO.match(r.pin):
                    errs.append(
                        f"{p}: designs may only use Pico GPIO pins (GPn). Power and control pins "
                        f"({r.pin}) are wired by the bench fixture; connect to the net 3V3, 5V or GND"
                    )
                continue
            if r.inst not in parts:
                errs.append(f"{p}: unknown part instance {r.inst!r}")
                continue
            t = parts[r.inst]
            if t in inv.parts and r.pin not in inv.parts[t].pin_names():
                errs.append(f"{p}: {t} has no pin {r.pin!r} (pins: {inv.parts[t].pin_names()})")
    for role, gp in design.pin_map.items():
        if not _GPIO.match(gp):
            errs.append(f"pin_map[{role}]={gp!r} is not a GPIO label like 'GP15'")
    return errs


def gpio_number(label: str) -> int:
    m = _GPIO.match(label)
    if not m:
        raise DesignError(f"{label!r} is not a GPIO label")
    return int(m.group(1))


def with_fixture(design: Design) -> Design:
    """Return a copy of ``design`` merged with the bench fixture.

    Fixture: Pico 3V3(OUT) -> INA219 VIN+ (and VCC); INA219 VIN- -> top + rail
    (net '3V3'); Pico GND -> GND rails; Pico VBUS -> bottom + rail (net '5V',
    only if used); INA219 on I2C1 (GP26 SDA / GP27 SCL).
    """
    if design.fixture:
        return design
    d = copy.deepcopy(design)
    nets = d.nets
    nets.setdefault("GND", [])
    nets["GND"] = ["pico.GND", "ina.gnd"] + nets["GND"]
    nets["V3_SRC"] = ["pico.3V3", "ina.vin_plus", "ina.vcc"]
    nets.setdefault("3V3", [])
    nets["3V3"] = ["ina.vin_minus"] + nets["3V3"]
    if nets.get("5V"):
        nets["5V"] = ["pico.VBUS"] + nets["5V"]
    nets["INA_SDA"] = ["pico.GP26", "ina.sda"] + nets.get("INA_SDA", [])
    nets["INA_SCL"] = ["pico.GP27", "ina.scl"] + nets.get("INA_SCL", [])
    d.fixture = True
    return d
