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

import itertools
import re
from dataclasses import dataclass, field, replace
from typing import List, Optional, Tuple

from ..kicad.library import sanitize_name
from .conventions import cpl_rotation, kicad_pose_to_cpl, normalize_angle
from .jlc_footprints import JlcFootprint
from .models import FootprintRecord, PadRecord
from .polarity import match_polarity
from .solver import (
    DEFAULT_TOLERANCES,
    GROUP_SIZE_MISMATCH,
    LOW,
    MATCHED_BY_POSITION,
    MIRRORED,
    MIRRORED_ARRAY,
    MIRRORED_BY_POSITION,
    POSITION_CENTRED,
    POSITION_ESTIMATE,
    POSITION_LOOSE,
    FitResult,
    cap_confidence,
    centred_origin,
    describe_flags,
    match_pads_by_position,
    normalize_pad_number,
    pair_unmatched_by_position,
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
    When most numbers pair but a few pads are numbered differently (a tab
    numbered like a pin), just those pads are paired by position.
    """
    polarity = match_polarity(
        fp.pads, jlc_fp.pads, jlc_fp.pin_names, jlc_fp.symbol_error, fp.footprint, jlc_marks=jlc_fp.pad_marks
    )
    polarity.own_footprint = own_footprint_name(fp.footprint, jlc_fp)
    fit = solve_placement(fp.pads, polarity.jlc_pads, fp.side, kicad_orientation=fp.orientation, polarity=polarity)
    if fit.usable:
        return fit
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
            return by_position
    if mirrored:
        return fit
    paired = pair_unmatched_by_position(fp.pads, polarity.jlc_pads, fp.side)
    if paired is not None:
        by_pairing = solve_placement(fp.pads, paired, fp.side, kicad_orientation=fp.orientation, polarity=polarity)
        if by_pairing.usable:
            cap_confidence(by_pairing, MATCHED_BY_POSITION)
            return by_pairing
    if renumbered is None:
        _estimate_by_position(fp, jlc_fp, fit)
    return fit


_LOOSE_POSITION_MATCH = replace(DEFAULT_TOLERANCES, medium_rms=1.5)


def _estimate_by_position(fp: FootprintRecord, jlc_fp: JlcFootprint, fit: FitResult) -> None:
    """Estimate by matching pads by position where the by-number estimate can't be trusted.

    That is when there is no by-number estimate, or it pairs a pad group's
    centre with a single pad (a tact switch's pads 1,1,2,2 against JLC's 1-4,
    which lands millimetres off).  Same-size patterns match first; then a loose
    match, ranked by fit, centres a differently sized pattern of the same shape;
    up to two pads that only one footprint has (a switch's centre pad) are left
    out.  Failing that, JLC's pads are centred on the board's.
    """
    if fit.estimate_angle is not None and GROUP_SIZE_MISMATCH not in fit.flags:
        return  # the by-number estimate stands
    board = [pad for pad in fp.pads if normalize_pad_number(pad.number)]
    jlc = [pad for pad in jlc_fp.pads if normalize_pad_number(pad.number)]
    best = None
    for board_sub, jlc_sub in _matching_subsets(board, jlc):
        for tol, flag, rank_by_fit in (
            (DEFAULT_TOLERANCES, POSITION_ESTIMATE, False),
            (_LOOSE_POSITION_MATCH, POSITION_LOOSE, True),
        ):
            renumbered = match_pads_by_position(
                board_sub,
                jlc_sub,
                fp.side,
                kicad_orientation=fp.orientation,
                tol=tol,
                break_ties=True,
                rank_by_fit=rank_by_fit,
            )
            if renumbered is None:
                continue
            solved = solve_placement(board_sub, renumbered, fp.side, kicad_orientation=fp.orientation)
            if solved.snapped:
                found = (solved.rms, solved.angle, solved.origin)
            elif solved.estimate_angle is not None:
                found = (solved.estimate_rms, solved.estimate_angle, solved.estimate_origin)
            else:
                continue
            if len(board_sub) != len(board) or len(jlc_sub) != len(jlc):
                flag = POSITION_LOOSE
            if best is None or found[0] < best[0][0]:
                best = (found, flag)
            break  # a same-size match beats a loose one for this pairing
    if best is not None:
        (rms, angle, origin), flag = best
        fit.estimate_angle, fit.estimate_origin, fit.estimate_rms = angle, origin, rms
        fit.flags.append(flag)
    elif fit.estimate_angle is not None and len(board) == len(jlc):
        fit.estimate_origin = centred_origin(board, jlc, fp.side, fit.estimate_angle)
        fit.flags.append(POSITION_CENTRED)


_MAX_UNPAIRED = 2  # pads only one footprint has, left out of a loose position match
_MAX_SUBSET_PADS = 24  # above this, don't try leaving pads out


def _matching_subsets(board: List[PadRecord], jlc: List[PadRecord]):
    """(board pads, JLC pads) of equal count: both whole, or the larger one with up to two pads left out."""
    extra = len(jlc) - len(board)
    if extra == 0:
        yield board, jlc
        return
    larger = jlc if extra > 0 else board
    if abs(extra) > _MAX_UNPAIRED or len(larger) > _MAX_SUBSET_PADS:
        return
    for left_out in itertools.combinations(range(len(larger)), abs(extra)):
        kept = [pad for i, pad in enumerate(larger) if i not in left_out]
        yield (board, kept) if extra > 0 else (kept, jlc)


def own_footprint_name(footprint: str, jlc_fp: JlcFootprint) -> bool:
    """True when *footprint* is named the way JLCImport names JLC's own footprint for this part.

    JLCImport names an imported footprint after the part (JLC's symbol title,
    or its first word, the part number), or after JLC's footprint title when
    the part has no symbol, optionally with the LCSC number appended.
    """
    name = (footprint or "").split(":")[-1].strip().lower()
    titles = [jlc_fp.symbol_title, (jlc_fp.symbol_title or "").split(" ")[0], jlc_fp.title]
    for title in titles:
        if not title:
            continue
        base = sanitize_name(title).lower()
        if name in (base, f"{base}_{jlc_fp.lcsc}".lower()):
            return True
    return False


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
