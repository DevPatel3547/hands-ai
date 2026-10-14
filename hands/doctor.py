"""``hands doctor``: check the environment and say exactly what is missing and how to fix it.

Every probe goes through :class:`Env`, so the whole report is unit-testable without
hardware. Nothing here changes the board: the only device action is a read-only
``mpremote exec`` (print the MicroPython version, scan the INA219's I2C bus).
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import shutil
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .bench.base import BenchError
from .bench.real_bench import (
    INFO_SNIPPET,
    PortInfo,
    _default_runner,
    install_hint,
    mpremote_exec_cmd,
    resolve_port,
    sigrok_scan_cmd,
)
from .llm import DEFAULT_OLLAMA_URL

OK, MISSING, WARN, SKIP = "OK", "MISSING", "WARN", "SKIP"
I2C_SCAN_SNIPPET = "from machine import Pin,SoftI2C\nprint('I2C', SoftI2C(sda=Pin(26),scl=Pin(27)).scan())\n"


def parse_scan(text: str) -> list[int]:
    m = re.search(r"I2C \[([^\]]*)\]", text)
    if not m:
        return []
    return [int(x) for x in re.findall(r"\d+", m.group(1))]


@dataclass
class Check:
    group: str
    name: str
    status: str
    detail: str = ""
    fix: str = ""
    required: bool = False


@dataclass
class Env:
    which: Callable[[str], str | None] = shutil.which
    has_module: Callable[[str], bool] = lambda m: importlib.util.find_spec(m) is not None
    run: Callable[[list[str], float], str] = _default_runner
    lister: Callable[[], list[PortInfo]] | None = None
    http_get: Callable[[str, float], bytes] = lambda url, t: urllib.request.urlopen(url, timeout=t).read()  # noqa: S310
    environ: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    python: tuple[int, int] = (sys.version_info.major, sys.version_info.minor)


def run_checks(env: Env | None = None, *, mode: str = "all", port: str = "auto") -> list[Check]:
    """mode: ``sim`` (only what the simulator needs), ``real`` or ``all``."""
    env = env or Env()
    out: list[Check] = []
    add = out.append

    py_ok = env.python >= (3, 11)
    add(
        Check(
            "core",
            "Python >= 3.11",
            OK if py_ok else MISSING,
            ".".join(map(str, env.python)),
            "install Python 3.11+ from https://www.python.org/downloads/",
            required=True,
        )
    )
    add(Check("core", "PyYAML", OK if env.has_module("yaml") else MISSING, "", "pip install pyyaml", required=True))
    add(
        Check(
            "optional",
            "mcp SDK (bench tools over MCP)",
            OK if env.has_module("mcp") else SKIP,
            "",
            "pip install 'hands[mcp]'",
        )
    )

    # ---- models (all optional: the scripted model needs nothing)
    if env.has_module("anthropic") and env.environ.get("ANTHROPIC_API_KEY"):
        add(Check("model", "Anthropic (Claude)", OK, "SDK installed, ANTHROPIC_API_KEY set"))
    else:
        missing = [
            x
            for x, ok in (
                ("pip install 'hands[llm]'", env.has_module("anthropic")),
                ("set ANTHROPIC_API_KEY", bool(env.environ.get("ANTHROPIC_API_KEY"))),
            )
            if not ok
        ]
        add(Check("model", "Anthropic (Claude)", SKIP, "not configured (optional)", "; ".join(missing)))
    base = (
        env.environ.get("OLLAMA_HOST", "").rstrip("/") + "/v1" if env.environ.get("OLLAMA_HOST") else DEFAULT_OLLAMA_URL
    )
    try:
        models = json.loads(env.http_get(base + "/models", 2.0)).get("data", [])
        names = ", ".join(m.get("id", "?") for m in models[:6]) or "no models pulled yet"
        add(
            Check(
                "model",
                "Ollama (free local models)",
                OK if models else WARN,
                names,
                "" if models else "ollama pull qwen2.5-coder:7b   (and a vision model: ollama pull qwen2.5vl:7b)",
            )
        )
    except Exception:  # noqa: BLE001 - any failure means "not running"
        add(
            Check(
                "model",
                "Ollama (free local models)",
                SKIP,
                f"not reachable at {base} (optional)",
                "install from https://ollama.com, then `ollama serve` and `ollama pull qwen2.5-coder:7b`",
            )
        )
    if mode == "sim":
        return out

    # ---- real bench
    req = mode == "real"
    add(
        Check(
            "bench", "pyserial", OK if env.has_module("serial") else MISSING, "", "pip install pyserial", required=req
        )
    )
    mp = env.which("mpremote")
    add(Check("bench", "mpremote", OK if mp else MISSING, mp or "", install_hint("mpremote"), required=req))
    pico = None
    if env.has_module("serial") or env.lister is not None:
        try:
            pico = resolve_port(port, env.lister)
            add(Check("bench", "Pico W on USB", OK, pico, required=req))
        except BenchError as e:
            add(Check("bench", "Pico W on USB", MISSING, "", str(e), required=req))
    else:
        add(Check("bench", "Pico W on USB", SKIP, "needs pyserial to look", "pip install pyserial", required=req))
    if pico and mp:
        try:
            info = env.run(mpremote_exec_cmd(pico, INFO_SNIPPET), 20).strip().splitlines()[-1]
            good = info.startswith("micropython")
            add(
                Check(
                    "bench",
                    "MicroPython on the Pico",
                    OK if good else MISSING,
                    info,
                    "" if good else "install MicroPython: https://micropython.org/download/RPI_PICO_W/",
                    required=req,
                )
            )
            scan = env.run(mpremote_exec_cmd(pico, I2C_SCAN_SNIPPET), 20)
            found = 0x40 in parse_scan(scan)
            add(
                Check(
                    "bench",
                    "INA219 at 0x40 on GP26/GP27",
                    OK if found else WARN,
                    scan.strip()[-60:],
                    "" if found else "wire the bench fixture: `hands bringup` walks you through it",
                )
            )
        except (BenchError, IndexError) as e:
            add(
                Check(
                    "bench",
                    "MicroPython on the Pico",
                    MISSING,
                    str(e)[:120],
                    "close other serial programs (Thonny), replug the Pico, or reinstall MicroPython",
                    required=req,
                )
            )
    sig = env.which("sigrok-cli")
    add(
        Check(
            "instruments",
            "sigrok-cli (logic analyzer)",
            OK if sig else WARN,
            sig or "",
            "" if sig else install_hint("sigrok-cli") + "  (optional: only needed for signal-walk debugging)",
        )
    )
    if sig:
        try:
            scan = env.run(sigrok_scan_cmd(), 15)
            seen = "fx2lafw" in scan
            add(
                Check(
                    "instruments",
                    "logic analyzer plugged in",
                    OK if seen else WARN,
                    scan.strip()[-80:],
                    "" if seen else "plug in the 24 MHz analyzer; on Linux: sudo apt install sigrok-firmware-fx2lafw",
                )
            )
        except BenchError as e:
            add(Check("instruments", "logic analyzer plugged in", WARN, str(e)[:120], "plug in the analyzer"))
    cam: str = ("OpenCV" if env.has_module("cv2") else "") or env.which("fswebcam") or env.which("imagesnap") or ""
    add(
        Check(
            "instruments",
            "camera backend",
            OK if cam else WARN,
            cam,
            ""
            if cam
            else "pip install opencv-python-headless, or use --camera prompt (phone photo) / the web UI's photo upload",
        )
    )
    add(
        Check(
            "optional",
            "uhubctl (software power cut)",
            OK if env.which("uhubctl") else SKIP,
            "",
            install_hint("uhubctl"),
        )
    )
    if platform.system() == "Linux" and env.environ.get("USER") not in (None, "root") and req and not pico:
        add(Check("bench", "serial port permission", WARN, "", "sudo usermod -aG dialout $USER, then log out/in"))
    return out


def render(checks: list[Check]) -> str:
    lines, group = [], ""
    order = list(dict.fromkeys(c.group for c in checks))
    for c in sorted(checks, key=lambda c: order.index(c.group)):
        if c.group != group:
            group = c.group
            lines.append(f"\n[{group}]")
        mark = {"OK": "ok  ", "MISSING": "MISS", "WARN": "warn", "SKIP": "skip"}[c.status]
        lines.append(f"  {mark}  {c.name}" + (f"  ({c.detail})" if c.detail else ""))
        if c.status != OK and c.fix:
            lines.append(f"        fix: {c.fix}")
    bad = [c for c in checks if c.required and c.status == MISSING]
    lines.append("")
    lines.append(
        "All required checks passed."
        if not bad
        else f"{len(bad)} required item(s) missing: " + ", ".join(c.name for c in bad)
    )
    return "\n".join(lines).lstrip("\n")


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.required and c.status == MISSING for c in checks) else 0


def as_json(checks: list[Check]) -> list[dict[str, Any]]:
    return [c.__dict__ for c in checks]
