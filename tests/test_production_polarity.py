"""Tests for polarity pairing, position matching and JLC pin-name fetching."""

import math
from unittest import mock

import pytest

from kicad_jlcimport.easyeda import api
from kicad_jlcimport.production import polarity as pol
from kicad_jlcimport.production.cache import KeyedCache
from kicad_jlcimport.production.conventions import BOTTOM, TOP
from kicad_jlcimport.production.jlc_footprints import (
    JlcFootprint,
    fetch_jlc_footprint,
    needs_pin_names,
    pin_names_from_data,
)
from kicad_jlcimport.production.kicad_pcb import read_board_text
from kicad_jlcimport.production.models import FootprintRecord, PadRecord
from kicad_jlcimport.production.placement import SOLVED, compute_placement
from kicad_jlcimport.production.report import POLARITY_CHECK_TEXT, WARNING, run_check
from kicad_jlcimport.production.solver import (
    HIGH,
    MATCHED_BY_POSITION,
    MEDIUM,
    POLARITY_BY_CONVENTION,
    POLARITY_BY_FOOTPRINT_NAME,
    POLARITY_BY_NUMBERING,
    POLARITY_BY_PIN_NAMES,
    POLARITY_JLC_UNNAMED,
    POLARITY_NAMES_UNAVAILABLE,
    POLARITY_SWAPPED,
    POLARITY_UNRESOLVED,
    match_pads_by_position,
)


def placed(pads, x, y, theta=0.0):
    """Board pads for a top-side footprint at (x, y, theta) whose library pads are *pads*."""
    c, s = math.cos(math.radians(theta)), math.sin(math.radians(theta))
    return [PadRecord(p.number, x + p.x * c + p.y * s, y - p.x * s + p.y * c) for p in pads]


def caches(lcsc, jlc_fp):
    part = {"lcsc": lcsc, "stock": 1000, "type": "Basic", "price": 0.01, "price_breaks": [(1, 0.01)]}
    return KeyedCache(lambda _l: part), KeyedCache(lambda _l: jlc_fp)


# JLC's C2286 (0603 LED): pad 2 = K on the left, pad 1 = A on the right.
JLC_LED_PADS = [PadRecord("2", -0.75, 0.0), PadRecord("1", 0.75, 0.0)]
JLC_LED_NAMES = {"1": "A", "2": "K"}
# KiCad LED footprint: pad 1 (cathode by KiCad convention) on the left.
KICAD_LED_PADS = [PadRecord("1", -0.75, 0.0), PadRecord("2", 0.75, 0.0)]


def led(ref="D1", theta=0.0, functions=None, numbers=("1", "2"), side=TOP):
    pads = placed([PadRecord(n, p.x, p.y) for n, p in zip(numbers, KICAD_LED_PADS)], 10.0, 10.0, theta)
    for pad, fn in zip(pads, functions or ("", "")):
        pad.function = fn
    return FootprintRecord(
        reference=ref,
        value="Red",
        footprint="LED_0603",
        x=10.0,
        y=10.0,
        orientation=theta,
        side=side,
        pads=pads,
        properties={"Reference": ref, "LCSC": "C2286"},
    )


class TestPinRole:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("K", (pol.NEGATIVE, pol.DIODE)),
            ("k_1", (pol.NEGATIVE, pol.DIODE)),
            ("A_2", (pol.POSITIVE, pol.DIODE)),
            ("Cathode", (pol.NEGATIVE, pol.DIODE)),
            ("+", (pol.POSITIVE, pol.POLARIZED)),
            ("-", (pol.NEGATIVE, pol.POLARIZED)),
            ("NEG", (pol.NEGATIVE, pol.POLARIZED)),
            ("1_1", None),
            ("", None),
            ("A/K", None),  # contradictory
            ("VIN", None),
        ],
    )
    def test_roles(self, name, expected):
        assert pol.pin_role(name) == expected


