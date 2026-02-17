"""Tests for gui/symbol_renderer.py — SVG-to-bitmap rendering."""

import pytest


class TestRenderSvgBitmap:
    """render_svg_bitmap requires wx.App."""

    @pytest.fixture(autouse=True)
    def _needs_wx(self):
        try:
            import wx

            if not wx.App.Get():
                self._app = wx.App()
        except (Exception, SystemExit):
            pytest.skip("wx not available")

    def test_valid_svg(self):
        from kicad_jlcimport.gui.symbol_renderer import render_svg_bitmap

        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20"/></svg>'
        bmp = render_svg_bitmap(svg, size=160)
        assert bmp is not None
        assert bmp.GetWidth() == 160
        assert bmp.GetHeight() == 160

    def test_empty_string(self):
        from kicad_jlcimport.gui.symbol_renderer import render_svg_bitmap

        assert render_svg_bitmap("", size=100) is None

    def test_invalid_svg(self):
        from kicad_jlcimport.gui.symbol_renderer import render_svg_bitmap

        assert render_svg_bitmap("not svg at all", size=100) is None

    def test_custom_size(self):
        from kicad_jlcimport.gui.symbol_renderer import render_svg_bitmap

        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><circle r="5"/></svg>'
        bmp = render_svg_bitmap(svg, size=200)
        assert bmp is not None
        assert bmp.GetWidth() == 200
        assert bmp.GetHeight() == 200
