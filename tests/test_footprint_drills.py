"""Slot direction, non-plated holes and drill-less multi-layer pads."""

import json
import os
import re

import pytest

from kicad_jlcimport.easyeda.ee_types import EEPad
from kicad_jlcimport.easyeda.parser import _parse_pad, _slot_angle, parse_footprint_shapes
from kicad_jlcimport.kicad.footprint_writer import write_footprint

from .test_footprint_writer import _make_footprint

TESTDATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testdata")


def _pad_string(shape, w, h, hole_r, rot=0, slot_len="", pts="", plated="Y", layer="11", num="1"):
    """An EasyEDA PAD shape string centred at (4000, 3000), in EasyEDA units."""
    fields = ["PAD", shape, 4000, 3000, w, h, layer, "", num, hole_r, "", rot, "gge" + num, slot_len, pts, plated]
    return "~".join(str(f) for f in fields + ["", "", "", ""])


def _pad_lines(shapes):
    text = write_footprint(parse_footprint_shapes(shapes, 4000, 3000), "T")
    return [line.strip() for line in text.splitlines() if line.strip().startswith("(pad")]


HORIZONTAL = "3998.4252 3000 4001.5748 3000"
VERTICAL = "4000 2998.4252 4000 3001.5748"


class TestParsePad:
    def test_slot_angle_from_end_points(self):
        assert _slot_angle(HORIZONTAL) == pytest.approx(0)
        assert _slot_angle(VERTICAL) == pytest.approx(90)
        assert _slot_angle("0 0 1 1") == pytest.approx(45)

    @pytest.mark.parametrize(
        "points", ["", "   ", "1 2 3", "1 2 3 4 5", "a b c d", "5 5 5 5", "nan nan nan nan", "0 0 inf 0"]
    )
    def test_slot_angle_missing_or_bad(self, points):
        assert _slot_angle(points) is None

    def test_pad_carries_slot_angle_and_plating(self):
        pad = _parse_pad(_pad_string("OVAL", 4.3307, 7.874, 1.1811, 0, 5.9055, HORIZONTAL, "N").split("~"))
        assert pad.slot_angle == pytest.approx(0)
        assert pad.plated is False

    @pytest.mark.parametrize("plated", ["Y", "y", "", "anything"])
    def test_only_n_means_non_plated(self, plated):
        assert _parse_pad(_pad_string("ELLIPSE", 9, 9, 2, plated=plated).split("~")).plated is True

    def test_short_pad_string_defaults(self):
        pad = _parse_pad(["PAD", "OVAL", "100", "200", "20", "10", "11", "", "2", "5", "", "0"])
        assert pad.slot_angle is None
        assert pad.plated is True


