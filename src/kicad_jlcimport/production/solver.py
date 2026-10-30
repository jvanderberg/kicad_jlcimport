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
   it fits clearly better, the pad numbering is mirrored and the fit is
   flagged.  :func:`match_pads_by_position` (with ``break_ties``) then
   places such a part by pad position instead.
5. Whatever the residual, the raw angle rounded to the nearest candidate is
   kept as an *estimate*.  A fit that is rejected (high residual, angle not
   near a 90° step) is still usually right about the orientation, and more
   often right than KiCad's own angle; the placement uses it at LOW
   confidence with a warning.

When the pad numbers don't line up at all (JLC renumbered its footprint,
say), :func:`match_pads_by_position` pairs pads by position instead.  It
only accepts a match that fits in exactly one orientation.  When most numbers
line up but a few pads are numbered differently (KiCad numbers a SOT-223 tab
like its middle pin, JLC gives it a number of its own),
:func:`pair_unmatched_by_position` pairs just those pads by position, with the
orientation fixed by the pads that do pair by number.

Pads at the same position (KiCad stacks a USB-C connector's A1 and B12 on one
land, which JLC draws as one pad ``A1B12``) are one *site* when pads are
matched by position.
"""

import math
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Sequence, Tuple

from .conventions import Point, board_to_cpl, local_to_up, normalize_angle, rotate, side_mirror
from .models import PadRecord, natural_key
from .polarity import (
    BY_CONVENTION,
    BY_FOOTPRINT_NAME,
    BY_NAMES,
    JLC_MARKS,
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
MIRRORED_BY_POSITION = "mirrored_by_position"
MIRRORED_ARRAY = "mirrored_array"
POLARITY_BY_PIN_NAMES = "polarity_by_pin_names"
POLARITY_BY_CONVENTION = "polarity_by_convention"
POLARITY_SWAPPED = "polarity_swapped"
POLARITY_UNRESOLVED = "polarity_unresolved"
POLARITY_NAMES_UNAVAILABLE = "polarity_names_unavailable"
POLARITY_BY_FOOTPRINT_NAME = "polarity_by_footprint_name"
POLARITY_JLC_UNNAMED = "polarity_jlc_unnamed"
POLARITY_BY_JLC_MARKS = "polarity_by_jlc_marks"
POLARITY_JLC_CONFLICT = "polarity_jlc_conflict"
POLARITY_ASSUMED = "polarity_assumed"
POSITION_ESTIMATE = "position_estimate"
POSITION_LOOSE = "position_loose"
POSITION_CENTRED = "position_centred"

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
    MIRRORED_BY_POSITION: (
        "JLC's footprint numbers the pads in mirror image of this one, so some pins can't line up whichever "
        "way it is turned; placed by pad position (pin 1 nearest pin 1). Check the footprint and JLC's preview"
    ),
    MIRRORED_ARRAY: (
        "JLC numbers this array's pads in mirror image, which doesn't change any connection; placed by pad position"
    ),
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
    POLARITY_BY_JLC_MARKS: "JLC's side from the + / - marks its footprint draws beside the pads",
    POLARITY_ASSUMED: (
        "polarized part on a footprint that isn't JLC's or a KiCad LED/diode/capacitor footprint; paired by pad "
        "number, which is only right if it is numbered like JLC's"
    ),
    POSITION_ESTIMATE: "pad numbers differ from JLC's; estimated by matching pads by position",
    POSITION_LOOSE: "pads differ from JLC's in size or count; estimated by matching them loosely by position",
    POSITION_CENTRED: "pads too different to pair; JLC's pads centred on the board's",
    POLARITY_JLC_CONFLICT: (
        "JLC's symbol and footprint disagree on which pad is which; placed by the footprint's + / - marks, "
        "which JLC's preview shows. Check its orientation"
    ),
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
    own_footprint_rms: float = 0.01  # a footprint named as JLC's own and this close is JLC's (imported)
    medium_rms: float = 0.25  # land-pattern differences that don't move the centre/rotation
    mirror_margin: float = 0.05  # how much better the mirrored fit must be to flag it
    snap_deg: float = 1.0
    min_spread: float = 0.01  # points closer than this don't determine an orientation
    same_site: float = 0.01  # pads closer than this are one land when matching by position
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
        if polarity is not None and polarity.jlc_source == JLC_MARKS:
            result.flags.append(POLARITY_BY_JLC_MARKS)
        if polarity is not None and polarity.conflict:
            result.flags.append(POLARITY_JLC_CONFLICT)
    elif polarity is not None and polarity.assumed and not (polarity.own_footprint and rms <= tol.own_footprint_rms):
        result.flags.append(POLARITY_ASSUMED)
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
    if (polarity_source != NOT_POLARIZED or POLARITY_ASSUMED in result.flags) and result.confidence == HIGH:
        result.confidence = MEDIUM
    # A repeated number paired with a different number of pads puts a group's
    # centre on the other side's pads.  Fine when those pads really sit there (a
    # terminal split into three pads, the middle one on JLC's single pad) or when
    # other groups agree; but a fit resting on a centre with no pad under it is
    # wrong, since two points always fit when their spacing matches (a tact
    # switch's pads 1,1,2,2 against JLC's 1-4).
    same_size = sum(1 for n in common if len(board_groups[n]) == len(jlc_groups[n]))
    if GROUP_SIZE_MISMATCH in result.flags and same_size < 2:
        if _rests_on_bare_centre(board_groups, jlc_groups, common, side, angle, origin, tol.medium_rms):
            result.confidence = LOW
    return result


def _rests_on_bare_centre(board_groups, jlc_groups, common, side, angle, origin, tol) -> bool:
    """True when, after the fit, some pad of a mismatched group has no pad of the other side's group under it."""
    for n in common:
        if len(board_groups[n]) == len(jlc_groups[n]):
            continue
        board = [board_to_cpl(x, y) for x, y in board_groups[n]]
        jlc = []
        for x, y in jlc_groups[n]:
            rx, ry = rotate(side_mirror(local_to_up(x, y), side), angle)
            jlc.append((rx + origin[0], ry + origin[1]))
        fewer, more = (board, jlc) if len(board) < len(jlc) else (jlc, board)
        if any(min(math.hypot(a[0] - b[0], a[1] - b[1]) for b in more) > tol for a in fewer):
            return True
    return False


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


