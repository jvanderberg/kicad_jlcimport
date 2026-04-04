"""Parse KiCad .kicad_mod files into geometry dicts for preview rendering."""

from __future__ import annotations

import math
import os
import re

from .library import resolve_kicad_var
from .version import DEFAULT_KICAD_VERSION


def _extract_blocks(text: str, keyword: str) -> list:
    """Extract all top-level s-expression blocks starting with (keyword ...}.

    Uses bracket-counting so nested parens inside a block don't confuse the
    extractor — essential for pad blocks which may contain drill/primitives.
    """
    results = []
    pattern = re.compile(rf"\({re.escape(keyword)}\b")
    for m in pattern.finditer(text):
        start = m.start()
        depth = 0
        i = start
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    results.append(text[start : i + 1])
                    break
            i += 1
    return results


def _parse_kicad_mod(path: str, project_dir: str = "", kicad_version: int = DEFAULT_KICAD_VERSION) -> dict:
    """Parse a KiCad 8/9/10 .kicad_mod file into geometry lists for preview rendering.

    Returns a dict with keys:
      lines   – list of ((x1,y1),(x2,y2), layer, width_mm)
      rects   – list of (x1,y1, x2,y2, layer, width_mm, corner_r, filled)
      circles – list of (cx, cy, r, layer, width_mm, filled)
      arcs    – list of (sx,sy, mx,my, ex,ey, layer, width_mm)
      polys   – list of ([(x,y),...], layer, filled)
      pads    – list of (num, x, y, w, h, shape, rotation, pad_type, drill_d)
      model   – (path_str, exists: bool) or None
      descr, tags – strings
      pads_count  – int

    KiCad 8/9/10 format only (quoted layer names, stroke blocks).
    """
    N = r"[\d.eE+\-]+"

    result: dict = {
        "lines": [],
        "rects": [],
        "circles": [],
        "arcs": [],
        "polys": [],
        "pads": [],
        "model": None,
        "descr": "",
        "tags": "",
        "pads_count": 0,
    }
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return result

    def _f(s) -> float:
        try:
            return float(s)
        except (ValueError, TypeError):
            return 0.0

    def _field(name: str) -> str:
        m = re.search(rf'\({name}\s+"([^"]*)"\)', text)
        return m.group(1) if m else ""

    result["descr"] = _field("descr")
    result["tags"] = _field("tags")

    def _layer(block: str) -> str:
        m = re.search(r'\(layer\s+"([^"]+)"\)', block)
        return m.group(1) if m else ""

    def _sw(block: str) -> float:
        m = re.search(rf"\(stroke\s*\(width\s+({N})\)", block, re.DOTALL)
        return _f(m.group(1)) if m else 0.1

    # ── fp_line ─────────────────────────────────────────────────────────
    for block in _extract_blocks(text, "fp_line"):
        c = re.search(
            rf"\(start\s+({N})\s+({N})\)\s*\(end\s+({N})\s+({N})\)",
            block,
            re.DOTALL,
        )
        layer = _layer(block)
        if c and layer:
            result["lines"].append(
                (
                    (_f(c.group(1)), _f(c.group(2))),
                    (_f(c.group(3)), _f(c.group(4))),
                    layer,
                    _sw(block),
                )
            )

    # ── fp_rect (KiCad 8+) ──────────────────────────────────────────────
    for block in _extract_blocks(text, "fp_rect"):
        s = re.search(rf"\(start\s+({N})\s+({N})\)", block)
        e = re.search(rf"\(end\s+({N})\s+({N})\)", block)
        layer = _layer(block)
        if s and e and layer:
            cr = re.search(rf"\(corner_radius\s+({N})\)", block)
            result["rects"].append(
                (
                    _f(s.group(1)),
                    _f(s.group(2)),
                    _f(e.group(1)),
                    _f(e.group(2)),
                    layer,
                    _sw(block),
                    _f(cr.group(1)) if cr else 0.0,
                    "(fill solid)" in block or "(fill yes)" in block,
                )
            )

    # ── fp_circle ───────────────────────────────────────────────────────
    for block in _extract_blocks(text, "fp_circle"):
        cx_m = re.search(rf"\(center\s+({N})\s+({N})\)", block)
        en_m = re.search(rf"\(end\s+({N})\s+({N})\)", block)
        layer = _layer(block)
        if cx_m and en_m and layer:
            cx, cy = _f(cx_m.group(1)), _f(cx_m.group(2))
            r = math.hypot(_f(en_m.group(1)) - cx, _f(en_m.group(2)) - cy)
            result["circles"].append((cx, cy, r, layer, _sw(block), "(fill solid)" in block or "(fill yes)" in block))

    # ── fp_arc (start/mid/end, KiCad 8+) ────────────────────────────────
    for block in _extract_blocks(text, "fp_arc"):
        layer = _layer(block)
        if not layer:
            continue
        s = re.search(rf"\(start\s+({N})\s+({N})\)", block)
        m = re.search(rf"\(mid\s+({N})\s+({N})\)", block)
        e = re.search(rf"\(end\s+({N})\s+({N})\)", block)
        if s and m and e:
            result["arcs"].append(
                (
                    _f(s.group(1)),
                    _f(s.group(2)),
                    _f(m.group(1)),
                    _f(m.group(2)),
                    _f(e.group(1)),
                    _f(e.group(2)),
                    layer,
                    _sw(block),
                )
            )

    # ── fp_poly ─────────────────────────────────────────────────────────
    for block in _extract_blocks(text, "fp_poly"):
        layer = _layer(block)
        if not layer:
            continue
        # Extract the (pts ...) sub-block with bracket counting to handle
        # nested (xy ...) parens correctly — a lazy regex stops too early.
        pts_blocks = _extract_blocks(block, "pts")
        if not pts_blocks:
            continue
        pts = [(_f(p.group(1)), _f(p.group(2))) for p in re.finditer(rf"\(xy\s+({N})\s+({N})\)", pts_blocks[0])]
        if pts:
            filled = "(fill solid)" in block or "(fill yes)" in block
            result["polys"].append((pts, layer, filled))

    # ── pads ────────────────────────────────────────────────────────────
    # Use independent re.search calls so attribute order inside a pad block
    # doesn't matter — (drill ...) often appears before (size ...) in
    # ThermalVias footprints, which breaks a single re.match pattern.
    _head_pat = re.compile(r'\(pad\s+"([^"]*)"\s+(\w+)\s+(\w+)', re.DOTALL)
    _at_pat = re.compile(rf"\(at\s+({N})\s+({N})(?:\s+({N}))?\)", re.DOTALL)
    _size_pat = re.compile(rf"\(size\s+({N})\s+({N})\)", re.DOTALL)
    _drill_pat = re.compile(rf"\(drill(?:\s+oval)?\s+({N})", re.DOTALL)
    for block in _extract_blocks(text, "pad"):
        h = _head_pat.search(block)
        a = _at_pat.search(block)
        s = _size_pat.search(block)
        if not (h and a and s):
            continue
        d = _drill_pat.search(block)
        # Custom-shape pads: the (size) field is the ANCHOR shape size (meaningful,
        # not a placeholder). Primitives contain one or more gr_poly blocks, each a
        # separate filled shape. KiCad renders: anchor_shape UNION all primitives.
        # Store as list-of-polygon-point-lists; anchor is drawn separately in renderer.
        poly_list: list = []
        if h.group(3).lower() == "custom":
            prims = _extract_blocks(block, "primitives")
            if prims:
                for gp in _extract_blocks(prims[0], "gr_poly"):
                    pts_blks = _extract_blocks(gp, "pts")
                    src = pts_blks[0] if pts_blks else gp
                    pts = [(_f(m.group(1)), _f(m.group(2))) for m in re.finditer(rf"\(xy\s+({N})\s+({N})\)", src)]
                    if len(pts) >= 2:
                        poly_list.append(pts)
                if not poly_list:
                    pts = [(_f(m.group(1)), _f(m.group(2))) for m in re.finditer(rf"\(xy\s+({N})\s+({N})\)", prims[0])]
                    if len(pts) >= 2:
                        poly_list.append(pts)
        result["pads"].append(
            (
                h.group(1),  # num
                _f(a.group(1)),
                _f(a.group(2)),  # x, y
                _f(s.group(1)),
                _f(s.group(2)),  # w, h
                h.group(3),  # shape
                _f(a.group(3)) if a.group(3) else 0.0,  # rotation
                h.group(2),  # pad_type
                _f(d.group(1)) if d else 0.0,  # drill_d
                poly_list,  # list of polygons (custom pads)
            )
        )

    result["pads_count"] = len(result["pads"])

    # ── 3D model ─────────────────────────────────────────────────────────
    m3d = re.search(r'\(model\s+"([^"]+)"', text)
    if m3d:
        raw_path = m3d.group(1).strip()

        def _resolve_var(m: re.Match) -> str:
            key = m.group(1)
            if key == "KIPRJMOD" and project_dir:
                return project_dir
            return resolve_kicad_var(key, kicad_version) or m.group(0)

        resolved = re.sub(r"\$\{([^}]+)\}", _resolve_var, raw_path)
        exists = os.path.isfile(resolved)
        result["model"] = (raw_path, exists)

    return result
