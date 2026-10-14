import json

import pytest

from hands.agent import extract_code, lint_firmware
from hands.demo import run_sim_demo
from hands.llm import ScriptedClient


def quiet(_):
    pass


@pytest.mark.parametrize("gid", ["steelers", "tempdisplay", "servo"])
def test_clean_build_reaches_working_device(gid, tmp_path):
    res = run_sim_demo(gid, faults=[], metrics_path=tmp_path / "m.jsonl", log=quiet)
    assert res.success, res.reason
    assert res.metrics["errors_caught_pre_power"] == 0
    assert res.metrics["tests_passed"] == res.metrics["tests_total"]


def test_steelers_default_demo(tmp_path):
    log = tmp_path / "m.jsonl"
    res = run_sim_demo("steelers", metrics_path=log, log=quiet)
    m = res.metrics
    assert res.success
    assert m["design_errors_caught"] == 1  # buzzer straight on GP16 rejected by rules
    assert "q1" in res.design.parts  # ...and redesigned with a transistor
    assert m["errors_caught_pre_power"] == 1  # missing resistor seen in the photo
    assert m["errors_caught_post_power"] == 1  # loose wire found by the signal walk
    assert m["faults_remaining"] == []
    events = [json.loads(line)["event"] for line in log.read_text().splitlines()]
    assert events[0] == "run_start" and events[-1] == "run_end"
    assert "photo_check" in events and "smoke_test" in events and "diagnosis" in events


@pytest.mark.parametrize(
    "gid,faults,miss",
    [
        ("steelers", ["reversed_led"], ["reversed_led"]),  # camera misses it -> walk finds the LED
        ("steelers", ["wrong_pin"], ["wrong_pin"]),  # camera misses it -> walk finds the wire
        ("steelers", ["reversed_led", "wrong_pin"], []),  # camera catches both pre-power
        ("steelers", ["hidden_short", "loose_wire"], []),  # INA219/brownout + walk
        ("tempdisplay", ["swapped_i2c"], ["swapped_i2c"]),  # I2C EIO -> module wiring check
        ("tempdisplay", ["swapped_i2c", "hidden_short"], []),
        ("servo", ["servo_wrong_rail"], ["servo_wrong_rail"]),  # servo dead under known-good fw -> wiring
        ("servo", ["servo_wrong_rail"], []),
    ],
)
def test_faults_are_caught_and_fixed(gid, faults, miss, tmp_path):
    res = run_sim_demo(gid, faults=faults, vision_miss=miss, metrics_path=tmp_path / "m.jsonl", log=quiet)
    assert res.success, res.reason
    assert res.metrics["faults_remaining"] == []
    caught = res.metrics["errors_caught_pre_power"] + res.metrics["errors_caught_post_power"]
    assert caught >= len(faults)
    if miss:
        assert res.metrics["errors_caught_post_power"] >= 1


def test_servo_firmware_bug_fixed_by_model_after_hardware_cleared(tmp_path):
    res = run_sim_demo("servo", faults=[], metrics_path=tmp_path / "m.jsonl", log=quiet)
    assert res.success
    assert "freq(50)" in res.firmware and res.metrics["firmware_versions"] >= 2


def test_lint_firmware():
    assert lint_firmware("import os\nprint('READY')\nimport time\ntime.sleep(1)\n", {"X": "GP1"})
    errs = lint_firmware(
        "from machine import Pin\nimport time\nPin(3, Pin.OUT)\nprint('READY')\ntime.sleep(1)", {"X": "GP15"}
    )
    assert any("Pin(3)" in e for e in errs)
    assert lint_firmware("def f(:\n", {}) and "syntax" in lint_firmware("def f(:\n", {})[0]


def test_extract_code_and_scripted_client():
    assert extract_code("blah\n```python\nprint(1)\n```\n") == "print(1)\n"
    c = ScriptedClient({"design": ["a", lambda p: p.upper()]})
    assert c.complete("s", "x", stage="design") == "a"
    assert c.complete("s", "hi", stage="design") == "HI"
    assert c.complete("s", "yo", stage="design") == "YO"  # last reply repeats
    with pytest.raises(KeyError):
        c.complete("s", "x", stage="nope")
