"""Regression tests for the safety review: rule-checker bypasses, hard limits, spec screening, firmware lint."""

import copy

import pytest
import yaml

from hands.circuit import Design, DesignError
from hands.fwlint import lint_firmware
from hands.inventory import DEFAULT_INVENTORY, InventoryError, load_inventory
from hands.rules import RULE_IDS, check_design
from hands.safety import HARD_LIMITS, SafetyRefusal, effective_limits, require_safe_text, screen_text

from .conftest import mk

LED_OK = {
    "parts": {"led1": "led_red", "r1": "resistor_220"},
    "nets": {"A": ["pico.GP15", "r1.1"], "B": ["r1.2", "led1.anode"], "GND": ["led1.cathode"]},
}


def hit(rep, severity="error"):
    return {f.rule for f in rep.findings if f.severity == severity}


def custom_inv(tmp_path, mutate):
    data = yaml.safe_load(DEFAULT_INVENTORY.read_text())
    mutate(data)
    p = tmp_path / "inv.yaml"
    p.write_text(yaml.safe_dump(data))
    return load_inventory(p)


# ------------------------------------------------------------ aliasing / bypass ---


@pytest.mark.parametrize(
    "pin", ["pico.3V3", "pico.VBUS", "pico.VSYS", "pico.RUN", "pico.3V3_EN", "pico.ADC_VREF", "pico.AGND", "pico.GND"]
)
def test_pico_power_and_control_pins_cannot_be_used_directly(inv, pin):
    rep = check_design(mk({"r1": "resistor_1k"}, {"X": [pin, "r1.1"], "GND": ["r1.2"]}), inv)
    assert not rep.ok and "structure" in hit(rep)
    assert any("bench fixture" in f.message for f in rep.errors)


@pytest.mark.parametrize("net", ["V3_SRC", "INA_SDA", "INA_SCL"])
def test_fixture_nets_are_reserved(inv, net):
    d = copy.deepcopy(LED_OK)
    d["nets"][net] = d["nets"].pop("GND")
    rep = check_design(mk(**d), inv)
    assert "structure" in hit(rep)


@pytest.mark.parametrize("net", ["Gnd", "gnd", "VCC", "vbus", "3v3", "+5V", "ground"])
def test_power_alias_net_names_rejected(inv, net):
    d = copy.deepcopy(LED_OK)
    d["nets"][net] = d["nets"].pop("GND")
    rep = check_design(mk(**d), inv)
    assert any("looks like a power rail" in f.message for f in rep.errors)


@pytest.mark.parametrize("ptype", ["pico", "ina219", "breadboard", "jumpers"])
def test_fixture_and_tool_parts_not_usable_in_designs(inv, ptype):
    rep = check_design(mk({"x1": ptype}, {}), inv)
    assert "structure" in hit(rep)


def test_unknown_kind_added_to_yaml_is_refused(inv, tmp_path):
    def add_relay(d):
        d["parts"]["relay5v"] = {
            "kind": "relay",
            "name": "5V relay module",
            "qty": 1,
            "mount": "header",
            "pins": [{"name": "in", "role": "signal"}, {"name": "gnd", "role": "gnd"}],
        }

    inv2 = custom_inv(tmp_path, add_relay)
    rep = check_design(mk({"k1": "relay5v"}, {"S": ["pico.GP2", "k1.in"], "GND": ["k1.gnd"]}), inv2)
    assert any("cannot be used in a design" in f.message for f in rep.errors)


@pytest.mark.parametrize(
    "bad",
    [
        {"parts": [], "nets": {}},
        {"parts": {}, "nets": {"A": "pico.GP2"}},
        {"parts": {"r1": 5}, "nets": {}},
        {"parts": {}, "nets": {}, "pin_map": []},
        [],
    ],
)
def test_design_json_strictly_typed(bad):
    with pytest.raises(DesignError):
        Design.from_dict(bad)


@pytest.mark.parametrize("name", ["r-1", "1r", "led 1", "x" * 30])
def test_instance_names_validated(inv, name):
    rep = check_design(mk({name: "resistor_1k"}, {}), inv)
    assert "structure" in hit(rep)


def test_pin_map_values_validated(inv):
    rep = check_design(mk(**LED_OK, pin_map={"L": "GPx"}), inv)
    assert "structure" in hit(rep)


# ------------------------------------------------------------- new rules ---


def test_button_across_supply_is_a_short(inv):
    rep = check_design(mk({"sw1": "button"}, {"3V3": ["sw1.a"], "GND": ["sw1.b"]}), inv)
    assert "shorted_part" in hit(rep)


def test_part_with_both_legs_on_one_net(inv):
    rep = check_design(mk({"r1": "resistor_1k"}, {"X": ["pico.GP2", "r1.1", "r1.2"]}), inv)
    assert "shorted_part" in hit(rep)


