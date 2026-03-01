"""Integration tests using real downloaded EasyEDA component data."""

import json
import os

import pytest

from kicad_jlcimport.easyeda.parser import parse_footprint_shapes, parse_symbol_shapes
from kicad_jlcimport.kicad.footprint_writer import write_footprint
from kicad_jlcimport.kicad.symbol_writer import write_symbol

TESTDATA_DIR = os.path.join(os.path.dirname(__file__), "..", "testdata")


def _extract_datastr(data: dict) -> dict:
    """Extract the dataStr dict regardless of JSON wrapper format.

    Some test data uses ``{"result": {"dataStr": ...}}`` while newer
    downloads put ``dataStr`` at the top level.
    """
    if "result" in data and "dataStr" in data["result"]:
        return data["result"]["dataStr"]
    return data["dataStr"]


def load_component_data(lcsc_id: str):
    """Load footprint and symbol data from testdata directory."""
    fp_path = os.path.join(TESTDATA_DIR, f"{lcsc_id}_footprint.json")
    sym_path = os.path.join(TESTDATA_DIR, f"{lcsc_id}_symbol.json")

    with open(fp_path) as f:
        fp_data = json.load(f)
    with open(sym_path) as f:
        sym_data = json.load(f)

    return _extract_datastr(fp_data), _extract_datastr(sym_data)


class TestC427602:
    """SOT-23-5 package - simple 5-pin SMD IC."""

    @pytest.fixture
    def component(self):
        fp_data, sym_data = load_component_data("C427602")
        return {
            "fp_shapes": fp_data["shape"],
            "fp_origin_x": fp_data["head"]["x"],
            "fp_origin_y": fp_data["head"]["y"],
            "sym_shapes": sym_data["shape"],
            "sym_origin_x": sym_data["head"]["x"],
            "sym_origin_y": sym_data["head"]["y"],
        }

    def test_footprint_pad_count(self, component):
        """Should have 5 pads for SOT-23-5."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        assert len(fp.pads) == 5

    def test_footprint_pad_numbers(self, component):
        """Pads should be numbered 1-5."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        pad_numbers = sorted([p.number for p in fp.pads])
        assert pad_numbers == ["1", "2", "3", "4", "5"]

    def test_footprint_layer_101_filtered(self, component):
        """Layer 101 (Component Marking Layer) circles should be filtered."""
        # Verify raw data has layer 101 circles
        raw_layer_101_count = sum(
            1 for s in component["fp_shapes"] if s.startswith("CIRCLE") and s.split("~")[5] == "101"
        )
        assert raw_layer_101_count > 0, "Test data should have layer 101 circles"

        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        # Only the F.Fab circle (layer 12) should remain, layer 101 filtered
        assert len(fp.circles) == 1
        assert fp.circles[0].layer == "F.Fab"

        # Verify output doesn't have extra circles
        output = write_footprint(fp, "Test", lcsc_id="C427602")
        assert output.count("(fp_circle") == 1

    def test_footprint_has_silkscreen_pin1_dot(self, component):
        """Silkscreen pin 1 indicator (SOLIDREGION on layer 3) should be imported.

        The pin 1 dot is stored as a filled arc/circle SOLIDREGION on layer 3
        (Top Silkscreen Layer), not as a CIRCLE shape.
        """
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        # Should have at least one silkscreen region for the pin 1 dot
        silkscreen_regions = [r for r in fp.regions if r.layer == "F.SilkS"]
        assert len(silkscreen_regions) >= 1, "Missing silkscreen pin 1 indicator"
        # Region must have valid polygon (at least 3 points)
        assert len(silkscreen_regions[0].points) >= 3, "Region has too few points"

        # Verify it actually appears in the output
        output = write_footprint(fp, "Test", lcsc_id="C427602")
        assert "(fp_poly" in output, "fp_poly missing from output"
        assert 'layer "F.SilkS"' in output, "Silkscreen layer missing"

    def test_footprint_has_silkscreen_tracks(self, component):
        """Should have silkscreen outline tracks."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        silk_tracks = [t for t in fp.tracks if t.layer == "F.SilkS"]
        assert len(silk_tracks) > 0

        # Verify silkscreen lines appear in output
        output = write_footprint(fp, "Test", lcsc_id="C427602")
        assert "(fp_line" in output
        assert 'layer "F.SilkS"' in output

    def test_footprint_writes_valid_kicad(self, component):
        """Should generate valid KiCad footprint format."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        output = write_footprint(fp, "SOT-23-5_Test", lcsc_id="C427602")

        assert output.startswith('(footprint "SOT-23-5_Test"')
        assert "(pad " in output
        assert 'property "LCSC" "C427602"' in output

    def test_symbol_pin_count(self, component):
        """Symbol should have 5 pins."""
        sym = parse_symbol_shapes(component["sym_shapes"], component["sym_origin_x"], component["sym_origin_y"])
        assert len(sym.pins) == 5

        # Verify pins appear in output
        output = write_symbol(sym, "Test", prefix="U", lcsc_id="C427602")
        assert output.count("(pin ") == 5

    def test_symbol_has_rectangle(self, component):
        """Symbol should have a body rectangle."""
        sym = parse_symbol_shapes(component["sym_shapes"], component["sym_origin_x"], component["sym_origin_y"])
        assert len(sym.rectangles) >= 1

        # Verify rectangle appears in output (rounded rects become polylines)
        output = write_symbol(sym, "Test", prefix="U", lcsc_id="C427602")
        assert "(rectangle" in output or "(polyline" in output


