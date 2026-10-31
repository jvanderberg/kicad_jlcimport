"""Plain data records shared by the production-export modules.

Nothing here knows about ``pcbnew``: the board adapter (or the
``.kicad_pcb`` reader) converts a board into :class:`FootprintRecord` objects
and everything downstream works on these.
"""

import re
from dataclasses import dataclass, field
from functools import cached_property
from typing import Dict, List, Optional

from .conventions import TOP

# Footprint/symbol field names that may carry an LCSC / JLCPCB part number,
# checked in order (case-insensitive).  "LCSC" is what JLCImport writes.
LCSC_FIELD_NAMES = (
    "LCSC",
    "LCSC Part",
    "LCSC Part #",
    "LCSC Part Number",
    "LCSC#",
    "JLCPCB Part #",
    "JLCPCB Part",
    "JLCPCB",
    "JLC",
    "JLC Part",
)

_LCSC_RE = re.compile(r"^C[0-9]{1,12}$")


@dataclass
class PadRecord:
    """A pad with its number and centre position.

    For board footprints ``x``/``y`` are board coordinates (mm, Y down).
    For JLC/EasyEDA footprints they are footprint-local (mm, Y down).
    ``function`` is the schematic pin name KiCad attaches to a board pad
    (e.g. ``K``); empty when unknown.  ``width`` / ``height`` are the pad's
    size in its own frame and ``drill`` its hole diameter (mm; 0 for an SMD
    pad or when unknown).
    """

    number: str
    x: float
    y: float
    function: str = ""
    width: float = 0.0
    height: float = 0.0
    drill: float = 0.0


@dataclass
class FootprintRecord:
    """Everything production export needs to know about one placed footprint."""

    reference: str
    value: str
    footprint: str  # footprint name without library nickname
    x: float  # board mm, Y down
    y: float
    orientation: float  # degrees, CCW as viewed from top
    side: str = TOP
    pads: List[PadRecord] = field(default_factory=list)
    properties: Dict[str, str] = field(default_factory=dict)
    dnp: bool = False
    exclude_from_bom: bool = False
    exclude_from_pos: bool = False

    def get_property(self, name: str) -> str:
        """Case-insensitive property lookup; returns '' when absent."""
        lname = name.lower()
        for key, val in self.properties.items():
            if key.lower() == lname:
                return (val or "").strip()
        return ""

    # Cached: records are not modified after construction, and these are read
    # several times per footprint for every report.
    @cached_property
    def raw_lcsc(self) -> str:
        """The first non-empty LCSC-like field value, unvalidated."""
        for name in LCSC_FIELD_NAMES:
            val = self.get_property(name)
            if val:
                return val
        return ""

    @cached_property
    def lcsc(self) -> Optional[str]:
        """Normalized LCSC part number, or None if absent/invalid."""
        return normalize_lcsc(self.raw_lcsc)

    @property
    def in_bom(self) -> bool:
        return not self.dnp and not self.exclude_from_bom

    @property
    def in_cpl(self) -> bool:
        """Parts JLCPCB will place: in the BOM and not excluded from position files."""
        return self.in_bom and not self.exclude_from_pos


def normalize_lcsc(value: str) -> Optional[str]:
    """Return ``C<digits>`` for a valid LCSC number, else None."""
    v = (value or "").strip().upper()
    if not v:
        return None
    if v[0].isdigit():
        v = "C" + v
    return v if _LCSC_RE.match(v) else None


def natural_key(text: str):
    """Sort key that orders R2 before R10."""
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", text)]
