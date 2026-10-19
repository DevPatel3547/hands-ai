"""Expose the bench as MCP tools so any MCP client (e.g. Claude) can drive it.

Uses the official ``mcp`` Python SDK (v2 ``MCPServer``; falls back to v1 ``FastMCP``).
"""

from __future__ import annotations

import json
from typing import Any

from ..circuit import Design, DesignError
from ..fwlint import lint_firmware
from ..inventory import Inventory, load_inventory
from ..rules import check_design, require_safe
from .base import BenchError

TOOL_NAMES = [
    "flash_firmware",
    "read_serial",
    "write_serial",
    "capture_logic",
    "read_current",
    "camera_snapshot",
    "power_cut",
    "check_design",
]


def _server_cls():
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2

        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # type: ignore[attr-defined,unused-ignore]  # mcp 1.x

        return FastMCP


def build_server(bench: Any, design: Design | None = None, inv: Inventory | None = None) -> Any:
    """MCP server over a bench.

    ``design`` is the rule-checked design that is on the breadboard. Firmware is only
    flashed if it passes the linter against that design's pin map; without a design,
    flashing is refused (an MCP client must not drive pins nobody checked).
    """
    inv = inv or load_inventory()
    if design is not None:
        require_safe(design, inv)
    Server = _server_cls()
    srv = Server(
        name="hands-bench",
        instructions=(
            "Electronics bench for a Raspberry Pi Pico W on a breadboard. Low-voltage USB power only. "
            "If read_current reports more than expected or the board stops responding, call power_cut first."
        ),
    )

    def guard(fn):
        try:
            return fn()
        except BenchError as e:
            return json.dumps({"error": str(e)})

    @srv.tool(
        description="Copy MicroPython code to the Pico as main.py and restart it. Returns boot output. "
        "The code must pass Hands' firmware linter for the loaded design."
    )
    def flash_firmware(code: str) -> str:
        if design is None:
            return json.dumps(
                {
                    "error": "no rule-checked design loaded (start the server with --design or "
                    "--gadget); refusing to flash"
                }
            )
        errs = lint_firmware(code, design.pin_map, design, inv)
        if errs:
            return json.dumps({"error": "firmware rejected by the linter", "problems": errs})
        return guard(lambda: json.dumps(bench.flash_firmware(code)))

    @srv.tool(
        name="check_design",
        description="Run Hands' deterministic electrical rule checker on a design JSON "
        "({name, parts, nets, pin_map}). Returns errors and warnings.",
    )
    def check_design_tool(design_json: str) -> str:
        try:
            d = Design.from_json(design_json)
        except DesignError as e:
            return json.dumps({"ok": False, "errors": [f"structure: {e}"]})
        rep = check_design(d, inv)
        return json.dumps(
            {
                "ok": rep.ok,
                "errors": [str(f) for f in rep.errors],
                "warnings": [str(f) for f in rep.warnings],
                "gpio_ma": rep.gpio_current_ma,
                "rail_ma": rep.rail_current_ma,
            }
        )

    @srv.tool(description="Read whatever the Pico prints on USB serial during the next `seconds`.")
    def read_serial(seconds: float = 1.0) -> str:
        return guard(lambda: bench.read_serial(seconds))

    @srv.tool(description="Send one line of text to the Pico over USB serial.")
    def write_serial(line: str) -> str:
        return guard(lambda: (bench.write_serial(line), "sent")[1])

    @srv.tool(
        description="Capture logic levels. `probes` maps analyzer channel (D0..D7) to the breadboard hole "
        "it is clipped to. Returns edge counts, duty, and PWM frequency per channel."
    )
    def capture_logic(probes: dict[str, str], seconds: float = 1.0) -> str:
        return guard(lambda: json.dumps(bench.capture_logic(probes, seconds).to_dict()))

    @srv.tool(description="Measure current on the 3V3 rail with the INA219 (mA), averaged/maxed over `seconds`.")
    def read_current(seconds: float = 0.0) -> str:
        return guard(lambda: json.dumps(bench.read_current(seconds).to_dict()))

    @srv.tool(
        description="Take a camera photo of the breadboard. Real bench: returns the image path. "
        "Simulated bench: returns the visible scene as JSON."
    )
    def camera_snapshot() -> str:
        def f():
            s = bench.camera_snapshot()
            return json.dumps({"t": s.t, "path": s.path, "scene": s.scene})

        return guard(f)

    @srv.tool(description="Emergency stop: tell the human to unplug USB (and switch the hub port off if configured).")
    def power_cut(reason: str) -> str:
        return bench.power_cut(reason)

    return srv


def sim_bench_for(gadget_id: str) -> Any:
    """A powered simulated bench with the gadget's final design built to plan."""
    from ..gadgets import registry
    from ..layout import place
    from ..sim.build import build_to_plan
    from .sim_bench import SimBench

    g = registry()[gadget_id]
    inv = load_inventory()
    layout = place(Design.from_dict(g.designs[-1]), inv)
    bench = SimBench(inv, build_to_plan(layout), g.env)
    bench.power_on()
    bench.design = layout.gadget
    return bench