class TestC2040:
    """RP2040 QFN-56 package - complex multi-pin microcontroller."""

    @pytest.fixture
    def component(self):
        fp_data, sym_data = load_component_data("C2040")
        return {
            "fp_shapes": fp_data["shape"],
            "fp_origin_x": fp_data["head"]["x"],
            "fp_origin_y": fp_data["head"]["y"],
            "sym_shapes": sym_data["shape"],
            "sym_origin_x": sym_data["head"]["x"],
            "sym_origin_y": sym_data["head"]["y"],
        }

    def test_footprint_pad_count(self, component):
        """Should have 57 pads (56 pins + thermal pad)."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        assert len(fp.pads) == 57

        # Verify output has all 57 pads
        output = write_footprint(fp, "RP2040_Test", lcsc_id="C2040")
        assert output.count("(pad ") == 57

    def test_footprint_centered_at_origin(self, component):
        """After origin adjustment, footprint should be roughly centered."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        # Calculate centroid of pads
        avg_x = sum(p.x for p in fp.pads) / len(fp.pads)
        avg_y = sum(p.y for p in fp.pads) / len(fp.pads)
        # Should be near origin (within 1mm)
        assert abs(avg_x) < 1.0
        assert abs(avg_y) < 1.0

    def test_footprint_has_silkscreen_pin1(self, component):
        """QFN should have silkscreen pin 1 indicator circles."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        silk_circles = [c for c in fp.circles if c.layer == "F.SilkS"]
        assert len(silk_circles) >= 1, "Missing silkscreen pin 1 indicator"

        # Verify circles appear in output
        output = write_footprint(fp, "RP2040_Test", lcsc_id="C2040")
        assert "(fp_circle" in output
        assert 'layer "F.SilkS"' in output

    def test_symbol_pin_count(self, component):
        """Symbol should have 57 pins."""
        sym = parse_symbol_shapes(component["sym_shapes"], component["sym_origin_x"], component["sym_origin_y"])
        assert len(sym.pins) == 57

        # Verify output has all 57 pins
        output = write_symbol(sym, "RP2040_Test", prefix="U", lcsc_id="C2040")
        assert output.count("(pin ") == 57

    def test_symbol_writes_valid_kicad(self, component):
        """Should generate valid KiCad symbol format."""
        sym = parse_symbol_shapes(component["sym_shapes"], component["sym_origin_x"], component["sym_origin_y"])
        output = write_symbol(sym, "RP2040_Test", prefix="U", lcsc_id="C2040")

        assert '(symbol "RP2040_Test"' in output
        assert "(pin " in output
        assert 'property "LCSC" "C2040"' in output

    def test_text_shapes_parsed(self, component):
        """T~ text shapes must be parsed and written to output."""
        shapes = component["sym_shapes"]
        raw_text_count = sum(1 for s in shapes if s.startswith("T~"))
        assert raw_text_count == 2, f"Expected 2 T~ text shapes, got {raw_text_count}"

        sym = parse_symbol_shapes(shapes, component["sym_origin_x"], component["sym_origin_y"])
        assert len(sym.texts) == raw_text_count, f"Expected {raw_text_count} texts parsed, got {len(sym.texts)}"

        # Verify specific text content is present
        text_contents = [t.text for t in sym.texts]
        assert "RP2040" in text_contents
        assert "Raspberry Pi" in text_contents

        # Verify texts appear in output
        output = write_symbol(sym, "RP2040_Test", prefix="U", lcsc_id="C2040")
        assert output.count("(text ") == raw_text_count
        assert '"RP2040"' in output
        assert '"Raspberry Pi"' in output


class TestC87097:
    """DIP-16 package - tests layer 101 filtering (Component Marking Layer)."""

    @pytest.fixture
    def component(self):
        fp_data, sym_data = load_component_data("C87097")
        return {
            "fp_shapes": fp_data["shape"],
            "fp_origin_x": fp_data["head"]["x"],
            "fp_origin_y": fp_data["head"]["y"],
            "sym_shapes": sym_data["shape"],
            "sym_origin_x": sym_data["head"]["x"],
            "sym_origin_y": sym_data["head"]["y"],
        }

    def test_footprint_pad_count(self, component):
        """Should have 16 pads for DIP-16."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        assert len(fp.pads) == 16

        # Verify output has all 16 pads
        output = write_footprint(fp, "DIP-16_Test", lcsc_id="C87097")
        assert output.count("(pad ") == 16

    def test_layer_101_circles_filtered(self, component):
        """Layer 101 (Component Marking Layer) circles must be filtered.

        This is a regression test - C87097 has a circle on layer 101 that
        was incorrectly appearing inside the silkscreen outline.
        """
        # Count circles by layer in raw data
        raw_layer_101_count = sum(
            1 for s in component["fp_shapes"] if s.startswith("CIRCLE") and s.split("~")[5] == "101"
        )
        assert raw_layer_101_count > 0, "Test data should have layer 101 circles"

        # Count non-101 circles in raw data
        raw_other_circles = sum(
            1 for s in component["fp_shapes"] if s.startswith("CIRCLE") and s.split("~")[5] != "101"
        )

        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])

        # Parsed circles should only include non-101 circles
        assert len(fp.circles) == raw_other_circles, (
            f"Expected {raw_other_circles} circles, got {len(fp.circles)} (layer 101 not filtered)"
        )

        # Verify output has correct circle count
        output = write_footprint(fp, "DIP-16_Test", lcsc_id="C87097")
        assert output.count("(fp_circle") == raw_other_circles

    def test_symbol_pin_count(self, component):
        """Symbol should have 16 pins."""
        sym = parse_symbol_shapes(component["sym_shapes"], component["sym_origin_x"], component["sym_origin_y"])
        assert len(sym.pins) == 16

        # Verify output has all 16 pins
        output = write_symbol(sym, "DIP-16_Test", prefix="U", lcsc_id="C87097")
        assert output.count("(pin ") == 16

    def test_footprint_writes_valid_kicad(self, component):
        """Should generate valid KiCad footprint format."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        output = write_footprint(fp, "DIP-16_Test", lcsc_id="C87097")

        assert output.startswith('(footprint "DIP-16_Test"')
        assert output.count("(pad ") == 16
        assert 'property "LCSC" "C87097"' in output


class TestC5360901:
    """Another component for variety in testing."""

    @pytest.fixture
    def component(self):
        fp_data, sym_data = load_component_data("C5360901")
        return {
            "fp_shapes": fp_data["shape"],
            "fp_origin_x": fp_data["head"]["x"],
            "fp_origin_y": fp_data["head"]["y"],
            "sym_shapes": sym_data["shape"],
            "sym_origin_x": sym_data["head"]["x"],
            "sym_origin_y": sym_data["head"]["y"],
        }

    def test_footprint_parses(self, component):
        """Footprint should parse without errors and produce valid output."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        assert len(fp.pads) > 0

        # Verify output matches parsed data
        output = write_footprint(fp, "C5360901_Test", lcsc_id="C5360901")
        assert output.count("(pad ") == len(fp.pads)

    def test_symbol_parses(self, component):
        """Symbol should parse without errors and produce valid output."""
        sym = parse_symbol_shapes(component["sym_shapes"], component["sym_origin_x"], component["sym_origin_y"])
        assert len(sym.pins) > 0

        # Verify output matches parsed data
        output = write_symbol(sym, "C5360901_Test", prefix="U", lcsc_id="C5360901")
        assert output.count("(pin ") == len(sym.pins)

    def test_roundtrip_footprint(self, component):
        """Parse and write should produce valid output with all elements."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        output = write_footprint(fp, "C5360901_Test", lcsc_id="C5360901")

        # Verify all elements present in output
        assert "(footprint " in output
        assert output.count("(pad ") == len(fp.pads)
        assert output.count("(fp_line") >= len([t for t in fp.tracks if len(t.points) > 1])
        assert output.count("(fp_circle") == len(fp.circles)
        assert output.count("(fp_poly") == len(fp.regions)

    def test_roundtrip_symbol(self, component):
        """Parse and write should produce valid output with all elements."""
        sym = parse_symbol_shapes(component["sym_shapes"], component["sym_origin_x"], component["sym_origin_y"])
        output = write_symbol(sym, "C5360901_Test", prefix="U", lcsc_id="C5360901")

        # Verify all elements present in output
        assert "(symbol " in output
        assert output.count("(pin ") == len(sym.pins)
        # Rounded rectangles are rendered as polylines
        sharp_rects = sum(1 for r in sym.rectangles if r.corner_radius == 0)
        assert output.count("(rectangle") == sharp_rects

    def test_symbol_pin1_dot_filled(self, component):
        """Pin 1 indicator circle should be filled, not hollow."""
        sym = parse_symbol_shapes(component["sym_shapes"], component["sym_origin_x"], component["sym_origin_y"])
        # Should have one filled circle
        assert len(sym.circles) == 1
        assert sym.circles[0].filled is True

        # Output must have filled circle (outline = solid dark fill)
        output = write_symbol(sym, "C5360901_Test", prefix="U", lcsc_id="C5360901")
        assert "(circle " in output
        assert "(fill (type outline))" in output

    def test_symbol_pin_names_parsed(self, component):
        """Pin names (including numeric ones like '1') must be parsed and output."""
        sym = parse_symbol_shapes(component["sym_shapes"], component["sym_origin_x"], component["sym_origin_y"])
        # All 9 pins should have names
        for pin in sym.pins:
            assert pin.name != "", f"Pin {pin.number} has empty name"

        # Output must contain pin names
        output = write_symbol(sym, "C5360901_Test", prefix="U", lcsc_id="C5360901")
        # Check that pin names appear (name "1" for pin 1, etc)
        assert '(name "1"' in output
        assert '(name "9"' in output


class TestC2765186:
    """USB-C 16-pin connector — tests oval slot drill support.

    This component has 4 OVAL mounting tab PADs with slot drill data
    in parts[13]. Without slot parsing, these produce tiny circular
    drills instead of proper oval slots.
    """

    @pytest.fixture
    def component(self):
        fp_data, sym_data = load_component_data("C2765186")
        return {
            "fp_shapes": fp_data["shape"],
            "fp_origin_x": fp_data["head"]["x"],
            "fp_origin_y": fp_data["head"]["y"],
            "sym_shapes": sym_data["shape"],
            "sym_origin_x": sym_data["head"]["x"],
            "sym_origin_y": sym_data["head"]["y"],
        }

    def test_slot_pads_parsed(self, component):
        """Should parse exactly 4 pads with non-zero slot_length."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        slot_pads = [p for p in fp.pads if p.slot_length > 0]
        assert len(slot_pads) == 4, f"Expected 4 slot pads, got {len(slot_pads)}"

    def test_non_slot_pads_have_zero_slot_length(self, component):
        """RECT signal pads must not get slot_length from parsing."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        rect_pads = [p for p in fp.pads if p.shape == "RECT"]
        assert len(rect_pads) > 0
        for p in rect_pads:
            assert p.slot_length == 0.0, f"RECT pad {p.number} has unexpected slot_length={p.slot_length}"

    def test_oval_drill_count_in_output(self, component):
        """Output must contain exactly 4 'drill oval' entries for the slot pads."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        output = write_footprint(fp, "USB-C_Test", lcsc_id="C2765186")
        assert output.count("drill oval") == 4, f"Expected 4 'drill oval', got {output.count('drill oval')}"

    def test_footprint_writes_valid_kicad(self, component):
        """Should generate valid KiCad footprint format."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        output = write_footprint(fp, "USB-C_Test", lcsc_id="C2765186")
        assert output.startswith('(footprint "USB-C_Test"')
        assert "(pad " in output
        assert 'property "LCSC" "C2765186"' in output


class TestC34376141:
    """TOLL-8 MOSFET — tests custom polygon pad with anchor rect.

    This component has a castellated polygon pad (pad 2) that requires
    proper KiCad custom pad output with (options (clearance outline)
    (anchor rect)) and a minimal anchor size to avoid filling in the
    castellated notches.
    """

    @pytest.fixture
    def component(self):
        fp_data, sym_data = load_component_data("C34376141")
        return {
            "fp_shapes": fp_data["shape"],
            "fp_origin_x": fp_data["head"]["x"],
            "fp_origin_y": fp_data["head"]["y"],
            "sym_shapes": sym_data["shape"],
            "sym_origin_x": sym_data["head"]["x"],
            "sym_origin_y": sym_data["head"]["y"],
        }

    def test_polygon_pad_parsed(self, component):
        """Should parse exactly 1 POLYGON pad (pad 2)."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        polygon_pads = [p for p in fp.pads if p.shape == "POLYGON"]
        assert len(polygon_pads) == 1
        assert polygon_pads[0].number == "2"
        assert len(polygon_pads[0].polygon_points) > 0

    def test_custom_pad_has_anchor_rect(self, component):
        """Output must contain anchor rect options for the custom polygon pad."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        output = write_footprint(fp, "TOLL8_Test", lcsc_id="C34376141")
        assert "(options (clearance outline) (anchor rect))" in output

    def test_custom_pad_has_minimal_anchor_size(self, component):
        """Custom pad anchor size must be minimal, not the bounding box."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        output = write_footprint(fp, "TOLL8_Test", lcsc_id="C34376141")
        assert "smd custom" in output
        # The anchor size must be 0.1x0.1, not the polygon bounding box
        assert "(size 0.1 0.1)" in output

    def test_custom_pad_has_primitives(self, component):
        """Custom pad must have gr_poly primitives defining the castellated shape."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        output = write_footprint(fp, "TOLL8_Test", lcsc_id="C34376141")
        assert "(primitives" in output
        assert "(gr_poly" in output
        assert "(fill yes)" in output

    def test_footprint_has_three_pads(self, component):
        """Should have 3 pads: pin 1 (rect), pin 2 (polygon), pin 3 (rect exposed pad)."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        assert len(fp.pads) == 3
        output = write_footprint(fp, "TOLL8_Test", lcsc_id="C34376141")
        assert output.count("(pad ") == 3


