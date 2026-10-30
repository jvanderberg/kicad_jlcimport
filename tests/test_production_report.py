"""Tests for availability checks, overrides, BOM/CPL output and the check orchestration."""

import csv
import json
import os
import zipfile
from unittest import mock

import pytest

from kicad_jlcimport.easyeda import api
from kicad_jlcimport.easyeda.api import APIError, SSLCertError
from kicad_jlcimport.production import availability as av
from kicad_jlcimport.production import conventions as cv
from kicad_jlcimport.production.cache import KeyedCache
from kicad_jlcimport.production.export import (
    board_basename,
    default_output_dir,
    make_caches,
    resolve_output_dir,
    write_bom_cpl,
)
from kicad_jlcimport.production.jlc_footprints import JlcFootprint, fetch_jlc_footprint
from kicad_jlcimport.production.models import FootprintRecord, PadRecord, natural_key, normalize_lcsc
from kicad_jlcimport.production.outputs import build_bom_rows, build_cpl_rows, write_csv, zip_files
from kicad_jlcimport.production.placement import (
    KICAD,
    MANUAL,
    POSITION_OFFSET_FIELD,
    ROTATION_OFFSET_FIELD,
    SOLVED,
    Placement,
    compute_placement,
    parse_override,
)
from kicad_jlcimport.production.report import ERROR, INFO, WARNING, format_row, row_cells, run_check

RES_PADS = [PadRecord("1", -0.8, 0.0), PadRecord("2", 0.8, 0.0)]


def placed(pads, x, y, theta=0.0):
    """Board pads for a top-side footprint whose library frame equals the given pads."""
    import math

    c, s = math.cos(math.radians(theta)), math.sin(math.radians(theta))
    return [PadRecord(p.number, x + p.x * c + p.y * s, y - p.x * s + p.y * c) for p in pads]


def fp(ref, lcsc="C25804", value="10k", footprint="R_0603", x=10.0, y=10.0, theta=0.0, **kw):
    props = {"Reference": ref, "Value": value}
    if lcsc is not None:
        props["LCSC"] = lcsc
    props.update(kw.pop("props", {}))
    return FootprintRecord(
        reference=ref,
        value=value,
        footprint=footprint,
        x=x,
        y=y,
        orientation=theta,
        pads=placed(RES_PADS, x, y, theta),
        properties=props,
        **kw,
    )


def part_info(lcsc, stock=1000, kind="Basic", breaks=None, price=0.01):
    return {
        "lcsc": lcsc,
        "stock": stock,
        "type": kind,
        "price": price,
        "price_breaks": breaks if breaks is not None else [(1, 0.01), (100, 0.005)],
        "description": "desc",
    }


