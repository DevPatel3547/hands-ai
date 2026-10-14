"""Command line entry point: ``hands <command>`` (or ``python -m hands <command>``)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .metrics import DEFAULT_LOG, study_report, summary_report

MODEL_HELP = (
    "scripted (default, offline, built-in gadgets only) | anthropic[:MODEL] (ANTHROPIC_API_KEY) | "
    "ollama[:MODEL] (free, local) | openai:MODEL (+ --base-url for any OpenAI-compatible server)"
)


def _faults(s: str | None) -> list[str] | None:
    if s is None:
        return None
    if s.strip().lower() in ("", "none"):
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def _camera(s: str | None) -> int | str:
    if s is None:
        return 0
    return int(s) if s.isdigit() else s


def _load_design(arg: str, first: bool) -> Any:
    from .circuit import Design
    from .gadgets import registry

    gs = registry()
    if arg in gs:
        return Design.from_dict(gs[arg].designs[0] if first else gs[arg].designs[-1])
    p = Path(arg)
    if not p.is_file():
        raise SystemExit(f"{arg!r} is neither a gadget id ({', '.join(sorted(gs))}) nor a design JSON file")
    return Design.from_dict(json.loads(p.read_text(encoding="utf-8")))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="hands", description="Hands: an AI hardware engineer that uses you as its hands.")
    ap.add_argument("--version", action="version", version=f"hands {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def model_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--model", default="scripted", help=MODEL_HELP)
        p.add_argument("--base-url", help="OpenAI-compatible base URL (default for ollama: http://localhost:11434/v1)")

    d = sub.add_parser("demo", help="run a gadget end-to-end on the simulated bench")
    d.add_argument("gadget", help="steelers | tempdisplay | servo | all")
    d.add_argument("--sim", action="store_true", help="simulated bench (the only option for demo; kept for clarity)")
    d.add_argument("--faults", help="comma list of human mistakes to inject, or 'none' (default: gadget's set)")
    d.add_argument("--vision-miss", help="comma list of fault kinds the camera fails to see")
    d.add_argument("--live-llm", action="store_true", help="shortcut for --model anthropic")
    d.add_argument("--metrics", default=str(DEFAULT_LOG), help="JSONL session log path")
    d.add_argument("--participant", default="", help="participant id for study logs (e.g. P01)")
    model_args(d)

    r = sub.add_parser("run", help="build a gadget: simulated (--sim) or on real hardware (--real)")
    r.add_argument("target", help="gadget id (steelers | tempdisplay | servo) or a quoted one-sentence spec")
    mode = r.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sim", action="store_true")
    mode.add_argument("--real", action="store_true")
    r.add_argument("--port", default="auto", help="(real) Pico serial port, or 'auto' to find it")
    r.add_argument("--camera", help="(real) webcam index/path, or 'prompt' to give phone photos (default 0)")
    r.add_argument(
        "--vision-model",
        help="model for photo checks, e.g. anthropic or ollama:qwen2.5vl:7b "
        "(default: you answer yes/no questions instead)",
    )
    r.add_argument("--uhubctl", help="(real) uhubctl args for software power cut, e.g. '-l 1-1 -p 2'")
    r.add_argument("--faults", help="(sim) human mistakes to inject")
    r.add_argument("--metrics", default=str(DEFAULT_LOG))
    r.add_argument("--participant", default="")
    model_args(r)

    c = sub.add_parser("check", help="run the deterministic rule checker on a design JSON or gadget")
    c.add_argument("design", help="path to design JSON, or a gadget id (uses its FIRST scripted design)")
    c.add_argument("--final", action="store_true", help="with a gadget id: check its final design instead")
    c.add_argument("--json", action="store_true")

    g = sub.add_parser("guide", help="print the wiring guide for a gadget or design JSON")
    g.add_argument("design")
    g.add_argument("--web", action="store_true", help="serve the step-by-step web UI")
    g.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to open it on a phone (adds an access token)")
    g.add_argument("--port", type=int, default=8765)
    g.add_argument("--no-diagrams", action="store_true")
    g.add_argument("--camera", help="webcam index/path for the 'Use the webcam' button")
    g.add_argument("--vision-model", help="model for photo checks (anthropic | ollama:MODEL | openai:MODEL)")
    g.add_argument("--base-url")

    rp = sub.add_parser("report", help="summarise the session log")
    rp.add_argument("--log", default=str(DEFAULT_LOG))
    rp.add_argument("--format", choices=["text", "md"], default="text")
    rp.add_argument("--out", help="write the report to this file")
    rp.add_argument("--title", default="Hands study report")

    m = sub.add_parser("mcp", help="serve the bench tools over MCP (stdio)")
    m.add_argument("--sim", action="store_true")
    m.add_argument("--gadget", default="steelers", help="(sim) which design is on the simulated breadboard")
    m.add_argument("--port", default="auto", help="(real) Pico serial port")
    m.add_argument("--design", help="(real) gadget id or design JSON that is on the breadboard (enables flashing)")

    b = sub.add_parser("bridge", help="live score -> serial bridge for the Steelers gadget")
    b.add_argument("--feed", choices=["mock", "espn"], default="mock")
    b.add_argument("--team", default="PIT")
    b.add_argument("--port", help="Pico serial port, or 'auto' (omit to just print commands)")
    b.add_argument("--interval", type=float, default=20.0)

    dr = sub.add_parser("doctor", help="check this computer for everything Hands needs; prints exact fixes")
    grp = dr.add_mutually_exclusive_group()
    grp.add_argument("--sim", action="store_true", help="only what the simulator needs")
    grp.add_argument("--real", action="store_true", help="treat real-bench items as required")
    dr.add_argument("--port", default="auto")
    dr.add_argument("--json", action="store_true")

    bu = sub.add_parser("bringup", help="guided first-time hardware check (LED, INA219, logic analyzer, camera)")
    bu.add_argument("--port", default="auto")
    bu.add_argument("--camera", help="webcam index/path, or 'prompt'")
    bu.add_argument("--skip", default="", help="comma list of steps to skip: led,ina,logic,camera")
    bu.add_argument("--metrics", default=str(DEFAULT_LOG))

    sub.add_parser("faults", help="list injectable simulated mistakes")
    sub.add_parser("inventory", help="show the parts inventory")
    sub.add_parser("rules", help="list the safety rules and the hard limits")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return _dispatch(a)
    except KeyboardInterrupt:
        print("\nStopped. If the Pico is plugged in and you were mid-change, unplug it now.")
        return 130


def _dispatch(a: argparse.Namespace) -> int:
    if a.cmd == "demo" or (a.cmd == "run" and a.sim):
        from .demo import run_sim_demo
        from .gadgets import registry

        target = a.gadget if a.cmd == "demo" else a.target
        ids = list(registry()) if target == "all" else [target]
        ok = True
        for gid in ids:
            res = run_sim_demo(
                gid,
                _faults(a.faults),
                _faults(getattr(a, "vision_miss", None)),
                a.metrics,
                getattr(a, "live_llm", False),
                model=a.model,
                base_url=a.base_url,
                participant=a.participant,
            )
            ok &= res.success
            print()
        return 0 if ok else 1

    if a.cmd == "run":
        from .demo import run_real

        res = run_real(
            a.target,
            port=a.port,
            camera=_camera(a.camera),
            model=a.model,
            vision_model=a.vision_model,
            base_url=a.base_url,
            metrics_path=a.metrics,
            participant=a.participant,
            uhubctl=a.uhubctl.split() if a.uhubctl else None,
        )
        return 0 if res.success else 1

    if a.cmd in ("check", "guide"):
        from .inventory import load_inventory
        from .rules import check_design

        inv = load_inventory()
        design = _load_design(a.design, first=a.cmd == "check" and not a.final)
        rep = check_design(design, inv)
        if a.cmd == "check":
            if a.json:
                print(
                    json.dumps(
                        {
                            "ok": rep.ok,
                            "findings": [f.__dict__ for f in rep.findings],
                            "gpio_ma": rep.gpio_current_ma,
                            "rail_ma": rep.rail_current_ma,
                        },
                        indent=2,
                    )
                )
            else:
                print(rep.render())
                print(f"Estimated GPIO loads (mA): {rep.gpio_current_ma}\nRail loads (mA): {rep.rail_current_ma}")
            return 0 if rep.ok else 1
        if not rep.ok:
            print(rep.render())
            print("Refusing to generate wiring for a design that fails the safety rules.")
            return 1
        from .layout import place
        from .wiring import build_guide

        layout = place(design, inv)
        guide = build_guide(layout, inv)
        if a.web:
            from .webui.server import serve

            photo = None
            if a.vision_model:
                from .bench.base import Snapshot
                from .llm import make_client
                from .vision import ModelVision
                from .wiring import compare

                vis = ModelVision(make_client(a.vision_model, base_url=a.base_url))
                cam = None
                if a.camera is not None:
                    from .bench.real_bench import RealBench

                    cam = RealBench(camera=_camera(a.camera))

                def photo(path: str | None) -> list[dict[str, Any]]:
                    if path is None:
                        if cam is None:
                            raise RuntimeError("no webcam configured; upload a photo instead")
                        snap = cam.camera_snapshot()
                    else:
                        snap = Snapshot(0.0, path)
                    return [d.__dict__ for d in compare(layout, vis.observe_wiring(snap, layout))]

            serve(
                guide,
                port=a.port,
                photo_check=photo,
                host=a.host,
                has_camera=a.camera is not None and photo is not None,
            )
            return 0
        print(guide.text(diagrams=not a.no_diagrams))
        return 0

    if a.cmd == "report":
        text = study_report(a.log, a.title) if a.format == "md" else summary_report(a.log)
        if a.out:
            Path(a.out).write_text(text + "\n", encoding="utf-8")
            print(f"wrote {a.out}")
        else:
            print(text)
        return 0

    if a.cmd == "mcp":
        from .bench.mcp_server import build_server, sim_bench_for

        if a.sim:
            bench = sim_bench_for(a.gadget)
            design = bench.design
        else:
            from .bench.real_bench import RealBench

            bench = RealBench(port=a.port)
            design = _load_design(a.design, first=False) if a.design else None
        build_server(bench, design).run("stdio")
        return 0

    if a.cmd == "bridge":
        from .scores import EspnFeed, demo_game, run_bridge

        feed = demo_game(a.team) if a.feed == "mock" else EspnFeed(a.team)
        if a.port:
            import serial

            from .bench.real_bench import resolve_port

            ser = serial.Serial(resolve_port(a.port), 115200, timeout=1)

            def send(cmd: str) -> None:
                ser.write((cmd + "\n").encode())
        else:

            def send(cmd: str) -> None:
                print(f"[would send] {cmd}")

        interval = 0 if a.feed == "mock" else max(a.interval, 15.0)
        run_bridge(feed, send, polls=None, interval_s=interval)
        return 0

    if a.cmd == "doctor":
        from .doctor import as_json, exit_code, render, run_checks

        checks = run_checks(mode="sim" if a.sim else "real" if a.real else "all", port=a.port)
        print(json.dumps(as_json(checks), indent=2) if a.json else render(checks))
        return exit_code(checks)

    if a.cmd == "bringup":
        from .bench.real_bench import RealBench
        from .bringup import run_bringup
        from .metrics import Metrics

        bench = RealBench(port=a.port, camera=_camera(a.camera))
        steps = run_bringup(
            bench,
            metrics=Metrics(a.metrics, gadget="bringup", mode="real+bringup"),
            skip={s.strip() for s in a.skip.split(",") if s.strip()},
        )
        return 0 if all(r.status != "FAIL" for r in steps) else 1

    if a.cmd == "faults":
        from .sim.build import FAULT_KINDS, INVISIBLE

        for k, v in FAULT_KINDS.items():
            print(f"{k:18} {v}{'  (invisible to camera)' if k in INVISIBLE else ''}")
        return 0

    if a.cmd == "inventory":
        from .inventory import load_inventory

        print(load_inventory().summary_for_prompt())
        return 0

    if a.cmd == "rules":
        from .inventory import load_inventory
        from .rules import RULE_IDS
        from .safety import DESIGN_KINDS

        print("Rules (all deterministic code, see docs/safety.md):\n  " + "\n  ".join(RULE_IDS))
        print("\nEffective limits:")
        for lname, lval in load_inventory().limits.items():
            print(f"  {lname:20} {lval:g}")
        print(f"\nPart kinds a design may use: {', '.join(sorted(DESIGN_KINDS))}")
        return 0
    return 1  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
