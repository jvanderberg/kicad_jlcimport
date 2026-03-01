"""Post-processing heuristics to refine pin electrical types.

EasyEDA provides poor electrical type data: passives get ``0`` (unspecified)
instead of ``passive``, and power pins on ICs get ``3`` (bidirectional) instead
of ``power_in``.  This module applies name- and prefix-based heuristics to
correct those after parsing.
"""

from __future__ import annotations

import re

from .ee_types import EESymbol

_PASSIVE_PREFIXES = {"C", "R", "L", "F", "FB", "CN", "RJ", "USB"}

_POWER_RE = re.compile(
    r"^("
    r"(\w+_?)?(V(CC|DD)|AV(CC|DD))"  # optional prefix for power rails
    r"|V(BUS|BAT|IN|OUT|REF|REG)"  # bare only
    r"|V\+|V-"
    r"|\d+V\d*"  # e.g. 3V3, 5V, 12V
    r")$",
    re.IGNORECASE,
)

_GROUND_RE = re.compile(
    r"^("
    r"GND\d*"
    r"|VSS\d*"
    r"|AGND|DGND|PGND|SGND|EGND"
    r"|GROUND"
    r")$",
    re.IGNORECASE,
)

_NC_RE = re.compile(
    r"^(NC|DNC|N/C)$",
    re.IGNORECASE,
)

_PASSIVE_NAME_RE = re.compile(
    r"^(SHELL|SHIELD)$",
    re.IGNORECASE,
)


def infer_pin_types(symbol: EESymbol, prefix: str) -> None:
    """Refine pin electrical types on *symbol* using heuristics.

    Mutates ``pin.electrical_type`` in place.

    1. **Pin-name matching** (all components, only when EasyEDA type is
       ``unspecified`` or ``bidirectional``):
       - Power names (incl. compound like DVDD, USB_VDD) → ``power_in``
       - Ground names (incl. GROUND) → ``power_in``
       - No-connect names → ``no_connect``
       - SHELL/SHIELD → ``passive``

    2. **Prefix-based passive override** (only when *every* pin is
       ``unspecified``): prefixes C, R, L, F, FB, CN, RJ, USB → set all
       pins to ``passive``.
    """
    if not symbol.pins:
        return

    # --- Heuristic 1: pin-name matching ---
    for pin in symbol.pins:
        if pin.electrical_type not in ("unspecified", "bidirectional"):
            continue
        name = pin.name.strip()
        if _POWER_RE.match(name) or _GROUND_RE.match(name):
            pin.electrical_type = "power_in"
        elif _NC_RE.match(name):
            pin.electrical_type = "no_connect"
        elif _PASSIVE_NAME_RE.match(name):
            pin.electrical_type = "passive"

    # --- Heuristic 2: passive prefix override ---
    all_unspecified = all(p.electrical_type == "unspecified" for p in symbol.pins)
    if all_unspecified and prefix.upper() in _PASSIVE_PREFIXES:
        for pin in symbol.pins:
            pin.electrical_type = "passive"
