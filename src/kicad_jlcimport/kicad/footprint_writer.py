"""Generate KiCad .kicad_mod footprint files (v8 and v9)."""

from typing import Tuple

from ..easyeda.ee_types import EEFootprint
from ..easyeda.parser import compute_arc_midpoint
from ._format import escape_sexpr as _escape
from ._format import fmt_float as _fmt
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
    has_tht = any(pad.layer == "11" for pad in footprint.pads)
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

    lines.append(f'(footprint "{name}"')
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
    lines.append(f'  (property "Reference" "REF**" (at 0 {_fmt(ref_y)} 0) (layer "F.SilkS") (uuid "{_uuid()}")')
    lines.append("    (effects (font (size 1 1) (thickness 0.15)))")
    lines.append("  )")
    lines.append(f'  (property "Value" "~" (at 0 {_fmt(val_y)} 0) (layer "F.Fab") (uuid "{_uuid()}")')
    lines.append("    (effects (font (size 1 1) (thickness 0.15)))")
    lines.append("  )")
    if datasheet:
        lines.append(f'  (property "Datasheet" "{datasheet}" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "{_uuid()}")')
        lines.append("    (effects (font (size 1 1) (thickness 0.15)))")
        lines.append("  )")
    if description:
        lines.append(
            f'  (property "Description" "{_escape(description)}" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "{_uuid()}")'
        )
        lines.append("    (effects (font (size 1 1) (thickness 0.15)))")
        lines.append("  )")
    if lcsc_id:
        lines.append(f'  (property "LCSC" "{lcsc_id}" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "{_uuid()}")')
        lines.append("    (effects (font (size 1 1) (thickness 0.15)))")
        lines.append("  )")

    lines.append(f"  (attr {attr})")

    # Tracks (fp_line segments)
    for track in footprint.tracks:
        for i in range(len(track.points) - 1):
            x1, y1 = track.points[i]
            x2, y2 = track.points[i + 1]
            lines.append(
                f"  (fp_line (start {_fmt(x1)} {_fmt(y1)}) (end {_fmt(x2)} {_fmt(y2)})"
                f" (stroke (width {_fmt(track.width)}) (type solid))"
                f' (layer "{track.layer}") (uuid "{_uuid()}"))'
            )

    # Circles
    for circle in footprint.circles:
        end_x = circle.cx + circle.radius
        fill_str = " (fill solid)" if circle.filled else ""
        lines.append(
            f"  (fp_circle (center {_fmt(circle.cx)} {_fmt(circle.cy)})"
            f" (end {_fmt(end_x)} {_fmt(circle.cy)})"
            f" (stroke (width {_fmt(circle.width)}) (type solid))"
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
            f"  (fp_arc (start {_fmt(s[0])} {_fmt(s[1])})"
            f" (mid {_fmt(mid[0])} {_fmt(mid[1])})"
            f" (end {_fmt(e[0])} {_fmt(e[1])})"
            f" (stroke (width {_fmt(arc.width)}) (type solid))"
            f' (layer "{arc.layer}") (uuid "{_uuid()}"))'
        )

    # Solid regions (e.g., pin 1 indicators)
    for region in footprint.regions:
        pts_str = " ".join(f"(xy {_fmt(x)} {_fmt(y)})" for x, y in region.points)
        lines.append(
            f"  (fp_poly (pts {pts_str})"
            f" (stroke (width 0) (type solid))"
            f" (fill solid)"
            f' (layer "{region.layer}") (uuid "{_uuid()}"))'
        )

    # Pads
    for pad in footprint.pads:
        pad_type, pad_shape, layers = _pad_type_info(pad)
        at_str = f"(at {_fmt(pad.x)} {_fmt(pad.y)}"
        if pad.rotation != 0:
            at_str += f" {_fmt(pad.rotation)}"
        at_str += ")"

        size_str = f"(size {_fmt(pad.width)} {_fmt(pad.height)})"
        layers_str = " ".join(f'"{layer}"' for layer in layers)

        if pad_shape == "custom" and pad.polygon_points:
            # Custom pad with polygon primitives.  The polygon vertices
            # already define the final shape, so omit pad rotation to
            # avoid double-rotating.
            custom_at = f"(at {_fmt(pad.x)} {_fmt(pad.y)})"
            pts = pad.polygon_points
            pts_str = " ".join(f"(xy {_fmt(pts[i])} {_fmt(pts[i + 1])})" for i in range(0, len(pts) - 1, 2))
            # Use a minimal anchor size — the actual shape is defined
            # entirely by the gr_poly primitive.  A large anchor would fill
            # in the castellated notches of the custom polygon.
            lines.append(f'  (pad "{pad.number}" {pad_type} {pad_shape} {custom_at} (size 0.1 0.1)')
            if pad.drill > 0:
                lines.append(f"    {_drill_str(pad)}")
            lines.append(f"    (layers {layers_str})")
            lines.append("    (options (clearance outline) (anchor rect))")
            lines.append("    (primitives")
            lines.append(f"      (gr_poly (pts {pts_str}) (width 0) (fill yes))")
            lines.append(f'    ) (uuid "{_uuid()}"))')
        else:
            pad_line = f'  (pad "{pad.number}" {pad_type} {pad_shape} {at_str} {size_str}'
            if pad.drill > 0:
                pad_line += f" {_drill_str(pad)}"
            pad_line += f' (layers {layers_str}) (uuid "{_uuid()}"))'
            lines.append(pad_line)

    # Holes (NPTH)
    for hole in footprint.holes:
        diameter = hole.radius * 2
        lines.append(
            f'  (pad "" np_thru_hole circle (at {_fmt(hole.x)} {_fmt(hole.y)})'
            f" (size {_fmt(diameter)} {_fmt(diameter)})"
            f" (drill {_fmt(diameter)})"
            f' (layers "*.Cu" "*.Mask") (uuid "{_uuid()}"))'
        )

    # 3D model
    if model_path:
        ox, oy, oz = model_offset
        rx, ry, rz = model_rotation
        lines.append(f'  (model "{model_path}"')
        lines.append(f"    (offset (xyz {_fmt(ox)} {_fmt(oy)} {_fmt(oz)}))")
        lines.append("    (scale (xyz 1 1 1))")
        lines.append(f"    (rotate (xyz {_fmt(rx)} {_fmt(ry)} {_fmt(rz)}))")
        lines.append("  )")

    if has_embedded_fonts(kicad_version):
        lines.append("  (embedded_fonts no)")
    lines.append(")")

    return "\n".join(lines) + "\n"


def _drill_str(pad) -> str:
    """Return the KiCad drill specification string for a pad.

    For oval slot drills (slot_length > 0), returns ``(drill oval W H)``
    with the slot oriented to match the pad aspect ratio.
    For circular drills, returns ``(drill D)``.
    """
    if pad.slot_length > 0:
        if pad.height >= pad.width:
            # Vertical slot: narrow dimension = drill, long dimension = slot_length
            return f"(drill oval {_fmt(pad.drill)} {_fmt(pad.slot_length)})"
        else:
            # Horizontal slot: long dimension = slot_length, narrow dimension = drill
            return f"(drill oval {_fmt(pad.slot_length)} {_fmt(pad.drill)})"
    return f"(drill {_fmt(pad.drill)})"


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

    if pad.layer == "11":
        # Through-hole
        pad_type = "thru_hole"
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