def test_led_sink_configuration_counted(inv):
    d = mk(
        {"led1": "led_red", "r1": "resistor_220"},
        {"3V3": ["led1.anode"], "K": ["led1.cathode", "r1.1"], "S": ["r1.2", "pico.GP2"]},
    )
    rep = check_design(d, inv)
    assert rep.ok, rep.render()
    assert rep.gpio_current_ma["GP2"] == pytest.approx((3.3 - 1.8) / 220 * 1000, rel=1e-3)


def test_led_overcurrent_from_5v(inv, tmp_path):
    def add_100r(d):
        d["parts"]["resistor_100"] = dict(d["parts"]["resistor_220"], ohms=100, name="100 ohm")

    inv2 = custom_inv(tmp_path, add_100r)
    rep = check_design(
        mk(
            {"led1": "led_red", "r1": "resistor_100"},
            {"5V": ["r1.1"], "A": ["r1.2", "led1.anode"], "GND": ["led1.cathode"]},
        ),
        inv2,
    )
    assert "led_current" in hit(rep)  # (5 - 1.8) / 100 = 32 mA > 20 mA


def test_five_volts_through_led_into_gpio(inv):
    rep = check_design(
        mk(
            {"led1": "led_red", "r1": "resistor_220"},
            {"5V": ["r1.1"], "A": ["r1.2", "led1.anode"], "K": ["led1.cathode", "pico.GP3"]},
        ),
        inv,
    )
    assert "overvoltage" in hit(rep)


def test_dht_on_5v_puts_pullup_voltage_on_gpio(inv):
    rep = check_design(mk({"d1": "dht22"}, {"5V": ["d1.vcc"], "D": ["d1.data", "pico.GP15"], "GND": ["d1.gnd"]}), inv)
    assert any("not 5 V tolerant" in f.message for f in rep.errors)


def test_npn_collector_without_load(inv):
    rep = check_design(
        mk(
            {"q1": "npn", "r2": "resistor_1k"},
            {"B0": ["pico.GP16", "r2.1"], "B": ["r2.2", "q1.b"], "3V3": ["q1.c"], "GND": ["q1.e"]},
        ),
        inv,
    )
    assert any("collector is wired straight" in f.message for f in rep.errors)


def test_npn_weak_base_drive_warns(inv):
    rep = check_design(
        mk(
            {"q1": "npn", "r2": "resistor_10k", "bz1": "buzzer_active"},
            {
                "B0": ["pico.GP16", "r2.1"],
                "B": ["r2.2", "q1.b"],
                "C": ["q1.c", "bz1.minus"],
                "3V3": ["bz1.plus"],
                "GND": ["q1.e"],
            },
        ),
        inv,
    )
    assert rep.ok and "transistor" in hit(rep, "warning")


def test_yaml_cannot_loosen_limits(tmp_path):
    def loosen(d):
        d["limits"]["gpio_max_ma"] = 100
        d["limits"]["rail_3v3_max_ma"] = 2000
        d["limits"]["gpio_warn_ma"] = 4  # tightening is allowed

    inv2 = custom_inv(tmp_path, loosen)
    assert inv2.limits["gpio_max_ma"] == HARD_LIMITS["gpio_max_ma"]
    assert inv2.limits["rail_3v3_max_ma"] == HARD_LIMITS["rail_3v3_max_ma"]
    assert inv2.limits["gpio_warn_ma"] == 4
    rep = check_design(mk({"bz1": "buzzer_active"}, {"B": ["pico.GP16", "bz1.plus"], "GND": ["bz1.minus"]}), inv2)
    assert "gpio_current" in hit(rep)
    assert effective_limits({"unknown": 1.0})["vbus_max_ma"] == 500.0


def test_part_needing_more_than_usb_voltage(inv, tmp_path):
    def hv(d):
        d["parts"]["oled"]["v_min"] = 9.0

    inv2 = custom_inv(tmp_path, hv)
    rep = check_design(
        mk(
            {"o1": "oled"},
            {"SDA": ["pico.GP4", "o1.sda"], "SCL": ["pico.GP5", "o1.scl"], "5V": ["o1.vcc"], "GND": ["o1.gnd"]},
        ),
        inv2,
    )
    assert any("never goes above USB 5 V" in f.message for f in rep.errors)


