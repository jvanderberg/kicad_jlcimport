"""Gerbers and drill files through ``kicad-cli``, for the CLI.

Inside KiCad the plugin plots through pcbnew
(:mod:`kicad_jlcimport.production_pcbnew`).  Outside it, ``kicad-cli pcb
export gerbers`` and ``pcb export drill`` are run with the same settings:
the layers JLCPCB needs, Protel extensions, X2 attributes and netlist,
6-digit precision, mask subtracted from silk, absolute origin (so the CPL and
the Gerbers share it), and Excellon in mm with separate PTH/NPTH files and
routed oval holes.  ``kicad-cli`` never writes to the board file; with
``--check-zones`` (KiCad 10+) it refills zones in memory before plotting.
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, List, Optional, Sequence, Tuple

from .outputs import move_files, zip_files

# Non-copper layers, in kicad-cli's untranslated names.
FIXED_LAYERS = ["F.Mask", "B.Mask", "F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen", "Edge.Cuts"]

_MAC_PATHS = [
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    os.path.expanduser("~/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
]
_WINDOWS_ROOTS = [
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs") if os.environ.get("LOCALAPPDATA") else "",
]

Runner = Callable[[List[str]], "subprocess.CompletedProcess"]


class KicadCliError(RuntimeError):
    """kicad-cli is missing or failed."""


def _version_key(path: str) -> Tuple[int, ...]:
    """Sort key for ``.../KiCad/<version>/bin/kicad-cli.exe``: newest version last."""
    version = os.path.basename(os.path.dirname(os.path.dirname(path)))
    return tuple(int(n) for n in re.findall(r"\d+", version))


def find_kicad_cli(explicit: Optional[str] = None) -> Optional[str]:
    """Path to kicad-cli: *explicit*, ``$KICAD_CLI``, ``PATH``, then the usual install folders.

    A path the user gave (*explicit* or ``$KICAD_CLI``) is used or rejected
    as it stands, never silently replaced by another install; see
    :func:`not_found_message`.
    """
    for candidate in (explicit, os.environ.get("KICAD_CLI")):
        if candidate:
            return candidate if os.path.isfile(candidate) else shutil.which(candidate)
    found = shutil.which("kicad-cli")
    if found:
        return found
    if sys.platform == "darwin":
        for path in _MAC_PATHS:
            if os.path.isfile(path):
                return path
    elif sys.platform == "win32":
        patterns = [os.path.join(root, "KiCad", "*", "bin", "kicad-cli.exe") for root in _WINDOWS_ROOTS if root]
        matches = [p for pattern in patterns for p in glob.glob(pattern)]
        if matches:
            return max(matches, key=_version_key)
    return None


def not_found_message(explicit: Optional[str] = None) -> str:
    """Why :func:`find_kicad_cli` found nothing, naming the path the user gave if there was one."""
    if explicit:
        return f"kicad-cli not found at {explicit}"
    env = os.environ.get("KICAD_CLI")
    if env:
        return f"kicad-cli not found at {env} (from $KICAD_CLI)"
    return "kicad-cli not found on PATH or in the usual KiCad install folders"


def _run(args: List[str]) -> "subprocess.CompletedProcess":
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    except OSError as e:
        raise KicadCliError(f"Could not run {args[0]}: {e}") from e


def _check(proc: "subprocess.CompletedProcess", what: str) -> None:
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise KicadCliError(f"kicad-cli {what} failed (exit {proc.returncode})" + (f":\n{detail}" if detail else ""))


def supports_zone_refill(kicad_cli: str, run: Optional[Runner] = None) -> bool:
    """True when this kicad-cli can refill zones before plotting (``--check-zones``, KiCad 10+)."""
    proc = (run or _run)([kicad_cli, "pcb", "export", "gerbers", "--help"])
    return "--check-zones" in (proc.stdout or "") + (proc.stderr or "")


def gerber_layers(copper_layers: Sequence[str]) -> List[str]:
    return list(copper_layers or ["F.Cu", "B.Cu"]) + FIXED_LAYERS


def gerber_command(kicad_cli: str, board: str, out_dir: str, copper_layers: Sequence[str], refill: bool) -> List[str]:
    args = [
        kicad_cli,
        "pcb",
        "export",
        "gerbers",
        "--output",
        out_dir + os.sep,
        "--layers",
        ",".join(gerber_layers(copper_layers)),
        "--subtract-soldermask",
        "--precision",
        "6",
    ]
    if refill:
        args.append("--check-zones")
    return args + [board]


def drill_command(kicad_cli: str, board: str, out_dir: str) -> List[str]:
    return [
        kicad_cli,
        "pcb",
        "export",
        "drill",
        "--output",
        out_dir + os.sep,
        "--format",
        "excellon",
        "--drill-origin",
        "absolute",
        "--excellon-units",
        "mm",
        "--excellon-zeros-format",
        "decimal",
        "--excellon-oval-format",
        "route",
        "--excellon-separate-th",
        board,
    ]


def generate_fabrication_zip(
    kicad_cli: str,
    board: str,
    out_dir: str,
    basename: str,
    copper_layers: Sequence[str],
    refill: bool = True,
    run: Optional[Runner] = None,
) -> Tuple[str, List[str]]:
    """Plot Gerbers + drill into ``out_dir/gerbers`` and zip them as ``<basename>-gerbers.zip``.

    kicad-cli picks the file names, so both commands write into empty staging
    folders, and only once both have succeeded is what they wrote moved into
    ``gerbers/`` (the Gerber job file and drill map are left out, as in the
    plugin).  A failure leaves ``out_dir`` as it was.  *refill* needs
    kicad-cli 10+; check :func:`supports_zone_refill` first.
    """
    run = run or _run
    os.makedirs(out_dir, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".kicad-cli-", dir=out_dir)
    try:
        plots, drills = os.path.join(staging, "gerbers"), os.path.join(staging, "drill")
        os.mkdir(plots)
        os.mkdir(drills)
        _check(run(gerber_command(kicad_cli, board, plots, copper_layers, refill)), "pcb export gerbers")
        _check(run(drill_command(kicad_cli, board, drills)), "pcb export drill")
        if not any(not name.lower().endswith(".gbrjob") for name in os.listdir(plots)):
            raise KicadCliError("kicad-cli wrote no Gerber files")
        if not any(name.lower().endswith(".drl") for name in os.listdir(drills)):
            raise KicadCliError("kicad-cli wrote no drill files")
        gerber_dir = os.path.join(out_dir, "gerbers")
        os.makedirs(gerber_dir, exist_ok=True)
        files = move_files(plots, gerber_dir, lambda name: not name.lower().endswith(".gbrjob"))
        files += move_files(drills, gerber_dir, lambda name: name.lower().endswith(".drl"))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    zip_path = zip_files(files, os.path.join(out_dir, f"{basename}-gerbers.zip"))
    return zip_path, files
