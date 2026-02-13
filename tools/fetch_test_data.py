#!/usr/bin/env python3
"""Fetch test data for JLCPCB parts."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_jlcimport.easyeda.api import download_wrl_source, fetch_full_component


def fetch_test_data(part_id: str, output_dir: Path):
    """Fetch and save symbol, footprint, and 3D model data."""
    print(f"Fetching {part_id}...")
    comp = fetch_full_component(part_id)

    # Save symbol data
    sym_list = comp.get("symbol_data_list", [])
    if sym_list:
        sym_data = sym_list[0]
        sym_path = output_dir / f"{part_id}_symbol.json"
        with open(sym_path, "w") as f:
            json.dump(sym_data, f, indent=2)
        print(f"  Saved symbol to {sym_path}")

    # Save footprint data
    fp_data = comp.get("footprint_data", {})
    if fp_data:
        fp_path = output_dir / f"{part_id}_footprint.json"
        with open(fp_path, "w") as f:
            json.dump(fp_data, f, indent=2)
        print(f"  Saved footprint to {fp_path}")

    # Save 3D model OBJ data
    # Try uuid_3d from fetch_full_component first
    uuid_3d = comp.get("uuid_3d", "")

    # If not found, look in footprint SVGNODE shapes
    if not uuid_3d and fp_data:
        shapes = fp_data.get("dataStr", {}).get("shape", [])
        for shape in shapes:
            if isinstance(shape, str) and shape.startswith("SVGNODE~"):
                # Parse the JSON part after SVGNODE~
                try:
                    import json as json_mod

                    json_str = shape[8:]  # Remove "SVGNODE~" prefix
                    svgnode_data = json_mod.loads(json_str)
                    uuid_3d = svgnode_data.get("attrs", {}).get("uuid", "")
                    if uuid_3d:
                        break
                except (json.JSONDecodeError, KeyError, AttributeError):
                    pass

    if uuid_3d:
        obj_content = download_wrl_source(uuid_3d)
        if obj_content:
            obj_path = output_dir / f"{part_id}_model.obj"
            with open(obj_path, "w") as f:
                f.write(obj_content)
            print(f"  Saved 3D model to {obj_path}")
        else:
            print(f"  Warning: Could not download 3D model for UUID {uuid_3d}")
    else:
        print("  No 3D model UUID found")

    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <part_id> [part_id...]")
        print(f"Example: {sys.argv[0]} C5213 C3794")
        sys.exit(1)

    testdata_dir = Path(__file__).resolve().parent.parent / "testdata"
    testdata_dir.mkdir(exist_ok=True)

    for part_id in sys.argv[1:]:
        try:
            fetch_test_data(part_id, testdata_dir)
        except Exception as e:
            print(f"  Error fetching {part_id}: {e}")