def test_usb_budget_and_resistor_power(inv, tmp_path):
    def big(d):
        d["parts"]["servo"]["qty"] = 2
        d["parts"]["resistor_10"] = dict(d["parts"]["resistor_220"], ohms=10, name="10 ohm")

    inv2 = custom_inv(tmp_path, big)
    rep = check_design(
        mk(
            {"s1": "servo", "s2": "servo"},
            {
                "S1": ["pico.GP16", "s1.sig"],
                "S2": ["pico.GP17", "s2.sig"],
                "5V": ["s1.vplus", "s2.vplus"],
                "GND": ["s1.gnd", "s2.gnd"],
            },
        ),
        inv2,
    )
    assert any("total USB draw" in f.message for f in rep.errors)
    rep = check_design(mk({"r1": "resistor_10"}, {"5V": ["r1.1"], "GND": ["r1.2"]}), inv2)
    assert {"resistor_power", "rail_budget"} <= hit(rep)


def test_every_rule_id_is_reachable(inv, gadgets, tmp_path):
    """Each documented rule fires on at least one bad design (keeps RULE_IDS and docs honest)."""

    def more(d):
        d["parts"]["bare_oled"] = dict(d["parts"]["oled"], pullup_onboard=False)
        d["parts"]["bare_dht"] = dict(d["parts"]["dht22"], pullup_onboard=False)
        d["parts"]["resistor_10"] = dict(d["parts"]["resistor_220"], ohms=10, name="10 ohm")
        d["parts"]["resistor_100"] = dict(d["parts"]["resistor_220"], ohms=100, name="100 ohm")

    inv2 = custom_inv(tmp_path, more)
    designs = [
        mk({"m1": "flux"}, {}),
        mk({f"l{i}": "led_red" for i in range(7)}, {}),
        mk(**LED_OK, pin_map={"A": "GP15", "B": "GP15"}),
        mk({}, {"GND": ["pico.GP2"], "X": ["pico.GP3", "pico.GP4"]}),
        mk({"sw1": "button"}, {"3V3": ["sw1.a"], "GND": ["sw1.b"]}),
        mk({"led1": "led_red"}, {"A": ["pico.GP15", "led1.anode"]}),
        mk({"led1": "led_red"}, {"A": ["pico.GP15", "led1.anode"], "GND": ["led1.cathode"]}),
        mk(
            {"led1": "led_red", "r1": "resistor_220"},
            {"A": ["pico.GP15", "r1.1"], "B": ["r1.2", "led1.cathode"], "GND": ["led1.anode"]},
        ),
        mk(
            {"led1": "led_red", "r1": "resistor_100"},
            {"5V": ["r1.1"], "A": ["r1.2", "led1.anode"], "GND": ["led1.cathode"]},
        ),
        mk(
            {"q1": "npn", "bz1": "buzzer_active"},
            {"B": ["pico.GP16", "q1.b"], "C": ["q1.c", "bz1.minus"], "GND": ["bz1.plus", "q1.e"]},
        ),
        Design.from_dict(gadgets["servo"].designs[0]),
        mk({"o1": "oled"}, {"SDA": ["pico.GP5", "o1.sda"], "SCL": ["pico.GP4", "o1.scl"], "GND": ["o1.vcc", "o1.gnd"]}),
        mk(
            {"o1": "bare_oled"},
            {"SDA": ["pico.GP4", "o1.sda"], "SCL": ["pico.GP5", "o1.scl"], "3V3": ["o1.vcc"], "GND": ["o1.gnd"]},
        ),
        mk({"d1": "bare_dht"}, {"3V3": ["d1.vcc"], "D": ["d1.data", "pico.GP15"], "GND": ["d1.gnd"]}),
        mk({"r1": "resistor_10"}, {"5V": ["r1.1"], "GND": ["r1.2"]}),
        mk({"r1": "resistor_220"}, {"5V": ["r1.1"], "S": ["r1.2", "pico.GP2"]}),
    ]
    from hands.circuit import with_fixture  # noqa: F401  (imported for clarity: fixtures merged inside check)

    seen = set()
    for d in designs:
        seen |= {f.rule for f in check_design(d, inv2).findings}
    # power_short needs two supplies on one net, which only the fixture could create; exercise the rule directly
    from hands.rules import _Ctx, _power_shorts

    full = Design.from_dict({"parts": {}, "nets": {"X": ["pico.3V3", "pico.GND"]}})
    full.fixture = True
    seen |= {f.rule for f in _power_shorts(_Ctx(full, inv2))}
    assert set(RULE_IDS) <= seen, set(RULE_IDS) - seen


