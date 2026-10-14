"""Deterministic MicroPython firmware linter, run before anything is flashed.

The model writes the firmware, but code decides whether it may touch the board:

* only allow-listed imports; no ``eval``/``exec``/``open``/dunder tricks;
* every ``Pin(...)`` must name a literal (or module-constant) GPIO from the
  design's rule-checked ``pin_map`` -- no computed pin numbers, so the linter
  can always tell which pins are driven;
* pins wired to a button are never outputs (pressing the button would short
  the pin to GND);
* I2C/SPI/UART must name their pins explicitly (the defaults may be pins the
  design never checked);
* no raw register access (``machine.mem32``), deep sleep, overclocking or
  bootloader jumps.

The simulator additionally runs firmware with restricted builtins, but that is
not a sandbox; this linter is the gate.
"""

from __future__ import annotations

import ast

from .circuit import Design, PinRef, gpio_number
from .inventory import Inventory

ALLOWED_IMPORTS = frozenset(
    {
        "machine",
        "time",
        "utime",
        "dht",
        "ssd1306",
        "select",
        "uselect",
        "sys",
        "math",
        "struct",
        "json",
        "ujson",
        "micropython",
        "gc",
    }
)
FORBIDDEN_CALLS = frozenset(
    {"eval", "exec", "compile", "__import__", "open", "globals", "locals", "vars", "breakpoint", "input", "delattr"}
)
FORBIDDEN_MACHINE = frozenset({"mem8", "mem16", "mem32", "bootloader", "deepsleep", "lightsleep"})
BUS_PINS = {
    "I2C": ("scl", "sda"),
    "SoftI2C": ("scl", "sda"),
    "SPI": ("sck", "mosi"),
    "SoftSPI": ("sck", "mosi"),
    "UART": ("tx", "rx"),
}
MAX_FIRMWARE_BYTES = 32_000
ONBOARD_LED = "LED"


def button_gpios(design: Design, inv: Inventory) -> set[int]:
    """GPIO numbers wired directly to a push button."""
    out: set[int] = set()
    for inst, t in design.parts.items():
        if t not in inv.parts or inv.parts[t].kind != "button":
            continue
        for _net, pins in design.nets.items():
            if any(PinRef.parse(p).inst == inst for p in pins if "." in p):
                out |= {gpio_number(p[5:]) for p in pins if p.startswith("pico.GP")}
    return out


def _const_ints(tree: ast.Module) -> dict[str, int]:
    """Module-level names assigned exactly once to an int literal (or const(int))."""
    seen: dict[str, list[ast.expr]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            seen.setdefault(node.targets[0].id, []).append(node.value)
    # any other (re)binding anywhere disqualifies a name
    rebound: set[str] = set()
    for anode in ast.walk(tree):
        if (isinstance(anode, (ast.AugAssign, ast.AnnAssign)) and isinstance(anode.target, ast.Name)) or (
            isinstance(anode, (ast.For, ast.comprehension)) and isinstance(anode.target, ast.Name)
        ):
            rebound.add(anode.target.id)
        elif isinstance(anode, ast.Global):
            rebound |= set(anode.names)
    out: dict[str, int] = {}
    v: ast.expr
    for name, vals in seen.items():
        if len(vals) != 1 or name in rebound:
            continue
        v = vals[0]
        if isinstance(v, ast.Call) and getattr(v.func, "id", getattr(v.func, "attr", "")) == "const" and v.args:
            v = v.args[0]
        if isinstance(v, ast.Constant) and isinstance(v.value, int) and not isinstance(v.value, bool):
            out[name] = v.value
    return out


def _resolve_pin(arg: ast.expr, consts: dict[str, int]) -> int | str | None:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, (int, str)) and not isinstance(arg.value, bool):
        return arg.value
    if isinstance(arg, ast.Name) and arg.id in consts:
        return consts[arg.id]
    return None


def _func_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _is_output_mode(call: ast.Call) -> bool:
    mode: ast.expr | None = call.args[1] if len(call.args) > 1 else None
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    return isinstance(mode, ast.Attribute) and mode.attr == "OUT"


