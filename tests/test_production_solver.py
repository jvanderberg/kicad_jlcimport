"""Tests for the CPL placement solver and coordinate conventions."""

import json
import math
from pathlib import Path

import pytest

from kicad_jlcimport.kicad.footprint_writer import write_footprint
from kicad_jlcimport.production import conventions as cv
from kicad_jlcimport.production.jlc_footprints import JlcFootprint, jlc_footprint_from_data
from kicad_jlcimport.production.kicad_pcb import parse_sexpr, read_board_text
from kicad_jlcimport.production.models import FootprintRecord, PadRecord
from kicad_jlcimport.production.placement import ESTIMATED, KICAD, SOLVED, compute_placement
from kicad_jlcimport.production.solver import (
    DEGENERATE,
    GEOMETRY_DIFFERS,
    GROUP_SIZE_MISMATCH,
    HIGH,
    HIGH_RESIDUAL,
    LOW,
    MEDIUM,
    MIRRORED,
    NOT_SNAPPED,
    PAD_MISMATCH,
    POLARITY_BY_NUMBERING,
    TOO_FEW_PADS,
    describe_flags,
    group_pads,
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

    def test_mirrored_is_never_estimated(self):
        mirrored = [PadRecord(p.number, -p.x, p.y) for p in soic8()]
        pl = compute_placement(self._rec(), JlcFootprint("C1", pads=mirrored))
        assert pl.source == KICAD and MIRRORED in pl.fit.flags

    def test_degenerate_fit_falls_back_to_kicad(self):
        jlc = JlcFootprint("C1", pads=[PadRecord("1", 0, 0), PadRecord("2", 0, 0)])
        pl = compute_placement(self._rec(), jlc)
        assert pl.source == KICAD
        assert pl.fit is not None and not pl.fit.usable
