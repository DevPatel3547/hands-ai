"""Runs MicroPython-style firmware (CPython syntax subset) against SimBoard.

The firmware the agent writes for the real Pico is executed here unchanged,
with stand-ins for ``machine``, ``time``, ``dht``, ``ssd1306``, ``select`` and
``sys``. Time is *virtual*: ``time.sleep(10)`` costs no wall-clock time; the
bench advances the clock explicitly.

Isolation note: this uses restricted builtins and an import allow-list, which
stops accidental damage from model-written code but is NOT a security sandbox.
"""

from __future__ import annotations

import builtins
import json
import math
import random
import struct
import threading
import traceback
import types

from .board import PinState, SimBoard

ALLOWED_BUILTINS = [
    "abs",
    "all",
    "any",
    "bool",
    "bytearray",
    "bytes",
    "chr",
    "dict",
    "divmod",
    "enumerate",
    "Exception",
    "filter",
    "float",
    "format",
    "hex",
    "int",
    "isinstance",
    "KeyError",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "object",
    "OSError",
    "ord",
    "pow",
    "range",
    "repr",
    "reversed",
    "round",
    "RuntimeError",
    "set",
    "slice",
    "sorted",
    "str",
    "sum",
    "tuple",
    "TypeError",
    "ValueError",
    "zip",
    "IndexError",
    "ImportError",
    "KeyboardInterrupt",
    "StopIteration",
    "ZeroDivisionError",
    "AttributeError",
    "NotImplementedError",
    "super",
    "property",
    "staticmethod",
    "classmethod",
    "getattr",
    "hasattr",
    "setattr",
    "iter",
    "callable",
    "BaseException",
    "NameError",
    "id",
    "hash",
    "memoryview",
    "bin",
    "oct",
    "type",
]


class SimHalt(BaseException):
    """Raised inside firmware to stop it (reset / power off)."""


class SerialPort:
    def __init__(self):
        self.out: list[str] = []
        self.inbox: list[str] = []
        self._read_pos = 0

    def write(self, s: str):
        self.out.append(s)

    def read_new(self) -> str:
        text = "".join(self.out)
        new = text[self._read_pos :]
        self._read_pos = len(text)
        return new

    def all_text(self) -> str:
        return "".join(self.out)


class VClock:
    def __init__(self, board: SimBoard):
        self.board = board
        self.cv = threading.Condition()
        self.until = board.now
        self.waiting = False
        self.finished = False
        self.halt = False

    def sleep(self, dt: float):
        with self.cv:
            if self.halt:
                raise SimHalt()
            target = self.board.now + max(dt, 0.0)
            while target > self.until + 1e-9 and not self.halt:
                self.board.now = self.until
                self.waiting = True
                self.cv.notify_all()
                self.cv.wait()
                self.waiting = False
            if self.halt:
                raise SimHalt()
            self.board.now = target

    def tick(self):
        self.sleep(1e-5)


