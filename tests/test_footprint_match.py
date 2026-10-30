"""Tests for kicad/footprint_match.py: suggesting a KiCad footprint only when its pads match JLC's."""

import math

import pytest

from kicad_jlcimport import importer
from kicad_jlcimport.kicad import footprint_match as fm
from kicad_jlcimport.kicad.footprint_match import (
    POLARITY_NA,
    POLARITY_REVERSED,
    POLARITY_SAME,
    POLARITY_UNKNOWN,
    FootprintMatch,
    find_matching_footprint,
    package_tokens,
    part_number_chunks,
    read_footprint_file,
)
from kicad_jlcimport.production.models import PadRecord

# SOT-363 / SC-70-6: two rows of three pads, 0.65 mm pitch, 1.9 mm apart.
SOT363 = [
    ("1", -0.95, -0.65, 0.65, 0.4),
    ("2", -0.95, 0.0, 0.65, 0.4),
    ("3", -0.95, 0.65, 0.65, 0.4),
    ("4", 0.95, 0.65, 0.65, 0.4),
    ("5", 0.95, 0.0, 0.65, 0.4),
    ("6", 0.95, -0.65, 0.65, 0.4),
]
# SC-74-6: same layout at 0.95 mm pitch.
SC74 = [(n, x * 1.15, y * 0.95 / 0.65, w, h) for n, x, y, w, h in SOT363]
SOT23_3 = [("1", -1.0, -0.95, 0.9, 0.8), ("2", -1.0, 0.95, 0.9, 0.8), ("3", 1.0, 0.0, 0.9, 0.8)]
CHIP_0603 = [("1", -0.775, 0.0, 0.9, 0.95), ("2", 0.775, 0.0, 0.9, 0.95)]
DIP8 = [(str(i + 1), -3.81 if i < 4 else 3.81, -3.81 + 2.54 * (i if i < 4 else 7 - i), 1.6, 1.6) for i in range(8)]


def _pad_line(num, x, y, w, h, drill=0.0):
    if drill:
        return f'  (pad "{num}" thru_hole circle (at {x} {y}) (size {w} {h}) (drill {drill}) (layers "*.Cu" "*.Mask"))'
    return f'  (pad "{num}" smd roundrect (at {x} {y}) (size {w} {h}) (layers "F.Cu" "F.Paste" "F.Mask"))'


def write_mod(lib_dir, name, pads, drill=0.0):
    """Write a minimal KiCad footprint file with *pads* (number, x, y, w, h)."""
    lib_dir.mkdir(parents=True, exist_ok=True)
    body = "\n".join(_pad_line(*p, drill=drill) for p in pads)
    text = f'(footprint "{name}"\n  (version 20240108)\n  (layer "F.Cu")\n{body}\n)\n'
    (lib_dir / f"{name}.kicad_mod").write_text(text)


def jlc(pads, angle=0.0, dx=0.0, dy=0.0, drill=0.0):
    """JLC's pads: *pads* turned by *angle* degrees and shifted, as PadRecords."""
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    out = []
    for num, x, y, w, h in pads:
        if round(angle) % 180 == 90:
            w, h = h, w
        out.append(PadRecord(number=num, x=x * c - y * s + dx, y=x * s + y * c + dy, width=w, height=h, drill=drill))
    return out


@pytest.fixture
def libs(tmp_path):
    """(nickname, path) list for the libraries written under tmp_path."""
    entries = []

    def add(nickname, footprints):
        lib_dir = tmp_path / f"{nickname}.pretty"
        for name, pads, *drill in footprints:
            write_mod(lib_dir, name, pads, drill=drill[0] if drill else 0.0)
        entries.append((nickname, str(lib_dir)))
        return entries

    return add


# ---------------------------------------------------------------------------
# Name tokens
# ---------------------------------------------------------------------------


