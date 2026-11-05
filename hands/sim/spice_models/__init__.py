"""Device models for the SPICE checks (and the matching closed-form LED model the rules use).

Every model here says where its numbers come from. Where a number is an assumption rather than
a datasheet value, it says so. Nothing here was measured on the kit's own parts.

* **PN2222A**: Gummel-Poon parameters of the Fairchild 2N2222A model as distributed in LTspice's
  ``standard.bjt`` (mirrored at https://ltwiki.org/index.php?title=Standard.bjt). The PN2222A is
  the TO-92 package of the same die. Parameters ngspice does not understand (``Vceo``,
  ``Icrating``, ``mfg``) are dropped.
* **5 mm LEDs**: a Shockley diode with series resistance, ``I = IS*(exp((V - I*RS)/(N*Vt)) - 1)``.
  ``IS`` is solved so the forward voltage at 20 mA equals the inventory's ``vf`` (typical),
  ``vf_min`` (low corner) or :data:`LED_VF_MAX` (high corner). ``N`` and ``RS`` are
  *assumptions*: they give a 0.30 V drop between 20 mA and 1 mA, the shape of the Vf-If curves
  in 5 mm indicator-LED datasheets. ``LED_VF_MAX`` = 2.5 V at 20 mA is the maximum in the
  Kingbright L-57IID datasheet (High Efficiency Red, T-1 3/4); the yellow and green parts are
  assumed to share it.
* **Active buzzer**: a DC-average equivalent (0.3 V diode + 120 ohm) fitted to the inventory's
  25 mA at 3.3 V. A real active buzzer is an oscillator plus a magnetic transducer; this is the
  same equivalent load the in-process simulator (``hands/sim/mna.py``) uses.
* **RP2040 GPIO pad**: a driver resistance to IOVDD (high) or GND (low) plus ESD clamp diodes
  to IOVDD and GND. The RP2040 datasheet (section 5.5.3, IO electrical characteristics) only
  bounds the driver from above (VOH >= IOVDD - 0.62 V at the selected drive current, i.e. at
  most ~155 ohm at the default 4 mA setting), so the worst-case ("hot") corners use an *ideal*
  pad (1 milliohm): the most current any real pad could deliver.
* **Modules** (DHT22, SSD1306, INA219, SG90): their supply pins draw the inventory's
  ``current_ma`` (``current_moving_ma`` for the servo) once the supply is above the part's
  ``v_min``, scaled down linearly below it. On-board pull-ups use the inventory's
  ``pullup_ohms``; the DHT22 has no figure there, so 4.7 kohm (the smaller common value, i.e.
  the worse case for the GPIO that pulls the line low) is assumed.
"""

from __future__ import annotations

import math

#: Thermal voltage at 27 C (SPICE's default temperature), in volts.
VT = 1.380649e-23 * 300.15 / 1.602176634e-19

# ------------------------------------------------------------------------ LEDs ---
LED_N = 2.0  # emission coefficient (assumption, see module docstring)
LED_RS = 7.5  # series resistance, ohm (assumption)
LED_I_REF = 0.020  # the datasheet test current, A
LED_VF_MAX = 2.5  # V at 20 mA (Kingbright L-57IID, HE red; assumed for yellow/green)


def led_is(vf_at_20ma: float) -> float:
    """Saturation current that puts the forward voltage at 20 mA exactly at ``vf_at_20ma``."""
    return LED_I_REF / math.expm1((vf_at_20ma - LED_I_REF * LED_RS) / (LED_N * VT))


def led_vf(i_a: float, vf_at_20ma: float) -> float:
    """Forward voltage of the LED model at current ``i_a`` (A)."""
    if i_a <= 0:
        return 0.0
    return LED_N * VT * math.log1p(i_a / led_is(vf_at_20ma)) + i_a * LED_RS


