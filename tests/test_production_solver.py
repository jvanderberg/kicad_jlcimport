"""Tests for the CPL placement solver and coordinate conventions."""

import json
import math
from pathlib import Path

import pytest

from kicad_jlcimport.kicad.footprint_writer import write_footprint
from kicad_jlcimport.production import conventions as cv
from kicad_jlcimport.production.cache import KeyedCache
from kicad_jlcimport.production.jlc_footprints import JlcFootprint, jlc_footprint_from_data
from kicad_jlcimport.production.kicad_pcb import parse_sexpr, read_board_text
from kicad_jlcimport.production.models import FootprintRecord, PadRecord
from kicad_jlcimport.production.placement import ESTIMATED, KICAD, SOLVED, compute_placement, is_isolated_array
from kicad_jlcimport.production.report import INFO, WARNING, run_check
from kicad_jlcimport.production.solver import (
    DEGENERATE,
    GEOMETRY_DIFFERS,
    GROUP_SIZE_MISMATCH,
    HIGH,
    HIGH_RESIDUAL,
    LOW,
    MEDIUM,
    MIRRORED,
    MIRRORED_ARRAY,
    MIRRORED_BY_POSITION,
    NOT_SNAPPED,
    PAD_MISMATCH,
    POLARITY_BY_NUMBERING,
    POSITION_CENTRED,
    POSITION_ESTIMATE,
    POSITION_LOOSE,
    TOO_FEW_PADS,
    describe_flags,
    group_pads,
    match_pads_by_position,
    solve_placement,
)

TESTDATA = Path(__file__).parent.parent / "testdata"


def load_jlc(part: str) -> JlcFootprint:
    data = json.loads((TESTDATA / f"{part}_footprint.json").read_text())
    return jlc_footprint_from_data(part, data.get("result", data))


def angle_close(a, b, tol=1e-6):
    d = math.fmod(a - b, 360.0)
    return min(abs(d), abs(abs(d) - 360.0)) < tol


# ---------------------------------------------------------------------------
# Board simulation helpers — mimic how KiCad stores and places footprints.
# ---------------------------------------------------------------------------


def _caches(jlc_fp):
    part = {"stock": 1000, "type": "Basic", "price": 0.01, "price_breaks": [(1, 0.01)]}
    return KeyedCache(lambda _l: part), KeyedCache(lambda _l: jlc_fp)


def kicad_place(local_pads, x, y, theta, side):
    """Board pad positions for a KiCad footprint (local Y-down) placed at (x, y, theta).

    Bottom side: KiCad mirrors local Y when flipping (see conventions.py).
    """
    rad = math.radians(theta)
    c, s = math.cos(rad), math.sin(rad)
    out = []
    for p in local_pads:
        px, py = p.x, (-p.y if side == cv.BOTTOM else p.y)
        out.append(PadRecord(p.number, x + px * c + py * s, y - px * s + py * c))
    return out


def written_footprint_pads(jlc: JlcFootprint, part: str):
    """Generate a .kicad_mod with our writer and read its pads back (local frame)."""
    data = json.loads((TESTDATA / f"{part}_footprint.json").read_text())
    data = data.get("result", data)
    from kicad_jlcimport.easyeda.parser import parse_footprint_shapes

    head = data["dataStr"]["head"]
    ee_fp = parse_footprint_shapes(data["dataStr"]["shape"], head["x"], head["y"])
    text = write_footprint(ee_fp, part)
    node = parse_sexpr(text)
    pads = []
    for child in node[1:]:
        if isinstance(child, list) and child and child[0] == "pad":
            at = next(c for c in child if isinstance(c, list) and c[0] == "at")
            pads.append(PadRecord(child[1], float(at[1]), float(at[2])))
    return text, pads