class TestPackageTokens:
    def test_family_and_pin_count(self):
        assert {"SOT23", "SOT236"} <= package_tokens("SOT-23-6_L2.9-W1.6-P0.95-LS2.8-BR")

    def test_aliases_reach_kicad_names(self):
        assert "SOT236" in package_tokens("TSOT-26_L2.9-W1.6-P0.95")
        assert "SOIC8" in package_tokens("SOP-8_L4.9-W3.9-P1.27")
        assert "BGA324" in package_tokens("NFBGA-324_L15.0-W15.0")
        assert "QFN24" in package_tokens("VQFN-24_L4.0-W4.0")

    def test_chip_size_and_standalone(self):
        assert "0603" in package_tokens("LED0603-RD")
        assert "SMA" in package_tokens("SMA(DO-214AC)")

    def test_decimal_is_not_a_pin_count(self):
        assert package_tokens("QFN_L4.0-W4.0") == frozenset()

    def test_letter_suffix_kept(self):
        assert "SOD523F" in package_tokens("SOD-523F_L1.2-W0.8")

    def test_part_number_chunks(self):
        assert "B4BPHKS" in part_number_chunks("CONN-TH_B4B-PH-K-S")
        assert part_number_chunks("SOT-23") == frozenset()


class TestNameConflicts:
    def test_pitch(self):
        assert fm._name_conflicts(["SC-70-6_L2.0-W1.3-P0.65"], "SC-74-6_1.55x2.9mm_P0.95mm")
        assert not fm._name_conflicts(["SIP-3_L4.1-W1.5-P1.3"], "Diodes_SIP-3_4.1x1.5mm_P1.27mm")

    def test_body(self):
        assert fm._name_conflicts(["QFN-24_L4.0-W4.0-P0.50"], "QFN-24-1EP_5x5mm_P0.5mm")
        assert not fm._name_conflicts(["QFN-24_L4.0-W4.0-P0.50"], "QFN-24-1EP_4x4mm_P0.5mm_EP2.6x2.6mm")

    def test_exposed_pad(self):
        assert fm._name_conflicts(["QFN-40_L6.0-W6.0-EP2.9"], "QFN-40-1EP_6x6mm_P0.5mm_EP4.6x4.6mm")
        assert not fm._name_conflicts(["QFN-40_L6.0-W6.0-EP2.9"], "VQFN-40-1EP_6x6mm_P0.5mm_EP2.9x2.9mm")


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


class TestPadGroupsAndGeometry:
    def test_sot363_gets_same_pitch_footprint_not_sot23_or_sc74(self, libs):
        libraries = libs(
            "Package_TO_SOT_SMD",
            [("SOT-23-3", SOT23_3), ("SC-74-6_1.55x2.9mm_P0.95mm", SC74), ("SOT-363_SC-70-6", SOT363)],
        )
        match = find_matching_footprint(
            jlc(SOT363, angle=90), ["SOT-363_L2.0-W1.3-P0.65-LS2.1-BR"], prefix="U", libraries=libraries
        )
        assert match is not None
        assert match.ref == "Package_TO_SOT_SMD:SOT-363_SC-70-6"
        assert match.pad_groups == 6
        assert match.rms < 1e-6
        assert match.polarity == POLARITY_NA
        assert match.recommended

    def test_wrong_pad_count_only_gives_nothing(self, libs):
        libraries = libs("Package_TO_SOT_SMD", [("SOT-23-3", SOT23_3)])
        assert find_matching_footprint(jlc(SOT363), ["SOT-363"], libraries=libraries) is None

    def test_same_pad_count_wrong_pitch_gives_nothing(self, libs):
        # Named SC-70-6 but laid out at 0.95 mm pitch: the name gets it a look, the pads reject it.
        libraries = libs("Package_TO_SOT_SMD", [("SC-70-6_Odd", SC74)])
        assert find_matching_footprint(jlc(SOT363), ["SC-70-6"], libraries=libraries) is None

    def test_mirrored_numbering_gives_nothing(self, libs):
        mirrored = [(n, -x, y, w, h) for n, x, y, w, h in SOT363]
        libraries = libs("Package_TO_SOT_SMD", [("SOT-363_SC-70-6", mirrored)])
        assert find_matching_footprint(jlc(SOT363), ["SOT-363"], libraries=libraries) is None

    def test_different_pad_numbers_give_nothing(self, libs):
        renumbered = [("1", -1.0, -0.95, 0.9, 0.8), ("2", -1.0, 0.95, 0.9, 0.8), ("EP", 1.0, 0.0, 0.9, 0.8)]
        libraries = libs("Package_TO_SOT_SMD", [("SOT-23-3", renumbered)])
        assert find_matching_footprint(jlc(SOT23_3), ["SOT-23-3"], libraries=libraries) is None

    def test_residual_above_high_rms_gives_nothing(self, libs):
        wide = [(n, x * 1.1, y, w, h) for n, x, y, w, h in SOT363]
        libraries = libs("Package_TO_SOT_SMD", [("SOT-363_SC-70-6", wide)])
        assert find_matching_footprint(jlc(SOT363), ["SOT-363"], libraries=libraries) is None

    def test_pad_sizes_far_apart_give_nothing(self, libs):
        big = [(n, x, y, w * 3, h * 3) for n, x, y, w, h in SOT363]
        libraries = libs("Package_TO_SOT_SMD", [("SOT-363_SC-70-6", big)])
        assert find_matching_footprint(jlc(SOT363), ["SOT-363"], libraries=libraries) is None

    def test_through_hole_part_does_not_get_smd_footprint(self, libs):
        libraries = libs("Package_DIP", [("DIP-8_W7.62mm_SMDSocket", DIP8), ("DIP-8_W7.62mm", DIP8, 0.8)])
        match = find_matching_footprint(jlc(DIP8, drill=0.8), ["DIP-8_L10.0-W6.5-P2.54"], libraries=libraries)
        assert match is not None
        assert match.ref == "Package_DIP:DIP-8_W7.62mm"

    def test_offset_origin_still_fits(self, libs):
        libraries = libs("Package_TO_SOT_SMD", [("SOT-363_SC-70-6", SOT363)])
        match = find_matching_footprint(jlc(SOT363, dx=0.95, dy=0.65), ["SOT-363"], libraries=libraries)
        assert match is not None


