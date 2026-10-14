"""Wire everything together for a run: simulated bench (default) or the real bench."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .agent import Engineer, RunResult
from .bench.sim_bench import SimBench
from .gadgets import registry
from .human import ConsoleHuman
from .inventory import load_inventory
from .llm import LLMClient, make_client, parse_model_spec
from .metrics import DEFAULT_LOG, Metrics
from .sim.build import Build
from .sim.human import SimHuman
from .vision import ConsoleVision, ModelVision, SimVision, Vision


def _client(model: str, gadget_id: str | None, base_url: str | None) -> LLMClient:
    g = registry().get(gadget_id or "")
    return make_client(model, scripted=g.scripted_client() if g else None, base_url=base_url)


def run_sim_demo(
    gadget_id: str,
    faults: list[str] | None = None,
    vision_miss: list[str] | None = None,
    metrics_path: str | Path | None = DEFAULT_LOG,
    live_llm: bool = False,
    log: Callable[[str], None] = print,
    inventory_path: str | None = None,
    model: str = "scripted",
    base_url: str | None = None,
    participant: str = "",
) -> RunResult:
    gadgets = registry()
    if gadget_id not in gadgets:
        raise SystemExit(f"unknown gadget {gadget_id!r}; choose from {sorted(gadgets)}")
    g = gadgets[gadget_id]
    inv = load_inventory(inventory_path)
    faults = g.default_faults if faults is None else faults
    if live_llm:
        model = "anthropic"
    llm = _client(model, gadget_id, base_url)
    bench = SimBench(inv, Build(), g.env, vision_miss=set(vision_miss or ()))
    human = SimHuman(bench, faults, log=log)
    metrics = Metrics(metrics_path, gadget=g.id, mode=f"sim+{parse_model_spec(model)[0]}", participant=participant)
    log(f"#### Hands demo: {g.title}  [simulated bench; model: {model}]")
    log(
        f"#### Injected human mistakes: {', '.join(faults) or 'none'}"
        + (f"; camera will miss: {', '.join(vision_miss)}" if vision_miss else "")
    )
    eng = Engineer(inv, llm, bench, human, SimVision(), metrics, log=log, protocol=g.protocol)
    res = eng.run(g.spec)
    if res.success and g.after_demo:
        g.after_demo(bench, log, res.layout)
    m = res.metrics
    log("\n#### Metrics")
    log(f"   result: {'WORKING DEVICE' if res.success else 'FAILED: ' + res.reason}")
    log(
        f"   design errors caught by rules: {m['design_errors_caught']}   "
        f"errors caught pre-power: {m['errors_caught_pre_power']}   post-power: {m['errors_caught_post_power']}"
    )
    log(
        f"   debug rounds: {m['debug_rounds']}   firmware versions: {m['firmware_versions']}   "
        f"LLM calls: {m['llm_calls']}   wiring steps: {m['wiring_steps']}"
    )
    if m["time_to_working_s"] is not None:
        log(
            f"   time to working device (sim estimate): {m['time_to_working_s'] / 60:.1f} min "
            f"(human actions {m['est_human_s'] / 60:.1f} min + bench {m['sim_bench_s']:.0f} s)"
        )
    truth_in = [f"{f.kind}@{f.target}" for f in human.injected]
    truth_left = [f"{f.kind}@{f.target}" for f in bench.board.build.faults]
    log(
        f"   sim ground truth: injected {truth_in or 'none'}; still present at end: {truth_left or 'none'}"
        + (f"; not applicable: {human.skipped}" if human.skipped else "")
    )
    if bench.board.stress:
        log(f"   electrical stress events seen by the simulator: {bench.board.stress}")
    res.metrics["faults_injected"] = truth_in
    res.metrics["faults_remaining"] = truth_left
    return res


def run_real(
    target: str,
    *,
    port: str = "auto",
    camera: int | str = 0,
    model: str = "scripted",
    vision_model: str | None = None,
    base_url: str | None = None,
    metrics_path: str | Path | None = DEFAULT_LOG,
    participant: str = "",
    uhubctl: list[str] | None = None,
    log: Callable[[str], None] = print,
    ask: Callable[[str], str] = input,
    bench: Any = None,
) -> RunResult:
    """Full loop on real hardware: a person at the terminal is the hands."""
    from .bench.real_bench import RealBench

    gadgets = registry()
    g = gadgets.get(target)
    spec = g.spec if g else target
    provider, _ = parse_model_spec(model)
    if provider == "scripted" and g is None:
        raise SystemExit("free-form specs need a real model: --model ollama[:MODEL] (free, local) or --model anthropic")
    llm = _client(model, target, base_url)
    inv = load_inventory()
    bench = bench or RealBench(port=port, camera=camera, uhubctl=uhubctl)
    human = ConsoleHuman(bench, input_fn=ask, print_fn=log)
    vision: Vision
    vm = vision_model or (model if provider in ("anthropic",) else None)
    if vm:
        vision = ModelVision(make_client(vm, base_url=base_url))
    else:
        vision = ConsoleVision(ask)
    metrics = Metrics(metrics_path, gadget=g.id if g else "custom", mode=f"real+{provider}", participant=participant)
    log(
        f"#### Hands: {g.title if g else spec}  [REAL bench; model: {model}; "
        f"camera: {'model vision' if vm else 'you answer questions'}]"
    )
    log("#### Low voltage only. Wire with USB unplugged. If anything gets hot or smells, unplug immediately.")
    eng = Engineer(inv, llm, bench, human, vision, metrics, log=log, protocol=g.protocol if g else "none")
    res = eng.run(spec)
    log(f"\n#### Result: {'WORKING DEVICE' if res.success else 'FAILED: ' + res.reason}")
    return res