def board_text_with(fp_text: str, x: float, y: float, theta: float, side: str, ref="U1", lcsc="C2040") -> str:
    """Embed a footprint into a minimal .kicad_pcb as KiCad would save it."""
    node = parse_sexpr(fp_text)
    body = []
    for child in node[2:]:
        if not isinstance(child, list):
            continue
        if child[0] == "pad":
            at = next(c for c in child if isinstance(c, list) and c[0] == "at")
            px, py = float(at[1]), float(at[2])
            if side == cv.BOTTOM:
                py = -py
            body.append(f'(pad "{child[1]}" smd rect (at {px} {py} {theta}) (size 1 1) (layers "F.Cu"))')
    layer = "B.Cu" if side == cv.BOTTOM else "F.Cu"
    return (
        f'(kicad_pcb (version 20240108) (generator "pcbnew")\n'
        f'  (footprint "JLCImport:{node[1]}" (layer "{layer}") (at {x} {y} {theta})\n'
        f'    (property "Reference" "{ref}") (property "Value" "V") (property "LCSC" "{lcsc}")\n'
        f"    (attr smd)\n    " + "\n    ".join(body) + "\n  )\n)\n"
    )


# ---------------------------------------------------------------------------
# Conventions
# ---------------------------------------------------------------------------


class TestConventions:
    def test_board_cpl_roundtrip(self):
        assert cv.board_to_cpl(1.5, 2.5) == (1.5, -2.5)

    def test_rotate_ccw(self):
        x, y = cv.rotate((1.0, 0.0), 90)
        assert x == pytest.approx(0.0, abs=1e-12) and y == pytest.approx(1.0)

    @pytest.mark.parametrize("raw,expected", [(-90, 270), (360, 0), (720.5, 0.5), (-1e-12, 0), (359.9999999999, 0)])
    def test_normalize_angle(self, raw, expected):
        assert cv.normalize_angle(raw) == pytest.approx(expected)

    def test_side_mirror(self):
        assert cv.side_mirror((1.0, 2.0), cv.TOP) == (1.0, 2.0)
        assert cv.side_mirror((1.0, 2.0), cv.BOTTOM) == (1.0 * cv.BOTTOM_MIRROR[0], 2.0 * cv.BOTTOM_MIRROR[1])

    @pytest.mark.parametrize("side", [cv.TOP, cv.BOTTOM])
    @pytest.mark.parametrize("theta", [0, 30, 90, 180, 270])
    def test_kicad_pose_matches_placement_model(self, side, theta):
        """KiCad's own pose, expressed in the placement model, reproduces KiCad's pad positions."""
        local = [PadRecord("1", 1.2, -0.4), PadRecord("2", -0.7, 2.0)]
        board = kicad_place(local, 50.0, 30.0, theta, side)
        t, phi = cv.kicad_pose_to_cpl(50.0, 30.0, theta)
        for lp, bp in zip(local, board):
            mx, my = cv.place_local((lp.x, lp.y), t, phi, side)
            assert (mx, my) == pytest.approx(cv.board_to_cpl(bp.x, bp.y))

    @pytest.mark.parametrize(
        "phi,expected",
        [
            # Read off JLCPCB's placement preview of the bottom-side test board:
            # diodes/LEDs/tantalum/SOT-23 (phi = KiCad angle or +180) ...
            (0, 180),
            (90, 90),
            (180, 0),
            (270, 270),
            # ... and SOT-23-6/SOIC-8, whose JLC footprint sits at 90° to KiCad's.
            (90 + 90, 0),
            (270 + 90, 180),
        ],
    )
    def test_bottom_rotation_matches_jlc_preview(self, phi, expected):
        assert cv.cpl_rotation(phi, cv.BOTTOM) == expected
        assert cv.cpl_rotation(phi, cv.TOP) == cv.normalize_angle(phi)

    def test_cpl_rotation_is_normalized(self):
        assert cv.cpl_rotation(-90, cv.TOP) == 270
        assert cv.cpl_rotation(450, cv.BOTTOM) == 90


# ---------------------------------------------------------------------------
# Self-test: our own generated footprints placed on a board
# ---------------------------------------------------------------------------

POSES = [
    (100.0, 80.0, 0.0),
    (12.345, 67.89, 90.0),
    (150.0, 20.0, 180.0),
    (33.3, 44.4, 270.0),
    (75.0, 75.0, 45.0),
    (10.0, 10.0, -30.0),
]


