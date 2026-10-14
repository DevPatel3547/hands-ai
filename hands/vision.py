"""Camera perception. The model only *describes* what it sees; deciding whether
that matches the plan is done by deterministic code (wiring.compare).

* :class:`SimVision` -- reads the simulator's scene.
* :class:`ModelVision` -- any vision-capable LLM client (Claude, or a local VLM through Ollama).
* :class:`ConsoleVision` -- no vision model (e.g. the free scripted mode on real hardware):
  the human answers simple yes/no questions instead. Weaker, but it keeps the loop working.

Model output is untrusted: :func:`sanitize_wiring` drops anything malformed, which can only
make the photo check *fail* (never pass by accident through bad data).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Protocol

from .bench.base import Snapshot
from .circuit import DesignError, extract_json
from .layout import Layout
from .llm import LLMClient
from .prompts import VISION_STATE_SYSTEM, VISION_WIRING_SYSTEM

LED_ON_MA = 0.5
HOLE = re.compile(
    r"^([a-j](?:[1-9]|[1-5][0-9]|6[0-3])|[TB][+-](?:[1-9]|[1-5][0-9]|6[0-3])|[a-z][a-z0-9_]{0,23}\.[a-z0-9_]+)$"
)


class Vision(Protocol):
    def observe_wiring(self, snap: Snapshot, layout: Layout) -> dict[str, Any]: ...
    def observe_state(self, snap: Snapshot, instances: list[str]) -> dict[str, Any]: ...


def sanitize_wiring(obs: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"placements": {}, "wires": [], "uncertain": []}
    if not isinstance(obs, dict):
        out["uncertain"] = ["unreadable vision output"]
        return out
    pl = obs.get("placements")
    if isinstance(pl, dict):
        for inst, pins in pl.items():
            if isinstance(inst, str) and isinstance(pins, dict):
                good = {str(p): h for p, h in pins.items() if isinstance(h, str) and HOLE.match(h)}
                if good:
                    out["placements"][inst] = good
    ws = obs.get("wires")
    if isinstance(ws, list):
        for w in ws[:200]:
            if isinstance(w, (list, tuple)) and len(w) == 2 and all(isinstance(h, str) and HOLE.match(h) for h in w):
                out["wires"].append([w[0], w[1]])
    unc = obs.get("uncertain")
    if isinstance(unc, list):
        out["uncertain"] = [str(u)[:200] for u in unc[:10]]
    out["pico_placed"] = bool(obs.get("pico_placed", True))
    return out


class SimVision:
    """Reads the simulator's scene. What it can 'see' of the wiring is already
    limited by the bench (loose contacts and hidden shorts are invisible)."""

    def observe_wiring(self, snap: Snapshot, layout: Layout) -> dict[str, Any]:
        assert snap.scene is not None
        w: dict[str, Any] = snap.scene["wiring"]
        return w

    def observe_state(self, snap: Snapshot, instances: list[str]) -> dict[str, Any]:
        s = snap.scene
        assert s is not None
        return {
            "leds": {k: ("on" if v > LED_ON_MA else "off") for k, v in s["led_ma"].items() if k in instances},
            "oled": {k: v for k, v in s["oled"].items() if k in instances},
            "servo_deg": {k: v for k, v in s["servo_deg"].items() if k in instances},
        }


class ModelVision:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def observe_wiring(self, snap: Snapshot, layout: Layout) -> dict[str, Any]:
        if not snap.path:
            raise ValueError("vision needs a photo file")
        plan = json.dumps(layout.to_dict()["placements"])
        reply = self.llm.complete(
            VISION_WIRING_SYSTEM + plan, "Describe the wiring in this photo.", stage="vision_wiring", images=[snap.path]
        )
        try:
            return sanitize_wiring(extract_json(reply))
        except DesignError:
            return sanitize_wiring(None)

    def observe_state(self, snap: Snapshot, instances: list[str]) -> dict[str, Any]:
        if not snap.path:
            raise ValueError("vision needs a photo file")
        reply = self.llm.complete(
            VISION_STATE_SYSTEM + ", ".join(instances),
            "What is the gadget showing?",
            stage="vision_state",
            images=[snap.path],
        )
        try:
            d = extract_json(reply)
        except DesignError:
            return {}
        return d if isinstance(d, dict) else {}


ClaudeVision = ModelVision  # backwards-compatible name


class ConsoleVision:
    """The human is the camera: Hands asks short questions and parses the answers."""

    interactive = True

    def __init__(self, ask: Callable[[str], str] = input):
        self.ask = ask

    def _yes(self, q: str) -> bool:
        for _ in range(3):
            a = self.ask(q + " [y/n] ").strip().lower()
            if a[:1] in ("y", "n"):
                return a.startswith("y")
        return False

    def observe_wiring(self, snap: Snapshot, layout: Layout) -> dict[str, Any]:
        """Walk the human through re-checking every part and wire; 'no' answers become discrepancies."""
        obs: dict[str, Any] = {"placements": {}, "wires": [], "uncertain": []}
        for inst, pl in layout.placements.items():
            if not pl.holes:
                continue
            legs = ", ".join(f"{p} in {h}" for p, h in pl.holes.items())
            if self._yes(f"Is {inst} in exactly these holes: {legs}?"):
                obs["placements"][inst] = dict(pl.holes)
        for w in layout.wires:
            if self._yes(f"Does wire {w.id} run from {w.a} to {w.b}, pushed fully in at both ends?"):
                obs["wires"].append([w.a, w.b])
        return obs

    def observe_state(self, snap: Snapshot, instances: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {"leds": {}, "oled": {}, "servo_deg": {}}
        for inst in instances:
            kind = inst.rstrip("0123456789")
            if kind in ("led", "light"):
                out["leds"][inst] = "on" if self._yes(f"Is {inst} lit right now?") else "off"
            elif kind == "oled":
                out["oled"][inst] = [self.ask(f"Type what the screen {inst} shows (one line): ").strip()]
            elif kind == "servo":
                a = self.ask(f"Which way does the {inst} arm point, in degrees (0 = far left, 180 = far right)? ")
                try:
                    out["servo_deg"][inst] = float(a)
                except ValueError:
                    out["servo_deg"][inst] = None
        return out

    def observe_led_pattern(self, inst: str) -> str:
        a = self.ask(f"Watch {inst} for 3 seconds. Is it on, off, or blinking? ").strip().lower()
        return a if a in ("on", "off", "blinking") else "off"
