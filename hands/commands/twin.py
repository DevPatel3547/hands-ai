"""``hands spice`` and ``hands emu``: the digital twin (SPICE operating points, RP2040 emulator).

Exit codes: 0 = pass, 1 = a hard limit is exceeded (or a firmware test failed), 2 = the optional
tool is missing (the message says how to install it).
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from ..circuit import Design, DesignError


def register(sub: Any) -> None:
    sp = sub.add_parser(
        "spice",
        help="check a design with ngspice at tolerance corners (independent of the rule checker)",
        description=(
            "Run SPICE (ngspice) DC operating points of a gadget design, with the bench fixture, for every "
            "GPIO drive state and button state. With --corners, also at resistor +/-5%%, LED Vf min/max, "
            "3V3 3.2-3.4 V, VBUS 4.75-5.25 V and an ideal/weak GPIO pad. Exit 1 if any hard limit is exceeded."
        ),
    )
    sp.add_argument("target", help="gadget id (steelers, tempdisplay, servo) or a design .json file")
    sp.add_argument("--corners", action="store_true", help="all tolerance corners (default: nominal only)")
    sp.add_argument(
        "--version",
        dest="design_version",
        type=int,
        default=0,
        help="for a gadget: which scripted design to check, 1-based (default: the final one)",
    )
    sp.add_argument("--json", action="store_true", help="print the report as JSON")
    sp.add_argument("--inventory", help="parts YAML (default: the starter kit)")
    sp.set_defaults(func=cmd_spice)


def _load_design(target: str, version: int) -> Design:
    from ..gadgets import registry

    g = registry().get(target)
    if g is not None:
        designs = g.designs
        if version < 0 or version > len(designs):
            raise SystemExit(f"{target} has {len(designs)} scripted design(s); --version 1..{len(designs)}")
        return Design.from_dict(copy.deepcopy(designs[version - 1 if version else -1]))
    p = Path(target)
    if not p.exists():
        raise SystemExit(f"{target!r} is neither a gadget id ({', '.join(sorted(registry()))}) nor a design file")
    try:
        return Design.from_json(p.read_text(encoding="utf-8"))
    except DesignError as e:
        raise SystemExit(f"{p}: {e}") from e


def cmd_spice(a: argparse.Namespace) -> int:
    from ..inventory import load_inventory
    from ..rules import check_design
    from ..sim.spice import SpiceUnavailable, analyze_design

    inv = load_inventory(a.inventory)
    design = _load_design(a.target, a.design_version)
    try:
        rep = analyze_design(design, inv, corners="all" if a.corners else "nom")
    except SpiceUnavailable as e:
        print(f"hands spice: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"hands spice: {e}", file=sys.stderr)
        return 1
    rules = check_design(design, inv)
    if a.json:
        out = rep.to_dict()
        out["rules_ok"] = rules.ok
        print(json.dumps(out, indent=2))
    else:
        print(rep.render())
        print(
            "Rule checker: "
            + ("PASS" if rules.ok else f"FAIL ({', '.join(sorted({f.rule for f in rules.errors}))})")
            + "  (the rules are the gate; SPICE is the independent check that they are strict enough)"
        )
    return 0 if rep.ok else 1
