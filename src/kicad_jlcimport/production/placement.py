"""Decide each part's CPL position and rotation: solved, KiCad fallback, or manual override.

Manual override fields (footprint properties, case-insensitive):

``JLCPCB Rotation Offset``
    Degrees, counter-clockwise as viewed from the top, e.g. ``90`` or ``-90``.
``JLCPCB Position Offset``
    ``dx, dy`` in millimetres in CPL coordinates (X right, Y up, viewed from
    the top), e.g. ``0.5, -0.25``.

When either field is present the solver result is **not** used for the CPL:
the row is KiCad's own position/rotation plus the offsets.  This keeps an
override stable regardless of network access or JLC footprint changes.  A
rotation offset of ``0`` is therefore a way to say "trust KiCad for this
part".
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .conventions import cpl_rotation, kicad_pose_to_cpl, normalize_angle
from .jlc_footprints import JlcFootprint
from .models import FootprintRecord
from .solver import FitResult, describe_flags, solve_placement

ROTATION_OFFSET_FIELD = "JLCPCB Rotation Offset"
POSITION_OFFSET_FIELD = "JLCPCB Position Offset"

SOLVED = "solved"
KICAD = "kicad"  # fallback: KiCad's own pose, unverified
MANUAL = "manual"

MANUAL_CONFIDENCE = "manual"
UNVERIFIED = "unverified"


@dataclass
class ManualOverride:
    rotation_offset: float = 0.0
    dx: float = 0.0
    dy: float = 0.0


@dataclass
class Placement:
    """Final CPL values for one footprint and how they were obtained."""

    reference: str
    side: str
    x: float  # CPL mm (Y up)
    y: float
    rotation: float  # value for the CPL Rotation column
    source: str
    confidence: str  # HIGH / MEDIUM / LOW from the solver, MANUAL_CONFIDENCE, or UNVERIFIED
    fit: Optional[FitResult] = None
    notes: List[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.source in (SOLVED, MANUAL)


_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)"


def _parse_number(text: str) -> float:
    m = re.fullmatch(rf"\s*({_NUM})\s*(?:°|deg|degrees|mm)?\s*", text, re.IGNORECASE)
    if not m:
        raise ValueError(text)
    return float(m.group(1))


def parse_override(fp: FootprintRecord) -> Tuple[Optional[ManualOverride], List[str]]:
    """Read the manual override fields.  Returns (override or None, error messages)."""
    rot_text = fp.get_property(ROTATION_OFFSET_FIELD)
    pos_text = fp.get_property(POSITION_OFFSET_FIELD)
    if not rot_text and not pos_text:
        return None, []

    errors: List[str] = []
    ov = ManualOverride()
    if rot_text:
        try:
            ov.rotation_offset = _parse_number(rot_text)
        except ValueError:
            errors.append(f'{ROTATION_OFFSET_FIELD} "{rot_text}" is not a number; ignored')
    if pos_text:
        parts = [p for p in re.split(r"[,;\s]+", pos_text.strip()) if p and p.lower() != "mm"]
        try:
            if len(parts) != 2:
                raise ValueError(pos_text)
            ov.dx, ov.dy = _parse_number(parts[0]), _parse_number(parts[1])
        except ValueError:
            errors.append(f'{POSITION_OFFSET_FIELD} "{pos_text}" must be "dx, dy" in mm; ignored')
    return ov, errors


def kicad_placement(fp: FootprintRecord, reason: str) -> Placement:
    """Fallback: the footprint's own KiCad pose, flagged unverified."""
    (x, y), phi = kicad_pose_to_cpl(fp.x, fp.y, fp.orientation, fp.side)
    return Placement(
        reference=fp.reference,
        side=fp.side,
        x=x,
        y=y,
        rotation=cpl_rotation(phi, fp.side),
        source=KICAD,
        confidence=UNVERIFIED,
        notes=[f"Unverified: {reason}; using KiCad position/rotation"],
    )


def compute_placement(
    fp: FootprintRecord,
    jlc_fp: Optional[JlcFootprint] = None,
    fetch_error: str = "",
) -> Placement:
    """Placement for one footprint.

    *jlc_fp* is JLC's footprint for the part's LCSC number (None when there
    is no number or the fetch failed, in which case *fetch_error* explains
    why).
    """
    fit: Optional[FitResult] = None
    if jlc_fp is not None:
        fit = solve_placement(fp.pads, jlc_fp.pads, fp.side, kicad_orientation=fp.orientation)

    override, override_errors = parse_override(fp)
    if override is not None:
        placement = kicad_placement(fp, "manual override")
        placement.x += override.dx
        placement.y += override.dy
        placement.rotation = normalize_angle(placement.rotation + override.rotation_offset)
        placement.source = MANUAL
        placement.confidence = MANUAL_CONFIDENCE
        placement.fit = fit
        placement.notes = [
            f"Manual override: rotation {override.rotation_offset:+g}°, offset ({override.dx:+g}, {override.dy:+g}) mm"
        ]
        if fit is not None and fit.usable:
            placement.notes.append(
                f"Solver suggests rotation {cpl_rotation(fit.angle, fp.side):g}° at "
                f"({fit.origin[0]:.3f}, {fit.origin[1]:.3f})"
            )
        placement.notes.extend(override_errors)
        return placement

    if fit is None:
        reason = fetch_error or "no LCSC part number"
        return kicad_placement(fp, reason)

    if not fit.usable:
        placement = kicad_placement(fp, "pads could not be matched to the JLC footprint")
        placement.fit = fit
        placement.notes.extend(describe_flags(fit.flags))
        return placement

    placement = Placement(
        reference=fp.reference,
        side=fp.side,
        x=fit.origin[0],
        y=fit.origin[1],
        rotation=cpl_rotation(fit.angle, fp.side),
        source=SOLVED,
        confidence=fit.confidence,
        fit=fit,
    )
    placement.notes.extend(describe_flags(fit.flags))
    return placement
