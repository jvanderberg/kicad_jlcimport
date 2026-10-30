"""Pair the two pads of a polarized two-terminal part by function, not by number.

Two pads always form a point-symmetric pattern, so the solver cannot tell a
part from its 180° turn: polarity comes only from which pad is paired with
which.  Pad numbers are not a reliable signal for that.  KiCad's diode and
LED symbols put the cathode on pin 1 while many JLC/EasyEDA symbols put the
anode there, so pairing by number mounts such a part backwards.

Instead each pad gets a role (positive = anode/+, negative = cathode/-):

* JLC side: from JLC's own symbol pin names (``A``/``K``, ``+``/``-``).
* Board side: from the pad's pin function (the schematic pin name KiCad
  stores on the pad), else from pad numbers that are themselves polarity
  names (``A``/``K``, ``+``/``-``).  When neither works, KiCad's library
  convention is assumed: diodes/LEDs have the cathode on pad 1, polarized
  capacitors have ``+`` on pad 1.

The JLC pads are then renumbered so that equal roles share a number, and the
normal solver runs on that.

A footprint with a third, centred pad (an LED or diode with a thermal pad) is
just as symmetric, so the same pairing applies whenever JLC's pads are
point-symmetric and exactly one pin is named as each polarity.  KiCad's
pad-1 convention is only assumed for footprints with pads 1 and 2 alone.

When JLC's symbol doesn't name its pins but the board says the part is
polarized (pin functions, an _AK / _KA name, or an LED_ / D_ / CP_ footprint),
the pads stay paired by number and the result is flagged ``JLC_UNNAMED``.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .models import PadRecord

POSITIVE = "+"
NEGATIVE = "-"

DIODE = "diode"  # anode/cathode naming
POLARIZED = "polarized"  # +/- naming

# How the pairing was decided.
NOT_POLARIZED = ""  # not a two-pad part with polarity names on JLC's symbol
BY_NAMES = "names"
BY_CONVENTION = "convention"
BY_FOOTPRINT_NAME = "footprint_name"  # KiCad's _AK / _KA suffix
UNRESOLVED = "unresolved"
NAMES_UNAVAILABLE = "names_unavailable"  # two-pad part, but JLC's symbol could not be fetched
JLC_UNNAMED = "jlc_unnamed"  # the board says polarized, JLC's symbol has no polarity names

_ROLE_TOKENS: Dict[str, Tuple[str, str]] = {
    "A": (POSITIVE, DIODE),
    "ANODE": (POSITIVE, DIODE),
    "K": (NEGATIVE, DIODE),
    "CATHODE": (NEGATIVE, DIODE),
    "+": (POSITIVE, POLARIZED),
    "POS": (POSITIVE, POLARIZED),
    "POSITIVE": (POSITIVE, POLARIZED),
    "-": (NEGATIVE, POLARIZED),
    "NEG": (NEGATIVE, POLARIZED),
    "NEGATIVE": (NEGATIVE, POLARIZED),
}

# Role of pad 1 in KiCad's library symbols, by kind of part.
_KICAD_PAD1_ROLE = {DIODE: NEGATIVE, POLARIZED: POSITIVE}

# KiCad footprint names state the pad order of some LEDs/diodes: "..._AK" means
# pad 1 = anode, pad 2 = cathode (e.g. LED_PLCC-2_3.4x3.0mm_AK).
_PAD_ORDER_RE = re.compile(r"(?:^|_)(AK|KA)(?:_|$)", re.IGNORECASE)
# KiCad library footprints for LEDs, diodes and polarized capacitors.
_POLARIZED_FOOTPRINT_RE = re.compile(r"^(LED|D|CP)_", re.IGNORECASE)

SYMMETRY_TOL = 0.05  # mm; same as the solver's Tolerances.symmetry_tol


def footprint_name_roles(footprint_name: str) -> Dict[str, str]:
    """Pad roles stated by an _AK / _KA footprint name, else {}."""
    m = _PAD_ORDER_RE.search(footprint_name or "")
    if not m:
        return {}
    first = POSITIVE if m.group(1).upper() == "AK" else NEGATIVE
    return {"1": first, "2": NEGATIVE if first == POSITIVE else POSITIVE}


def _name_roles(name: str) -> set:
    tokens = [t for t in re.split(r"[\s_/(),.]+", (name or "").strip().upper()) if t]
    return {_ROLE_TOKENS[t] for t in tokens if t in _ROLE_TOKENS}


def pin_role(name: str) -> Optional[Tuple[str, str]]:
    """(polarity, kind) for a pin name such as ``K``, ``A_2`` or ``+``; None if not a polarity name."""
    roles = _name_roles(name)
    return roles.pop() if len(roles) == 1 else None


def point_symmetric(pads: Sequence[PadRecord], tol: float = SYMMETRY_TOL) -> bool:
    """True when the numbered pads are unchanged by a 180° turn about their centroid."""
    points = [(p.x, p.y) for p in pads if _normalize(p.number)]
    if not points:
        return False
    cx = sum(x for x, _ in points) / len(points)
    cy = sum(y for _, y in points) / len(points)
    return all(any(abs(2 * cx - x - ox) <= tol and abs(2 * cy - y - oy) <= tol for ox, oy in points) for x, y in points)


@dataclass
class PolarityMatch:
    """Outcome of :func:`match_polarity`."""

    jlc_pads: List[PadRecord]  # JLC pads, renumbered to the board's numbers when paired by role
    source: str = NOT_POLARIZED
    kind: str = ""
    swapped: bool = False  # JLC numbers the pads the other way round from the board


def _normalize(number: str) -> str:
    return (number or "").strip().upper()


def _numbers(pads: Sequence[PadRecord]) -> List[str]:
    seen: List[str] = []
    for pad in pads:
        num = _normalize(pad.number)
        if num and num not in seen:
            seen.append(num)
    return seen


def _complete(roles: Dict[str, str]) -> Dict[str, str]:
    """*roles* if it names one positive and one negative pad, else {}."""
    return roles if sorted(roles.values()) == sorted([POSITIVE, NEGATIVE]) else {}


def _board_roles(board_pads: Sequence[PadRecord], numbers: List[str]) -> Dict[str, str]:
    """Board pad number -> polarity from pin functions, else from the pad numbers themselves."""
    roles: Dict[str, str] = {}
    for num in numbers:
        found = {pin_role(p.function) for p in board_pads if _normalize(p.number) == num} - {None}
        if len(found) == 1:
            roles[num] = found.pop()[0]
    if _complete(roles):
        return roles
    by_number = {num: pin_role(num) for num in numbers}
    return _complete({num: role[0] for num, role in by_number.items() if role is not None})


def _jlc_roles(jlc_nums: List[str], names: Dict[str, str]) -> Tuple[Dict[str, str], str]:
    """(JLC pad number -> polarity, kind) when exactly one pin is named as each polarity, else ({}, "").

    Any other pin must not mention a polarity at all: a common pin named
    ``K/A`` (a dual diode) means this is not a two-terminal part.
    """
    roles: Dict[str, str] = {}
    kinds = set()
    for num in jlc_nums:
        role = pin_role(names.get(num, ""))
        if role is not None:
            roles[num] = role[0]
            kinds.add(role[1])
        elif _name_roles(names.get(num, "")):
            return {}, ""
    if not _complete(roles) or len(kinds) != 1:
        return {}, ""
    return roles, kinds.pop()


def board_says_polarized(board_pads: Sequence[PadRecord], footprint_name: str) -> bool:
    """True when the board footprint marks itself as polarized."""
    name = (footprint_name or "").split(":")[-1]
    return bool(
        _board_roles(board_pads, _numbers(board_pads))
        or footprint_name_roles(name)
        or _POLARIZED_FOOTPRINT_RE.match(name)
    )


def match_polarity(
    board_pads: Sequence[PadRecord],
    jlc_pads: Sequence[PadRecord],
    jlc_pin_names: Dict[str, str],
    names_error: str = "",
    footprint_name: str = "",
) -> PolarityMatch:
    """Renumber *jlc_pads* so each JLC pad shares a number with the board pad of the same polarity.

    *names_error* is set when JLC's symbol could not be fetched; a two-pad
    part, or a three-pad one the board marks as polarized, then gets
    ``NAMES_UNAVAILABLE`` rather than silently pairing by number.
    *footprint_name* is the board footprint's name, whose _AK / _KA suffix
    beats KiCad's generic pad-1 convention.
    """
    unchanged = PolarityMatch(jlc_pads=list(jlc_pads))
    board_nums = _numbers(board_pads)
    jlc_nums = _numbers(jlc_pads)
    if len(board_nums) < 2 or len(jlc_nums) < 2:
        return unchanged
    # Only a point-symmetric pad pattern leaves polarity to the pairing; on
    # anything else the geometry already fixes which pad is which.
    if len(jlc_nums) > 2 and not point_symmetric(jlc_pads):
        return unchanged
    # Two pads, or two plus a centred pad: the shapes whose JLC symbol is fetched.
    two_terminal = len(jlc_nums) <= 3
    if names_error:
        if len(jlc_nums) == 2 or (two_terminal and board_says_polarized(board_pads, footprint_name)):
            return PolarityMatch(jlc_pads=list(jlc_pads), source=NAMES_UNAVAILABLE)
        return unchanged

    names = {_normalize(k): v for k, v in jlc_pin_names.items()}
    jlc_roles, kind = _jlc_roles(jlc_nums, names)
    if not jlc_roles:
        # Polarity-named pins that don't make a two-terminal part (a dual
        # diode's K/A pin) aren't "unnamed"; numbers decide.
        named = any(_name_roles(names.get(num, "")) for num in jlc_nums)
        if two_terminal and not named and board_says_polarized(board_pads, footprint_name):
            return PolarityMatch(jlc_pads=list(jlc_pads), source=JLC_UNNAMED)
        return unchanged

    board_roles = _board_roles(board_pads, board_nums)
    source = BY_NAMES
    if not board_roles and set(board_nums) == {"1", "2"}:
        board_roles = footprint_name_roles(footprint_name)
        source = BY_FOOTPRINT_NAME
    if not board_roles:
        if set(board_nums) != {"1", "2"}:
            return PolarityMatch(jlc_pads=list(jlc_pads), source=UNRESOLVED, kind=kind)
        pad1 = _KICAD_PAD1_ROLE[kind]
        board_roles = {"1": pad1, "2": NEGATIVE if pad1 == POSITIVE else POSITIVE}
        source = BY_CONVENTION

    board_by_role = {role: num for num, role in board_roles.items()}
    mapping = {num: board_by_role[role] for num, role in jlc_roles.items()}
    taken = set(mapping.values())

    def renumber(number: str) -> str:
        num = _normalize(number)
        if num in mapping:
            return mapping[num]
        # A JLC pad without polarity (a thermal pad) must not take a number
        # that now belongs to a polarized pad.
        return f"jlc:{number}" if num in taken else number

    renumbered = [PadRecord(number=renumber(p.number), x=p.x, y=p.y, function=p.function) for p in jlc_pads]
    # "Swapped" only means something when both sides use the same two numbers.
    swapped = set(mapping) == set(mapping.values()) and any(src != dst for src, dst in mapping.items())
    return PolarityMatch(jlc_pads=renumbered, source=source, kind=kind, swapped=swapped)
