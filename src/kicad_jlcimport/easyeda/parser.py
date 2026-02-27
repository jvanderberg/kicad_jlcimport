"""EasyEDA shape string parser for footprints and symbols."""

import json
import math
import re
from typing import List, Tuple

from .ee_types import (
    EE3DModel,
    EEArc,
    EECircle,
    EEFootprint,
    EEHole,
    EEPad,
    EEPin,
    EEPolyline,
    EERectangle,
    EESolidRegion,
    EESymbol,
    EEText,
    EETrack,
)

# EasyEDA layer ID -> KiCad layer name
LAYER_MAP = {
    "1": "F.Cu",
    "2": "B.Cu",
    "3": "F.SilkS",
    "4": "B.SilkS",
    "5": "F.Paste",
    "6": "B.Paste",
    "7": "F.Mask",
    "8": "B.Mask",
    "10": "Edge.Cuts",
    "12": "F.Fab",
    "99": "F.SilkS",
    "100": "F.SilkS",
    "101": "F.SilkS",
}

PIN_TYPES = {
    "0": "unspecified",
    "1": "input",
    "2": "output",
    "3": "bidirectional",
    "4": "power_in",
}

_SOLID_REGION_LAYERS = {"3", "4", "12"}


MILS_TO_MM_DIVISOR = 3.937


def mil_to_mm(mil: float) -> float:
    """Convert mils to millimeters."""
    return mil / MILS_TO_MM_DIVISOR


_SVG_ARC_RE = re.compile(
    r"M\s*([\d.e+-]+)[,\s]+([\d.e+-]+)\s*A\s*([\d.e+-]+)[,\s]+([\d.e+-]+)"
    r"[,\s]+([\d.e+-]+)[,\s]+([01])[,\s]+([01])[,\s]+([\d.e+-]+)[,\s]+([\d.e+-]+)"
)


def _parse_svg_arc_path(svg_path: str):
    """Parse an SVG arc path string (M sx sy A rx ry rot large sweep ex ey).

    Returns (sx, sy, rx, ry, large_arc, sweep, ex, ey) or None if parsing fails.
    """
    match = _SVG_ARC_RE.match(svg_path)
    if not match:
        return None
    sx = float(match.group(1))
    sy = float(match.group(2))
    rx = float(match.group(3))
    ry = float(match.group(4))
    large_arc = int(match.group(6))
    sweep = int(match.group(7))
    ex = float(match.group(8))
    ey = float(match.group(9))
    if rx <= 0 or ry <= 0:
        return None
    return (sx, sy, rx, ry, large_arc, sweep, ex, ey)


def _find_svg_path(parts: List[str], start: int = 1) -> str:
    """Find the SVG path field (starting with 'M') in a parts list."""
    for i in range(start, len(parts)):
        p = parts[i].strip()
        if p.startswith("M"):
            return p
    return ""


def parse_footprint_shapes(shapes: List[str], origin_x: float, origin_y: float) -> EEFootprint:
    """Parse footprint shape strings into an EEFootprint."""
    fp = EEFootprint()

    for shape_str in shapes:
        parts = shape_str.split("~")
        shape_type = parts[0]

        if shape_type == "PAD":
            pad = _parse_pad(parts)
            if pad:
                fp.pads.append(pad)
        elif shape_type == "TRACK":
            track = _parse_track(parts)
            if track:
                fp.tracks.append(track)
        elif shape_type == "ARC":
            arc = _parse_fp_arc(parts)
            if arc:
                fp.arcs.append(arc)
        elif shape_type == "CIRCLE":
            circle = _parse_circle(parts)
            if circle:
                fp.circles.append(circle)
        elif shape_type == "HOLE":
            hole = _parse_hole(parts)
            if hole:
                fp.holes.append(hole)
        elif shape_type == "SOLIDREGION":
            region = _parse_solid_region(parts)
            if region:
                fp.regions.append(region)
        elif shape_type == "SVGNODE":
            model = _parse_svgnode(parts)
            if model:
                fp.model = model
        elif shape_type == "TEXT":
            tracks = _parse_fp_text(parts)
            if tracks:
                fp.tracks.extend(tracks)
        elif shape_type == "RECT":
            track = _parse_rect_as_tracks(parts)
            if track:
                fp.tracks.extend(track)

    # Apply origin offset to all coordinates
    ox = mil_to_mm(origin_x)
    oy = mil_to_mm(origin_y)

    for pad in fp.pads:
        pad.x -= ox
        pad.y -= oy
    for track in fp.tracks:
        track.points = [(x - ox, y - oy) for x, y in track.points]
    for arc in fp.arcs:
        arc.start = (arc.start[0] - ox, arc.start[1] - oy)
        arc.end = (arc.end[0] - ox, arc.end[1] - oy)
    for circle in fp.circles:
        circle.cx -= ox
        circle.cy -= oy
    for hole in fp.holes:
        hole.x -= ox
        hole.y -= oy
    for region in fp.regions:
        region.points = [(x - ox, y - oy) for x, y in region.points]

    return fp