def caches(parts=None, footprints=None, part_error=None, fp_error=None):
    parts = parts or {}
    footprints = footprints or {}

    def part_fetch(lcsc):
        if part_error:
            raise part_error
        return parts.get(lcsc)

    def fp_fetch(lcsc):
        if fp_error:
            raise fp_error
        if lcsc not in footprints:
            raise APIError(f"No component found for {lcsc}")
        return footprints[lcsc]

    return KeyedCache(part_fetch), KeyedCache(fp_fetch)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestModels:
    @pytest.mark.parametrize(
        "raw,expected", [("C25804", "C25804"), (" c25804 ", "C25804"), ("25804", "C25804"), ("", None), ("abc", None)]
    )
    def test_normalize_lcsc(self, raw, expected):
        assert normalize_lcsc(raw) == expected

    def test_lcsc_from_alternate_field_names(self):
        rec = FootprintRecord("R1", "", "", 0, 0, 0, properties={"JLCPCB Part #": "C1"})
        assert rec.lcsc == "C1"
        rec = FootprintRecord("R1", "", "", 0, 0, 0, properties={"lcsc part": "c2"})
        assert rec.lcsc == "C2"

    def test_bom_cpl_membership(self):
        assert fp("R1").in_cpl
        assert not fp("R1", dnp=True).in_bom
        assert not fp("R1", exclude_from_bom=True).in_cpl
        r = fp("R1", exclude_from_pos=True)
        assert r.in_bom and not r.in_cpl

    def test_natural_key(self):
        assert sorted(["R10", "R2", "C1"], key=natural_key) == ["C1", "R2", "R10"]


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_group_by_lcsc_skips_dnp_and_missing(self):
        groups = av.group_by_lcsc([fp("R2"), fp("R10"), fp("R1"), fp("R3", dnp=True), fp("R4", lcsc=None)])
        assert groups == {"C25804": ["R1", "R2", "R10"]}

    @pytest.mark.parametrize(
        "breaks,qty,expected",
        [([(1, 0.1), (100, 0.05)], 5, 0.1), ([(1, 0.1), (100, 0.05)], 100, 0.05), ([(20, 0.2)], 5, 0.2)],
    )
    def test_price_for_quantity(self, breaks, qty, expected):
        assert av.price_for_quantity(breaks, qty) == expected

    def test_ok(self):
        a = av.evaluate("C1", ["R1", "R2"], 10, part_info("C1", stock=100))
        assert a.status == av.OK
        assert a.qty_needed == 20
        assert a.unit_price == 0.01
        assert a.total_price == pytest.approx(0.2)
        assert not a.is_problem and not a.is_extended

    def test_low_stock(self):
        a = av.evaluate("C1", ["R1", "R2"], 10, part_info("C1", stock=15))
        assert a.status == av.LOW_STOCK
        assert "15 < 20" in a.message

    def test_out_of_stock(self):
        assert av.evaluate("C1", ["R1"], 1, part_info("C1", stock=0)).status == av.OUT_OF_STOCK

    @pytest.mark.parametrize(
        "stock,needed,expected",
        [(25, 20, av.STOCK_TIGHT), (30, 20, av.OK), (1050, 1000, av.STOCK_TIGHT), (1100, 1000, av.OK)],
    )
    def test_stock_margin(self, stock, needed, expected):
        # Warn unless stock beats the need by 10%, and by at least 10 parts.
        a = av.evaluate("C1", ["R1"], needed, part_info("C1", stock=stock))
        assert a.status == expected

    def test_preferred_extended(self):
        info = dict(part_info("C1", kind="Extended"), preferred=True)
        a = av.evaluate("C1", ["U1"], 1, info)
        assert a.part_type == av.PREFERRED and a.is_preferred and not a.is_extended
        basic = dict(part_info("C2"), preferred=True)
        assert av.evaluate("C2", ["R1"], 1, basic).part_type == av.BASIC

    def test_not_found(self):
        a = av.evaluate("C1", ["R1"], 1, None)
        assert a.status == av.NOT_FOUND and a.is_problem and a.total_price is None

    def test_extended_and_price_fallback(self):
        a = av.evaluate("C1", ["U1"], 1, part_info("C1", kind="Extended", breaks=[], price=1.5))
        assert a.is_extended
        assert a.unit_price == 1.5

    def test_bad_stock_value(self):
        a = av.evaluate("C1", ["U1"], 1, {"stock": "n/a"})
        assert a.status == av.OUT_OF_STOCK

    def test_lookup_failed(self):
        a = av.lookup_failed("C1", ["U1"], 3, "timeout")
        assert a.status == av.LOOKUP_FAILED and a.qty_needed == 3 and "timeout" in a.message


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


