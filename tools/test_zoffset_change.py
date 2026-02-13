#!/usr/bin/env python3
"""Test what happens if we apply z-offset heuristic to all parts with OBJ data."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_jlcimport.easyeda.parser import parse_footprint_shapes
from kicad_jlcimport.kicad.model3d import _obj_bounding_box


def analyze_with_new_logic(lcsc_id: str, testdata_dir: Path):
    """Show what z-offset would be with new logic."""
    fp_path = testdata_dir / f"{lcsc_id}_footprint.json"
    if not fp_path.exists():
        return None

    with open(fp_path) as f:
        fp_data = json.load(f)

    fp_head = fp_data["dataStr"]["head"]
    fp_origin_x = fp_head["x"]
    fp_origin_y = fp_head["y"]
    fp_shapes = fp_data["dataStr"]["shape"]
    footprint = parse_footprint_shapes(fp_shapes, fp_origin_x, fp_origin_y)

    if not footprint.model:
        return None

    obj_path = testdata_dir / f"{lcsc_id}_model.obj"
    if not obj_path.exists():
        return {"has_obj": False, "z_offset": 0.0}

    with open(obj_path) as f:
        obj_source = f.read()

    cx, cy, z_min, z_max = _obj_bounding_box(obj_source)

    # NEW LOGIC: apply heuristic to all parts with OBJ data
    if z_max < abs(z_min):
        z_offset = z_max  # extends below
    else:
        z_offset = -z_min / 2  # extends above

    return {
        "has_obj": True,
        "z_min": z_min,
        "z_max": z_max,
        "extends_below": z_max < abs(z_min),
        "z_offset_new": z_offset,
        "z_offset_old": footprint.model.z / 100.0,
    }


if __name__ == "__main__":
    testdata_dir = Path(__file__).resolve().parent.parent / "testdata"

    # Test parts with known expected values
    test_cases = [
        ("C160404", 0.0),
        ("C668119", -0.134),
        ("C385834", 6.35),
        ("C395958", 4.2),
        ("C5206", 2.0),
    ]

    print("Part ID  | Expected | Old Logic | New Logic | Match?")
    print("-" * 60)

    for part_id, expected_z in test_cases:
        result = analyze_with_new_logic(part_id, testdata_dir)
        if result and result["has_obj"]:
            old_z = result["z_offset_old"]
            new_z = result["z_offset_new"]
            # Check if new matches expected (within tolerance)
            matches = abs(new_z - expected_z) < 0.2
            print(f"{part_id:8} | {expected_z:8.3f} | {old_z:9.3f} | {new_z:9.3f} | {'✓' if matches else '✗'}")
        else:
            print(f"{part_id:8} | {expected_z:8.3f} | No OBJ data")
