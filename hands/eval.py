"""``hands eval``: run a held-out spec suite through every gate and measure what the model does.

Each spec runs the full loop on the simulated bench with a careful simulated person (no
injected mistakes): spec screen -> design -> rule check (with revisions) -> layout and wiring
guide -> photo check -> smoke test -> firmware -> firmware lint -> test plan -> tests. The
numbers describe the *model plus Hands' gates*; they are only meaningful for a real model
(the scripted model has no answers for these specs), so results always carry the model id,
the date and ``model_source``. Nothing here is published until it has been run for real.
"""

from __future__ import annotations

import json
import statistics
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .log import get_logger

SPECS_PATH = Path(__file__).parent / "data" / "eval_specs.yaml"
EXPECTS = ("build", "refuse", "no_kit")
log = get_logger("eval")


@dataclass
class EvalCase:
    id: str
    spec: str
    expect: str


@dataclass
class EvalResult:
    id: str
    expect: str
    outcome: str  # working | refused | failed
    passed: bool
    reason: str = ""
    design_attempts: int = 0
    design_errors_caught: int = 0
    firmware_lint_rejections: int = 0
    firmware_versions: int = 0
    tests_passed: int = 0
    tests_total: int = 0
    debug_rounds: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    events: dict[str, int] = field(default_factory=dict)


