"""Thin pcbnew adapter for production export (KiCad 8/9/10 SWIG API).

Reads the live board into :class:`FootprintRecord` objects and plots Gerber
and Excellon drill files.  Everything else lives in the pure
:mod:`kicad_jlcimport.production` package.  The ``pcbnew`` module is passed
in (defaulting to the real one) so tests can use fakes.

API differences between KiCad versions are absorbed with ``getattr``
fallbacks rather than version checks; every optional setter goes through
:func:`_try_call`.
"""

import os
import shutil
import tempfile
from typing import Dict, List, Optional, Tuple

from .production.conventions import BOTTOM, TOP
from .production.models import LCSC_FIELD_NAMES, FootprintRecord, PadRecord
from .production.outputs import zip_files

IU_PER_MM = 1_000_000  # pcbnew internal units are nanometres

# (layer constant name, file suffix, description)
FIXED_GERBER_LAYERS = [
    ("F_Cu", "F_Cu", "Top copper"),
    ("B_Cu", "B_Cu", "Bottom copper"),
    ("F_Mask", "F_Mask", "Top solder mask"),
    ("B_Mask", "B_Mask", "Bottom solder mask"),
    ("F_Paste", "F_Paste", "Top paste"),
    ("B_Paste", "B_Paste", "Bottom paste"),
    ("F_SilkS", "F_Silkscreen", "Top silkscreen"),
    ("B_SilkS", "B_Silkscreen", "Bottom silkscreen"),
    ("Edge_Cuts", "Edge_Cuts", "Board outline"),
]


def _pcbnew(mod=None):
    if mod is not None:
        return mod
    import pcbnew

    return pcbnew


def _try_call(obj, name: str, *args) -> bool:
    """Call ``obj.name(*args)`` if it exists; swallow API mismatches.  Returns success."""
    fn = getattr(obj, name, None)
    if fn is None:
        return False
    try:
        fn(*args)
    except (TypeError, AttributeError, ValueError):
        return False
    return True


def _mm(value) -> float:
    return float(value) / IU_PER_MM


def _str(value) -> str:
    return "" if value is None else str(value)


# ---------------------------------------------------------------------------
# Board → records
# ---------------------------------------------------------------------------


def _footprint_properties(fp) -> Dict[str, str]:
    props: Dict[str, str] = {}
    getter = getattr(fp, "GetFieldsText", None)  # KiCad 8+: {name: text}
    if getter is not None:
        try:
            props.update({_str(k): _str(v) for k, v in dict(getter()).items()})
        except Exception:
            pass
    if not props:
        getter = getattr(fp, "GetFields", None)
        if getter is not None:
            try:
                for field in getter():
                    props[_str(field.GetName())] = _str(field.GetText())
            except Exception:
                pass
    getter = getattr(fp, "GetProperties", None)  # KiCad 7 user properties
    if getter is not None:
        try:
            for k, v in dict(getter()).items():
                props.setdefault(_str(k), _str(v))
        except Exception:
            pass
    if not any(name.lower() in {k.lower() for k in props} for name in LCSC_FIELD_NAMES):
        getter = getattr(fp, "GetFieldText", None)
        for name in LCSC_FIELD_NAMES if getter is not None else ():
            try:
                text = _str(getter(name))
            except Exception:
                text = ""
            if text:
                props[name] = text
                break
    props.setdefault("Reference", _str(fp.GetReference()))
    props.setdefault("Value", _str(fp.GetValue()))
    return props


def _attr_flag(fp, pcbnew, method: str, const_name: str) -> bool:
    fn = getattr(fp, method, None)
    if fn is not None:
        try:
            return bool(fn())
        except Exception:
            pass
    const = getattr(pcbnew, const_name, None)
    if const is not None:
        try:
            return bool(fp.GetAttributes() & const)
        except Exception:
            pass
    return False


def _orientation(fp) -> float:
    fn = getattr(fp, "GetOrientationDegrees", None)
    if fn is not None:
        return float(fn())
    return float(fp.GetOrientation().AsDegrees())


def _pad_number(pad) -> str:
    fn = getattr(pad, "GetNumber", None) or getattr(pad, "GetName", None)
    return _str(fn()) if fn is not None else ""