def parse_symbol_shapes(shapes: List[str], origin_x: float, origin_y: float) -> EESymbol:
    """Parse symbol shape strings into an EESymbol."""
    sym = EESymbol()

    for shape_str in shapes:
        # Symbols use ^^ as sub-delimiter within pin shapes
        if shape_str.startswith("P~"):
            pin = _parse_pin(shape_str, origin_x, origin_y)
            if pin:
                sym.pins.append(pin)
        elif shape_str.startswith("R~"):
            rect = _parse_sym_rect(shape_str, origin_x, origin_y)
            if rect:
                sym.rectangles.append(rect)
        elif shape_str.startswith("E~"):
            circle = _parse_sym_circle(shape_str, origin_x, origin_y)
            if circle:
                sym.circles.append(circle)
        elif shape_str.startswith("PL~") or shape_str.startswith("PG~"):
            poly = _parse_sym_polyline(shape_str, origin_x, origin_y)
            if poly:
                sym.polylines.append(poly)
        elif shape_str.startswith("A~"):
            arc = _parse_sym_arc(shape_str, origin_x, origin_y)
            if arc:
                sym.arcs.append(arc)
        elif shape_str.startswith("PT~"):
            path = _parse_sym_path(shape_str, origin_x, origin_y)
            if path:
                sym.polylines.append(path)
        elif shape_str.startswith("C~"):
            # C~ is circle (different from E~ ellipse in some versions)
            circle = _parse_sym_circle_c(shape_str, origin_x, origin_y)
            if circle:
                sym.circles.append(circle)
        elif shape_str.startswith("T~"):
            text = _parse_sym_text(shape_str, origin_x, origin_y)
            if text:
                sym.texts.append(text)

    return sym


# --- Footprint shape parsers ---


def _parse_pad(parts: List[str]) -> EEPad:
    """Parse PAD shape string."""
    # PAD~shape~x~y~sx~sy~layer~<empty>~number~drill~polygon_nodes~rotation~id~...
    shape = parts[1]
    x = float(parts[2])
    y = float(parts[3])
    sx = float(parts[4])
    sy = float(parts[5])
    layer = parts[6]
    # parts[7] is empty (net field)
    number = parts[8] if len(parts) > 8 else ""
    drill = float(parts[9]) if len(parts) > 9 and parts[9] else 0.0

    # parts[10] = polygon_nodes (space-separated coords, empty for ELLIPSE)
    # parts[11] = rotation
    # parts[13] = slot length (oval drill slot, in mils — full length, not radius)
    polygon_str = parts[10] if len(parts) > 10 else ""
    rotation = float(parts[11]) if len(parts) > 11 and parts[11] else 0.0
    try:
        slot_length_mil = float(parts[13]) if len(parts) > 13 and parts[13] else 0.0
    except ValueError:
        slot_length_mil = 0.0

    poly_points: List[float] = []
    if polygon_str and shape == "POLYGON":
        try:
            coords = [float(c) for c in polygon_str.strip().split(" ") if c]
        except ValueError:
            return None  # Reject pad with invalid polygon coordinates
        # Store as pad-center-relative coordinates in mm (pairs of x, y)
        for i in range(0, len(coords) - 1, 2):
            poly_points.append(mil_to_mm(coords[i] - x))
            poly_points.append(mil_to_mm(coords[i + 1] - y))

    return EEPad(
        shape=shape,
        x=mil_to_mm(x),
        y=mil_to_mm(y),
        width=mil_to_mm(sx),
        height=mil_to_mm(sy),
        layer=layer,
        number=number,
        drill=mil_to_mm(drill * 2),  # Convert radius to diameter, then to mm
        rotation=rotation,
        polygon_points=poly_points,
        slot_length=mil_to_mm(slot_length_mil),
    )


