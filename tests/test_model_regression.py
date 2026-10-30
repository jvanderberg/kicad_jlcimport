"""3D model regression suite over the visual-comparison parts.

Every part in tools/regression_parts.txt that has a 3D model is stored in
testdata/models (see tools/fetch_model_fixtures.py) and is checked two ways:

* Physically: the placed model must pass :func:`check_model` (it sits on the
  board, nothing but pins goes through it, it covers its pads, its pins reach
  the pads).  Parts whose EasyEDA data is itself inconsistent are listed in
  KNOWN_BAD with the issues they are expected to have.
* Against testdata/models/snapshot.json: any change to a part's offset,
  rotation or check result fails until the snapshot is regenerated with
  tools/update_model_snapshot.py and the diff reviewed.
"""

import pytest

from .model_fixtures import evaluate, fixture_ids, load_snapshot

# EasyEDA model and footprint disagree; the checks must keep reporting it.
KNOWN_BAD = {
    # Leads 9.8 mm apart on a diagonal; pads 7.5 mm apart in a line.
    "C8760": ["below_board", "pins_unmatched"],
}

TOLERANCE = 0.001  # mm / degrees

FIXTURES = fixture_ids()


def test_fixtures_present():
    assert len(FIXTURES) >= 100, "run tools/fetch_model_fixtures.py"


@pytest.mark.parametrize("lcsc_id", FIXTURES)
def test_placement_is_physically_sound(lcsc_id):
    record = evaluate(lcsc_id)
    assert record["issues"] == KNOWN_BAD.get(lcsc_id, []), record


def test_snapshot_covers_all_fixtures():
    assert sorted(load_snapshot()) == FIXTURES, "run tools/update_model_snapshot.py"


def _differs(key, old, new):
    if key == "offset":
        return old is None or any(abs(a - b) > TOLERANCE for a, b in zip(old, new))
    if key == "rotation":  # angles compare modulo 360
        return old is None or any(abs((a - b + 180) % 360 - 180) > TOLERANCE for a, b in zip(old, new))
    return old != new


def test_placements_match_snapshot():
    snapshot = load_snapshot()
    changes = []
    for lcsc_id in FIXTURES:
        new = evaluate(lcsc_id)
        old = snapshot.get(lcsc_id, {})
        for key in new:
            if _differs(key, old.get(key), new[key]):
                changes.append(f"{lcsc_id}.{key}: {old.get(key)} -> {new[key]}")
    assert not changes, (
        "3D model placements changed. If intended, run tools/update_model_snapshot.py and review the diff:\n"
        + "\n".join(changes)
    )