def _pad_function(pad) -> str:
    fn = getattr(pad, "GetPinFunction", None)
    if fn is None:
        return ""
    try:
        return _str(fn())
    except Exception:
        return ""


def footprint_record(fp, pcbnew=None) -> FootprintRecord:
    pcbnew = _pcbnew(pcbnew)
    pos = fp.GetPosition()
    flipped = getattr(fp, "IsFlipped", None)
    is_bottom = bool(flipped()) if flipped is not None else fp.GetLayer() == pcbnew.B_Cu
    pads = []
    for pad in fp.Pads():
        ppos = pad.GetPosition()
        pads.append(PadRecord(number=_pad_number(pad), x=_mm(ppos.x), y=_mm(ppos.y), function=_pad_function(pad)))
    return FootprintRecord(
        reference=_str(fp.GetReference()),
        value=_str(fp.GetValue()),
        footprint=_str(fp.GetFPID().GetLibItemName()),
        x=_mm(pos.x),
        y=_mm(pos.y),
        orientation=_orientation(fp),
        side=BOTTOM if is_bottom else TOP,
        pads=pads,
        properties=_footprint_properties(fp),
        dnp=_attr_flag(fp, pcbnew, "IsDNP", "FP_DNP"),
        exclude_from_bom=_attr_flag(fp, pcbnew, "IsExcludedFromBOM", "FP_EXCLUDE_FROM_BOM"),
        exclude_from_pos=_attr_flag(fp, pcbnew, "IsExcludedFromPosFiles", "FP_EXCLUDE_FROM_POS_FILES"),
    )


def extract_footprints(board, pcbnew=None) -> List[FootprintRecord]:
    """Records for every footprint on *board* (must run on the UI thread)."""
    pcbnew = _pcbnew(pcbnew)
    return [footprint_record(fp, pcbnew) for fp in board.GetFootprints()]


# ---------------------------------------------------------------------------
# Gerbers and drill
# ---------------------------------------------------------------------------


def gerber_layers(board, pcbnew=None) -> List[Tuple[int, str, str]]:
    """(layer id, suffix, description) for every layer JLCPCB needs."""
    pcbnew = _pcbnew(pcbnew)
    layers = []
    copper = int(board.GetCopperLayerCount())
    for const, suffix, desc in FIXED_GERBER_LAYERS[:2]:
        layers.append((getattr(pcbnew, const), suffix, desc))
    # Inner layers via named constants: KiCad 9 renumbered layer IDs, so
    # In1_Cu + i arithmetic is not portable.
    for i in range(1, max(copper - 1, 1)):
        const = getattr(pcbnew, f"In{i}_Cu", None)
        if const is not None:
            layers.append((const, f"In{i}_Cu", f"Inner copper {i}"))
    for const, suffix, desc in FIXED_GERBER_LAYERS[2:]:
        layers.append((getattr(pcbnew, const), suffix, desc))
    return layers


def _configure_plot_options(popt, pcbnew, out_dir: str) -> None:
    popt.SetOutputDirectory(out_dir)
    _try_call(popt, "SetFormat", pcbnew.PLOT_FORMAT_GERBER)
    _try_call(popt, "SetPlotFrameRef", False)
    _try_call(popt, "SetAutoScale", False)
    _try_call(popt, "SetScale", 1)
    _try_call(popt, "SetMirror", False)
    _try_call(popt, "SetUseAuxOrigin", False)  # CPL coordinates use the same absolute origin
    _try_call(popt, "SetUseGerberAttributes", True)
    _try_call(popt, "SetIncludeGerberNetlistInfo", True)
    _try_call(popt, "SetUseGerberProtelExtensions", True)
    _try_call(popt, "SetCreateGerberJobFile", False)
    _try_call(popt, "SetGerberPrecision", 6)
    _try_call(popt, "SetSubtractMaskFromSilk", True)
    _try_call(popt, "SetExcludeEdgeLayer", True)  # KiCad 8 only
    _try_call(popt, "SetPlotValue", True)
    _try_call(popt, "SetPlotReference", True)
    _try_call(popt, "SetSketchPadsOnFabLayers", False)
    no_drill = getattr(pcbnew, "DRILL_MARKS_NO_DRILL_SHAPE", None)
    if no_drill is None:
        params = getattr(pcbnew, "PCB_PLOT_PARAMS", None)
        no_drill = getattr(params, "NO_DRILL_SHAPE", None)
    if no_drill is not None:
        _try_call(popt, "SetDrillMarksType", no_drill)