def _parse_track(parts: List[str]) -> EETrack:
    """Parse TRACK shape string."""
    # TRACK~width~layer~[id]~points...
    width = float(parts[1])
    layer = parts[2]

    # Find the points field - may be at index 3 or 4
    points_str = ""
    for i in range(3, len(parts)):
        if " " in parts[i] and any(c.isdigit() for c in parts[i]):
            points_str = parts[i]
            break

    if not points_str:
        return None

    coords = points_str.strip().split(" ")
    points = []
    for i in range(0, len(coords) - 1, 2):
        try:
            px = mil_to_mm(float(coords[i]))
            py = mil_to_mm(float(coords[i + 1]))
            points.append((px, py))
        except (ValueError, IndexError):
            continue

    if len(points) < 2:
        return None

    kicad_layer = LAYER_MAP.get(layer, "F.SilkS")
    return EETrack(width=mil_to_mm(width), layer=kicad_layer, points=points)


def _parse_fp_arc(parts: List[str]) -> EEArc:
    """Parse footprint ARC shape string."""
    # ARC~width~layer~[S$xx]~svg_path~...
    width = float(parts[1])
    layer = parts[2]

    # Find SVG path - look for field starting with "M"
    svg_path = _find_svg_path(parts, start=3)
    if not svg_path:
        return None

    parsed = _parse_svg_arc_path(svg_path)
    if not parsed:
        return None
    sx, sy, rx, ry, large_arc, sweep, ex, ey = parsed

    kicad_layer = LAYER_MAP.get(layer, "F.SilkS")
    return EEArc(
        width=mil_to_mm(width),
        layer=kicad_layer,
        start=(mil_to_mm(sx), mil_to_mm(sy)),
        end=(mil_to_mm(ex), mil_to_mm(ey)),
        rx=mil_to_mm(rx),
        ry=mil_to_mm(ry),
        large_arc=large_arc,
        sweep=sweep,
    )


def _parse_circle(parts: List[str]) -> EECircle:
    """Parse CIRCLE shape string."""
    # CIRCLE~cx~cy~radius~width~layer~id~flag~...
    cx = float(parts[1])
    cy = float(parts[2])
    radius = float(parts[3])
    width = float(parts[4])
    layer = parts[5]
    # Flag at position 7 - "0" may indicate auxiliary/interior circles
    flag = parts[7] if len(parts) > 7 else ""

    # Skip decorative/annotation circles:
    # - Layer 100: Lead shape layer (decorative pad circles)
    # - Layer 101: Component Marking Layer (small annotation markers)
    if layer in ("100", "101"):
        return None

    kicad_layer = LAYER_MAP.get(layer, "F.SilkS")
    # When stroke width >= 2*radius the stroke covers the entire circle,
    # which is how EasyEDA represents a solid filled circle.
    filled = width >= 2 * radius and radius > 0
    return EECircle(
        cx=mil_to_mm(cx),
        cy=mil_to_mm(cy),
        radius=mil_to_mm(radius),
        width=mil_to_mm(width),
        layer=kicad_layer,
        flag=flag,
        filled=filled,
    )


def _parse_hole(parts: List[str]) -> EEHole:
    """Parse HOLE shape string."""
    # HOLE~x~y~radius~...
    x = float(parts[1])
    y = float(parts[2])
    radius = float(parts[3])
    return EEHole(x=mil_to_mm(x), y=mil_to_mm(y), radius=mil_to_mm(radius))