class TestMatchPolarity:
    def board(self, functions=("", ""), numbers=("1", "2")):
        return led(functions=functions, numbers=numbers).pads

    def test_board_names_pair_by_role(self):
        m = pol.match_polarity(self.board(("K_1", "A_2")), JLC_LED_PADS, JLC_LED_NAMES)
        assert m.source == pol.BY_NAMES and m.swapped
        assert [(p.number, p.x) for p in m.jlc_pads] == [("1", -0.75), ("2", 0.75)]

    def test_kicad_convention_when_board_has_no_names(self):
        m = pol.match_polarity(self.board(), JLC_LED_PADS, JLC_LED_NAMES)
        assert m.source == pol.BY_CONVENTION and m.kind == pol.DIODE and m.swapped
        assert [p.number for p in m.jlc_pads] == ["1", "2"]

    def test_polarized_capacitor_convention_pad1_positive(self):
        jlc = [PadRecord("1", -1.0, 0.0), PadRecord("2", 1.0, 0.0)]
        m = pol.match_polarity(self.board(), jlc, {"1": "+", "2": "-"})
        assert m.source == pol.BY_CONVENTION and m.kind == pol.POLARIZED and not m.swapped

    def test_unresolved_when_board_numbers_unknown(self):
        m = pol.match_polarity(self.board(numbers=("A", "C")), JLC_LED_PADS, JLC_LED_NAMES)
        assert m.source == pol.UNRESOLVED
        assert [p.number for p in m.jlc_pads] == ["2", "1"]

    def test_board_letters_with_names_are_not_called_swapped(self):
        m = pol.match_polarity(self.board(("K", "A"), numbers=("A", "C")), JLC_LED_PADS, JLC_LED_NAMES)
        assert m.source == pol.BY_NAMES and not m.swapped
        assert [p.number for p in m.jlc_pads] == ["A", "C"]

    @pytest.mark.parametrize(
        "names",
        [{}, {"1": "1", "2": "2"}, {"1": "A", "2": "A"}, {"1": "A", "2": "-"}],
    )
    def test_not_polarized(self, names):
        m = pol.match_polarity(self.board(), JLC_LED_PADS, names)
        assert m.source == pol.NOT_POLARIZED
        assert [p.number for p in m.jlc_pads] == ["2", "1"]

    def test_asymmetric_third_pad_ignored(self):
        # Geometry already fixes which pad is which, so numbers decide.
        jlc = JLC_LED_PADS + [PadRecord("3", 0.0, 1.0)]
        assert pol.match_polarity(self.board(), jlc, JLC_LED_NAMES).source == pol.NOT_POLARIZED

    def test_jlc_centred_thermal_pad_still_pairs_by_role(self):
        jlc = JLC_LED_PADS + [PadRecord("3", 0.0, 0.0)]
        m = pol.match_polarity(self.board(), jlc, dict(JLC_LED_NAMES, **{"3": "EP"}))
        assert m.source == pol.BY_CONVENTION and m.swapped
        assert [p.number for p in m.jlc_pads] == ["1", "2", "3"]

    def test_board_centred_thermal_pad_pairs_by_pin_function(self):
        board = self.board(("K", "A")) + [PadRecord("3", 10.0, 10.0)]
        m = pol.match_polarity(board, JLC_LED_PADS, JLC_LED_NAMES)
        assert m.source == pol.BY_NAMES
        assert [p.number for p in m.jlc_pads] == ["1", "2"]

    def test_board_with_extra_pads_and_no_names_is_unresolved(self):
        # KiCad's pad-1 convention is only assumed for footprints with pads 1 and 2 alone.
        board = self.board() + [PadRecord("3", 10.0, 10.0)]
        assert pol.match_polarity(board, JLC_LED_PADS, JLC_LED_NAMES).source == pol.UNRESOLVED

    def test_dual_diode_common_pin_is_not_two_terminal(self):
        jlc = [PadRecord("1", -1.0, 0.0), PadRecord("3", 0.0, 0.0), PadRecord("2", 1.0, 0.0)]
        board = self.board(("A", "K")) + [PadRecord("3", 10.0, 10.0)]
        m = pol.match_polarity(board, jlc, {"1": "A", "2": "K", "3": "K/A"})
        assert m.source == pol.NOT_POLARIZED

    def test_unpolarized_jlc_pad_does_not_take_a_polarized_number(self):
        # Board: K on pad 1 (left), A on pad 3 (right), pad 2 in the middle.
        board = [PadRecord("1", 9.25, 10.0, "K"), PadRecord("3", 10.75, 10.0, "A"), PadRecord("2", 10.0, 10.0)]
        jlc = JLC_LED_PADS + [PadRecord("3", 0.0, 0.0)]
        m = pol.match_polarity(board, jlc, JLC_LED_NAMES)
        assert [p.number for p in m.jlc_pads] == ["1", "3", "jlc:3"]

    def test_board_polarized_but_jlc_names_missing(self):
        m = pol.match_polarity(self.board(("K", "A")), JLC_LED_PADS, {"1": "1", "2": "2"})
        assert m.source == pol.JLC_UNNAMED
        assert [p.number for p in m.jlc_pads] == ["2", "1"]

    def test_thermal_pad_part_without_jlc_names_is_flagged(self):
        jlc = JLC_LED_PADS + [PadRecord("3", 0.0, 0.0)]
        board = self.board(("K", "A")) + [PadRecord("3", 10.0, 10.0)]
        names = {"1": "1", "2": "2", "3": "3"}
        assert pol.match_polarity(board, jlc, names).source == pol.JLC_UNNAMED
        assert pol.match_polarity(board, jlc, {}, names_error="gone").source == pol.NAMES_UNAVAILABLE
        # Not polarized on the board (a 3-pin resonator, say): left alone.
        plain = self.board() + [PadRecord("3", 10.0, 10.0)]
        assert pol.match_polarity(plain, jlc, names, footprint_name="Resonator_SMD").source == pol.NOT_POLARIZED
        assert pol.match_polarity(plain, jlc, {}, names_error="gone").source == pol.NOT_POLARIZED

    @pytest.mark.parametrize("name", ["LED_0603_1608Metric", "Diode_SMD:D_SOD-123", "CP_Elec_4x5.4", "LED_X_AK"])
    def test_polarized_footprint_names(self, name):
        assert pol.board_says_polarized(self.board(), name)

    def test_resistor_is_not_polarized(self):
        assert not pol.board_says_polarized(self.board(), "R_0603_1608Metric")

    def test_board_pad_numbers_as_names(self):
        m = pol.match_polarity(self.board(numbers=("K", "A")), JLC_LED_PADS, JLC_LED_NAMES)
        assert m.source == pol.BY_NAMES
        assert [(p.number, p.x) for p in m.jlc_pads] == [("K", -0.75), ("A", 0.75)]

    def test_names_unavailable(self):
        m = pol.match_polarity(self.board(), JLC_LED_PADS, {}, names_error="timed out")
        assert m.source == pol.NAMES_UNAVAILABLE
        assert [p.number for p in m.jlc_pads] == ["2", "1"]

    def test_a_c_pin_names_are_not_read_as_anode_cathode(self):
        # Checked against JLCPCB's preview and the zoo boards: on JLC parts named
        # A/C, reading C as cathode put every one of them on backwards.
        m = pol.match_polarity(self.board(), JLC_LED_PADS, {"1": "A", "2": "C"})
        assert m.source == pol.NOT_POLARIZED

    def test_footprint_name_roles(self):
        assert pol.footprint_name_roles("LED_PLCC-2_3.4x3.0mm_AK") == {"1": pol.POSITIVE, "2": pol.NEGATIVE}
        assert pol.footprint_name_roles("D_KA_Thing") == {"1": pol.NEGATIVE, "2": pol.POSITIVE}
        assert pol.footprint_name_roles("LED_0603_1608Metric") == {}
        assert pol.footprint_name_roles("BAKED") == {}

    def test_contradictory_board_names_fall_back_to_convention(self):
        m = pol.match_polarity(self.board(("K", "K")), JLC_LED_PADS, JLC_LED_NAMES)
        assert m.source == pol.BY_CONVENTION


