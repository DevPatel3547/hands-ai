"""ngspice checks: netlist/parse plumbing, gadget designs, regression tests for every rule hole the
soundness property test found, and the mna.py-vs-SPICE cross-check on the as-built board."""

from __future__ import annotations

import copy
import itertools
import json

import pytest

from hands.circuit import Design
from hands.cli import main
from hands.layout import place
from hands.rules import check_design
from hands.sim import spice
from hands.sim import spice_models as M
from hands.sim.board import PinState, SimBoard
from hands.sim.build import FAULT_KINDS, Build
from hands.wiring import build_guide

from .conftest import mk

needs_ngspice = pytest.mark.skipif(
    spice.find_ngspice() is None, reason="ngspice not installed (brew install ngspice / sudo apt install ngspice)"
)


# ------------------------------------------------------------- no ngspice needed ---


def test_led_model_hits_its_datasheet_point():
    for vf in (1.8, 2.0, 2.5):
        assert M.led_vf(0.020, vf) == pytest.approx(vf, abs=1e-9)
    # Vf falls at low current: this is why a constant Vf under-estimates LED current
    assert M.led_vf(0.005, 2.0) < 1.85
    i = M.led_current(3.3, 220, 2.0)
    assert i * 220 + M.led_vf(i, 2.0) == pytest.approx(3.3, abs=1e-6)
    assert M.led_current(0.0, 220, 2.0) == 0.0


def test_mna_led_is_fitted_to_the_spice_curve():
    knee, rd = M.led_pwl(2.1)
    for i in (0.005, 0.020):
        assert knee + i * rd == pytest.approx(M.led_vf(i, 2.1), abs=1e-9)


def test_parse_raw_ascii():
    raw = (
        "Title: t\nPlotname: Operating Point\nFlags: real\nNo. Variables: 3\nNo. Points: 1\n"
        "Variables:\n\t0\tv(a)\tvoltage\n\t1\tv(b)\tvoltage\n\t2\ti(v1)\tcurrent\n"
        "Values:\n0\t\t3.3e+00\n\t1.5e+00\n\t-2.0e-03\n"
    )
    assert spice.parse_raw(raw) == {"v(a)": 3.3, "v(b)": 1.5, "i(v1)": -0.002}


def test_corner_names():
    assert spice.corners_named("nom") == (spice.NOMINAL,)
    assert len(spice.corners_named("all")) == len(spice.CORNERS) == 6
    with pytest.raises(ValueError, match="unknown corner"):
        spice.corners_named(["nope"])


def test_missing_ngspice_is_a_clean_error(monkeypatch, capsys):
    monkeypatch.setenv(spice.NGSPICE_ENV, "/nonexistent/ngspice")
    assert spice.find_ngspice() is None
    with pytest.raises(spice.SpiceUnavailable, match="install it with"):
        spice.require_ngspice()
    assert main(["spice", "steelers"]) == 2
    err = capsys.readouterr().err
    assert "ngspice not found" in err and len(err.strip().splitlines()) == 1
    chk = spice.doctor_check()
    assert chk.status == "WARN" and chk.fix


# ------------------------------------------------------------------- ngspice ---


@needs_ngspice
def test_ngspice_error_is_reported():
    with pytest.raises(spice.SpiceError):
        spice.run_ngspice("* broken\nR1 a\n.op\n.end\n")


@needs_ngspice
@pytest.mark.parametrize("gid", ["steelers", "tempdisplay", "servo"])
def test_final_gadget_designs_pass_spice(inv, gadgets, gid):
    rep = spice.analyze_design(Design.from_dict(copy.deepcopy(gadgets[gid].designs[-1])), inv)
    assert rep.ok, rep.render()
    assert rep.exhaustive and rep.copies == rep.states_total * len(spice.CORNERS)


@needs_ngspice
def test_steelers_v1_buzzer_on_gpio_fails_in_both(inv, gadgets):
    d = Design.from_dict(copy.deepcopy(gadgets["steelers"].designs[0]))
    rep = spice.analyze_design(d, inv)
    assert not rep.ok and spice.iter_rule_ids(rep.findings) == ["gpio"]
    assert rep.metrics["gpio:GP16"].hi > 18
    assert not check_design(d, inv).ok