class TestRanking:
    def test_standard_variant_beats_handsoldering(self, libs):
        libraries = libs("Package_TO_SOT_SMD", [("SOT-363_SC-70-6_HandSoldering", SOT363), ("SOT-363_SC-70-6", SOT363)])
        match = find_matching_footprint(jlc(SOT363), ["SOT-363"], libraries=libraries)
        assert match.ref == "Package_TO_SOT_SMD:SOT-363_SC-70-6"

    def test_handsoldering_used_when_only_it_fits(self, libs):
        wide = [(n, x * 1.2, y, w, h) for n, x, y, w, h in SOT363]
        libraries = libs("Package_TO_SOT_SMD", [("SOT-363_SC-70-6_HandSoldering", SOT363), ("SOT-363_SC-70-6", wide)])
        match = find_matching_footprint(jlc(SOT363), ["SOT-363"], libraries=libraries)
        assert match.ref == "Package_TO_SOT_SMD:SOT-363_SC-70-6_HandSoldering"

    def test_lower_residual_wins(self, libs):
        near = [(n, x * 1.02, y, w, h) for n, x, y, w, h in SOT363]
        libraries = libs("Package_TO_SOT_SMD", [("SOT-363", near), ("SOT-363_SC-70-6", SOT363)])
        match = find_matching_footprint(jlc(SOT363), ["SOT-363"], libraries=libraries)
        assert match.ref == "Package_TO_SOT_SMD:SOT-363_SC-70-6"

    def test_part_number_in_name_finds_connector(self, libs):
        header = [("1", 0.0, 0.0, 1.2, 1.7), ("2", 2.0, 0.0, 1.2, 1.7), ("3", 4.0, 0.0, 1.2, 1.7)]
        libraries = libs("Connector_JST", [("JST_PH_B3B-PH-K_1x03_P2.00mm_Vertical", header, 0.75)])
        match = find_matching_footprint(jlc(header, drill=0.75), ["CONN-TH_B3B-PH-K-S"], libraries=libraries)
        assert match.ref == "Connector_JST:JST_PH_B3B-PH-K_1x03_P2.00mm_Vertical"

    def test_no_package_tokens_gives_nothing(self, libs):
        libraries = libs("Package_TO_SOT_SMD", [("SOT-363_SC-70-6", SOT363)])
        assert find_matching_footprint(jlc(SOT363), ["IND-SMD_L4.0-W4.0"], libraries=libraries) is None

    def test_unreadable_footprint_is_skipped(self, libs, tmp_path):
        libraries = libs("Package_TO_SOT_SMD", [("SOT-363_SC-70-6", SOT363)])
        (tmp_path / "Package_TO_SOT_SMD.pretty" / "SOT-363.kicad_mod").write_text("(footprint (pad")
        match = find_matching_footprint(jlc(SOT363), ["SOT-363"], libraries=libraries)
        assert match.ref == "Package_TO_SOT_SMD:SOT-363_SC-70-6"

    def test_missing_library_is_skipped(self, libs, tmp_path):
        libraries = libs("Package_TO_SOT_SMD", [("SOT-363_SC-70-6", SOT363)])
        libraries.insert(0, ("Gone", str(tmp_path / "Gone.pretty")))
        assert find_matching_footprint(jlc(SOT363), ["SOT-363"], libraries=libraries) is not None