class TestPolarityPlacement:
    def jlc(self, names=None):
        return JlcFootprint("C2286", pads=JLC_LED_PADS, pin_names=JLC_LED_NAMES if names is None else names)

    @pytest.mark.parametrize("theta", [0.0, 90.0, 180.0, 270.0])
    def test_led_follows_cathode_not_pad_number(self, theta):
        pl = compute_placement(led(theta=theta), self.jlc())
        assert pl.source == SOLVED
        # JLC's cathode is on the left at 0°, like KiCad's pad 1, so the CPL angle equals KiCad's.
        assert pl.rotation == pytest.approx(theta)
        assert pl.confidence == MEDIUM
        assert POLARITY_BY_CONVENTION in pl.fit.flags and POLARITY_SWAPPED in pl.fit.flags
        assert POLARITY_BY_NUMBERING not in pl.fit.flags

    def test_board_pin_names_solve_but_still_ask_for_a_check(self):
        # JLC's own pin names can contradict how JLC places a part, so polarized
        # results are never HIGH and the report asks for a look in the preview.
        fp = led(functions=("K_1", "A_2"))
        pl = compute_placement(fp, self.jlc())
        assert pl.rotation == pytest.approx(0.0)
        assert pl.confidence == MEDIUM
        assert POLARITY_BY_PIN_NAMES in pl.fit.flags
        report = run_check([fp, led("D2", functions=("K", "A"))], 1, *caches("C2286", self.jlc()))
        assert report.rows[0].severity == WARNING
        assert f"[WARNING] D1, D2: {POLARITY_CHECK_TEXT}" in report.issue_lines()

    def test_ak_footprint_name_sets_pad_order(self):
        # KiCad's LED_PLCC-2_..._AK: pad 1 is the anode, against the pad-1 = cathode convention.
        fp = led()
        fp.footprint = "LED_PLCC-2_3.4x3.0mm_AK"
        pl = compute_placement(fp, self.jlc())
        assert POLARITY_BY_FOOTPRINT_NAME in pl.fit.flags
        assert pl.rotation == pytest.approx(180.0)  # JLC's cathode (left) lands on pad 2 (right)
        fp.footprint = "LED_X_KA"
        assert compute_placement(fp, self.jlc()).rotation == pytest.approx(0.0)

    def test_without_jlc_names_pairs_by_number_and_asks_for_a_check(self):
        # The board footprint (LED_0603) is polarized, so pairing by number is a guess.
        fp = led()
        pl = compute_placement(fp, self.jlc(names={}))
        assert pl.rotation == pytest.approx(180.0)
        assert pl.confidence == MEDIUM
        assert POLARITY_JLC_UNNAMED in pl.fit.flags
        report = run_check([fp], 1, *caches("C2286", self.jlc(names={})))
        assert f"[WARNING] D1: {POLARITY_CHECK_TEXT}" in report.issue_lines()

    def test_unpolarized_part_without_names_stays_high(self):
        fp = led()
        fp.footprint = "R_0603_1608Metric"
        pl = compute_placement(fp, self.jlc(names={}))
        assert pl.confidence == HIGH
        assert POLARITY_BY_NUMBERING in pl.fit.flags

    def test_led_with_thermal_pad_follows_cathode(self):
        fp = led(functions=("K", "A"))
        fp.pads.append(PadRecord("3", 10.0, 10.0))
        pl = compute_placement(fp, self.jlc())
        assert pl.rotation == pytest.approx(0.0)
        assert POLARITY_BY_PIN_NAMES in pl.fit.flags

    def test_unresolved_is_medium_and_a_report_warning(self):
        # Pads numbered A/C on both sides: numbers pair up, but nothing says
        # which board pad is the cathode (no pin names, not KiCad's 1/2).
        jlc = JlcFootprint(
            "C2286",
            pads=[PadRecord("A", 0.75, 0.0), PadRecord("C", -0.75, 0.0)],
            pin_names={"A": "A", "C": "K"},
        )
        fp = led(numbers=("C", "A"))
        pl = compute_placement(fp, jlc)
        assert pl.source == SOLVED and pl.confidence == MEDIUM
        assert POLARITY_UNRESOLVED in pl.fit.flags

        part_cache, fp_cache = caches("C2286", jlc)
        report = run_check([fp], 1, part_cache, fp_cache)
        assert report.rows[0].severity == WARNING

    def test_board_pads_named_k_and_a_are_solved(self):
        pl = compute_placement(led(numbers=("K", "A")), self.jlc())
        assert pl.source == SOLVED and pl.rotation == pytest.approx(0.0) and pl.confidence == MEDIUM

    def test_symbol_fetch_failure_warns_instead_of_trusting_numbers(self):
        jlc = JlcFootprint("C2286", pads=JLC_LED_PADS, symbol_error="timed out")
        pl = compute_placement(led(), jlc)
        assert pl.confidence == MEDIUM
        assert POLARITY_NAMES_UNAVAILABLE in pl.fit.flags and POLARITY_BY_NUMBERING not in pl.fit.flags
        report = run_check([led()], 1, *caches("C2286", jlc))
        assert report.rows[0].severity == WARNING

    def test_swapped_note_shown_on_high_confidence_rows(self):
        part_cache, fp_cache = caches("C2286", self.jlc())
        report = run_check([led(functions=("K", "A"))], 1, part_cache, fp_cache)
        assert any("other way round" in i.message for i in report.rows[0].issues)


