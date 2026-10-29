"""File-level helpers shared by the plugin and the CLI."""

import os
from typing import Dict, Tuple

from ..easyeda.api import lookup_part
from .cache import KeyedCache
from .jlc_footprints import fetch_jlc_footprint
from .outputs import BOM_HEADER, CPL_HEADER, write_csv
from .report import ProductionReport

OUTPUT_DIR_NAME = "production"


def default_output_dir(board_path: str) -> str:
    """``<project>/production`` next to the board file."""
    return os.path.join(os.path.dirname(os.path.abspath(board_path)), OUTPUT_DIR_NAME)


def board_basename(board_path: str) -> str:
    return os.path.splitext(os.path.basename(board_path))[0] or "board"


def make_caches() -> Tuple[KeyedCache, KeyedCache]:
    """(part lookup cache, JLC footprint cache) backed by the live APIs."""
    return KeyedCache(lookup_part), KeyedCache(fetch_jlc_footprint)


def write_bom_cpl(report: ProductionReport, out_dir: str, basename: str) -> Dict[str, str]:
    """Write ``<basename>-bom.csv``, ``-cpl.csv`` and ``-report.txt``.  Returns their paths."""
    paths = {
        "bom": os.path.join(out_dir, f"{basename}-bom.csv"),
        "cpl": os.path.join(out_dir, f"{basename}-cpl.csv"),
        "report": os.path.join(out_dir, f"{basename}-report.txt"),
    }
    write_csv(paths["bom"], BOM_HEADER, report.bom_rows())
    write_csv(paths["cpl"], CPL_HEADER, report.cpl_rows())
    with open(paths["report"], "w", encoding="utf-8") as f:
        f.write(report.to_text())
    return paths
