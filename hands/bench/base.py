"""Bench tool interface shared by the real and simulated backends."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol


class BenchError(RuntimeError):
    pass


@dataclass
class ChannelTrace:
    hole: str
    edges: int
    high_fraction: float
    first_level: int
    pwm_hz: float | None = None
    pwm_duty: float | None = None


@dataclass
class LogicCapture:
    seconds: float
    channels: dict[str, ChannelTrace]

    def to_dict(self):
        return {"seconds": self.seconds, "channels": {k: asdict(v) for k, v in self.channels.items()}}


@dataclass
class CurrentReading:
    avg_ma: float
    max_ma: float
    bus_v: float

    def to_dict(self):
        return asdict(self)


@dataclass
class Snapshot:
    t: float
    path: str | None = None  # real camera: image file
    scene: dict | None = None  # simulator: ground-truth scene (incl. what's visible of the wiring)
    meta: dict = field(default_factory=dict)


class Bench(Protocol):
    mode: str

    def power_on(self) -> str: ...
    def power_cut(self, reason: str) -> str: ...
    def flash_firmware(self, code: str, extra_files: dict[str, str] | None = None) -> dict: ...
    def read_serial(self, seconds: float = 1.0) -> str: ...
    def write_serial(self, line: str) -> None: ...
    def capture_logic(self, probes: dict[str, str], seconds: float = 1.0) -> LogicCapture: ...
    def read_current(self, seconds: float = 0.0) -> CurrentReading: ...
    def camera_snapshot(self) -> Snapshot: ...
    def wait(self, seconds: float) -> None: ...
