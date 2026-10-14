"""The engineer loop:

spec (safety screen) -> design (LLM) -> rule check (code, HARD GATE) -> wiring guide (code)
-> human builds -> photo check (vision describes, code diffs, HARD GATE) -> power-on smoke
test (INA219) -> firmware (LLM, code-linted) -> test plan (LLM, code-validated) -> run tests
-> diagnose (code first, LLM fallback) -> fix (human or firmware) -> repeat.

Safety invariants enforced here (and tested in ``tests/test_gates.py``):

1. No wiring step is produced for a design that fails :func:`hands.rules.require_safe`.
2. USB power is only applied after the latest wiring passed the photo check, and every
   power-on starts with safe-idle firmware and an INA219 smoke test.
3. Every physical change after power-on happens with USB unplugged, and is followed by
   another photo check and smoke test before power returns.
4. The model can only ask the human to *restore the approved plan* (re-seat a wire,
   re-check a part); it cannot introduce new wiring.
5. Every firmware image passes :func:`hands.fwlint.lint_firmware` before flashing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .bench.base import BenchError
from .circuit import Design, DesignError, extract_json, gpio_number
from .diagnose import (
    DIAG_SERVO_FW,
    DIAG_TOGGLE_FW,
    SAFE_IDLE_FW,
    PathPoint,
    check_part_instruction,
    power_wiring_instruction,
    probe_hole,
    signal_path,
)
from .fwlint import ALLOWED_IMPORTS, button_gpios, lint_firmware
from .inventory import Inventory
from .layout import Layout, place, strip_of
from .llm import LLMClient
from .metrics import Metrics
from .prompts import (
    DESIGN_SYSTEM,
    DIAGNOSE_PROMPT,
    DIAGNOSE_SYSTEM,
    FIRMWARE_PROMPT,
    FIRMWARE_SYSTEM,
    REVISE_PROMPT,
    TESTPLAN_PROMPT,
    TESTPLAN_SYSTEM,
)
from .rules import CheckReport, UnsafeDesignError, check_design, require_safe
from .safety import SafetyRefusal, refusal_message, screen_text
from .vision import Vision
from .wiring import Guide, build_guide, compare

__all__ = [
    "ALLOWED_IMPORTS",
    "AgentConfig",
    "AgentFailure",
    "Engineer",
    "RunResult",
    "extract_code",
    "lint_firmware",
    "validate_fix",
    "validate_test_plan",
]

CHECK_KINDS = {
    "serial_contains",
    "serial_number_range",
    "current_max",
    "current_min",
    "led",
    "logic_toggles",
    "oled_contains",
    "servo_angle",
}
SAFE_FIX_ACTIONS = {"verify_wire", "verify_part", "reseat_wire", "inspect_rails"}
MAX_TESTS, MAX_WAIT_S, MAX_SECONDS = 30, 30.0, 30.0


@dataclass
class AgentConfig:
    max_design_attempts: int = 3
    max_photo_rounds: int = 4
    max_power_attempts: int = 3
    max_firmware_attempts: int = 3
    max_plan_attempts: int = 2
    max_debug_rounds: int = 6


@dataclass
class TestResult:
    id: str
    passed: bool
    check: dict[str, Any]
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    success: bool
    design: Design | None
    layout: Layout | None
    firmware: str | None
    metrics: dict[str, Any]
    results: list[TestResult] = field(default_factory=list)
    reason: str = ""


class AgentFailure(Exception):
    pass


def extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


# ------------------------------------------------------- model-output checks ---


def _num(
    d: dict[str, Any], key: str, lo: float, hi: float, errs: list[str], tid: str, default: float | None = None
) -> None:
    v = d.get(key, default)
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not (lo <= float(v) <= hi):
        errs.append(f"test {tid}: '{key}' must be a number in [{lo:g}, {hi:g}]")


def validate_test_plan(plan: Any, design: Design, inv: Inventory) -> list[str]:
    """Structural and safety validation of a model-written test plan.

    Safety-relevant: a test may only probe/toggle GPIOs that the approved design uses, the
    signal-walk diagnostic never toggles a pin wired to a button, and ``press`` targets
    must be real buttons.
    """
    errs: list[str] = []
    if not isinstance(plan, list) or not plan:
        return ["test plan must be a non-empty list under 'tests'"]
    if len(plan) > MAX_TESTS:
        return [f"test plan has more than {MAX_TESTS} tests"]
    buttons = {i for i, t in design.parts.items() if t in inv.parts and inv.parts[t].kind == "button"}
    btn_gpios = button_gpios(design, inv)
    pin_gpios = set(design.pin_map.values())
    for t in plan:
        if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not isinstance(t.get("check"), dict):
            errs.append("each test needs a string 'id' and a 'check' object")
            continue
        tid, chk = t["id"], t["check"]
        k = chk.get("kind")
        if k not in CHECK_KINDS:
            errs.append(f"test {tid}: unknown check kind {k!r}")
            continue
        setup = t.get("setup", [])
        if not isinstance(setup, list):
            errs.append(f"test {tid}: setup must be a list")
            setup = []
        for a in setup:
            if not isinstance(a, dict) or len(a.keys() & {"send", "press", "wait"}) != 1:
                errs.append(f"test {tid}: each setup step is one of send/press/wait")
            elif "send" in a and (not isinstance(a["send"], str) or len(a["send"]) > 200 or "\n" in a["send"]):
                errs.append(f"test {tid}: 'send' must be one short line")
            elif "press" in a and a["press"] not in buttons:
                errs.append(f"test {tid}: 'press' target {a['press']!r} is not a button in the design")
            elif "wait" in a:
                _num(a, "wait", 0, MAX_WAIT_S, errs, tid)
        if "seconds" in chk:
            _num(chk, "seconds", 0, MAX_SECONDS, errs, tid)
        part = chk.get("part")
        if k in ("led", "logic_toggles", "oled_contains", "servo_angle") and part not in design.parts:
            errs.append(f"test {tid}: part {part!r} is not in the design")
        gp = chk.get("gpio")
        if gp is not None:
            if gp not in pin_gpios:
                errs.append(f"test {tid}: gpio {gp!r} is not in the design's pin_map")
            elif gpio_number(gp) in btn_gpios:
                errs.append(f"test {tid}: gpio {gp} is wired to a button; Hands will not toggle it")
        if k == "serial_contains" and not isinstance(chk.get("text"), str):
            errs.append(f"test {tid}: serial_contains needs 'text'")
        if k == "serial_number_range":
            pat = chk.get("pattern")
            if not isinstance(pat, str) or len(pat) > 100:
                errs.append(f"test {tid}: 'pattern' must be a short regex")
            else:
                try:
                    if re.compile(pat).groups != 1:
                        errs.append(f"test {tid}: 'pattern' needs exactly one group")
                except re.error as e:
                    errs.append(f"test {tid}: bad regex: {e}")
            _num(chk, "min", -1e9, 1e9, errs, tid)
            _num(chk, "max", -1e9, 1e9, errs, tid)
        if k == "current_max":
            _num(chk, "max_ma", 0, 500, errs, tid)
        if k == "current_min":
            _num(chk, "min_ma", 0, 500, errs, tid)
        if k == "led" and chk.get("expect") not in ("on", "off", "blinking"):
            errs.append(f"test {tid}: led expect must be on/off/blinking")
        if k == "servo_angle":
            _num(chk, "deg", 0, 180, errs, tid)
        if k == "oled_contains" and not isinstance(chk.get("text"), str):
            errs.append(f"test {tid}: oled_contains needs 'text'")
    return errs


def validate_fix(fix: Any, layout: Layout) -> dict[str, Any] | None:
    """Accept a model-proposed physical fix only if it *restores the approved plan*.

    Allowed: verify/reseat a planned wire, verify a placed part, inspect the rails.
    Anything else (new wires, moved parts, unknown ids) is rejected.
    """
    if not isinstance(fix, dict):
        return None
    a = fix.get("action")
    if a == "multi":
        subs = [validate_fix(f, layout) for f in fix.get("fixes", []) if isinstance(f, dict)]
        good = [s for s in subs if s is not None]
        return {"action": "multi", "fixes": good} if good and len(good) == len(subs) else None
    if a not in SAFE_FIX_ACTIONS:
        return None
    if a in ("verify_wire", "reseat_wire"):
        return {"action": a, "wire": fix.get("wire")} if fix.get("wire") in {w.id for w in layout.wires} else None
    if a == "verify_part":
        inst = fix.get("inst")
        return {"action": a, "inst": inst} if inst in layout.placements and inst not in ("pico",) else None
    return {"action": "inspect_rails"}


def fix_instruction(fix: dict[str, Any], layout: Layout) -> str:
    """Deterministic human-readable text for a validated fix (the model's prose is only a hint)."""
    a = fix["action"]
    if a == "multi":
        return " Then: ".join(fix_instruction(f, layout) for f in fix["fixes"])
    if a in ("verify_wire", "reseat_wire"):
        w = next(x for x in layout.wires if x.id == fix["wire"])
        return f"Pull wire {w.id} out and push it firmly back in: one end in {w.a}, the other in {w.b}."
    if a == "verify_part":
        pl = layout.placements[fix["inst"]]
        legs = ", ".join(f"{p} in {h}" for p, h in pl.holes.items()) or "its connector sockets"
        return f"Check {fix['inst']}: {legs}. If it is turned the wrong way, pull it out and re-insert it."
    return "Look along the red and blue rail lines for any stray lead or bent leg touching both, and remove it."


# ------------------------------------------------------------------ engineer ---


class Engineer:
    def __init__(
        self,
        inv: Inventory,
        llm: LLMClient,
        bench: Any,
        human: Any,
        vision: Vision,
        metrics: Metrics,
        config: AgentConfig | None = None,
        log: Callable[[str], None] = print,
        protocol: str = "none",
    ):
        self.inv, self.llm, self.bench, self.human, self.vision = inv, llm, bench, human, vision
        self.m, self.cfg, self.log = metrics, config or AgentConfig(), log
        self.protocol = protocol
        self.spec = ""
        self.design: Design | None = None
        self.report: CheckReport | None = None
        self.layout: Layout | None = None
        self.guide: Guide | None = None
        self.firmware: str | None = None
        self.serial_seen = ""
        self.boot_mark = 0
        self.history: list[str] = []
        self.wiring_verified = False  # True only between a passing photo check and the next physical change
        self.powered = False

    # ---------------------------------------------------------------- gates
    def _approved(self) -> tuple[Design, Layout]:
        """Re-assert gate 1 (the rule check) before anything physical happens."""
        if self.design is None or self.layout is None or self.layout.report is None:
            raise AgentFailure("safety gate: no rule-checked design/layout")
        rep = require_safe(self.design, self.inv)
        if rep.digest != self.layout.report.digest:
            raise AgentFailure("safety gate: design changed after it was checked")
        return self.design, self.layout

    def _ensure_wiring_verified(self) -> None:
        if not self.wiring_verified:
            raise AgentFailure("safety gate: wiring has not passed the photo check since the last change")

    def _human_change(self, instruction: str, fix: dict[str, Any] | None, phase: str, reason: str = "") -> None:
        """Every physical change goes through here: it invalidates the photo check."""
        if self.powered:
            raise AgentFailure("safety gate: refusing to ask for a wiring change while USB is plugged in")
        self.wiring_verified = False
        self.m.event("intervention", phase=phase, reason=reason[:200], instruction=instruction[:400])
        self.human.do(instruction, fix)

    def _plug(self) -> None:
        self.human.plug_usb()
        self.powered = True
        self.m.event("power_on")

    def _unplug(self, reason: str) -> None:
        self.human.unplug_usb(reason)
        self.powered = False
        self.m.event("power_cut", reason=reason[:200])

    # ================================================================ run
    def run(self, spec: str) -> RunResult:
        self.spec = spec
        self.m.event("run_start", spec=spec)
        self.log(f"== Spec: {spec}")
        try:
            refusals = screen_text(spec)
            if refusals:
                self.m.event("refused", categories=[r.category for r in refusals])
                raise AgentFailure(refusal_message(refusals))
            self.stage_design()
            self.stage_prepare_board()
            self.stage_wiring()
            self.stage_photo_check()
            self.stage_power_on()
            self.stage_firmware()
            plan = self.stage_test_plan()
            ok, results = self.stage_test_and_debug(plan)
            return self._finish(ok, results, "" if ok else "tests still failing after max debug rounds")
        except (AgentFailure, SafetyRefusal, UnsafeDesignError) as e:
            self.log(f"!! Agent stopped: {e}")
            if self.powered:
                try:
                    self._unplug(f"stopping: {str(e)[:120]}")
                except BenchError:
                    pass
            return self._finish(False, [], str(e))

    def _finish(self, ok: bool, results: list[TestResult], reason: str) -> RunResult:
        c = self.m.counters
        human_s = getattr(self.human, "est_seconds", 0.0)
        bench_s = getattr(getattr(self.bench, "board", None), "now", 0.0)
        if ok and self.bench.mode == "sim":
            ttw: float | None = round(human_s + bench_s, 1)
        else:
            ttw = round(self.m.elapsed(), 1) if ok else None
        metrics = {
            "time_to_working_s": ttw,
            "est_human_s": round(human_s, 1),
            "sim_bench_s": round(bench_s, 1),
            "debug_rounds": int(c["debug_rounds"]),
            "design_errors_caught": int(c["design_errors_caught"]),
            "errors_caught_pre_power": int(c["errors_caught_pre_power"]),
            "errors_caught_post_power": int(c["errors_caught_post_power"]),
            "interventions": int(c["interventions"]),
            "firmware_versions": int(c["firmware_versions"]),
            "firmware_lint_rejections": int(c["firmware_lint_rejections"]),
            "llm_calls": len(self.llm.calls),
            "wiring_steps": len(self.guide.steps) if self.guide else 0,
            "tests_passed": sum(r.passed for r in results),
            "tests_total": len(results),
        }
        self.m.event("run_end", success=ok, reason=reason[:500], metrics=metrics)
        return RunResult(ok, self.design, self.layout, self.firmware, metrics, results, reason)

    # ============================================================= design
    def stage_design(self) -> None:
        system = DESIGN_SYSTEM.format(inventory=self.inv.summary_for_prompt())
        reply = self.llm.complete(system, f"Gadget spec: {self.spec}", stage="design")
        for attempt in range(1, self.cfg.max_design_attempts + 1):
            try:
                design = Design.from_json(reply)
            except DesignError as e:
                self.log(f"   design JSON invalid ({e}); asking again")
                self.m.event("rule_check", attempt=attempt, errors=[f"invalid JSON: {e}"], warnings=[])
                reply = self.llm.complete(
                    system, f"Gadget spec: {self.spec}\nYour last reply was invalid: {e}", stage="design"
                )
                continue
            report = check_design(design, self.inv)
            self.m.event(
                "rule_check",
                attempt=attempt,
                errors=[str(f) for f in report.errors],
                warnings=[str(f) for f in report.warnings],
                digest=report.digest,
            )
            self.log(
                f"-- Design v{attempt} '{design.name}': {len(design.parts)} parts, "
                f"{len(design.nets)} nets\n" + _indent(report.render())
            )
            if report.ok:
                self.design, self.report = design, report
                self.layout = place(design, self.inv)  # gate 1 runs again inside place()
                return
            self.m.inc("design_errors_caught", len(report.errors))
            reply = self.llm.complete(
                system,
                REVISE_PROMPT.format(
                    design=json.dumps(design.to_dict()), findings="\n".join(str(f) for f in report.errors)
                ),
                stage="design_revise",
            )
        raise AgentFailure("could not produce a design that passes the safety rules")

    # ============================================================ prepare
    def stage_prepare_board(self) -> None:
        """Before any wiring: replace whatever program is on the Pico with safe-idle firmware.

        A real Pico keeps its last ``main.py`` across power cycles. Without this step an old
        program could drive pins into the new wiring the moment USB is plugged in.
        """
        self.log("-- Prepare: plug in the bare Pico (nothing on the breadboard yet), load safe-idle firmware")
        self.human.say(
            "Before wiring: plug the Pico into USB with NOTHING else connected. Hands will load a "
            "safe program, then ask you to unplug it."
        )
        self._plug()
        try:
            res = self.bench.flash_firmware(SAFE_IDLE_FW)
            self.m.event(
                "flash", why="safe-idle (prepare)", bytes=len(SAFE_IDLE_FW), boot=res.get("boot_output", "")[-200:]
            )
        except BenchError as e:
            raise AgentFailure(f"could not load safe-idle firmware on the bare Pico: {e}") from e
        finally:
            self._unplug("prepared; now wire the circuit with USB unplugged")

    # ============================================================= wiring
    def stage_wiring(self) -> None:
        _, layout = self._approved()
        self.guide = build_guide(layout, self.inv)  # build_guide re-checks gate 1 too
        self.log(f"-- Wiring guide: {len(self.guide.steps)} steps, {len(layout.wires)} jumpers")
        if hasattr(self.human, "begin_build"):
            self.human.begin_build(layout)
        self.wiring_verified = False
        for step in self.guide.steps:
            self.log(f"   step {step.n:>2}: {step.title}")
            self.human.perform_step(step)
            step.done = True
            self.m.event("wiring_step", n=step.n, step_kind=step.kind)
        if hasattr(self.human, "finish_wiring"):
            self.human.finish_wiring()
        self.m.event("wiring_done", steps=len(self.guide.steps))

    def stage_photo_check(self) -> None:
        _, layout = self._approved()
        for rnd in range(1, self.cfg.max_photo_rounds + 1):
            snap = self.bench.camera_snapshot()
            obs = self.vision.observe_wiring(snap, layout)
            diffs = compare(layout, obs)
            unclear = [str(u) for u in obs.get("uncertain", [])] if isinstance(obs, dict) else []
            self.m.event("photo_check", round=rnd, discrepancies=[d.message for d in diffs], uncertain=unclear[:5])
            if not diffs and unclear:
                self.log(f"-- Photo check round {rnd}: photo unclear ({'; '.join(unclear[:3])}); retaking")
                self.human.say(
                    "The photo was unclear. Retake it from directly above, in good light, "
                    "with the whole breadboard in view."
                )
                continue
            if not diffs:
                self.log(f"-- Photo check round {rnd}: wiring matches the plan")
                self.wiring_verified = True
                return
            self.log(f"-- Photo check round {rnd}: {len(diffs)} problem(s) caught BEFORE power-on")
            self.m.inc("errors_caught_pre_power", len(diffs))
            for d in diffs:
                self.log(f"   ! {d.message}\n     -> {d.instruction}")
                self.m.inc("interventions")
                self._human_change(d.instruction, d.fix, "pre_power", d.message)
        raise AgentFailure("wiring still differs from the plan after several photo checks")

    # ========================================================== power-on
    def _idle_budget_ma(self) -> float:
        est = self.report.rail_current_ma.get("3V3", 0.0) if self.report else 0.0
        return max(50.0, 1.5 * est + 20.0)

    def stage_power_on(self) -> None:
        """Gate 2: power only after a passing photo check; safe-idle + INA219 smoke test first."""
        _, layout = self._approved()
        budget = self._idle_budget_ma()
        for attempt in range(1, self.cfg.max_power_attempts + 1):
            self._ensure_wiring_verified()
            self.log(f"-- Power-on #{attempt}: plug in USB, flash safe-idle firmware, read 3V3 rail current")
            self._plug()
            try:
                self.bench.flash_firmware(SAFE_IDLE_FW)
                r = self.bench.read_current(1.0)
                self.m.event("smoke_test", attempt=attempt, avg_ma=r.avg_ma, max_ma=r.max_ma, budget_ma=budget)
                if r.max_ma <= budget:
                    self.log(f"   3V3 rail idle current {r.avg_ma:.1f} mA (budget {budget:.0f} mA): OK")
                    return
                problem = f"idle current {r.max_ma:.0f} mA exceeds budget {budget:.0f} mA"
            except BenchError as e:
                problem = str(e)
                self.m.event("smoke_test", attempt=attempt, error=problem)
            self.log(f"   !! {problem}")
            self._unplug(problem)
            self.m.inc("errors_caught_post_power")
            self.m.inc("debug_rounds")
            self.m.inc("interventions")
            txt, fix = power_wiring_instruction(layout)
            self.log(f"   -> {txt[:160]}...")
            self._human_change(txt, fix, "post_power", problem)
            self.stage_photo_check()
        raise AgentFailure("power-on smoke test keeps failing; stopping for safety")

    def _physical_fix(self, instruction: str, fix: dict[str, Any] | None, reason: str) -> None:
        """Unplug -> change -> photo check -> smoke test -> app firmware back on."""
        try:  # park the board on safe-idle firmware so the old app can't run at the next plug-in
            if self.powered:
                self.bench.flash_firmware(SAFE_IDLE_FW)
        except BenchError:
            pass
        if self.powered:
            self._unplug(reason)
        self.m.inc("errors_caught_post_power")
        self.m.inc("interventions")
        self._human_change(instruction, fix, "post_power", reason)
        self.stage_photo_check()
        self.stage_power_on()
        if self.firmware:
            self._flash(self.firmware, "firmware (after hardware fix)")

    # ========================================================== firmware
    def _lint(self, code: str) -> list[str]:
        assert self.design is not None
        return lint_firmware(code, self.design.pin_map, self.design, self.inv)

    def stage_firmware(self, extra: str = "") -> None:
        design, _ = self._approved()
        prompt = (
            FIRMWARE_PROMPT.format(
                spec=self.spec,
                design=json.dumps(design.to_dict()),
                pin_map=json.dumps(design.pin_map),
                protocol=self.protocol,
            )
            + extra
        )
        for _attempt in range(1, self.cfg.max_firmware_attempts + 1):
            code = extract_code(self.llm.complete(FIRMWARE_SYSTEM, prompt, stage="firmware"))
            errs = self._lint(code)
            if errs:
                self.m.inc("firmware_lint_rejections")
                self.m.event("firmware_lint", errors=errs[:10])
                self.log(f"   firmware lint failed: {errs}")
                prompt += "\nYour previous firmware failed lint: " + "; ".join(errs)
                continue
            self._flash(code, "firmware")
            return
        raise AgentFailure("could not get firmware that passes lint")

    def _flash(self, code: str, why: str) -> None:
        errs = self._lint(code)
        if errs:  # defence in depth: nothing unlinted reaches the board
            raise AgentFailure(f"refusing to flash {why}: {errs}")
        self._ensure_wiring_verified()
        self.firmware = code
        self.m.inc("firmware_versions")
        res = self.bench.flash_firmware(code)
        boot = res.get("boot_output", "")
        self.boot_mark = len(self.serial_seen)
        self.serial_seen += boot
        self.m.event("flash", why=why, bytes=len(code), boot=boot[-300:])
        self.log(f"-- Flashed {why} ({len(code)} bytes). Boot: {boot.strip()[:100]!r}")

    # ========================================================= test plan
    def stage_test_plan(self) -> list[dict[str, Any]]:
        design, _ = self._approved()
        assert self.report is not None
        prompt = TESTPLAN_PROMPT.format(
            spec=self.spec,
            design=json.dumps(design.to_dict()),
            firmware=self.firmware,
            rail_ma=self.report.rail_current_ma.get("3V3"),
        )
        errs: list[str] = []
        for _ in range(self.cfg.max_plan_attempts):
            reply = self.llm.complete(TESTPLAN_SYSTEM, prompt, stage="test_plan")
            try:
                plan = extract_json(reply).get("tests", [])
            except DesignError as e:
                errs = [str(e)]
            else:
                errs = validate_test_plan(plan, design, self.inv)
                if not errs:
                    self.m.event("test_plan", tests=[t["id"] for t in plan])
                    self.log(f"-- Test plan: {', '.join(t['id'] for t in plan)}")
                    return list(plan)
            self.log(f"   test plan rejected: {errs[:3]}")
            prompt += "\nYour previous test plan was rejected: " + "; ".join(errs[:10])
        raise AgentFailure(f"test plan invalid: {errs[:5]}")

    # ================================================== tests + debugging
    def stage_test_and_debug(self, plan: list[dict[str, Any]]) -> tuple[bool, list[TestResult]]:
        results: list[TestResult] = []
        for rnd in range(self.cfg.max_debug_rounds + 1):
            results = []
            failed = None
            for t in plan:
                r = self.run_test(t)
                results.append(r)
                self.m.event("test", round=rnd, id=r.id, passed=r.passed, evidence=r.evidence)
                self.log(f"   [{'PASS' if r.passed else 'FAIL'}] {r.id}: {_brief(r.evidence)}")
                if not r.passed:
                    failed = r
                    break
            if failed is None:
                self.log(f"== All {len(plan)} tests pass: device WORKING")
                return True, results
            if rnd == self.cfg.max_debug_rounds:
                break
            self.m.inc("debug_rounds")
            self.log(f"-- Debug round {rnd + 1}: diagnosing '{failed.id}'")
            self.diagnose(failed)
        return False, results

    def _read_serial(self, seconds: float) -> str:
        s: str = self.bench.read_serial(seconds)
        self.serial_seen += s
        return s

    def _probe_hole_for(self, part: str, pin: str | None) -> str:
        assert self.layout is not None
        pl = self.layout.placements[part]
        if not pl.holes:
            return f"{part}.{pin}"
        hole = pl.holes.get(pin or "") or next(iter(pl.holes.values()))
        return probe_hole(self.layout, strip_of(hole))

    def run_test(self, t: dict[str, Any]) -> TestResult:
        chk = t["check"]
        ev: dict[str, Any] = {}
        mark = len(self.serial_seen)
        ok = False
        try:
            for a in t.get("setup", []):
                if "send" in a:
                    self.bench.write_serial(a["send"])
                elif "press" in a:
                    self.human.press(a["press"], float(a.get("hold_s", 0.3)))
                elif "wait" in a:
                    self.bench.wait(float(a["wait"]))
            k = chk["kind"]
            secs = float(chk.get("seconds", 1.0))
            if k == "serial_contains":
                self._read_serial(secs)
                text = self.serial_seen[self.boot_mark if chk.get("since") == "boot" else mark :]
                ok = chk["text"] in text
                ev = {"serial": text[-200:]}
            elif k == "serial_number_range":
                self._read_serial(secs)
                text = self.serial_seen[mark:]
                vals = [float(x) for x in re.findall(chk["pattern"], text[-4000:])]
                ok = bool(vals) and all(chk["min"] <= v <= chk["max"] for v in vals)
                ev = {"values": vals[-5:], "serial": text[-200:]}
            elif k in ("current_max", "current_min"):
                r = self.bench.read_current(secs)
                ok = r.max_ma <= chk["max_ma"] if k == "current_max" else r.max_ma >= chk["min_ma"]
                ev = {"avg_ma": r.avg_ma, "max_ma": r.max_ma}
            elif k == "led":
                ok, ev = self._check_led(chk["part"], chk["expect"], secs)
            elif k == "logic_toggles":
                hole = self._probe_hole_for(chk["part"], chk.get("pin"))
                self.human.clip_probe("D0", hole)
                cap = self.bench.capture_logic({"D0": hole}, secs).channels["D0"]
                ok = cap.edges >= int(chk.get("min_edges", 2)) or bool(cap.pwm_hz)
                ev = {"probe": hole, "edges": cap.edges, "high_fraction": cap.high_fraction, "pwm_hz": cap.pwm_hz}
            elif k == "oled_contains":
                self.bench.wait(secs)
                st = self.vision.observe_state(self.bench.camera_snapshot(), [chk["part"]])
                lines = st.get("oled", {}).get(chk["part"], [])
                ok = chk["text"] in " ".join(lines)
                ev = {"oled": lines}
            elif k == "servo_angle":
                self.bench.wait(secs)
                st = self.vision.observe_state(self.bench.camera_snapshot(), [chk["part"]])
                ang = st.get("servo_deg", {}).get(chk["part"])
                ok = isinstance(ang, (int, float)) and abs(ang - chk["deg"]) <= chk.get("tol", 10)
                ev = {"servo_deg": ang}
            else:
                ok, ev = False, {"error": f"unknown check {k}"}
        except BenchError as e:
            ok, ev = False, {"bench_error": str(e)}
        if "Traceback" in self.serial_seen[mark:]:
            ev["traceback"] = self.serial_seen[mark:][self.serial_seen[mark:].index("Traceback") :][:300]
        self.serial_seen += self._drain()
        return TestResult(t["id"], bool(ok), chk, ev)

    def _drain(self) -> str:
        try:
            s: str = self.bench.read_serial(0.0)
            return s
        except BenchError:
            return ""

    def _check_led(self, part: str, expect: str, secs: float) -> tuple[bool, dict[str, Any]]:
        if getattr(self.vision, "interactive", False):  # a person is the camera: ask once
            self.bench.wait(max(secs, 0.2))
            seen = self.vision.observe_led_pattern(part)  # type: ignore[attr-defined]
            return seen == expect, {"led_seen": [seen]}
        n = 8 if expect == "blinking" else 1
        states = []
        for _ in range(n):
            self.bench.wait(max(secs, 0.2) / n)
            st = self.vision.observe_state(self.bench.camera_snapshot(), [part])
            states.append(st.get("leds", {}).get(part, "off"))
        ok = ("on" in states and "off" in states) if expect == "blinking" else states[-1] == expect
        return ok, {"led_seen": states}

    # ======================================================== diagnosis
    def diagnose(self, r: TestResult) -> None:
        assert self.design is not None and self.layout is not None
        chk, ev = r.check, r.evidence
        err = ev.get("bench_error", "")
        if "not responding" in err or "unpowered" in err:
            self.log("   board unresponsive -> treating as a power fault")
            txt, fix = power_wiring_instruction(self.layout)
            self._physical_fix(txt, fix, err)
            return
        if chk["kind"] == "current_max" and not r.passed:
            self.log("   over-current -> cutting power and checking power wiring")
            txt, fix = power_wiring_instruction(self.layout)
            self._physical_fix(txt, fix, f"3V3 rail at {ev.get('max_ma')} mA > {chk['max_ma']} mA")
            return
        tb = ev.get("traceback", "") + ev.get("serial", "")
        i2c_parts = [i for i, t in self.design.parts.items() if "i2c_addr" in self.inv.parts[t].facts]
        if i2c_parts and ("EIO" in tb or "I2C scan: []" in self.serial_seen or chk["kind"] == "oled_contains"):
            key = f"i2c:{r.id}"
            if self.history.count(key) < 1:
                self.history.append(key)
                txts, fixes = [], []
                for inst in i2c_parts:
                    txt, fix = check_part_instruction(self.layout, self.inv, inst)
                    txts.append(txt)
                    fixes.append(fix)
                self.log(f"   I2C device(s) {i2c_parts} not answering -> ask human to check their wiring")
                self._physical_fix(" ".join(txts), {"action": "multi", "fixes": fixes}, "I2C device not answering")
                return
        dht = [i for i, t in self.design.parts.items() if self.inv.parts[t].kind == "dht22"]
        if dht and ("SENSOR_ERROR" in tb or "ETIMEDOUT" in tb):
            key = f"dht:{r.id}"
            if self.history.count(key) < 1:
                self.history.append(key)
                txt, fix = check_part_instruction(self.layout, self.inv, dht[0])
                self.log(f"   DHT22 not answering -> ask human: {txt[:150]}...")
                self._physical_fix(txt, fix, "DHT22 not answering")
                return
        if chk.get("part") and chk.get("gpio") and chk["kind"] in ("led", "logic_toggles", "servo_angle"):
            key = f"walk:{r.id}"
            if self.history.count(key) < 2:
                self.history.append(key)
                if self.signal_walk(chk["gpio"], chk["part"], r):
                    return
        self.llm_diagnose(r)

    def signal_walk(self, gpio: str, part: str, r: TestResult) -> bool:
        """Known-good firmware + logic probe walked from the Pico pin to the part.
        Returns True if it identified (and had the human fix) a hardware fault."""
        assert self.layout is not None and self.design is not None and self.firmware is not None
        kind = self.inv.parts[self.layout.placements[part].type_id].kind
        n = gpio_number(gpio)
        if gpio not in self.design.pin_map.values() or n in button_gpios(self.design, self.inv):
            raise AgentFailure(f"refusing to toggle {gpio}: not a design output pin")
        main_fw = self.firmware
        path = signal_path(self.layout, self.inv, gpio, part)
        self.log(f"   signal walk {gpio} -> {part}: " + " -> ".join(p.strip for p in path))
        first = path[0] if path else None
        main_cap = None
        if first:
            self.human.clip_probe("D0", first.hole)
            main_cap = self.bench.capture_logic({"D0": first.hole}, 1.5).channels["D0"]
        diag = (DIAG_SERVO_FW if kind == "servo" else DIAG_TOGGLE_FW).format(n=n)
        if self._lint(diag):
            raise AgentFailure(f"diagnostic firmware failed lint: {self._lint(diag)}")
        self.bench.flash_firmware(diag)
        self.m.event("flash", why="diagnostic", gpio=gpio)
        last_ok: PathPoint | None = None
        evidence: dict[str, Any] = {"main_fw_at_pin": main_cap.__dict__ if main_cap else None, "points": []}
        for p in path:
            if not p.probe:
                continue
            self.human.clip_probe("D0", p.hole)
            cap = self.bench.capture_logic({"D0": p.hole}, 1.5).channels["D0"]
            alive = cap.edges >= 2 or bool(cap.pwm_hz)
            evidence["points"].append(
                {"hole": p.hole, "net": p.net, "alive": alive, "edges": cap.edges, "pwm_hz": cap.pwm_hz}
            )
            self.log(f"     probe {p.hole:>5} ({p.net}): {'signal' if alive else 'NO SIGNAL'}")
            if not alive:
                via = p.via
                self.m.event("diagnosis", method="signal_walk", finding=f"signal lost at {p.hole}", via=via)
                if via and via[0] == "wire":
                    w = next(x for x in self.layout.wires if x.id == via[1])
                    txt = (
                        f"The signal reaches {last_ok.hole if last_ok else gpio} but not {p.hole}. "
                        f"Wire {w.id} should run {w.a} -> {w.b}: pull it out and push both ends firmly back "
                        f"into exactly those holes."
                    )
                    fix: dict[str, Any] = {"action": "verify_wire", "wire": w.id}
                else:
                    inst = via[1] if via else part
                    txt, fix = check_part_instruction(self.layout, self.inv, inst)
                    txt = f"The signal stops at {inst}. " + txt
                self.log(f"   -> ask human: {txt[:200]}")
                self._physical_fix(txt, fix, "signal walk found a break")
                return True
            last_ok = p
        load_ok = self._load_responds(part, kind)
        evidence["load_responds_to_diag_fw"] = load_ok
        r.evidence["signal_walk"] = evidence
        if not load_ok:
            self.m.event("diagnosis", method="signal_walk", finding=f"signal reaches {part} but it doesn't respond")
            txt, fix = check_part_instruction(self.layout, self.inv, part)
            txt = (
                f"The signal reaches {part} correctly, so {part} itself is suspect "
                f"(backwards, wrong leg, or bad power wire). " + txt
            )
            self.log(f"   -> ask human: {txt[:200]}")
            self._physical_fix(txt, fix, f"{part} does not respond")
            return True
        self.m.event("diagnosis", method="signal_walk", finding="hardware OK with diagnostic firmware; firmware bug")
        self.log("     hardware works with known-good firmware -> application firmware is at fault")
        self._flash(main_fw, "firmware (restore after walk)")
        return False

    def _load_responds(self, part: str, kind: str) -> bool:
        if kind == "led":
            ok, _ = self._check_led(part, "blinking", 2.0)
            return ok
        if kind == "servo":
            angs = []
            for _ in range(5):
                self.bench.wait(0.45)  # not a multiple of the 0.6 s sweep, so samples see both ends
                st = self.vision.observe_state(self.bench.camera_snapshot(), [part])
                angs.append(st.get("servo_deg", {}).get(part))
            vals = [a for a in angs if isinstance(a, (int, float))]
            return len(vals) >= 2 and max(vals) - min(vals) > 45
        if kind == "buzzer":
            r = self.bench.read_current(2.0)
            return bool(r.max_ma - r.avg_ma > 5 or r.max_ma > 15)  # buzzer draws ~25 mA from the 3V3 rail
        return True

    def llm_diagnose(self, r: TestResult) -> None:
        assert self.design is not None and self.layout is not None
        self.log("   deterministic checks exhausted -> asking the model for a diagnosis")
        reply = self.llm.complete(
            DIAGNOSE_SYSTEM,
            DIAGNOSE_PROMPT.format(
                spec=self.spec,
                design=json.dumps(self.design.to_dict()),
                layout=json.dumps(self.layout.to_dict()),
                firmware=self.firmware,
                test=json.dumps(r.check),
                evidence=json.dumps(r.evidence, default=str),
                history=json.dumps(self.history[-6:]),
            ),
            stage="diagnose",
        )
        try:
            d = extract_json(reply)
        except DesignError as e:
            raise AgentFailure(f"model diagnosis was not JSON: {e}") from e
        raw_act = d.get("action")
        act: dict[str, Any] = raw_act if isinstance(raw_act, dict) else {}
        self.m.event("diagnosis", method="llm", hypothesis=str(d.get("hypothesis"))[:300], action=act.get("type"))
        self.log(f"     model hypothesis: {d.get('hypothesis')}")
        self.history.append(f"llm:{r.id}:{act.get('type')}")
        if act.get("type") == "firmware_patch" and isinstance(act.get("code"), str):
            code = extract_code(act["code"]) if "```" in act["code"] else act["code"]
            errs = self._lint(code)
            if errs:
                self.m.inc("firmware_lint_rejections")
                self.log(f"     patch rejected by lint: {errs}")
                return
            self._flash(code, "firmware patch")
        elif act.get("type") == "ask_human":
            fix = validate_fix(act.get("fix"), self.layout)
            note = str(act.get("instruction", ""))[:300]
            if fix is None or screen_text(note):
                self.m.event("diagnosis", method="llm", rejected="fix does not restore the approved plan")
                self.log("     model's suggested physical change rejected: it does not restore the approved plan")
                return
            txt = fix_instruction(fix, self.layout) + (f" (Hint: {note})" if note else "")
            self._physical_fix(txt, fix, f"model diagnosis: {str(d.get('hypothesis'))[:120]}")
        else:
            raise AgentFailure(f"no actionable fix for failing test '{r.id}' (model: {d.get('hypothesis')})")


def _indent(s: str, n: int = 3) -> str:
    return "\n".join(" " * n + line for line in s.splitlines())


def _brief(ev: dict[str, Any]) -> str:
    s = json.dumps(ev, default=str)
    return s if len(s) < 140 else s[:137] + "..."