class TestOverrides:
    def test_no_override(self):
        assert parse_override(fp("R1")) == (None, [])

    @pytest.mark.parametrize("text,expected", [("90", 90.0), ("-90°", -90.0), (" 45 deg", 45.0), ("+180", 180.0)])
    def test_rotation_offset(self, text, expected):
        ov, errors = parse_override(fp("R1", props={ROTATION_OFFSET_FIELD: text}))
        assert ov.rotation_offset == expected and errors == []

    @pytest.mark.parametrize("text", ["0.5, -0.25", "0.5 -0.25", "0.5mm; -0.25mm", "0.5 mm, -0.25 mm"])
    def test_position_offset(self, text):
        ov, errors = parse_override(fp("R1", props={POSITION_OFFSET_FIELD: text}))
        assert (ov.dx, ov.dy) == (0.5, -0.25) and errors == []

    def test_bad_values_reported(self):
        ov, errors = parse_override(fp("R1", props={ROTATION_OFFSET_FIELD: "abc", POSITION_OFFSET_FIELD: "1"}))
        assert ov is None  # nothing readable: no override
        assert len(errors) == 2

    def test_override_uses_kicad_pose_plus_offsets(self):
        rec = fp("R1", x=10, y=20, theta=90, props={ROTATION_OFFSET_FIELD: "-90", POSITION_OFFSET_FIELD: "1, 2"})
        jlc = JlcFootprint("C25804", pads=RES_PADS)
        pl = compute_placement(rec, jlc)
        assert pl.source == MANUAL and pl.verified
        assert (pl.x, pl.y, pl.rotation) == (11.0, -18.0, 0.0)
        assert pl.notes[0].startswith("Manual override: rotation 90° (KiCad) -90° = 0°")
        assert any("Solver suggests" in n for n in pl.notes)

    def test_bottom_override_offsets_the_bottom_cpl_rotation(self):
        # KiCad orientation 0 on the bottom is CPL rotation 180; the offset is added to that.
        rec = fp("R1", theta=0, side="bottom", props={ROTATION_OFFSET_FIELD: "90"})
        pl = compute_placement(rec)
        assert pl.rotation == 270.0
        assert pl.notes[0].startswith("Manual override: rotation 180° (KiCad) +90° = 270°")

    def test_unreadable_override_keeps_the_solver_and_warns(self):
        # KiCad says 90°, the solver says 0°: a typo in the field must not switch to KiCad's pose.
        rec = fp("R1", theta=0, props={ROTATION_OFFSET_FIELD: "ninety"})
        rec.orientation = 90.0
        pl = compute_placement(rec, JlcFootprint("C25804", pads=RES_PADS))
        assert pl.source != MANUAL and pl.rotation == 0.0
        assert any("not a number" in e for e in pl.override_errors)
        jlc = JlcFootprint("C25804", pads=RES_PADS)
        report = run_check([rec], 1, *caches({"C25804": part_info("C25804")}, {"C25804": jlc}))
        assert any(i.severity == WARNING and "not a number" in i.message for i in report.rows[0].issues)

    def test_partly_readable_override_applies_the_readable_field(self):
        rec = fp("R1", props={ROTATION_OFFSET_FIELD: "90", POSITION_OFFSET_FIELD: "oops"})
        pl = compute_placement(rec, JlcFootprint("C25804", pads=RES_PADS))
        assert pl.source == MANUAL and pl.rotation == 90.0
        assert len(pl.override_errors) == 1


# ---------------------------------------------------------------------------
# BOM / CPL output
# ---------------------------------------------------------------------------


class TestOutputs:
    def test_bom_grouping(self):
        rows = build_bom_rows(
            [
                fp("R10"),
                fp("R2"),
                fp("R1", value="1k", lcsc="C21190"),
                fp("R3", dnp=True),
                fp("R4", exclude_from_bom=True),
                fp("J1", value="Conn", footprint="PinHeader", lcsc=None),
                fp("U1", value="MCU", footprint="QFN", lcsc="bogus"),
            ]
        )
        assert rows == [
            ["Conn", "J1", "PinHeader", ""],
            ["1k", "R1", "R_0603", "C21190"],
            ["10k", "R2,R10", "R_0603", "C25804"],
            ["MCU", "U1", "QFN", "bogus"],
        ]

    def test_cpl_rows(self):
        pls = [
            Placement("R10", cv.TOP, 1.0, -2.5, 90.0, SOLVED, "high"),
            Placement("C1", cv.BOTTOM, 3.14159, 0.0, 359.999, KICAD, "unverified"),
            Placement("R2", cv.TOP, 0.0, 0.0, 12.5, SOLVED, "high"),
        ]
        rows = build_cpl_rows(pls)
        assert rows == [
            ["C1", "3.1416mm", "0.0000mm", "0", "Bottom"],
            ["R2", "0.0000mm", "0.0000mm", "12.5", "Top"],
            ["R10", "1.0000mm", "-2.5000mm", "90", "Top"],
        ]

    def test_write_csv_and_zip(self, tmp_path):
        path = tmp_path / "out" / "x.csv"
        write_csv(str(path), ["A", "B"], [["1", "a,b"]])
        with open(path, newline="") as f:
            assert list(csv.reader(f)) == [["A", "B"], ["1", "a,b"]]
        z = zip_files([str(path)], str(tmp_path / "z" / "f.zip"))
        with zipfile.ZipFile(z) as zf:
            assert zf.namelist() == ["x.csv"]


