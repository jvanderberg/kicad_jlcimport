"""Tests for kicad_version.py - version constants and format helpers."""

import pytest

from kicad_jlcimport.kicad.version import (
    DEFAULT_KICAD_VERSION,
    KICAD_V8,
    KICAD_V9,
    KICAD_V10,
    SUPPORTED_VERSIONS,
    detect_kicad_version_from_pcbnew,
    footprint_format_version,
    has_embedded_fonts,
    has_generator_version,
    symbol_format_version,
    validate_kicad_version,
)


class TestConstants:
    def test_kicad_v8(self):
        assert KICAD_V8 == 8

    def test_kicad_v9(self):
        assert KICAD_V9 == 9

    def test_default_version(self):
        assert DEFAULT_KICAD_VERSION == KICAD_V9

    def test_kicad_v10(self):
        assert KICAD_V10 == 10

    def test_supported_versions(self):
        assert KICAD_V8 in SUPPORTED_VERSIONS
        assert KICAD_V9 in SUPPORTED_VERSIONS
        assert KICAD_V10 in SUPPORTED_VERSIONS


class TestValidateKicadVersion:
    def test_valid_v8(self):
        assert validate_kicad_version(8) == 8

    def test_valid_v9(self):
        assert validate_kicad_version(9) == 9

    def test_invalid_version(self):
        with pytest.raises(ValueError, match="Unsupported KiCad version"):
            validate_kicad_version(7)

    def test_valid_v10(self):
        assert validate_kicad_version(10) == 10

    def test_invalid_version_11(self):
        with pytest.raises(ValueError):
            validate_kicad_version(11)


class TestSymbolFormatVersion:
    def test_v8(self):
        assert symbol_format_version(KICAD_V8) == "20231120"

    def test_v9(self):
        assert symbol_format_version(KICAD_V9) == "20241209"

    def test_v10(self):
        assert symbol_format_version(KICAD_V10) == "20241209"

    def test_default(self):
        assert symbol_format_version() == "20241209"


class TestFootprintFormatVersion:
    def test_v8(self):
        assert footprint_format_version(KICAD_V8) == "20240108"

    def test_v9(self):
        assert footprint_format_version(KICAD_V9) == "20241229"

    def test_v10(self):
        assert footprint_format_version(KICAD_V10) == "20241229"

    def test_default(self):
        assert footprint_format_version() == "20241229"


class TestHasGeneratorVersion:
    def test_v8(self):
        assert has_generator_version(KICAD_V8) is False

    def test_v9(self):
        assert has_generator_version(KICAD_V9) is True

    def test_v10(self):
        assert has_generator_version(KICAD_V10) is True


class TestHasEmbeddedFonts:
    def test_v8(self):
        assert has_embedded_fonts(KICAD_V8) is False

    def test_v9(self):
        assert has_embedded_fonts(KICAD_V9) is True

    def test_v10(self):
        assert has_embedded_fonts(KICAD_V10) is True


class TestDetectKicadVersionFromPcbnew:
    def test_fallback_without_pcbnew(self):
        # pcbnew is not available in test env, should return default
        result = detect_kicad_version_from_pcbnew()
        assert result == DEFAULT_KICAD_VERSION

    def test_detect_v8(self, monkeypatch):
        import types

        fake_pcbnew = types.ModuleType("pcbnew")
        fake_pcbnew.Version = lambda: "8.0.5"
        monkeypatch.setitem(__import__("sys").modules, "pcbnew", fake_pcbnew)
        result = detect_kicad_version_from_pcbnew()
        assert result == 8

    def test_detect_v9(self, monkeypatch):
        import types

        fake_pcbnew = types.ModuleType("pcbnew")
        fake_pcbnew.Version = lambda: "(9.0.1)"
        monkeypatch.setitem(__import__("sys").modules, "pcbnew", fake_pcbnew)
        result = detect_kicad_version_from_pcbnew()
        assert result == 9

    def test_detect_unsupported_returns_default(self, monkeypatch):
        import types

        fake_pcbnew = types.ModuleType("pcbnew")
        fake_pcbnew.Version = lambda: "7.0.0"
        monkeypatch.setitem(__import__("sys").modules, "pcbnew", fake_pcbnew)
        result = detect_kicad_version_from_pcbnew()
        assert result == DEFAULT_KICAD_VERSION