def _parse_solid_region(parts: List[str]) -> EESolidRegion:
    """Parse SOLIDREGION shape string."""
    # SOLIDREGION~layer_data~[id]~svg_path~type~...
    layer = parts[1]

    # Find SVG path and type
    svg_path = ""
    region_type = "solid"
    for i in range(2, len(parts)):
        p = parts[i].strip()
        # SVG paths start with M followed by coordinates (with or without space)
        if p.startswith("M") and len(p) > 1 and (p[1].isdigit() or p[1] in " \t-"):
            svg_path = p
        elif p in ("npth", "solid", "cutout"):
            region_type = p

    if not svg_path:
        return None

    # Handle npth (edge cuts) and silkscreen solid regions
    if region_type == "npth":
        # Parse M x y L x y L x y ... Z
        points = _parse_svg_polygon(svg_path)
        if not points:
            return None
        return EESolidRegion(layer="Edge.Cuts", points=points, region_type=region_type)

    # Import solid regions on silk/fab layers (e.g., pin 1 dots, polarity marks)
    if layer in _SOLID_REGION_LAYERS and region_type == "solid":
        kicad_layer = LAYER_MAP.get(layer, "F.SilkS")
        # Check if path contains arc commands - if so, use arc parser
        if " A " in svg_path or "\tA " in svg_path:
            points = _parse_svg_path_with_arcs(svg_path)
            if points:
                return EESolidRegion(layer=kicad_layer, points=points, region_type=region_type)
        # Otherwise try to parse as polygon (needs at least 3 points)
        points = _parse_svg_polygon(svg_path)
        if len(points) >= 3:
            return EESolidRegion(layer=kicad_layer, points=points, region_type=region_type)

    return None


def _parse_svg_polygon(svg_path: str) -> List[Tuple[float, float]]:
    """Parse SVG path with M and L commands into point list."""
    points = []
    # Remove Z at end
    path = svg_path.replace("Z", "").replace("z", "").strip()
    # Split on M and L commands
    tokens = re.split(r"[ML]\s*", path)
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        # Split on whitespace or commas
        coords = re.split(r"[,\s]+", token)
        if len(coords) >= 2:
            try:
                x = mil_to_mm(float(coords[0]))
                y = mil_to_mm(float(coords[1]))
                points.append((x, y))
            except ValueError:
                continue
    return points


def _parse_svg_path_with_arcs(svg_path: str) -> List[Tuple[float, float]]:
    """Parse SVG path containing arc commands, approximating arcs as polygons.

    Handles paths like: M x y A rx ry rot large sweep ex ey A rx ry rot large sweep ex ey
    which draw circles using two 180-degree arcs.
    """
    points = []
    # Remove Z at end
    path = svg_path.replace("Z", "").replace("z", "").strip()

    # Extract starting point from M command
    m_match = re.match(r"M\s*([\d.e+-]+)\s+([\d.e+-]+)", path)
    if not m_match:
        return []

    start_x = float(m_match.group(1))
    start_y = float(m_match.group(2))

    # Find all arc commands
    arc_pattern = r"A\s*([\d.e+-]+)\s+([\d.e+-]+)\s+([\d.e+-]+)\s+([01])\s+([01])\s+([\d.e+-]+)\s+([\d.e+-]+)"
    arcs = re.findall(arc_pattern, path)

    if not arcs:
        return []

    # For a circle made of two arcs, compute center and radius
    # First arc goes from start to end1, second arc goes from end1 back to start
    rx = float(arcs[0][0])
    ry = float(arcs[0][1])
    end1_x = float(arcs[0][5])
    end1_y = float(arcs[0][6])

    # Center is midpoint between start and end1 (for a circle)
    cx = (start_x + end1_x) / 2
    cy = (start_y + end1_y) / 2
    radius = (rx + ry) / 2  # Average for slight ellipses

    # Generate polygon approximation of circle (16 segments)
    num_segments = 16
    for i in range(num_segments):
        angle = 2 * math.pi * i / num_segments
        px = cx + radius * math.cos(angle)
        py = cy + radius * math.sin(angle)
        points.append((mil_to_mm(px), mil_to_mm(py)))

    return points


