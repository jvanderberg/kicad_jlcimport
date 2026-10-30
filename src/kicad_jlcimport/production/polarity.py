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
UNRESOLVED = "unresolved"
NAMES_UNAVAILABLE = "names_unavailable"  # two-pad part, but JLC's symbol could not be fetched

# Sources that rest on an assumption or on nothing: the fit is at most MEDIUM.
UNCERTAIN_SOURCES = (BY_CONVENTION, UNRESOLVED, NAMES_UNAVAILABLE)

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


def pin_role(name: str) -> Optional[Tuple[str, str]]:
    """(polarity, kind) for a pin name such as ``K``, ``A_2`` or ``+``; None if not a polarity name."""
    tokens = [t for t in re.split(r"[\s_/(),.]+", (name or "").strip().upper()) if t]
    roles = {_ROLE_TOKENS[t] for t in tokens if t in _ROLE_TOKENS}
    return roles.pop() if len(roles) == 1 else None


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


def match_polarity(
    board_pads: Sequence[PadRecord],
    jlc_pads: Sequence[PadRecord],
    jlc_pin_names: Dict[str, str],
    names_error: str = "",
) -> PolarityMatch:
    """Renumber *jlc_pads* so each JLC pad shares a number with the board pad of the same polarity.

    *names_error* is set when JLC's symbol could not be fetched; a two-pad
    part then gets ``NAMES_UNAVAILABLE`` rather than silently pairing by number.
    """
    unchanged = PolarityMatch(jlc_pads=list(jlc_pads))
    board_nums = _numbers(board_pads)
    jlc_nums = _numbers(jlc_pads)
    if len(board_nums) != 2 or len(jlc_nums) != 2:
        return unchanged
    if names_error:
        return PolarityMatch(jlc_pads=list(jlc_pads), source=NAMES_UNAVAILABLE)

    names = {_normalize(k): v for k, v in jlc_pin_names.items()}
    jlc_roles = {num: pin_role(names.get(num, "")) for num in jlc_nums}
    if any(r is None for r in jlc_roles.values()):
        return unchanged
    polarities = {r[0] for r in jlc_roles.values()}
    kinds = {r[1] for r in jlc_roles.values()}
    if polarities != {POSITIVE, NEGATIVE} or len(kinds) != 1:
        return unchanged
    kind = kinds.pop()

    board_roles = _board_roles(board_pads, board_nums)
    source = BY_NAMES
    if not board_roles:
        if set(board_nums) != {"1", "2"}:
            return PolarityMatch(jlc_pads=list(jlc_pads), source=UNRESOLVED, kind=kind)
        pad1 = _KICAD_PAD1_ROLE[kind]
        board_roles = {"1": pad1, "2": NEGATIVE if pad1 == POSITIVE else POSITIVE}
        source = BY_CONVENTION

    board_by_role = {role: num for num, role in board_roles.items()}
    mapping = {num: board_by_role[role[0]] for num, role in jlc_roles.items()}
    renumbered = [
        PadRecord(number=mapping.get(_normalize(p.number), p.number), x=p.x, y=p.y, function=p.function)
        for p in jlc_pads
    ]
    # "Swapped" only means something when both sides use the same two numbers.
    swapped = set(mapping) == set(mapping.values()) and any(src != dst for src, dst in mapping.items())
    return PolarityMatch(jlc_pads=renumbered, source=source, kind=kind, swapped=swapped)