class FirmwareRunner:
    def __init__(self, board: SimBoard, code: str, serial: SerialPort, filename: str = "main.py"):
        self.board = board
        self.code = code
        self.serial = serial
        self.filename = filename
        self.clock = VClock(board)
        self.error: str | None = None
        self.hung = False
        self.yield_timeout = 5.0  # real seconds firmware may run without touching a shim
        self.thread: threading.Thread | None = None

    # --- lifecycle -------------------------------------------------------
    def start(self):
        g = make_globals(self)
        try:
            compiled = compile(self.code, self.filename, "exec")
        except SyntaxError as e:
            self.error = f"SyntaxError: {e.msg} (line {e.lineno})"
            self.serial.write(
                f'Traceback (most recent call last):\n  File "{self.filename}", line {e.lineno}\nSyntaxError: {e.msg}\n'
            )
            self.clock.finished = True
            return

        def run():
            try:
                exec(compiled, g)
            except SimHalt:
                pass
            except BaseException as e:  # noqa: BLE001 - firmware crash -> REPL-style traceback
                tb = traceback.extract_tb(e.__traceback__)
                lines = [
                    f'  File "{f.filename}", line {f.lineno}, in {f.name}' for f in tb if f.filename == self.filename
                ]
                msg = f"{type(e).__name__}: {e}"
                self.error = msg
                self.serial.write("Traceback (most recent call last):\n" + "\n".join(lines) + "\n" + msg + "\n")
            finally:
                with self.clock.cv:
                    self.clock.finished = True
                    self.clock.cv.notify_all()

        self.thread = threading.Thread(target=run, daemon=True, name="sim-firmware")
        self.thread.start()
        self._wait_idle()

    def _wait_idle(self):
        with self.clock.cv:
            ok = self.clock.cv.wait_for(lambda: self.clock.waiting or self.clock.finished, timeout=self.yield_timeout)
            if not ok:
                self.hung = True
                self.error = "firmware did not yield (busy loop without sleep?)"
                self.clock.halt = True

    def advance(self, dt: float):
        """Let virtual time run forward by ``dt`` seconds."""
        if self.clock.finished or self.hung:
            self.board.now += dt
            return
        with self.clock.cv:
            self.clock.until = self.board.now + dt
            self.clock.cv.notify_all()
        with self.clock.cv:
            ok = self.clock.cv.wait_for(
                lambda: (self.clock.waiting and self.board.now >= self.clock.until - 1e-9) or self.clock.finished,
                timeout=self.yield_timeout * 2,
            )
            if not ok:
                self.hung = True
                self.error = "firmware did not yield (busy loop without sleep?)"
                self.clock.halt = True
        if self.clock.finished:
            self.board.now = max(self.board.now, self.clock.until)

    def stop(self):
        with self.clock.cv:
            self.clock.halt = True
            self.clock.cv.notify_all()
        if self.thread:
            self.thread.join(timeout=2.0)


# ---------------------------------------------------------------- shims ---


def make_globals(runner: FirmwareRunner) -> dict:
    board, clock, serial = runner.board, runner.clock, runner.serial
    mods = {
        "machine": _machine(board, clock),
        "time": _time(board, clock),
        "utime": None,
        "dht": _dht(board, clock),
        "ssd1306": _ssd1306(board, clock),
        "select": _select(serial, clock),
        "uselect": None,
        "sys": _sys_mod(serial, clock),
        "micropython": types.SimpleNamespace(const=lambda x: x, mem_info=lambda: None),
        "gc": types.SimpleNamespace(collect=lambda: None, mem_free=lambda: 150_000),
        "math": math,
        "struct": struct,
        "ustruct": struct,
        "json": json,
        "ujson": json,
        "random": random.Random(board.env.seed),
        "urandom": None,
    }
    mods["utime"] = mods["time"]
    mods["uselect"] = mods["select"]
    mods["urandom"] = mods["random"]

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".")[0]
        if root not in mods:
            raise ImportError(f"no module named '{name}'")
        return mods[root]

    def _print(*args, sep=" ", end="\n", **kw):
        serial.write(sep.join(str(a) for a in args) + end)

    b = {n: getattr(builtins, n) for n in ALLOWED_BUILTINS if hasattr(builtins, n)}
    b["__import__"] = _import
    b["print"] = _print
    b["__build_class__"] = builtins.__build_class__
    b["__name__"] = "__main__"
    return {"__builtins__": b, "__name__": "__main__"}


def _changed(board: SimBoard):
    board.record()
    board.update_servos()
    board.check_power()
    if board.browned_out:
        raise SimHalt()


