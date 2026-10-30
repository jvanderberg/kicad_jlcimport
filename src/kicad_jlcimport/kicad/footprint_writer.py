"""Generate KiCad .kicad_mod footprint files (v8 and v9)."""

import math
from typing import Optional, Tuple

from ..easyeda.ee_types import EEFootprint
from ..easyeda.parser import compute_arc_midpoint
from ._format import escape_sexpr as _escape
from ._format import fmt_float as _fmt
from ._format import fmt_geometry as _geom
from ._format import gen_uuid as _uuid
from .version import DEFAULT_KICAD_VERSION, footprint_format_version, has_embedded_fonts, has_generator_version


def write_footprint(
    footprint: EEFootprint,
    name: str,
    lcsc_id: str = "",
    description: str = "",
    keywords: str = "",
    datasheet: str = "",
    model_path: str = "",
    model_offset: Tuple[float, float, float] = (0, 0, 0),
    model_rotation: Tuple[float, float, float] = (0, 0, 0),
    kicad_version: int = DEFAULT_KICAD_VERSION,
) -> str:
    """Generate complete .kicad_mod content for a footprint."""
    lines = []

    # Determine if SMD or through-hole
    has_tht = any(pad.layer == "11" and pad.drill > 0 for pad in footprint.pads)
    attr = "through_hole" if has_tht else "smd"

    # Calculate bounding box for reference/value placement
    all_y = []
    for pad in footprint.pads:
        all_y.extend([pad.y - pad.height / 2, pad.y + pad.height / 2])
    for track in footprint.tracks:
        all_y.extend([p[1] for p in track.points])
    min_y = min(all_y) if all_y else -2
    max_y = max(all_y) if all_y else 2

    ref_y = min_y - 1.0
    val_y = max_y + 1.0

    lines.append(f'(footprint "{_escape(name)}"')
    lines.append(f"  (version {footprint_format_version(kicad_version)})")
    lines.append('  (generator "JLCImport")')
    if has_generator_version(kicad_version):
        lines.append('  (generator_version "1.0")')
    lines.append('  (layer "F.Cu")')
    if description:
        lines.append(f'  (descr "{_escape(description)}")')
    if keywords:
        lines.append(f'  (tags "{_escape(keywords)}")')

    # Properties
    lines.append(f'  (property "Reference" "REF**" (at 0 {_geom(ref_y)} 0) (layer "F.SilkS") (uuid "{_uuid()}")')
    lines.append("    (effects (font (size 1 1) (thickness 0.15)))")
    lines.append("  )")
    lines.append(f'  (property "Value" "~" (at 0 {_geom(val_y)} 0) (layer "F.Fab") (uuid "{_uuid()}")')
    lines.append("    (effects (font (size 1 1) (thickness 0.15)))")
    lines.append("  )")
    if datasheet:
        lines.append(
            f'  (property "Datasheet" "{_escape(datasheet)}" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "{_uuid()}")'
        )
        lines.append("    (effects (font (size 1 1) (thickness 0.15)))")
        lines.append("  )")
    if description:
        lines.append(
            f'  (property "Description" "{_escape(description)}" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "{_uuid()}")'
        )
        lines.append("    (effects (font (size 1 1) (thickness 0.15)))")
        lines.append("  )")
    if lcsc_id:
        lines.append(
            f'  (property "LCSC" "{_escape(lcsc_id)}" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "{_uuid()}")'
        )
        lines.append("    (effects (font (size 1 1) (thickness 0.15)))")
        lines.append("  )")

    lines.append(f"  (attr {attr})")

    # Tracks (fp_line segments)
    for track in footprint.tracks:
        for i in range(len(track.points) - 1):
            x1, y1 = track.points[i]
            x2, y2 = track.points[i + 1]
            lines.append(
                f"  (fp_line (start {_geom(x1)} {_geom(y1)}) (end {_geom(x2)} {_geom(y2)})"
                f" (stroke (width {_geom(track.width)}) (type solid))"
                f' (layer "{track.layer}") (uuid "{_uuid()}"))'
            )

    # Circles
    for circle in footprint.circles:
        end_x = circle.cx + circle.radius
        fill_str = " (fill solid)" if circle.filled else ""
        lines.append(
            f"  (fp_circle (center {_geom(circle.cx)} {_geom(circle.cy)})"
            f" (end {_geom(end_x)} {_geom(circle.cy)})"
            f" (stroke (width {_geom(circle.width)}) (type solid))"
            f"{fill_str}"
            f' (layer "{circle.layer}") (uuid "{_uuid()}"))'
        )

    # Arcs
    for arc in footprint.arcs:
        mid = compute_arc_midpoint(arc.start, arc.end, arc.rx, arc.ry, arc.large_arc, arc.sweep)
        # If sweep == 0, swap start and end
        if arc.sweep == 0:
            s, e = arc.end, arc.start
        else:
            s, e = arc.start, arc.end
        lines.append(
            f"  (fp_arc (start {_geom(s[0])} {_geom(s[1])})"
            f" (mid {_geom(mid[0])} {_geom(mid[1])})"
            f" (end {_geom(e[0])} {_geom(e[1])})"
            f" (stroke (width {_geom(arc.width)}) (type solid))"
            f' (layer "{arc.layer}") (uuid "{_uuid()}"))'
        )

    # Solid regions (e.g., pin 1 indicators, courtyard outlines)
    for region in footprint.regions:
        pts_str = " ".join(f"(xy {_geom(x)} {_geom(y)})" for x, y in region.points)
        if region.layer in ("F.CrtYd", "B.CrtYd"):
            # Courtyard must be an unfilled outline, not a filled polygon
            lines.append(
                f"  (fp_poly (pts {pts_str})"
                f" (stroke (width 0.05) (type solid))"
                f" (fill none)"
                f' (layer "{region.layer}") (uuid "{_uuid()}"))'
            )
        else:
            lines.append(
                f"  (fp_poly (pts {pts_str})"
                f" (stroke (width 0) (type solid))"
                f" (fill solid)"
                f' (layer "{region.layer}") (uuid "{_uuid()}"))'
            )

    # Pads
    for pad in footprint.pads:
        pad_type, pad_shape, layers = _pad_type_info(pad)
        at_str = f"(at {_geom(pad.x)} {_geom(pad.y)}"
        if pad.rotation != 0:
            at_str += f" {_fmt(pad.rotation)}"
        at_str += ")"

        size_str = f"(size {_geom(pad.width)} {_geom(pad.height)})"
        layers_str = " ".join(f'"{layer}"' for layer in layers)

        if pad_shape == "custom" and pad.polygon_points:
            # Custom pad with polygon primitives.  The polygon vertices
            # already define the final shape, so omit pad rotation to
            # avoid double-rotating.
            custom_at = f"(at {_geom(pad.x)} {_geom(pad.y)})"
            pts = pad.polygon_points
            pts_str = " ".join(f"(xy {_geom(pts[i])} {_geom(pts[i + 1])})" for i in range(0, len(pts) - 1, 2))
            # Use a minimal anchor size — the actual shape is defined
            # entirely by the gr_poly primitive.  A large anchor would fill
            # in the castellated notches of the custom polygon.
            lines.append(f'  (pad "{_escape(pad.number)}" {pad_type} {pad_shape} {custom_at} (size 0.1 0.1)')
            if pad.drill > 0:
                # Custom pads are written unrotated, so the slot is measured on the board.
                lines.append(f"    {_drill_str(pad, written_rotation=0.0)}")
            lines.append(f"    (layers {layers_str})")
            lines.append("    (options (clearance outline) (anchor rect))")
            lines.append("    (primitives")
            lines.append(f"      (gr_poly (pts {pts_str}) (width 0) (fill yes))")
            lines.append(f'    ) (uuid "{_uuid()}"))')
        else:
            pad_line = f'  (pad "{_escape(pad.number)}" {pad_type} {pad_shape} {at_str} {size_str}'
            if pad.drill > 0:
                pad_line += f" {_drill_str(pad, written_rotation=pad.rotation)}"
            pad_line += f' (layers {layers_str}) (uuid "{_uuid()}"))'
            lines.append(pad_line)

    # Holes (NPTH)
    for hole in footprint.holes:
        diameter = hole.radius * 2
        lines.append(
            f'  (pad "" np_thru_hole circle (at {_geom(hole.x)} {_geom(hole.y)})'
            f" (size {_geom(diameter)} {_geom(diameter)})"
            f" (drill {_geom(diameter)})"
            f' (layers "*.Cu" "*.Mask") (uuid "{_uuid()}"))'
        )

    # 3D model
    if model_path:
        ox, oy, oz = model_offset
        rx, ry, rz = model_rotation
        lines.append(f'  (model "{_escape(model_path)}"')
        lines.append(f"    (offset (xyz {_geom(ox)} {_geom(oy)} {_geom(oz)}))")
        lines.append("    (scale (xyz 1 1 1))")
        lines.append(f"    (rotate (xyz {_fmt(rx)} {_fmt(ry)} {_fmt(rz)}))")
        lines.append("  )")

    if has_embedded_fonts(kicad_version):
        lines.append("  (embedded_fonts no)")
    lines.append(")")

    return "\n".join(lines) + "\n"


