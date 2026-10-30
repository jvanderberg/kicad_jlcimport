"""3D model placement, WRL conversion, and file saving.

Placement
---------
EasyEDA describes where a footprint's 3D model sits with an ``SVGNODE``
carrying ``c_origin``, ``z``, ``c_rotation`` and an ``outline3D`` drawing:
EasyEDA's own top-view outline of the placed model.  ``c_origin`` is not
reliable (it can be millimetres, or hundreds of millimetres, away from the
model), but the outline matches what EasyEDA renders, so it is the placement
target.  :func:`place_model` works in three steps:

1. **Rotation.**  EasyEDA's 3D view composes ``c_rotation`` as
   ``Rx · Ry · Rz`` (Z applied to the model first); KiCad applies its file
   rotation as ``Rz(-rz) · Ry(-ry) · Rx(-rx)`` (X first).  The EasyEDA matrix
   is decomposed into KiCad's order.  For models rotated about Z only, which
   is nearly all of them, this is just ``-c_rotation``.
2. **Anchor.**  EasyEDA draws the outline with only the Z rotation applied, so
   the model's Z-rotated XY bounding box is centred on the outline's bounding
   box.  If the outline is missing or is not a drawing of this model (its size
   does not match), ``c_origin`` is used instead, unless that would put the
   model clear of its own footprint, in which case the model is centred on the
   footprint origin.  Vertically, EasyEDA lifts the unrotated model so its
   lowest point is at ``z`` and then rotates it; for Z-only rotations that
   puts the lowest point at ``z``.
3. **Pin fit.**  If the footprint's pads are given, the model's pins (where its
   geometry crosses the board surface on through-hole pads, or rests on it on
   SMD pads) are paired with the pads they land on.  The mean pin-to-pad error
   is removed when doing so centres every pin on its pad to within
   :data:`PIN_FIT_TOLERANCE_MM`.  Through-hole pins have one physically correct
   position, so this corrects EasyEDA's own placement error.  SMD land patterns
   that extend past their terminals leave a larger residual and keep the
   anchor placement.

KiCad's model offset has its Y axis pointing up, opposite to the board's Y.

Why WRL instead of STEP for KiCad model references
--------------------------------------------------
EasyEDA provides 3D models in OBJ format, which we convert to WRL (VRML) with
exactly the same geometry and coordinate system, so the offsets computed from
the OBJ apply to the WRL unchanged.  STEP files from EasyEDA can have a
different origin or orientation.  Both files are saved, but KiCad footprints
reference the WRL.
"""

import math
import os
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from ..easyeda.ee_types import EE3DModel, EEPad
from ..easyeda.parser import mil_to_mm
from ._fileio import atomic_write

Vec3 = Tuple[float, float, float]
Point = Tuple[float, float]
Box = Tuple[float, float, float, float]  # min_x, max_x, min_y, max_y
Matrix = Tuple[Vec3, Vec3, Vec3]

# The outline is used only when its size matches the model's Z-rotated XY size
# to within this fraction; otherwise it is not a drawing of this model.
OUTLINE_SIZE_TOLERANCE = 0.05

# The pin fit is applied only when every paired pin then lies on its pad
# centre to within this RMS distance (mm).
PIN_FIT_TOLERANCE_MM = 0.05

# Geometry within this distance of the board surface (mm) rests on it.
_BOARD_CONTACT_MM = 0.05

# Placement anchors (ModelPlacement.anchor)
ANCHOR_OUTLINE = "outline"  # EasyEDA's outline3D drawing
ANCHOR_C_ORIGIN = "c_origin"  # outline unusable; EasyEDA's c_origin
ANCHOR_FOOTPRINT = "footprint"  # c_origin implausible; centred on the footprint origin
ANCHOR_NONE = "none"  # no model geometry

# Pin fit outcomes (ModelPlacement.pins)
PINS_ALIGNED = "aligned"  # pins centred on their pads; fit applied
PINS_OFFSET = "offset"  # pins land on pads but not centred; anchor kept
PINS_UNMATCHED = "unmatched"  # fewer than two pads have model geometry on them
PINS_NOT_CHECKED = "not_checked"  # no pads given


