"""Real-bench robustness without hardware: discovery, retries, tool checks, INA helper parsing, camera, doctor, bringup."""

import json
from pathlib import Path

import pytest

from hands.bench.base import BenchError, ChannelTrace, LogicCapture
from hands.bench.real_bench import (
    PortInfo,
    RealBench,
    find_bootsel_drives,
    install_hint,
    mpremote_install_cmd,
    parse_json_line,
    require_tool,
    resolve_port,
    retrying,
    split_ina_lines,
)
from hands.bringup import fixture_guide, run_bringup
from hands.doctor import MISSING, OK, Env, exit_code, parse_scan, render, run_checks

PICO = PortInfo("/dev/ttyACM0", 0x2E8A, 0x0005, "Board in FS mode")
OTHER = PortInfo("/dev/ttyUSB0", 0x10C4, 0xEA60, "CP2102")


def test_resolve_port_auto_and_errors(tmp_path):
    assert resolve_port("COM7") == "COM7"
    assert resolve_port("auto", lambda: [OTHER, PICO], [tmp_path]) == "/dev/ttyACM0"
    with pytest.raises(BenchError, match="several Picos"):
        resolve_port("auto", lambda: [PICO, PortInfo("/dev/ttyACM1", 0x2E8A, 5)], [tmp_path])
    with pytest.raises(BenchError, match="DATA micro-USB"):
        resolve_port(None, lambda: [OTHER], [tmp_path])
    drive = tmp_path / "RPI-RP2"
    drive.mkdir()
    (drive / "INFO_UF2.TXT").write_text("UF2 Bootloader")
    assert find_bootsel_drives([tmp_path]) == [drive]
    with pytest.raises(BenchError, match="BOOTSEL"):
        resolve_port("auto", list, [tmp_path])


def test_retrying_runner_retries_only_transient_errors():
    calls, sleeps = [], []

    def flaky(cmd, timeout):
        calls.append(cmd)
        if len(calls) < 3:
            raise BenchError("could not enter raw repl")
        return "ok"

    assert retrying(flaky, retries=3, sleep=sleeps.append)(["mpremote"], 5) == "ok"
    assert len(calls) == 3 and sleeps == [1.0, 2.0]

    def broken(cmd, timeout):
        calls.append(cmd)
        raise BenchError("SyntaxError in your code")

    calls.clear()
    with pytest.raises(BenchError, match="SyntaxError"):
        retrying(broken, sleep=sleeps.append)(["x"], 1)
    assert len(calls) == 1


def test_tool_checks_have_actionable_hints():
    with pytest.raises(BenchError, match="Fix:"):
        require_tool("sigrok-cli", which=lambda t: None)
    assert require_tool("mpremote", which=lambda t: "/usr/bin/mpremote") == "/usr/bin/mpremote"
    assert "sigrok" in install_hint("sigrok-cli")
    assert install_hint("unknown-tool") == "install unknown-tool"


def test_parsers():
    assert parse_json_line('Warning: blah\n{"shunt_mv": 1.0, "bus_v": 3.3}\n') == {"shunt_mv": 1.0, "bus_v": 3.3}
    with pytest.raises(BenchError):
        parse_json_line("Traceback...\nOSError: [Errno 5] EIO\n")
    text, samples, status = split_ina_lines(
        "READY x\n#INA 1.50 3.00 3.290 12\nLIGHT ON#INA 2.00 4.00 3.288 12\n\n#INA MISSING\n"
    )
    assert samples == [(1.5, 3.0, 3.29, 12), (2.0, 4.0, 3.288, 12)]
    assert "#INA" not in text and "LIGHT ON" in text and "READY x" in text
    assert status == ["#INA MISSING"]
    assert parse_scan("I2C [60, 64]\n") == [60, 64] and parse_scan("nope") == []


class FakeSerial:
    def __init__(self, chunks):
        self.chunks, self.written, self.closed = list(chunks), [], False

    def read(self, n):
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, b):
        self.written.append(b)

    def close(self):
        self.closed = True


def make_bench(tmp_path, chunks=(), runner=None, **kw):
    ser = FakeSerial(chunks)
    calls = []

    def default_runner(cmd, timeout):
        calls.append(cmd)
        return ""

    rb = RealBench(
        port="/dev/ttyACM0", runner=runner or default_runner, workdir=str(tmp_path), serial_factory=lambda p: ser, **kw
    )
    return rb, ser, calls