class TestSelfConsistency:
    @pytest.mark.parametrize("part", ["C2040", "C427602", "C1027", "C6186", "C82899", "C87097"])
    @pytest.mark.parametrize("side", [cv.TOP, cv.BOTTOM])
    @pytest.mark.parametrize("pose", POSES)
    def test_recovers_pose_of_generated_footprint(self, part, side, pose):
        jlc = load_jlc(part)
        _, local = written_footprint_pads(jlc, part)
        x, y, theta = pose
        board = kicad_place(local, x, y, theta, side)

        fit = solve_placement(board, jlc.pads, side, kicad_orientation=theta)

        assert fit.confidence == HIGH
        assert fit.rms < 1e-3
        assert angle_close(fit.angle, theta)
        assert fit.origin == pytest.approx(cv.board_to_cpl(x, y), abs=1e-3)

    @pytest.mark.parametrize("side", [cv.TOP, cv.BOTTOM])
    @pytest.mark.parametrize("pose", POSES[:4])
    def test_end_to_end_through_kicad_pcb_file(self, side, pose):
        """Writer → .kicad_pcb text → reader → solver → CPL placement."""
        jlc = load_jlc("C2040")
        fp_text, _ = written_footprint_pads(jlc, "C2040")
        x, y, theta = pose
        (rec,) = read_board_text(board_text_with(fp_text, x, y, theta, side))
        assert rec.side == side

        pl = compute_placement(rec, jlc)

        assert pl.source == SOLVED
        assert (pl.x, pl.y) == pytest.approx(cv.board_to_cpl(x, y), abs=1e-3)
        assert angle_close(pl.rotation, cv.cpl_rotation(theta, side))


# ---------------------------------------------------------------------------
# Hand-built footprint with a different 0° orientation and origin
# ---------------------------------------------------------------------------


def rebased_library(jlc_pads, rot_deg, origin_number):
    """A 'stock library' version of JLC's footprint: rotated CCW by rot_deg, origin at a pin."""
    ox, oy = next((p.x, p.y) for p in jlc_pads if p.number == origin_number)
    out = []
    for p in jlc_pads:
        ux, uy = cv.local_to_up(p.x - ox, p.y - oy)
        rx, ry = cv.rotate((ux, uy), rot_deg)
        out.append(PadRecord(p.number, rx, -ry))  # back to local Y-down
    return out, cv.local_to_up(ox, oy)


class TestDifferentLibraryFrame:
    @pytest.mark.parametrize("part,pin", [("C87097", "1"), ("C427602", "1"), ("C2078", "2")])
    @pytest.mark.parametrize("side", [cv.TOP, cv.BOTTOM])
    @pytest.mark.parametrize("theta", [0.0, 90.0, 135.0, 270.0])
    def test_corrects_rotated_library_with_pin1_origin(self, part, pin, side, theta):
        jlc = load_jlc(part)
        lib, pin_up = rebased_library(jlc.pads, 90.0, pin)
        x, y = 40.0, 25.0
        board = kicad_place(lib, x, y, theta, side)

        fit = solve_placement(board, jlc.pads, side, kicad_orientation=theta)

        # KiCad pad (local_up) L = R(90) (J - pin).  Placed:
        #   p = t_k + R(theta) M L = t_k + R(theta) M R(90) (J - pin)
        # Top (M = I):        phi = theta + 90,  t = t_k - R(phi) pin
        # Bottom (M = diag(1,-1)): M R(90) = R(-90) M, so phi = theta - 90,
        #                     t = t_k - R(phi) M pin
        t_k = cv.board_to_cpl(x, y)
        if side == cv.TOP:
            phi = theta + 90.0
            shift = cv.rotate(pin_up, phi)
        else:
            phi = theta - 90.0
            shift = cv.rotate(cv.side_mirror(pin_up, cv.BOTTOM), phi)
        expected_t = (t_k[0] - shift[0], t_k[1] - shift[1])

        assert fit.confidence == HIGH
        assert angle_close(fit.angle, phi)
        assert fit.origin == pytest.approx(expected_t, abs=1e-6)
        # The KiCad footprint's own origin (pin 1) is clearly not JLC's.
        assert math.dist(fit.origin, t_k) > 0.5

    def test_placement_uses_solved_values_not_kicad(self):
        jlc = load_jlc("C87097")
        lib, _ = rebased_library(jlc.pads, 90.0, "1")
        rec = FootprintRecord(
            "U3", "TSSOP", "TSSOP-16", 40.0, 25.0, 0.0, pads=kicad_place(lib, 40.0, 25.0, 0.0, cv.TOP)
        )
        pl = compute_placement(rec, jlc)
        assert pl.source == SOLVED
        assert pl.rotation == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# Edge cases and flags