@needs_ngspice
def test_rules_estimates_cover_spice_on_the_hero(inv, gadgets):
    d = Design.from_dict(copy.deepcopy(gadgets["steelers"].designs[-1]))
    rep, rules = spice.analyze_design(d, inv), check_design(d, inv)
    for g, ma in rules.gpio_current_ma.items():
        if f"gpio:{g}" not in rep.metrics:  # GP26/GP27: the bench INA219 bus, counted in the total
            continue
        assert ma >= rep.metrics[f"gpio:{g}"].hi - 0.006, g  # the report rounds to 0.01 mA
    assert rules.rail_current_ma["usb_total"] >= rep.metrics["rail:usb"].hi


# Each design below is a counterexample the soundness property test found in the original rules:
# the rules accepted it and SPICE showed a hard limit exceeded. The rules now reject every one.
FINDINGS = {
    "resistor_between_gpios": (
        mk({"r1": "resistor_220"}, {"A": ["pico.GP0", "r1.1"], "B": ["pico.GP16", "r1.2"]}),
        "gpio_current",
        "gpio",
    ),
    "rail_resistor_at_tolerance": (
        mk({"r1": "resistor_220"}, {"5V": ["r1.1"], "GND": ["r1.2"]}),
        "resistor_power",
        "res",
    ),
    "led_with_parallel_resistors": (
        mk(
            {"led1": "led_green", "r1": "resistor_220", "r2": "resistor_220"},
            {"5V": ["led1.anode"], "K": ["led1.cathode", "r1.1", "r2.1"], "GND": ["r1.2", "r2.2"]},
        ),
        "led_current",
        "led",
    ),
    "two_buttons_short_the_rails": (
        mk(
            {"sw1": "button", "sw2": "button"},
            {"G": ["pico.GP16", "sw1.a", "sw2.a"], "3V3": ["sw1.b"], "GND": ["sw2.b"]},
        ),
        "shorted_part",
        "rail",
    ),
    "parallel_resistors_between_gpios": (
        mk(
            {"r1": "resistor_220", "r2": "resistor_220"},
            {"A": ["pico.GP2", "r1.1", "r2.1"], "B": ["pico.GP1", "r1.2", "r2.2"]},
        ),
        "gpio_current",
        "gpio_total",
    ),
}


@pytest.mark.parametrize("name", sorted(FINDINGS))
def test_soundness_findings_are_now_rejected_by_the_rules(inv, name):
    d, rule, _ = FINDINGS[name]
    rep = check_design(d, inv)
    assert rule in {f.rule for f in rep.errors}, rep.render()


@needs_ngspice
@pytest.mark.parametrize("name", sorted(FINDINGS))
def test_soundness_findings_really_exceed_a_limit_in_spice(inv, name):
    d, _, spice_rule = FINDINGS[name]
    rep = spice.analyze_design(d, inv)
    assert spice_rule in spice.iter_rule_ids(rep.findings), rep.render()


@needs_ngspice
@pytest.mark.parametrize("led", ["led_red", "led_yellow", "led_green"])
def test_led_vf_curve_finding(inv, led):
    """Finding 5: with a constant Vf the old rules said 6.4 mA for 220R + yellow; SPICE says 7.8 mA at
    the hot corner, because Vf drops at low current. The rules now use the same curve."""
    d = mk(
        {"led1": led, "r1": "resistor_220"},
        {"A": ["pico.GP15", "r1.1"], "B": ["r1.2", "led1.anode"], "GND": ["led1.cathode"]},
    )
    sp, rules = spice.analyze_design(d, inv), check_design(d, inv)
    old = (3.3 - inv.parts[led].get("vf_min")) / 220 * 1000
    assert sp.metrics["gpio:GP15"].hi > old + 0.5
    assert rules.gpio_current_ma["GP15"] >= sp.metrics["gpio:GP15"].hi - 0.01


