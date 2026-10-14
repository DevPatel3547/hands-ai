"""The three ready-made gadget specs, each with the scripted model replies used
by the offline demo (in real mode, Claude produces these instead)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from ..llm import ScriptedClient
from ..sim.board import SimEnv


@dataclass
class Gadget:
    id: str
    title: str
    spec: str
    protocol: str
    designs: list[dict]  # scripted design replies, in order (first may be flawed on purpose)
    firmwares: list[str]  # scripted firmware replies, in order
    test_plan: dict
    diagnoses: list[dict] = field(default_factory=list)  # scripted LLM-fallback diagnoses
    default_faults: list[str] = field(default_factory=list)
    env: SimEnv = field(default_factory=SimEnv)
    after_demo: Callable | None = None

    def scripted_client(self) -> ScriptedClient:
        script: dict[str, list[str | Callable[[str], str]]] = {
            "design": [json.dumps(self.designs[0])],
            "design_revise": [json.dumps(d) for d in self.designs[1:]] or [json.dumps(self.designs[0])],
            "firmware": [f"```python\n{fw}```" for fw in self.firmwares[:1]],
            "test_plan": [json.dumps(self.test_plan)],
            "diagnose": [json.dumps(d) for d in self.diagnoses]
            or [json.dumps({"hypothesis": "no scripted diagnosis available", "action": {"type": "none"}})],
        }
        return ScriptedClient(script)


def registry() -> dict[str, Gadget]:
    from . import servo_pointer, steelers, temp_display

    return {g.id: g for g in (steelers.GADGET, temp_display.GADGET, servo_pointer.GADGET)}