class TestSlotDirection:
    def test_round_pad_with_horizontal_slot(self):
        # The old code guessed vertical for any pad that isn't wider than tall.
        (line,) = _pad_lines([_pad_string("OVAL", 9.4488, 9.4488, 1.1811, 0, 5.9055, HORIZONTAL)])
        assert "(drill oval 1.5 0.6)" in line

    def test_slot_wins_over_pad_shape(self):
        (line,) = _pad_lines([_pad_string("OVAL", 4.3307, 7.874, 1.1811, 0, 5.9055, HORIZONTAL)])
        assert "(size 1.1 2)" in line
        assert "(drill oval 1.5 0.6)" in line

    @pytest.mark.parametrize(
        "rot, points, expected",
        [
            (90, VERTICAL, "(drill oval 1.5 0.6)"),
            (90, HORIZONTAL, "(drill oval 0.6 1.5)"),
            (180, HORIZONTAL, "(drill oval 1.5 0.6)"),
            (270, VERTICAL, "(drill oval 1.5 0.6)"),
            (-90, HORIZONTAL, "(drill oval 0.6 1.5)"),
        ],
    )
    def test_right_angle_rotations(self, rot, points, expected):
        (line,) = _pad_lines([_pad_string("OVAL", 7.874, 4.3307, 1.1811, rot, 5.9055, points)])
        assert expected in line

    def test_ambiguous_rotation_falls_back_to_pad_shape(self):
        # At 45 degrees the two possible rotation signs disagree, so use the pad's long axis.
        pts = "3998.8866 2998.8866 4001.1134 3001.1134"
        (tall,) = _pad_lines([_pad_string("OVAL", 4.3307, 7.874, 1.1811, 45, 5.9055, pts)])
        (wide,) = _pad_lines([_pad_string("OVAL", 7.874, 4.3307, 1.1811, 45, 5.9055, pts)])
        assert "(drill oval 0.6 1.5)" in tall
        assert "(drill oval 1.5 0.6)" in wide

    def test_custom_pad_slot_measured_on_board(self):
        # Custom pads are written without rotation, so a 90-degree polygon pad
        # with a vertical slot must get a vertical drill.
        pad = EEPad(
            "POLYGON",
            0,
            0,
            2.0,
            1.1,
            "11",
            "1",
            drill=0.6,
            rotation=90,
            polygon_points=[-0.55, -1.0, 0.55, -1.0, 0.55, 1.0, -0.55, 1.0],
            slot_length=1.5,
            slot_angle=90.0,
        )
        text = write_footprint(_make_footprint(pads=[pad]), "T")
        assert " custom " in text
        assert "(drill oval 0.6 1.5)" in text

    def test_missing_end_points_fall_back_to_pad_shape(self):
        pad = EEPad("OVAL", 0, 0, 2.0, 2.0, "11", "1", drill=0.6, slot_length=1.5)
        assert "(drill oval 0.6 1.5)" in write_footprint(_make_footprint(pads=[pad]), "T")


class TestPlatingAndMissingDrill:
    def test_non_plated_hole_is_np_thru_hole(self):
        (line,) = _pad_lines([_pad_string("ELLIPSE", 9.4488, 9.4488, 1.9685, plated="N")])
        assert " np_thru_hole " in line
        assert "(drill 1)" in line
        assert '(layers "*.Cu" "*.Mask")' in line

    def test_plated_hole_unchanged(self):
        (line,) = _pad_lines([_pad_string("ELLIPSE", 9.4488, 9.4488, 1.9685)])
        assert " thru_hole " in line and "np_thru_hole" not in line

    def test_multilayer_pad_without_hole_is_never_drill_less_thru_hole(self):
        text = write_footprint(parse_footprint_shapes([_pad_string("RECT", 6, 6, 0)], 4000, 3000), "T")
        (line,) = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("(pad")]
        assert " smd " in line
        assert "(drill" not in line
        assert '(layers "F.Cu" "B.Cu" "F.Mask" "B.Mask")' in line
        assert "(attr smd)" in text


class TestSavedPartsUnchanged:
    """Every saved part has plated, axis-aligned slots whose direction matches the pad shape."""

    @pytest.mark.parametrize("lcsc", ["C2765186", "C393939"])
    def test_slots_follow_easyeda_end_points(self, lcsc):
        path = os.path.join(TESTDATA_DIR, f"{lcsc}_footprint.json")
        if not os.path.exists(path):
            pytest.skip(f"{lcsc} not in testdata")
        with open(path) as f:
            data = json.load(f)
        ds = data["result"]["dataStr"] if "result" in data else data["dataStr"]
        fp = parse_footprint_shapes(ds["shape"], ds["head"]["x"], ds["head"]["y"])
        slots = [p for p in fp.pads if p.slot_length > 0]
        assert slots
        text = write_footprint(fp, lcsc)
        for pad in slots:
            assert pad.plated
            assert pad.slot_angle == pytest.approx(90, abs=0.01) or pad.slot_angle == pytest.approx(-90, abs=0.01)
        # Vertical slots in unrotated pads: narrow side along x.
        assert re.findall(r"\(drill oval ([\d.]+) ([\d.]+)\)", text)
        for w, h in re.findall(r"\(drill oval ([\d.]+) ([\d.]+)\)", text):
            assert float(w) < float(h)
