import copy

import pytest

from hands.circuit import Design
from hands.inventory import load_inventory
from hands.rules import check_design

from .conftest import mk

LED_OK = {
    "parts": {"led1": "led_red", "r1": "resistor_220"},
    "nets": {"A": ["pico.GP15", "r1.1"], "B": ["r1.2", "led1.anode"], "GND": ["led1.cathode"]},
}


def rules_hit(rep, severity="error"):
    return {f.rule for f in rep.findings if f.severity == severity}


@pytest.mark.parametrize("gid", ["steelers", "tempdisplay", "servo"])
def test_final_gadget_designs_pass(inv, gadgets, gid):
    rep = check_design(Design.from_dict(gadgets[gid].designs[-1]), inv)
    assert rep.ok, rep.render()


def test_led_with_resistor_ok_and_current_estimate(inv):
    rep = check_design(mk(**LED_OK), inv)
    assert rep.ok
    # worst case uses the LED's minimum forward voltage (1.8 V for red)
    assert rep.gpio_current_ma["GP15"] == pytest.approx((3.3 - 1.8) / 220 * 1000, rel=1e-3)


def test_led_without_resistor(inv):
    rep = check_design(mk({"led1": "led_red"}, {"A": ["pico.GP15", "led1.anode"], "GND": ["led1.cathode"]}), inv)
    assert {"led_resistor", "gpio_current"} <= rules_hit(rep)


def test_led_reversed(inv):
    d = copy.deepcopy(LED_OK)
    d["nets"] = {"A": ["pico.GP15", "r1.1"], "B": ["r1.2", "led1.cathode"], "GND": ["led1.anode"]}
    assert "led_polarity" in rules_hit(check_design(mk(**d), inv))


def test_gpio_direct_to_gnd_and_3v3(inv):
    rep = check_design(mk({"sw1": "button"}, {"GND": ["pico.GP3", "sw1.a", "sw1.b"]}), inv)
    assert "gpio_short" in rules_hit(rep)
    rep = check_design(mk({"r1": "resistor_220"}, {"3V3": ["pico.GP3", "r1.1", "r1.2"]}), inv)
    assert "gpio_short" in rules_hit(rep)


def test_two_gpios_tied(inv):
    rep = check_design(mk({"r1": "resistor_1k"}, {"X": ["pico.GP2", "pico.GP3", "r1.1"], "GND": ["r1.2"]}), inv)
    assert "gpio_short" in rules_hit(rep)


def test_buzzer_on_gpio_exceeds_current(inv, gadgets):
    rep = check_design(Design.from_dict(gadgets["steelers"].designs[0]), inv)
    assert [f.rule for f in rep.errors] == ["gpio_current"]
    assert "25" in rep.errors[0].message


def test_transistor_base_without_resistor(inv):
    rep = check_design(
        mk(
            {"q1": "npn", "bz1": "buzzer_active"},
            {"B": ["pico.GP16", "q1.b"], "C": ["q1.c", "bz1.minus"], "3V3": ["bz1.plus"], "GND": ["q1.e"]},
        ),
        inv,
    )
    assert "transistor" in rules_hit(rep)


def test_servo_on_3v3_rejected(inv, gadgets):
    rep = check_design(Design.from_dict(gadgets["servo"].designs[0]), inv)
    assert "servo_power" in rules_hit(rep)


def test_i2c_invalid_and_swapped_pins(inv):
    base = {"oled1": "oled"}
    swapped = mk(
        base,
        {
            "SDA": ["pico.GP5", "oled1.sda"],
            "SCL": ["pico.GP4", "oled1.scl"],
            "3V3": ["oled1.vcc"],
            "GND": ["oled1.gnd"],
        },
    )
    rep = check_design(swapped, inv)
    assert "i2c" in rules_hit(rep)
    assert "swapped" in " ".join(f.message for f in rep.errors)


def test_i2c_module_on_5v_puts_5v_on_gpio(inv):
    rep = check_design(
        mk(
            {"oled1": "oled"},
            {
                "SDA": ["pico.GP4", "oled1.sda"],
                "SCL": ["pico.GP5", "oled1.scl"],
                "5V": ["oled1.vcc"],
                "GND": ["oled1.gnd"],
            },
        ),
        inv,
    )
    assert any("not 5 V tolerant" in f.message for f in rep.errors)


def test_i2c_address_conflict_and_missing_pullups(tmp_path):
    import yaml

    from hands.inventory import DEFAULT_INVENTORY

    data = yaml.safe_load(DEFAULT_INVENTORY.read_text())
    data["parts"]["oled"]["qty"] = 2
    data["parts"]["bare_oled"] = dict(data["parts"]["oled"], pullup_onboard=False)
    p = tmp_path / "inv.yaml"
    p.write_text(yaml.safe_dump(data))
    inv2 = load_inventory(p)
    nets = {
        "SDA": ["pico.GP4", "o1.sda", "o2.sda"],
        "SCL": ["pico.GP5", "o1.scl", "o2.scl"],
        "3V3": ["o1.vcc", "o2.vcc"],
        "GND": ["o1.gnd", "o2.gnd"],
    }
    rep = check_design(mk({"o1": "oled", "o2": "oled"}, nets), inv2)
    assert any("address 0x3C" in f.message for f in rep.errors)
    rep = check_design(
        mk(
            {"o1": "bare_oled"},
            {"SDA": ["pico.GP4", "o1.sda"], "SCL": ["pico.GP5", "o1.scl"], "3V3": ["o1.vcc"], "GND": ["o1.gnd"]},
        ),
        inv2,
    )
    assert any("pull-up" in f.message for f in rep.errors)


def test_reserved_and_conflicting_pins(inv):
    rep = check_design(mk(**LED_OK, pin_map={"LIGHT": "GP15", "OTHER": "GP15"}), inv)
    assert "pin_conflict" in rules_hit(rep)
    rep = check_design(mk(**LED_OK, pin_map={}), inv)  # wired GPIO missing from pin_map
    assert "pin_conflict" in rules_hit(rep)
    d = copy.deepcopy(LED_OK)
    d["nets"]["A"][0] = "pico.GP26"
    assert "pin_conflict" in rules_hit(check_design(mk(**d), inv))


def test_inventory_only(inv):
    rep = check_design(mk({"m1": "stepper_motor"}, {"X": ["pico.GP2", "m1.a"]}), inv)
    assert "structure" in rules_hit(rep)
    many = {f"led{i}": "led_red" for i in range(7)}
    rep = check_design(mk(many, {}), inv)
    assert "inventory" in rules_hit(rep)


def test_unconnected_pin(inv):
    rep = check_design(
        mk({"led1": "led_red", "r1": "resistor_220"}, {"A": ["pico.GP15", "r1.1"], "B": ["r1.2", "led1.anode"]}), inv
    )
    assert "unconnected_pin" in rules_hit(rep)


def test_checker_is_deterministic(inv, gadgets):
    d = Design.from_dict(gadgets["steelers"].designs[0])
    a = [str(f) for f in check_design(d, inv).findings]
    b = [str(f) for f in check_design(d, inv).findings]
    assert a == b


def test_inventory_facts(inv):
    assert len(inv.pico_header) == 40
    assert inv.pico_pin_number("GP15") == 20 and inv.pico_pin_number("VBUS") == 40
    assert inv.pico_pins_with("GND") == [3, 8, 13, 18, 23, 28, 38]
    assert inv.part("led_red").get("vf") == 2.0
    assert inv.part("servo").get("v_min") == 4.8
    with pytest.raises(KeyError):
        inv.part("flux_capacitor")