def _sites(points: Sequence[Point], tol: float) -> List[List[int]]:
    """Indices of *points* grouped by position: a point within *tol* of a site's first point joins that site."""
    sites: List[List[int]] = []
    for i, (x, y) in enumerate(points):
        for site in sites:
            sx, sy = points[site[0]]
            if math.hypot(x - sx, y - sy) <= tol:
                site.append(i)
                break
        else:
            sites.append([i])
    return sites


def _site_numbers(pads: Sequence[PadRecord], site: Sequence[int]) -> List[str]:
    """The pad numbers at a site, once each (as written on the first pad with that number)."""
    numbers: Dict[str, str] = {}
    for i in site:
        numbers.setdefault(normalize_pad_number(pads[i].number), pads[i].number)
    return list(numbers.values())


def match_pads_by_position(
    board_pads: Sequence[PadRecord],
    jlc_pads: Sequence[PadRecord],
    side: str,
    kicad_orientation: Optional[float] = None,
    tol: Tolerances = DEFAULT_TOLERANCES,
    break_ties: bool = False,
    rank_by_fit: bool = False,
) -> Optional[List[PadRecord]]:
    """JLC's pads renamed with the board's pad numbers, matched by position.

    For footprints with the same pads but different numbering.  Pads at the
    same position (within ``tol.same_site``) count as one site, so KiCad's
    USB-C pads A1 and B12, stacked on one land, match JLC's single ``A1B12``
    pad.  Site centroids are aligned and every 90° step (and KiCad's angle
    plus 90° steps) is tried; each JLC site takes the numbers of the nearest
    board site, one pad per number.  Returns None when the site counts differ
    or nothing fits within ``tol.medium_rms``.

    A symmetric pattern can fit in more than one orientation with a different
    numbering, and then position alone cannot tell which way round the part
    goes: the result is None, unless *break_ties* is set.  Then the fit that
    keeps the most pad numbers wins, and after that the one that puts JLC's
    pin 1 nearest the board's pin 1 (used when the numbering is mirrored, so
    no orientation keeps every number).  With *rank_by_fit* the best fit wins
    first and those preferences only break ties (for loose matches, where a
    worse orientation can also fit).
    """
    board = [pad for pad in board_pads if normalize_pad_number(pad.number)]
    jlc = [pad for pad in jlc_pads if normalize_pad_number(pad.number)]
    board_points = [board_to_cpl(pad.x, pad.y) for pad in board]
    jlc_points = [side_mirror(local_to_up(pad.x, pad.y), side) for pad in jlc]
    board_sites = _sites(board_points, tol.same_site)
    jlc_sites = _sites(jlc_points, tol.same_site)
    if len(board_sites) != len(jlc_sites) or len(board_sites) < 3:
        return None
    p = [board_points[site[0]] for site in board_sites]
    q = [jlc_points[site[0]] for site in jlc_sites]
    board_numbers = [_site_numbers(board, site) for site in board_sites]
    pc, qc = _centroid(p), _centroid(q)

    angles = [k * 90.0 for k in range(4)]
    if kicad_orientation is not None:
        angles += [normalize_angle(kicad_orientation + k * 90.0) for k in range(4)]
    fits = []
    for angle in sorted({round(a, 6) for a in angles}):
        found = _assign_nearest(q, p, angle, _translation(qc, pc, angle))
        if found is not None and found[1] <= tol.medium_rms:
            numbers = tuple(tuple(normalize_pad_number(n) for n in board_numbers[j]) for j in found[0])
            fits.append((found[1], numbers, found[0]))
    if not fits:
        return None
    if len({numbers for _, numbers, _ in fits}) > 1:
        if not break_ties:
            return None
        jlc_numbers = [{normalize_pad_number(n) for n in _site_numbers(jlc, site)} for site in jlc_sites]
        board_index = {normalize_pad_number(n): j for j, site in enumerate(board_numbers) for n in site}
        first = min((n for site in jlc_numbers for n in site), key=natural_key)
        first_site = next(i for i, site in enumerate(jlc_numbers) if first in site)

        def preference(fit):
            rms, numbers, assignment = fit
            kept = sum(1 for a, b in zip(jlc_numbers, numbers) if a & set(b))
            pin1 = 0.0
            if first in board_index:
                lands = p[assignment[first_site]]
                target = p[board_index[first]]
                pin1 = math.hypot(lands[0] - target[0], lands[1] - target[1])
            if rank_by_fit:
                return (round(rms, 3), -kept, round(pin1, 6))
            return (-kept, round(pin1, 6), rms)

        fits.sort(key=preference)
        assignment = fits[0][2]
    else:
        assignment = min(fits)[2]
    return [
        PadRecord(number=number, x=jlc[site[0]].x, y=jlc[site[0]].y)
        for site, j in zip(jlc_sites, assignment)
        for number in board_numbers[j]
    ]


