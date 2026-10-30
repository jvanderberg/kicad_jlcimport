"""Fetch JLC's own (EasyEDA) footprint for an LCSC part, reduced to pad centres."""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..easyeda import api
from ..easyeda.parser import parse_footprint_shapes, parse_symbol_shapes
from .models import PadRecord


@dataclass
class JlcFootprint:
    """JLC's footprint for a part: pads in footprint-local mm (Y down, origin at JLC's origin).

    ``pin_names`` maps pad number to JLC's symbol pin name (e.g. ``"2": "K"``).
    It is only fetched for two-pad parts, where it decides polarity.
    """

    lcsc: str
    title: str = ""
    pads: List[PadRecord] = field(default_factory=list)
    pin_names: Dict[str, str] = field(default_factory=dict)


def _data_shapes(data: Dict[str, Any]):
    data_str = data.get("dataStr", {}) or {}
    head = data_str.get("head", {}) or {}
    return data_str.get("shape", []) or [], head.get("x", 0), head.get("y", 0)


def jlc_footprint_from_data(lcsc: str, fp_data: Dict[str, Any]) -> JlcFootprint:
    """Build a :class:`JlcFootprint` from an EasyEDA component-data result."""
    fp = parse_footprint_shapes(*_data_shapes(fp_data))
    pads = [PadRecord(number=p.number, x=p.x, y=p.y) for p in fp.pads]
    return JlcFootprint(lcsc=lcsc, title=fp_data.get("title", ""), pads=pads)


def pin_names_from_data(symbol_data: List[Dict[str, Any]]) -> Dict[str, str]:
    """Pad number -> pin name from EasyEDA symbol documents (first name wins)."""
    names: Dict[str, str] = {}
    for data in symbol_data:
        for pin in parse_symbol_shapes(*_data_shapes(data)).pins:
            if pin.number and pin.name:
                names.setdefault(pin.number.strip(), pin.name.strip())
    return names


def fetch_jlc_footprint(lcsc: str) -> JlcFootprint:
    """Download and parse the EasyEDA footprint linked to *lcsc*.

    Fetches the footprint document only, plus the symbol for two-pad parts
    (diodes, LEDs, polarized capacitors) whose pin names give polarity.
    Raises :class:`api.APIError` on network or lookup failure; a failed
    symbol fetch just leaves ``pin_names`` empty.
    """
    uuids = api.fetch_component_uuids(lcsc)
    fp_data = api.fetch_component_data(uuids[-1]["component_uuid"])
    jlc = jlc_footprint_from_data(lcsc, fp_data)
    if len({p.number for p in jlc.pads if p.number}) == 2 and len(uuids) > 1:
        try:
            symbols = [api.fetch_component_data(u["component_uuid"]) for u in uuids[:-1]]
            jlc.pin_names = pin_names_from_data(symbols)
        except api.SSLCertError:
            raise
        except Exception:  # polarity just stays unresolved
            jlc.pin_names = {}
    return jlc
