"""3D-model regression fixtures (testdata/models) and their evaluation.

Shared by tests/test_model_regression.py and tools/update_model_snapshot.py.
"""

import glob
import gzip
import json
import os
from typing import Dict, List

from kicad_jlcimport.easyeda.parser import parse_footprint_shapes
from kicad_jlcimport.kicad.model3d import place_model
from kicad_jlcimport.kicad.model_checks import check_model

MODELS_DIR = os.path.join(os.path.dirname(__file__), "..", "testdata", "models")
SNAPSHOT_PATH = os.path.join(MODELS_DIR, "snapshot.json")


def fixture_ids() -> List[str]:
    return sorted(os.path.basename(p).split(".")[0] for p in glob.glob(os.path.join(MODELS_DIR, "*.json.gz")))


def evaluate(lcsc_id: str) -> Dict:
    """Place and check one fixture part; the record stored in the snapshot."""
    with gzip.open(os.path.join(MODELS_DIR, f"{lcsc_id}.json.gz"), "rt", encoding="utf-8") as f:
        fixture = json.load(f)
    ox, oy = fixture["head"]["x"], fixture["head"]["y"]
    footprint = parse_footprint_shapes(fixture["shape"], ox, oy)
    placement = place_model(footprint.model, ox, oy, fixture["obj"], footprint.pads)
    check = check_model(footprint, placement, fixture["obj"])
    return {
        "offset": [round(v, 3) + 0.0 for v in placement.offset],
        "rotation": [round(v, 3) + 0.0 for v in placement.rotation],
        "anchor": placement.anchor,
        "pins": placement.pins,
        "issues": check.issues,
        "warnings": check.warnings,
    }


def load_snapshot() -> Dict[str, Dict]:
    with open(SNAPSHOT_PATH) as f:
        return json.load(f)


def write_snapshot(records: Dict[str, Dict]) -> None:
    # One part per line, so a change reads as a per-part diff.
    lines = [f"  {json.dumps(k)}: {json.dumps(records[k], sort_keys=True)}" for k in sorted(records)]
    with open(SNAPSHOT_PATH, "w") as f:
        f.write("{\n" + ",\n".join(lines) + "\n}\n")