def _drill_str(pad, written_rotation: float) -> str:
    """Return the KiCad drill specification string for a pad.

    For oval slot drills (slot_length > 0), returns ``(drill oval W H)``,
    where W runs along the pad's own x axis.  *written_rotation* is the
    rotation the pad is written with (KiCad rotates the drill with the pad).
    For circular drills, returns ``(drill D)``.
    """
    if pad.slot_length > 0:
        if _slot_along_pad_x(pad, written_rotation):
            return f"(drill oval {_geom(pad.slot_length)} {_geom(pad.drill)})"
        return f"(drill oval {_geom(pad.drill)} {_geom(pad.slot_length)})"
    return f"(drill {_geom(pad.drill)})"


def _slot_along_pad_x(pad, written_rotation: float) -> bool:
    """Return True if the pad's slot runs along the pad's own x axis.

    Uses the slot direction EasyEDA records.  The sign of EasyEDA's pad
    rotation relative to KiCad's hasn't been verified, so the slot is
    measured against the pad under both signs; they always agree for
    0/90/180/270 degree pads.  If they disagree, or EasyEDA gives no
    direction, fall back to the pad's long axis.
    """
    if pad.slot_angle is not None:
        answers = {
            _mostly_along_x(pad.slot_angle - written_rotation),
            _mostly_along_x(pad.slot_angle + written_rotation),
        }
        if len(answers) == 1 and None not in answers:
            return answers.pop()
    return pad.width > pad.height


