"""3D model transforms, WRL conversion, and file saving.

Why WRL instead of STEP for KiCad model references:
----------------------------------------------------
EasyEDA provides 3D models in OBJ format, which we convert to both WRL (VRML) and STEP.
Our offset calculations are based on analyzing the OBJ/WRL geometry (bounding box, center point).
The WRL files maintain the exact same geometry and coordinate system as the OBJ source.

STEP files can have different origins/orientations after conversion, leading to mismatches
between calculated offsets and actual model placement. Using WRL ensures consistency
between offset calculations and the model geometry KiCad will render.

Both files are still saved for compatibility, but KiCad footprints reference the WRL.
"""

import math
import os
from typing import Optional, Tuple

from ..easyeda.ee_types import EE3DModel
from ..easyeda.parser import mil_to_mm

# ============================================================================
# Unit Conversion
# ============================================================================
#
# 3D model origin and SVGNODE z use the same 10-mil units as the rest of
# EasyEDA's coordinate system, so they share the canonical mil_to_mm
# conversion (1 unit = 10 mils = 0.254 mm exactly).
# Example: z = -13.7795 EasyEDA units → -13.7795 × 0.254 mm = -3.5 mm.

# ============================================================================
# Spurious Offset Detection Thresholds
# ============================================================================

# Model origin offsets smaller than this are likely noise/measurement errors
_SPURIOUS_OFFSET_MIN_MM = 0.5

# For parts shorter than this, apply relative offset threshold check
_SHORT_PART_HEIGHT_MM = 3.0

# For short parts, offsets greater than this fraction of height are unreasonable
# (e.g., 0.965mm offset on 1.649mm part = 58.5% is clearly wrong)
_SPURIOUS_OFFSET_HEIGHT_RATIO = 0.4

# Model origin offsets larger than this are EasyEDA data errors
_SPURIOUS_OFFSET_MAX_MM = 50.0

# ============================================================================
# Geometry Offset Thresholds
# ============================================================================

# If OBJ center (cy) is more than this fraction of part height, it's significant
# (Empirically derived: C160404 @ 12%, C395958 @ 23.8% are significant,
#  C2562 @ 2.1%, C385834 @ 2.1% are not)
_SIGNIFICANT_CY_HEIGHT_RATIO = 0.05

# ============================================================================
# Rotation Transformation Thresholds
# ============================================================================

# Tolerance for detecting ±180° Z-rotation (degrees)
_ROTATION_180_TOLERANCE_DEG = 0.1


# ============================================================================
# Helper Functions
# ============================================================================


def _is_spurious_offset(model_origin_diff_y: float, height: float) -> bool:
    """Check if model origin offset is spurious (EasyEDA data error).

    Three types of spurious offsets:
    1. Very small offsets (< 0.5mm) - noise/measurement errors
    2. Physically unreasonable offsets for short parts (offset > 40% of height)
    3. Absurdly large offsets (> 50mm) - obvious data errors

    Args:
        model_origin_diff_y: Y offset between model origin and footprint origin (mm)
        height: Part height from OBJ bounding box (z_max - z_min, mm)

    Returns:
        True if offset should be ignored as spurious
    """
    abs_offset = abs(model_origin_diff_y)

    if abs_offset < _SPURIOUS_OFFSET_MIN_MM:
        return True

    if height > 0 and height < _SHORT_PART_HEIGHT_MM:
        if abs_offset > _SPURIOUS_OFFSET_HEIGHT_RATIO * height:
            return True

    if abs_offset > _SPURIOUS_OFFSET_MAX_MM:
        return True

    return False


def _apply_rotation_transform(offset: Tuple[float, float, float], rotation_z: float) -> Tuple[float, float, float]:
    """Apply Z-axis rotation transformation to XY offset.

    In KiCad, model offsets are applied in the footprint coordinate system (after model rotation).
    However, geometry offsets (like -cx, -cy from the OBJ bounding box) are measured in the
    model's local coordinate system and need to be transformed to footprint space.

    For Z-axis rotations of ±90° and ±180°, we transform the XY components of the geometry offset.
    X and Y axis rotations are NOT transformed here - they affect the model's 3D orientation
    which KiCad handles separately.

    Uses standard 2D rotation matrix:
        x' = x*cos(θ) - y*sin(θ)
        y' = x*sin(θ) + y*cos(θ)

    Args:
        offset: (x, y, z) offset in mm
        rotation_z: Z-axis rotation in degrees

    Returns:
        Transformed (x, y, z) offset in mm
    """
    # Only transform for common Z-axis rotation angles: ±90° and ±180°
    abs_rot = abs(rotation_z)
    if not (abs(abs_rot - 90.0) < 0.1 or abs(abs_rot - 180.0) < 0.1):
        return offset

    rz_rad = math.radians(rotation_z)
    cos_rz = math.cos(rz_rad)
    sin_rz = math.sin(rz_rad)

    x_rot = offset[0] * cos_rz - offset[1] * sin_rz
    y_rot = offset[0] * sin_rz + offset[1] * cos_rz

    return (x_rot, y_rot, offset[2])


