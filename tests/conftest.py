import copy

import pytest

from hands.circuit import Design
from hands.gadgets import registry
from hands.inventory import load_inventory


@pytest.fixture(scope="session")
def inv():
    return load_inventory()


@pytest.fixture
def gadgets():
    return registry()


@pytest.fixture
def steelers_design(gadgets):
    return Design.from_dict(copy.deepcopy(gadgets["steelers"].designs[-1]))


def mk(parts, nets, pin_map=None, name="t"):
    """Build a design; by default every GPIO used gets a pin_map entry (required by the rules)."""
    if pin_map is None:
        pin_map = {}
        for pins in nets.values():
            for p in pins:
                if p.startswith("pico.GP"):
                    pin_map.setdefault(f"P_{p[5:]}", p[5:])
    return Design.from_dict({"name": name, "parts": parts, "nets": nets, "pin_map": pin_map})
