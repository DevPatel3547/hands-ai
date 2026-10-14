"""MicroPython files Hands installs on the Pico next to the generated program."""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent
BENCH_HELPER = HERE / "hands_bench.py"
MAIN_SHIM = HERE / "main_shim.py"
