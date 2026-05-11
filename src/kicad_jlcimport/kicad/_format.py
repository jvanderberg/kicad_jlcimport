"""Shared formatting utilities for KiCad file output."""

import math
import uuid as _uuid_mod

_GRID_SNAP_TOLERANCE_MM = 0.000125
_SNAP_GRIDS_MM = (0.001, 0.00254)  # 1 um metric, 0.1 mil imperial


def gen_uuid() -> str:
    """Generate a random UUID string for KiCad elements."""
    return str(_uuid_mod.uuid4())


def _snap_grid_noise(v: float) -> float:
    """Snap tiny EasyEDA conversion noise to common metric or imperial grids."""
    snapped = v
    best_delta = float("inf")
    for grid in _SNAP_GRIDS_MM:
        candidate = round(v / grid) * grid
        delta = abs(v - candidate)
        if delta < best_delta:
            snapped = candidate
            best_delta = delta
    if best_delta <= _GRID_SNAP_TOLERANCE_MM:
        return snapped
    return v


def fmt_float(v: float) -> str:
    """Format a float for KiCad S-expression output.

    Returns integers without decimals, otherwise up to 6 decimal places
    with trailing zeros stripped. NaN/Inf values are clamped to 0.
    """
    if math.isnan(v) or math.isinf(v):
        return "0"
    if v == int(v) and abs(v) < 1e10:
        return str(int(v))
    return f"{v:.6f}".rstrip("0").rstrip(".")


def fmt_geometry(v: float) -> str:
    """Format a KiCad coordinate or dimension, snapping tiny grid noise."""
    if math.isnan(v) or math.isinf(v):
        return "0"
    return fmt_float(_snap_grid_noise(v))


def escape_sexpr(s: str) -> str:
    """Escape special characters for S-expression string values."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