# ============================================================================
# Main Transform Function
# ============================================================================


def compute_model_transform(
    model: EE3DModel,
    fp_origin_x: float,
    fp_origin_y: float,
    obj_source: Optional[str] = None,
) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    """Compute 3D model offset and rotation from footprint model data.

    The offset has two independent components:

    1. **Geometry offset** (-cx, -cy_eff, z) — compensates for the 3D model's
       bounding-box not being centered at the origin. This is in *model* space,
       so it must be rotated together with the model.

    2. **Origin offset** (-diff_x, -diff_y, 0) — compensates for the EasyEDA model
       origin differing from the footprint origin. This is in *footprint* space,
       so it must NOT be rotated. Both X and Y components are considered to handle
       rotated models where the offset may be in any direction.

    Separating these two concerns eliminates special-case sign handling for
    rotated parts: the rotation transform is applied only to the geometry
    offset, then the origin offset is added afterwards.

    Args:
        model: EasyEDA 3D model data (contains origin, z-offset, rotation)
        fp_origin_x: Footprint origin X coordinate (in EasyEDA mils)
        fp_origin_y: Footprint origin Y coordinate (in EasyEDA mils)
        obj_source: Optional OBJ file content for geometry analysis

    Returns:
        Tuple of (offset, rotation) where:
        - offset: (x, y, z) translation in mm
        - rotation: (rx, ry, rz) rotation in degrees
    """
    if obj_source is None:
        return (0.0, 0.0, 0.0), model.rotation

    # Parse OBJ geometry
    cx, cy, z_min, z_max = _obj_bounding_box(obj_source)
    height = z_max - z_min

    # --- Effective cy: only use if significant relative to part height ---
    cy_eff = cy if (height > 0 and abs(cy) / height > _SIGNIFICANT_CY_HEIGHT_RATIO) else 0.0

    # --- Effective origin diff: zero out spurious offsets (check both X and Y) ---
    model_origin_diff_x = mil_to_mm(model.origin_x - fp_origin_x)
    model_origin_diff_y = mil_to_mm(model.origin_y - fp_origin_y)
    # Use magnitude of offset vector to determine if spurious
    origin_diff_magnitude = (model_origin_diff_x**2 + model_origin_diff_y**2) ** 0.5
    is_spurious = _is_spurious_offset(origin_diff_magnitude, height)
    diff_eff_x = 0.0 if is_spurious else model_origin_diff_x
    diff_eff_y = 0.0 if is_spurious else model_origin_diff_y

    # --- Z offset (universal formula) ---
    z_offset = -z_min + mil_to_mm(model.z)

    # --- Geometry offset: rotates with the model ---
    # Only apply Z-axis rotation - X/Y rotations are handled by KiCad's model orientation
    geometry = _apply_rotation_transform((-cx, -cy_eff, z_offset), model.rotation[2])

    # --- Origin offset: stays in footprint space (not rotated) ---
    offset = (geometry[0] - diff_eff_x, geometry[1] - diff_eff_y, geometry[2])

    return offset, model.rotation


def _obj_bounding_box(obj_source: str) -> Tuple[float, float, float, float]:
    """Return XY center and Z range of OBJ vertex data (cx, cy, z_min, z_max in mm)."""
    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")
    found = False

    for line in obj_source.split("\n"):
        line = line.strip()
        if not line.startswith("v ") or line.startswith("vn") or line.startswith("vt"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
        except ValueError:
            continue
        min_x, max_x = min(min_x, x), max(max_x, x)
        min_y, max_y = min(min_y, y), max(max_y, y)
        min_z, max_z = min(min_z, z), max(max_z, z)
        found = True

    if not found:
        return 0.0, 0.0, 0.0, 0.0

    cx = (min_x + max_x) / 2
    cy = (min_y + max_y) / 2
    return cx, cy, min_z, max_z


def _obj_xy_center(obj_source: str) -> Tuple[float, float]:
    """Return the XY bounding-box centre of OBJ vertex data (in mm).

    Deprecated: Use _obj_bounding_box for new code.
    """
    cx, cy, _, _ = _obj_bounding_box(obj_source)
    return cx, cy


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
        with open(step_path, "wb") as f:
            f.write(step_data)
        step_out = step_path
    elif os.path.exists(step_path):
        step_out = step_path
    else:
        step_out = None

    # Convert and save WRL
    if wrl_source is not None:
        wrl_content = convert_to_vrml(wrl_source)
        if wrl_content:
            with open(wrl_path, "w", encoding="utf-8") as f:
                f.write(wrl_content)
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
                # Divide by 2.54 to convert from mils to VRML units
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
