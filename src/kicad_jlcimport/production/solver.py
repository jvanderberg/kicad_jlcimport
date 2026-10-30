"""Solve a part's CPL rotation and centre by fitting JLC's footprint onto the board pads.

JLCPCB places a part using the orientation and origin of its own EasyEDA
footprint.  The footprint on the board may use a different 0° orientation and
origin, so we compute the rigid transform that maps JLC's footprint onto the
actual pads:

1. Pads are paired by number.  Pads sharing a number (thermal pads split
   into segments, several GND pins, …) are reduced to the centroid of their
   group on each side, so one group contributes one point.
2. JLC's local points ``q`` (Y up, mirrored for bottom-side parts — see
   :mod:`.conventions`) and board points ``p`` (CPL frame) are fitted with
   the closed-form 2D least-squares rigid transform::

       qc, pc = centroids;  a = q - qc;  b = p - pc
       phi = atan2(sum(a x b), sum(a . b))
       t   = pc - R(phi) qc

   ``t`` is where JLC's footprint origin lands; ``phi`` is the rotation.
3. The angle snaps to the nearest 90° step (or KiCad's own orientation plus a
   90° step) when within :attr:`Tolerances.snap_deg`, and the residual is
   recomputed with the snapped angle.
4. The opposite-handedness fit (mirror applied/removed) is also computed; if
   it fits clearly better, the pad numbering is mirrored and we refuse to
   guess.
5. Whatever the residual, the raw angle rounded to the nearest candidate is
   kept as an *estimate*.  A fit that is rejected (high residual, angle not
   near a 90° step) is still usually right about the orientation, and far
   more often right than KiCad's own angle; the placement uses it at LOW
   confidence with a warning.

When the pad numbers don't line up at all (JLC renumbered its footprint,
say), :func:`match_pads_by_position` pairs pads by position instead.  It
only accepts a match that fits in exactly one orientation.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .conventions import Point, board_to_cpl, local_to_up, normalize_angle, rotate, side_mirror
from .models import PadRecord
from .polarity import (
    BY_CONVENTION,
    BY_FOOTPRINT_NAME,
    BY_NAMES,
    JLC_UNNAMED,
    NAMES_UNAVAILABLE,
    NOT_POLARIZED,
    UNRESOLVED,
    PolarityMatch,
)

# Flag codes.  Severity is decided by FitResult.confidence; FLAG_TEXT holds the
# human-readable explanation shown in reports.
TOO_FEW_PADS = "too_few_pads"
DEGENERATE = "orientation_undetermined"
PAD_MISMATCH = "pad_mismatch"
GROUP_SIZE_MISMATCH = "group_size_mismatch"
GEOMETRY_DIFFERS = "geometry_differs"
HIGH_RESIDUAL = "high_residual"
MIRRORED = "mirrored_numbering"
POLARITY_BY_NUMBERING = "polarity_by_numbering"
NOT_SNAPPED = "angle_not_snapped"
MATCHED_BY_POSITION = "matched_by_position"
POLARITY_BY_PIN_NAMES = "polarity_by_pin_names"
POLARITY_BY_CONVENTION = "polarity_by_convention"
POLARITY_SWAPPED = "polarity_swapped"
POLARITY_UNRESOLVED = "polarity_unresolved"
POLARITY_NAMES_UNAVAILABLE = "polarity_names_unavailable"
POLARITY_BY_FOOTPRINT_NAME = "polarity_by_footprint_name"
POLARITY_JLC_UNNAMED = "polarity_jlc_unnamed"

FLAG_TEXT = {
    TOO_FEW_PADS: "fewer than 2 pad numbers in common with the JLC footprint",
    DEGENERATE: "paired pads do not determine an orientation",
    PAD_MISMATCH: "pad numbers differ between board and JLC footprints",
    GROUP_SIZE_MISMATCH: "a repeated pad number has a different pad count on each footprint",
    GEOMETRY_DIFFERS: "pad positions differ slightly from the JLC footprint",
    HIGH_RESIDUAL: "pads do not match the JLC footprint (wrong package?)",
    MIRRORED: "pad numbering is mirrored relative to the JLC footprint",
    POLARITY_BY_NUMBERING: "symmetric 2-terminal part: polarity relies on pad numbering only",
    NOT_SNAPPED: "solved angle is not a multiple of 90° from KiCad's; rounded to the nearest step",
    MATCHED_BY_POSITION: "pad numbers differ from the JLC footprint; pads matched by position",
    POLARITY_BY_PIN_NAMES: "polarity matched by pin name (anode/cathode or +/-)",
    POLARITY_BY_FOOTPRINT_NAME: "polarity from the footprint name (_AK / _KA)",
    POLARITY_BY_CONVENTION: (
        "no pin names on the board pads; assumed KiCad's convention (diode/LED: pad 1 = cathode, "
        "polarized capacitor: pad 1 = +)"
    ),
    POLARITY_SWAPPED: "JLC numbers this part's pads the other way round; paired by polarity instead",
    POLARITY_UNRESOLVED: "polarized part, but the board pads don't say which is which; check its orientation",
    POLARITY_NAMES_UNAVAILABLE: (
        "JLC's pin names could not be fetched, so polarity relies on pad numbering; check its orientation"
    ),
    POLARITY_JLC_UNNAMED: "polarized part, but JLC's symbol doesn't name its pins; paired by pad number",
}

_POLARITY_FLAG = {
    BY_NAMES: POLARITY_BY_PIN_NAMES,
    BY_FOOTPRINT_NAME: POLARITY_BY_FOOTPRINT_NAME,
    BY_CONVENTION: POLARITY_BY_CONVENTION,
    UNRESOLVED: POLARITY_UNRESOLVED,
    NAMES_UNAVAILABLE: POLARITY_NAMES_UNAVAILABLE,
    JLC_UNNAMED: POLARITY_JLC_UNNAMED,
}

HIGH = "high"
MEDIUM = "medium"
LOW = "low"


@dataclass
class Tolerances:
    """Thresholds used by :func:`solve_placement` (millimetres / degrees)."""

    high_rms: float = 0.05  # residual for a confident match
    medium_rms: float = 0.25  # land-pattern differences that don't move the centre/rotation
    mirror_margin: float = 0.05  # how much better the mirrored fit must be to flag it
    snap_deg: float = 1.0
    min_spread: float = 0.01  # points closer than this don't determine an orientation
    symmetry_tol: float = 0.05
    min_matched_fraction: float = 0.5


DEFAULT_TOLERANCES = Tolerances()


@dataclass
class FitResult:
    """Outcome of fitting a JLC footprint onto a board footprint."""

    angle: float = 0.0  # placement-model angle (degrees, [0, 360))
    raw_angle: float = 0.0  # angle before snapping
    origin: Point = (0.0, 0.0)  # JLC footprint origin in CPL coordinates
    rms: float = 0.0
    max_error: float = 0.0
    pairs_used: int = 0
    alt_rms: Optional[float] = None  # residual of the opposite-handedness fit
    # Raw angle rounded to the nearest candidate, with its origin and residual;
    # set whenever an orientation could be computed (see module docstring, step 5).
    estimate_angle: Optional[float] = None
    estimate_origin: Point = (0.0, 0.0)
    estimate_rms: float = 0.0
    snapped: bool = False
    confidence: str = LOW
    flags: List[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """True when the solved placement should be written to the CPL."""
        return self.confidence in (HIGH, MEDIUM)


def normalize_pad_number(number: str) -> str:
    return (number or "").strip().upper()


def group_pads(pads: Sequence[PadRecord]) -> Dict[str, List[Point]]:
    """Group pad positions by normalized number, skipping unnumbered pads."""
    groups: Dict[str, List[Point]] = {}
    for pad in pads:
        num = normalize_pad_number(pad.number)
        if num:
            groups.setdefault(num, []).append((pad.x, pad.y))
    return groups


def _centroid(points: Sequence[Point]) -> Point:
    n = float(len(points))
    return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n)


def _fit_rotation(q: Sequence[Point], p: Sequence[Point]) -> Tuple[float, Point, Point, float, float]:
    """Closed-form least-squares rotation.  Returns (deg, qc, pc, spread_q, spread_p)."""
    qc = _centroid(q)
    pc = _centroid(p)
    s_cos = s_sin = spread_q = spread_p = 0.0
    for (qx, qy), (px, py) in zip(q, p):
        ax, ay = qx - qc[0], qy - qc[1]
        bx, by = px - pc[0], py - pc[1]
        s_cos += ax * bx + ay * by
        s_sin += ax * by - ay * bx
        spread_q += ax * ax + ay * ay
        spread_p += bx * bx + by * by
    return math.degrees(math.atan2(s_sin, s_cos)), qc, pc, spread_q, spread_p


def _residuals(q: Sequence[Point], p: Sequence[Point], deg: float, t: Point) -> Tuple[float, float]:
    errs = []
    for qi, pi in zip(q, p):
        rx, ry = rotate(qi, deg)
        errs.append(math.hypot(rx + t[0] - pi[0], ry + t[1] - pi[1]))
    rms = math.sqrt(sum(e * e for e in errs) / len(errs))
    return rms, max(errs)


def _translation(qc: Point, pc: Point, deg: float) -> Point:
    rx, ry = rotate(qc, deg)
    return (pc[0] - rx, pc[1] - ry)


def _is_collinear(points: Sequence[Point]) -> bool:
    """True when the points' covariance is (nearly) rank one."""
    cx, cy = _centroid(points)
    sxx = sum((x - cx) ** 2 for x, _ in points)
    syy = sum((y - cy) ** 2 for _, y in points)
    sxy = sum((x - cx) * (y - cy) for x, y in points)
    tr = sxx + syy
    if tr <= 0:
        return True
    det = sxx * syy - sxy * sxy
    disc = math.sqrt(max(tr * tr / 4 - det, 0.0))
    small = tr / 2 - disc
    return small / tr < 1e-4


