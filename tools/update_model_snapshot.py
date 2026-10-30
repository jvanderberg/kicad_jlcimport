#!/usr/bin/env python3
"""Regenerate testdata/models/snapshot.json from the current placement code.

Run this after an intentional change to 3D model placement, then review the
diff (``git diff testdata/models/snapshot.json``) part by part before
committing it.  Prints the parts whose record changed.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from model_fixtures import SNAPSHOT_PATH, evaluate, fixture_ids, load_snapshot, write_snapshot  # noqa: E402


def main() -> None:
    old = load_snapshot() if Path(SNAPSHOT_PATH).exists() else {}
    new = {lcsc_id: evaluate(lcsc_id) for lcsc_id in fixture_ids()}
    for lcsc_id in sorted(set(old) | set(new)):
        if old.get(lcsc_id) != new.get(lcsc_id):
            print(f"{lcsc_id}:\n  was {json.dumps(old.get(lcsc_id))}\n  now {json.dumps(new.get(lcsc_id))}")
    write_snapshot(new)
    print(f"Wrote {len(new)} parts to {SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()