def test_inventory_yaml_validation(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("parts: {x: {qty: 1}}\n")
    with pytest.raises(InventoryError):
        load_inventory(p)
    p.write_text("- just a list\n")
    with pytest.raises(InventoryError):
        load_inventory(p)


# ----------------------------------------------------------- spec screening ---


@pytest.mark.parametrize(
    "spec,cat",
    [
        ("a lamp that plugs into the wall outlet", "mains"),
        ("control my 120V desk fan", "mains"),
        ("a portable gadget running off a LiPo battery", "battery"),
        ("use an 18650 cell", "battery"),
        ("drive a 12V LED strip", "high_voltage"),
        ("a 2A motor driver", "high_current"),
        ("spin a DC motor", "high_current"),
        ("switch a relay", "high_current"),
    ],
)
def test_unsafe_specs_refused(spec, cat):
    refusals = screen_text(spec)
    assert cat in {r.category for r in refusals}
    with pytest.raises(SafetyRefusal, match="USB port"):
        require_safe_text(spec)


@pytest.mark.parametrize(
    "spec",
    [
        "A desk light with a button, plus a buzzer when the Steelers score",
        "Show the temperature on a small screen, updating 3 a day",
        "A servo needle that points at a 0-100 value",
        "LED that runs at 5V from USB using 20 mA",
    ],
)
def test_safe_specs_pass(spec):
    assert screen_text(spec) == []


# ----------------------------------------------------------------- firmware lint ---

GOOD = "from machine import Pin\nimport time\nLED = 15\np = Pin(LED, Pin.OUT)\nprint('READY')\nwhile True:\n    time.sleep(1)\n"


@pytest.mark.parametrize(
    "code,needle",
    [
        (
            "from machine import Pin\nimport time\nn = 15\nn += 11\nPin(n, Pin.OUT)\nprint('READY')\ntime.sleep(1)\n",
            "literal GPIO",
        ),
        ("from machine import Pin as P\nimport time\nP(26, P.OUT)\nprint('READY')\ntime.sleep(1)\n", "Pin(26)"),
        ("import machine, time\nmachine.mem32[0x40014000] = 5\nprint('READY')\ntime.sleep(1)\n", "mem32"),
        ("from machine import Pin, I2C\nimport time\ni = I2C(1)\nprint('READY')\ntime.sleep(1)\n", "explicitly"),
        ("import time\neval('1')\nprint('READY')\ntime.sleep(1)\n", "eval()"),
        ("import time\nx = ().__class__\nprint('READY')\ntime.sleep(1)\n", "__class__"),
        ("import time\ngetattr(time, '__dict__')\nprint('READY')\ntime.sleep(1)\n", "getattr"),
        ("import time\nfrom machine import Pin\nPin('WL_GPIO1')\nprint('READY')\ntime.sleep(1)\n", "only 'LED'"),
        ("import machine, time\nmachine.freq(250_000_000)\nprint('READY')\ntime.sleep(1)\n", "CPU clock"),
        ("import machine, time\nmachine.deepsleep(1000)\nprint('READY')\ntime.sleep(1)\n", "deepsleep"),
        ("from machine import *\nimport time\nprint('READY')\ntime.sleep(1)\n", "import *"),
        ("import network\nimport time\nprint('READY')\ntime.sleep(1)\n", "network"),
        (
            "from machine import Pin\nimport time\np = Pin(15)\np.irq(lambda x: 0)\nprint('READY')\ntime.sleep(1)\n",
            "irq",
        ),
        ("x = 1\n" * 10 + "print(1)\n", "READY"),
    ],
)
def test_firmware_lint_rejects(code, needle):
    errs = lint_firmware(code, {"LIGHT": "GP15"})
    assert any(needle in e for e in errs), errs


def test_firmware_lint_accepts_constants_and_onboard_led():
    assert lint_firmware(GOOD, {"LIGHT": "GP15"}) == []
    code = (
        "from machine import Pin\nimport time\nfrom micropython import const\nX = const(15)\nPin(X)\n"
        "Pin('LED', Pin.OUT)\nprint('READY')\ntime.sleep(1)\n"
    )
    assert lint_firmware(code, {"LIGHT": "GP15"}) == []
    assert lint_firmware("x" * 40_000, {}) and "larger" in lint_firmware("x" * 40_000, {})[0]


def test_firmware_lint_button_pin_never_output(inv, steelers_design):
    code = "from machine import Pin, PWM\nimport time\nPin(14, Pin.OUT)\nPWM(Pin(14))\nprint('READY')\ntime.sleep(1)\n"
    errs = lint_firmware(code, steelers_design.pin_map, steelers_design, inv)
    assert sum("wired to a button" in e for e in errs) == 2


def test_scripted_firmwares_pass_lint(inv, gadgets):
    from hands.diagnose import DIAG_SERVO_FW, DIAG_TOGGLE_FW, SAFE_IDLE_FW

    for g in gadgets.values():
        d = Design.from_dict(g.designs[-1])
        for fw in g.firmwares:
            assert lint_firmware(fw, d.pin_map, d, inv) == [], g.id
    assert lint_firmware(SAFE_IDLE_FW, {}) == []
    assert lint_firmware(DIAG_TOGGLE_FW.format(n=15), {"X": "GP15"}) == []
    assert lint_firmware(DIAG_SERVO_FW.format(n=16), {"X": "GP16"}) == []
