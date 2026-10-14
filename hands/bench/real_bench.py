"""Real bench: drives actual instruments through their standard CLIs.

Not yet run against physical hardware in this repo (see README "Limitations"). Every
command is built by a small pure function and every external call goes through an
injectable runner, so the logic is unit-tested and each command can be checked by hand.

Host requirements (``hands doctor`` checks all of them and prints the exact fix):

* ``mpremote``  (pip install mpremote)         -- copy files to / run code on the Pico
* ``pyserial``  (pip install pyserial)         -- USB serial + Pico port discovery
* ``sigrok-cli`` with the ``fx2lafw`` driver     -- the 8-channel logic analyzer (optional)
* a camera: OpenCV (pip install opencv-python-headless), ``fswebcam`` (Linux), ``imagesnap``
  (macOS), or a phone photo (``camera="prompt"`` or the web UI upload)
* optional ``uhubctl`` + a compatible USB hub    -- lets power_cut() switch the port off itself
"""

from __future__ import annotations

import csv
import io
import json
import platform
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..circuit import INA_I2C
from ..firmware import BENCH_HELPER, MAIN_SHIM
from .base import BenchError, ChannelTrace, CurrentReading, LogicCapture, Snapshot

Runner = Callable[[list[str], float], str]

RPI_VID = 0x2E8A
PICO_SERIAL_PIDS = {0x0005: "MicroPython", 0x000A: "Pico SDK serial", 0x0009: "CircuitPython"}
BOOTSEL_LABELS = ("RPI-RP2", "RP2350")
TRANSIENT = re.compile(
    r"could not enter raw repl|failed to access|could not open|resource busy|errno 16|timed out|"
    r"could not connect|no device found|serial exception|input/output error|errno 5",
    re.I,
)
INA_LINE = re.compile(r"#INA (-?[\d.]+) (-?[\d.]+) ([\d.]+) (\d+)")
INA_ANY = re.compile(r"#INA[^\n]*\n?")
CHANNEL = re.compile(r"^D[0-7]$")

INSTALL_HINTS: dict[str, str | dict[str, str]] = {
    "mpremote": "pip install mpremote",
    "sigrok-cli": {
        "Linux": "sudo apt install sigrok-cli   (Debian/Ubuntu; also installs the fx2lafw firmware)",
        "Darwin": "brew install sigrok-cli",
        "Windows": "install sigrok-cli from https://sigrok.org/wiki/Downloads and add it to PATH",
    },
    "fswebcam": "sudo apt install fswebcam",
    "imagesnap": "brew install imagesnap",
    "uhubctl": {"Linux": "sudo apt install uhubctl", "Darwin": "brew install uhubctl", "Windows": "not supported"},
}


def install_hint(tool: str) -> str:
    h = INSTALL_HINTS.get(tool, f"install {tool}")
    if isinstance(h, dict):
        return h.get(platform.system(), next(iter(h.values())))
    return str(h)


def require_tool(tool: str, which: Callable[[str], str | None] = shutil.which) -> str:
    path = which(tool)
    if not path:
        raise BenchError(f"{tool} is not installed. Fix: {install_hint(tool)}")
    return path


