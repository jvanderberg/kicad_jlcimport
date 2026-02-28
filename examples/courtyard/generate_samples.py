#!/usr/bin/env python3
"""Generate realistic courtyard example footprints and SVG renders.

Uses the real _compute_courtyard() logic from footprint_writer.py.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from kicad_jlcimport.easyeda.ee_types import (
    EEFootprint,
    EEPad,
    EETrack,
    EECircle,
    EESolidRegion,
    EEHole,
)
from kicad_jlcimport.kicad.footprint_writer import (
    _compute_courtyard,
    _COURTYARD_CLEARANCE,
    write_footprint,
)


def make_qfp32():
    """QFP-32: pads on all 4 sides, silkscreen outline, pin-1 dot."""
    pads = []
    pitch = 0.8
    pad_w, pad_h = 1.6, 0.4
    body = 5.0  # body half-size from center

    # Bottom side (pads 1-8)
    for i in range(8):
        x = -2.8 + i * pitch
        pads.append(EEPad("RECT", x, body + pad_w / 2, pad_h, pad_w, "1", str(i + 1), 0))
    # Right side (pads 9-16)
    for i in range(8):
        y = 2.8 - i * pitch
        pads.append(EEPad("RECT", body + pad_w / 2, y, pad_w, pad_h, "1", str(8 + i + 1), 0))
    # Top side (pads 17-24)
    for i in range(8):
        x = 2.8 - i * pitch
        pads.append(EEPad("RECT", x, -(body + pad_w / 2), pad_h, pad_w, "1", str(16 + i + 1), 0))
    # Left side (pads 25-32)
    for i in range(8):
        y = -2.8 + i * pitch
        pads.append(EEPad("RECT", -(body + pad_w / 2), y, pad_w, pad_h, "1", str(24 + i + 1), 0))

    # Silkscreen outline (slightly inside the pads)
    silk = 4.4
    tracks = [
        # Bottom-left corner to bottom-right (with notch for pin 1)
        EETrack(0.15, "F.SilkS", [(-silk, silk), (silk, silk)]),
        EETrack(0.15, "F.SilkS", [(silk, silk), (silk, -silk)]),
        EETrack(0.15, "F.SilkS", [(silk, -silk), (-silk, -silk)]),
        EETrack(0.15, "F.SilkS", [(-silk, -silk), (-silk, silk)]),
    ]

    # Pin 1 dot on silkscreen
    circles = [
        EECircle(-3.5, 3.5, 0.3, 0.15, "F.SilkS", filled=True),
    ]

    # Pin 1 triangle marker on fab layer
    regions = [
        EESolidRegion("F.Fab", [(-4.0, 4.8), (-3.5, 4.0), (-3.0, 4.8)], "solid"),
    ]

    return EEFootprint(pads=pads, tracks=tracks, circles=circles, regions=regions)


def make_sot23_5():
    """SOT-23-5: asymmetric 3+2 pad layout with silkscreen."""
    pads = [
        # Left side: 3 pads
        EEPad("RECT", -1.1, -0.95, 1.0, 0.6, "1", "1", 0),
        EEPad("RECT", -1.1, 0.0, 1.0, 0.6, "1", "2", 0),
        EEPad("RECT", -1.1, 0.95, 1.0, 0.6, "1", "3", 0),
        # Right side: 2 pads
        EEPad("RECT", 1.1, -0.95, 1.0, 0.6, "1", "5", 0),
        EEPad("RECT", 1.1, 0.95, 1.0, 0.6, "1", "4", 0),
    ]

    # Silkscreen body outline
    tracks = [
        EETrack(0.15, "F.SilkS", [(-0.4, -1.45), (0.4, -1.45)]),
        EETrack(0.15, "F.SilkS", [(0.4, -1.45), (0.4, 1.45)]),
        EETrack(0.15, "F.SilkS", [(0.4, 1.45), (-0.4, 1.45)]),
        EETrack(0.15, "F.SilkS", [(-0.4, 1.45), (-0.4, -1.45)]),
    ]

    # Pin 1 marker
    circles = [EECircle(-0.15, -1.1, 0.15, 0.1, "F.SilkS", filled=True)]

    return EEFootprint(pads=pads, tracks=tracks, circles=circles)


def make_usb_c():
    """USB-C receptacle: wide body, mounting holes, many pads."""
    pads = []

    # Signal pads (two rows, 0.5mm pitch)
    for i, num in enumerate(["A1", "A2", "A3", "A4", "A5", "A6",
                             "A7", "A8", "A9", "A10", "A11", "A12"]):
        x = -2.75 + i * 0.5
        pads.append(EEPad("RECT", x, -3.8, 0.3, 1.0, "1", num, 0))
    for i, num in enumerate(["B12", "B11", "B10", "B9", "B8", "B7",
                             "B6", "B5", "B4", "B3", "B2", "B1"]):
        x = -2.75 + i * 0.5
        pads.append(EEPad("RECT", x, -5.4, 0.3, 1.0, "1", num, 0))

    # Shield / mounting tabs (large)
    pads.append(EEPad("RECT", -4.32, -4.6, 1.0, 1.6, "1", "S1", 0))
    pads.append(EEPad("RECT", 4.32, -4.6, 1.0, 1.6, "1", "S2", 0))

    # Silkscreen outline (connector body)
    bw, bh = 4.5, 3.8
    tracks = [
        EETrack(0.15, "F.SilkS", [(-bw, -1.5), (bw, -1.5)]),
        EETrack(0.15, "F.SilkS", [(bw, -1.5), (bw, -bh - 1.5)]),
        EETrack(0.15, "F.SilkS", [(bw, -bh - 1.5), (-bw, -bh - 1.5)]),
        EETrack(0.15, "F.SilkS", [(-bw, -bh - 1.5), (-bw, -1.5)]),
    ]

    # Mounting holes (NPTH)
    holes = [
        EEHole(-4.32, -2.6, 0.55),
        EEHole(4.32, -2.6, 0.55),
    ]

    return EEFootprint(pads=pads, tracks=tracks, holes=holes)


def make_dip8_tht():
    """DIP-8: through-hole, wide body, silkscreen + fab outline."""
    pitch = 2.54
    pads = []
    for i in range(4):
        y = -1.5 * pitch + i * pitch
        pads.append(EEPad("OVAL", -3.81, y, 1.6, 1.6, "11", str(i + 1), 0.8))
        pads.append(EEPad("OVAL", 3.81, y, 1.6, 1.6, "11", str(8 - i), 0.8))

    # Silkscreen outline
    body_x = 3.2
    body_y = 4.4
    tracks = [
        # U-shaped notch at top
        EETrack(0.15, "F.SilkS", [(-body_x, -body_y), (-0.5, -body_y)]),
        EETrack(0.15, "F.SilkS", [(-0.5, -body_y), (-0.5, -body_y + 0.8)]),
        EETrack(0.15, "F.SilkS", [(-0.5, -body_y + 0.8), (0.5, -body_y + 0.8)]),
        EETrack(0.15, "F.SilkS", [(0.5, -body_y + 0.8), (0.5, -body_y)]),
        EETrack(0.15, "F.SilkS", [(0.5, -body_y), (body_x, -body_y)]),
        # Rest of outline
        EETrack(0.15, "F.SilkS", [(body_x, -body_y), (body_x, body_y)]),
        EETrack(0.15, "F.SilkS", [(body_x, body_y), (-body_x, body_y)]),
        EETrack(0.15, "F.SilkS", [(-body_x, body_y), (-body_x, -body_y)]),
    ]

    # Pin 1 dot
    circles = [EECircle(-2.2, -3.2, 0.25, 0.1, "F.SilkS", filled=True)]

    return EEFootprint(pads=pads, tracks=tracks, circles=circles)


# ---------- SVG rendering ----------

COLORS = {
    "pad_smd": "#C87533",
    "pad_tht": "#C87533",
    "drill": "#2a2a2a",
    "silk": "#C4A000",
    "fab": "#808080",
    "courtyard": "#FF00FF",
    "clearance_fill": "rgba(255,0,255,0.06)",
    "bg": "#1a1a2e",
    "text": "#cccccc",
    "dim_line": "#888888",
    "pin1_dot": "#C4A000",
    "hole": "#3a3a3a",
}

SCALE = 30  # pixels per mm


def render_svg(name, footprint, out_path):
    """Render a footprint + courtyard to an annotated SVG."""
    crtyd = _compute_courtyard(footprint)
    if crtyd is None:
        return

    cx1, cy1, cx2, cy2 = crtyd
    clearance = _COURTYARD_CLEARANCE

    # Viewport with margin for annotations
    margin = 2.5
    vx1, vy1 = cx1 - margin, cy1 - margin
    vx2, vy2 = cx2 + margin, cy2 + margin
    vw, vh = vx2 - vx1, vy2 - vy1
    svg_w, svg_h = int(vw * SCALE), int(vh * SCALE)

    parts = []
    parts.append(f'<?xml version="1.0" encoding="UTF-8"?>')
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_w}" height="{svg_h}" '
        f'viewBox="{vx1} {vy1} {vw} {vh}">'
    )
    parts.append(f'  <rect x="{vx1}" y="{vy1}" width="{vw}" height="{vh}" fill="{COLORS["bg"]}"/>')

    # Courtyard clearance zone (shaded region between geometry and courtyard)
    # Outer rect (courtyard), inner cut-out (geometry bounds)
    geo_xs, geo_ys = [], []
    for pad in footprint.pads:
        hw, hh = pad.width / 2, pad.height / 2
        geo_xs.extend([pad.x - hw, pad.x + hw])
        geo_ys.extend([pad.y - hh, pad.y + hh])
    for track in footprint.tracks:
        tw = track.width / 2
        for px, py in track.points:
            geo_xs.extend([px - tw, px + tw])
            geo_ys.extend([py - tw, py + tw])
    for circle in footprint.circles:
        r = circle.radius + circle.width / 2
        geo_xs.extend([circle.cx - r, circle.cx + r])
        geo_ys.extend([circle.cy - r, circle.cy + r])
    for region in footprint.regions:
        for px, py in region.points:
            geo_xs.append(px)
            geo_ys.append(py)
    for hole in footprint.holes:
        geo_xs.extend([hole.x - hole.radius, hole.x + hole.radius])
        geo_ys.extend([hole.y - hole.radius, hole.y + hole.radius])

    if geo_xs and geo_ys:
        gx1, gy1 = min(geo_xs), min(geo_ys)
        gx2, gy2 = max(geo_xs), max(geo_ys)
        # Shaded clearance zone
        parts.append(f'  <defs>')
        parts.append(f'    <mask id="clearance-mask">')
        parts.append(f'      <rect x="{cx1}" y="{cy1}" width="{cx2-cx1}" height="{cy2-cy1}" fill="white"/>')
        parts.append(f'      <rect x="{gx1}" y="{gy1}" width="{gx2-gx1}" height="{gy2-gy1}" fill="black"/>')
        parts.append(f'    </mask>')
        parts.append(f'  </defs>')
        parts.append(
            f'  <rect x="{cx1}" y="{cy1}" width="{cx2-cx1}" height="{cy2-cy1}" '
            f'fill="#FF00FF" opacity="0.08" mask="url(#clearance-mask)"/>'
        )

    # Silkscreen tracks
    for track in footprint.tracks:
        if "Silk" in track.layer:
            color = COLORS["silk"]
        elif "Fab" in track.layer:
            color = COLORS["fab"]
        else:
            color = COLORS["silk"]
        for i in range(len(track.points) - 1):
            x1, y1 = track.points[i]
            x2, y2 = track.points[i + 1]
            parts.append(
                f'  <line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                f'stroke="{color}" stroke-width="{track.width}" stroke-linecap="round"/>'
            )

    # Circles
    for circle in footprint.circles:
        if "Silk" in circle.layer:
            color = COLORS["pin1_dot"]
        else:
            color = COLORS["fab"]
        if circle.filled:
            parts.append(
                f'  <circle cx="{circle.cx}" cy="{circle.cy}" r="{circle.radius}" '
                f'fill="{color}" stroke="{color}" stroke-width="{circle.width}"/>'
            )
        else:
            parts.append(
                f'  <circle cx="{circle.cx}" cy="{circle.cy}" r="{circle.radius}" '
                f'fill="none" stroke="{color}" stroke-width="{circle.width}"/>'
            )

    # Solid regions (pin 1 indicators)
    for region in footprint.regions:
        pts_str = " ".join(f"{x},{y}" for x, y in region.points)
        parts.append(f'  <polygon points="{pts_str}" fill="{COLORS["silk"]}" opacity="0.7"/>')

    # Pads
    for pad in footprint.pads:
        hw, hh = pad.width / 2, pad.height / 2
        parts.append(
            f'  <rect x="{pad.x - hw}" y="{pad.y - hh}" width="{pad.width}" height="{pad.height}" '
            f'fill="{COLORS["pad_smd"]}" stroke="#8B4513" stroke-width="0.05" rx="0.05"/>'
        )
        if pad.drill > 0:
            parts.append(
                f'  <circle cx="{pad.x}" cy="{pad.y}" r="{pad.drill / 2}" fill="{COLORS["drill"]}"/>'
            )

    # NPTH holes
    for hole in footprint.holes:
        parts.append(
            f'  <circle cx="{hole.x}" cy="{hole.y}" r="{hole.radius}" '
            f'fill="{COLORS["hole"]}" stroke="#555" stroke-width="0.1"/>'
        )

    # Courtyard rectangle (magenta, dashed)
    parts.append(
        f'  <rect x="{cx1}" y="{cy1}" width="{cx2-cx1}" height="{cy2-cy1}" '
        f'fill="none" stroke="{COLORS["courtyard"]}" stroke-width="0.08" stroke-dasharray="0.2,0.1"/>'
    )

    # Dimension annotations showing clearance
    font = 0.35
    dim_color = COLORS["dim_line"]

    if geo_xs:
        # Right-side clearance dimension
        arr_x = cx2
        arr_y = (cy1 + cy2) / 2
        parts.append(
            f'  <line x1="{gx2}" y1="{arr_y - 0.5}" x2="{gx2}" y2="{arr_y + 0.5}" '
            f'stroke="{dim_color}" stroke-width="0.04" stroke-dasharray="0.15,0.08"/>'
        )
        parts.append(
            f'  <line x1="{cx2}" y1="{arr_y - 0.5}" x2="{cx2}" y2="{arr_y + 0.5}" '
            f'stroke="{dim_color}" stroke-width="0.04" stroke-dasharray="0.15,0.08"/>'
        )
        parts.append(
            f'  <line x1="{gx2}" y1="{arr_y}" x2="{cx2}" y2="{arr_y}" '
            f'stroke="{dim_color}" stroke-width="0.04"/>'
        )
        # Arrow heads
        aw = 0.12
        parts.append(
            f'  <polygon points="{gx2},{arr_y} {gx2+aw},{arr_y-aw/2} {gx2+aw},{arr_y+aw/2}" fill="{dim_color}"/>'
        )
        parts.append(
            f'  <polygon points="{cx2},{arr_y} {cx2-aw},{arr_y-aw/2} {cx2-aw},{arr_y+aw/2}" fill="{dim_color}"/>'
        )
        mid_x = (gx2 + cx2) / 2
        parts.append(
            f'  <text x="{mid_x}" y="{arr_y - 0.15}" text-anchor="middle" '
            f'font-size="{font}" font-family="sans-serif" fill="{dim_color}">'
            f'≥{clearance}mm</text>'
        )

        # Bottom clearance dimension
        arr_y2 = cy2
        arr_x2 = (cx1 + cx2) / 2
        parts.append(
            f'  <line x1="{arr_x2 - 0.5}" y1="{gy2}" x2="{arr_x2 + 0.5}" y2="{gy2}" '
            f'stroke="{dim_color}" stroke-width="0.04" stroke-dasharray="0.15,0.08"/>'
        )
        parts.append(
            f'  <line x1="{arr_x2 - 0.5}" y1="{cy2}" x2="{arr_x2 + 0.5}" y2="{cy2}" '
            f'stroke="{dim_color}" stroke-width="0.04" stroke-dasharray="0.15,0.08"/>'
        )
        parts.append(
            f'  <line x1="{arr_x2}" y1="{gy2}" x2="{arr_x2}" y2="{cy2}" '
            f'stroke="{dim_color}" stroke-width="0.04"/>'
        )
        parts.append(
            f'  <polygon points="{arr_x2},{gy2} {arr_x2-aw/2},{gy2+aw} {arr_x2+aw/2},{gy2+aw}" fill="{dim_color}"/>'
        )
        parts.append(
            f'  <polygon points="{arr_x2},{cy2} {arr_x2-aw/2},{cy2-aw} {arr_x2+aw/2},{cy2-aw}" fill="{dim_color}"/>'
        )
        mid_y = (gy2 + cy2) / 2
        parts.append(
            f'  <text x="{arr_x2 + 0.3}" y="{mid_y + 0.1}" '
            f'font-size="{font}" font-family="sans-serif" fill="{dim_color}">'
            f'≥{clearance}mm</text>'
        )

    # Title
    parts.append(
        f'  <text x="{(vx1+vx2)/2}" y="{vy1 + 0.6}" text-anchor="middle" '
        f'font-size="0.5" font-family="sans-serif" fill="{COLORS["text"]}" font-weight="bold">{name}</text>'
    )

    # Legend
    legend_y = vy2 - 0.3
    legend_x = vx1 + 0.4
    items = [
        (COLORS["pad_smd"], "Pads"),
        (COLORS["silk"], "Silkscreen"),
        (COLORS["courtyard"], "Courtyard (F.CrtYd)"),
    ]
    for i, (color, label) in enumerate(items):
        lx = legend_x + i * (vw / 3.5)
        parts.append(
            f'  <rect x="{lx}" y="{legend_y - 0.2}" width="0.35" height="0.25" fill="{color}" rx="0.03"/>'
        )
        parts.append(
            f'  <text x="{lx + 0.5}" y="{legend_y}" '
            f'font-size="0.3" font-family="sans-serif" fill="{COLORS["text"]}">{label}</text>'
        )

    parts.append("</svg>")

    svg_content = "\n".join(parts)
    with open(out_path, "w") as f:
        f.write(svg_content)
    print(f"  wrote {out_path}")

    # Also write the .kicad_mod
    mod_path = out_path.replace(".svg", ".kicad_mod")
    with open(mod_path, "w") as f:
        f.write(write_footprint(footprint, name))
    print(f"  wrote {mod_path}")


if __name__ == "__main__":
    out_dir = os.path.dirname(os.path.abspath(__file__))

    cases = [
        ("QFP-32", make_qfp32()),
        ("SOT-23-5", make_sot23_5()),
        ("USB-C_Receptacle", make_usb_c()),
        ("DIP-8_THT", make_dip8_tht()),
    ]

    for name, fp in cases:
        crtyd = _compute_courtyard(fp)
        print(f"{name}: courtyard = {crtyd}")
        render_svg(name, fp, os.path.join(out_dir, f"{name}.svg"))