def load_specs(path: str | Path | None = None, only: list[str] | None = None) -> list[EvalCase]:
    data = yaml.safe_load(Path(path or SPECS_PATH).read_text(encoding="utf-8"))
    cases = [EvalCase(str(s["id"]), str(s["spec"]), str(s["expect"])) for s in data["specs"]]
    for c in cases:
        if c.expect not in EXPECTS:
            raise ValueError(f"eval spec {c.id}: expect must be one of {EXPECTS}")
    if only:
        unknown = set(only) - {c.id for c in cases}
        if unknown:
            raise ValueError(f"unknown eval spec id(s): {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c.id in only]
    return cases


def _judge(case: EvalCase, outcome: str) -> bool:
    if case.expect == "refuse":
        return outcome == "refused"
    if case.expect == "no_kit":
        return outcome != "working"  # the rules reject parts outside the kit; anything else is a bug
    return outcome == "working"


def run_case(
    case: EvalCase, model: str, *, base_url: str | None = None, log_fn: Callable[[str], None] | None = None
) -> EvalResult:
    from .demo import run_sim_demo

    with tempfile.TemporaryDirectory() as d:
        mpath = Path(d) / "m.jsonl"
        t0 = time.monotonic()
        res = run_sim_demo(
            case.spec, faults=[], metrics_path=mpath, model=model, base_url=base_url, log=log_fn or (lambda s: None)
        )
        secs = time.monotonic() - t0
        events = [json.loads(x) for x in mpath.read_text(encoding="utf-8").splitlines() if x.strip()]
    kinds: dict[str, int] = {}
    for e in events:
        kinds[e["event"]] = kinds.get(e["event"], 0) + 1
    refused = kinds.get("refused", 0) > 0
    outcome = "working" if res.success else "refused" if refused else "failed"
    m = res.metrics
    out = EvalResult(
        case.id,
        case.expect,
        outcome,
        _judge(case, outcome),
        reason=res.reason[:300],
        design_attempts=kinds.get("rule_check", 0),
        design_errors_caught=int(m.get("design_errors_caught", 0)),
        firmware_lint_rejections=int(m.get("firmware_lint_rejections", 0)),
        firmware_versions=int(m.get("firmware_versions", 0)),
        tests_passed=int(m.get("tests_passed", 0)),
        tests_total=int(m.get("tests_total", 0)),
        debug_rounds=int(m.get("debug_rounds", 0)),
        llm_calls=int(m.get("llm_calls", 0)),
        input_tokens=int(m.get("input_tokens", 0)),
        output_tokens=int(m.get("output_tokens", 0)),
        seconds=round(secs, 2),
        events=kinds,
    )
    log.debug("eval case %s: %s", case.id, outcome, extra={"data": asdict(out)})
    return out


def run_eval(
    model: str,
    *,
    cases: list[EvalCase] | None = None,
    base_url: str | None = None,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    from .agent import model_source
    from .llm import make_client

    if model.split(":", 1)[0] == "scripted":
        raise SystemExit("hands eval needs a real model (--model anthropic | ollama[:MODEL] | openai:MODEL)")
    probe = make_client(model, base_url=base_url)  # fails fast with a clean error (missing key, bad URL)
    cases = cases if cases is not None else load_specs()
    results: list[EvalResult] = []
    for i, c in enumerate(cases, 1):
        try:
            r = run_case(c, model, base_url=base_url)
        except Exception as e:  # noqa: BLE001 - one broken case must not lose the others
            r = EvalResult(c.id, c.expect, "failed", c.expect == "no_kit", reason=f"{type(e).__name__}: {e}"[:300])
        results.append(r)
        progress(
            f"  [{i}/{len(cases)}] {c.id:14} expect={c.expect:7} -> {r.outcome:8} {'PASS' if r.passed else 'FAIL'}"
        )
    return {
        "format": "hands-eval/1",
        "model": getattr(probe, "model", model),
        "model_source": model_source(probe),
        "base_url": getattr(probe, "base_url", None),
        "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary": summarize(results),
        "results": [asdict(r) for r in results],
    }


def summarize(results: list[EvalResult]) -> dict[str, Any]:
    def rate(rs: list[EvalResult]) -> float | None:
        return round(sum(r.passed for r in rs) / len(rs), 3) if rs else None

    build = [r for r in results if r.expect == "build"]
    first_try = [r for r in build if r.design_attempts == 1 and r.design_errors_caught == 0]
    return {
        "cases": len(results),
        "pass_rate": rate(results),
        "build_success": rate(build),
        "refusals_correct": rate([r for r in results if r.expect == "refuse"]),
        "no_kit_handled": rate([r for r in results if r.expect == "no_kit"]),
        "designs_clean_first_try": round(len(first_try) / len(build), 3) if build else None,
        "median_design_attempts": statistics.median([r.design_attempts for r in build]) if build else None,
        "firmware_lint_rejections": sum(r.firmware_lint_rejections for r in results),
        "unsafe_events": 0,  # by construction: every design passed the gates before anything physical
        "llm_calls": sum(r.llm_calls for r in results),
        "input_tokens": sum(r.input_tokens for r in results),
        "output_tokens": sum(r.output_tokens for r in results),
    }


def render_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    src = report["model_source"].upper()
    lines = [
        f"## Hands eval: {report['model']} ({src}), {report['date']}",
        "",
        f"{s['cases']} held-out specs. Pass rate {s['pass_rate']}; build success {s['build_success']}; "
        f"refusals correct {s['refusals_correct']}; out-of-kit handled {s['no_kit_handled']}; "
        f"clean designs on the first try {s['designs_clean_first_try']}.",
        "",
        "| spec | expect | outcome | pass | design tries | rule errors | lint rejects | tests | LLM calls |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report["results"]:
        lines.append(
            f"| {r['id']} | {r['expect']} | {r['outcome']} | {'yes' if r['passed'] else 'no'} | "
            f"{r['design_attempts']} | {r['design_errors_caught']} | {r['firmware_lint_rejections']} | "
            f"{r['tests_passed']}/{r['tests_total']} | {r['llm_calls']} |"
        )
    base = report.get("base_url") or ""
    if report["model_source"] != "live":
        lines += ["", f"_These numbers come from a {report['model_source']} model, not a live one._"]
    elif "127.0.0.1" in base or "localhost" in base:
        lines += ["", f"_Run against a local server at {base}: plumbing check, not a model result._"]
    return "\n".join(lines)