@dataclass
class ModelPlacement:
    """KiCad 3D model transform and how it was derived."""

    offset: Vec3
    rotation: Vec3
    anchor: str
    pins: str = PINS_NOT_CHECKED
    pin_rms: Optional[float] = None  # RMS pin-to-pad error after the fit (mm)


class _Mesh:
    """Vertices (mm), faces (vertex indices) and undirected edges of an OBJ model."""

    def __init__(self, vertices: List[Vec3], faces: List[List[int]], edges: List[Tuple[int, int]]):
        self.vertices = vertices
        self.faces = faces
        self.edges = edges


def _parse_obj_mesh(obj_source: str) -> _Mesh:
    vertices: List[Vec3] = []
    faces: List[List[int]] = []
    edges = set()
    for raw in obj_source.split("\n"):
        line = raw.strip()
        if line.startswith("v "):
            parts = line.split()
            if len(parts) < 4:
                continue
            try:
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            except ValueError:
                continue
        elif line.startswith("f "):
            idx = []
            for token in line.split()[1:]:
                try:
                    idx.append(int(token.split("/")[0]) - 1)
                except ValueError:
                    continue
            faces.append(idx)
            for i, a in enumerate(idx):
                b = idx[(i + 1) % len(idx)]
                if a != b:
                    edges.add((min(a, b), max(a, b)))
    count = len(vertices)
    faces = [f for f in faces if len(f) >= 3 and all(0 <= i < count for i in f)]
    return _Mesh(vertices, faces, [(a, b) for a, b in edges if 0 <= a < count and 0 <= b < count])


def _rot(axis: str, deg: float) -> Matrix:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    if axis == "x":
        return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))
    if axis == "y":
        return ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))
    return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


def _matmul(a: Matrix, b: Matrix) -> Matrix:
    rows = []
    for i in range(3):
        rows.append(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)))
    return (rows[0], rows[1], rows[2])  # type: ignore[return-value]


def _apply(m: Matrix, v: Vec3) -> Vec3:
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def _kicad_rotation(rotation: Vec3) -> Matrix:
    """The rotation KiCad applies for a model file rotation (rx, ry, rz)."""
    rx, ry, rz = rotation
    return _matmul(_matmul(_rot("z", -rz), _rot("y", -ry)), _rot("x", -rx))


def _normalize_deg(deg: float) -> float:
    deg = math.fmod(round(deg, 6), 360.0)
    if deg <= -180.0:
        deg += 360.0
    elif deg > 180.0:
        deg -= 360.0
    return 0.0 if deg == 0 else deg


def _kicad_file_rotation(model_rotation: Vec3) -> Vec3:
    """Convert EasyEDA's model rotation to the KiCad file rotation (step 1).

    ``model_rotation`` holds EasyEDA's ``c_rotation`` negated (see the parser).
    """
    ex, ey, ez = (-a for a in model_rotation)
    m = _matmul(_matmul(_rot("x", ex), _rot("y", ey)), _rot("z", ez))
    # m == Rz(a) · Ry(b) · Rx(c); KiCad applies Rz(-rz) · Ry(-ry) · Rx(-rx).
    sin_b = max(-1.0, min(1.0, -m[2][0]))
    b = math.degrees(math.asin(sin_b))
    if abs(sin_b) < 1.0 - 1e-9:
        a = math.degrees(math.atan2(m[1][0], m[0][0]))
        c = math.degrees(math.atan2(m[2][1], m[2][2]))
    else:  # gimbal lock: fold the X angle into Z
        a = math.degrees(math.atan2(-m[0][1], m[1][1]))
        c = 0.0
    return (_normalize_deg(-c), _normalize_deg(-b), _normalize_deg(-a))


def _box_of(points: Sequence[Point]) -> Box:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), max(xs), min(ys), max(ys)


def _overlaps(a: Box, b: Box) -> bool:
    return a[0] <= b[1] and b[0] <= a[1] and a[2] <= b[3] and b[2] <= a[3]


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= OUTLINE_SIZE_TOLERANCE * max(abs(a), abs(b))