class TestC558421:
    """TVS diode array (SOP-8) - tests PT path shapes (filled triangles).

    This component has complex symbol graphics including:
    - PL (polylines): cathode bars and bent Zener indicator lines
    - PT (paths): filled triangular arrowheads for diode symbols
    - P (pins): 8 pins
    - R (rectangle): body outline

    This is a regression test to ensure PT shapes are not silently dropped.
    """

    @pytest.fixture
    def component(self):
        fp_data, sym_data = load_component_data("C558421")
        return {
            "fp_shapes": fp_data["shape"],
            "fp_origin_x": fp_data["head"]["x"],
            "fp_origin_y": fp_data["head"]["y"],
            "sym_shapes": sym_data["shape"],
            "sym_origin_x": sym_data["head"]["x"],
            "sym_origin_y": sym_data["head"]["y"],
        }

    def test_raw_shape_counts(self, component):
        """Verify expected shape counts in raw data."""
        shapes = component["sym_shapes"]
        pl_count = sum(1 for s in shapes if s.startswith("PL~"))
        pt_count = sum(1 for s in shapes if s.startswith("PT~"))
        pin_count = sum(1 for s in shapes if s.startswith("P~"))
        rect_count = sum(1 for s in shapes if s.startswith("R~"))

        assert pl_count == 12, f"Expected 12 PL shapes, got {pl_count}"
        assert pt_count == 8, f"Expected 8 PT shapes, got {pt_count}"
        assert pin_count == 8, f"Expected 8 P shapes, got {pin_count}"
        assert rect_count == 1, f"Expected 1 R shape, got {rect_count}"

    def test_all_shapes_parsed(self, component):
        """Every shape in raw data must be parsed - nothing silently dropped."""
        shapes = component["sym_shapes"]
        sym = parse_symbol_shapes(shapes, component["sym_origin_x"], component["sym_origin_y"])

        # Count raw shapes
        raw_pl = sum(1 for s in shapes if s.startswith("PL~"))
        raw_pt = sum(1 for s in shapes if s.startswith("PT~"))
        raw_pins = sum(1 for s in shapes if s.startswith("P~"))
        raw_rects = sum(1 for s in shapes if s.startswith("R~"))

        # Verify all are parsed
        # PL and PT both become polylines
        assert len(sym.polylines) == raw_pl + raw_pt, (
            f"Expected {raw_pl + raw_pt} polylines (PL + PT), got {len(sym.polylines)}"
        )
        assert len(sym.pins) == raw_pins, f"Expected {raw_pins} pins, got {len(sym.pins)}"
        assert len(sym.rectangles) == raw_rects, f"Expected {raw_rects} rectangles, got {len(sym.rectangles)}"

    def test_pt_paths_are_filled(self, component):
        """PT path shapes must be parsed as filled polylines."""
        shapes = component["sym_shapes"]
        sym = parse_symbol_shapes(shapes, component["sym_origin_x"], component["sym_origin_y"])

        # Count filled polylines - should match PT count
        raw_pt_count = sum(1 for s in shapes if s.startswith("PT~"))
        filled_count = sum(1 for p in sym.polylines if p.fill)

        assert filled_count == raw_pt_count, (
            f"Expected {raw_pt_count} filled polylines from PT shapes, got {filled_count}"
        )

    def test_pt_paths_are_closed(self, component):
        """PT path shapes must be parsed as closed polylines."""
        shapes = component["sym_shapes"]
        sym = parse_symbol_shapes(shapes, component["sym_origin_x"], component["sym_origin_y"])

        raw_pt_count = sum(1 for s in shapes if s.startswith("PT~"))
        closed_count = sum(1 for p in sym.polylines if p.closed)

        assert closed_count == raw_pt_count, (
            f"Expected {raw_pt_count} closed polylines from PT shapes, got {closed_count}"
        )

    def test_all_shapes_written_to_output(self, component):
        """Every parsed shape must appear in the written output."""
        shapes = component["sym_shapes"]
        sym = parse_symbol_shapes(shapes, component["sym_origin_x"], component["sym_origin_y"])
        output = write_symbol(sym, "C558421_Test", prefix="D", lcsc_id="C558421")

        # Verify counts in output match parsed counts
        # Rounded rectangles are rendered as polylines
        rounded_rects = sum(1 for r in sym.rectangles if r.corner_radius > 0)
        sharp_rects = sum(1 for r in sym.rectangles if r.corner_radius == 0)
        assert output.count("(polyline") == len(sym.polylines) + rounded_rects, (
            f"Output has {output.count('(polyline')} polylines, "
            f"expected {len(sym.polylines)} + {rounded_rects} rounded rects"
        )
        assert output.count("(pin ") == len(sym.pins), (
            f"Output has {output.count('(pin ')} pins, expected {len(sym.pins)}"
        )
        assert output.count("(rectangle") == sharp_rects, (
            f"Output has {output.count('(rectangle')} rectangles, expected {sharp_rects}"
        )

    def test_filled_polylines_use_outline_fill(self, component):
        """Filled polylines must use 'fill (type outline)' not 'background'."""
        shapes = component["sym_shapes"]
        sym = parse_symbol_shapes(shapes, component["sym_origin_x"], component["sym_origin_y"])
        output = write_symbol(sym, "C558421_Test", prefix="D", lcsc_id="C558421")

        raw_pt_count = sum(1 for s in shapes if s.startswith("PT~"))
        outline_fills = output.count("fill (type outline)")

        assert outline_fills == raw_pt_count, (
            f"Expected {raw_pt_count} 'fill (type outline)' for PT shapes, got {outline_fills}"
        )

    def test_closed_polylines_have_repeated_first_point(self, component):
        """Closed polylines must repeat first point at end for proper rendering."""
        shapes = component["sym_shapes"]
        sym = parse_symbol_shapes(shapes, component["sym_origin_x"], component["sym_origin_y"])
        output = write_symbol(sym, "C558421_Test", prefix="D", lcsc_id="C558421")

        # Find all polylines with outline fill (the PT paths)
        import re

        # Split output into polyline blocks and check closed ones
        lines = output.split("(polyline")
        for block in lines[1:]:  # Skip first split part (before any polyline)
            if "fill (type outline)" in block:
                # Extract points
                pts_match = re.search(r"\(pts ([^)]+\))+", block)
                if pts_match:
                    pts_str = pts_match.group(0)
                    # Extract all xy coordinates
                    xy_matches = re.findall(r"\(xy ([\d.-]+) ([\d.-]+)\)", pts_str)
                    if len(xy_matches) >= 2:
                        first = xy_matches[0]
                        last = xy_matches[-1]
                        assert first == last, f"Closed polyline doesn't repeat first point: first={first}, last={last}"

    def test_footprint_pad_count(self, component):
        """Should have 8 pads for SOP-8."""
        fp = parse_footprint_shapes(component["fp_shapes"], component["fp_origin_x"], component["fp_origin_y"])
        assert len(fp.pads) == 8

        output = write_footprint(fp, "SOP-8_Test", lcsc_id="C558421")
        assert output.count("(pad ") == 8
