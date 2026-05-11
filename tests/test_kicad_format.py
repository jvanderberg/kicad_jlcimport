"""Tests for _kicad_format.py - shared formatting utilities."""

from kicad_jlcimport.kicad._format import escape_sexpr, fmt_float, fmt_geometry, gen_uuid


class TestGenUuid:
    def test_returns_string(self):
        result = gen_uuid()
        assert isinstance(result, str)

    def test_uuid_format(self):
        result = gen_uuid()
        # UUID4 format: 8-4-4-4-12
        parts = result.split("-")
        assert len(parts) == 5
        assert len(parts[0]) == 8
        assert len(parts[1]) == 4
        assert len(parts[2]) == 4
        assert len(parts[3]) == 4
        assert len(parts[4]) == 12

    def test_unique(self):
        ids = {gen_uuid() for _ in range(100)}
        assert len(ids) == 100


class TestFmtFloat:
    def test_integer_value(self):
        assert fmt_float(5.0) == "5"

    def test_negative_integer(self):
        assert fmt_float(-3.0) == "-3"

    def test_zero(self):
        assert fmt_float(0.0) == "0"

    def test_decimal_value(self):
        result = fmt_float(1.5)
        assert result == "1.5"

    def test_strips_trailing_zeros(self):
        result = fmt_float(1.100000)
        assert result == "1.1"

    def test_small_decimal(self):
        result = fmt_float(0.001)
        assert result == "0.001"

    def test_precision_limit(self):
        # Should have at most 6 decimal places
        result = fmt_float(1.123456789)
        assert len(result.split(".")[1]) <= 6

    def test_plain_float_does_not_snap_grid_noise(self):
        assert fmt_float(2.800102) == "2.800102"

    def test_large_integer(self):
        assert fmt_float(1000.0) == "1000"

    def test_very_large_float(self):
        # Should not use integer format for very large values
        result = fmt_float(1e11 + 0.5)
        assert "." in result

    def test_nan_returns_zero(self):
        assert fmt_float(float("nan")) == "0"

    def test_inf_returns_zero(self):
        assert fmt_float(float("inf")) == "0"

    def test_negative_inf_returns_zero(self):
        assert fmt_float(float("-inf")) == "0"


class TestFmtGeometry:
    def test_snaps_easyeda_grid_noise(self):
        # Issue #102: footprint coordinates/dimensions can carry tiny EasyEDA
        # decimal residuals that make KiCad treat otherwise grid-aligned pads as
        # off-grid.
        assert fmt_geometry(2.800102) == "2.8"
        assert fmt_geometry(0.240005) == "0.24"
        assert fmt_geometry(-0.974981) == "-0.975"
        assert fmt_geometry(0.000076) == "0"

    def test_preserves_meaningful_sub_micron_values(self):
        assert fmt_geometry(1.937512) == "1.937512"

    def test_preserves_exact_imperial_values(self):
        assert fmt_geometry(0.0254) == "0.0254"  # 1 mil
        assert fmt_geometry(0.0508) == "0.0508"  # 2 mil
        assert fmt_geometry(0.0762) == "0.0762"  # 3 mil

    def test_nan_returns_zero(self):
        assert fmt_geometry(float("nan")) == "0"


class TestEscapeSexpr:
    def test_no_escaping_needed(self):
        assert escape_sexpr("hello") == "hello"

    def test_escape_backslash(self):
        assert escape_sexpr("a\\b") == "a\\\\b"

    def test_escape_quote(self):
        assert escape_sexpr('a"b') == 'a\\"b'

    def test_escape_newline(self):
        assert escape_sexpr("a\nb") == "a b"

    def test_combined_escaping(self):
        assert escape_sexpr('line1\npath\\to\\"file"') == 'line1 path\\\\to\\\\\\"file\\"'

    def test_empty_string(self):
        assert escape_sexpr("") == ""