def _footprint_box(pads: Optional[Sequence[EEPad]]) -> Box:
    """Board-coordinate area the model must cover: its pads, or the origin."""
    if not pads:
        return (0.0, 0.0, 0.0, 0.0)
    corners = []
    for pad in pads:
        hx, hy = _pad_half_extents(pad)
        corners.extend([(pad.x - hx, pad.y - hy), (pad.x + hx, pad.y + hy)])
    return _box_of(corners)


def _anchor(
    model: EE3DModel,
    fp_origin_x: float,
    fp_origin_y: float,
    mesh: _Mesh,
    rotation: Vec3,
    pads: Optional[Sequence[EEPad]],
) -> Tuple[Vec3, str]:
    """Offset that puts the model where EasyEDA draws it (step 2)."""
    rz = _rot("z", -rotation[2])
    min_x, max_x, min_y, max_y = _box_of([_apply(rz, v)[:2] for v in mesh.vertices])
    cx, cy = (min_x + max_x) / 2, (min_y + max_y) / 2
    half_w, half_h = (max_x - min_x) / 2, (max_y - min_y) / 2

    # Target centre in board coordinates (mm, Y down, relative to the footprint origin)
    anchor = ANCHOR_C_ORIGIN
    target_x, target_y = model.origin_x, model.origin_y
    if model.outline is not None:
        o_min_x, o_max_x, o_min_y, o_max_y = model.outline
        width, height = mil_to_mm(o_max_x - o_min_x), mil_to_mm(o_max_y - o_min_y)
        if _close(width, 2 * half_w) and _close(height, 2 * half_h):
            target_x, target_y = (o_min_x + o_max_x) / 2, (o_min_y + o_max_y) / 2
            anchor = ANCHOR_OUTLINE
    tx, ty = mil_to_mm(target_x - fp_origin_x), mil_to_mm(target_y - fp_origin_y)
    if anchor == ANCHOR_C_ORIGIN:
        placed = (tx - half_w, tx + half_w, ty - half_h, ty + half_h)
        if not _overlaps(placed, _footprint_box(pads)):
            tx, ty, anchor = 0.0, 0.0, ANCHOR_FOOTPRINT

    # EasyEDA lifts the unrotated model so its lowest point is at z, then
    # rotates it; only for Z-only rotations does the lift stay vertical.
    lift = mil_to_mm(model.z) - min(v[2] for v in mesh.vertices)
    z = _apply(_kicad_rotation(rotation), (0.0, 0.0, lift))[2]
    # KiCad's offset Y points up.
    return (tx - cx, -ty - cy, z), anchor


def _placed_vertices(mesh: _Mesh, rotation: Vec3, offset: Vec3) -> List[Vec3]:
    """Model vertices as KiCad places them, in board coordinates (Y down)."""
    m = _kicad_rotation(rotation)
    out = []
    for v in mesh.vertices:
        x, y, z = _apply(m, v)
        out.append((x + offset[0], -(y + offset[1]), z + offset[2]))
    return out


def _board_crossings(mesh: _Mesh, placed: Sequence[Vec3]) -> List[Point]:
    """Where model edges pass through the board surface (through-hole pins)."""
    points = []
    for a, b in mesh.edges:
        za, zb = placed[a][2], placed[b][2]
        if (za < 0) != (zb < 0):
            t = za / (za - zb)
            points.append(
                (
                    placed[a][0] + t * (placed[b][0] - placed[a][0]),
                    placed[a][1] + t * (placed[b][1] - placed[a][1]),
                )
            )
    return points


def _pad_half_extents(pad: EEPad) -> Point:
    """Half-size of the pad's axis-aligned bounding box (mm)."""
    if pad.polygon_points:
        x0, x1, y0, y1 = _box_of(list(zip(pad.polygon_points[0::2], pad.polygon_points[1::2])))
        return max(abs(x0), abs(x1)), max(abs(y0), abs(y1))
    c, s = abs(math.cos(math.radians(pad.rotation))), abs(math.sin(math.radians(pad.rotation)))
    hw, hh = pad.width / 2, pad.height / 2
    return hw * c + hh * s, hw * s + hh * c