def _parse_svgnode(parts: List[str]) -> EE3DModel:
    """Parse SVGNODE shape string for 3D model info."""
    # SVGNODE~{json}
    json_str = "~".join(parts[1:])  # Rejoin in case JSON contained ~
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return None

    attrs = data.get("attrs", {})
    uuid = attrs.get("uuid", "")
    if not uuid:
        return None

    c_origin = attrs.get("c_origin", "0,0").split(",")
    origin_x = float(c_origin[0]) if len(c_origin) > 0 else 0
    origin_y = float(c_origin[1]) if len(c_origin) > 1 else 0

    z = float(attrs.get("z", "0"))

    c_rotation = attrs.get("c_rotation", "0,0,0").split(",")
    rot = tuple(-float(a) for a in c_rotation[:3]) if len(c_rotation) >= 3 else (0, 0, 0)

    return EE3DModel(
        uuid=uuid,
        origin_x=origin_x,
        origin_y=origin_y,
        z=z,
        rotation=rot,
    )


def _parse_rect_as_tracks(parts: List[str]) -> List[EETrack]:
    """Parse RECT shape as track segments."""
    # RECT~x~y~dx~dy~layer~...~width~...
    try:
        x = float(parts[1])
        y = float(parts[2])
        dx = float(parts[3])
        dy = float(parts[4])
        layer = parts[5]
        width = float(parts[8]) if len(parts) > 8 and parts[8] else 0.0
    except (ValueError, IndexError):
        return []

    kicad_layer = LAYER_MAP.get(layer, "F.SilkS")
    x1, y1 = mil_to_mm(x), mil_to_mm(y)
    x2, y2 = mil_to_mm(x + dx), mil_to_mm(y + dy)
    w = mil_to_mm(width) if width > 0 else 0.1

    points = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
    return [EETrack(width=w, layer=kicad_layer, points=points)]


def _parse_fp_text(parts: List[str]) -> List[EETrack]:
    """Parse footprint TEXT shape as track segments from its SVG stroke path.

    Format: TEXT~type~x~y~stroke_width~rotation~?~layer~~font_size~text~svg_path~...
    The SVG path contains the actual stroke geometry (M/L commands).
    """
    try:
        width = float(parts[4]) if parts[4] else 0.0
        layer = parts[7]
        svg_path = parts[11] if len(parts) > 11 else ""
    except (ValueError, IndexError):
        return []

    if not svg_path or layer not in LAYER_MAP:
        return []

    kicad_layer = LAYER_MAP[layer]
    w = mil_to_mm(width) if width > 0 else 0.1

    # Split SVG path on M commands to get individual sub-paths
    # Each sub-path is M x y L x y [L x y ...]
    tracks = []
    segments = re.split(r"M\s*", svg_path.strip())
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        # Parse all coordinates (M start + L continuations)
        tokens = re.split(r"L\s*", seg)
        points = []
        for token in tokens:
            token = token.strip()
            if not token:
                continue
            coords = re.split(r"[,\s]+", token)
            if len(coords) >= 2:
                try:
                    px = mil_to_mm(float(coords[0]))
                    py = mil_to_mm(float(coords[1]))
                    points.append((px, py))
                except ValueError:
                    continue
        if len(points) >= 2:
            tracks.append(EETrack(width=w, layer=kicad_layer, points=points))

    return tracks


# --- Symbol shape parsers ---