def led_current(v_avail: float, r_ohms: float, vf_at_20ma: float) -> float:
    """Exact current (A) of the LED model in series with ``r_ohms`` across ``v_avail`` volts.

    Solved by bisection on the monotonic ``I*R + Vf(I) = V``. Used by the rule checker so its
    worst-case LED arithmetic is the same physics SPICE uses (``tests/test_rules_soundness.py``).
    """
    if v_avail <= 0:
        return 0.0
    lo, hi = 0.0, v_avail / max(r_ohms + LED_RS, 1e-9)
    for _ in range(60):
        mid = (lo + hi) / 2
        if mid * r_ohms + led_vf(mid, vf_at_20ma) > v_avail:
            hi = mid
        else:
            lo = mid
    return lo


def led_pwl(vf_at_20ma: float, i_lo: float = 0.005) -> tuple[float, float]:
    """(knee voltage, slope ohms) of the straight line through the model at ``i_lo`` and 20 mA:
    the piecewise-linear LED ``hands/sim/mna.py`` uses, fitted to the same curve as SPICE."""
    v_lo, v_hi = led_vf(i_lo, vf_at_20ma), led_vf(LED_I_REF, vf_at_20ma)
    rd = (v_hi - v_lo) / (LED_I_REF - i_lo)
    return v_hi - LED_I_REF * rd, rd


def led_model(name: str, vf_at_20ma: float) -> str:
    return f".model {name} D(IS={led_is(vf_at_20ma):.6e} N={LED_N} RS={LED_RS})"


# -------------------------------------------------------------------- others ---
PN2222A = (
    ".model QPN2222A NPN(Is=14.34f Xti=3 Eg=1.11 Vaf=74.03 Bf=255.9 Ne=1.307 Ise=14.34f Ikf=.2847 "
    "Xtb=1.5 Br=6.092 Nc=2 Isc=0 Ikr=0 Rc=1 Cjc=7.306p Mjc=.3416 Vjc=.75 Fc=.5 Cje=22.01p Mje=.377 "
    "Vje=.75 Tr=46.91n Tf=411.1p Itf=.6 Vtf=1.7 Xtf=3 Rb=10)"
)

BUZZER_VD = 0.3  # V
BUZZER_R = 120.0  # ohm
BUZZER_IS = 0.025 / math.expm1(BUZZER_VD / VT)  # 0.3 V at 25 mA through the junction alone
BUZZER = f".model DBUZZER D(IS={BUZZER_IS:.6e} N=1 RS={BUZZER_R})"

#: ESD clamp diodes on every RP2040 GPIO (to IOVDD and to GND). Generic silicon junction.
CLAMP = ".model DCLAMP D(IS=1e-14 N=1)"

PAD_NOMINAL_OHMS = 40.0  # same as the in-process simulator (hands/sim/board.py)
PAD_IDEAL_OHMS = 1e-3  # worst case: no driver resistance at all
PAD_WEAK_OHMS = 155.0  # datasheet upper bound at the default 4 mA drive (0.62 V / 4 mA)
PULL_OHMS = 50e3  # internal pull-up/-down (low end of the RP2040's range: the larger current)

#: Supply tolerances used by the corners. 3V3: +/-3 % around the Pico's 3.3 V regulator output
#: (assumption; the regulator datasheet is not cited yet). VBUS: the USB 2.0 range.
V3V3_RANGE = (3.2, 3.4)
VBUS_RANGE = (4.75, 5.25)
R_TOL = 0.05  # gold-band resistors
DHT22_PULLUP_OHMS = 4700.0
#: The rules' number for the Pico's 3V3 regulator efficiency (VBUS power = 3V3 power / 0.85).
REG_EFFICIENCY = 0.85

__all__ = [
    "BUZZER",
    "CLAMP",
    "LED_VF_MAX",
    "PN2222A",
    "R_TOL",
    "V3V3_RANGE",
    "VBUS_RANGE",
    "led_current",
    "led_is",
    "led_model",
    "led_pwl",
    "led_vf",
]