# USB-C-like pattern: a row of signal pads plus asymmetric shell pads, so it
# only fits one way round.
USB_JLC = [PadRecord(n, x, -2.375) for n, x in [("A1B12", -3.2), ("A4B9", -2.4), ("B8", -1.75), ("A5", -1.25)]] + [
    PadRecord("13", -4.325, -1.825),
    PadRecord("14", 4.325, -1.825),
    PadRecord("13", -4.325, 2.375),
    PadRecord("14", 4.325, 2.375),
]
USB_BOARD_LOCAL = [PadRecord(str(i), p.x, p.y) for i, p in enumerate(USB_JLC[:4], start=1)] + USB_JLC[4:]


def usb(theta=0.0, side=TOP, local=None):
    return FootprintRecord(
        reference="USB1",
        value="USB-C",
        footprint="USB_C",
        x=50.0,
        y=40.0,
        orientation=theta,
        side=side,
        pads=placed(local or USB_BOARD_LOCAL, 50.0, 40.0, theta),
        properties={"Reference": "USB1", "LCSC": "C2765186"},
    )


class TestMatchByPosition:
    @pytest.mark.parametrize("theta", [0.0, 90.0, 270.0, 45.0])
    def test_renumbered_footprint_is_solved(self, theta):
        pl = compute_placement(usb(theta), JlcFootprint("C2765186", pads=USB_JLC))
        assert pl.source == SOLVED
        assert pl.rotation == pytest.approx(theta)
        assert (pl.x, pl.y) == (pytest.approx(50.0), pytest.approx(-40.0))
        assert pl.confidence == MEDIUM
        assert MATCHED_BY_POSITION in pl.fit.flags

    def test_renamed_pads_take_board_numbers(self):
        renumbered = match_pads_by_position(usb().pads, USB_JLC, TOP, kicad_orientation=0.0)
        assert [p.number for p in renumbered] == ["1", "2", "3", "4", "13", "14", "13", "14"]

    def test_symmetric_pattern_is_ambiguous(self):
        square = [PadRecord(n, x, y) for n, x, y in [("a", -1, -1), ("b", 1, -1), ("c", 1, 1), ("d", -1, 1)]]
        board = placed([PadRecord(str(i), p.x, p.y) for i, p in enumerate(square, 1)], 0, 0)
        assert match_pads_by_position(board, square, TOP) is None

    def test_pad_count_mismatch(self):
        assert match_pads_by_position(usb().pads, USB_JLC[:-1], TOP) is None

    def test_too_few_pads(self):
        assert match_pads_by_position(led().pads, JLC_LED_PADS, TOP) is None

    def test_different_geometry_rejected(self):
        stretched = [PadRecord(p.number, p.x * 1.5, p.y) for p in USB_JLC]
        assert match_pads_by_position(usb().pads, stretched, TOP) is None
        pl = compute_placement(usb(), JlcFootprint("C2765186", pads=stretched))
        assert pl.source != SOLVED

    def test_bottom_side(self):
        mirrored_local = [PadRecord(p.number, p.x, -p.y) for p in USB_BOARD_LOCAL]
        fp = usb(side=BOTTOM, local=mirrored_local)
        pl = compute_placement(fp, JlcFootprint("C2765186", pads=USB_JLC))
        assert pl.source == SOLVED and MATCHED_BY_POSITION in pl.fit.flags