def test_flash_installs_helper_and_reads_current_from_helper(tmp_path, monkeypatch):
    monkeypatch.setattr("hands.bench.real_bench.time.sleep", lambda s: None)
    rb, ser, calls = make_bench(tmp_path, [b"READY steelers\r\n#INA 8.00 24.50 3.290 12\r\n"])
    out = rb.flash_firmware("print('READY')\nimport ssd1306\n")
    assert calls[0][-3:] == ["mip", "install", "ssd1306"]
    assert calls[1] == mpremote_install_cmd("/dev/ttyACM0", str(tmp_path / "app.py"), calls[1][5], calls[1][15])
    assert calls[1][5].endswith("hands_bench.py") and calls[1][15].endswith("main_shim.py")
    assert (tmp_path / "app.py").read_text().startswith("print('READY')")
    assert out["boot_output"] == "READY steelers\n"
    ser.chunks = [b"#INA 10.00 25.00 3.288 12\n", b"#INA 6.00 9.00 3.290 12\n"]
    r = rb.read_current(0.0)
    assert r.avg_ma == pytest.approx(8.0) and r.max_ma == 25.0 and r.bus_v == pytest.approx(3.29)
    rb.write_serial("SCORE 7 7")
    assert ser.written[-1] == b"SCORE 7 7\r\n"


def test_read_current_falls_back_to_exec_and_reports_missing_sensor(tmp_path, monkeypatch):
    monkeypatch.setattr("hands.bench.real_bench.time.sleep", lambda s: None)

    def runner(cmd, timeout):
        if "exec" in cmd:
            return 'noise\n{"shunt_mv": 1.5, "bus_v": 3.28}\n'
        return ""

    rb, ser, _ = make_bench(tmp_path, [b"no helper output\n"], runner=runner)
    r = rb.read_current(0.0)
    assert r.avg_ma == pytest.approx(15.0) and r.bus_v == 3.28
    rb2, _, _ = make_bench(tmp_path, [b"#INA MISSING\n"], runner=runner)
    with pytest.raises(BenchError, match="INA219 not found"):
        rb2.read_current(0.0)


def test_serial_open_failure_is_actionable(tmp_path):
    def boom(p):
        raise OSError("Permission denied")

    rb = RealBench(port="/dev/ttyACM0", workdir=str(tmp_path), serial_factory=boom, runner=lambda c, t: "")
    with pytest.raises(BenchError, match="Close other programs"):
        rb.read_serial(0.0)


