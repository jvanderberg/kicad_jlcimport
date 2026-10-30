"""Run the production check and hold its results.

:func:`run_check` is the single entry point used by the plugin dialog and the
CLI: it looks up every unique LCSC part (availability + JLC footprint) in
parallel, solves each placement and collects issues into a
:class:`ProductionReport`.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..easyeda.api import SSLCertError
from . import availability as av
from .cache import KeyedCache
from .jlc_footprints import JlcFootprint
from .models import FootprintRecord, natural_key
from .outputs import build_bom_rows, build_cpl_rows
from .placement import ESTIMATED, KICAD, MANUAL, Placement, compute_placement
from .solver import (
    MEDIUM,
    POLARITY_BY_CONVENTION,
    POLARITY_BY_FOOTPRINT_NAME,
    POLARITY_BY_NUMBERING,
    POLARITY_BY_PIN_NAMES,
    POLARITY_JLC_UNNAMED,
    POLARITY_NAMES_UNAVAILABLE,
    POLARITY_SWAPPED,
    POLARITY_UNRESOLVED,
)

# Solver flags that make a solved placement a warning rather than a note.
_WARNING_FLAGS = (POLARITY_UNRESOLVED, POLARITY_NAMES_UNAVAILABLE)
# Polarized parts: worth a look, because JLC's symbol pin names don't always
# match how JLC places the part (or name the pins at all).
_POLARIZED_FLAGS = (POLARITY_BY_PIN_NAMES, POLARITY_BY_FOOTPRINT_NAME, POLARITY_BY_CONVENTION, POLARITY_JLC_UNNAMED)

# Fixed texts so the issue list groups them into one line each.
ESTIMATED_TEXT = (
    "Estimated placement: pads don't closely match JLC's footprint; check its orientation in JLC's placement preview"
)
POLARITY_CHECK_TEXT = "Polarized part: check its polarity in JLC's placement preview"

ERROR = "error"
WARNING = "warning"
INFO = "info"
_SEVERITY_RANK = {ERROR: 0, WARNING: 1, INFO: 2}

ProgressFn = Callable[[int, int, str], None]


@dataclass
class Issue:
    severity: str
    message: str


@dataclass
class PartRow:
    """One footprint as shown in the report table."""

    footprint: FootprintRecord
    availability: Optional[av.Availability] = None
    placement: Optional[Placement] = None
    issues: List[Issue] = field(default_factory=list)

    @property
    def reference(self) -> str:
        return self.footprint.reference

    @property
    def severity(self) -> str:
        """Worst issue severity, or '' when there are no issues."""
        if not self.issues:
            return ""
        return min(self.issues, key=lambda i: _SEVERITY_RANK[i.severity]).severity

    def add(self, severity: str, message: str) -> None:
        self.issues.append(Issue(severity, message))


@dataclass
class ProductionReport:
    boards: int
    rows: List[PartRow] = field(default_factory=list)
    availability: Dict[str, av.Availability] = field(default_factory=dict)
    board_issues: List[Issue] = field(default_factory=list)

    def issues(self) -> List[Issue]:
        out = list(self.board_issues)
        for row in self.rows:
            out.extend(row.issues)
        return out

    def count(self, severity: str) -> int:
        return sum(1 for i in self.issues() if i.severity == severity)

    @property
    def extended_parts(self) -> List[str]:
        """Extended parts that carry a feeder loading fee."""
        return sorted((lcsc for lcsc, a in self.availability.items() if a.is_extended), key=natural_key)

    @property
    def preferred_parts(self) -> List[str]:
        return sorted((lcsc for lcsc, a in self.availability.items() if a.is_preferred), key=natural_key)

    @property
    def double_sided(self) -> bool:
        """True when assembled parts sit on both sides of the board."""
        sides = {r.footprint.side for r in self.rows if r.footprint.in_cpl and r.footprint.lcsc}
        return len(sides) > 1

    @property
    def placements(self) -> List[Placement]:
        return [r.placement for r in self.rows if r.placement is not None]

    @property
    def footprints(self) -> List[FootprintRecord]:
        return [r.footprint for r in self.rows]

    def estimated_parts_cost(self) -> Optional[float]:
        """Sum of part costs for the whole order, or None if no prices are known."""
        totals = [a.total_price for a in self.availability.values() if a.total_price is not None]
        return sum(totals) if totals else None

    def bom_rows(self) -> List[List[str]]:
        return build_bom_rows(self.footprints)

    def cpl_rows(self) -> List[List[str]]:
        return build_cpl_rows(self.placements)

    def summary_lines(self) -> List[str]:
        n_unverified = sum(1 for p in self.placements if p.source == KICAD)
        n_estimated = sum(1 for p in self.placements if p.source == ESTIMATED)
        lines = [
            f"{len(self.rows)} footprints, {len(self.availability)} unique LCSC parts, {self.boards} boards",
            f"{self.count(ERROR)} errors, {self.count(WARNING)} warnings",
        ]
        if self.extended_parts:
            lines.append(
                f"{len(self.extended_parts)} extended parts (each adds a feeder loading fee): "
                + ", ".join(self.extended_parts)
            )
        if self.preferred_parts:
            lines.append(
                f"{len(self.preferred_parts)} preferred extended parts (no loading fee on Economic assembly): "
                + ", ".join(self.preferred_parts)
            )
        if self.double_sided:
            lines.append(
                "Parts on both sides: JLCPCB assembles both sides only with Standard PCBA, "
                "which charges a loading fee for every unique part"
            )
        cost = self.estimated_parts_cost()
        if cost is not None:
            lines.append(f"Estimated parts cost: ${cost:.2f} (unit prices at order quantity, excl. fees)")
        if n_estimated:
            lines.append(f"{n_estimated} placements estimated (loose fit to JLC's footprint, rounded to 90°)")
        if n_unverified:
            lines.append(f"{n_unverified} placements unverified (KiCad position/rotation used)")
        if any(a.status == av.NOT_CHECKED for a in self.availability.values()):
            lines.append("Stock, part type and prices not checked")
        return lines

    def issue_lines(self, min_severity: str = WARNING) -> List[str]:
        """One line per distinct issue, worst first, e.g. ``[WARNING] TP1, TP2: No LCSC...``.

        Rows sharing the same severity and message are merged into one line so
        a handful of identical warnings (test points, say) don't drown the rest.
        """
        limit = _SEVERITY_RANK[min_severity]
        grouped: Dict[Tuple[str, str], List[str]] = {}
        for issue in self.board_issues:
            grouped.setdefault((issue.severity, issue.message), [])
        for row in self.rows:
            for issue in row.issues:
                grouped.setdefault((issue.severity, issue.message), []).append(row.reference)
        lines = []
        for (severity, message), refs in sorted(grouped.items(), key=lambda kv: _SEVERITY_RANK[kv[0][0]]):
            if _SEVERITY_RANK[severity] > limit:
                continue
            prefix = f"[{severity.upper()}] " + (", ".join(refs) + ": " if refs else "")
            lines.append(prefix + message)
        return lines

    def to_text(self) -> str:
        """Plain-text report for the CLI and the report file."""
        lines = ["JLCPCB production check", "=" * 23]
        lines.extend(self.summary_lines())
        lines.append("")
        issues = self.issue_lines()
        if issues:
            lines.extend(issues)
            lines.append("")
        header = f"{'Ref':<8} {'Value':<16} {'LCSC':<11} {'Stock':<26} {'Type':<9} {'Rot':>7} {'Conf':<10} Notes"
        lines.append(header)
        lines.append("-" * len(header))
        for row in self.rows:
            lines.append(format_row(row))
        return "\n".join(lines) + "\n"


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def row_cells(row: PartRow) -> Dict[str, str]:
    """Display strings for a report row, shared by the CLI and the dialog."""
    fp = row.footprint
    a = row.availability
    pl = row.placement
    if not fp.in_bom:
        stock = "DNP" if fp.dnp else "excluded"
    elif a is None:
        stock = "-"
    else:
        stock = a.message
    return {
        "ref": fp.reference,
        "value": fp.value,
        "lcsc": fp.lcsc or fp.raw_lcsc,
        "stock": stock,
        "type": a.part_type if a else "",
        "rotation": f"{pl.rotation:g}" if pl else "",
        "x": f"{pl.x:.3f}" if pl else "",
        "y": f"{pl.y:.3f}" if pl else "",
        "confidence": pl.confidence if pl else "",
        "notes": "; ".join(i.message for i in row.issues),
    }


def format_row(row: PartRow) -> str:
    c = row_cells(row)
    return (
        f"{_clip(c['ref'], 8):<8} {_clip(c['value'], 16):<16} {_clip(c['lcsc'], 11):<11} "
        f"{_clip(c['stock'], 26):<26} {c['type']:<9} {c['rotation']:>7} {c['confidence']:<10} {c['notes']}"
    )


def _error_text(e: Exception) -> str:
    return str(e) or type(e).__name__


def _fetch(cache: KeyedCache, lcsc: str, on_ssl_error: Optional[Callable[[], None]]) -> Tuple[Any, str]:
    """Fetch through *cache*; returns (value, error message).

    Network/parse failures become report entries.  A TLS certificate error
    without an *on_ssl_error* handler is raised instead: it affects every
    part, and the caller (the CLI) reports it once with its ``--insecure`` hint.
    """
    try:
        return cache.get(lcsc), ""
    except SSLCertError:
        if on_ssl_error is None:
            raise
        on_ssl_error()
        try:
            return cache.get(lcsc), ""
        except Exception as e:
            return None, _error_text(e)
    except Exception as e:
        return None, _error_text(e)


def run_check(
    footprints: Sequence[FootprintRecord],
    boards: int,
    part_cache: KeyedCache,
    footprint_cache: KeyedCache,
    progress: Optional[ProgressFn] = None,
    on_ssl_error: Optional[Callable[[], None]] = None,
    max_workers: int = 4,
    check_stock: bool = True,
) -> ProductionReport:
    """Check availability and solve placements for all footprints.

    *part_cache* wraps ``api.lookup_part`` (dict or None per LCSC);
    *footprint_cache* wraps :func:`fetch_jlc_footprint`.  Network failures
    are reported, never raised.  ``check_stock=False`` skips the parts-library
    lookup (stock, basic/extended, price); placements are still solved.
    """
    boards = max(int(boards), 1)
    report = ProductionReport(boards=boards)
    groups = av.group_by_lcsc(footprints)
    lcscs = sorted(groups, key=natural_key)

    part_results: Dict[str, Tuple[Any, str]] = {}
    fp_results: Dict[str, Tuple[Optional[JlcFootprint], str]] = {}

    def task(lcsc: str) -> str:
        if check_stock:
            part_results[lcsc] = _fetch(part_cache, lcsc, on_ssl_error)
        fp_results[lcsc] = _fetch(footprint_cache, lcsc, on_ssl_error)
        return lcsc

    if lcscs:
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(lcscs)))) as pool:
            for done, lcsc in enumerate(pool.map(task, lcscs), start=1):
                if progress:
                    progress(done, len(lcscs), lcsc)

    for lcsc in lcscs:
        refs = groups[lcsc]
        if not check_stock:
            report.availability[lcsc] = av.not_checked(lcsc, refs, boards)
            continue
        info, err = part_results[lcsc]
        report.availability[lcsc] = (
            av.lookup_failed(lcsc, refs, boards, err) if err else av.evaluate(lcsc, refs, boards, info)
        )

    _check_duplicates(footprints, report)
    for fp in sorted(footprints, key=lambda f: natural_key(f.reference)):
        report.rows.append(_build_row(fp, report, fp_results))
    return report


def _check_duplicates(footprints: Sequence[FootprintRecord], report: ProductionReport) -> None:
    seen: Dict[str, int] = {}
    for fp in footprints:
        if fp.in_bom:
            seen[fp.reference] = seen.get(fp.reference, 0) + 1
    dups = sorted((r for r, n in seen.items() if n > 1), key=natural_key)
    if dups:
        report.board_issues.append(Issue(ERROR, "Duplicate designators: " + ", ".join(dups)))


def _build_row(
    fp: FootprintRecord,
    report: ProductionReport,
    fp_results: Dict[str, Tuple[Optional[JlcFootprint], str]],
) -> PartRow:
    row = PartRow(footprint=fp)
    if not fp.in_bom:
        return row

    lcsc = fp.lcsc
    if lcsc is None:
        if fp.raw_lcsc:
            row.add(ERROR, f'Invalid LCSC part number "{fp.raw_lcsc}"')
        else:
            row.add(WARNING, "No LCSC part number — JLCPCB will not assemble this part")
    else:
        a = report.availability[lcsc]
        row.availability = a
        # A failed lookup is an error too: nothing about the part was checked.
        if a.status in (av.NOT_FOUND, av.OUT_OF_STOCK, av.LOW_STOCK, av.LOOKUP_FAILED):
            row.add(ERROR, a.message)
        elif a.status == av.STOCK_TIGHT:
            row.add(WARNING, a.message)
        if a.is_extended:
            row.add(INFO, "Extended part (loading fee)")

    if not fp.in_cpl:
        return row

    jlc_fp, fetch_err = fp_results.get(lcsc, (None, "")) if lcsc else (None, "")
    if lcsc and jlc_fp is None:
        fetch_err = f"JLC footprint unavailable ({fetch_err})" if fetch_err else "JLC footprint unavailable"
    pl = compute_placement(fp, jlc_fp, fetch_err)
    row.placement = pl
    for error in pl.override_errors:
        row.add(WARNING, error)
    if pl.source == KICAD:
        if lcsc:
            row.add(WARNING, "; ".join(pl.notes))
        else:
            row.add(INFO, "Placement unverified")
    elif pl.source == MANUAL:
        row.add(INFO, "; ".join(pl.notes))
    elif pl.source == ESTIMATED:
        row.add(WARNING, ESTIMATED_TEXT)
        row.add(INFO, "; ".join(pl.notes))
    elif pl.fit is not None and any(f in pl.fit.flags for f in _WARNING_FLAGS):
        row.add(WARNING, "Placement solved: " + "; ".join(pl.notes))
    else:
        if pl.fit is not None and any(f in pl.fit.flags for f in _POLARIZED_FLAGS):
            row.add(WARNING, POLARITY_CHECK_TEXT)
        if pl.confidence == MEDIUM:
            row.add(INFO, "Placement solved: " + "; ".join(pl.notes))
        elif pl.fit is not None and (POLARITY_BY_NUMBERING in pl.fit.flags or POLARITY_SWAPPED in pl.fit.flags):
            row.add(INFO, "; ".join(pl.notes))
    return row