def _parse_pin(shape_str: str, origin_x: float, origin_y: float) -> EEPin:
    """Parse pin shape string."""
    # Split on ^^ first to get sub-parts
    sections = shape_str.split("^^")
    # First section has the main pin data
    main_parts = sections[0].split("~")

    # P~show~elec_type~spice_index~x~y~rotation~id~...
    # Note: main_parts[3] is the SPICE pin index, not the display number.
    # The actual display pin number is in section 4 (pin number text).
    try:
        elec_code = main_parts[2]
        x = float(main_parts[4])
        y = float(main_parts[5])
        rotation_str = main_parts[6] if len(main_parts) > 6 else ""
        rotation = float(rotation_str) if rotation_str else 0.0
    except (ValueError, IndexError):
        return None

    # Default to SPICE index; overridden below by display number if available
    number = main_parts[3]

    # Parse pin length and direction from the path section (section index 2)
    # The SVG path is the source of truth for pin direction.
    # The path can start at the pin position and go toward the body, OR start
    # at the body edge and go toward the pin position.  We compare the M start
    # coordinates with the pin position to determine which case we have.
    length = 10.0  # default
    path_direction = None  # Will be 0, 90, 180, or 270
    if len(sections) > 2:
        path_section = sections[2]
        # Path is like "M360,290h10" or "M 440 310 h -10" or "M400,300v10"
        m_match = re.match(r"M\s*([-\d.]+)[,\s]+([-\d.]+)", path_section)
        h_match = re.search(r"h\s*([-\d.]+)", path_section)
        v_match = re.search(r"v\s*([-\d.]+)", path_section)
        # Check whether path M start matches the pin position (both axes).
        # Default True so we preserve original behaviour when M is unparseable.
        starts_at_pin = True
        if m_match:
            mx = float(m_match.group(1))
            my = float(m_match.group(2))
            starts_at_pin = abs(mx - x) < 0.5 and abs(my - y) < 0.5

        if h_match:
            val = float(h_match.group(1))
            length = abs(val)
            if starts_at_pin:
                # Path goes from pin connection point toward body
                path_direction = 0 if val > 0 else 180
            else:
                # Path goes from body toward pin — flip direction
                path_direction = 180 if val > 0 else 0
        elif v_match:
            val = float(v_match.group(1))
            length = abs(val)
            if starts_at_pin:
                # Path goes from pin connection point toward body
                path_direction = 270 if val > 0 else 90
            else:
                # Path goes from body toward pin — flip direction
                path_direction = 90 if val > 0 else 270

    # Parse pin name from section 3 (name display)
    # Format: visible~x~y~rotation~text~alignment~...~color
    name = ""
    name_visible = True
    if len(sections) > 3:
        name_parts = sections[3].split("~")
        # Text is at index 4
        if len(name_parts) > 4:
            name = name_parts[4]
        # Visibility: "0" in first field means hidden
        if name_parts and name_parts[0] == "0":
            name_visible = False

    # Parse pin number text and visibility from section 4
    # Format: visible~x~y~rotation~number_text~alignment~...~color
    number_visible = True
    if len(sections) > 4:
        num_parts = sections[4].split("~")
        if num_parts and num_parts[0] == "0":
            number_visible = False
        if len(num_parts) > 4 and num_parts[4]:
            number = num_parts[4]

    # Use path direction if available, otherwise fall back to rotation field with +180
    if path_direction is not None:
        kicad_rotation = path_direction
    else:
        kicad_rotation = (rotation + 180) % 360

    # Convert coordinates (symbol: origin-relative, Y-inverted)
    kicad_x = mil_to_mm(x - origin_x)
    kicad_y = -mil_to_mm(y - origin_y)

    electrical_type = PIN_TYPES.get(elec_code, "unspecified")

    return EEPin(
        number=number,
        name=name,
        x=kicad_x,
        y=kicad_y,
        rotation=kicad_rotation,
        length=mil_to_mm(length),
        electrical_type=electrical_type,
        name_visible=name_visible,
        number_visible=number_visible,
    )


def _parse_sym_rect(shape_str: str, origin_x: float, origin_y: float) -> EERectangle:
    """Parse symbol rectangle."""
    parts = shape_str.split("~")
    # R~x~y~[rx]~[ry]~width~height~... (12+ fields)
    # or R~x~y~width~height~... (shorter)
    try:
        x = float(parts[1])
        y = float(parts[2])
        rx = 0.0
        if len(parts) >= 12:
            rx = float(parts[3]) if parts[3] else 0.0
            w = float(parts[5])
            h = float(parts[6])
        else:
            w = float(parts[3])
            h = float(parts[4])
    except (ValueError, IndexError):
        return None

    kx = mil_to_mm(x - origin_x)
    ky = -mil_to_mm(y - origin_y)
    kw = mil_to_mm(w)
    kh = mil_to_mm(h)
    kr = mil_to_mm(rx)

    return EERectangle(x=kx, y=ky, width=kw, height=-kh, corner_radius=kr)