# ---------------------------------------------------------------------------
# run_check
# ---------------------------------------------------------------------------


class TestRunCheck:
    def test_full_report(self):
        footprints = [
            fp("R1"),
            fp("R2", theta=90, x=20),
            fp("U1", lcsc="C999", value="MCU"),
            fp("J1", lcsc=None, value="Conn"),
            fp("D1", lcsc="nope"),
            fp("R9", dnp=True),
            fp("TP1", lcsc="C25804", exclude_from_pos=True),
        ]
        part_cache, fp_cache = caches(
            parts={"C25804": part_info("C25804", stock=5), "C999": part_info("C999", kind="Extended")},
            footprints={"C25804": JlcFootprint("C25804", pads=RES_PADS)},
        )
        progress = mock.Mock()
        report = run_check(footprints, 2, part_cache, fp_cache, progress=progress)

        rows = {r.reference: r for r in report.rows}
        assert progress.call_count == 2
        # C25804 used 3× per board (R1, R2, TP1) × 2 boards = 6 > stock 5
        assert report.availability["C25804"].status == av.LOW_STOCK
        assert rows["R1"].severity == ERROR
        assert rows["R1"].placement.source == SOLVED
        assert rows["R2"].placement.rotation == pytest.approx(90.0)
        assert rows["R2"].placement.x == pytest.approx(20.0)
        # U1: extended, JLC footprint fetch failed → fallback
        assert rows["U1"].placement.source == KICAD
        assert any(i.severity == INFO and "Extended" in i.message for i in rows["U1"].issues)
        assert any(i.severity == WARNING and "JLC footprint unavailable" in i.message for i in rows["U1"].issues)
        # J1: no LCSC → warning, unverified placement info
        assert rows["J1"].severity == WARNING
        assert rows["J1"].placement.source == KICAD
        # D1: invalid number
        assert rows["D1"].severity == ERROR
        # DNP: no placement, no issues
        assert rows["R9"].placement is None and rows["R9"].severity == ""
        # TP1 in BOM but not CPL
        assert rows["TP1"].placement is None

        assert report.extended_parts == ["C999"]
        assert not report.double_sided
        assert report.estimated_parts_cost() == pytest.approx(6 * 0.01 + 2 * 0.01)
        assert len(report.cpl_rows()) == 5
        text = report.to_text()
        assert "extended parts" in text and "unverified" in text
        assert "U1" in format_row(rows["U1"])
        assert row_cells(rows["R9"])["stock"] == "DNP"

    def test_fee_summary_and_tight_stock(self):
        parts = {
            "C1": dict(part_info("C1", kind="Extended"), preferred=True),
            "C2": part_info("C2", kind="Extended"),
            "C3": part_info("C3", stock=5),
        }
        footprints = [fp("U1", lcsc="C1"), fp("U2", lcsc="C2"), fp("R1", lcsc="C3", side=cv.BOTTOM)]
        report = run_check(footprints, 1, *caches(parts))
        assert report.preferred_parts == ["C1"] and report.extended_parts == ["C2"]
        assert report.double_sided
        text = report.to_text()
        assert "1 preferred extended parts (no loading fee on Economic assembly): C1" in text
        assert "1 extended parts (each adds a feeder loading fee): C2" in text
        assert "Standard PCBA" in text
        rows = {r.reference: r for r in report.rows}
        assert any(i.severity == WARNING and "close to the 1 needed" in i.message for i in rows["R1"].issues)
        assert row_cells(rows["U1"])["type"] == "Preferred"

    def test_lookup_failures_do_not_raise(self):
        part_cache, fp_cache = caches(part_error=APIError("down"), fp_error=APIError("down"))
        report = run_check([fp("R1")], 1, part_cache, fp_cache)
        row = report.rows[0]
        assert report.availability["C25804"].status == av.LOOKUP_FAILED
        assert row.severity == ERROR  # nothing about the part could be checked
        assert row.placement.source == KICAD

    def test_unexpected_exception_reported(self):
        part_cache, fp_cache = caches(part_error=RuntimeError(""))
        report = run_check([fp("R1")], 1, part_cache, fp_cache)
        assert "RuntimeError" in report.availability["C25804"].message

    def test_ssl_error_handler_retries(self):
        calls = []

        def part_fetch(lcsc):
            calls.append(lcsc)
            if len(calls) == 1:
                raise SSLCertError("bad cert")
            return part_info(lcsc)

        handler = mock.Mock()
        _, fp_cache = caches(footprints={"C25804": JlcFootprint("C25804", pads=RES_PADS)})
        report = run_check([fp("R1")], 1, KeyedCache(part_fetch), fp_cache, on_ssl_error=handler)
        handler.assert_called()
        assert report.availability["C25804"].status == av.OK

    def test_ssl_error_without_handler_propagates(self):
        part_cache, fp_cache = caches(part_error=SSLCertError("bad cert"))
        with pytest.raises(SSLCertError):
            run_check([fp("R1")], 1, part_cache, fp_cache)

    def test_ssl_error_after_handler_retry_is_failure(self):
        handler = mock.Mock()
        part_cache, fp_cache = caches(part_error=SSLCertError("still bad"))
        report = run_check([fp("R1")], 1, part_cache, fp_cache, on_ssl_error=handler)
        handler.assert_called()
        assert report.availability["C25804"].status == av.LOOKUP_FAILED

    def test_duplicates_and_empty_board(self):
        part_cache, fp_cache = caches()
        report = run_check([fp("R1", lcsc=None), fp("R1", lcsc=None)], 1, part_cache, fp_cache)
        assert report.board_issues[0].severity == ERROR
        assert "R1" in report.board_issues[0].message
        assert "[ERROR] Duplicate" in report.to_text()
        assert report.estimated_parts_cost() is None

    def test_issue_lines_grouped_worst_first(self):
        part_cache, fp_cache = caches(parts={"C999": part_info("C999", kind="Extended")})
        report = run_check(
            [fp("TP1", lcsc=None), fp("TP2", lcsc=None), fp("D1", lcsc="bogus"), fp("U1", lcsc="C999")],
            1,
            part_cache,
            fp_cache,
        )
        lines = report.issue_lines()
        assert lines[0] == '[ERROR] D1: Invalid LCSC part number "bogus"'
        assert any(line.startswith("[WARNING] TP1, TP2: No LCSC part number") for line in lines)
        assert any(line.startswith("[WARNING] U1: Unverified") for line in lines)
        assert not any(line.startswith("[INFO]") for line in lines)
        assert "[INFO] U1: Extended part (loading fee)" in report.issue_lines(INFO)

    def test_issue_lines_include_board_issues(self):
        part_cache, fp_cache = caches()
        report = run_check([fp("R1", lcsc=None), fp("R1", lcsc=None)], 1, part_cache, fp_cache)
        assert report.issue_lines()[0] == "[ERROR] Duplicate designators: R1"

    def test_stock_check_can_be_skipped(self):
        part_fetch = mock.Mock(side_effect=AssertionError("parts library must not be queried"))
        _, fp_cache = caches(footprints={"C25804": JlcFootprint("C25804", pads=RES_PADS)})
        report = run_check([fp("R1"), fp("R2")], 3, KeyedCache(part_fetch), fp_cache, check_stock=False)
        a = report.availability["C25804"]
        assert a.status == av.NOT_CHECKED and a.qty_needed == 6
        assert report.count(ERROR) == 0 and report.count(WARNING) == 0
        assert report.rows[0].placement.source == SOLVED
        assert "not checked" in "\n".join(report.summary_lines())
        assert row_cells(report.rows[0])["stock"] == "Stock not checked"
        part_fetch.assert_not_called()

    def test_medium_confidence_and_manual_notes(self):
        wide = [PadRecord("1", -0.9, 0.0), PadRecord("2", 0.9, 0.0)]
        part_cache, fp_cache = caches(
            parts={"C25804": part_info("C25804")},
            footprints={"C25804": JlcFootprint("C25804", pads=wide)},
        )
        report = run_check(
            [fp("R1"), fp("R2", props={ROTATION_OFFSET_FIELD: "90"})],
            1,
            part_cache,
            fp_cache,
        )
        r1, r2 = report.rows
        assert r1.placement.confidence == "medium"
        assert any("Placement solved" in i.message for i in r1.issues)
        assert r2.placement.source == MANUAL
        assert r2.severity == INFO

    def test_caches_reused(self):
        part_fetch = mock.Mock(return_value=part_info("C25804"))
        fp_fetch = mock.Mock(return_value=JlcFootprint("C25804", pads=RES_PADS))
        pc, fc = KeyedCache(part_fetch), KeyedCache(fp_fetch)
        run_check([fp("R1")], 1, pc, fc)
        run_check([fp("R1")], 1, pc, fc)
        assert part_fetch.call_count == 1 and fp_fetch.call_count == 1
        pc.clear()
        run_check([fp("R1")], 1, pc, fc)
        assert part_fetch.call_count == 2

    def test_discard_where_refetches_matching_entries(self):
        fetch = mock.Mock(side_effect=lambda key: key.lower())
        cache = KeyedCache(fetch)
        cache.get("A")
        cache.get("B")
        cache.discard_where(lambda value: value == "a")
        cache.get("A")
        cache.get("B")
        assert [c.args[0] for c in fetch.call_args_list] == ["A", "B", "A"]