def _angle_delta(a: float, b: float) -> float:
    d = math.fmod(a - b, 360.0)
    if d > 180:
        d -= 360
    elif d < -180:
        d += 360
    return abs(d)


def _nearest_candidate(angle: float, kicad_orientation: Optional[float]) -> float:
    candidates = [k * 90.0 for k in range(4)]
    if kicad_orientation is not None:
        candidates += [normalize_angle(kicad_orientation + k * 90.0) for k in range(4)]
    return min(candidates, key=lambda c: _angle_delta(angle, c))


def _snap(angle: float, kicad_orientation: Optional[float], tol: float) -> Tuple[float, bool]:
    best = _nearest_candidate(angle, kicad_orientation)
    if _angle_delta(angle, best) <= tol:
        return best, True
    return angle, False


def _is_point_symmetric(points: Sequence[Point], tol: float) -> bool:
    """True when the point set is invariant under a 180° turn about its centroid."""
    cx, cy = _centroid(points)
    for x, y in points:
        mx, my = 2 * cx - x, 2 * cy - y
        if not any(math.hypot(mx - ox, my - oy) <= tol for ox, oy in points):
            return False
    return True


def solve_placement(
    board_pads: Sequence[PadRecord],
    jlc_pads: Sequence[PadRecord],
    side: str,
    kicad_orientation: Optional[float] = None,
    tol: Tolerances = DEFAULT_TOLERANCES,
    polarity: Optional[PolarityMatch] = None,
) -> FitResult:
    """Fit JLC's footprint (local pads) onto a board footprint's pads.

    *board_pads* are in board coordinates (Y down), *jlc_pads* in the JLC
    footprint's local frame (Y down).  *side* selects the bottom-side mirror.
    *kicad_orientation* adds KiCad's own angle (plus 90° steps) as snapping
    candidates, which helps parts placed at odd angles.  *polarity* says how
    a two-pad part's pads were paired (see :mod:`.polarity`); *jlc_pads*
    must already be renumbered accordingly.
    """
    polarity_source = polarity.source if polarity is not None else NOT_POLARIZED
    result = FitResult()
    board_groups = group_pads(board_pads)
    jlc_groups = group_pads(jlc_pads)
    common = sorted(set(board_groups) & set(jlc_groups))
    only_one_side = set(board_groups) ^ set(jlc_groups)

    if only_one_side:
        result.flags.append(PAD_MISMATCH)
    if any(len(board_groups[n]) != len(jlc_groups[n]) for n in common):
        result.flags.append(GROUP_SIZE_MISMATCH)

    result.pairs_used = len(common)
    if len(common) < 2:
        result.flags.append(TOO_FEW_PADS)
        return result

    q = [side_mirror(local_to_up(*_centroid(jlc_groups[n])), side) for n in common]
    p = [board_to_cpl(*_centroid(board_groups[n])) for n in common]

    raw, qc, pc, spread_q, spread_p = _fit_rotation(q, p)
    min_spread_sq = tol.min_spread * tol.min_spread
    if spread_q < min_spread_sq or spread_p < min_spread_sq:
        result.flags.append(DEGENERATE)
        return result

    raw = normalize_angle(raw)
    angle, snapped = _snap(raw, kicad_orientation, tol.snap_deg)
    origin = _translation(qc, pc, angle)
    rms, max_err = _residuals(q, p, angle, origin)

    result.estimate_angle = normalize_angle(_nearest_candidate(raw, kicad_orientation))
    result.estimate_origin = _translation(qc, pc, result.estimate_angle)
    result.estimate_rms, _ = _residuals(q, p, result.estimate_angle, result.estimate_origin)

    result.raw_angle = raw
    result.angle = normalize_angle(angle)
    result.snapped = snapped
    result.origin = origin
    result.rms = rms
    result.max_error = max_err
    if not snapped:
        result.flags.append(NOT_SNAPPED)

    # Opposite handedness: undo the bottom mirror (or apply it on top).
    if not _is_collinear(q):
        q_alt = [(x, -y) for x, y in q]
        alt_raw, aqc, apc, _, _ = _fit_rotation(q_alt, p)
        alt_rms, _ = _residuals(q_alt, p, alt_raw, _translation(aqc, apc, alt_raw))
        result.alt_rms = alt_rms

    matched_fraction = len(common) / float(max(len(board_groups), len(jlc_groups)))
    jlc_points = [(pad.x, pad.y) for pad in jlc_pads if normalize_pad_number(pad.number)]
    if polarity_source != NOT_POLARIZED:
        result.flags.append(_POLARITY_FLAG[polarity_source])
        if polarity is not None and polarity.swapped:
            result.flags.append(POLARITY_SWAPPED)
    elif len(jlc_groups) == 2 and _is_point_symmetric(jlc_points, tol.symmetry_tol):
        result.flags.append(POLARITY_BY_NUMBERING)

    if result.alt_rms is not None and result.alt_rms + tol.mirror_margin < rms and result.alt_rms <= tol.medium_rms:
        result.flags.append(MIRRORED)
        result.confidence = LOW
    elif rms > tol.medium_rms:
        result.flags.append(HIGH_RESIDUAL)
        result.confidence = LOW
    elif not snapped:
        result.confidence = LOW
    elif matched_fraction < tol.min_matched_fraction:
        result.confidence = LOW
    elif rms > tol.high_rms:
        result.flags.append(GEOMETRY_DIFFERS)
        result.confidence = MEDIUM
    elif PAD_MISMATCH in result.flags:
        result.confidence = MEDIUM
    else:
        result.confidence = HIGH
    # JLC's own pin names don't always match how it places a part, so no
    # polarized two-pad result is HIGH.
    if polarity_source != NOT_POLARIZED and result.confidence == HIGH:
        result.confidence = MEDIUM
    return result


