import pytest

from hands.bench.base import BenchError
from hands.bench.sim_bench import SimBench
from hands.circuit import Design
from hands.layout import place
from hands.sim import mna
from hands.sim.board import SimBoard
from hands.sim.build import build_to_plan
from hands.sim.runtime import FirmwareRunner, SerialPort


def test_mna_led_current():
    c = mna.Circuit(ground="0")
    c.sources.append(mna.Source("g", 3.3, 40, "gp"))
    c.res.append(mna.Res("g", "a", 220, "r"))
    c.diodes.append(mna.Diode("a", "0", 2.0, 15, "led"))
    s = mna.solve(c)
    assert s.diode_current("led") * 1000 == pytest.approx(1.3 / 275 * 1000, rel=0.01)
    c.diodes[0] = mna.Diode("0", "a", 2.0, 15, "led")  # reversed
    assert mna.solve(c).diode_current("led") == 0.0


def test_mna_npn_switch():
    c = mna.Circuit(ground="0")
    c.sources += [mna.Source("v", 3.3, 0.3, "reg"), mna.Source("g", 3.3, 40, "gp")]
    c.res.append(mna.Res("g", "b", 1000))
    c.npns.append(mna.NPN("b", "c", "0", "q"))
    c.diodes.append(mna.Diode("v", "c", 0.3, 120, "bz"))
    assert mna.solve(c).diode_current("bz") * 1000 == pytest.approx(24.5, abs=1.5)
    c.sources[1] = mna.Source("g", 0.0, 40, "gp")
    assert mna.solve(c).diode_current("bz") == 0.0


@pytest.fixture
def bench(inv, steelers_design):
    b = SimBench(inv, build_to_plan(place(steelers_design, inv)))
    b.power_on()
    return b


BLINK = """from machine import Pin
import time
p = Pin(15, Pin.OUT)
print("READY")
while True:
    p.toggle()
    time.sleep(0.5)
"""


def test_virtual_time_and_logic_capture(bench):
    import time

    t0 = time.time()
    out = bench.flash_firmware(BLINK)
    assert "READY" in out["boot_output"]
    cap = bench.capture_logic({"D0": "j20"}, 10.0)  # 10 s of virtual time
    assert cap.channels["D0"].edges == 20
    assert 0.4 < cap.channels["D0"].high_fraction < 0.6
    assert time.time() - t0 < 3


def test_firmware_crash_shows_traceback(bench):
    bench.flash_firmware('print("READY")\nx = 1 / 0\n')
    bench.read_serial(0.2)
    assert "Traceback" in bench.serial.all_text() and "ZeroDivisionError" in bench.serial.all_text()


def test_firmware_import_allowlist(bench):
    bench.flash_firmware("import os\nprint('READY')\n")
    assert "ImportError" in bench.serial.all_text()


def test_busy_loop_detected(inv, steelers_design):
    board = SimBoard(inv, build_to_plan(place(steelers_design, inv)))
    board.powered = True
    r = FirmwareRunner(board, "x = 0\nwhile True:\n    x += 1\n", SerialPort())
    r.yield_timeout = 0.3
    r.start()
    assert r.hung


def test_ina_current_and_buzzer(bench):
    bench.flash_firmware(
        "from machine import Pin\nimport time\nPin(16, Pin.OUT).on()\nprint('READY')\nwhile True:\n    time.sleep(1)\n"
    )
    r = bench.read_current(0.5)
    assert r.max_ma == pytest.approx(24.5, abs=2)  # buzzer through the transistor, from the 3V3 rail
    assert r.bus_v == pytest.approx(3.29, abs=0.02)


def test_hidden_short_browns_out(inv, steelers_design):
    b = build_to_plan(place(steelers_design, inv))
    b.hidden_shorts.append(("T+", "T-"))
    bench = SimBench(inv, b)
    bench.power_on()
    with pytest.raises(BenchError, match="not responding"):
        bench.read_current()
    assert bench.board.browned_out


def test_ina_unwired_reports_error(inv, steelers_design):
    layout = place(steelers_design, inv)
    b = build_to_plan(layout)
    sda = next(w for w in layout.wires if w.net == "INA_SDA")
    b.wires = [w for w in b.wires if w.id != sda.id]
    b.touch()
    bench = SimBench(inv, b)
    bench.power_on()
    with pytest.raises(BenchError, match="INA219 not found"):
        bench.read_current()


def test_i2c_and_oled_and_dht(inv, gadgets):
    g = gadgets["tempdisplay"]
    bench = SimBench(inv, build_to_plan(place(Design.from_dict(g.designs[-1]), inv)), g.env)
    bench.power_on()
    bench.flash_firmware(g.firmwares[0])
    out = bench.read_serial(2.5)
    assert "0x3c" in bench.serial.all_text() and "T=22" in out
    assert bench.camera_snapshot().scene["oled"]["oled1"][0].startswith("Temp:")


def test_unpowered_board_errors(inv, steelers_design):
    bench = SimBench(inv, build_to_plan(place(steelers_design, inv)))
    with pytest.raises(BenchError):
        bench.flash_firmware(BLINK)
