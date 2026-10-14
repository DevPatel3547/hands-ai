"""The rule check and the photo check are hard gates: regression tests for every bypass found in review."""

import json

import pytest

from hands.agent import AgentFailure, Engineer, fix_instruction, validate_fix, validate_test_plan
from hands.bench.sim_bench import SimBench
from hands.circuit import Design
from hands.gadgets import registry
from hands.layout import place
from hands.llm import ScriptedClient
from hands.metrics import Metrics
from hands.rules import UnsafeDesignError
from hands.sim.build import Build
from hands.sim.human import SimHuman
from hands.vision import SimVision
from hands.wiring import build_guide

UNSAFE = {
    "name": "bad",
    "parts": {"led1": "led_red"},
    "nets": {"A": ["pico.GP15", "led1.anode"], "GND": ["led1.cathode"]},
    "pin_map": {"L": "GP15"},
}


def quiet(_):
    pass


def make_engineer(inv, script, gadget="steelers", tmp_path=None, faults=None, vision=None):
    g = registry()[gadget]
    bench = SimBench(inv, Build(), g.env)
    human = SimHuman(bench, faults or [])
    eng = Engineer(
        inv,
        ScriptedClient(script),
        bench,
        human,
        vision or SimVision(),
        Metrics(tmp_path / "m.jsonl" if tmp_path else None),
        log=quiet,
        protocol=g.protocol,
    )
    return eng, bench, human


def test_place_and_guide_refuse_unsafe_designs(inv, steelers_design):
    with pytest.raises(UnsafeDesignError):
        place(Design.from_dict(UNSAFE), inv)
    layout = place(steelers_design, inv)
    layout.gadget = Design.from_dict(UNSAFE)  # tamper after placement
    with pytest.raises(UnsafeDesignError):
        build_guide(layout, inv)
    layout.gadget = None
    with pytest.raises(UnsafeDesignError):
        build_guide(layout, inv)
    full = place(steelers_design, inv).design  # fixture-merged designs are not accepted either
    with pytest.raises(UnsafeDesignError):
        place(full, inv)


def test_llm_that_never_fixes_its_design_gets_no_wiring_and_no_power(inv, tmp_path):
    bad = json.dumps(UNSAFE)
    eng, bench, human = make_engineer(inv, {"design": [bad], "design_revise": [bad]}, tmp_path=tmp_path)
    res = eng.run("a light")
    assert not res.success and "safety rules" in res.reason
    assert eng.guide is None and human.est_seconds == 0  # no wiring step was ever performed
    assert "power_on" not in bench.calls and "flash_firmware" not in bench.calls


def test_unsafe_spec_refused_before_any_model_call(inv, tmp_path):
    eng, bench, _ = make_engineer(inv, {}, tmp_path=tmp_path)
    res = eng.run("a desk lamp that plugs into the wall outlet")
    assert not res.success and "Mains" in res.reason
    assert eng.llm.calls == [] and bench.calls == []
    events = [json.loads(x)["event"] for x in (tmp_path / "m.jsonl").read_text().splitlines()]
    assert "refused" in events


class BlindVision(SimVision):
    """A camera that always sees the same wrong thing: the wiring can never be verified."""

    def observe_wiring(self, snap, layout):
        return {"placements": {}, "wires": []}


def test_power_never_applied_without_a_passing_photo_check(inv, tmp_path):
    g = registry()["steelers"]
    eng, bench, _ = make_engineer(inv, g.scripted_client().script, tmp_path=tmp_path, vision=BlindVision())
    res = eng.run(g.spec)
    assert not res.success and "photo checks" in res.reason
    assert bench.calls.count("power_on") == 1  # only the bare-board prepare step, before any wiring
    assert bench.calls.count("read_current") == 0


def test_stage_power_on_requires_verified_wiring(inv, tmp_path):
    g = registry()["steelers"]
    eng, bench, _ = make_engineer(inv, g.scripted_client().script, tmp_path=tmp_path)
    eng.spec = g.spec
    eng.stage_design()
    eng.stage_wiring()
    with pytest.raises(AgentFailure, match="photo check"):
        eng.stage_power_on()
    with pytest.raises(AgentFailure, match="photo check"):
        eng._flash(g.firmwares[0], "firmware")
    eng.powered = True
    with pytest.raises(AgentFailure, match="while USB is plugged in"):
        eng._human_change("move a wire", None, "post_power")


def test_design_tampering_after_check_is_detected(inv, tmp_path):
    g = registry()["steelers"]
    eng, _, _ = make_engineer(inv, g.scripted_client().script, tmp_path=tmp_path)
    eng.spec = g.spec
    eng.stage_design()
    eng.design.nets["LIGHT_A"].append("pico.GP2")  # mutate the approved design in place
    with pytest.raises((AgentFailure, UnsafeDesignError)):
        eng.stage_wiring()


def test_unlinted_firmware_never_flashed(inv, tmp_path):
    g = registry()["steelers"]
    script = dict(g.scripted_client().script)
    script["firmware"] = ["```python\nimport os\nprint('READY')\n```"]
    eng, bench, _ = make_engineer(inv, script, tmp_path=tmp_path)
    res = eng.run(g.spec)
    assert not res.success and "lint" in res.reason
    from hands.diagnose import SAFE_IDLE_FW

    assert all(code == SAFE_IDLE_FW for code in bench.flashed)


