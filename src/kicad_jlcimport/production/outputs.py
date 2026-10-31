"""Build and write JLCPCB BOM/CPL CSV files and the Gerber zip."""

import csv
import os
import zipfile
from typing import Callable, Dict, Iterable, List, Sequence, Tuple

from .conventions import BOTTOM
from .models import FootprintRecord, natural_key
from .placement import Placement

BOM_HEADER = ["Comment", "Designator", "Footprint", "JLCPCB Part #"]
CPL_HEADER = ["Designator", "Mid X", "Mid Y", "Rotation", "Layer"]


def distinct(values: Iterable[str]) -> List[str]:
    """The distinct non-empty *values*, in first-seen order."""
    seen: List[str] = []
    for v in values:
        if v and v not in seen:
            seen.append(v)
    return seen


def join_distinct(values: Iterable[str]) -> str:
    """The distinct non-empty *values*, in first-seen order, joined with `` / ``."""
    return " / ".join(distinct(values))


def build_bom_rows(footprints: Sequence[FootprintRecord]) -> List[List[str]]:
    """Group BOM parts into JLCPCB BOM rows: one row per LCSC part number.

    JLCPCB matches BOM lines by part number.  When two lines carry the same
    number it unticks both with quantity 0, so it assembles neither.  For that
    reason every part with a valid LCSC number gets one line, however its
    Values or footprints differ (two LEDs named "TX" and "RX", say).  The
    Comment and Footprint columns are the group's distinct values, in
    designator order.

    Footprints without a valid LCSC number are grouped by (value, footprint,
    raw field value).  They keep the raw value (if any), so JLCPCB's BOM
    matcher shows them as unmatched instead of losing them.
    """
    groups: Dict[Tuple[str, ...], List[FootprintRecord]] = {}
    for fp in footprints:
        if not fp.in_bom:
            continue
        key = (fp.lcsc,) if fp.lcsc else (fp.value, fp.footprint, fp.raw_lcsc)
        groups.setdefault(key, []).append(fp)

    rows = []
    for members in groups.values():
        members.sort(key=lambda f: natural_key(f.reference))
        rows.append(
            [
                join_distinct(f.value for f in members),
                ",".join(f.reference for f in members),
                join_distinct(f.footprint for f in members),
                members[0].lcsc or members[0].raw_lcsc,
            ]
        )
    rows.sort(key=lambda r: natural_key(r[1].split(",")[0]))
    return rows


def _fmt_mm(value: float) -> str:
    return f"{value:.4f}mm"


def _fmt_deg(value: float) -> str:
    text = f"{round(value, 2) % 360.0:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def build_cpl_rows(placements: Sequence[Placement]) -> List[List[str]]:
    """CPL rows in JLCPCB's column order, naturally sorted by designator."""
    rows = []
    for pl in sorted(placements, key=lambda p: natural_key(p.reference)):
        rows.append(
            [
                pl.reference,
                _fmt_mm(pl.x),
                _fmt_mm(pl.y),
                _fmt_deg(pl.rotation),
                "Bottom" if pl.side == BOTTOM else "Top",
            ]
        )
    return rows


def write_csv(path: str, header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """Write a UTF-8 CSV with LF line endings, creating the directory if needed."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def move_files(src_dir: str, dest_dir: str, keep: Callable[[str], bool]) -> List[str]:
    """Move the files in *src_dir* whose names pass *keep* into *dest_dir*, replacing any there.

    Returns their new paths, sorted by name.  Used to pick up files whose names
    KiCad chooses, from a staging folder they were written to.
    """
    files = []
    for name in sorted(os.listdir(src_dir)):
        if keep(name):
            dest = os.path.join(dest_dir, name)
            os.replace(os.path.join(src_dir, name), dest)
            files.append(dest)
    return files


def zip_files(paths: Sequence[str], zip_path: str) -> str:
    """Zip *paths* (flat, by basename) into *zip_path*.  Returns *zip_path*."""
    os.makedirs(os.path.dirname(os.path.abspath(zip_path)), exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(paths):
            zf.write(p, os.path.basename(p))
    return zip_path