class TestTwoPadParts:
    def _libraries(self, libs):
        libs("Capacitor_SMD", [("C_0603_1608Metric", CHIP_0603)])
        libs("LED_SMD", [("LED_0603_1608Metric", CHIP_0603)])
        return libs("Resistor_SMD", [("R_0603_1608Metric", CHIP_0603)])

    def test_designator_picks_the_kind_of_footprint(self, libs):
        libraries = self._libraries(libs)
        match = find_matching_footprint(jlc(CHIP_0603), ["R0603"], prefix="R", libraries=libraries)
        assert match.ref == "Resistor_SMD:R_0603_1608Metric"
        match = find_matching_footprint(jlc(CHIP_0603), ["C0603"], prefix="C", libraries=libraries)
        assert match.ref == "Capacitor_SMD:C_0603_1608Metric"

    def test_unknown_designator_gives_nothing(self, libs):
        libraries = self._libraries(libs)
        assert find_matching_footprint(jlc(CHIP_0603), ["0603"], prefix="U", libraries=libraries) is None

    def test_led_anode_on_pad_1_is_reversed(self, libs):
        libraries = self._libraries(libs)
        match = find_matching_footprint(
            jlc(CHIP_0603), ["LED0603-RD"], prefix="LED", marks={"1": "+", "2": "-"}, libraries=libraries
        )
        assert match.ref == "LED_SMD:LED_0603_1608Metric"
        assert match.polarity == POLARITY_REVERSED
        assert not match.recommended
        assert "polarity reversed" in match.describe()

    def test_led_cathode_on_pad_1_lines_up(self, libs):
        libraries = self._libraries(libs)
        match = find_matching_footprint(
            jlc(CHIP_0603), ["LED0603-RD"], prefix="LED", marks={"1": "-", "2": "+"}, libraries=libraries
        )
        assert match.polarity == POLARITY_SAME
        assert match.recommended

    def test_led_polarity_from_pin_names(self, libs):
        libraries = self._libraries(libs)
        match = find_matching_footprint(
            jlc(CHIP_0603), ["LED0603"], prefix="LED", pin_names={"1": "A", "2": "K"}, libraries=libraries
        )
        assert match.polarity == POLARITY_REVERSED

    def test_led_without_marks_or_names_is_unknown(self, libs):
        libraries = self._libraries(libs)
        match = find_matching_footprint(jlc(CHIP_0603), ["LED0603"], prefix="LED", libraries=libraries)
        assert match.polarity == POLARITY_UNKNOWN
        assert not match.recommended
        assert "can't be checked" in match.describe()

    def test_ak_footprint_name_states_pad_order(self, libs):
        libraries = libs("LED_SMD", [("LED_0603_1608Metric_AK", CHIP_0603)])
        match = find_matching_footprint(
            jlc(CHIP_0603), ["LED0603"], prefix="LED", marks={"1": "+", "2": "-"}, libraries=libraries
        )
        assert match.polarity == POLARITY_SAME

    def test_polarized_part_does_not_get_unpolarized_footprint(self, libs):
        libraries = libs("Capacitor_SMD", [("C_1206_3216Metric", CHIP_0603)])
        marks = {"1": "+", "2": "-"}
        assert find_matching_footprint(jlc(CHIP_0603), ["CASE-A_3216"], "C", marks=marks, libraries=libraries) is None
        libraries = libs("Capacitor_Tantalum_SMD", [("CP_EIA-3216-18_Kemet-A", CHIP_0603)])
        match = find_matching_footprint(jlc(CHIP_0603), ["CASE-A_3216"], "C", marks=marks, libraries=libraries)
        assert match.ref == "Capacitor_Tantalum_SMD:CP_EIA-3216-18_Kemet-A"
        assert match.polarity == POLARITY_SAME

    def test_unpolarized_footprint_preferred_for_unpolarized_part(self, libs):
        libs("Capacitor_Tantalum_SMD", [("CP_EIA-1608-08_AVX-J", CHIP_0603)])
        libraries = libs("Capacitor_SMD", [("C_0603_1608Metric", CHIP_0603)])
        match = find_matching_footprint(jlc(CHIP_0603), ["C0603"], prefix="C", libraries=libraries)
        assert match.ref == "Capacitor_SMD:C_0603_1608Metric"