def test_test_plan_validation(inv, steelers_design):
    good = registry()["steelers"].test_plan["tests"]
    assert validate_test_plan(good, steelers_design, inv) == []
    bad = [
        {"id": "a", "check": {"kind": "led", "part": "led1", "gpio": "GP14", "expect": "on"}},  # button pin
        {"id": "b", "check": {"kind": "led", "part": "led1", "gpio": "GP26", "expect": "on"}},  # not in pin_map
        {"id": "c", "setup": [{"press": "led1"}], "check": {"kind": "serial_contains", "text": "x"}},
        {"id": "d", "check": {"kind": "serial_number_range", "pattern": "(a", "min": 0, "max": 1}},
        {"id": "e", "check": {"kind": "rm -rf"}},
        {"id": "f", "check": {"kind": "current_max", "max_ma": "lots"}},
        {"id": "g", "setup": [{"wait": 9999}], "check": {"kind": "led", "part": "ghost", "expect": "on"}},
        "nope",
    ]
    errs = validate_test_plan(bad, steelers_design, inv)
    for needle in (
        "wired to a button",
        "not in the design's pin_map",
        "not a button",
        "bad regex",
        "unknown check kind",
        "max_ma",
        "wait",
        "ghost",
        "string 'id'",
    ):
        assert any(needle in e for e in errs), needle
    assert validate_test_plan([], steelers_design, inv)
    assert validate_test_plan([good[0]] * 31, steelers_design, inv)


def test_bad_test_plan_from_model_stops_run(inv, tmp_path):
    g = registry()["steelers"]
    script = dict(g.scripted_client().script)
    script["test_plan"] = [
        json.dumps({"tests": [{"id": "x", "check": {"kind": "led", "part": "led1", "gpio": "GP14", "expect": "on"}}]})
    ]
    eng, _, _ = make_engineer(inv, script, tmp_path=tmp_path)
    res = eng.run(g.spec)
    assert not res.success and "test plan invalid" in res.reason
    assert not eng.powered  # power is cut when the agent stops


def test_model_fixes_must_restore_the_plan(inv, steelers_design):
    layout = place(steelers_design, inv)
    w = layout.wires[0].id
    assert validate_fix({"action": "verify_wire", "wire": w}, layout) == {"action": "verify_wire", "wire": w}
    assert validate_fix({"action": "verify_part", "inst": "r1"}, layout)
    assert validate_fix({"action": "inspect_rails"}, layout) == {"action": "inspect_rails"}
    assert validate_fix({"action": "multi", "fixes": [{"action": "verify_wire", "wire": w}]}, layout)
    for bad in (
        {"action": "add_wire", "wire": "W99"},
        {"action": "move_wire", "wire": w, "to": "T+3"},
        {"action": "verify_wire", "wire": "W999"},
        {"action": "verify_part", "inst": "pico"},
        {"action": "multi", "fixes": [{"action": "verify_wire", "wire": w}, {"action": "remove_wire"}]},
        "rewire everything",
        None,
    ):
        assert validate_fix(bad, layout) is None, bad
    txt = fix_instruction(
        {
            "action": "multi",
            "fixes": [
                {"action": "verify_wire", "wire": w},
                {"action": "verify_part", "inst": "r1"},
                {"action": "inspect_rails"},
            ],
        },
        layout,
    )
    assert w in txt and "r1" in txt and "rail" in txt


def test_model_cannot_ask_human_for_unplanned_wiring(inv, tmp_path):
    """Servo app firmware is buggy; the model's 'fix' tries to add a wire to 5V -> rejected, no physical change."""
    g = registry()["servo"]
    script = dict(g.scripted_client().script)
    script["diagnose"] = [
        json.dumps(
            {
                "hypothesis": "needs more power",
                "action": {
                    "type": "ask_human",
                    "instruction": "add a wire from GP16 to the 5V rail",
                    "fix": {"action": "add_wire", "a": "j16", "b": "B+3"},
                },
            }
        )
    ]
    eng, bench, human = make_engineer(inv, script, gadget="servo", tmp_path=tmp_path)
    res = eng.run(g.spec)
    assert not res.success
    assert not any("GP16 to the 5V" in t for t in human.transcript)
    events = [json.loads(x) for x in (tmp_path / "m.jsonl").read_text().splitlines()]
    assert any(e.get("rejected") for e in events if e["event"] == "diagnosis")


def test_model_restoring_fix_is_applied_with_power_off(inv, tmp_path):
    g = registry()["servo"]
    script = dict(g.scripted_client().script)
    layout = place(Design.from_dict(g.designs[-1]), inv)
    w = layout.wires[-1].id
    script["diagnose"] = [
        json.dumps(
            {
                "hypothesis": "loose",
                "action": {
                    "type": "ask_human",
                    "instruction": "reseat it",
                    "fix": {"action": "verify_wire", "wire": w},
                },
            }
        ),
        json.dumps(g.diagnoses[0]),
    ]
    eng, bench, human = make_engineer(inv, script, gadget="servo", tmp_path=tmp_path)
    res = eng.run(g.spec)
    assert res.success
    i = next(i for i, t in enumerate(human.transcript) if "reseat it" in t)
    assert "UNPLUG" in human.transcript[i - 1]  # unplugged right before the change
