"""Temperature / humidity readout on an OLED."""

from __future__ import annotations

from . import Gadget

SPEC = "Show the room temperature and humidity on a small screen, updating every couple of seconds."

DESIGN = {
    "name": "temp-humidity-display",
    "parts": {"dht1": "dht22", "oled1": "oled"},
    "nets": {
        "OLED_SDA": ["pico.GP4", "oled1.sda"],
        "OLED_SCL": ["pico.GP5", "oled1.scl"],
        "DHT_DATA": ["pico.GP15", "dht1.data"],
        "3V3": ["oled1.vcc", "dht1.vcc"],
        "GND": ["oled1.gnd", "dht1.gnd"],
    },
    "pin_map": {"OLED_SDA": "GP4", "OLED_SCL": "GP5", "DHT": "GP15"},
    "notes": "OLED on I2C0 (GP4/GP5, module has pull-ups). DHT22 module has its own pull-up.",
}

FIRMWARE = """# Hands firmware: temperature + humidity on SSD1306 OLED (Pico W, MicroPython)
# Needs the ssd1306 driver on the board:  mpremote mip install ssd1306
from machine import Pin, I2C
import time, dht
from ssd1306 import SSD1306_I2C

i2c = I2C(0, scl=Pin(5), sda=Pin(4), freq=400000)
print("I2C scan:", [hex(a) for a in i2c.scan()])
oled = SSD1306_I2C(128, 64, i2c)
sensor = dht.DHT22(Pin(15))
print("READY temp-display")

while True:
    try:
        sensor.measure()
        t = sensor.temperature()
        h = sensor.humidity()
        oled.fill(0)
        oled.text("Temp: %.1f C" % t, 0, 0)
        oled.text("Hum:  %.1f %%" % h, 0, 16)
        oled.show()
        print("T=%.1f H=%.1f" % (t, h))
    except OSError as e:
        print("SENSOR_ERROR", e)
        oled.fill(0)
        oled.text("sensor error", 0, 0)
        oled.show()
    time.sleep(2)
"""

TEST_PLAN = {
    "tests": [
        {
            "id": "boot",
            "setup": [],
            "check": {"kind": "serial_contains", "text": "READY", "seconds": 0.5, "since": "boot"},
        },
        {
            "id": "i2c_found",
            "setup": [],
            "check": {"kind": "serial_contains", "text": "0x3c", "seconds": 0.1, "since": "boot"},
        },
        {"id": "idle_current", "setup": [], "check": {"kind": "current_max", "max_ma": 40, "seconds": 1.0}},
        {
            "id": "temperature_plausible",
            "setup": [],
            "check": {"kind": "serial_number_range", "pattern": r"T=(-?\d+\.\d)", "min": 5, "max": 40, "seconds": 4.5},
        },
        {
            "id": "humidity_plausible",
            "setup": [],
            "check": {"kind": "serial_number_range", "pattern": r"H=(\d+\.\d)", "min": 5, "max": 95, "seconds": 2.5},
        },
        {"id": "oled_shows_temp", "setup": [], "check": {"kind": "oled_contains", "part": "oled1", "text": "Temp:"}},
        {"id": "oled_shows_humidity", "setup": [], "check": {"kind": "oled_contains", "part": "oled1", "text": "%"}},
    ]
}

GADGET = Gadget(
    id="tempdisplay",
    title="Temperature/humidity OLED",
    spec=SPEC,
    protocol="none (stand-alone)",
    designs=[DESIGN],
    firmwares=[FIRMWARE],
    test_plan=TEST_PLAN,
    default_faults=["swapped_i2c", "hidden_short"],
)
