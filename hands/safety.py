"""The hard safety envelope Hands never leaves, whatever the model or the YAML says.

Three layers use this module:

* :func:`screen_text` refuses a gadget *spec* (before any model call) or a
  model-written instruction that asks for mains power, batteries, voltages
  above USB 5 V, or high-current loads.
* :data:`HARD_LIMITS` caps every electrical limit. ``starter_kit.yaml`` may make
  a limit *stricter*, never looser (see :func:`effective_limits`).
* :data:`DESIGN_KINDS` is the closed set of part kinds the rule checker has
  rules for. Anything else is refused, even if someone adds it to the YAML.

Every number here is documented, with its datasheet source, in ``docs/safety.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Absolute ceilings. Sources: docs/safety.md.
HARD_LIMITS: dict[str, float] = {
    "gpio_voltage_v": 3.3,  # RP2040/RP2350 IOVDD on Pico boards; treat GPIO as NOT 5 V tolerant
    "gpio_warn_ma": 8.0,  # comfortable per-pin load
    "gpio_max_ma": 12.0,  # highest programmable RP2040/RP2350 drive strength
    "gpio_total_max_ma": 50.0,  # RP2040 total IOVDD current
    "rail_3v3_max_ma": 300.0,  # Pico W datasheet: keep 3V3(OUT) load under 300 mA
    "vbus_max_ma": 500.0,  # USB 2.0 port budget
    "supply_max_v": 5.25,  # USB VBUS upper tolerance; nothing above USB 5 V, ever
    "resistor_max_w": 0.125,  # half of a 1/4 W resistor's rating
    "pico_self_ma": 100.0,  # Pico W's own worst-case draw from VBUS (RP2040 + wireless chip)
}

#: Part kinds the rule checker understands. Fixture/tool kinds are handled separately.
DESIGN_KINDS = frozenset({"led", "resistor", "buzzer", "npn", "button", "dht22", "ssd1306", "servo"})
FIXTURE_KINDS = frozenset({"mcu", "ina219"})
TOOL_KINDS = frozenset({"breadboard", "jumpers"})

REFUSAL_HELP = (
    "Hands only builds low-voltage gadgets powered from a computer's USB port (5 V, under 500 mA), "
    "using the parts in your inventory. Rephrase the idea without that, or build it with an experienced "
    "adult and proper equipment."
)


@dataclass(frozen=True)
class Refusal:
    category: str
    message: str

    def __str__(self) -> str:
        return f"[{self.category}] {self.message}"


_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "mains",
        re.compile(
            r"\b(mains|wall\s*(outlet|socket|plug|power)|power\s*outlet|ac\s*(power|mains|line)|household\s*"
            r"(power|current|electricity)|line\s*voltage|1[12]0\s*v(ac|olts?)?|2[2-4]0\s*v(ac|olts?)?|"
            r"extension\s*cord|light\s*switch\s*wiring)\b",
            re.I,
        ),
        "Mains (wall) electricity can kill. Hands never designs anything that connects to it.",
    ),
    (
        "battery",
        re.compile(
            r"\b(li-?po|lipo|lithium|li-?ion|18650|9\s*v\s*batter(y|ies)|car\s*batter(y|ies)|batter(y|ies)|"
            r"power\s*bank|solar\s*panel)\b",
            re.I,
        ),
        "Batteries (especially lithium cells) can catch fire if shorted or wired backwards. Hands only uses USB power.",
    ),
    (
        "high_current",
        re.compile(
            r"\b(dc\s*motor|stepper|solenoid|heater|heating\s*element|hot\s*plate|pump|fan\s*motor|"
            r"led\s*strip|neopixel\s*strip|high[-\s]*current|high[-\s]*power|relay|tesla\s*coil|laser)\b",
            re.I,
        ),
        "That needs more current than a USB port can safely give, or a part outside the safe starter kit.",
    ),
    (
        "high_voltage",
        re.compile(r"\bhigh[-\s]*voltage\b", re.I),
        "Hands never works above USB 5 V.",
    ),
]

_VOLTS = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:v|volts?|vdc|vac)\b", re.I)
# "500 mA", "2 amps", "2A" (a bare "a" must be attached to the number: "3 a day" is not a current)
_AMPS = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?:\s*(ma|milliamps?|amps?)|(a))\b", re.I)


def screen_text(text: str) -> list[Refusal]:
    """Return refusals for a spec or instruction that leaves the safe envelope.

    Deliberately conservative: a false refusal costs a rephrase, a false
    accept could cost a fire.
    """
    out: list[Refusal] = []
    for cat, pat, msg in _PATTERNS:
        if pat.search(text):
            out.append(Refusal(cat, msg))
    for m in _VOLTS.finditer(text):
        if float(m.group(1)) > HARD_LIMITS["supply_max_v"]:
            out.append(Refusal("high_voltage", f"{m.group(0)} is above USB 5 V. Hands never works above 5 V."))
            break
    for m in _AMPS.finditer(text):
        val, unit = float(m.group(1)), (m.group(2) or m.group(3)).lower()
        ma = val if unit.startswith("m") else val * 1000
        if ma > HARD_LIMITS["vbus_max_ma"]:
            out.append(Refusal("high_current", f"{m.group(0)} is more than a USB port's 500 mA."))
            break
    seen: set[str] = set()
    uniq = []
    for r in out:
        if r.category not in seen:
            seen.add(r.category)
            uniq.append(r)
    return uniq


def refusal_message(refusals: list[Refusal]) -> str:
    lines = ["Hands will not build this:"] + [f"  - {r.message}" for r in refusals] + ["", REFUSAL_HELP]
    return "\n".join(lines)


class SafetyRefusal(Exception):
    """Raised when a request is outside the hard safety envelope."""

    def __init__(self, refusals: list[Refusal]):
        self.refusals = refusals
        super().__init__(refusal_message(refusals))


def require_safe_text(text: str) -> None:
    r = screen_text(text)
    if r:
        raise SafetyRefusal(r)


def effective_limits(yaml_limits: dict[str, float]) -> dict[str, float]:
    """YAML limits clamped to the hard ceilings (the YAML can only tighten them)."""
    out = dict(HARD_LIMITS)
    for k, v in yaml_limits.items():
        if k in HARD_LIMITS:
            out[k] = min(float(v), HARD_LIMITS[k])
    return out
