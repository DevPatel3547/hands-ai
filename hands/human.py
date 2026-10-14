"""The human-in-the-loop interface: the agent's "hands".

Each instruction carries human-readable text plus an optional structured
``fix`` dict. A real person reads the text; the simulated person applies the
structured fix to the as-built board model.
"""

from __future__ import annotations

from typing import Protocol

from .wiring import Step

# rough per-action time estimates for a beginner (seconds); used for metrics in sim
EST_SECONDS = {
    "safety": 15,
    "place_pico": 60,
    "insert_part": 45,
    "jumper": 30,
    "fix": 60,
    "probe": 25,
    "press": 5,
    "power": 10,
    "photo": 20,
}


class Human(Protocol):
    est_seconds: float

    def perform_step(self, step: Step) -> None: ...
    def do(self, instruction: str, fix: dict | None = None) -> None: ...
    def clip_probe(self, channel: str, hole: str) -> None: ...
    def press(self, inst: str, hold_s: float) -> None: ...
    def plug_usb(self) -> None: ...
    def unplug_usb(self, reason: str) -> None: ...
    def say(self, msg: str) -> None: ...


class ConsoleHuman:
    """Real person at a terminal. Each instruction waits for Enter."""

    def __init__(self, bench, input_fn=input, print_fn=print):
        self.bench = bench
        self.input, self.print = input_fn, print_fn
        self.est_seconds = 0.0

    def _ask(self, text: str) -> str:
        self.print("\n>>> " + text)
        return self.input("    [Enter when done] ")

    def perform_step(self, step: Step) -> None:
        self._ask(f"Step {step.n}: {step.title}\n    {step.detail}")

    def do(self, instruction: str, fix: dict | None = None) -> None:
        self._ask(instruction)

    def clip_probe(self, channel: str, hole: str) -> None:
        self._ask(
            f"Clip logic-analyzer channel {channel} to a spare jumper pushed into hole {hole} "
            f"(analyzer GND clip on the GND rail)."
        )

    def press(self, inst: str, hold_s: float) -> None:
        self._ask(f"Press and hold button {inst} for about {hold_s:g} s, then release.")

    def plug_usb(self) -> None:
        self._ask("Plug the Pico's USB cable into the computer. Watch for smoke/heat; unplug if anything gets hot.")
        self.bench.power_on()

    def unplug_usb(self, reason: str) -> None:
        self.print("\n!!! " + self.bench.power_cut(reason))
        self.input("    [Enter once unplugged] ")

    def say(self, msg: str) -> None:
        self.print(msg)
