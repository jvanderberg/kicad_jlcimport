"""Coordinate conventions for production export — the single source of truth.

Every frame conversion used by the solver, the fallback placement and the CPL
writer lives here so the conventions can be audited (and corrected) in one
place.

Frames
------
``board``
    KiCad board coordinates in millimetres.  X points right, **Y points
    down** (screen convention).  This is what ``pcbnew`` and ``.kicad_pcb``
    files use.  A footprint orientation ``theta`` is counter-clockwise *as
    seen on screen* (viewed from the top of the board).

``cpl``
    Pick-and-place coordinates in millimetres.  X points right, **Y points
    up**, viewed from the top of the board.  This matches KiCad's own
    ``.pos`` export and the Gerber files we plot (no aux origin, so both share
    the board's absolute origin).  ``cpl = (x, -y)`` of the board frame.

``local``
    A footprint's own frame, in millimetres, as stored in a ``.kicad_mod``
    or in an EasyEDA footprint after :func:`parse_footprint_shapes`.  X
    right, **Y down** — EasyEDA's canvas is Y-down and ``parser.py`` only
    subtracts the footprint origin (no Y flip), and ``footprint_writer.py``
    writes those coordinates straight into KiCad, so the EasyEDA local frame
    and KiCad's footprint-local frame are the same.

``local_up``
    ``local`` with Y negated (Y up), so rotations are ordinary
    counter-clockwise rotations.  The solver works in ``local_up`` / ``cpl``.

Placement model
---------------
Top side::

    p_cpl = t + R(phi) * q_local_up

Bottom side::

    p_cpl = t + R(phi) * BOTTOM_MIRROR * q_local_up

``R(phi)`` is the standard counter-clockwise rotation, ``t`` is where the
footprint origin lands in CPL coordinates and ``phi`` is the CPL rotation.

``BOTTOM_MIRROR = diag(1, -1)`` reproduces KiCad's own flip: KiCad mirrors a
footprint's local Y and negates its orientation when moving it to the bottom
layer, which gives exactly ``p = t + R(theta) * diag(1, -1) * q`` for a KiCad
footprint with orientation ``theta``.  Consequently, for a footprint whose
library frame already matches JLC's, the solver returns KiCad's own
orientation on both sides.

CPL ``Rotation`` column
-----------------------
Top side: ``phi``.  Bottom side: ``180° - phi``, because JLCPCB measures
bottom-side rotations in the opposite direction; the two agree at 90°/270°
and differ by 180° at 0°/180°.
"""

import math
from typing import Tuple

Point = Tuple[float, float]

TOP = "top"
BOTTOM = "bottom"

# Mirror applied to a footprint's local_up coordinates when it sits on the
# bottom side: (x, y) -> (x * BOTTOM_MIRROR[0], y * BOTTOM_MIRROR[1]).
BOTTOM_MIRROR = (1.0, -1.0)


def board_to_cpl(x: float, y: float) -> Point:
    """Board (Y down) → CPL (Y up)."""
    return (x, -y)


def local_to_up(x: float, y: float) -> Point:
    """Footprint-local (Y down) → local_up (Y up)."""
    return (x, -y)


def side_mirror(point: Point, side: str) -> Point:
    """Apply the bottom-side mirror to a local_up point (identity on top)."""
    if side == BOTTOM:
        return (point[0] * BOTTOM_MIRROR[0], point[1] * BOTTOM_MIRROR[1])
    return point


def rotate(point: Point, degrees: float) -> Point:
    """Counter-clockwise rotation in a Y-up frame."""
    rad = math.radians(degrees)
    c, s = math.cos(rad), math.sin(rad)
    return (point[0] * c - point[1] * s, point[0] * s + point[1] * c)


def normalize_angle(degrees: float) -> float:
    """Normalize an angle to [0, 360), mapping values within 1e-9 of 360 to 0."""
    a = math.fmod(degrees, 360.0)
    if a < 0:
        a += 360.0
    if abs(a - 360.0) < 1e-9 or abs(a) < 1e-9:
        return 0.0
    return a


def place_local(q_local: Point, t_cpl: Point, phi: float, side: str) -> Point:
    """Map a footprint-local point to CPL coordinates using the placement model.

    The forward form of the model the solver inverts; the tests check KiCad's
    own flip and the solver against it.
    """
    return _add(t_cpl, rotate(side_mirror(local_to_up(*q_local), side), phi))


def kicad_pose_to_cpl(x: float, y: float, orientation: float) -> Tuple[Point, float]:
    """Express a KiCad footprint's own pose in the placement model.

    KiCad stores ``(position, orientation)`` such that pads land at
    ``R_screen(theta) * q`` (top) or ``R_screen(theta) * flipY * q`` (bottom).
    Converted to Y-up, that is the placement model above with ``t = pos`` and
    ``phi = theta`` on both sides — this is the "trust KiCad's library frame"
    fallback.
    """
    return board_to_cpl(x, y), normalize_angle(orientation)


def cpl_rotation(phi: float, side: str) -> float:
    """Placement-model angle → value written to the CPL ``Rotation`` column.

    Bottom-side rotations run the other way in JLCPCB's CPL (see module docstring).
    """
    if side == BOTTOM:
        return normalize_angle(180.0 - phi)
    return normalize_angle(phi)


def _add(a: Point, b: Point) -> Point:
    return (a[0] + b[0], a[1] + b[1])
