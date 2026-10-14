"""Simulated bench: every tool answers from SimBoard + the firmware runtime."""

from __future__ import annotations

from ..circuit import Design
from ..inventory import Inventory
from ..sim.board import LOGIC_TH, SimBoard, SimEnv
from ..sim.build import Build
from ..sim.runtime import FirmwareRunner, SerialPort
from .base import BenchError, ChannelTrace, CurrentReading, LogicCapture, Snapshot

INA_SDA, INA_SCL = 26, 27


class SimBench:
    mode = "sim"

    def __init__(self, inv: Inventory, build: Build, env: SimEnv | None = None, vision_miss: set[str] | None = None):
        self.board = SimBoard(inv, build, env)
        self.serial = SerialPort()
        self.runner: FirmwareRunner | None = None
        self.vision_miss = set(vision_miss or ())
        self.flashed: list[str] = []
        self.calls: list[str] = []
        self.design: Design | None = None  # set by the MCP helper: the design built on this bench

    # ------------------------------------------------------------- power
    def power_on(self) -> str:
        self.calls.append("power_on")
        b = self.board
        b.powered, b.browned_out = True, False
        b.reset_pins()
        b.check_power()
        if self.flashed and not b.browned_out:
            self._boot(self.flashed[-1])
        return "USB connected (sim)."

    def power_cut(self, reason: str) -> str:
        self.calls.append("power_cut")
        self._halt()
        self.board.powered = False
        self.board.record()
        return f"UNPLUG THE USB CABLE NOW. Reason: {reason}"

    def _halt(self):
        if self.runner:
            self.runner.stop()
            self.runner = None

    def _require_alive(self):
        if not self.board.powered:
            raise BenchError("no USB connection (board unpowered)")
        if self.board.browned_out:
            raise BenchError("board not responding on USB: possible short circuit / brownout")

    # ------------------------------------------------------------ firmware
    def _boot(self, code: str):
        self._halt()
        self.board.reset_pins()
        self.runner = FirmwareRunner(self.board, code, self.serial)
        self.runner.start()
        self.runner.advance(0.2)

    def flash_firmware(self, code: str, extra_files: dict[str, str] | None = None) -> dict:
        self.calls.append("flash_firmware")
        self._require_alive()
        self.flashed.append(code)
        self.serial.read_new()
        self._boot(code)
        return {"ok": True, "bytes": len(code), "boot_output": self.serial.read_new()}

    def wait(self, seconds: float) -> None:
        if self.runner and self.board.powered:
            self.runner.advance(seconds)
        else:
            self.board.now += seconds

    # -------------------------------------------------------------- serial
    def read_serial(self, seconds: float = 1.0) -> str:
        self.calls.append("read_serial")
        self._require_alive()
        self.wait(seconds)
        return self.serial.read_new()

    def write_serial(self, line: str) -> None:
        self.calls.append("write_serial")
        self._require_alive()
        self.serial.inbox.append(line.rstrip("\n") + "\n")

    # ------------------------------------------------------- logic analyzer
    def capture_logic(self, probes: dict[str, str], seconds: float = 1.0) -> LogicCapture:
        self.calls.append("capture_logic")
        self._require_alive()
        b = self.board
        start = b.now
        b.record()
        i0 = len(b.log) - 1
        self.wait(seconds)
        b.record()
        window = b.log[i0:]
        out = {}
        for ch, hole in probes.items():
            node = b.node_of_hole(hole)
            # PWM is reported as a measured frequency/duty (as sigrok's pwm decoder would)
            pwm = None
            for n, s in b.pins.items():
                if s.mode == "pwm" and b.node_of_gpio(n) == node and s.pwm_freq:
                    pwm = (s.pwm_freq, s.pwm_duty / 65535)
            levels: list[int] = []
            times: list[float] = []
            for t, key in window:
                lv = int(b.hole_voltage(hole, key) > LOGIC_TH)
                if not levels or levels[-1] != lv:
                    levels.append(lv)
                    times.append(t)
            # time-weighted high fraction
            hi = 0.0
            for i, (t, lv) in enumerate(zip(times, levels, strict=False)):
                t_end = times[i + 1] if i + 1 < len(times) else b.now
                hi += (t_end - t) * lv
            dur = max(b.now - start, 1e-9)
            out[ch] = ChannelTrace(
                hole,
                max(len(levels) - 1, 0),
                round(hi / dur, 3),
                levels[0] if levels else 0,
                round(pwm[0], 1) if pwm else None,
                round(pwm[1], 4) if pwm else None,
            )
        return LogicCapture(seconds, out)

    # --------------------------------------------------------------- INA219
    def read_current(self, seconds: float = 0.0) -> CurrentReading:
        self.calls.append("read_current")
        self._require_alive()
        b = self.board
        if 0x40 not in b.i2c_devices(INA_SDA, INA_SCL):
            raise BenchError("INA219 not found at 0x40 on I2C1 (GP26/GP27): check its wiring")
        b.record()
        i0 = len(b.log) - 1
        if seconds:
            self.wait(seconds)
            b.record()
        vals = [b.ina_current_ma(key=k) for _, k in b.log[i0:]]
        self._require_alive()
        return CurrentReading(round(sum(vals) / len(vals), 2), round(max(vals), 2), round(b.ina_bus_v(), 3))

    # --------------------------------------------------------------- camera
    def camera_snapshot(self) -> Snapshot:
        self.calls.append("camera_snapshot")
        scene = self.board.scene()
        scene["wiring"] = self.board.build.observe(self.vision_miss)
        return Snapshot(self.board.now, None, scene)
