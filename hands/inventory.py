"""Inventory model: what parts the human actually owns, plus electrical facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .safety import effective_limits

DEFAULT_INVENTORY = Path(__file__).parent / "data" / "starter_kit.yaml"


@dataclass(frozen=True)
class PinSpec:
    name: str
    role: str
    hint: str = ""


@dataclass
class PartType:
    id: str
    kind: str
    name: str
    qty: int
    mount: str
    pins: list[PinSpec] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def pin(self, name: str) -> PinSpec:
        for p in self.pins:
            if p.name == name:
                return p
        raise KeyError(f"{self.id} has no pin {name!r} (has {[p.name for p in self.pins]})")

    def pin_names(self) -> list[str]:
        return [p.name for p in self.pins]

    def get(self, key: str, default: Any = None) -> Any:
        return self.facts.get(key, default)


@dataclass
class Inventory:
    parts: dict[str, PartType]
    limits: dict[str, float]
    instruments: dict[str, Any]
    pico_header: dict[int, str]
    pico_internal_gpio: list[int]
    pico_i2c: dict[str, dict[str, list[int]]]

    def part(self, type_id: str) -> PartType:
        if type_id not in self.parts:
            raise KeyError(f"part type {type_id!r} is not in the inventory")
        return self.parts[type_id]

    def pico_pin_number(self, label: str) -> int:
        """Physical header pin (1-40) for a label like 'GP15' or '3V3'.
        For GND, returns the first GND pin; use pico_pins_with(label) for all."""
        for num, lab in self.pico_header.items():
            if lab == label:
                return num
        raise KeyError(f"Pico has no pin labelled {label!r}")

    def pico_pins_with(self, label: str) -> list[int]:
        return [n for n, lab in self.pico_header.items() if lab == label]

    def summary_for_prompt(self) -> str:
        lines = []
        for p in self.parts.values():
            if p.mount == "none":
                continue
            pins = ", ".join(f"{s.name}({s.role})" for s in p.pins)
            facts = {k: v for k, v in p.facts.items() if k not in ("header", "i2c", "internal_gpio", "adc_gpio")}
            lines.append(f"- {p.id} x{p.qty}: {p.name}; pins: [{pins}]; facts: {facts}")
        return "\n".join(lines)


class InventoryError(ValueError):
    pass


def load_inventory(path: str | Path | None = None) -> Inventory:
    """Load the parts YAML. Limits are clamped to :data:`hands.safety.HARD_LIMITS`:
    editing the YAML can make Hands stricter, never looser."""
    data = yaml.safe_load(Path(path or DEFAULT_INVENTORY).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("parts"), dict):
        raise InventoryError(f"{path or DEFAULT_INVENTORY}: expected a mapping with a 'parts' section")
    parts: dict[str, PartType] = {}
    pico_header: dict[int, str] = {}
    internal: list[int] = []
    i2c: dict[str, dict[str, list[int]]] = {}
    for pid, raw in data["parts"].items():
        if not isinstance(raw, dict) or "kind" not in raw or "name" not in raw:
            raise InventoryError(f"part {pid!r} needs at least 'kind' and 'name'")
        raw = dict(raw)
        pins_raw = raw.pop("pins", [])
        pins = [PinSpec(**p) if isinstance(p, dict) else PinSpec(str(p), "passive") for p in pins_raw]
        kind = raw.pop("kind")
        name = raw.pop("name")
        qty = int(raw.pop("qty", 1))
        mount = raw.pop("mount", "none")
        if kind == "mcu":
            pico_header = {int(k): str(v) for k, v in raw["header"].items()}
            internal = list(raw.get("internal_gpio", []))
            i2c = raw.get("i2c", {})
        parts[pid] = PartType(pid, kind, name, qty, mount, pins, raw)
    return Inventory(
        parts=parts,
        limits=effective_limits({k: float(v) for k, v in (data.get("limits") or {}).items()}),
        instruments=data.get("instruments", {}),
        pico_header=pico_header,
        pico_internal_gpio=internal,
        pico_i2c=i2c,
    )