# ---------------------------------------------------------------------------
# Export helpers and network wrappers (all mocked)
# ---------------------------------------------------------------------------


class TestResolveOutputDir:
    BOARD = os.path.join(os.sep, "proj", "b.kicad_pcb")
    PROJ = os.path.join(os.sep, "proj")

    def test_relative_is_next_to_the_board(self):
        assert resolve_output_dir("production", self.BOARD) == os.path.join(self.PROJ, "production")

    def test_kiprjmod(self):
        assert resolve_output_dir("${KIPRJMOD}/out", self.BOARD) == os.path.join(self.PROJ, "out")

    def test_absolute_and_home(self, monkeypatch):
        monkeypatch.setenv("HOME", os.path.join(os.sep, "home", "me"))
        assert resolve_output_dir(os.path.join(os.sep, "x", "y"), self.BOARD) == os.path.join(os.sep, "x", "y")
        assert resolve_output_dir("~/out", self.BOARD) == os.path.join(os.sep, "home", "me", "out")

    def test_empty_or_unsaved(self):
        assert resolve_output_dir("  ", self.BOARD) is None
        assert resolve_output_dir("production", None) is None
        assert resolve_output_dir(os.path.join(os.sep, "x"), None) == os.path.join(os.sep, "x")


class TestExport:
    def test_paths(self, tmp_path):
        board = tmp_path / "proj" / "my_board.kicad_pcb"
        assert default_output_dir(str(board)) == str(tmp_path / "proj" / "production")
        assert board_basename(str(board)) == "my_board"
        assert board_basename("") == "board"

    def test_write_bom_cpl(self, tmp_path):
        part_cache, fp_cache = caches(
            parts={"C25804": part_info("C25804")}, footprints={"C25804": JlcFootprint("C25804", pads=RES_PADS)}
        )
        report = run_check([fp("R1", x=5, y=6, theta=180)], 1, part_cache, fp_cache)
        paths = write_bom_cpl(report, str(tmp_path / "production"), "b")
        with open(paths["cpl"]) as f:
            assert f.read().splitlines() == ["Designator,Mid X,Mid Y,Rotation,Layer", "R1,5.0000mm,-6.0000mm,180,Top"]
        with open(paths["bom"]) as f:
            assert f.read().splitlines()[1] == "10k,R1,R_0603,C25804"
        assert "JLCPCB production check" in open(paths["report"]).read()

    def test_make_caches_use_api(self):
        pc, fc = make_caches()
        with mock.patch.object(api, "search_components", return_value={"total": 0, "results": []}):
            assert pc.get("C1") is None

    def test_fetch_jlc_footprint(self):
        data = {
            "title": "R0603",
            "dataStr": {
                "head": {"x": 4000, "y": 3000},
                "shape": [
                    "PAD~RECT~3997~3000~3~3~1~~1~0~~0~g1",
                    "PAD~RECT~4003~3000~3~3~1~~2~0~~0~g2",
                ],
            },
        }
        uuids = [{"component_uuid": "s"}, {"component_uuid": "f"}]
        with mock.patch.object(api, "fetch_component_uuids", return_value=uuids):
            with mock.patch.object(api, "fetch_component_data", return_value=data) as fetch_data:
                jlc = fetch_jlc_footprint("C1")
        # Two-pad part: footprint first, then the symbol for pin names (none here).
        assert [c.args[0] for c in fetch_data.call_args_list] == ["f", "s"]
        assert jlc.pin_names == {}
        assert jlc.title == "R0603"
        assert [(p.number, round(p.x, 4), p.y) for p in jlc.pads] == [("1", -0.762, 0.0), ("2", 0.762, 0.0)]


