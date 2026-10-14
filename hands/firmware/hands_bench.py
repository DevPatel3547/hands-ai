# hands_bench.py -- installed on the Pico by Hands (do not edit).
#
# Samples the bench INA219 current sensor every 20 ms from a soft timer and prints one
# summary line every ~250 ms over USB serial:
#
#     #INA <avg_mA> <max_mA> <bus_V> <samples>
#
# The host strips these lines out of the program's own output. Because it runs on a timer
# next to your program, current can be measured *while the gadget is doing something*
# (e.g. the buzzer sounding), without stopping the program.
#
# The INA219 is on its own SoftI2C bus (GP26 SDA / GP27 SCL), so it never fights the
# program for a hardware I2C peripheral. Power-on default config (0x399F: 32 V range,
# PGA /8 = +/-320 mV, 12-bit, continuous) is used; shunt LSB 10 uV, bus LSB 4 mV.
from machine import Pin, SoftI2C, Timer
import struct

ADDR = 0x40
SHUNT_OHMS = 0.1
SAMPLE_MS = 20
REPORT_EVERY = 12

_i2c = None
_timer = None
_acc = [0.0, -1e9, 0, 0.0, 0]  # sum mA, max mA, samples, last bus V, ticks


def _sample(_t):
    try:
        shunt = struct.unpack(">h", _i2c.readfrom_mem(ADDR, 1, 2))[0] * 0.01  # mV
        bus = (struct.unpack(">H", _i2c.readfrom_mem(ADDR, 2, 2))[0] >> 3) * 0.004  # V
    except OSError:
        _acc[4] += 1
        if _acc[4] >= REPORT_EVERY:
            print("#INA ERR")
            _acc[4] = 0
        return
    ma = shunt / SHUNT_OHMS
    _acc[0] += ma
    if ma > _acc[1]:
        _acc[1] = ma
    _acc[2] += 1
    _acc[3] = bus
    _acc[4] += 1
    if _acc[4] >= REPORT_EVERY and _acc[2]:
        print("#INA %.2f %.2f %.3f %d" % (_acc[0] / _acc[2], _acc[1], _acc[3], _acc[2]))
        _acc[0], _acc[1], _acc[2], _acc[4] = 0.0, -1e9, 0, 0


def start():
    global _i2c, _timer
    _i2c = SoftI2C(sda=Pin(26), scl=Pin(27), freq=100000)
    if ADDR not in _i2c.scan():
        print("#INA MISSING")
        return
    _timer = Timer(period=SAMPLE_MS, mode=Timer.PERIODIC, callback=_sample)


def stop():
    if _timer is not None:
        _timer.deinit()
