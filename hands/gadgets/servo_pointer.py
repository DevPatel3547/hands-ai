"""Servo needle that points at a 0-100 value sent from the computer."""

from __future__ import annotations

from . import Gadget

SPEC = (
    "A servo pointer gauge: the computer sends a value from 0 to 100 and a needle on a servo "
    "points to it (0 = far left, 100 = far right)."
)

DESIGN_V1 = {
    "name": "servo-gauge",
    "parts": {"servo1": "servo"},
    "nets": {"SERVO_SIG": ["pico.GP16", "servo1.sig"], "3V3": ["servo1.vplus"], "GND": ["servo1.gnd"]},
    "pin_map": {"SERVO": "GP16"},
    "notes": "Servo on 3V3 to keep everything on one rail.",
}
DESIGN_V2 = {
    "name": "servo-gauge",
    "parts": {"servo1": "servo"},
    "nets": {"SERVO_SIG": ["pico.GP16", "servo1.sig"], "5V": ["servo1.vplus"], "GND": ["servo1.gnd"]},
    "pin_map": {"SERVO": "GP16"},
    "notes": "Servo powered from USB 5V (VBUS) rail; 3.3 V signal from GP16 is fine for an SG90.",
}

_FW = """# Hands firmware: servo pointer gauge (Pico W, MicroPython)
from machine import Pin, PWM
import time, sys, select

servo = PWM(Pin(16))
servo.freq({freq})


def angle_to_u16(deg):
    pulse_ms = 0.5 + 2.0 * deg / 180      # SG90: ~0.5 ms = 0 deg, ~2.5 ms = 180 deg
    return int(pulse_ms / 20 * 65535)      # 20 ms period at 50 Hz


def show(value):
    value = max(0, min(100, value))
    deg = value * 1.8
    servo.duty_u16(angle_to_u16(deg))
    print("VALUE", value, "DEG", round(deg))


show(0)
poll = select.poll()
poll.register(sys.stdin, select.POLLIN)
print("READY servo-gauge")
while True:
    if poll.poll(0):
        parts = sys.stdin.readline().strip().split()
        if len(parts) == 2 and parts[0] == "V":
            show(float(parts[1]))
    time.sleep(0.02)
"""
FIRMWARE_BUGGY = _FW.replace("{freq}", "500")  # plausible slip: 500 Hz instead of 50 Hz
FIRMWARE_FIXED = _FW.replace("{freq}", "50")

TEST_PLAN = {
    "tests": [
        {
            "id": "boot",
            "setup": [],
            "check": {"kind": "serial_contains", "text": "READY", "seconds": 0.5, "since": "boot"},
        },
        {"id": "rail_current", "setup": [], "check": {"kind": "current_max", "max_ma": 30, "seconds": 0.5}},
        {
            "id": "point_mid",
            "setup": [{"send": "V 50"}],
            "check": {"kind": "servo_angle", "part": "servo1", "gpio": "GP16", "deg": 90, "tol": 10, "seconds": 1.0},
        },
        {
            "id": "point_max",
            "setup": [{"send": "V 100"}],
            "check": {"kind": "servo_angle", "part": "servo1", "gpio": "GP16", "deg": 180, "tol": 10, "seconds": 1.0},
        },
        {
            "id": "point_min",
            "setup": [{"send": "V 0"}],
            "check": {"kind": "servo_angle", "part": "servo1", "gpio": "GP16", "deg": 0, "tol": 10, "seconds": 1.0},
        },
        {
            "id": "rail_current_moving",
            "description": "servo must not load the measured 3V3 rail",
            "setup": [{"send": "V 75"}],
            "check": {"kind": "current_max", "max_ma": 30, "seconds": 0.5},
        },
    ]
}

DIAGNOSES = [
    {
        "hypothesis": "Wiring is fine (servo follows the known-good 50 Hz diagnostic firmware), but the "
        "application firmware sets PWM to 500 Hz; an SG90 needs 50 Hz pulses of 0.5-2.5 ms.",
        "action": {"type": "firmware_patch", "code": FIRMWARE_FIXED},
    }
]

GADGET = Gadget(
    id="servo",
    title="Servo pointer gauge",
    spec=SPEC,
    protocol="Host sends 'V <0-100>' lines.",
    designs=[DESIGN_V1, DESIGN_V2],
    firmwares=[FIRMWARE_BUGGY],
    test_plan=TEST_PLAN,
    diagnoses=DIAGNOSES,
    default_faults=["servo_wrong_rail"],
)