# ---------------------------------------------------------------------------


def soic8():
    """SOIC-8-like pads, local Y-down: 1-4 down the left, 5-8 up the right."""
    pads = []
    for i in range(4):
        pads.append(PadRecord(str(i + 1), -2.7, -1.905 + i * 1.27))
        pads.append(PadRecord(str(8 - i), 2.7, -1.905 + i * 1.27))
    return pads


class TestFlags:
    def test_mirrored_numbering_flagged_not_solved(self):
        jlc = soic8()
        # Mirror the numbering: a footprint drawn as seen from the bottom.
        mirrored = [PadRecord(p.number, -p.x, p.y) for p in jlc]
        board = kicad_place(mirrored, 10, 10, 0, cv.TOP)
        fit = solve_placement(board, jlc, cv.TOP)
        assert MIRRORED in fit.flags
        assert fit.confidence == LOW
        assert not fit.usable
        assert fit.alt_rms == pytest.approx(0.0, abs=1e-9)

    def test_bottom_side_part_is_not_flagged_mirrored(self):
        jlc = soic8()
        board = kicad_place(jlc, 10, 10, 90, cv.BOTTOM)
        fit = solve_placement(board, jlc, cv.BOTTOM)
        assert MIRRORED not in fit.flags
        assert fit.confidence == HIGH

    def test_top_footprint_wrongly_marked_bottom_is_flagged(self):
        jlc = soic8()
        board = kicad_place(jlc, 10, 10, 0, cv.TOP)
        fit = solve_placement(board, jlc, cv.BOTTOM)
        assert MIRRORED in fit.flags

    def test_too_few_pads(self):
        jlc = [PadRecord("1", 0, 0), PadRecord("2", 1, 0)]
        board = [PadRecord("A", 0, 0), PadRecord("2", 1, 0)]
        fit = solve_placement(board, jlc, cv.TOP)
        assert TOO_FEW_PADS in fit.flags
        assert PAD_MISMATCH in fit.flags
        assert not fit.usable

    def test_unnumbered_pads_ignored(self):
        jlc = [PadRecord("1", -1, 0), PadRecord("2", 1, 0), PadRecord("", 0, 3)]
        board = kicad_place([PadRecord("1", -1, 0), PadRecord("2", 1, 0), PadRecord("", 5, 5)], 0, 0, 0, cv.TOP)
        fit = solve_placement(board, jlc, cv.TOP)
        assert PAD_MISMATCH not in fit.flags
        assert fit.confidence == HIGH

    def test_repeated_pad_numbers_use_group_centroid(self):
        # QFN-ish: 4 signal pads plus an exposed pad split into 4 segments, all "5".
        jlc = [
            PadRecord("1", -2, -1),
            PadRecord("2", -2, 1),
            PadRecord("3", 2, 1),
            PadRecord("4", 2, -1),
            PadRecord("5", 0, 0),
        ]
        kicad_local = jlc[:4] + [PadRecord("5", dx, dy) for dx in (-0.5, 0.5) for dy in (-0.5, 0.5)]
        board = kicad_place(kicad_local, 20, 30, 90, cv.TOP)
        fit = solve_placement(board, jlc, cv.TOP)
        assert GROUP_SIZE_MISMATCH in fit.flags
        assert fit.confidence == HIGH
        assert angle_close(fit.angle, 90)
        assert fit.origin == pytest.approx((20, -30))

    def test_repeated_numbers_on_real_fixture(self):
        jlc = load_jlc("C2765186")  # USB-C with repeated shield pads 13/14
        assert any(len(v) > 1 for v in group_pads(jlc.pads).values())
        board = kicad_place(jlc.pads, 5, 5, 180, cv.BOTTOM)
        fit = solve_placement(board, jlc.pads, cv.BOTTOM)
        assert fit.confidence == HIGH
        assert angle_close(fit.angle, 180)

    def test_degenerate_groups(self):
        # Two groups whose centroids coincide: diagonal pairs of a 4-pad crystal.
        jlc = [PadRecord("1", -1, -1), PadRecord("1", 1, 1), PadRecord("2", -1, 1), PadRecord("2", 1, -1)]
        fit = solve_placement(kicad_place(jlc, 0, 0, 0, cv.TOP), jlc, cv.TOP)
        assert DEGENERATE in fit.flags
        assert not fit.usable

    def test_polarity_by_numbering_flag(self):
        jlc = load_jlc("C1027")  # 0603 two-pad
        fit = solve_placement(kicad_place(jlc.pads, 0, 0, 0, cv.TOP), jlc.pads, cv.TOP)
        assert POLARITY_BY_NUMBERING in fit.flags
        assert fit.confidence == HIGH

    def test_swapped_polarity_gives_180(self):
        jlc = load_jlc("C1027")
        swapped = [PadRecord("2" if p.number == "1" else "1", p.x, p.y) for p in jlc.pads]
        fit = solve_placement(kicad_place(swapped, 0, 0, 0, cv.TOP), jlc.pads, cv.TOP, kicad_orientation=0)
        assert angle_close(fit.angle, 180)

    def test_geometry_differs_medium(self):
        jlc = soic8()
        widened = [PadRecord(p.number, p.x * 1.05, p.y) for p in jlc]
        fit = solve_placement(kicad_place(widened, 0, 0, 0, cv.TOP), jlc, cv.TOP)
        assert GEOMETRY_DIFFERS in fit.flags
        assert fit.confidence == MEDIUM
        assert fit.usable
        assert fit.origin == pytest.approx((0, 0), abs=1e-9)

    def test_wrong_package_high_residual(self):
        jlc = [PadRecord("1", -0.75, 0), PadRecord("2", 0.75, 0)]
        board = [PadRecord("1", -2.0, 0), PadRecord("2", 2.0, 0)]
        fit = solve_placement(board, jlc, cv.TOP)
        assert HIGH_RESIDUAL in fit.flags
        assert fit.confidence == LOW

    def test_pad_mismatch_caps_confidence(self):
        jlc = soic8() + [PadRecord("9", 0, 0)]
        fit = solve_placement(kicad_place(soic8(), 0, 0, 0, cv.TOP), jlc, cv.TOP)
        assert PAD_MISMATCH in fit.flags
        assert fit.confidence == MEDIUM

    def test_mostly_unmatched_is_low(self):
        jlc = soic8()
        board = kicad_place(jlc[:2] + [PadRecord(f"X{i}", i, 5) for i in range(6)], 0, 0, 0, cv.TOP)
        fit = solve_placement(board, jlc, cv.TOP)
        assert fit.confidence == LOW

    def test_odd_angle_not_snapped(self):
        jlc = soic8()
        fit = solve_placement(kicad_place(jlc, 0, 0, 12.3, cv.TOP), jlc, cv.TOP)
        assert NOT_SNAPPED in fit.flags
        assert fit.angle == pytest.approx(12.3, abs=1e-6)
        # An unsnapped angle is never written as-is; the estimate rounds it.
        assert fit.confidence == LOW and not fit.usable
        assert fit.estimate_angle == pytest.approx(0.0)

    def test_odd_angle_snaps_to_kicad_orientation(self):
        jlc = soic8()
        fit = solve_placement(kicad_place(jlc, 0, 0, 12.3, cv.TOP), jlc, cv.TOP, kicad_orientation=12.3 - 90)
        assert NOT_SNAPPED not in fit.flags
        assert fit.snapped

    def test_near_90_snaps(self):
        jlc = soic8()
        fit = solve_placement(kicad_place(jlc, 0, 0, 90.4, cv.TOP), jlc, cv.TOP)
        assert fit.snapped
        assert fit.angle == 90.0
        assert fit.raw_angle == pytest.approx(90.4, abs=1e-6)

    def test_pad_numbers_normalized(self):
        jlc = [PadRecord("a1", 0, 0), PadRecord("A2", 1, 0), PadRecord("B1", 0, 1)]
        board = kicad_place([PadRecord(" A1 ", 0, 0), PadRecord("a2", 1, 0), PadRecord("b1", 0, 1)], 0, 0, 0, cv.TOP)
        assert solve_placement(board, jlc, cv.TOP).confidence == HIGH

    def test_describe_flags(self):
        assert describe_flags([MIRRORED, "unknown"])[1] == "unknown"
        assert "mirrored" in describe_flags([MIRRORED])[0]


