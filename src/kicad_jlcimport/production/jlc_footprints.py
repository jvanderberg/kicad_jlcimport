"""Fetch JLC's own (EasyEDA) footprint for an LCSC part, reduced to pad centres."""

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from ..easyeda import api
from ..easyeda.parser import parse_footprint_shapes, parse_symbol_shapes
from .models import PadRecord
from .polarity import point_symmetric

_MARK_LAYER = "12"  # EasyEDA layer JLC draws polarity marks (+ / -) on
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_BAR_ASPECT = 2.5  # a mark stroke is at least this much longer than wide
_NEAREST_MARGIN = 1.5  # a mark belongs to a pad this much nearer than the next pad


@dataclass
class JlcFootprint:
    """JLC's footprint for a part: pads in footprint-local mm (Y down, origin at JLC's origin).

    ``pin_names`` maps pad number to JLC's symbol pin name (e.g. ``"2": "K"``).
    It is only fetched where it can decide polarity (see :func:`needs_pin_names`);
    ``symbol_error`` says why it is missing when that fetch failed.
    ``pad_marks`` maps pad number to the ``+`` / ``-`` mark JLC's footprint
    draws beside it (see :func:`polarity_marks`).
    """

    lcsc: str
    title: str = ""
    pads: List[PadRecord] = field(default_factory=list)
    pin_names: Dict[str, str] = field(default_factory=dict)
    symbol_error: str = ""
    pad_marks: Dict[str, str] = field(default_factory=dict)
    symbol_title: str = ""  # JLC's symbol title, the name JLCImport gives an imported part


def _data_shapes(data: Dict[str, Any]):
    data_str = data.get("dataStr", {}) or {}
    head = data_str.get("head", {}) or {}
    return data_str.get("shape", []) or [], head.get("x", 0), head.get("y", 0)


def _mark_strokes(shapes: List[str]) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Strokes drawn on EasyEDA's polarity layer (12), each as a line segment in EasyEDA units."""
    strokes = []
    for shape in shapes:
        parts = shape.split("~")
        if parts[0] == "SOLIDREGION" and len(parts) > 3 and parts[1] == _MARK_LAYER:
            nums = [float(v) for v in _NUMBER_RE.findall(parts[3])]
            xs, ys = nums[0::2], nums[1::2]
            if not 3 <= len(xs) <= 6:
                continue
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            w, h = x1 - x0, y1 - y0
            # A bar: long and thin.  Triangles and other marks don't say + or -.
            if min(w, h) <= 0 or max(w, h) < _BAR_ASPECT * min(w, h):
                continue
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            strokes.append(((x0, cy), (x1, cy)) if w > h else ((cx, y0), (cx, y1)))
        elif parts[0] == "TRACK" and len(parts) > 4 and parts[2] == _MARK_LAYER:
            try:
                pts = [float(v) for v in parts[4].split()]
            except ValueError:
                continue
            strokes.extend(((pts[i - 2], pts[i - 1]), (pts[i], pts[i + 1])) for i in range(2, len(pts) - 1, 2))
    return strokes


def _marks(shapes: List[str]) -> List[Tuple[str, float, float]]:
    """(sign, x, y) of each mark: two crossing strokes make a ``+``, a lone stroke a ``-``."""
    strokes = []
    for (ax, ay), (bx, by) in _mark_strokes(shapes):
        strokes.append(((ax + bx) / 2, (ay + by) / 2, math.hypot(bx - ax, by - ay), math.atan2(by - ay, bx - ax)))
    used = set()
    marks = []
    for i, (x, y, length, angle) in enumerate(strokes):
        if i in used:
            continue
        for j in range(i + 1, len(strokes)):
            x2, y2, length2, angle2 = strokes[j]
            crossing = abs(math.cos(angle - angle2)) < 0.2 and math.hypot(x - x2, y - y2) < 0.25 * max(length, length2)
            if j not in used and crossing and 0.4 < length / length2 < 2.5:
                used.update((i, j))
                marks.append(("+", (x + x2) / 2, (y + y2) / 2))
                break
    marks.extend(("-", x, y) for i, (x, y, _, _) in enumerate(strokes) if i not in used)
    return marks