def _in_polygon(x: float, y: float, coords: Sequence[float]) -> bool:
    pts = list(zip(coords[0::2], coords[1::2]))
    inside = False
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _on_pad(pad: EEPad, x: float, y: float) -> bool:
    if len(pad.polygon_points) >= 6:
        return _in_polygon(x - pad.x, y - pad.y, pad.polygon_points)
    hx, hy = _pad_half_extents(pad)
    return abs(x - pad.x) <= hx and abs(y - pad.y) <= hy


def _clip_polygon(polygon: Sequence[Point], box: Box) -> List[Point]:
    """Part of a polygon inside an axis-aligned box (Sutherland-Hodgman)."""
    points = list(polygon)
    # (axis, limit, sign): keep points with sign * (p[axis] - limit) >= 0
    for axis, limit, sign in ((0, box[0], 1.0), (0, box[1], -1.0), (1, box[2], 1.0), (1, box[3], -1.0)):
        clipped: List[Point] = []
        for i, cur in enumerate(points):
            prev = points[i - 1]
            cur_in = sign * (cur[axis] - limit) >= 0
            if cur_in != (sign * (prev[axis] - limit) >= 0):
                t = (limit - prev[axis]) / (cur[axis] - prev[axis])
                clipped.append((prev[0] + t * (cur[0] - prev[0]), prev[1] + t * (cur[1] - prev[1])))
            if cur_in:
                clipped.append(cur)
        points = clipped
    return points


def _resting_faces(mesh: _Mesh, placed: Sequence[Vec3]) -> List[List[Point]]:
    """Model faces lying on the board surface (SMD terminal bottoms)."""
    return [
        [(placed[i][0], placed[i][1]) for i in face]
        for face in mesh.faces
        if all(abs(placed[i][2]) <= _BOARD_CONTACT_MM for i in face)
    ]


def _pin_pairs(mesh: _Mesh, rotation: Vec3, offset: Vec3, pads: Sequence[EEPad]) -> List[Tuple[Point, Point]]:
    """Pair each pad with the centre of the model geometry that meets it.

    Through-hole pads pair with where the model passes through the board.
    Top SMD pads pair with the geometry resting on the board, clipped to the
    pad (a terminal can be larger than its pad, or joined to the body).
    Bottom-side SMD pads cannot touch a top-side model and are skipped.
    """
    placed = _placed_vertices(mesh, rotation, offset)
    crossings = _board_crossings(mesh, placed)
    resting = _resting_faces(mesh, placed)
    pairs = []
    for pad in pads:
        if pad.drill > 0:
            on_pad = [p for p in crossings if _on_pad(pad, p[0], p[1])]
        elif pad.layer == "2":
            continue
        else:
            hx, hy = _pad_half_extents(pad)
            pad_box = (pad.x - hx, pad.x + hx, pad.y - hy, pad.y + hy)
            on_pad = []
            for face in resting:
                on_pad.extend(_clip_polygon(face, pad_box))
        if on_pad:
            x0, x1, y0, y1 = _box_of(on_pad)
            pairs.append(((pad.x, pad.y), ((x0 + x1) / 2, (y0 + y1) / 2)))
    return pairs


def _fit_to_pads(offset: Vec3, rotation: Vec3, mesh: _Mesh, pads: Sequence[EEPad]) -> Tuple[Vec3, str, Optional[float]]:
    """Shift the model so its pins centre on their pads, when that fits (step 3)."""
    pairs = _pin_pairs(mesh, rotation, offset, pads)
    if len(pairs) < 2:
        return offset, PINS_UNMATCHED, None
    dx = sum(pad[0] - pin[0] for pad, pin in pairs) / len(pairs)
    dy = sum(pad[1] - pin[1] for pad, pin in pairs) / len(pairs)
    rms = math.sqrt(sum((pad[0] - pin[0] - dx) ** 2 + (pad[1] - pin[1] - dy) ** 2 for pad, pin in pairs) / len(pairs))
    if rms > PIN_FIT_TOLERANCE_MM:
        return offset, PINS_OFFSET, rms
    # Board Y points down; KiCad's model offset Y points up.
    return (offset[0] + dx, offset[1] - dy, offset[2]), PINS_ALIGNED, rms


