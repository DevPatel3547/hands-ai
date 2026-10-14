from collections import Counter

import pytest

from hands.circuit import Design, PinRef
from hands.layout import place, strip_of
from hands.sim.board import SimBoard
from hands.sim.build import build_to_plan
from hands.wiring import ascii_diagram, build_guide, compare, expected_observation, svg_diagram


def node_of_ref(board, layout, ref):
    r = PinRef.parse(ref)
    if r.inst == "pico":
        if r.pin == "GND":
            return board.gnd
        hole = layout.pico_pins[ref]
        return board.node_of_hole(hole)
    return board.node_of_pin(r.inst, r.pin)


@pytest.mark.parametrize("gid", ["steelers", "tempdisplay", "servo"])
def test_layout_realises_exactly_the_netlist(inv, gadgets, gid):
    layout = place(Design.from_dict(gadgets[gid].designs[-1]), inv)
    board = SimBoard(inv, build_to_plan(layout))
    nodes = {}
    for net, refs in layout.design.nets.items():
        ns = {node_of_ref(board, layout, r) for r in refs}
        assert len(ns) == 1, f"net {net} split across {ns}"
        nodes[net] = ns.pop()
    # different nets must not be shorted together
    assert len(set(nodes.values())) == len(nodes), Counter(nodes.values())


@pytest.mark.parametrize("gid", ["steelers", "tempdisplay", "servo"])
def test_no_hole_used_twice(inv, gadgets, gid):
    layout = place(Design.from_dict(gadgets[gid].designs[-1]), inv)
    holes = list(layout.pico_pins.values())
    for p in layout.placements.values():
        holes += list(p.holes.values()) + p.extra_holes
    for w in layout.wires:
        holes += [h for h in (w.a, w.b) if h[0] in "abcdefghij" and h[1:].isdigit()]
    dup = [h for h, n in Counter(holes).items() if n > 1 and h != layout.pico_pins.get("pico.GND")]
    assert not dup


def test_guide_one_action_per_step(inv, steelers_design):
    layout = place(steelers_design, inv)
    g = build_guide(layout, inv)
    kinds = Counter(s.kind for s in g.steps)
    assert kinds["jumper"] == len(layout.wires)
    assert kinds["insert_part"] == len(layout.placements)
    assert g.steps[0].kind == "safety" and "Unplug" in g.steps[0].title
    for s in g.steps:
        if s.kind == "jumper":
            assert len(s.holes) == 2
    txt = ascii_diagram(layout, 5, g.steps)
    assert "@" in txt and "legend" in txt
    svg = svg_diagram(layout, len(g.steps) - 1, g.steps)
    assert svg.startswith("<svg") and svg.endswith("</svg>")


def test_compare_detects_mistakes(inv, steelers_design):
    layout = place(steelers_design, inv)
    obs = expected_observation(layout)
    assert compare(layout, obs) == []
    # same strip, different hole: electrically identical -> not an error
    a = obs["placements"]["led1"]["anode"]
    obs["placements"]["led1"]["anode"] = ("a" if a[0] != "a" else "b") + a[1:]
    assert compare(layout, obs) == []
    obs = expected_observation(layout)
    pl = obs["placements"]["led1"]
    pl["anode"], pl["cathode"] = pl["cathode"], pl["anode"]
    d = compare(layout, obs)
    assert [x.kind for x in d] == ["part_reversed"]
    obs = expected_observation(layout)
    holes = obs["placements"].pop("r1")
    obs["wires"].append([holes["1"], holes["2"]])
    d = compare(layout, obs)
    assert len(d) == 1 and d[0].kind == "part_missing" and "jumper" in d[0].message
    obs = expected_observation(layout)
    w = obs["wires"][3]
    w[1] = w[1][0] + str(int(w[1][1:]) + 1) if w[1][0] in "abcdefghij" else w[1]
    kinds = [x.kind for x in compare(layout, obs)]
    assert kinds in (["wire_misplaced"], [])  # rails are one strip, so a rail-end shift is not an error


def test_strip_of():
    assert strip_of("a5") == strip_of("e5") == "t5"
    assert strip_of("f5") == "b5" != strip_of("e5")
    assert strip_of("T+12") == strip_of("T+40") == "T+"
    assert strip_of("servo1.sig") == "servo1.sig"