class TestPlacementFallback:
    def _rec(self):
        pads = kicad_place(soic8(), 10.0, 20.0, 90.0, cv.TOP)
        return FootprintRecord("U1", "V", "F", 10.0, 20.0, 90.0, pads=pads)

    def test_no_jlc_footprint_uses_kicad(self):
        pl = compute_placement(self._rec(), None, "network down")
        assert pl.source == KICAD
        assert not pl.verified
        assert (pl.x, pl.y, pl.rotation) == (10.0, -20.0, 90.0)
        assert "network down" in pl.notes[0]

    def test_loose_fit_is_estimated_not_kicad(self):
        # Same pad numbers, wrong pitch: the fit is rejected but its orientation
        # (rounded to 90°) is kept rather than KiCad's angle.
        wide = [PadRecord(p.number, p.x * 1.4, p.y * 1.4) for p in soic8()]
        pl = compute_placement(self._rec(), JlcFootprint("C1", pads=wide))
        assert pl.source == ESTIMATED and pl.confidence == LOW and not pl.verified
        assert pl.fit is not None and not pl.fit.usable and HIGH_RESIDUAL in pl.fit.flags
        assert pl.rotation == pytest.approx(90.0)
        assert (pl.x, pl.y) == (pytest.approx(10.0), pytest.approx(-20.0))
        assert "RMS" in pl.notes[0]

    def test_unsnapped_fit_is_rounded(self):
        rec = self._rec()
        rec.pads = kicad_place(soic8(), 10.0, 20.0, 97.0, cv.TOP)  # KiCad says 90, pads say 97
        pl = compute_placement(rec, JlcFootprint("C1", pads=soic8()))
        assert pl.source == ESTIMATED and pl.rotation == pytest.approx(90.0)

    def test_mirrored_numbering_is_placed_by_pad_position_with_a_warning(self):
        mirrored = [PadRecord(p.number, -p.x, p.y) for p in soic8()]
        rec = self._rec()
        rec.properties = {"Reference": "U1", "LCSC": "C1"}
        pl = compute_placement(rec, JlcFootprint("C1", pads=mirrored))
        assert pl.source == SOLVED and pl.confidence == MEDIUM
        assert MIRRORED_BY_POSITION in pl.fit.flags
        assert pl.rotation % 180 == pytest.approx(90.0)  # body along the pads, like KiCad's 90°
        report = run_check([rec], 1, *_caches(JlcFootprint("C1", pads=mirrored)))
        assert report.rows[0].severity == WARNING

    def test_mirrored_chiral_pattern_falls_back_to_kicad(self):
        # An L with unequal arms: its mirror image can't be turned to fit, so no position match.
        jlc = [PadRecord("1", 0, 0), PadRecord("2", 2, 0), PadRecord("3", 0, 1)]
        board_local = [PadRecord(p.number, -p.x, p.y) for p in jlc]
        pads = kicad_place(board_local, 10.0, 20.0, 0.0, cv.TOP)
        rec = FootprintRecord("U1", "V", "F", 10.0, 20.0, 0.0, pads=pads)
        pl = compute_placement(rec, JlcFootprint("C1", pads=jlc))
        assert pl.source == KICAD and MIRRORED in pl.fit.flags

    def test_degenerate_fit_falls_back_to_kicad(self):
        jlc = JlcFootprint("C1", pads=[PadRecord("1", 0, 0), PadRecord("2", 0, 0)])
        pl = compute_placement(self._rec(), jlc)
        assert pl.source == KICAD
        assert pl.fit is not None and not pl.fit.usable