def _machine(board: SimBoard, clock: VClock):
    m = types.ModuleType("machine")

    class Pin:
        IN, OUT, OPEN_DRAIN = 0, 1, 2
        PULL_UP, PULL_DOWN = 1, 2
        IRQ_FALLING, IRQ_RISING = 4, 8

        def __init__(self, id, mode=-1, pull=-1, value=None):
            if id == "LED" or id == "WL_GPIO0":
                self.n = "LED"
            else:
                if not isinstance(id, int) or not (0 <= id <= 29) or id in board.inv.pico_internal_gpio:
                    raise ValueError("invalid pin")
                self.n = id
            self.init(mode, pull, value)

        def init(self, mode=-1, pull=-1, value=None):
            if self.n == "LED":
                if value is not None:
                    board.onboard_led = int(bool(value))
                return
            s = board.pin(self.n)
            if mode == Pin.OUT:
                s.mode = "out"
            elif mode == Pin.IN:
                s.mode = "in"
            if pull == Pin.PULL_UP:
                s.pull = "up"
            elif pull == Pin.PULL_DOWN:
                s.pull = "down"
            elif pull is None:
                s.pull = ""
            if value is not None:
                s.value = int(bool(value))
            _changed(board)

        def value(self, v=None):
            clock.tick()
            if self.n == "LED":
                if v is None:
                    return board.onboard_led
                board.onboard_led = int(bool(v))
                return None
            s = board.pin(self.n)
            if v is None:
                if s.mode == "out":
                    return s.value
                return board.gpio_read(self.n)
            s.value = int(bool(v))
            _changed(board)
            return None

        __call__ = value

        def on(self):
            self.value(1)

        def off(self):
            self.value(0)

        high, low = on, off

        def toggle(self):
            self.value(0 if self.value() else 1)

        def irq(self, *a, **k):
            raise NotImplementedError("Hands sim: poll pins in the main loop instead of using irq()")

    class PWM:
        def __init__(self, pin, freq=None, duty_u16=None):
            self.n = pin.n
            s = board.pin(self.n)
            s.mode = "pwm"
            if freq:
                s.pwm_freq = float(freq)
            if duty_u16 is not None:
                s.pwm_duty = int(duty_u16)
            _changed(board)

        def freq(self, f=None):
            s = board.pin(self.n)
            if f is None:
                return int(s.pwm_freq)
            s.pwm_freq = float(f)
            _changed(board)

        def duty_u16(self, d=None):
            s = board.pin(self.n)
            if d is None:
                return s.pwm_duty
            s.pwm_duty = max(0, min(65535, int(d)))
            clock.tick()
            _changed(board)

        def duty_ns(self, ns=None):
            s = board.pin(self.n)
            if ns is None:
                return int(s.pwm_duty / 65535 * 1e9 / max(s.pwm_freq, 1))
            self.duty_u16(int(ns * max(s.pwm_freq, 1) / 1e9 * 65535))

        def deinit(self):
            board.pins[self.n] = PinState()
            _changed(board)

    class I2C:
        def __init__(self, id, scl=None, sda=None, freq=400000):
            ctrl = board.inv.pico_i2c.get(f"I2C{id}")
            if ctrl is None:
                raise ValueError("I2C(%s) doesn't exist" % id)
            if scl is None or sda is None:
                raise ValueError("Hands sim: pass scl=Pin(n), sda=Pin(n) explicitly")
            if scl.n not in ctrl["scl"]:
                raise ValueError("bad SCL pin")
            if sda.n not in ctrl["sda"]:
                raise ValueError("bad SDA pin")
            self.sda, self.scl = sda.n, scl.n
            for n in (self.sda, self.scl):
                board.pins[n] = PinState(mode="i2c")
            _changed(board)

        def _dev(self, addr):
            clock.sleep(0.0005)
            devs = board.i2c_devices(self.sda, self.scl)
            if addr not in devs:
                raise OSError(5, "EIO")  # as MicroPython rp2 reports a NACK
            return devs[addr]

        def scan(self):
            clock.sleep(0.01)
            return sorted(board.i2c_devices(self.sda, self.scl))

        def writeto(self, addr, buf, stop=True):
            self._dev(addr)
            return len(buf)

        def writeto_mem(self, addr, reg, buf):
            self._dev(addr)

        def readfrom_mem(self, addr, reg, n):
            inst = self._dev(addr)
            kind = board.inv.parts[board.build.placements[inst]["type"]].kind
            if kind == "ina219":
                if reg == 0x01:
                    v = round(board.ina_current_ma(inst) * 0.1 / 0.01)  # shunt mV / 10uV
                    return struct.pack(">h", max(-32768, min(32767, v)))
                if reg == 0x02:
                    return struct.pack(">H", (int(board.ina_bus_v(inst) / 0.004) << 3) | 0x2)
            return bytes(n)

        def readfrom(self, addr, n):
            self._dev(addr)
            return bytes(n)

    class ADC:
        def __init__(self, pin):
            self.n = pin.n if hasattr(pin, "n") else pin

        def read_u16(self):
            clock.tick()
            v = board.solve().volt(board.node_of_gpio(self.n))
            return int(max(0, min(3.3, v)) / 3.3 * 65535)

    m.Pin, m.PWM, m.I2C, m.SoftI2C, m.ADC = Pin, PWM, I2C, I2C, ADC
    m.freq = lambda *a: 125_000_000
    m.unique_id = lambda: b"\xe6\x61\x41\x04\x03\x2a\x5b\x2c"

    def reset():
        raise SimHalt()

    m.reset = m.soft_reset = reset
    m.idle = lambda: clock.tick()
    return m


