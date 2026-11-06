"""The agent's optional SPICE gate: runs after the rules, fails closed, and is labelled in metrics."""

from __future__ import annotations

import json
import types

import pytest

from hands.demo import run_sim_demo
from hands.sim import spice


def events(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


@pytest.mark.skipif(spice.find_ngspice() is None, reason="ngspice not installed")
def test_demo_with_real_spice(tmp_path):
    lines: list[str] = []
    res = run_sim_demo("steelers", metrics_path=tmp_path / "m.jsonl", spice="require", log=lines.append)
    assert res.success
    ev = [e for e in events(tmp_path / "m.jsonl") if e["event"] == "spice_check"]
    assert len(ev) == 1 and ev[0]["ok"] is True and ev[0]["points"] > 100  # only the approved v2 design
    assert any("SPICE check (ngspice" in x and "PASS" in x for x in lines)


def test_spice_violation_sends_design_back(tmp_path, monkeypatch):
    calls = []

    def fake(design, inv, corners="all"):
        calls.append(design.name)
        bad = len(calls) == 1
        f = spice.SpiceFinding("gpio_current", "error", "GP15 reaches 13.1 mA at hot_3.4_4.75")
        return types.SimpleNamespace(
            ok=not bad,
            violations=[f] if bad else [],
            engine="fake",
            copies=1,
            corners=["nom"],
            states_run=1,
            metrics={},
            seconds=0.0,
        )

    monkeypatch.setattr(spice, "find_ngspice", lambda: "/usr/bin/ngspice")
    monkeypatch.setattr(spice, "analyze_design", fake)
    lines: list[str] = []
    res = run_sim_demo("steelers", metrics_path=tmp_path / "m.jsonl", spice="auto", log=lines.append)
    assert res.success and len(calls) == 2  # v2 failed SPICE, was revised (scripted repeats v2), then passed
    ev = [e for e in events(tmp_path / "m.jsonl") if e["event"] == "spice_check"]
    assert [e["ok"] for e in ev] == [False, True]
    assert res.metrics["design_errors_caught"] == 2  # 1 rule error + 1 SPICE error


def test_spice_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(spice, "find_ngspice", lambda: None)
    res = run_sim_demo("servo", faults=[], metrics_path=tmp_path / "m.jsonl", spice="auto", log=lambda s: None)
    assert res.success
    assert any(e.get("skipped") for e in events(tmp_path / "m.jsonl") if e["event"] == "spice_check")
    res = run_sim_demo("servo", faults=[], metrics_path=tmp_path / "m2.jsonl", spice="require", log=lambda s: None)
    assert not res.success and "ngspice is missing" in res.reason