@needs_ngspice
def test_cli_spice_json_and_exit_codes(capsys, tmp_path, gadgets):
    assert main(["spice", "steelers", "--corners", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["source"] == "ngspice" and out["ok"] and out["rules_ok"] and len(out["corners"]) == 6
    assert out["metrics"]["led:led1"]["max"] > out["metrics"]["led:led1"]["nominal"]
    p = tmp_path / "v1.json"
    p.write_text(json.dumps(gadgets["steelers"].designs[0]))
    assert main(["spice", str(p)]) == 1
    assert "SPICE verdict: FAIL" in capsys.readouterr().out


# --------------------------------------------------- mna.py vs SPICE, as built ---


def _built(inv, design, fault):
    from hands.bench.sim_bench import SimBench
    from hands.sim.human import SimHuman

    bench = SimBench(inv, Build())
    human = SimHuman(bench, [fault] if fault else [])
    layout = place(design, inv)
    human.begin_build(layout)
    for step in build_guide(layout, inv).steps:
        human.perform_step(step)
    human.finish_wiring()
    return bench.board, human


def _states(board: SimBoard, design: Design):
    outs = [int(g[2:]) for g in design.pin_map.values()]
    buttons = [i for i, p in board.build.placements.items() if board.inv.parts[p["type"]].kind == "button"]
    for levels in itertools.product((0, 1), repeat=len(outs)):
        for pressed in itertools.product((False, True), repeat=len(buttons)):
            board.pins = {n: PinState(mode="out", value=v) for n, v in zip(outs, levels, strict=True)}
            board.pressed = {b for b, on in zip(buttons, pressed, strict=True) if on}
            yield board.state_key()


#: Documented agreement between the piecewise-linear mna.py and ngspice (docs/digital-twin.md).
LED_TOL_MA, SHUNT_TOL_MA, NODE_TOL_V = 0.35, 1.0, 0.08


@needs_ngspice
@pytest.mark.parametrize("gid", ["steelers", "tempdisplay", "servo"])
@pytest.mark.parametrize("fault", [None, *sorted(FAULT_KINDS)])
def test_mna_agrees_with_spice_as_built(inv, gadgets, gid, fault):
    design = Design.from_dict(copy.deepcopy(gadgets[gid].designs[-1]))
    board, human = _built(inv, design, fault)
    if fault and fault in human.skipped:
        pytest.skip(f"fault {fault} does not apply to {gid}")
    board.powered = True
    worst = {"led": 0.0, "shunt": 0.0, "node": 0.0}
    n = 0
    from hands.sim.board import REG_LIMIT_A

    for key in _states(board, design):
        a = board.solve(key)
        b = spice.solve_board(board, key)
        n += 1
        reg_a, reg_b = a.source_current("reg3v3"), b.source_current("reg3v3")
        if max(reg_a, reg_b) > REG_LIMIT_A:
            # A shorted 3V3 rail: both must see the regulator overloaded (-> brownout). Past that the
            # models differ on purpose: SPICE's pad clamps follow the collapsed IOVDD, mna's pins don't.
            assert min(reg_a, reg_b) > REG_LIMIT_A, (reg_a, reg_b)
            worst["brownout"] = 1.0
            continue
        for inst, p in board.build.placements.items():
            if inv.parts[p["type"]].kind in ("led", "buzzer"):
                d = abs(a.diode_current(inst) - b.diode_current(inst)) * 1000
                assert d <= LED_TOL_MA + 0.05 * b.diode_current(inst) * 1000, (inst, key)
                worst["led"] = max(worst["led"], d)
        d = abs(a.res_current("shunt:ina") - b.res_current("shunt:ina")) * 1000
        assert d <= SHUNT_TOL_MA + 0.05 * abs(b.res_current("shunt:ina")) * 1000, key
        for gpio in design.pin_map.values():
            node = board.node_of_gpio(int(gpio[2:]))
            dv = abs(a.volt(node) - b.volt(node))
            assert dv <= NODE_TOL_V, (gpio, key, a.volt(node), b.volt(node))
            worst["node"] = max(worst["node"], dv)
    assert n > 0


@needs_ngspice
def test_simbench_can_use_ngspice_for_operating_points(inv, steelers_design):
    from hands.bench.sim_bench import SimBench
    from hands.sim.build import build_to_plan

    bench = SimBench(inv, build_to_plan(place(steelers_design, inv)), solver="spice")
    bench.power_on()
    bench.flash_firmware(
        "from machine import Pin\nimport time\nPin(16, Pin.OUT).on()\nwhile True:\n    time.sleep(1)\n"
    )
    r = bench.read_current(0.2)
    assert r.max_ma == pytest.approx(24.5, abs=1.0)  # buzzer through the PN2222A, Gummel-Poon model
    assert isinstance(bench.board.solve(), spice.SpiceSolution)
    with pytest.raises(ValueError):
        SimBoard(inv, Build(), solver="nope")