def place_model(
    model: EE3DModel,
    fp_origin_x: float,
    fp_origin_y: float,
    obj_source: Optional[str] = None,
    pads: Optional[Sequence[EEPad]] = None,
) -> ModelPlacement:
    """Compute the KiCad 3D model offset (mm) and rotation (degrees).

    Args:
        model: EasyEDA 3D model data (origin, outline, z, rotation)
        fp_origin_x: Footprint origin X (EasyEDA units)
        fp_origin_y: Footprint origin Y (EasyEDA units)
        obj_source: OBJ model text; without it the offset is zero
        pads: Footprint pads (mm, relative to the footprint origin), used to
            check the placement and centre the model's pins on their pads
    """
    rotation = _kicad_file_rotation(model.rotation)
    mesh = _parse_obj_mesh(obj_source) if obj_source is not None else _Mesh([], [], [])
    if not mesh.vertices:
        return ModelPlacement((0.0, 0.0, 0.0), rotation, ANCHOR_NONE)

    offset, anchor = _anchor(model, fp_origin_x, fp_origin_y, mesh, rotation, pads)
    if not pads:
        return ModelPlacement(offset, rotation, anchor)
    offset, pins, rms = _fit_to_pads(offset, rotation, mesh, pads)
    return ModelPlacement(offset, rotation, anchor, pins, rms)


def compute_model_transform(
    model: EE3DModel,
    fp_origin_x: float,
    fp_origin_y: float,
    obj_source: Optional[str] = None,
    pads: Optional[Sequence[EEPad]] = None,
) -> Tuple[Vec3, Vec3]:
    """Return ``(offset, rotation)`` from :func:`place_model`."""
    placement = place_model(model, fp_origin_x, fp_origin_y, obj_source, pads)
    return placement.offset, placement.rotation