def test_capture_logic_validates_channels_and_tool(tmp_path):
    rb, _, _ = make_bench(tmp_path, which=lambda t: None)
    with pytest.raises(BenchError, match="D0..D7"):
        rb.capture_logic({"D9": "a1"})
    with pytest.raises(BenchError, match="sigrok-cli is not installed"):
        rb.capture_logic({"D0": "a1"})
    csv_text = "D0\n" + "\n".join(str((i // 5) % 2) for i in range(100))
    rb2, _, _ = make_bench(tmp_path, runner=lambda c, t: csv_text, which=lambda t: "/usr/bin/" + t)
    cap = rb2.capture_logic({"D0": "j20"}, 0.1)
    assert cap.channels["D0"].hole == "j20" and cap.channels["D0"].edges == 19

    def no_dev(c, t):
        raise BenchError("sigrok-cli -d fx2lafw... failed: No devices found.")

    rb3, _, _ = make_bench(tmp_path, runner=no_dev, which=lambda t: "/usr/bin/" + t)
    with pytest.raises(BenchError, match="logic analyzer not found"):
        rb3.capture_logic({"D0": "a1"})


def test_camera_prompt_mode(tmp_path):
    photo = tmp_path / "board.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0fake")
    answers = iter(["nope.txt", f"'{photo}'"])
    rb = RealBench(port="x", camera="prompt", workdir=str(tmp_path), prompt=lambda q: next(answers))
    assert rb.camera_snapshot().path == str(photo)
    rb2 = RealBench(port="x", camera="prompt", workdir=str(tmp_path), prompt=lambda q: "")
    with pytest.raises(BenchError, match="no photo"):
        rb2.camera_snapshot()


def test_power_cut_with_and_without_uhubctl(tmp_path):
    rb, _, calls = make_bench(tmp_path, uhubctl=["-l", "1-1", "-p", "2"])
    assert "switched off" in rb.power_cut("test")
    assert calls[-1] == ["uhubctl", "-l", "1-1", "-p", "2", "-a", "off"]

    def fail(cmd, t):
        raise BenchError("no hub")

    rb2, _, _ = make_bench(tmp_path, runner=fail, uhubctl=["-a"])
    assert "uhubctl failed" in rb2.power_cut("x")


# ------------------------------------------------------------------- doctor ---


def fake_env(**kw):
    defaults = dict(
        which=lambda t: f"/usr/bin/{t}",
        has_module=lambda m: True,
        run=lambda cmd, t: (
            "micropython 1.24.1 Raspberry Pi Pico W with RP2040\n"
            if "sys" in cmd[-1]
            else "I2C [64]\n"
            if "SoftI2C" in cmd[-1]
            else "fx2lafw - Saleae Logic"
        ),
        lister=lambda: [PICO],
        http_get=lambda url, t: json.dumps({"data": [{"id": "qwen2.5-coder:7b"}]}).encode(),
        environ={"ANTHROPIC_API_KEY": "x"},
        python=(3, 12),
    )
    defaults.update(kw)
    return Env(**defaults)


def test_doctor_all_ok():
    checks = run_checks(fake_env(), mode="real")
    assert all(c.status == OK for c in checks if c.required)
    assert exit_code(checks) == 0
    text = render(checks)
    assert "All required checks passed" in text and "qwen2.5-coder" in text


def test_doctor_reports_exactly_whats_missing():
    def boom(url, t):
        raise OSError("refused")

    env = fake_env(
        which=lambda t: None, has_module=lambda m: m == "yaml", lister=list, http_get=boom, environ={}, python=(3, 10)
    )
    checks = run_checks(env, mode="real")
    missing = {c.name for c in checks if c.status == MISSING}
    assert {"Python >= 3.11", "pyserial", "mpremote", "Pico W on USB"} <= missing
    assert exit_code(checks) == 1
    text = render(checks)
    assert "pip install mpremote" in text and "pip install pyserial" in text and "ollama" in text
    sim = run_checks(env, mode="sim")
    assert {c.group for c in sim} <= {"core", "model", "optional"}


def test_doctor_detects_missing_micropython_and_ina():
    env = fake_env(run=lambda cmd, t: "I2C []\n" if "SoftI2C" in cmd[-1] else "circuitpython 9\n")
    checks = {c.name: c for c in run_checks(env, mode="real")}
    assert checks["MicroPython on the Pico"].status == MISSING
    assert checks["INA219 at 0x40 on GP26/GP27"].status == "WARN"


# ------------------------------------------------------------------ bringup ---


class FakeBench:
    port = "/dev/ttyACM0"

    def __init__(self, current=(2.0, 3.0, 3.29), edges=8, info="micropython 1.24.1 rp2"):
        self.current, self.edges, self.info = current, edges, info
        self.flashed, self.cuts = [], []

    def device_info(self):
        return self.info

    def blink_onboard_led(self, n):
        return "BLINK OK"

    def flash_firmware(self, code, extra_files=None):
        self.flashed.append(code)
        return {"ok": True}

    def power_cut(self, reason):
        self.cuts.append(reason)
        return "UNPLUG"

    def power_on(self):
        return "plug"

    def read_current(self, s=0.0):
        from hands.bench.base import CurrentReading

        return CurrentReading(*self.current)

    def capture_logic(self, probes, seconds=1.0):
        return LogicCapture(seconds, {"D0": ChannelTrace(probes["D0"], self.edges, 0.5, 0)})

    def camera_snapshot(self):
        from hands.bench.base import Snapshot

        return Snapshot(0.0, "/tmp/x.jpg")


def test_bringup_happy_path(inv, tmp_path):
    from hands.metrics import Metrics

    bench = FakeBench()
    out = []
    res = run_bringup(bench, ask=lambda q: "y", say=out.append, inv=inv, metrics=Metrics(tmp_path / "b.jsonl"))
    assert [r.status for r in res] == ["PASS"] * 5
    assert "toggle GP15" in bench.flashed[-2] and "safe-idle" in bench.flashed[-1]
    assert any("PASSED" in line for line in out)
    assert len(fixture_guide(inv).steps) > 5


def test_bringup_stops_on_bad_ina_reading(inv):
    bench = FakeBench(current=(180.0, 250.0, 2.1))
    out = []
    res = run_bringup(bench, ask=lambda q: "y", say=out.append, inv=inv)
    assert res[-1].name == "INA219 current sensor" and res[-1].status == "FAIL"
    assert "INA219 reading out of range" in bench.cuts
    assert any("UNPLUG NOW" in line for line in out)


def test_bringup_without_micropython_stops_early(inv):
    res = run_bringup(FakeBench(info="circuitpython"), ask=lambda q: "y", say=lambda s: None, inv=inv)
    assert len(res) == 1 and res[0].status == "FAIL" and "micropython.org" in res[0].fix


def test_bringup_skips(inv):
    res = run_bringup(FakeBench(edges=0), ask=lambda q: "n", say=lambda s: None, inv=inv, skip={"ina", "camera"})
    st = {r.name: r.status for r in res}
    assert st["on-board LED blink"] == "FAIL" and st["INA219 current sensor"] == "SKIP"
    assert st["logic analyzer self-test"] == "FAIL" and st["camera"] == "SKIP"


def test_firmware_helper_files_are_valid_micropython_syntax():
    import ast

    from hands.firmware import BENCH_HELPER, MAIN_SHIM

    for f in (BENCH_HELPER, MAIN_SHIM):
        ast.parse(Path(f).read_text())
    assert "SoftI2C(sda=Pin(26), scl=Pin(27)" in BENCH_HELPER.read_text()