# KiCad's R_Array_Convex_4x0402 numbers its pads counter-clockwise (1-4 down
# the left, 5-8 up the right); JLC's C25724 numbers them clockwise, along the
# long axis horizontally.
KICAD_ARRAY_4X0402 = [PadRecord(str(n), -0.5, y) for n, y in zip(range(1, 5), (-0.75, -0.25, 0.25, 0.75))] + [
    PadRecord(str(n), 0.5, y) for n, y in zip(range(5, 9), (0.75, 0.25, -0.25, -0.75))
]
JLC_ARRAY_4X0402 = [PadRecord(str(n), x, -0.427) for n, x in zip(range(1, 5), (-0.75, -0.25, 0.25, 0.75))] + [
    PadRecord(str(n), x, 0.427) for n, x in zip(range(8, 4, -1), (-0.75, -0.25, 0.25, 0.75))
]


class TestMirroredArrays:
    def _rec(self, theta, side=cv.TOP):
        return FootprintRecord(
            reference="RN1",
            value="10k",
            footprint="R_Array_Convex_4x0402",
            x=20.0,
            y=30.0,
            orientation=theta,
            side=side,
            pads=kicad_place(KICAD_ARRAY_4X0402, 20.0, 30.0, theta, side),
            properties={"Reference": "RN1", "LCSC": "C25724"},
        )

    # Checked in JLCPCB's placement preview: the body lies along the pads at every angle.
    @pytest.mark.parametrize(
        "theta,side,expected",
        [
            (0, cv.TOP, 270),
            (90, cv.TOP, 0),
            (180, cv.TOP, 90),
            (270, cv.TOP, 180),
            (0, cv.BOTTOM, 90),
            (90, cv.BOTTOM, 0),
        ],
    )
    def test_resistor_array_is_placed_along_its_pads(self, theta, side, expected):
        jlc = JlcFootprint("C25724", title="RES-ARRAY-SMD_0402-8P-L2.0-W1.0-BL", pads=JLC_ARRAY_4X0402)
        pl = compute_placement(self._rec(theta, side), jlc)
        assert pl.source == SOLVED and pl.confidence == MEDIUM
        assert MIRRORED_ARRAY in pl.fit.flags
        assert pl.rotation == pytest.approx(expected)
        assert (pl.x, pl.y) == (pytest.approx(20.0), pytest.approx(-30.0))

    def test_array_mirror_is_a_note_not_a_warning(self):
        jlc = JlcFootprint("C25724", title="RES-ARRAY-SMD_0402-8P-L2.0-W1.0-BL", pads=JLC_ARRAY_4X0402)
        report = run_check([self._rec(0)], 1, *_caches(jlc))
        assert report.rows[0].severity == INFO

    def test_tie_break_keeps_each_resistor_on_its_own_pads(self):
        # Pin 1 nearest pin 1 lands JLC pad k on board pad 9 - k: the same resistor, ends swapped.
        renumbered = match_pads_by_position(KICAD_ARRAY_4X0402, JLC_ARRAY_4X0402, cv.TOP, 0.0, break_ties=True)
        assert {p.number: q.number for p, q in zip(JLC_ARRAY_4X0402, renumbered)} == {
            str(k): str(9 - k) for k in range(1, 9)
        }
        assert match_pads_by_position(KICAD_ARRAY_4X0402, JLC_ARRAY_4X0402, cv.TOP, 0.0) is None

    @pytest.mark.parametrize(
        "footprint,title,expected",
        [
            ("Resistor_SMD:R_Array_Convex_4x0402", "", True),
            ("R_Array_Concave_4x0603", "", True),
            ("SOIC-8", "RES-ARRAY-SMD_0402-8P", True),
            ("SOIC-8", "SOIC-8_L4.9-W3.9", False),
        ],
    )
    def test_isolated_array_names(self, footprint, title, expected):
        assert is_isolated_array(footprint, title) is expected