class TestLibraryTables:
    def test_project_fp_lib_table(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "project"
        write_mod(project_dir / "Local.pretty", "SOT-363_SC-70-6", SOT363)
        (project_dir / "fp-lib-table").write_text(
            "(fp_lib_table\n  (version 7)\n"
            '  (lib (name "Local")(type "KiCad")(uri "${KIPRJMOD}/Local.pretty")(options "")(descr ""))\n)\n'
        )
        empty_global = tmp_path / "empty_global"
        empty_global.mkdir()
        monkeypatch.setattr("kicad_jlcimport.kicad.library.get_global_config_dir", lambda _v=9: str(empty_global))

        match = find_matching_footprint(jlc(SOT363), ["SOT-363"], project_dir=str(project_dir), kicad_version=9)
        assert match.ref == "Local:SOT-363_SC-70-6"

    def test_global_fp_lib_table_with_env_var(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        fp_root = tmp_path / "fp_root"
        write_mod(fp_root / "Package_TO_SOT_SMD.pretty", "SOT-363_SC-70-6", SOT363)
        (config_dir / "fp-lib-table").write_text(
            "(fp_lib_table\n  (version 7)\n"
            '  (lib (name "Package_TO_SOT_SMD")(type "KiCad")'
            '(uri "${KICAD9_FOOTPRINT_DIR}/Package_TO_SOT_SMD.pretty")(options "")(descr ""))\n)\n'
        )
        monkeypatch.setenv("KICAD9_FOOTPRINT_DIR", str(fp_root))
        monkeypatch.setattr("kicad_jlcimport.kicad.library.get_global_config_dir", lambda _v=9: str(config_dir))

        match = find_matching_footprint(jlc(SOT363), ["SOT-363"], kicad_version=9)
        assert match.ref == "Package_TO_SOT_SMD:SOT-363_SC-70-6"

    def test_no_libraries_gives_nothing(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "fp-lib-table").write_text("(fp_lib_table\n  (version 7)\n)\n")
        monkeypatch.setattr("kicad_jlcimport.kicad.library.get_global_config_dir", lambda _v=9: str(config_dir))
        assert find_matching_footprint(jlc(SOT363), ["SOT-363"], project_dir=str(tmp_path), kicad_version=9) is None


class TestReadFootprintFile:
    def test_reads_pad_sizes_and_drills(self, tmp_path):
        write_mod(tmp_path / "L.pretty", "DIP-8_W7.62mm", DIP8, drill=0.8)
        record = read_footprint_file(str(tmp_path / "L.pretty" / "DIP-8_W7.62mm.kicad_mod"))
        assert record.footprint == "DIP-8_W7.62mm"
        assert len(record.pads) == 8
        assert record.pads[0].width == pytest.approx(1.6)
        assert record.pads[0].drill == pytest.approx(0.8)

    def test_not_a_footprint(self, tmp_path):
        path = tmp_path / "x.kicad_mod"
        path.write_text("(kicad_pcb (version 1))")
        assert read_footprint_file(str(path)) is None
        assert read_footprint_file(str(tmp_path / "missing.kicad_mod")) is None


class TestDescribe:
    def test_plain(self):
        text = FootprintMatch(ref="L:F", rms=0.012, max_error=0.02, pad_groups=6, size_error=0.08).describe()
        assert text == "6 pads, centres within 0.012 mm RMS of JLC's footprint, pad sizes within 8%"

    def test_polarity_same(self):
        match = FootprintMatch(ref="L:F", rms=0.0, max_error=0.0, pad_groups=2, size_error=0.0, polarity="same")
        assert match.describe().endswith("pad 1 has the same polarity")


# ---------------------------------------------------------------------------
# Importer: the suggestion is built from the component data already fetched
# ---------------------------------------------------------------------------

_MM_PER_UNIT = 0.254  # EasyEDA footprint units are 10 mil


def _easyeda_pad(num, x, y, w, h):
    u = _MM_PER_UNIT
    return f"PAD~RECT~{x / u + 400}~{y / u + 300}~{w / u}~{h / u}~1~~{num}~0~~0~gge{num}~0~~Y~0~0~0.2~"


class TestImporterSuggestion:
    def _comp(self, pads, package):
        return {
            "lcsc_id": "C7842",
            "package": package,
            "prefix": "U",
            "manufacturer_part": "SN74LVC1G58DCKR",
            "symbol_data_list": [],
            "footprint_data": {
                "title": package,
                "dataStr": {"head": {"x": 400, "y": 300}, "shape": [_easyeda_pad(*p) for p in pads]},
            },
        }

    def test_suggestion_from_easyeda_shapes(self, libs, monkeypatch):
        libraries = libs(
            "Package_TO_SOT_SMD", [("SOT-23-3", SOT23_3), ("SC-74-6_1.55x2.9mm", SC74), ("SOT-363_SC-70-6", SOT363)]
        )
        monkeypatch.setattr(fm, "_iter_footprint_libraries", lambda *a, **k: libraries)
        comp = self._comp(SOT363, "SOT-363_L2.0-W1.3-P0.65-LS2.1-BR")
        match = importer._suggest_footprint(comp, "", "", 10)
        assert match.ref == "Package_TO_SOT_SMD:SOT-363_SC-70-6"

    def test_no_suggestion_when_nothing_fits(self, libs, monkeypatch):
        libraries = libs("Package_TO_SOT_SMD", [("SOT-23-3", SOT23_3), ("SC-74-6_1.55x2.9mm", SC74)])
        monkeypatch.setattr(fm, "_iter_footprint_libraries", lambda *a, **k: libraries)
        comp = self._comp(SOT363, "SOT-363_L2.0-W1.3-P0.65-LS2.1-BR")
        assert importer._suggest_footprint(comp, "SC-70-6", "", 10) is None


# ---------------------------------------------------------------------------
# UIs: the fit is shown and decides the default choice
# ---------------------------------------------------------------------------

try:
    import wx  # noqa: F401

    _has_wx = True
except ImportError:
    _has_wx = False

try:
    import textual  # noqa: F401

    _has_textual = True
except ImportError:
    _has_textual = False


@pytest.mark.skipif(not _has_wx, reason="wxPython not installed")
class TestDialogFitText:
    def _dialog(self):
        from types import SimpleNamespace

        return SimpleNamespace(_footprint_candidate_ref="Lib:Fp", _footprint_candidate_fit="6 pads, centres within")

    def test_suggestion_shows_its_fit(self):
        from kicad_jlcimport.dialog import MetadataEditDialog

        assert MetadataEditDialog._fit_text(self._dialog(), "Lib:Fp") == "Matches JLC's pads: 6 pads, centres within"

    def test_browsed_footprint_is_not_checked(self):
        from kicad_jlcimport.dialog import MetadataEditDialog

        assert "not checked" in MetadataEditDialog._fit_text(self._dialog(), "Other:Fp")

    def test_no_suggestion(self):
        from kicad_jlcimport.dialog import MetadataEditDialog

        assert "No KiCad library footprint" in MetadataEditDialog._fit_text(self._dialog(), "")


@pytest.mark.skipif(not _has_textual, reason="textual not installed")
class TestTuiReuseCheckbox:
    def _run(self, metadata, toggle=False):
        import asyncio

        from textual.app import App
        from textual.widgets import Checkbox

        from kicad_jlcimport.tui.app import MetadataEditScreen

        results = []

        class Host(App):
            def on_mount(self):
                self.push_screen(MetadataEditScreen(metadata), results.append)

        async def drive():
            app = Host()
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                boxes = list(screen.query(Checkbox))
                if toggle:
                    boxes[0].value = not boxes[0].value
                state = [b.value for b in boxes]
                screen._accept()
                await pilot.pause()
                return state

        return asyncio.run(drive()), results[0]

    def test_recommended_match_is_ticked(self):
        state, result = self._run(
            {
                "__footprint_candidate_ref": "Lib:Fp",
                "__footprint_candidate_fit": "fits",
                "__footprint_candidate_recommended": True,
            }
        )
        assert state == [True]
        assert result["__reuse_existing_footprint"] is True

    def test_polarity_caveat_leaves_it_unticked(self):
        state, result = self._run(
            {
                "__footprint_candidate_ref": "Lib:Fp",
                "__footprint_candidate_fit": "x",
                "__footprint_candidate_recommended": False,
            }
        )
        assert state == [False]
        assert result["__reuse_existing_footprint"] is False

    def test_no_suggestion_no_checkbox(self):
        state, result = self._run({"description": "d"})
        assert state == []
        assert "__reuse_existing_footprint" not in result