def lint_firmware(
    code: str, pin_map: dict[str, str], design: Design | None = None, inv: Inventory | None = None
) -> list[str]:
    """Return a list of problems; an empty list means the firmware may be flashed."""
    errs: list[str] = []
    if len(code.encode()) > MAX_FIRMWARE_BYTES:
        return [f"firmware is larger than {MAX_FIRMWARE_BYTES} bytes"]
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error line {e.lineno}: {e.msg}"]
    allowed_pins: set[int] = set()
    for v in pin_map.values():
        try:
            allowed_pins.add(gpio_number(v))
        except ValueError:
            errs.append(f"pin_map value {v!r} is not a GPIO label")
    buttons = button_gpios(design, inv) if design is not None and inv is not None else set()
    consts = _const_ints(tree)
    pin_aliases = {"Pin"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "machine":
            for a in node.names:
                if a.name == "Pin" and a.asname:
                    pin_aliases.add(a.asname)

    has_sleep = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for n in names:
                if n.split(".")[0] not in ALLOWED_IMPORTS:
                    errs.append(f"import of {n!r} is not allowed")
            if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                errs.append("'from ... import *' is not allowed")
            if isinstance(node, ast.ImportFrom) and (node.module or "") == "machine":
                bad = [a.name for a in node.names if a.name in FORBIDDEN_MACHINE]
                if bad:
                    errs.append(f"machine.{bad[0]} is not allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                errs.append(f"line {node.lineno}: access to {node.attr} is not allowed")
            if node.attr in FORBIDDEN_MACHINE:
                errs.append(
                    f"line {node.lineno}: machine.{node.attr} is not allowed "
                    f"(raw registers / sleep modes / bootloader bypass the safety checks)"
                )
            if node.attr == "irq":
                errs.append(f"line {node.lineno}: Pin.irq() is not allowed; poll the pin in the main loop")
        elif isinstance(node, ast.Name):
            if node.id.startswith("__") and node.id != "__name__":
                errs.append(f"line {node.lineno}: name {node.id} is not allowed")
        elif isinstance(node, ast.Call):
            name = _func_name(node)
            if isinstance(node.func, ast.Name) and name in FORBIDDEN_CALLS:
                errs.append(f"line {node.lineno}: {name}() is not allowed")
            if isinstance(node.func, ast.Name) and name in ("getattr", "setattr", "hasattr"):
                if (
                    len(node.args) < 2
                    or not isinstance(node.args[1], ast.Constant)
                    or str(node.args[1].value).startswith("_")
                ):
                    errs.append(f"line {node.lineno}: {name}() needs a literal, non-underscore attribute name")
            if name in ("sleep", "sleep_ms", "sleep_us"):
                has_sleep = True
            if (
                name == "freq"
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "machine"
                and node.args
            ):
                errs.append(f"line {node.lineno}: changing the CPU clock is not allowed")
            if name in pin_aliases or (isinstance(node.func, ast.Attribute) and node.func.attr == "Pin"):
                if not node.args:
                    errs.append(f"line {node.lineno}: Pin() needs a pin number")
                    continue
                pin = _resolve_pin(node.args[0], consts)
                if pin is None:
                    errs.append(
                        f"line {node.lineno}: Pin() argument must be a literal GPIO number or a "
                        f"module-level constant (so the pin can be checked)"
                    )
                elif isinstance(pin, str):
                    if pin != ONBOARD_LED:
                        errs.append(f"line {node.lineno}: Pin({pin!r}) is not allowed (only 'LED')")
                elif pin not in allowed_pins:
                    errs.append(f"Pin({pin}) is not in the design's pin map {sorted(allowed_pins)}")
                elif pin in buttons and _is_output_mode(node):
                    errs.append(
                        f"line {node.lineno}: GP{pin} is wired to a button and must not be an output "
                        f"(pressing the button would short it); use Pin.IN with Pin.PULL_UP"
                    )
            if name in BUS_PINS:
                kws = {k.arg for k in node.keywords}
                need = BUS_PINS[name]
                if not set(need) <= kws:
                    errs.append(f"line {node.lineno}: {name}() must pass {'= and '.join(need)}= pins explicitly")
            if name == "PWM" and node.args and isinstance(node.args[0], ast.Call):
                inner = node.args[0]
                p = _resolve_pin(inner.args[0], consts) if inner.args else None
                if isinstance(p, int) and p in buttons:
                    errs.append(f"line {node.lineno}: GP{p} is wired to a button and must not drive PWM")
    has_ready = any(
        isinstance(n, ast.Constant) and isinstance(n.value, str) and "READY" in n.value for n in ast.walk(tree)
    )
    if not has_ready:
        errs.append('firmware never prints "READY"')
    if not has_sleep:
        errs.append("main loop must sleep")
    # de-duplicate, keep order
    return list(dict.fromkeys(errs))