def polarity_marks(shapes: List[str]) -> Dict[str, str]:
    """Pad number -> ``+`` / ``-`` from the polarity marks drawn beside the pads, else {}.

    JLC's footprints draw a ``+`` beside the anode / positive pad and a
    ``-`` beside the cathode / negative pad on layer 12; JLC's placement
    preview shows these marks.  Only one ``+`` and one ``-``, each clearly
    nearer one pad than any other, count.
    """
    pads: Dict[str, Tuple[float, float]] = {}
    for shape in shapes:
        parts = shape.split("~")
        if parts[0] == "PAD" and len(parts) > 8 and parts[8].strip():
            try:
                pads.setdefault(parts[8].strip(), (float(parts[2]), float(parts[3])))
            except ValueError:
                continue
    marks = _marks(shapes)
    if len(pads) < 2 or sorted(sign for sign, _, _ in marks) != ["+", "-"]:
        return {}
    roles: Dict[str, str] = {}
    for sign, x, y in marks:
        dist = sorted((math.hypot(px - x, py - y), num) for num, (px, py) in pads.items())
        if dist[0][0] * _NEAREST_MARGIN > dist[1][0]:
            return {}
        roles[dist[0][1]] = sign
    return roles if len(roles) == 2 else {}


def jlc_footprint_from_data(lcsc: str, fp_data: Dict[str, Any]) -> JlcFootprint:
    """Build a :class:`JlcFootprint` from an EasyEDA component-data result."""
    shapes, origin_x, origin_y = _data_shapes(fp_data)
    fp = parse_footprint_shapes(shapes, origin_x, origin_y)
    pads = [PadRecord(number=p.number, x=p.x, y=p.y) for p in fp.pads]
    return JlcFootprint(lcsc=lcsc, title=fp_data.get("title", ""), pads=pads, pad_marks=polarity_marks(shapes))


def pin_names_from_data(symbol_data: List[Dict[str, Any]]) -> Dict[str, str]:
    """Pad number -> pin name from EasyEDA symbol documents (first name wins)."""
    names: Dict[str, str] = {}
    for data in symbol_data:
        for pin in parse_symbol_shapes(*_data_shapes(data)).pins:
            if pin.number and pin.name:
                names.setdefault(pin.number.strip(), pin.name.strip())
    return names


def needs_pin_names(pads: List[PadRecord]) -> bool:
    """True for two-pad parts, and three-pad parts whose pads are point-symmetric (a centred thermal pad)."""
    count = len({p.number for p in pads if p.number})
    return count == 2 or (count == 3 and point_symmetric(pads))


def fetch_jlc_footprint(lcsc: str) -> JlcFootprint:
    """Download and parse the EasyEDA footprint linked to *lcsc*.

    Fetches the footprint document only, plus the symbol for parts whose pin
    names can give polarity (diodes, LEDs, polarized capacitors).
    Raises :class:`api.APIError` on network or lookup failure.  A failed
    symbol fetch is recorded in ``symbol_error`` (the report warns about it)
    rather than failing the whole footprint.
    """
    uuids = api.fetch_component_uuids(lcsc)
    fp_data = api.fetch_component_data(uuids[-1]["component_uuid"])
    jlc = jlc_footprint_from_data(lcsc, fp_data)
    if needs_pin_names(jlc.pads) and len(uuids) > 1:
        try:
            symbols = [api.fetch_component_data(u["component_uuid"]) for u in uuids[:-1]]
            jlc.pin_names = pin_names_from_data(symbols)
            jlc.symbol_title = next((d.get("title", "") for d in symbols if d.get("title")), "")
        except api.SSLCertError:
            raise
        except Exception as e:
            jlc.symbol_error = str(e) or type(e).__name__
    return jlc