SYMBOL_PINS = [
    "P~show~0~2~-20~-5~180~gge28~0^^-20~-5^^M -20 -5 h 15~#800^^0~-1~-5~0~K~start~~~#800^^0~-9~-9~0~2~end~~~#800",
    "P~show~0~1~20~-5~0~gge37~0^^20~-5^^M 20 -5 h -15~#800^^0~1~-5~0~A~end~~~#800^^0~9~-9~0~1~start~~~#800",
]
TWO_PAD_FP = {
    "title": "LED0603",
    "dataStr": {
        "head": {"x": 4000, "y": 3000},
        "shape": ["PAD~RECT~3997~3000~3~3~1~~2~0~~0~g1", "PAD~RECT~4003~3000~3~3~1~~1~0~~0~g2"],
    },
}
SYMBOL = {"dataStr": {"head": {"x": 0, "y": -5}, "shape": SYMBOL_PINS}}


class TestJlcPinNames:
    def test_pin_names_from_symbol(self):
        assert pin_names_from_data([SYMBOL]) == {"2": "K", "1": "A"}

    def fetch(self, side_effect):
        uuids = [{"component_uuid": "s"}, {"component_uuid": "f"}]
        with mock.patch.object(api, "fetch_component_uuids", return_value=uuids):
            with mock.patch.object(api, "fetch_component_data", side_effect=side_effect) as fetch_data:
                return fetch_jlc_footprint("C2286"), fetch_data

    def test_two_pad_part_fetches_symbol(self):
        jlc, fetch_data = self.fetch(lambda uuid: TWO_PAD_FP if uuid == "f" else SYMBOL)
        assert jlc.pin_names == {"2": "K", "1": "A"}
        assert [c.args[0] for c in fetch_data.call_args_list] == ["f", "s"]

    def test_which_parts_need_pin_names(self):
        assert needs_pin_names(JLC_LED_PADS)
        assert needs_pin_names(JLC_LED_PADS + [PadRecord("3", 0.0, 0.0)])
        sot23 = [PadRecord("1", -0.95, 1.0), PadRecord("2", 0.95, 1.0), PadRecord("3", 0.0, -1.0)]
        assert not needs_pin_names(sot23)
        assert not needs_pin_names([PadRecord(str(i), i, 0.0) for i in range(1, 5)])

    def test_symbol_failure_is_not_fatal(self):
        def data(uuid):
            if uuid == "s":
                raise api.APIError("gone")
            return TWO_PAD_FP

        jlc, _ = self.fetch(data)
        assert jlc.pin_names == {} and len(jlc.pads) == 2
        assert jlc.symbol_error == "gone"

    def test_symbol_ssl_error_propagates(self):
        def data(uuid):
            if uuid == "s":
                raise api.SSLCertError("bad cert")
            return TWO_PAD_FP

        with pytest.raises(api.SSLCertError):
            self.fetch(data)


BOARD = """(kicad_pcb (version 20240108)
  (footprint "LED:LED_0603" (layer "F.Cu") (at 10 10)
    (property "Reference" "D1")
    (pad "1" smd rect (at -0.75 0) (size 0.8 0.8) (layers "F.Cu") (net 1 "N1") (pinfunction "K"))
    (pad "2" smd rect (at 0.75 0) (size 0.8 0.8) (layers "F.Cu"))
  )
)"""


def test_kicad_pcb_reader_reads_pin_function():
    (fp,) = read_board_text(BOARD)
    assert [(p.number, p.function) for p in fp.pads] == [("1", "K"), ("2", "")]