def cap_confidence(result: FitResult, flag: str) -> None:
    """Add *flag* and lower a HIGH result to MEDIUM (it rests on an assumption)."""
    if flag not in result.flags:
        result.flags.append(flag)
    if result.confidence == HIGH:
        result.confidence = MEDIUM


def _assign_nearest(q: Sequence[Point], p: Sequence[Point], deg: float, t: Point) -> Optional[Tuple[List[int], float]]:
    """Map each transformed q to its nearest p.  None unless the mapping is one-to-one."""
    assignment: List[int] = []
    sq = 0.0
    for qi in q:
        rx, ry = rotate(qi, deg)
        x, y = rx + t[0], ry + t[1]
        dists = [math.hypot(x - px, y - py) for px, py in p]
        j = min(range(len(p)), key=dists.__getitem__)
        assignment.append(j)
        sq += dists[j] ** 2
    if len(set(assignment)) != len(assignment):
        return None
    return assignment, math.sqrt(sq / len(q))


def match_pads_by_position(
    board_pads: Sequence[PadRecord],
    jlc_pads: Sequence[PadRecord],
    side: str,
    kicad_orientation: Optional[float] = None,
    tol: Tolerances = DEFAULT_TOLERANCES,
) -> Optional[List[PadRecord]]:
    """JLC's pads renamed with the board's pad numbers, matched by position.

    For footprints with the same pads but different numbering.  Pad centroids
    are aligned and every 90° step (and KiCad's angle plus 90° steps) is tried;
    each JLC pad takes the number of the nearest board pad.  Returns None when
    the pad counts differ, nothing fits within ``tol.medium_rms``, or the
    pattern fits in more than one orientation with a different numbering, in
    which case position alone cannot tell which way round the part goes.
    """
    board = [pad for pad in board_pads if normalize_pad_number(pad.number)]
    jlc = [pad for pad in jlc_pads if normalize_pad_number(pad.number)]
    if len(board) != len(jlc) or len(board) < 3:
        return None
    p = [board_to_cpl(pad.x, pad.y) for pad in board]
    q = [side_mirror(local_to_up(pad.x, pad.y), side) for pad in jlc]
    pc, qc = _centroid(p), _centroid(q)

    angles = [k * 90.0 for k in range(4)]
    if kicad_orientation is not None:
        angles += [normalize_angle(kicad_orientation + k * 90.0) for k in range(4)]
    fits = []
    for angle in sorted({round(a, 6) for a in angles}):
        found = _assign_nearest(q, p, angle, _translation(qc, pc, angle))
        if found is not None and found[1] <= tol.medium_rms:
            numbers = tuple(normalize_pad_number(board[j].number) for j in found[0])
            fits.append((found[1], numbers, found[0]))
    if not fits or len({numbers for _, numbers, _ in fits}) > 1:
        return None
    _, _, assignment = min(fits)
    return [PadRecord(number=board[j].number, x=pad.x, y=pad.y) for pad, j in zip(jlc, assignment)]


def describe_flags(flags: Sequence[str]) -> List[str]:
    return [FLAG_TEXT.get(f, f) for f in flags]