class TestApiLookupPart:
    def _resp(self, items):
        return {
            "data": {
                "componentPageInfo": {
                    "total": len(items),
                    "list": items,
                }
            }
        }

    def _patch(self, items):
        body = json.dumps(self._resp(items)).encode()
        resp = mock.MagicMock()
        resp.read.return_value = body
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        return mock.patch.object(api, "_urlopen", return_value=resp)

    def test_exact_match_and_price_breaks(self):
        items = [
            {"componentCode": "C258040", "stockCount": 1},
            {
                "componentCode": "C25804",
                "stockCount": 500,
                "componentLibraryType": "base",
                "componentPrices": [
                    {"startNumber": 100, "productPrice": 0.002},
                    {"startNumber": 20, "productPrice": 0.004},
                    {"startNumber": None, "productPrice": None},
                    {"startNumber": "x", "productPrice": 1},
                ],
            },
        ]
        with self._patch(items):
            info = api.lookup_part("c25804")
        assert info["lcsc"] == "C25804"
        assert info["stock"] == 500
        assert info["type"] == "Basic"
        assert info["price_breaks"] == [(20, 0.004), (100, 0.002)]

    def test_not_found(self):
        with self._patch([{"componentCode": "C1"}]):
            assert api.lookup_part("C2") is None

    def test_preferred_flag(self):
        items = [{"componentCode": "C89811", "componentLibraryType": "expand", "preferredComponentFlag": True}]
        with self._patch(items):
            info = api.lookup_part("C89811")
        assert info["type"] == "Extended" and info["preferred"] is True

    def test_error_body_is_an_error_not_not_found(self):
        # JLCPCB answers some failures with HTTP 200 and this body.
        body = json.dumps({"success": False, "code": 101, "message": "unknown error", "data": None}).encode()
        resp = mock.MagicMock()
        resp.read.return_value = body
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(api, "_urlopen", return_value=resp):
            with pytest.raises(APIError, match="code 101"):
                api.lookup_part("C25804")

    def test_invalid_number(self):
        with pytest.raises(ValueError):
            api.lookup_part("xyz")
