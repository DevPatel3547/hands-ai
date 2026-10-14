"""HERO gadget: desk light + buzzer that goes off when the Steelers score."""

from __future__ import annotations

from ..scores import demo_game, run_bridge
from . import Gadget

SPEC = (
    "A desk light with a button to turn it on and off, plus a buzzer and light show "
    "that goes off whenever the Pittsburgh Steelers score."
)

# v1: what a model plausibly proposes first -- buzzer straight on a GPIO pin.
DESIGN_V1 = {
    "name": "steelers-desk-light",
    "parts": {"led1": "led_yellow", "r1": "resistor_220", "bz1": "buzzer_active", "sw1": "button"},
    "nets": {
        "LIGHT": ["pico.GP15", "r1.1"],
        "LIGHT_A": ["r1.2", "led1.anode"],
        "BUZZ": ["pico.GP16", "bz1.plus"],
        "BTN": ["pico.GP14", "sw1.a"],
        "GND": ["led1.cathode", "bz1.minus", "sw1.b"],
    },
    "pin_map": {"LIGHT": "GP15", "BUZZER": "GP16", "BUTTON": "GP14"},
    "notes": "LED via 220R; buzzer driven from GP16; button to GND with internal pull-up.",
}

# v2: after the rule checker flags ~25 mA on GP16 -> NPN low-side switch.
DESIGN_V2 = {
    "name": "steelers-desk-light",
    "parts": {
        "led1": "led_yellow",
        "r1": "resistor_220",
        "q1": "npn",
        "r2": "resistor_1k",
        "bz1": "buzzer_active",
        "sw1": "button",
    },
    "nets": {
        "LIGHT": ["pico.GP15", "r1.1"],
        "LIGHT_A": ["r1.2", "led1.anode"],
        "BUZ_DRV": ["pico.GP16", "r2.1"],
        "BUZ_B": ["r2.2", "q1.b"],
        "BUZ_C": ["q1.c", "bz1.minus"],
        "3V3": ["bz1.plus"],
        "BTN": ["pico.GP14", "sw1.a"],
        "GND": ["led1.cathode", "q1.e", "sw1.b"],
    },
    "pin_map": {"LIGHT": "GP15", "BUZZER": "GP16", "BUTTON": "GP14"},
    "notes": "Buzzer (~25 mA) switched by PN2222A on the low side, 1k base resistor (~2.6 mA from GP16).",
}

FIRMWARE = """# Hands firmware: Steelers desk light + score alarm (Raspberry Pi Pico W, MicroPython)
from machine import Pin
import time, sys, select

LIGHT = Pin(15, Pin.OUT)
BUZZER = Pin(16, Pin.OUT)
BUTTON = Pin(14, Pin.IN, Pin.PULL_UP)

# (buzz seconds, pause seconds) per beep; bigger scores get a longer show
PATTERNS = {6: 3, 7: 4, 8: 4, 3: 2, 2: 2, 1: 1}

light_on = True
LIGHT.value(1)
BUZZER.value(0)
poll = select.poll()
poll.register(sys.stdin, select.POLLIN)
print("READY steelers-desk-light")


def celebrate(points, total):
    print("CELEBRATE", points, total)
    for _ in range(PATTERNS.get(points, 2)):
        BUZZER.on()
        LIGHT.on()
        time.sleep(0.4)
        BUZZER.off()
        LIGHT.off()
        time.sleep(0.2)
    LIGHT.value(1 if light_on else 0)


last_btn = 1
while True:
    if poll.poll(0):
        parts = sys.stdin.readline().strip().split()
        if len(parts) == 3 and parts[0] == "SCORE":
            celebrate(int(parts[1]), int(parts[2]))
        elif parts and parts[0] == "PING":
            print("PONG")
    b = BUTTON.value()
    if b == 0 and last_btn == 1:
        light_on = not light_on
        LIGHT.value(1 if light_on else 0)
        print("LIGHT", "ON" if light_on else "OFF")
    last_btn = b
    time.sleep(0.02)
"""

