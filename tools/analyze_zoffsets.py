#!/usr/bin/env python3
"""Analyze z-offset calculations for test parts."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_jlcimport.easyeda.parser import parse_footprint_shapes
from kicad_jlcimport.kicad.model3d import _obj_bounding_box, compute_model_transform


def analyze_part(lcsc_id: str, testdata_dir: Path):
    """Analyze z-offset calculation for a part."""
    print(f"\n=== {lcsc_id} ===")

    # Load footprint data
    fp_path = testdata_dir / f"{lcsc_id}_footprint.json"
    if not fp_path.exists():
        print("  No footprint data found")
        return

    with open(fp_path) as f:
        fp_data = json.load(f)

    fp_head = fp_data["dataStr"]["head"]
    fp_origin_x = fp_head["x"]
    fp_origin_y = fp_head["y"]

    fp_shapes = fp_data["dataStr"]["shape"]
    footprint = parse_footprint_shapes(fp_shapes, fp_origin_x, fp_origin_y)

    if not footprint.model:
        print("  No 3D model in footprint")
        return

    # Load OBJ data if available
    obj_path = testdata_dir / f"{lcsc_id}_model.obj"
    obj_source = None
    if obj_path.exists():
        with open(obj_path) as f:
            obj_source = f.read()

        # Analyze OBJ bounding box
        cx, cy, z_min, z_max = _obj_bounding_box(obj_source)
        print(f"  OBJ bbox: cx={cx:.3f}, cy={cy:.3f}, z_min={z_min:.3f}, z_max={z_max:.3f}")
        print(f"  z_max < abs(z_min): {z_max < abs(z_min)} ({z_max:.3f} < {abs(z_min):.3f})")
    else:
        print("  No OBJ file available")

    # Compute current offset
    offset, rotation = compute_model_transform(footprint.model, fp_origin_x, fp_origin_y, obj_source)

    print(f"  Model origin: x={footprint.model.origin_x}, y={footprint.model.origin_y}, z={footprint.model.z}")
    print(f"  FP origin: x={fp_origin_x}, y={fp_origin_y}")
    print(f"  Computed offset: x={offset[0]:.3f}, y={offset[1]:.3f}, z={offset[2]:.3f}")
    print(f"  Rotation: {rotation}")


if __name__ == "__main__":
    testdata_dir = Path(__file__).resolve().parent.parent / "testdata"

    # Existing test parts
    existing = ["C160404", "C668119", "C385834", "C395958"]

    # New test parts
    new_parts = ["C5213", "C3794", "C5206", "C8852", "C18901", "C10081", "C138392"]

    print("EXISTING TEST PARTS:")
    print("=" * 60)
    for part_id in existing:
        analyze_part(part_id, testdata_dir)

    print("\n\nNEW TEST PARTS:")
    print("=" * 60)
    for part_id in new_parts:
        analyze_part(part_id, testdata_dir)