def save_models(
    output_dir: str,
    name: str,
    step_data: Optional[bytes] = None,
    wrl_source: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Save STEP and WRL model files to *output_dir*.

    *step_data* — raw STEP bytes (``None`` to skip saving).
    *wrl_source* — OBJ-like text to convert to VRML (``None`` to skip).

    Existing files on disk are returned even when new data is not provided.
    Returns *(step_path, wrl_path)* — either may be ``None``.
    """
    os.makedirs(output_dir, exist_ok=True)

    step_path = os.path.join(output_dir, f"{name}.step")
    wrl_path = os.path.join(output_dir, f"{name}.wrl")

    output_dir_abs = os.path.abspath(output_dir)
    if not os.path.abspath(step_path).startswith(output_dir_abs):
        raise ValueError(f"Invalid model name: {name}")
    if not os.path.abspath(wrl_path).startswith(output_dir_abs):
        raise ValueError(f"Invalid model name: {name}")

    # Save STEP
    if step_data is not None:
        atomic_write(step_path, step_data)
        step_out = step_path
    elif os.path.exists(step_path):
        step_out = step_path
    else:
        step_out = None

    # Convert and save WRL
    if wrl_source is not None:
        wrl_content = convert_to_vrml(wrl_source)
        if wrl_content:
            atomic_write(wrl_path, wrl_content)
            wrl_out = wrl_path
        elif os.path.exists(wrl_path):
            wrl_out = wrl_path
        else:
            wrl_out = None
    elif os.path.exists(wrl_path):
        wrl_out = wrl_path
    else:
        wrl_out = None

    return step_out, wrl_out


def convert_to_vrml(obj_source: str) -> Optional[str]:
    """Convert EasyEDA OBJ-like 3D text format to VRML 2.0."""
    materials = {}
    vertices = []
    shape_groups = []

    lines = obj_source.split("\n")

    # Parse materials
    current_mtl = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("newmtl "):
            current_mtl = {
                "name": line[7:].strip(),
                "Ka": (0.2, 0.2, 0.2),
                "Kd": (0.8, 0.8, 0.8),
                "Ks": (0, 0, 0),
                "d": 0,
            }
        elif line.startswith("Ka ") and current_mtl:
            parts = line.split()
            if len(parts) >= 4:
                current_mtl["Ka"] = (float(parts[1]), float(parts[2]), float(parts[3]))
        elif line.startswith("Kd ") and current_mtl:
            parts = line.split()
            if len(parts) >= 4:
                current_mtl["Kd"] = (float(parts[1]), float(parts[2]), float(parts[3]))
        elif line.startswith("Ks ") and current_mtl:
            parts = line.split()
            if len(parts) >= 4:
                current_mtl["Ks"] = (float(parts[1]), float(parts[2]), float(parts[3]))
        elif line.startswith("d ") and current_mtl:
            parts = line.split()
            if len(parts) >= 2:
                current_mtl["d"] = float(parts[1])
        elif line == "endmtl" and current_mtl:
            materials[current_mtl["name"]] = current_mtl
            current_mtl = None
        elif line.startswith("v ") and not line.startswith("vn") and not line.startswith("vt"):
            parts = line.split()
            if len(parts) >= 4:
                # OBJ is in mm; KiCad's VRML unit is 0.1 inch (2.54 mm)
                x = float(parts[1]) / 2.54
                y = float(parts[2]) / 2.54
                z = float(parts[3]) / 2.54
                vertices.append((x, y, z))
        elif line.startswith("usemtl "):
            mtl_name = line[7:].strip()
            shape_groups.append({"material": mtl_name, "faces": []})
        elif line.startswith("f ") and shape_groups:
            # Parse face: f v1//n1 v2//n2 v3//n3
            parts = line.split()[1:]
            face_indices = []
            for p in parts:
                idx_str = p.split("//")[0].split("/")[0]
                try:
                    face_indices.append(int(idx_str) - 1)  # 1-based to 0-based
                except ValueError:
                    continue
            if len(face_indices) >= 3:
                shape_groups[-1]["faces"].append(face_indices)
        i += 1

    if not vertices or not shape_groups:
        return None

    # Generate VRML 2.0
    vrml_lines = ["#VRML V2.0 utf8", ""]

    for group in shape_groups:
        if not group["faces"]:
            continue

        mtl = materials.get(group["material"], {"Kd": (0.8, 0.8, 0.8), "Ks": (0, 0, 0), "d": 0})

        # Collect unique vertex indices used by this group
        used_indices = set()
        for face in group["faces"]:
            used_indices.update(face)

        # Build local index mapping
        sorted_indices = sorted(used_indices)
        global_to_local = {g: local_idx for local_idx, g in enumerate(sorted_indices)}

        # Build point array
        points = []
        for gi in sorted_indices:
            if gi < len(vertices):
                v = vertices[gi]
                points.append(f"{v[0]:.6f} {v[1]:.6f} {v[2]:.6f}")

        # Build coordIndex
        coord_indices = []
        for face in group["faces"]:
            local_face = [str(global_to_local[gi]) for gi in face if gi in global_to_local]
            if len(local_face) >= 3:
                coord_indices.append(", ".join(local_face) + ", -1")

        if not points or not coord_indices:
            continue

        kd = mtl.get("Kd", (0.8, 0.8, 0.8))
        ks = mtl.get("Ks", (0, 0, 0))
        transparency = mtl.get("d", 0)

        vrml_lines.append("Shape {")
        vrml_lines.append("  appearance Appearance {")
        vrml_lines.append("    material Material {")
        vrml_lines.append(f"      diffuseColor {kd[0]:.4f} {kd[1]:.4f} {kd[2]:.4f}")
        vrml_lines.append(f"      specularColor {ks[0]:.4f} {ks[1]:.4f} {ks[2]:.4f}")
        vrml_lines.append("      ambientIntensity 0.2")
        vrml_lines.append(f"      transparency {transparency:.4f}")
        vrml_lines.append("      shininess 0.5")
        vrml_lines.append("    }")
        vrml_lines.append("  }")
        vrml_lines.append("  geometry IndexedFaceSet {")
        vrml_lines.append("    ccw TRUE")
        vrml_lines.append("    solid FALSE")
        vrml_lines.append("    coord DEF co Coordinate {")
        vrml_lines.append("      point [")
        for pt in points:
            vrml_lines.append(f"        {pt},")
        vrml_lines.append("      ]")
        vrml_lines.append("    }")
        vrml_lines.append("    coordIndex [")
        for ci in coord_indices:
            vrml_lines.append(f"      {ci},")
        vrml_lines.append("    ]")
        vrml_lines.append("  }")
        vrml_lines.append("}")
        vrml_lines.append("")

    return "\n".join(vrml_lines)
