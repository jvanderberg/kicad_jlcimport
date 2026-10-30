#!/usr/bin/env python3
"""Download the 3D-model regression fixtures used by tests/test_model_regression.py.

For each part in tools/regression_parts.txt this saves the EasyEDA footprint
(origin and shapes) and the OBJ model as one gzipped JSON file in
testdata/models/<LCSC>.json.gz.  Re-run it to refresh the fixtures, then run
tools/update_model_snapshot.py and review the snapshot diff.

usage: fetch_model_fixtures.py [LCSC ...]
"""

import gzip
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kicad_jlcimport.easyeda.api import download_wrl_source, fetch_full_component  # noqa: E402

PARTS_FILE = ROOT / "tools" / "regression_parts.txt"
OUT_DIR = ROOT / "testdata" / "models"


def regression_parts() -> list:
    return [line.strip() for line in PARTS_FILE.read_text().splitlines() if line.strip()]


def _model_uuid(shapes: list) -> str:
    for shape in shapes:
        if isinstance(shape, str) and shape.startswith("SVGNODE~"):
            try:
                return json.loads(shape.split("~", 1)[1]).get("attrs", {}).get("uuid", "")
            except json.JSONDecodeError:
                continue
    return ""


def fetch(lcsc_id: str) -> str:
    comp = fetch_full_component(lcsc_id)
    data = comp["footprint_data"]["dataStr"]
    uuid = _model_uuid(data["shape"]) or comp.get("uuid_3d", "")
    if not uuid:
        return f"{lcsc_id}: no 3D model"
    obj = download_wrl_source(uuid)
    if not obj:
        return f"{lcsc_id}: model download failed"
    fixture = {
        "lcsc_id": lcsc_id,
        "head": {"x": data["head"]["x"], "y": data["head"]["y"]},
        "shape": data["shape"],
        "obj": obj,
    }
    path = OUT_DIR / f"{lcsc_id}.json.gz"
    # mtime=0 keeps the file byte-identical when the data has not changed
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as f:
        f.write(json.dumps(fixture, sort_keys=True).encode("utf-8"))
    return f"{lcsc_id}: saved"


def main() -> None:
    parts = sys.argv[1:] or regression_parts()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(8) as pool:
        for line in pool.map(fetch, parts):
            print(line)


if __name__ == "__main__":
    main()