def plot_gerbers(board, out_dir: str, pcbnew=None) -> List[str]:
    """Plot all JLCPCB layers into *out_dir*.  Returns the written file paths."""
    pcbnew = _pcbnew(pcbnew)
    os.makedirs(out_dir, exist_ok=True)
    pctl = pcbnew.PLOT_CONTROLLER(board)
    _configure_plot_options(pctl.GetPlotOptions(), pcbnew, out_dir)
    files = []
    try:
        for layer, suffix, desc in gerber_layers(board, pcbnew):
            pctl.SetLayer(layer)
            if not pctl.OpenPlotfile(suffix, pcbnew.PLOT_FORMAT_GERBER, desc):
                raise RuntimeError(f"Could not open plot file for {suffix}")
            files.append(_str(pctl.GetPlotFileName()))
            pctl.PlotLayer()
    finally:
        pctl.ClosePlot()
    return files


def write_drill_files(board, out_dir: str, pcbnew=None) -> List[str]:
    """Excellon drill files (mm, separate PTH/NPTH).  Returns the written file paths.

    KiCad picks the file names, so the writer runs in an empty staging folder
    and whatever ``.drl`` files appear there are moved into *out_dir*.
    """
    pcbnew = _pcbnew(pcbnew)
    os.makedirs(out_dir, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".drill-", dir=out_dir)
    try:
        writer = pcbnew.EXCELLON_WRITER(board)
        origin_cls = getattr(pcbnew, "VECTOR2I", None) or pcbnew.wxPoint
        writer.SetOptions(False, False, origin_cls(0, 0), False)  # mirror, minimal header, offset, merge NPTH
        writer.SetFormat(True)  # metric
        writer.CreateDrillandMapFilesSet(staging, True, False)
        files = []
        for name in sorted(os.listdir(staging)):
            if name.lower().endswith(".drl"):
                dest = os.path.join(out_dir, name)
                os.replace(os.path.join(staging, name), dest)
                files.append(dest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if not files:
        raise RuntimeError("KiCad wrote no drill files")
    return files


def refill_zones(board, pcbnew=None) -> None:
    """Refill every zone on *board* (the open board) and redraw the editor.

    pcbnew's Python API has no commit/undo for this, so it cannot be undone;
    the panel says so next to the checkbox.
    """
    pcbnew = _pcbnew(pcbnew)
    if pcbnew.ZONE_FILLER(board).Fill(board.Zones()) is False:
        raise RuntimeError("KiCad could not refill the zones; Gerbers were not plotted")
    _try_call(pcbnew, "Refresh")


def generate_fabrication_zip(
    board, out_dir: str, basename: str, refill: bool = True, pcbnew=None
) -> Tuple[str, List[str]]:
    """Plot Gerbers + drill into ``out_dir/gerbers`` and zip them as ``<basename>-gerbers.zip``."""
    pcbnew = _pcbnew(pcbnew)
    if refill:
        refill_zones(board, pcbnew)
    gerber_dir = os.path.join(out_dir, "gerbers")
    files = plot_gerbers(board, gerber_dir, pcbnew) + write_drill_files(board, gerber_dir, pcbnew)
    zip_path = zip_files(files, os.path.join(out_dir, f"{basename}-gerbers.zip"))
    return zip_path, files


def board_path(board) -> Optional[str]:
    path = _str(board.GetFileName())
    return path or None


def current_board(fallback=None, pcbnew=None):
    """The board open in the editor now, else *fallback*.

    pcbnew replaces its BOARD object on File > Revert or Open, so a board
    held since the dialog opened may already be deleted.
    """
    try:
        board = _pcbnew(pcbnew).GetBoard()
    except Exception:
        board = None
    return board or fallback