def _default_runner(cmd: list[str], timeout: float) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as e:
        raise BenchError(f"{cmd[0]} is not installed. Fix: {install_hint(cmd[0])}") from e
    except subprocess.TimeoutExpired as e:
        raise BenchError(f"{cmd[0]} timed out after {timeout:g} s") from e
    if p.returncode != 0:
        raise BenchError(f"{' '.join(cmd[:3])}... failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout


def retrying(
    runner: Runner, retries: int = 3, backoff_s: float = 1.0, sleep: Callable[[float], None] = time.sleep
) -> Runner:
    """Wrap a runner: transient USB/serial errors (port busy, raw REPL hiccup) are retried with backoff."""

    def run(cmd: list[str], timeout: float) -> str:
        last: BenchError | None = None
        for attempt in range(retries):
            try:
                return runner(cmd, timeout)
            except BenchError as e:
                last = e
                if not TRANSIENT.search(str(e)) or attempt + 1 == retries:
                    raise
                sleep(backoff_s * (2**attempt))
        raise last or BenchError("retry loop exited")  # pragma: no cover

    return run


# ------------------------------------------------------------- port discovery


@dataclass(frozen=True)
class PortInfo:
    device: str
    vid: int | None
    pid: int | None
    description: str = ""


def _list_ports() -> list[PortInfo]:
    try:
        from serial.tools import list_ports
    except ImportError as e:
        raise BenchError("pyserial is not installed. Fix: pip install pyserial") from e
    return [PortInfo(p.device, p.vid, p.pid, p.description or "") for p in list_ports.comports()]


def find_pico_ports(lister: Callable[[], list[PortInfo]] | None = None) -> list[PortInfo]:
    """Serial ports that belong to a Raspberry Pi RP2040/RP2350 board (USB VID 0x2E8A)."""
    return [p for p in (lister or _list_ports)() if p.vid == RPI_VID]


def find_bootsel_drives(roots: list[Path] | None = None) -> list[Path]:
    """Mounted BOOTSEL drives (board waiting for a UF2 file, i.e. MicroPython not installed)."""
    if roots is None:
        roots = [Path("/Volumes"), Path("/media"), Path("/run/media")]
        if platform.system() == "Windows":  # pragma: no cover - platform specific
            roots = [Path(f"{d}:/") for d in "DEFGHIJKLMNOPQRSTUVWXYZ"]
    found: list[Path] = []
    for r in roots:
        try:
            if (r / "INFO_UF2.TXT").exists():
                found.append(r)
                continue
            for d in list(r.iterdir()) if r.is_dir() else []:
                cands = [d] + (list(d.iterdir()) if d.is_dir() and d.name not in BOOTSEL_LABELS else [])
                found += [c for c in cands if c.name in BOOTSEL_LABELS and (c / "INFO_UF2.TXT").exists()]
        except OSError:
            continue
    return found


def resolve_port(
    port: str | None, lister: Callable[[], list[PortInfo]] | None = None, bootsel_roots: list[Path] | None = None
) -> str:
    """``auto``/None -> the single connected Pico's serial port, or an actionable error."""
    if port and port != "auto":
        return port
    picos = find_pico_ports(lister)
    if len(picos) == 1:
        return picos[0].device
    if len(picos) > 1:
        listing = ", ".join(f"{p.device} ({p.description})" for p in picos)
        raise BenchError(f"several Picos are connected ({listing}); pass --port with the one to use")
    if find_bootsel_drives(bootsel_roots):
        raise BenchError(
            "the Pico is in BOOTSEL mode (a drive called RPI-RP2/RP2350 is mounted) and has no "
            "MicroPython yet. Fix: download the Pico W UF2 from "
            "https://micropython.org/download/RPI_PICO_W/ and drag it onto that drive."
        )
    hints = {
        "Linux": "check `ls /dev/ttyACM*`; if it exists but can't be opened, run "
        "`sudo usermod -aG dialout $USER` and log out/in",
        "Darwin": "check `ls /dev/cu.usbmodem*`",
        "Windows": "check Device Manager > Ports (COM & LPT) for 'USB Serial Device'",
    }
    raise BenchError(
        "no Pico found on USB. Plug it in with a DATA micro-USB cable (charge-only cables don't "
        f"work); {hints.get(platform.system(), hints['Linux'])}. If it has never been set up, hold "
        "BOOTSEL while plugging in and install MicroPython."
    )


# --------------------------------------------------------- command builders


def mpremote_flash_cmd(port: str, local_main: str) -> list[str]:
    """Plain copy of a single main.py (used when the bench helper is disabled)."""
    return ["mpremote", "connect", port, "fs", "cp", local_main, ":main.py", "+", "reset"]


def mpremote_install_cmd(port: str, app: str, helper: str, shim: str) -> list[str]:
    """Copy helper + program + boot shim in one mpremote session, then reset."""
    return [
        "mpremote",
        "connect",
        port,
        "fs",
        "cp",
        helper,
        ":hands_bench.py",
        "+",
        "fs",
        "cp",
        app,
        ":app.py",
        "+",
        "fs",
        "cp",
        shim,
        ":main.py",
        "+",
        "reset",
    ]


def mpremote_mip_cmd(port: str, package: str) -> list[str]:
    return ["mpremote", "connect", port, "mip", "install", package]


def mpremote_exec_cmd(port: str, code: str) -> list[str]:
    return ["mpremote", "connect", port, "exec", code]


INA_SNIPPET = (
    "from machine import I2C,Pin\n"
    "import struct,json\n"
    "i=I2C({bus},sda=Pin({sda}),scl=Pin({scl}))\n"
    "v=struct.unpack('>h',i.readfrom_mem({addr},1,2))[0]*0.01\n"  # shunt mV (10 uV LSB)
    "b=(struct.unpack('>H',i.readfrom_mem({addr},2,2))[0]>>3)*0.004\n"  # bus V (4 mV LSB, bits 15..3)
    "print(json.dumps({{'shunt_mv':v,'bus_v':b}}))\n"
)

BLINK_SNIPPET = (
    "from machine import Pin\nimport time\np=Pin('LED',Pin.OUT)\n"
    "for i in range({n}):\n p.toggle()\n time.sleep(0.25)\np.off()\nprint('BLINK OK')\n"
)

INFO_SNIPPET = "import sys,os\nprint(sys.implementation.name, os.uname().release, os.uname().machine)\n"


def mpremote_ina_cmd(port: str) -> list[str]:
    return mpremote_exec_cmd(port, INA_SNIPPET.format(**INA_I2C))


def sigrok_cmd(channels: list[str], seconds: float, samplerate: str = "1m") -> list[str]:
    return [
        "sigrok-cli",
        "-d",
        "fx2lafw",
        "--config",
        f"samplerate={samplerate}",
        "--channels",
        ",".join(channels),
        "--time",
        f"{int(seconds * 1000)}ms",
        "-O",
        "csv",
    ]


def sigrok_scan_cmd() -> list[str]:
    return ["sigrok-cli", "--driver", "fx2lafw", "--scan"]


def parse_json_line(out: str) -> dict[str, Any]:
    """Last line of command output that parses as a JSON object (mpremote may print noise first)."""
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                return d
    raise BenchError(f"no JSON result in device output: {out.strip()[-200:]!r}")


def parse_sigrok_csv(text: str, channels: list[str], samplerate_hz: float = 1e6) -> dict[str, ChannelTrace]:
    rows = [r for r in csv.reader(io.StringIO(text)) if r and not r[0].startswith(";")]
    if rows and not rows[0][0].strip().isdigit():
        header = [h.strip() for h in rows[0]]
        rows = rows[1:]
    else:
        header = channels
    out = {}
    for ch in channels:
        if ch not in header:
            continue
        col = header.index(ch)
        vals = [int(r[col]) for r in rows if len(r) > col and r[col].strip() in ("0", "1")]
        edges = sum(1 for a, b in zip(vals, vals[1:], strict=False) if a != b)
        hi = sum(vals) / len(vals) if vals else 0.0
        pwm = None
        if edges > 20:
            rising = [i for i, (a, b) in enumerate(zip(vals, vals[1:], strict=False)) if a == 0 and b == 1]
            if len(rising) > 2:
                period = (rising[-1] - rising[0]) / (len(rising) - 1) / samplerate_hz
                pwm = round(1 / period, 1) if period else None
        out[ch] = ChannelTrace(ch, edges, round(hi, 3), vals[0] if vals else 0, pwm, round(hi, 4) if pwm else None)
    return out


def split_ina_lines(text: str) -> tuple[str, list[tuple[float, float, float, int]], list[str]]:
    """Separate the helper's ``#INA`` reports from the program's own output."""
    samples = [(float(a), float(b), float(c), int(d)) for a, b, c, d in INA_LINE.findall(text)]
    status = [m.group(0).strip() for m in INA_ANY.finditer(text) if not INA_LINE.match(m.group(0))]
    return INA_ANY.sub("", text), samples, status


class RealBench:
    mode = "real"

    def __init__(
        self,
        port: str = "auto",
        camera: int | str = 0,
        runner: Runner | None = None,
        shunt_ohms: float = 0.1,
        uhubctl: list[str] | None = None,
        workdir: str | None = None,
        use_helper: bool = True,
        serial_factory: Callable[[str], Any] | None = None,
        prompt: Callable[[str], str] = input,
        which: Callable[[str], str | None] = shutil.which,
        lister: Callable[[], list[PortInfo]] | None = None,
    ):
        self._port_arg = port
        self._port: str | None = None if port in (None, "auto") else port
        self.camera = camera
        self.run = runner or retrying(_default_runner)
        self.shunt = shunt_ohms
        self.uhubctl = uhubctl  # e.g. ["-l", "1-1", "-p", "2"]
        self.workdir = Path(workdir or tempfile.mkdtemp(prefix="hands-"))
        self.use_helper = use_helper
        self.serial_factory = serial_factory
        self.prompt = prompt
        self.which = which
        self.lister = lister
        self._serial: Any = None
        self._ina: list[tuple[float, float, float, int]] = []
        self._ina_status: list[str] = []

    @property
    def port(self) -> str:
        if self._port is None:
            self._port = resolve_port(self._port_arg, self.lister)
        return self._port

    # ------------------------------------------------------------- power
    def power_on(self) -> str:
        if self.uhubctl:
            self.run(["uhubctl", *self.uhubctl, "-a", "on"], 10)
        return "Plug in the USB cable."

    def power_cut(self, reason: str) -> str:
        msg = f"UNPLUG THE USB CABLE NOW. Reason: {reason}"
        if self.uhubctl:
            try:
                self.run(["uhubctl", *self.uhubctl, "-a", "off"], 10)
                msg += " (USB port power already switched off via uhubctl)"
            except BenchError as e:
                msg += f" (uhubctl failed: {e})"
        self._close_serial()
        return msg

    # ---------------------------------------------------------- firmware
    def device_info(self) -> str:
        self._close_serial()
        return self.run(mpremote_exec_cmd(self.port, INFO_SNIPPET), 20).strip()

    def blink_onboard_led(self, n: int = 8) -> str:
        self._close_serial()
        return self.run(mpremote_exec_cmd(self.port, BLINK_SNIPPET.format(n=n)), 30).strip()

    def flash_firmware(self, code: str, extra_files: dict[str, str] | None = None) -> dict[str, Any]:
        self._close_serial()
        if "ssd1306" in code:
            try:
                self.run(mpremote_mip_cmd(self.port, "ssd1306"), 90)
            except BenchError as e:
                raise BenchError(f"could not install the ssd1306 driver (needs internet on this computer): {e}") from e
        app = self.workdir / "app.py"
        app.write_text(code, encoding="utf-8")
        if self.use_helper:
            self.run(mpremote_install_cmd(self.port, str(app), str(BENCH_HELPER), str(MAIN_SHIM)), 60)
        else:
            self.run(mpremote_flash_cmd(self.port, str(app)), 60)
        time.sleep(1.5)
        self._ina.clear()
        return {"ok": True, "bytes": len(code), "boot_output": self.read_serial(1.0)}

    # ------------------------------------------------------------ serial
    def _ser(self) -> Any:
        if self._serial is None:
            factory = self.serial_factory
            if factory is None:
                try:
                    import serial
                except ImportError as e:
                    raise BenchError("pyserial is not installed. Fix: pip install pyserial") from e

                def factory(p: str) -> Any:
                    return serial.Serial(p, 115200, timeout=0.1)

            try:
                self._serial = factory(self.port)
            except BenchError:
                raise
            except Exception as e:
                raise BenchError(
                    f"cannot open {self.port}: {e}. Close other programs using it (Thonny, "
                    f"a serial monitor) and try again."
                ) from e
        return self._serial

    def _close_serial(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def read_serial(self, seconds: float = 1.0) -> str:
        s, end, buf = self._ser(), time.monotonic() + seconds, b""
        try:
            while True:
                buf += s.read(4096)
                if time.monotonic() >= end:
                    break
        except Exception as e:
            self._close_serial()
            raise BenchError(f"board not responding on USB ({e}): possible short circuit / brownout") from e
        text = buf.decode(errors="replace").replace("\r\n", "\n")
        text, samples, status = split_ina_lines(text)
        self._ina += samples
        self._ina_status += status
        return text

    def write_serial(self, line: str) -> None:
        try:
            self._ser().write((line.rstrip("\n") + "\r\n").encode())
        except BenchError:
            raise
        except Exception as e:
            self._close_serial()
            raise BenchError(f"board not responding on USB ({e})") from e

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)

    # ----------------------------------------------------- logic analyzer
    def capture_logic(self, probes: dict[str, str], seconds: float = 1.0) -> LogicCapture:
        chans = list(probes)
        bad = [c for c in chans if not CHANNEL.match(c)]
        if bad or not chans:
            raise BenchError(f"logic analyzer channels must be D0..D7 (got {chans})")
        require_tool("sigrok-cli", self.which)
        try:
            out = self.run(sigrok_cmd(chans, seconds), seconds + 20)
        except BenchError as e:
            if "no device" in str(e).lower() or "fx2lafw" in str(e):
                raise BenchError(
                    f"logic analyzer not found: plug it in; on Linux install the udev rules "
                    f"(`sudo apt install sigrok-firmware-fx2lafw`). ({e})"
                ) from e
            raise
        traces = parse_sigrok_csv(out, chans)
        for ch, t in traces.items():
            t.hole = probes[ch]
        return LogicCapture(seconds, traces)

    # ------------------------------------------------------------ INA219
    def read_current(self, seconds: float = 0.0) -> CurrentReading:
        """Current on the measured 3V3 rail.

        Preferred: the on-board helper's ``#INA`` reports (program keeps running). Fallback
        when the helper is absent: an ``mpremote exec`` read, which interrupts the program and
        restarts it afterwards.
        """
        if self.use_helper:
            self._ina.clear()
            status_before = len(self._ina_status)
            self.read_serial(max(seconds, 0.6))
            if self._ina:
                n = sum(s[3] for s in self._ina) or 1
                avg = sum(s[0] * s[3] for s in self._ina) / n
                return CurrentReading(round(avg, 2), round(max(s[1] for s in self._ina), 2), round(self._ina[-1][2], 3))
            new_status = self._ina_status[status_before:]
            if any("MISSING" in s or "ERR" in s for s in new_status):
                raise BenchError("INA219 not found at 0x40 on GP26/GP27: check its wiring")
        return self._read_current_exec(seconds)

    def _read_current_exec(self, seconds: float) -> CurrentReading:
        self._close_serial()
        vals, bus = [], 0.0
        end = time.monotonic() + seconds
        while True:
            d = parse_json_line(self.run(mpremote_ina_cmd(self.port), 20))
            vals.append(float(d["shunt_mv"]) / self.shunt)  # mV / ohm = mA
            bus = float(d["bus_v"])
            if time.monotonic() >= end:
                break
        self.run(["mpremote", "connect", self.port, "reset"], 20)
        return CurrentReading(round(sum(vals) / len(vals), 2), round(max(vals), 2), round(bus, 3))

    # ------------------------------------------------------------ camera
    def camera_snapshot(self) -> Snapshot:
        path = self.workdir / f"snap_{int(time.time() * 1000)}.jpg"
        if self.camera == "prompt":
            return Snapshot(time.time(), str(self._prompt_photo()), None, {"source": "phone"})
        try:
            import cv2
        except ImportError:
            cv2 = None
        if cv2 is not None:
            cap = cv2.VideoCapture(self.camera)
            try:
                if not cap.isOpened():
                    raise BenchError(
                        f"camera {self.camera!r} could not be opened (in use, or no permission; on "
                        f"macOS allow camera access for your terminal). Or use --camera prompt to "
                        f"give a phone photo instead."
                    )
                ok, frame = False, None
                for _ in range(8):  # let auto-exposure settle
                    ok, frame = cap.read()
                if not ok or frame is None:
                    raise BenchError("camera returned no frame")
                cv2.imwrite(str(path), frame)
            finally:
                cap.release()
        elif self.which("fswebcam"):
            self.run(["fswebcam", "-r", "1280x720", "--no-banner", str(path)], 20)
        elif self.which("imagesnap"):
            self.run(["imagesnap", "-w", "1", str(path)], 20)
        else:
            raise BenchError(
                "no camera backend: pip install opencv-python-headless, install fswebcam/imagesnap, "
                "or use --camera prompt to give a phone photo"
            )
        return Snapshot(time.time(), str(path), None, {"source": "webcam"})

    def _prompt_photo(self) -> Path:
        for _ in range(3):
            raw = (
                self.prompt(
                    "Take a photo of the breadboard from directly above, copy it to this computer, "
                    "then drag the file here (or type its path) and press Enter: "
                )
                .strip()
                .strip("'\"")
            )
            p = Path(raw).expanduser()
            if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                return p
            print(f"   not a JPEG/PNG/WebP file: {raw!r}")
        raise BenchError("no photo given")