class TestSymmetricRenumberedEstimate:
    """A tact switch: KiCad numbers its pads 1,1,2,2, JLC numbers them 1-4."""

    KICAD = [
        PadRecord("1", -3.0, -2.25),
        PadRecord("1", 3.0, -2.25),
        PadRecord("2", -3.0, 2.25),
        PadRecord("2", 3.0, 2.25),
    ]

    def board(self, jlc_size):
        pads = [PadRecord(p.number, 100.0 + p.x, 50.0 + p.y) for p in self.KICAD]
        return FootprintRecord("SW1", "SW", "SW_PUSH_6mm", 100.0, 50.0, 0.0, cv.TOP, pads, {"LCSC": "C1"})

    @pytest.mark.parametrize("hx, hy", [(3.0, 2.25), (2.25, 1.5), (3.7, 3.25)])
    def test_estimate_is_centred_on_the_pads(self, hx, hy):
        jlc = JlcFootprint(
            "C1",
            pads=[PadRecord(n, x, y) for n, x, y in (("1", -hx, -hy), ("2", hx, -hy), ("3", -hx, hy), ("4", hx, hy))],
        )
        pl = compute_placement(self.board(None), jlc)
        assert pl.source == ESTIMATED
        assert (pl.x, pl.y) == (pytest.approx(100.0, abs=1e-6), pytest.approx(-50.0, abs=1e-6))
        assert {POSITION_ESTIMATE, POSITION_LOOSE, POSITION_CENTRED} & set(pl.fit.flags)