def _claim_nearest(a: Sequence[Point], b: Sequence[Point]) -> Tuple[List[int], List[int]]:
    """Pair points of *a* and *b* one-to-one, closest pairs first.  Returns the unpaired indices of each."""
    pairs = sorted((math.hypot(ax - bx, ay - by), i, j) for i, (ax, ay) in enumerate(a) for j, (bx, by) in enumerate(b))
    used_a, used_b = set(), set()
    for _, i, j in pairs:
        if i not in used_a and j not in used_b:
            used_a.add(i)
            used_b.add(j)
    return [i for i in range(len(a)) if i not in used_a], [j for j in range(len(b)) if j not in used_b]


def _reach(pads: Sequence[PadRecord]) -> float:
    """Half the longest side of the largest pad (0 when the sizes are unknown)."""
    return max(max(pad.width, pad.height) for pad in pads) / 2


def pair_unmatched_by_position(
    board_pads: Sequence[PadRecord],
    jlc_pads: Sequence[PadRecord],
    side: str,
    tol: Tolerances = DEFAULT_TOLERANCES,
) -> Optional[List[PadRecord]]:
    """JLC's pads, with those whose numbers don't pair renamed after the board pads they land on.

    For footprints that number most pads alike but a few differently.  KiCad
    numbers a SOT-223's tab 2, like the pin it joins, where JLC numbers it 4;
    KiCad stacks a USB-C connector's A1 and B12 on one land that JLC calls
    ``A1B12``, and numbers the shield pads SH where JLC numbers them 1-4.

    The groups with the same number and pad count on both footprints fix the
    transform; they must fit within ``tol.medium_rms``.  The other pads are
    numbers only one footprint has, and the extra pads of a group that has
    more pads on one footprint (the pads of the group nearest the other
    footprint's keep the number).  Each JLC site among them is paired with a
    board site when each is the other's nearest and they are no further apart
    than half the longest side of the smaller of the two pads (at least
    ``tol.medium_rms``), and takes the board site's numbers.  The fit is
    then solved again with :func:`solve_placement`, which judges it as usual.
    Returns None when no pad is renamed.
    """
    board = [pad for pad in board_pads if normalize_pad_number(pad.number)]
    jlc = [pad for pad in jlc_pads if normalize_pad_number(pad.number)]
    board_groups: Dict[str, List[int]] = {}
    jlc_groups: Dict[str, List[int]] = {}
    for groups, pads in ((board_groups, board), (jlc_groups, jlc)):
        for i, pad in enumerate(pads):
            groups.setdefault(normalize_pad_number(pad.number), []).append(i)
    common = sorted(set(board_groups) & set(jlc_groups))
    anchors = [n for n in common if len(board_groups[n]) == len(jlc_groups[n])]
    if len(anchors) < 2:
        return None

    p_all = [board_to_cpl(pad.x, pad.y) for pad in board]
    q_all = [side_mirror(local_to_up(pad.x, pad.y), side) for pad in jlc]
    q = [_centroid([q_all[i] for i in jlc_groups[n]]) for n in anchors]
    p = [_centroid([p_all[i] for i in board_groups[n]]) for n in anchors]
    deg, qc, pc, spread_q, spread_p = _fit_rotation(q, p)
    if min(spread_q, spread_p) < tol.min_spread * tol.min_spread:
        return None
    t = _translation(qc, pc, deg)
    if _residuals(q, p, deg, t)[0] > tol.medium_rms:
        return None
    placed = []
    for point in q_all:
        rx, ry = rotate(point, deg)
        placed.append((rx + t[0], ry + t[1]))

    board_left = [i for n, group in board_groups.items() if n not in jlc_groups for i in group]
    jlc_left = [i for n, group in jlc_groups.items() if n not in board_groups for i in group]
    for n in common:
        b, j = board_groups[n], jlc_groups[n]
        if len(b) != len(j):
            b_extra, j_extra = _claim_nearest([p_all[i] for i in b], [placed[i] for i in j])
            board_left += [b[i] for i in b_extra]
            jlc_left += [j[i] for i in j_extra]
    if not board_left or not jlc_left:
        return None

    board_sites = [[board_left[i] for i in site] for site in _sites([p_all[i] for i in board_left], tol.same_site)]
    jlc_sites = [[jlc_left[i] for i in site] for site in _sites([placed[i] for i in jlc_left], tol.same_site)]

    def nearest(point: Point, points: Sequence[Point]) -> Tuple[int, float]:
        dists = [math.hypot(point[0] - x, point[1] - y) for x, y in points]
        k = min(range(len(dists)), key=dists.__getitem__)
        return k, dists[k]

    b_points = [p_all[site[0]] for site in board_sites]
    j_points = [placed[site[0]] for site in jlc_sites]
    renamed: Dict[int, List[str]] = {}
    for js, site in enumerate(jlc_sites):
        bs, dist = nearest(j_points[js], b_points)
        if nearest(b_points[bs], j_points)[0] != js:
            continue
        reach = min(_reach([board[i] for i in board_sites[bs]]), _reach([jlc[i] for i in site]))
        if dist <= max(reach, tol.medium_rms):
            renamed[site[0]] = _site_numbers(board, board_sites[bs])
            renamed.update((i, []) for i in site[1:])
    if not renamed:
        return None
    out = [pad for pad in jlc_pads if not normalize_pad_number(pad.number)]
    for i, pad in enumerate(jlc):
        out.extend([replace(pad, number=n) for n in renamed[i]] if i in renamed else [pad])
    return out


def centred_origin(board_pads: Sequence[PadRecord], jlc_pads: Sequence[PadRecord], side: str, angle: float) -> Point:
    """JLC origin (CPL coordinates) that puts the centre of JLC's pads on the centre of the board's at *angle*."""
    p = [board_to_cpl(pad.x, pad.y) for pad in board_pads if normalize_pad_number(pad.number)]
    q = [side_mirror(local_to_up(pad.x, pad.y), side) for pad in jlc_pads if normalize_pad_number(pad.number)]
    return _translation(_centroid(q), _centroid(p), angle)


def describe_flags(flags: Sequence[str]) -> List[str]:
    return [FLAG_TEXT.get(f, f) for f in flags]
