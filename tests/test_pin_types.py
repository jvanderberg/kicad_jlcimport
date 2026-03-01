"""Tests for pin electrical type heuristics."""

import pytest

from kicad_jlcimport.easyeda.ee_types import EEPin, EESymbol
from kicad_jlcimport.easyeda.pin_types import infer_pin_types


def _pin(name: str = "", electrical_type: str = "unspecified", number: str = "1") -> EEPin:
    return EEPin(
        number=number,
        name=name,
        x=0.0,
        y=0.0,
        rotation=0.0,
        length=2.54,
        electrical_type=electrical_type,
    )


def _symbol(*pins: EEPin) -> EESymbol:
    return EESymbol(pins=list(pins))


# --- Passive prefix override ---


@pytest.mark.parametrize("prefix", ["C", "R", "L", "F", "FB", "CN", "RJ", "USB", "c", "r", "fb", "cn", "rj", "usb"])
def test_passive_prefix_sets_all_unspecified_to_passive(prefix):
    sym = _symbol(_pin("1"), _pin("2"))
    infer_pin_types(sym, prefix)
    assert all(p.electrical_type == "passive" for p in sym.pins)


def test_passive_prefix_skipped_when_any_pin_not_unspecified():
    """If any pin already has a real type (e.g. from name match), don't override."""
    sym = _symbol(_pin("1"), _pin("VCC"))
    # VCC will get matched to power_in first, so not all pins are unspecified
    infer_pin_types(sym, "C")
    # Pin "1" stays unspecified (not overridden to passive because not all were unspecified)
    assert sym.pins[0].electrical_type == "unspecified"
    assert sym.pins[1].electrical_type == "power_in"


def test_unknown_prefix_no_passive_override():
    sym = _symbol(_pin("1"), _pin("2"))
    infer_pin_types(sym, "U")
    assert all(p.electrical_type == "unspecified" for p in sym.pins)


# --- Power name matching ---


@pytest.mark.parametrize(
    "name",
    [
        "VCC",
        "VDD",
        "VBUS",
        "VBAT",
        "VIN",
        "VOUT",
        "VREF",
        "VREG",
        "AVCC",
        "AVDD",
        "V+",
        "V-",
        "3V3",
        "5V",
        "12V",
        "DVDD",
        "IOVDD",
        "USB_VDD",
        "ADC_AVDD",
    ],
)
def test_power_names_become_power_in(name):
    sym = _symbol(_pin(name))
    infer_pin_types(sym, "U")
    assert sym.pins[0].electrical_type == "power_in"


def test_power_name_overrides_bidirectional():
    """EasyEDA marks GND/VCC as bidirectional on ICs — should become power_in."""
    sym = _symbol(_pin("VCC", electrical_type="bidirectional"))
    infer_pin_types(sym, "U")
    assert sym.pins[0].electrical_type == "power_in"


# --- Ground name matching ---


@pytest.mark.parametrize(
    "name", ["GND", "GND1", "GND2", "VSS", "VSS1", "AGND", "DGND", "PGND", "SGND", "EGND", "GROUND", "Ground"]
)
def test_ground_names_become_power_in(name):
    sym = _symbol(_pin(name))
    infer_pin_types(sym, "U")
    assert sym.pins[0].electrical_type == "power_in"


def test_ground_bidirectional_becomes_power_in():
    sym = _symbol(_pin("GND", electrical_type="bidirectional"))
    infer_pin_types(sym, "U")
    assert sym.pins[0].electrical_type == "power_in"


# --- NC name matching ---


@pytest.mark.parametrize("name", ["NC", "DNC", "N/C", "nc"])
def test_nc_names_become_no_connect(name):
    sym = _symbol(_pin(name))
    infer_pin_types(sym, "U")
    assert sym.pins[0].electrical_type == "no_connect"


# --- SHELL/SHIELD name matching ---


@pytest.mark.parametrize("name", ["SHELL", "SHIELD", "shell", "Shield"])
def test_shell_shield_become_passive(name):
    sym = _symbol(_pin(name))
    infer_pin_types(sym, "U")
    assert sym.pins[0].electrical_type == "passive"


# --- Non-zero types preserved ---


@pytest.mark.parametrize("etype", ["input", "output"])
def test_nonzero_types_preserved(etype):
    """Input/output pins keep their types even if name looks like power."""
    sym = _symbol(_pin("DATA", electrical_type=etype))
    infer_pin_types(sym, "U")
    assert sym.pins[0].electrical_type == etype


def test_input_pin_not_overridden_by_name():
    sym = _symbol(_pin("VCC", electrical_type="input"))
    infer_pin_types(sym, "U")
    assert sym.pins[0].electrical_type == "input"


# --- Edge cases ---


def test_empty_symbol():
    sym = _symbol()
    infer_pin_types(sym, "R")  # should not raise


def test_mixed_ic_pins():
    """Simulate a typical IC with power, ground, signal, and NC pins."""
    sym = _symbol(
        _pin("VCC", electrical_type="bidirectional", number="1"),
        _pin("GND", electrical_type="bidirectional", number="2"),
        _pin("DATA", electrical_type="input", number="3"),
        _pin("CLK", electrical_type="input", number="4"),
        _pin("OUT", electrical_type="output", number="5"),
        _pin("NC", electrical_type="unspecified", number="6"),
    )
    infer_pin_types(sym, "U")
    assert sym.pins[0].electrical_type == "power_in"  # VCC
    assert sym.pins[1].electrical_type == "power_in"  # GND
    assert sym.pins[2].electrical_type == "input"  # DATA
    assert sym.pins[3].electrical_type == "input"  # CLK
    assert sym.pins[4].electrical_type == "output"  # OUT
    assert sym.pins[5].electrical_type == "no_connect"  # NC


def test_name_with_whitespace_stripped():
    sym = _symbol(_pin("  VCC  "))
    infer_pin_types(sym, "U")
    assert sym.pins[0].electrical_type == "power_in"