TEST_PLAN = {
    "tests": [
        {
            "id": "boot",
            "description": "firmware starts",
            "setup": [],
            "check": {"kind": "serial_contains", "text": "READY", "seconds": 0.5, "since": "boot"},
        },
        {
            "id": "idle_current",
            "description": "3V3 rail idle draw is small (buzzer off)",
            "setup": [],
            "check": {"kind": "current_max", "max_ma": 15, "seconds": 1.0},
        },
        {
            "id": "light_on",
            "description": "desk light is on at boot",
            "setup": [],
            "check": {"kind": "led", "part": "led1", "gpio": "GP15", "expect": "on", "seconds": 0.3},
        },
        {
            "id": "button_off",
            "description": "button turns the light off",
            "setup": [{"press": "sw1", "hold_s": 0.2}],
            "check": {"kind": "led", "part": "led1", "gpio": "GP15", "expect": "off", "seconds": 0.3},
        },
        {
            "id": "button_on",
            "description": "button turns it back on",
            "setup": [{"press": "sw1", "hold_s": 0.2}],
            "check": {"kind": "serial_contains", "text": "LIGHT ON", "seconds": 0.3},
        },
        {
            "id": "touchdown_buzzer",
            "description": "a 7-point score pulses the buzzer driver",
            "setup": [{"send": "SCORE 7 7"}],
            "check": {
                "kind": "logic_toggles",
                "part": "bz1",
                "pin": "minus",
                "gpio": "GP16",
                "min_edges": 6,
                "seconds": 3.0,
            },
        },
        {
            "id": "touchdown_current",
            "description": "buzzer really draws current (INA219 sees it)",
            "setup": [{"send": "SCORE 3 10"}],
            "check": {"kind": "current_min", "min_ma": 15, "seconds": 1.5},
        },
        {
            "id": "light_show",
            "description": "light flashes during the celebration",
            "setup": [{"send": "SCORE 7 17"}],
            "check": {"kind": "led", "part": "led1", "gpio": "GP15", "expect": "blinking", "seconds": 2.0},
        },
        {
            "id": "celebrate_log",
            "description": "celebration reported on serial",
            "setup": [],
            "check": {"kind": "serial_contains", "text": "CELEBRATE 7 17", "seconds": 1.0, "since": "boot"},
        },
    ]
}


def after_demo(bench, log, layout):
    """Replay a mock game through the bridge into the (simulated) board."""
    from ..diagnose import probe_hole
    from ..layout import strip_of

    log("\n== Live-game replay (MockFeed -> bridge -> USB serial -> Pico)")
    buzz = []
    hole = probe_hole(layout, strip_of(layout.hole_of("bz1.minus")))

    def send(cmd):
        bench.write_serial(cmd)
        cap = bench.capture_logic({"D0": hole}, 3.0)  # watch the buzzer driver while it celebrates
        buzz.append(cap.channels["D0"].edges)

    sent = run_bridge(demo_game(), send, polls=9, interval_s=0, sleep=lambda s: None, log=log)
    log(f"   bridge sent {sent}; buzzer-driver edges per celebration (logic analyzer at {hole}): {buzz}")
    out = bench.read_serial(0.5)
    log("   serial: " + " | ".join(line for line in out.splitlines() if line.startswith("CELEBRATE")))


GADGET = Gadget(
    id="steelers",
    title="Steelers score alarm (hero)",
    spec=SPEC,
    protocol="Host bridge sends 'SCORE <points> <new_total>' when PIT scores, and 'PING' (reply 'PONG').",
    designs=[DESIGN_V1, DESIGN_V2],
    firmwares=[FIRMWARE],
    test_plan=TEST_PLAN,
    default_faults=["missing_resistor", "loose_wire"],
    after_demo=after_demo,
)