def _parse_sym_circle(shape_str: str, origin_x: float, origin_y: float) -> EECircle:
    """Parse symbol circle/ellipse."""
    parts = shape_str.split("~")
    # E~cx~cy~rx~ry~stroke_color~stroke_width~?~fill_color~id~...
    try:
        cx = float(parts[1])
        cy = float(parts[2])
        radius = float(parts[3])
    except (ValueError, IndexError):
        return None

    # Check for fill color at parts[8] - if it's a color (starts with #) and not "none", it's filled
    filled = False
    if len(parts) > 8:
        fill_color = parts[8].strip().lower()
        if fill_color.startswith("#") and fill_color != "none":
            filled = True

    return EECircle(
        cx=mil_to_mm(cx - origin_x),
        cy=-mil_to_mm(cy - origin_y),
        radius=mil_to_mm(radius),
        width=0.254,
        layer="",
        filled=filled,
    )


def _parse_sym_polyline(shape_str: str, origin_x: float, origin_y: float) -> EEPolyline:
    """Parse symbol polyline/polygon."""
    parts = shape_str.split("~")
    is_polygon = parts[0] == "PG"

    # Points are consecutive x,y pairs.
    # Format 1 (space-separated in one field): PL~13 -8 13 8~#880000~...
    # Format 2 (tilde-separated): PL~100~100~200~200~0~3
    points = []

    # Check if coordinates are space-separated in a single field
    if len(parts) > 1 and " " in parts[1]:
        coords = parts[1].strip().split()
        i = 0
        while i + 1 < len(coords):
            try:
                x = float(coords[i])
                y = float(coords[i + 1])
                points.append((mil_to_mm(x - origin_x), -mil_to_mm(y - origin_y)))
                i += 2
            except (ValueError, IndexError):
                break
    else:
        # Tilde-separated: last fields are stroke/layer
        i = 1
        while i < len(parts) - 2:
            try:
                x = float(parts[i])
                y = float(parts[i + 1])
                points.append((mil_to_mm(x - origin_x), -mil_to_mm(y - origin_y)))
                i += 2
            except (ValueError, IndexError):
                break

    if len(points) < 2:
        return None

    return EEPolyline(points=points, closed=is_polygon, fill=is_polygon)


def _parse_sym_path(shape_str: str, origin_x: float, origin_y: float) -> EEPolyline:
    """Parse symbol path (PT~) - SVG-style path shapes.

    Format: PT~<svg_path>~<stroke_color>~<stroke_width>~<stroke_style>~<fill_color>~<id>~...
    Example: PT~M 414 279 L 412 275 L 410 279 Z ~#880000~1~0~#880000~gge44~0~
    """
    parts = shape_str.split("~")
    if len(parts) < 2:
        return None

    svg_path = parts[1].strip()
    if not svg_path:
        return None

    # Parse SVG path manually to apply origin offset correctly
    # (can't use _parse_svg_polygon directly as it converts to mm before we apply offset)
    points = []
    path = svg_path.replace("Z", "").replace("z", "").strip()
    tokens = re.split(r"[ML]\s*", path)
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        # Split on whitespace or commas for consistency with _parse_svg_polygon
        coords = re.split(r"[,\s]+", token)
        if len(coords) >= 2:
            try:
                x = float(coords[0])
                y = float(coords[1])
                points.append((mil_to_mm(x - origin_x), -mil_to_mm(y - origin_y)))
            except ValueError:
                continue

    if len(points) < 2:
        return None

    # Check if filled (fill color is not "none" and is specified)
    filled = False
    if len(parts) > 5:
        fill_color = parts[5].strip().lower() if parts[5] else ""
        filled = fill_color and fill_color != "none"

    # Path with Z is closed
    is_closed = "Z" in svg_path.upper()

    return EEPolyline(points=points, closed=is_closed, fill=filled)


def _parse_sym_circle_c(shape_str: str, origin_x: float, origin_y: float) -> EECircle:
    """Parse symbol circle (C~ format).

    Format: C~cx~cy~radius~stroke_color~stroke_width~fill_color~id~...
    """
    parts = shape_str.split("~")
    try:
        cx = float(parts[1])
        cy = float(parts[2])
        radius = float(parts[3])
    except (ValueError, IndexError):
        return None

    # Check for fill color
    filled = False
    if len(parts) > 6:
        fill_color = parts[6].strip().lower() if parts[6] else ""
        if fill_color and fill_color.startswith("#") and fill_color != "none":
            filled = True

    return EECircle(
        cx=mil_to_mm(cx - origin_x),
        cy=-mil_to_mm(cy - origin_y),
        radius=mil_to_mm(radius),
        width=0.254,
        layer="",
        filled=filled,
    )


