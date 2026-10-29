"""Check JLCPCB assembly-library availability for the parts on a board."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import FootprintRecord, natural_key

OK = "ok"
NOT_FOUND = "not_found"
OUT_OF_STOCK = "out_of_stock"
LOW_STOCK = "low_stock"
LOOKUP_FAILED = "lookup_failed"

STATUS_TEXT = {
    OK: "In stock",
    NOT_FOUND: "Not found in JLCPCB parts library",
    OUT_OF_STOCK: "Out of stock",
    LOW_STOCK: "Stock below required quantity",
    LOOKUP_FAILED: "Lookup failed",
}

BASIC = "Basic"
EXTENDED = "Extended"


@dataclass
class Availability:
    """Availability of one LCSC part for the whole order."""

    lcsc: str
    references: List[str] = field(default_factory=list)
    qty_per_board: int = 0
    qty_needed: int = 0
    status: str = LOOKUP_FAILED
    stock: Optional[int] = None
    part_type: str = ""  # BASIC, EXTENDED, or "" when unknown
    unit_price: Optional[float] = None  # at the price tier for qty_needed
    description: str = ""
    message: str = ""

    @property
    def is_extended(self) -> bool:
        return self.part_type == EXTENDED

    @property
    def is_problem(self) -> bool:
        return self.status != OK

    @property
    def total_price(self) -> Optional[float]:
        if self.unit_price is None:
            return None
        return self.unit_price * self.qty_needed


def group_by_lcsc(footprints: Sequence[FootprintRecord]) -> Dict[str, List[str]]:
    """Map each valid LCSC number to the (sorted) references that use it.

    Only BOM parts count — DNP and excluded footprints don't consume stock.
    """
    groups: Dict[str, List[str]] = {}
    for fp in footprints:
        lcsc = fp.lcsc
        if fp.in_bom and lcsc:
            groups.setdefault(lcsc, []).append(fp.reference)
    for refs in groups.values():
        refs.sort(key=natural_key)
    return groups


def price_for_quantity(price_breaks: Sequence[Tuple[int, float]], qty: int) -> Optional[float]:
    """Unit price for *qty* from sorted ``(min_qty, price)`` breaks."""
    chosen = None
    for min_qty, price in price_breaks:
        if qty >= min_qty or chosen is None:
            chosen = price
    return chosen


def evaluate(lcsc: str, refs: List[str], boards: int, info: Optional[Dict[str, Any]]) -> Availability:
    """Turn a lookup result (``api.lookup_part`` dict or None) into an :class:`Availability`."""
    per_board = len(refs)
    needed = per_board * max(boards, 1)
    av = Availability(lcsc=lcsc, references=list(refs), qty_per_board=per_board, qty_needed=needed)
    if info is None:
        av.status = NOT_FOUND
        av.message = STATUS_TEXT[NOT_FOUND]
        return av

    try:
        av.stock = int(info.get("stock") or 0)
    except (TypeError, ValueError):
        av.stock = 0
    av.part_type = info.get("type") or ""
    av.description = info.get("description") or info.get("model") or ""
    breaks = info.get("price_breaks") or []
    av.unit_price = price_for_quantity(breaks, needed) if breaks else info.get("price")

    if av.stock <= 0:
        av.status = OUT_OF_STOCK
        av.message = STATUS_TEXT[OUT_OF_STOCK]
    elif av.stock < needed:
        av.status = LOW_STOCK
        av.message = f"Stock {av.stock} < {needed} needed ({per_board}/board × {boards})"
    else:
        av.status = OK
        av.message = f"{STATUS_TEXT[OK]} ({av.stock})"
    return av


def lookup_failed(lcsc: str, refs: List[str], boards: int, error: str) -> Availability:
    per_board = len(refs)
    return Availability(
        lcsc=lcsc,
        references=list(refs),
        qty_per_board=per_board,
        qty_needed=per_board * max(boards, 1),
        status=LOOKUP_FAILED,
        message=f"{STATUS_TEXT[LOOKUP_FAILED]}: {error}",
    )
