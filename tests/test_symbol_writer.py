"""Tests for symbol_writer.py - KiCad symbol generation."""

import re

from kicad_jlcimport.easyeda.ee_types import EECircle, EEPin, EEPolyline, EERectangle, EESymbol
from kicad_jlcimport.kicad.symbol_writer import (
    _estimate_bottom,
    _estimate_top,
    _rounded_rect_points,
    write_symbol,
    write_symbol_library,
)
from kicad_jlcimport.kicad.version import KICAD_V8, KICAD_V9


def _make_symbol(**kwargs):
    """Create an EESymbol with optional components."""
    sym = EESymbol()
    if "pins" in kwargs:
        sym.pins = kwargs["pins"]
    if "rectangles" in kwargs:
        sym.rectangles = kwargs["rectangles"]
    if "circles" in kwargs:
        sym.circles = kwargs["circles"]
    if "polylines" in kwargs:
        sym.polylines = kwargs["polylines"]
    return sym


class TestWriteSymbol:
    def test_minimal_symbol(self):
        sym = _make_symbol()
        result = write_symbol(sym, "Test")
        assert '(symbol "Test"' in result
        assert "(in_bom yes)" in result
        assert "(on_board yes)" in result

    def test_reference_property(self):
        sym = _make_symbol()
        result = write_symbol(sym, "Test", prefix="R")
        assert '(property "Reference" "R"' in result

    def test_value_property(self):
        sym = _make_symbol()
        result = write_symbol(sym, "My_Resistor")
        assert '(property "Value" "My_Resistor"' in result

    def test_reference_positioned_above_symbol(self):
        """Reference property must be positioned above the symbol body, not inside it."""
        rect = EERectangle(x=-5, y=5, width=10, height=-10)  # top=5, bottom=-5
        sym = _make_symbol(rectangles=[rect])
        result = write_symbol(sym, "Test", prefix="U")

        # Extract Reference Y position
        match = re.search(r'\(property "Reference" "U" \(at [\d.-]+ ([\d.-]+)', result)
        assert match, "Reference property not found"
        ref_y = float(match.group(1))

        # Reference should be above the symbol top (y=5), so ref_y > 5
        assert ref_y > 5, f"Reference Y={ref_y} should be above symbol top (5)"

    def test_value_positioned_below_symbol(self):
        """Value property must be positioned below the symbol body, not inside it."""
        rect = EERectangle(x=-5, y=5, width=10, height=-10)  # top=5, bottom=-5
        sym = _make_symbol(rectangles=[rect])
        result = write_symbol(sym, "TestValue", prefix="U")

        # Extract Value Y position
        match = re.search(r'\(property "Value" "TestValue" \(at [\d.-]+ ([\d.-]+)', result)
        assert match, "Value property not found"
        val_y = float(match.group(1))

        # Value should be below the symbol bottom (y=-5), so val_y < -5
        assert val_y < -5, f"Value Y={val_y} should be below symbol bottom (-5)"

    def test_footprint_property(self):
        sym = _make_symbol()
        result = write_symbol(sym, "Test", footprint_ref="JLCImport:Test")
        assert '(property "Footprint" "JLCImport:Test"' in result

    def test_metadata_properties_are_hidden(self):
        """Footprint, Datasheet, LCSC properties must have 'hide' to avoid schematic clutter."""
        sym = _make_symbol()
        result = write_symbol(sym, "Test", footprint_ref="JLC:Test", datasheet="https://x.com", lcsc_id="C1")
        for prop_name in ["Footprint", "Datasheet", "LCSC"]:
            # Find the property block (property line + effects line)
            lines = result.split("\n")
            for i, line in enumerate(lines):
                if f'"{prop_name}"' in line:
                    effects_line = lines[i + 1]
                    assert "hide" in effects_line, f'Property "{prop_name}" must be hidden'
                    break

    def test_ki_keywords_property(self):
        sym = _make_symbol()
        result = write_symbol(sym, "Test", keywords="C123 MPN SOT-23")
        assert '(property "ki_keywords" "C123 MPN SOT-23"' in result
        # ki_keywords must be hidden
        lines = result.split("\n")
        for i, line in enumerate(lines):
            if '"ki_keywords"' in line:
                assert "hide" in lines[i + 1]
                break

    def test_ki_keywords_omitted_when_empty(self):
        sym = _make_symbol()
        result = write_symbol(sym, "Test")
        assert "ki_keywords" not in result

    def test_lcsc_property(self):
        sym = _make_symbol()
        result = write_symbol(sym, "Test", lcsc_id="C123456")
        assert '(property "LCSC" "C123456"' in result

    def test_datasheet_property(self):
        sym = _make_symbol()
        result = write_symbol(sym, "Test", datasheet="https://example.com/ds.pdf")
        assert '(property "Datasheet" "https://example.com/ds.pdf"' in result

    def test_manufacturer_properties(self):
        sym = _make_symbol()
        result = write_symbol(sym, "Test", manufacturer="Acme Corp", manufacturer_part="ACM-001")
        assert '(property "Manufacturer" "Acme Corp"' in result
        assert '(property "Manufacturer Part" "ACM-001"' in result

    def test_pin_generation(self):
        pin = EEPin(number="1", name="VCC", x=0, y=0, rotation=0, length=2.54, electrical_type="power_in")
        sym = _make_symbol(pins=[pin])
        result = write_symbol(sym, "Test")
        assert "(pin power_in line" in result
        assert '(name "VCC"' in result
        assert '(number "1"' in result

    def test_pin_hidden_name(self):
        pin = EEPin(
            number="1", name="VCC", x=0, y=0, rotation=0, length=2.54, electrical_type="power_in", name_visible=False
        )
        sym = _make_symbol(pins=[pin])
        result = write_symbol(sym, "Test")
        assert "(name" in result
        # The hide should be in the name effects
        name_line = [line for line in result.split("\n") if '(name "VCC"' in line][0]
        assert "hide" in name_line

    def test_pin_hidden_number(self):
        pin = EEPin(
            number="1", name="X", x=0, y=0, rotation=0, length=2.54, electrical_type="input", number_visible=False
        )
        sym = _make_symbol(pins=[pin])
        result = write_symbol(sym, "Test")
        num_line = [line for line in result.split("\n") if '(number "1"' in line][0]
        assert "hide" in num_line

    def test_rectangle_generation(self):
        rect = EERectangle(x=-5, y=5, width=10, height=-10)
        sym = _make_symbol(rectangles=[rect])
        result = write_symbol(sym, "Test")
        assert "(rectangle" in result
        assert "(start" in result
        assert "(end" in result
        assert "(fill (type background))" in result

    def test_circle_generation(self):
        circle = EECircle(cx=0, cy=0, radius=3.0, width=0.254, layer="")
        sym = _make_symbol(circles=[circle])
        result = write_symbol(sym, "Test")
        assert "(circle" in result
        assert "(center" in result
        assert "(radius" in result

    def test_polyline_generation(self):
        poly = EEPolyline(points=[(0, 0), (1, 1), (2, 0)], closed=False, fill=False)
        sym = _make_symbol(polylines=[poly])
        result = write_symbol(sym, "Test")
        assert "(polyline" in result
        assert "(pts" in result
        assert "(fill (type none))" in result

    def test_polygon_filled(self):
        poly = EEPolyline(points=[(0, 0), (1, 1), (2, 0)], closed=True, fill=True)
        sym = _make_symbol(polylines=[poly])
        result = write_symbol(sym, "Test")
        assert "(fill (type outline))" in result

    def test_special_chars_escaped(self):
        sym = _make_symbol()
        result = write_symbol(sym, "Test", description='Has "quotes" and\nnewlines')
        assert '\\"quotes\\"' in result

    def test_unit_sub_symbol_naming(self):
        sym = _make_symbol()
        result = write_symbol(sym, "MyPart")
        # Single unit: uses _0_1
        assert '(symbol "MyPart_0_1"' in result

    def test_rounded_rect_produces_closed_polyline(self):
        """A rounded rectangle must produce a closed polyline (first point == last point).

        Without the closing fix, the polyline would have a gap between the
        last corner arc and the first corner arc.
        """
        rect = EERectangle(x=-5, y=5, width=10, height=-10, corner_radius=1.0)
        sym = _make_symbol(rectangles=[rect])
        result = write_symbol(sym, "Test")
        # Should NOT produce a native (rectangle ...) block
        assert "(rectangle" not in result
        # Should produce a (polyline ...) block instead
        assert "(polyline" in result
        # Extract all xy points from the polyline
        xy_matches = re.findall(r"\(xy ([\d.e+-]+) ([\d.e+-]+)\)", result)
        assert len(xy_matches) > 4, "Rounded rect should have many points"
        first = (float(xy_matches[0][0]), float(xy_matches[0][1]))
        last = (float(xy_matches[-1][0]), float(xy_matches[-1][1]))
        assert first == last, f"Polyline not closed: first={first}, last={last}"


