"""Physical sanity checks for a placed 3D model.

These checks need no rendering: they look at where the model's geometry ends
up relative to the board and the footprint, and catch the failures the visual
comparison used to be needed for:

Issues (the placement is wrong):

* ``floating`` — the model does not reach the board.
* ``below_board`` — geometry comes out of the bottom of the board outside any
  hole, e.g. an upside-down part.  Only pins and pegs may pass through the
  board, and only through a drilled pad, mounting hole or NPTH cutout.
* ``off_footprint`` — a pad is not under the model at all.
* ``pins_unmatched`` — the model's pins do not reach the pads (the model and
  footprint disagree; see :func:`.model3d.place_model`).

Warnings (worth knowing, usually EasyEDA's own data):

* ``sunk`` — part of the body sits inside the board outside any hole.
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from ..easyeda.ee_types import EEFootprint, EEPad
from .model3d import (
    PINS_UNMATCHED,
    ModelPlacement,
    _in_polygon,
    _pad_half_extents,
    _parse_obj_mesh,
    _placed_vertices,
)

# KiCad's default board thickness (mm); geometry deeper than this outside a
# hole comes out of the bottom of the board.
BOARD_THICKNESS_MM = 1.6

# Geometry deeper than this inside the board (mm), outside a hole, is "sunk".
SUNK_MM = 0.1

# A model whose lowest point is higher than this above the board (mm) floats.
FLOATING_MM = 0.1

# Slack around holes and pads (mm): modelled pins are often drawn a little
# wider than the hole they go through.
CLEARANCE_MM = 0.1

FLOATING = "floating"
BELOW_BOARD = "below_board"
OFF_FOOTPRINT = "off_footprint"
PINS_MISMATCH = "pins_unmatched"
SUNK = "sunk"


@dataclass
class ModelCheck:
    """Problems found with a placed model; empty when it looks right."""

    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    details: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def add(self, issue: str, detail: str) -> None:
        self.issues.append(issue)
        self.details.append(detail)

    def warn(self, warning: str, detail: str) -> None:
        self.warnings.append(warning)
        self.details.append(detail)


def _in_pad_area(pad: EEPad, x: float, y: float) -> bool:
    if len(pad.polygon_points) >= 6:
        return _in_polygon(x - pad.x, y - pad.y, pad.polygon_points)
    hx, hy = _pad_half_extents(pad)
    return abs(x - pad.x) <= hx + CLEARANCE_MM and abs(y - pad.y) <= hy + CLEARANCE_MM


def _through_hole(footprint: EEFootprint, x: float, y: float) -> bool:
    """True when (x, y) is over a drilled pad, a mounting hole or an NPTH cutout."""
    for pad in footprint.pads:
        if pad.drill > 0 and _in_pad_area(pad, x, y):
            return True
    for hole in footprint.holes:
        if math.hypot(x - hole.x, y - hole.y) <= hole.radius + CLEARANCE_MM:
            return True
    for region in footprint.regions:
        if region.region_type == "npth" and len(region.points) >= 3:
            coords = [c for point in region.points for c in point]
            if _in_polygon(x, y, coords):
                return True
    return False


def check_model(
    footprint: EEFootprint,
    placement: ModelPlacement,
    obj_source: Optional[str],
) -> ModelCheck:
    """Check a placed model against the board and its footprint."""
    result = ModelCheck()
    mesh = _parse_obj_mesh(obj_source) if obj_source else None
    if mesh is None or not mesh.vertices:
        return result
    placed = _placed_vertices(mesh, placement.rotation, placement.offset)

    lowest = min(v[2] for v in placed)
    if lowest > FLOATING_MM:
        result.add(FLOATING, f"lowest point is {lowest:.2f} mm above the board")

    stray = [v for v in placed if v[2] < -SUNK_MM and not _through_hole(footprint, v[0], v[1])]
    through = [v for v in stray if v[2] < -BOARD_THICKNESS_MM]
    if through:
        result.add(
            BELOW_BOARD,
            f"{len(through)} vertices come out of the bottom of the board outside any hole "
            f"(down to {min(v[2] for v in through):.2f} mm)",
        )
    elif stray:
        result.warn(SUNK, f"body is sunk {-min(v[2] for v in stray):.2f} mm into the board")

    xs = [v[0] for v in placed]
    ys = [v[1] for v in placed]
    uncovered = _uncovered_pads(footprint.pads, min(xs), max(xs), min(ys), max(ys))
    if uncovered:
        result.add(OFF_FOOTPRINT, "pads not under the model: " + ", ".join(uncovered))

    if placement.pins == PINS_UNMATCHED:
        result.add(PINS_MISMATCH, "the model's pins do not reach the footprint pads")
    return result


def _uncovered_pads(pads: Sequence[EEPad], x0: float, x1: float, y0: float, y1: float) -> List[str]:
    """Pads whose area does not overlap the model's footprint on the board."""
    out = []
    for pad in pads:
        hx, hy = _pad_half_extents(pad)
        if pad.x + hx < x0 or pad.x - hx > x1 or pad.y + hy < y0 or pad.y - hy > y1:
            out.append(pad.number or "?")
    return out
