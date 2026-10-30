"""Decide each part's CPL position and rotation: solved, estimated, KiCad fallback, or manual override.

Manual override fields (footprint properties, case-insensitive):

``JLCPCB Rotation Offset``
    Degrees added to the CPL ``Rotation`` value of KiCad's own orientation,
    e.g. ``90`` or ``-90``.  On the top side a positive offset turns the part
    counter-clockwise as viewed from the top; on the bottom side JLCPCB
    counts rotation the other way, so it turns the part clockwise.
``JLCPCB Position Offset``
    ``dx, dy`` in millimetres in CPL coordinates (X right, Y up, viewed from
    the top, on both sides), e.g. ``0.5, -0.25``.

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
from .polarity import match_polarity
from .solver import (
    LOW,
    MATCHED_BY_POSITION,
    MIRRORED,
    MIRRORED_ARRAY,
    MIRRORED_BY_POSITION,
    FitResult,
    cap_confidence,
    describe_flags,
    match_pads_by_position,
    solve_placement,
)

ROTATION_OFFSET_FIELD = "JLCPCB Rotation Offset"
POSITION_OFFSET_FIELD = "JLCPCB Position Offset"

SOLVED = "solved"
ESTIMATED = "estimated"  # loose fit: orientation rounded to the nearest 90°, LOW confidence
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
    override_errors: List[str] = field(default_factory=list)  # unreadable override fields (ignored)

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
    """Read the manual override fields.  Returns (override or None, error messages).

    The override is None unless at least one field could be read, so a typo
    never replaces the solver with KiCad's pose.
    """
    rot_text = fp.get_property(ROTATION_OFFSET_FIELD)
    pos_text = fp.get_property(POSITION_OFFSET_FIELD)
    if not rot_text and not pos_text:
        return None, []

    errors: List[str] = []
    ov = ManualOverride()
    parsed = False
    if rot_text:
        try:
            ov.rotation_offset = _parse_number(rot_text)
            parsed = True
        except ValueError:
            errors.append(f'{ROTATION_OFFSET_FIELD} "{rot_text}" is not a number; ignored')
    if pos_text:
        parts = [p for p in re.split(r"[,;\s]+", pos_text.strip()) if p and p.lower() != "mm"]
        try:
            if len(parts) != 2:
                raise ValueError(pos_text)
            ov.dx, ov.dy = _parse_number(parts[0]), _parse_number(parts[1])
            parsed = True
        except ValueError:
            errors.append(f'{POSITION_OFFSET_FIELD} "{pos_text}" must be "dx, dy" in mm; ignored')
    return (ov if parsed else None), errors


def kicad_placement(fp: FootprintRecord, reason: str) -> Placement:
    """Fallback: the footprint's own KiCad pose, flagged unverified."""
    (x, y), phi = kicad_pose_to_cpl(fp.x, fp.y, fp.orientation)
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


# Resistor and capacitor arrays are isolated elements, each pad k joined to the
# pad opposite it, so mirror-image pad numbering doesn't change any connection.
_ARRAY_RE = re.compile(r"^(?:[RC]_Array_|(?:RES|CAP)-ARRAY)", re.IGNORECASE)


def is_isolated_array(footprint_name: str, jlc_title: str) -> bool:
    return bool(_ARRAY_RE.match(footprint_name.split(":")[-1]) or _ARRAY_RE.match(jlc_title or ""))


def solve_against_jlc(fp: FootprintRecord, jlc_fp: JlcFootprint) -> FitResult:
    """Fit JLC's footprint onto *fp*, pairing pads by polarity, then number, then position.

    When JLC numbers the pads in mirror image of the board footprint, no
    rotation pairs every number, so the pads are matched by position instead
    (pin 1 nearest pin 1) rather than falling back to KiCad's own angle.
    """
    polarity = match_polarity(fp.pads, jlc_fp.pads, jlc_fp.pin_names, jlc_fp.symbol_error, fp.footprint)
    fit = solve_placement(fp.pads, polarity.jlc_pads, fp.side, kicad_orientation=fp.orientation, polarity=polarity)
    if not fit.usable:
        mirrored = MIRRORED in fit.flags
        renumbered = match_pads_by_position(
            fp.pads, jlc_fp.pads, fp.side, kicad_orientation=fp.orientation, break_ties=mirrored
        )
        if renumbered is not None:
            by_position = solve_placement(fp.pads, renumbered, fp.side, kicad_orientation=fp.orientation)
            if by_position.usable:
                if not mirrored:
                    flag = MATCHED_BY_POSITION
                elif is_isolated_array(fp.footprint, jlc_fp.title):
                    flag = MIRRORED_ARRAY
                else:
                    flag = MIRRORED_BY_POSITION
                cap_confidence(by_position, flag)
                fit = by_position
    return fit


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
        fit = solve_against_jlc(fp, jlc_fp)

    override, override_errors = parse_override(fp)
    if override is not None:
        placement = _manual_placement(fp, override, fit)
    else:
        placement = _automatic_placement(fp, fit, fetch_error)
    placement.override_errors = override_errors
    return placement


def _manual_placement(fp: FootprintRecord, override: ManualOverride, fit: Optional[FitResult]) -> Placement:
    """KiCad's own pose plus the override offsets."""
    placement = kicad_placement(fp, "manual override")
    base_rotation = placement.rotation
    placement.x += override.dx
    placement.y += override.dy
    placement.rotation = normalize_angle(base_rotation + override.rotation_offset)
    placement.source = MANUAL
    placement.confidence = MANUAL_CONFIDENCE
    placement.fit = fit
    placement.notes = [
        f"Manual override: rotation {base_rotation:g}° (KiCad) {override.rotation_offset:+g}° = "
        f"{placement.rotation:g}°, position offset ({override.dx:+g}, {override.dy:+g}) mm"
    ]
    if fit is not None and fit.usable:
        placement.notes.append(
            f"Solver suggests rotation {cpl_rotation(fit.angle, fp.side):g}° at "
            f"({fit.origin[0]:.3f}, {fit.origin[1]:.3f})"
        )
    return placement


def _automatic_placement(fp: FootprintRecord, fit: Optional[FitResult], fetch_error: str) -> Placement:
    """Solved, estimated or KiCad-fallback placement."""
    if fit is None:
        reason = fetch_error or "no LCSC part number"
        return kicad_placement(fp, reason)

    if not fit.usable and fit.estimate_angle is not None and MIRRORED not in fit.flags:
        placement = Placement(
            reference=fp.reference,
            side=fp.side,
            x=fit.estimate_origin[0],
            y=fit.estimate_origin[1],
            rotation=cpl_rotation(fit.estimate_angle, fp.side),
            source=ESTIMATED,
            confidence=LOW,
            fit=fit,
            notes=[f"pads differ from JLC's footprint (RMS {fit.estimate_rms:.2f} mm)"],
        )
        placement.notes.extend(describe_flags(fit.flags))
        return placement

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
