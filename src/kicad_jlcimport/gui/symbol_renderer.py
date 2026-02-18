"""Render an EasyEDA SVG string as a wx.Bitmap for the detail panel preview."""

from __future__ import annotations

import re

import wx

try:
    import wx.svg

    _has_svg = True
except (ImportError, ModuleNotFoundError):
    _has_svg = False

# Pattern to extract layerid CSS rules from EasyEDA's <style> block.
# Example: *[layerid="1"] {stroke:#FF0000;fill:#FF0000;}
_CSS_RULE_RE = re.compile(
    r'\*\[layerid="(\d+)"\]\s*\{([^}]+)\}',
)


def _inline_layer_styles(svg: str) -> str:
    """Inline EasyEDA's CSS layerid rules as style attributes.

    nanosvg (used by wx.svg) doesn't support <style> blocks, so we
    extract the CSS rules and apply them directly to matching elements.
    """
    rules: dict[str, str] = {}
    for m in _CSS_RULE_RE.finditer(svg):
        rules[m.group(1)] = m.group(2).strip().rstrip(";")

    if not rules:
        return svg

    # Remove the <style> block so nanosvg doesn't choke on it
    svg = re.sub(r"<style[^>]*>.*?</style>", "", svg, flags=re.DOTALL)

    # Add inline style to elements with layerid="N"
    def _add_style(m: re.Match) -> str:
        tag_content = m.group(0)
        lid_match = re.search(r'layerid="(\d+)"', tag_content)
        if lid_match and lid_match.group(1) in rules:
            css = rules[lid_match.group(1)]
            # Respect fill="none" / stroke="none" attributes — the
            # original CSS has *[fill="none"]{fill:none} rules that
            # override layerid colours for outline-only elements.
            if 'fill="none"' in tag_content:
                css = re.sub(r"fill:[^;]+", "fill:none", css)
            if 'stroke="none"' in tag_content:
                css = re.sub(r"stroke:[^;]+", "stroke:none", css)
            # Append to existing style or add new
            if 'style="' in tag_content:
                return tag_content.replace('style="', f'style="{css};', 1)
            # Insert style before /> (self-closing) or >
            if tag_content.endswith("/>"):
                return tag_content[:-2] + f' style="{css}"/>'
            return tag_content[:-1] + f' style="{css}">'
        return tag_content

    return re.sub(r"<[^/][^>]*layerid=[^>]*>", _add_style, svg)


def has_svg_support() -> bool:
    """Return True if the platform can render SVG via wx.svg."""
    return _has_svg


def render_svg_bitmap(svg_string: str, size: int = 160) -> wx.Bitmap | None:
    """Render an SVG string to a wx.Bitmap, scaled to fit *size* x *size*.

    Returns None if the SVG cannot be parsed or wx.svg is unavailable
    (e.g. KiCad 9.x on Windows ships without a compiled wx.svg._nanosvg).
    """
    if not _has_svg:
        return None

    svg_string = _inline_layer_styles(svg_string)

    try:
        img = wx.svg.SVGimage.CreateFromBytes(svg_string.encode("utf-8"))
    except Exception:
        return None
    if img.width <= 0 or img.height <= 0:
        return None

    return img.ConvertToScaledBitmap(wx.Size(size, size))
