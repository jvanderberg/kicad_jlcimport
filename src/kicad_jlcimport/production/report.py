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
from .placement import KICAD, MANUAL, Placement, compute_placement
from .solver import MEDIUM, POLARITY_BY_NUMBERING, POLARITY_SWAPPED, POLARITY_UNRESOLVED

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
        return sorted(lcsc for lcsc, a in self.availability.items() if a.is_extended)

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
        lines = [
            f"{len(self.rows)} footprints, {len(self.availability)} unique LCSC parts, {self.boards} boards",
            f"{self.count(ERROR)} errors, {self.count(WARNING)} warnings",
        ]
        if self.extended_parts:
            lines.append(
                f"{len(self.extended_parts)} extended parts (each adds a JLCPCB setup fee): "
                + ", ".join(self.extended_parts)
            )
        cost = self.estimated_parts_cost()
        if cost is not None:
            lines.append(f"Estimated parts cost: ${cost:.2f} (unit prices at order quantity, excl. fees)")
        if n_unverified:
            lines.append(f"{n_unverified} placements unverified (KiCad position/rotation used)")
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
        for issue in self.board_issues:
            lines.append(f"[{issue.severity.upper()}] {issue.message}")
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


def _fetch(cache: KeyedCache, lcsc: str, on_ssl_error: Optional[Callable[[], None]]) -> Tuple[Any, str]:
    """Fetch through *cache*; returns (value, error message)."""
    try:
        try:
            return cache.get(lcsc), ""
        except SSLCertError:
            if on_ssl_error is None:
                raise
            on_ssl_error()
            return cache.get(lcsc), ""
    except Exception as e:  # network/parse failures become report entries
        return None, str(e) or type(e).__name__


def run_check(
    footprints: Sequence[FootprintRecord],
    boards: int,
    part_cache: KeyedCache,
    footprint_cache: KeyedCache,
    progress: Optional[ProgressFn] = None,
    on_ssl_error: Optional[Callable[[], None]] = None,
    max_workers: int = 4,
) -> ProductionReport:
    """Check availability and solve placements for all footprints.

    *part_cache* wraps ``api.lookup_part`` (dict or None per LCSC);
    *footprint_cache* wraps :func:`fetch_jlc_footprint`.  Network failures
    are reported, never raised.
    """
    boards = max(int(boards), 1)
    report = ProductionReport(boards=boards)
    groups = av.group_by_lcsc(footprints)
    lcscs = sorted(groups, key=natural_key)

    part_results: Dict[str, Tuple[Any, str]] = {}
    fp_results: Dict[str, Tuple[Optional[JlcFootprint], str]] = {}

    def task(lcsc: str) -> str:
        part_results[lcsc] = _fetch(part_cache, lcsc, on_ssl_error)
        fp_results[lcsc] = _fetch(footprint_cache, lcsc, on_ssl_error)
        return lcsc

    if lcscs:
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(lcscs)))) as pool:
            for done, lcsc in enumerate(pool.map(task, lcscs), start=1):
                if progress:
                    progress(done, len(lcscs), lcsc)

    for lcsc in lcscs:
        info, err = part_results[lcsc]
        refs = groups[lcsc]
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
        if a.status in (av.NOT_FOUND, av.OUT_OF_STOCK, av.LOW_STOCK):
            row.add(ERROR, a.message)
        elif a.status == av.LOOKUP_FAILED:
            row.add(WARNING, a.message)
        if a.is_extended:
            row.add(INFO, "Extended part (setup fee)")

    if not fp.in_cpl:
        return row

    jlc_fp, fetch_err = fp_results.get(lcsc, (None, "")) if lcsc else (None, "")
    if lcsc and jlc_fp is None:
        fetch_err = f"JLC footprint unavailable ({fetch_err})" if fetch_err else "JLC footprint unavailable"
    pl = compute_placement(fp, jlc_fp, fetch_err)
    row.placement = pl
    if pl.source == KICAD:
        if lcsc:
            row.add(WARNING, "; ".join(pl.notes))
        else:
            row.add(INFO, "Placement unverified")
    elif pl.source == MANUAL:
        row.add(INFO, "; ".join(pl.notes))
    elif pl.fit is not None and POLARITY_UNRESOLVED in pl.fit.flags:
        row.add(WARNING, "Placement solved: " + "; ".join(pl.notes))
    elif pl.confidence == MEDIUM:
        row.add(INFO, "Placement solved: " + "; ".join(pl.notes))
    elif pl.fit is not None and (POLARITY_BY_NUMBERING in pl.fit.flags or POLARITY_SWAPPED in pl.fit.flags):
        row.add(INFO, "; ".join(pl.notes))
    return row
