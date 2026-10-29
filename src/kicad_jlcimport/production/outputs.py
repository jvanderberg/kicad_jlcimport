"""Build and write JLCPCB BOM/CPL CSV files and the Gerber zip."""

import csv
import os
import zipfile
from typing import Dict, List, Sequence, Tuple

from .conventions import BOTTOM
from .models import FootprintRecord, natural_key
from .placement import Placement

BOM_HEADER = ["Comment", "Designator", "Footprint", "JLCPCB Part #"]
CPL_HEADER = ["Designator", "Mid X", "Mid Y", "Rotation", "Layer"]


def build_bom_rows(footprints: Sequence[FootprintRecord]) -> List[List[str]]:
    """Group BOM parts by (value, footprint, LCSC) into JLCPCB BOM rows.

    Footprints without a valid LCSC number keep their raw field value (if
    any) so JLCPCB's BOM matcher shows them as unmatched rather than losing
    them.
    """
    groups: Dict[Tuple[str, str, str], List[str]] = {}
    for fp in footprints:
        if not fp.in_bom:
            continue
        part = fp.lcsc or fp.raw_lcsc
        groups.setdefault((fp.value, fp.footprint, part), []).append(fp.reference)

    rows = []
    for (value, footprint, part), refs in groups.items():
        refs.sort(key=natural_key)
        rows.append([value, ",".join(refs), footprint, part])
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


def zip_files(paths: Sequence[str], zip_path: str) -> str:
    """Zip *paths* (flat, by basename) into *zip_path*.  Returns *zip_path*."""
    os.makedirs(os.path.dirname(os.path.abspath(zip_path)), exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(paths):
            zf.write(p, os.path.basename(p))
    return zip_path