def _time(board: SimBoard, clock: VClock):
    t = types.ModuleType("time")
    t.sleep = lambda s: clock.sleep(float(s))
    t.sleep_ms = lambda ms: clock.sleep(ms / 1000)
    t.sleep_us = lambda us: clock.sleep(us / 1e6)

    def ticks_ms():
        clock.tick()
        return int(board.now * 1000)

    t.ticks_ms = ticks_ms
    t.ticks_us = lambda: int(board.now * 1e6)
    t.ticks_diff = lambda a, b: a - b
    t.ticks_add = lambda a, b: a + b
    t.time = lambda: int(board.now)
    return t


def _dht(board: SimBoard, clock: VClock):
    d = types.ModuleType("dht")

    class DHT22:
        def __init__(self, pin):
            self.n = pin.n
            self._t = self._h = None

        def measure(self):
            clock.sleep(0.005)
            r = board.dht_read(self.n)
            if r is None:
                raise OSError(110, "ETIMEDOUT")  # sensor didn't answer
            self._t, self._h = r

        def temperature(self):
            return self._t

        def humidity(self):
            return self._h

    d.DHT22 = DHT22
    d.DHT11 = DHT22
    return d


def _ssd1306(board: SimBoard, clock: VClock):
    s = types.ModuleType("ssd1306")

    class SSD1306_I2C:
        def __init__(self, width, height, i2c, addr=0x3C, external_vcc=False):
            self.w, self.h, self.i2c, self.addr = width, height, i2c, addr
            self.buf: dict[tuple[int, int], str] = {}
            self.i2c.writeto(addr, b"\x00\xae")  # display off cmd: raises OSError if absent

        def fill(self, c):
            self.buf.clear()

        def text(self, txt, x, y, c=1):
            self.buf[(y, x)] = str(txt)

        def show(self):
            self.i2c.writeto(self.addr, b"\x40")
            from .board import OledState

            lines = [self.buf[k] for k in sorted(self.buf)]
            board.oled[self.addr] = OledState(lines)

        def poweroff(self):
            board.oled.pop(self.addr, None)

        def contrast(self, c):
            pass

        def pixel(self, *a):
            pass

        def rect(self, *a):
            pass

        def fill_rect(self, *a):
            pass

        def hline(self, *a):
            pass

    s.SSD1306_I2C = SSD1306_I2C
    return s


class _Stdin:
    def __init__(self, serial: SerialPort, clock: VClock):
        self.serial, self.clock = serial, clock

    def readline(self):
        while not self.serial.inbox:
            self.clock.sleep(0.01)
        return self.serial.inbox.pop(0)

    def read(self, n=1):
        line = self.readline()
        return line[:n]


def _select(serial: SerialPort, clock: VClock):
    sel = types.ModuleType("select")
    sel.POLLIN = 1

    class Poll:
        def __init__(self):
            self.objs = []

        def register(self, obj, mask=1):
            self.objs.append(obj)

        def poll(self, timeout=-1):
            if serial.inbox:
                return [(self.objs[0] if self.objs else None, 1)]
            if timeout and timeout > 0:
                clock.sleep(timeout / 1000)
            else:
                clock.tick()
            return [(self.objs[0] if self.objs else None, 1)] if serial.inbox else []

    sel.poll = Poll
    return sel


def _sys_mod(serial: SerialPort, clock: VClock):
    s = types.ModuleType("sys")
    s.stdin = _Stdin(serial, clock)
    s.stdout = types.SimpleNamespace(write=serial.write)
    s.implementation = types.SimpleNamespace(name="micropython")
    s.platform = "rp2"
    s.print_exception = lambda e: serial.write(f"{type(e).__name__}: {e}\n")
    s.exit = lambda *a: (_ for _ in ()).throw(SimHalt())
    return s