def _mostly_along_x(angle_deg: float) -> Optional[bool]:
    """True if *angle_deg* is nearer the x axis, False if nearer y, None at 45 degrees."""
    c = abs(math.cos(math.radians(angle_deg)))
    s = abs(math.sin(math.radians(angle_deg)))
    if abs(c - s) < 1e-9:
        return None
    return c > s


def _pad_type_info(pad):
    """Determine pad type, shape, and layers."""
    # Shape mapping
    shape_map = {
        "RECT": "rect",
        "OVAL": "oval",
        "ELLIPSE": "oval",
        "POLYGON": "custom",
    }
    pad_shape = shape_map.get(pad.shape, "rect")

    # Only use custom shape if polygon data is available; fall back to rect
    if pad_shape == "custom" and not pad.polygon_points:
        pad_shape = "rect"

    if pad.layer == "11" and pad.drill <= 0:
        # Multi-layer pad without a hole: KiCad has no drill-less thru_hole
        # pad, so put plain copper on both sides.
        pad_type = "smd"
        layers = ["F.Cu", "B.Cu", "F.Mask", "B.Mask"]
    elif pad.layer == "11":
        # Through-hole
        pad_type = "thru_hole" if pad.plated else "np_thru_hole"
        layers = ["*.Cu", "*.Mask"]
        # First pad is often rect for THT
        if pad.number == "1" and pad_shape == "rect":
            pad_shape = "rect"
    elif pad.layer == "2":
        pad_type = "smd"
        layers = ["B.Cu", "B.Mask", "B.Paste"]
    else:
        pad_type = "smd"
        layers = ["F.Cu", "F.Mask", "F.Paste"]

    return pad_type, pad_shape, layers
