"""Fetch JLC's own (EasyEDA) footprint for an LCSC part, reduced to pad centres."""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..easyeda import api
from ..easyeda.parser import parse_footprint_shapes
from .models import PadRecord


@dataclass
class JlcFootprint:
    """JLC's footprint for a part: pads in footprint-local mm (Y down, origin at JLC's origin)."""

    lcsc: str
    title: str = ""
    pads: List[PadRecord] = field(default_factory=list)


def jlc_footprint_from_data(lcsc: str, fp_data: Dict[str, Any]) -> JlcFootprint:
    """Build a :class:`JlcFootprint` from an EasyEDA component-data result."""
    data_str = fp_data.get("dataStr", {}) or {}
    head = data_str.get("head", {}) or {}
    fp = parse_footprint_shapes(data_str.get("shape", []) or [], head.get("x", 0), head.get("y", 0))
    pads = [PadRecord(number=p.number, x=p.x, y=p.y) for p in fp.pads]
    return JlcFootprint(lcsc=lcsc, title=fp_data.get("title", ""), pads=pads)


def fetch_jlc_footprint(lcsc: str) -> JlcFootprint:
    """Download and parse the EasyEDA footprint linked to *lcsc*.

    Only fetches the footprint document (two requests) rather than the full
    component.  Raises :class:`api.APIError` on network or lookup failure.
    """
    uuids = api.fetch_component_uuids(lcsc)
    fp_data = api.fetch_component_data(uuids[-1]["component_uuid"])
    return jlc_footprint_from_data(lcsc, fp_data)