class TestRepeatedPadNumbers:
    def place(self, kicad, jlc_pads):
        pads = [PadRecord(n, 50.0 + x, 60.0 + y) for n, x, y in kicad]
        fp = FootprintRecord("X1", "X", "Test", 50.0, 60.0, 0.0, cv.TOP, pads, {"LCSC": "C1"})
        return compute_placement(fp, JlcFootprint("C1", pads=[PadRecord(n, x, y) for n, x, y in jlc_pads]))

    def test_terminals_split_into_three_pads_still_solve(self):
        # KiCad's Murata CSTCR resonator: each terminal is three pads; JLC's has one.
        kicad = [(str(t), x, (t - 2) * 1.5) for t in (1, 2, 3) for x in (-1.05, 0.0, 1.05)]
        pl = self.place(kicad, [("1", -1.5, 0.0), ("2", 0.0, 0.0), ("3", 1.5, 0.0)])
        assert pl.source == SOLVED and pl.confidence == HIGH
        assert (pl.x, pl.y) == (pytest.approx(50.0), pytest.approx(-60.0))

    def test_extra_jlc_pad_is_left_out_of_the_estimate(self):
        # A switch numbered 1,1,2,2 against JLC's 1-4 plus a centre pad 5.
        kicad = [("1", -3.0, -2.25), ("1", 3.0, -2.25), ("2", -3.0, 2.25), ("2", 3.0, 2.25)]
        jlc = [("1", -3.0, -2.25), ("2", 3.0, -2.25), ("3", -3.0, 2.25), ("4", 3.0, 2.25), ("5", 0.0, 0.0)]
        pl = self.place(kicad, jlc)
        assert pl.source == ESTIMATED and pl.rotation in (0.0, 180.0)
        assert (pl.x, pl.y) == (pytest.approx(50.0, abs=1e-6), pytest.approx(-60.0, abs=1e-6))

    def test_loose_match_prefers_the_better_fitting_orientation(self):
        # JLC 6.0 x 3.7 mm pattern against a 5.6 x 4.2 mm board pattern: 0/180 fit, 90/270 would short the rows.
        kicad = [("1", -2.8, -2.1), ("1", 2.8, -2.1), ("2", -2.8, 2.1), ("2", 2.8, 2.1)]
        jlc = [("1", -3.0, -1.85), ("2", 3.0, -1.85), ("3", -3.0, 1.85), ("4", 3.0, 1.85)]
        pl = self.place(kicad, jlc)
        assert pl.rotation in (0.0, 180.0)