class TestRoundedRectPoints:
    def test_first_and_last_point_match(self):
        """The generated points list must be closed (first == last)."""
        points = _rounded_rect_points(-5, -5, 5, 5, 1.0)
        assert points[0] == points[-1]

    def test_sufficient_point_count(self):
        """4 corners * 8 segments + closing point = 33 points minimum."""
        points = _rounded_rect_points(0, 0, 10, 10, 2.0)
        # First corner has segments+1 points, others have segments each, plus closing
        # = (8+1) + 8 + 8 + 8 + 1 = 34
        assert len(points) == 34

    def test_radius_clamped_to_half_dimension(self):
        """Radius larger than half the smallest side should be clamped."""
        # 4x10 rect with radius 5 -> clamped to 2 (half of width 4)
        points = _rounded_rect_points(0, 0, 4, 10, 5.0)
        assert points[0] == points[-1]


class TestEstimateTopBottom:
    def test_top_from_rectangles(self):
        sym = _make_symbol(rectangles=[EERectangle(x=0, y=5, width=10, height=-10)])
        top = _estimate_top(sym)
        assert top == 5  # max of y and y+height

    def test_bottom_from_rectangles(self):
        sym = _make_symbol(rectangles=[EERectangle(x=0, y=5, width=10, height=-10)])
        bottom = _estimate_bottom(sym)
        assert bottom == -5  # min of y and y+height

    def test_top_from_pins(self):
        sym = _make_symbol(
            pins=[EEPin(number="1", name="A", x=0, y=10, rotation=0, length=2.54, electrical_type="input")]
        )
        top = _estimate_top(sym)
        assert top == 10

    def test_defaults_when_empty(self):
        sym = _make_symbol()
        assert _estimate_top(sym) == 5.0
        assert _estimate_bottom(sym) == -5.0


class TestWriteSymbolLibraryVersions:
    def test_v9_library_has_generator_version(self):
        result = write_symbol_library([], kicad_version=KICAD_V9)
        assert "(version 20241209)" in result
        assert '(generator_version "1.0")' in result

    def test_v8_library_no_generator_version(self):
        result = write_symbol_library([], kicad_version=KICAD_V8)
        assert "(version 20231120)" in result
        assert "generator_version" not in result

    def test_v8_library_has_generator(self):
        result = write_symbol_library([], kicad_version=KICAD_V8)
        assert '(generator "JLCImport")' in result

    def test_default_is_v9(self):
        result = write_symbol_library([])
        assert "(version 20241209)" in result