def _parse_sym_text(shape_str: str, origin_x: float, origin_y: float) -> EEText:
    """Parse symbol text label (T~ format).

    Format: T~type~x~y~rotation~color~font~size~?~?~anchor~?~text~...
    Example: T~L~400~290~0~#0000FF~Tahoma~11.5pt~0.1~~middle~comment~RP2040~1~middle~gge860~0~pinpart
    """
    parts = shape_str.split("~")
    try:
        x = float(parts[2])
        y = float(parts[3])
        rotation = float(parts[4]) if parts[4] else 0.0
        text = parts[12] if len(parts) > 12 else ""
    except (ValueError, IndexError):
        return None

    if not text:
        return None

    # Parse font size (e.g., "11.5pt" -> 11.5 points -> ~4mm)
    font_size = 1.27  # default
    if len(parts) > 7:
        size_str = parts[7]
        if "pt" in size_str:
            try:
                pt_size = float(size_str.replace("pt", ""))
                font_size = pt_size * 0.3528  # points to mm
            except ValueError:
                pass

    return EEText(
        text=text,
        x=mil_to_mm(x - origin_x),
        y=-mil_to_mm(y - origin_y),
        rotation=rotation,
        font_size=font_size,
    )


def _parse_sym_arc(shape_str: str, origin_x: float, origin_y: float) -> EEArc:
    """Parse symbol arc."""
    parts = shape_str.split("~")
    svg_path = _find_svg_path(parts, start=1)
    if not svg_path:
        return None

    parsed = _parse_svg_arc_path(svg_path)
    if not parsed:
        return None
    sx, sy, rx, ry, large_arc, sweep, ex, ey = parsed

    # When Y is flipped (negated), arc sweep direction must also flip
    flipped_sweep = 1 - sweep

    return EEArc(
        width=0.254,
        layer="",
        start=(mil_to_mm(sx - origin_x), -mil_to_mm(sy - origin_y)),
        end=(mil_to_mm(ex - origin_x), -mil_to_mm(ey - origin_y)),
        rx=mil_to_mm(rx),
        ry=mil_to_mm(ry),
        large_arc=large_arc,
        sweep=flipped_sweep,
    )


def compute_arc_midpoint(
    start: Tuple[float, float], end: Tuple[float, float], rx: float, ry: float, large_arc: int, sweep: int
) -> Tuple[float, float]:
    """Compute the midpoint on an SVG arc for KiCad's start/mid/end format."""
    sx, sy = start
    ex, ey = end

    # Midpoint of chord
    mx = (sx + ex) / 2
    my = (sy + ey) / 2

    # Direction perpendicular to chord
    dx = ex - sx
    dy = ey - sy
    chord_len = math.sqrt(dx * dx + dy * dy)
    if chord_len < 1e-10:
        return (mx, my)

    # Use average radius
    r = (rx + ry) / 2
    if r < chord_len / 2:
        r = chord_len / 2

    # Distance from midpoint to center
    h = math.sqrt(max(0, r * r - (chord_len / 2) ** 2))

    # Perpendicular direction (normalized)
    px = -dy / chord_len
    py = dx / chord_len

    # Choose center side based on large_arc and sweep
    if large_arc != sweep:
        cx = mx + h * px
        cy = my + h * py
    else:
        cx = mx - h * px
        cy = my - h * py

    # Midpoint on arc: angle bisector from center
    a_start = math.atan2(sy - cy, sx - cx)
    a_end = math.atan2(ey - cy, ex - cx)

    # Handle angle wrapping based on sweep direction
    if sweep == 1:
        if a_end <= a_start:
            a_end += 2 * math.pi
    else:
        if a_end >= a_start:
            a_end -= 2 * math.pi

    a_mid = (a_start + a_end) / 2
    mid_x = cx + r * math.cos(a_mid)
    mid_y = cy + r * math.sin(a_mid)

    return (mid_x, mid_y)
