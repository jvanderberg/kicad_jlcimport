#!/usr/bin/env python3
"""Fetch JLCPCB parts, render EasyEDA and KiCad SVGs, and generate an HTML comparison page."""

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_jlcimport.easyeda.api import fetch_component_uuids, fetch_full_component
from kicad_jlcimport.importer import import_component
from kicad_jlcimport.kicad.library import sanitize_name

KICAD_CLI = shutil.which("kicad-cli") or ""


def fetch_easyeda_svgs(lcsc_id: str) -> dict:
    """Fetch EasyEDA preview SVGs for a part.

    Returns dict with 'symbol_svg' and 'footprint_svg' strings (or None).
    Multi-unit symbols use docType 6 for subpart SVGs; docType 2 often has
    bogus dimensions (negative width or Infinity), so prefer docType 6.
    """
    uuids = fetch_component_uuids(lcsc_id)
    symbol_svg = None
    footprint_svg = None
    for entry in uuids:
        doc_type = entry.get("docType")
        svg = entry.get("svg")
        if doc_type in (2, 6) and symbol_svg is None and svg:
            # Skip SVGs with bogus dimensions (negative width, Infinity)
            if 'width="-' not in svg and 'width="Infinity' not in svg:
                symbol_svg = svg
        elif doc_type == 4 and footprint_svg is None:
            footprint_svg = svg
    return {"symbol_svg": symbol_svg, "footprint_svg": footprint_svg}


def convert_to_kicad(comp: dict, tmp_dir: str) -> dict:
    """Convert component data to KiCad files via the shared importer.

    Uses import_component(export_only=True) so that the compare tool
    exercises the exact same parsing/writing code path as a real import.
    Returns dict with file paths, sanitized name, and metadata.
    """
    lcsc_id = comp["lcsc_id"]
    title = comp.get("title", lcsc_id)
    name = sanitize_name(title)

    result = {
        "name": name,
        "title": title,
        "lcsc_id": lcsc_id,
        "prefix": comp.get("prefix", ""),
        "manufacturer": comp.get("manufacturer", ""),
        "manufacturer_part": comp.get("manufacturer_part", ""),
        "datasheet": comp.get("datasheet", ""),
        "description": comp.get("description", ""),
        "sym_file": None,
        "fp_file": None,
    }

    import_result = import_component(
        lcsc_id,
        tmp_dir,
        "compare",
        export_only=True,
        log=lambda msg: None,
        component_data=comp,
        symbol_kwargs={"include_pin_dots": True, "hide_properties": True},
    )

    if import_result is None:
        return result

    # import_component(export_only=True) writes files to tmp_dir:
    #   {name}.kicad_sym, {name}.kicad_mod, 3dmodels/{name}.wrl
    sym_path = Path(tmp_dir) / f"{name}.kicad_sym"
    if sym_path.exists():
        result["sym_file"] = str(sym_path)

    fp_path = Path(tmp_dir) / f"{name}.kicad_mod"
    if fp_path.exists():
        # kicad-cli requires .kicad_mod inside a .pretty directory
        pretty_dir = Path(tmp_dir) / f"{lcsc_id}.pretty"
        pretty_dir.mkdir(exist_ok=True)
        dest = pretty_dir / f"{name}.kicad_mod"
        shutil.copy2(str(fp_path), str(dest))
        result["fp_file"] = str(dest)
        result["pretty_dir"] = str(pretty_dir)

    return result


def _estimate_footprint_bounds(footprint_content: str) -> tuple:
    """Estimate footprint bounding box from coordinate values in the content.

    Returns (min_x, min_y, max_x, max_y) or None if no coordinates found.
    """
    # Find all coordinate patterns: (at x y), (start x y), (end x y), (xy x y)
    coords = re.findall(r"\((?:at|start|end|xy)\s+([-\d.]+)\s+([-\d.]+)", footprint_content)
    if not coords:
        return None
    xs = [float(c[0]) for c in coords]
    ys = [float(c[1]) for c in coords]
    return (min(xs), min(ys), max(xs), max(ys))


def _create_minimal_pcb(footprint_content: str) -> str:
    """Create a minimal .kicad_pcb containing a footprint for SVG export.

    This allows using 'pcb export svg' which supports --drill-shape-opt
    for rendering drill holes, unlike 'fp export svg'.
    """
    # Calculate Edge.Cuts with 25% padding based on footprint bounds
    bounds = _estimate_footprint_bounds(footprint_content)
    edge_cuts = ""
    if bounds:
        min_x, min_y, max_x, max_y = bounds
        width = max_x - min_x
        height = max_y - min_y
        pad_x = max(width * 0.25, 1.0)  # At least 1mm padding
        pad_y = max(height * 0.25, 1.0)
        edge_cuts = (
            f"  (gr_rect (start {min_x - pad_x:.4f} {min_y - pad_y:.4f}) "
            f"(end {max_x + pad_x:.4f} {max_y + pad_y:.4f}) "
            f'(layer "Edge.Cuts") (stroke (width 0.1) (type solid)))\n'
        )

    pcb_header = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad_jlcimport")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
  (setup
    (pad_to_mask_clearance 0.05)
  )
  (net 0 "")
"""
    # Inject (at 0 0 0) into the footprint to make it a placed footprint
    # This ensures kicad-cli renders it the same as the interactive viewer
    footprint_with_at = re.sub(r'(\(footprint "[^"]*"\s*\n)', r"\1  (at 0 0 0)\n", footprint_content, count=1)

    return pcb_header + edge_cuts + footprint_with_at + "\n)"


def render_kicad_svgs(kicad_files: dict, tmp_dir: str) -> dict:
    """Render KiCad files to SVG using kicad-cli.

    Returns dict with 'symbol_svg' and 'footprint_svg' strings (or None).
    """
    result = {"symbol_svg": None, "footprint_svg": None}

    # Symbol SVG — kicad-cli sym export matches by the sanitized symbol name
    # stored inside the .kicad_sym file (same name the importer uses).
    if kicad_files.get("sym_file"):
        sym_svg_dir = Path(tmp_dir) / "sym_svg"
        sym_svg_dir.mkdir(exist_ok=True)
        cmd = [
            KICAD_CLI,
            "sym",
            "export",
            "svg",
            "--symbol",
            kicad_files["name"],
            "--output",
            str(sym_svg_dir),
            kicad_files["sym_file"],
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            svg_files = sorted(sym_svg_dir.glob("*.svg"))
            if svg_files:
                # For multi-unit symbols kicad-cli emits one SVG per unit.
                # Collect all of them so the comparison page can show every unit.
                result["symbol_svg"] = svg_files[0].read_text()
                if len(svg_files) > 1:
                    result["symbol_svgs"] = [f.read_text() for f in svg_files]
        if not result["symbol_svg"]:
            print(f"  Warning: kicad-cli sym export failed: {proc.stderr.strip()}")

    # Footprint SVG — use pcb export instead of fp export to get drill holes rendered.
    # We create a minimal .kicad_pcb containing the footprint and export that.
    if kicad_files.get("fp_file"):
        fp_svg_dir = Path(tmp_dir) / "fp_svg"
        fp_svg_dir.mkdir(exist_ok=True)

        # Create minimal PCB file with the footprint embedded
        fp_content = Path(kicad_files["fp_file"]).read_text()
        pcb_content = _create_minimal_pcb(fp_content)
        pcb_file = Path(tmp_dir) / "footprint.kicad_pcb"
        pcb_file.write_text(pcb_content)

        svg_output = fp_svg_dir / "footprint.svg"
        cmd = [
            KICAD_CLI,
            "pcb",
            "export",
            "svg",
            "--layers",
            "F.Mask,F.Cu,F.SilkS,F.Fab",
            "--drill-shape-opt",
            "2",
            "--page-size-mode",
            "2",
            "--exclude-drawing-sheet",
            "--theme",
            "user",
            "--output",
            str(svg_output),
            str(pcb_file),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and svg_output.exists():
            svg_content = svg_output.read_text()
            result["footprint_svg"] = _add_board_background(svg_content)
        if not result["footprint_svg"]:
            print(f"  Warning: kicad-cli pcb export failed: {proc.stderr.strip()}")

    return result


def clean_svg_for_inline(svg: str) -> str:
    """Strip XML declaration and DOCTYPE (invalid in inline HTML5)."""
    svg = re.sub(r"<\?xml[^?]*\?>", "", svg)
    svg = re.sub(r"<!DOCTYPE[^>]*>", "", svg)
    return svg.lstrip()


def _add_board_background(svg: str, color: str = "#001023") -> str:
    """Add a dark board background rect to the SVG based on its viewBox."""
    match = re.search(r'viewBox="([^"]+)"', svg)
    if not match:
        return svg
    parts = match.group(1).split()
    if len(parts) != 4:
        return svg
    x, y, w, h = parts
    bg_rect = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{color}"/>'
    # Insert after </desc> if present, otherwise after opening <svg> tag
    if "</desc>" in svg:
        svg = re.sub(r"(</desc>)", rf"\1\n{bg_rect}", svg, count=1)
    else:
        svg = re.sub(r"(<svg[^>]*>)", rf"\1\n{bg_rect}", svg, count=1)
    return svg


def render_3d_model(
    footprint_content: str,
    vrml_path: str,
    tmp_dir: str,
    model_offset: tuple,
    model_rotation: tuple,
    view: str = "oblique",
) -> Optional[str]:
    """Render 3D model snapshot using kicad-cli.

    Args:
        footprint_content: KiCad footprint content
        vrml_path: Path to VRML model file
        tmp_dir: Temporary directory for intermediate files
        model_offset: (x, y, z) offset tuple
        model_rotation: (x, y, z) rotation tuple
        view: View type - "top", "bottom", or "oblique" (default)

    Returns base64-encoded PNG data or None if rendering fails.
    """
    try:
        # Parse footprint to inject 3D model reference
        # Look for closing parenthesis before the end of the footprint
        if "(model " not in footprint_content:
            # Insert model reference with computed offsets before the final closing paren
            model_ref = f"""  (model "{vrml_path}"
    (offset (xyz {model_offset[0]} {model_offset[1]} {model_offset[2]}))
    (scale (xyz 1 1 1))
    (rotate (xyz {model_rotation[0]} {model_rotation[1]} {model_rotation[2]}))
  )
"""
            # Find the last closing paren and insert before it
            last_paren = footprint_content.rfind(")")
            if last_paren != -1:
                footprint_content = footprint_content[:last_paren] + model_ref + footprint_content[last_paren:]

        # Resolve relative model paths to absolute — kicad-cli doesn't resolve
        # them from the PCB file's directory.
        def _abs_model(m):
            path = m.group(1)
            if not os.path.isabs(path):
                path = os.path.join(tmp_dir, path)
            return f'(model "{path}"'

        footprint_content = re.sub(r'\(model "([^"]+)"', _abs_model, footprint_content)

        # Create minimal PCB with the footprint (including 3D model)
        pcb_content = _create_minimal_pcb(footprint_content)
        pcb_file = Path(tmp_dir) / f"render_3d_{view}.kicad_pcb"
        pcb_file.write_text(pcb_content)

        # Determine rotation based on view
        # All views use oblique angle from investigation, just from different sides
        if view == "top":
            rotation = "120,180,30"  # Oblique view from top (from investigation)
        elif view == "bottom":
            rotation = "120,0,30"  # Oblique view from bottom (flip Y axis)
        else:  # oblique (default top)
            rotation = "120,180,30"  # Oblique view from investigation

        # Render with optimal settings from investigation
        png_output = Path(tmp_dir) / f"render_3d_{view}.png"
        cmd = [
            KICAD_CLI,
            "pcb",
            "render",
            str(pcb_file),
            "--output",
            str(png_output),
            "--rotate",
            rotation,
            "--width",
            "800",
            "--height",
            "600",
            "--background",
            "transparent",
            "--quality",
            "high",
            "--preset",
            "follow_pcb_editor",
            "--floor",
            "--zoom",
            "1.2",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)

        if proc.returncode == 0 and png_output.exists():
            # Read PNG and convert to base64 data URI
            import base64

            png_data = png_output.read_bytes()
            b64_data = base64.b64encode(png_data).decode("utf-8")
            return f"data:image/png;base64,{b64_data}"
        else:
            print(f"  Warning: 3D render ({view}) failed: {proc.stderr.strip()}")
            return None
    except Exception as e:
        print(f"  Error rendering 3D model ({view}): {e}")
        return None


def generate_html(parts: list) -> str:
    """Build an HTML comparison page for all parts."""
    rows = []
    for part in parts:
        meta = part["metadata"]
        easyeda = part["easyeda_svgs"]
        kicad = part["kicad_svgs"]

        # Metadata header
        meta_parts = [f"LCSC: {html.escape(meta['lcsc_id'])}"]
        if meta.get("prefix"):
            meta_parts.append(f"Prefix: {html.escape(meta['prefix'])}")
        if meta.get("manufacturer"):
            meta_parts.append(f"Mfr: {html.escape(meta['manufacturer'])}")
        if meta.get("manufacturer_part"):
            meta_parts.append(html.escape(meta["manufacturer_part"]))
        datasheet_link = ""
        if meta.get("datasheet"):
            ds_url = html.escape(meta["datasheet"])
            datasheet_link = f' | <a href="{ds_url}" target="_blank">Datasheet</a>'

        title_text = html.escape(meta.get("title", meta["lcsc_id"]))
        meta_line = " | ".join(meta_parts) + datasheet_link

        # SVG cells
        def svg_cell(svg, label):
            if svg:
                cleaned = clean_svg_for_inline(svg)
                inner = (
                    "<html><head><style>"
                    "body{margin:0;display:flex;align-items:center;"
                    "justify-content:center;width:100vw;height:100vh;overflow:hidden}"
                    "svg{width:100%;height:100%;}"
                    "</style></head><body>"
                    f"{cleaned}</body></html>"
                )
                srcdoc = html.escape(inner, quote=True)
                return (
                    f'<div class="svg-cell">'
                    f'<iframe srcdoc="{srcdoc}" '
                    f'style="width:100%;aspect-ratio:1;border:none;" '
                    f'scrolling="no"></iframe></div>'
                )
            return f'<div class="svg-cell empty">{html.escape(label)}</div>'

        # For multi-unit symbols, show each KiCad unit side by side with the
        # single EasyEDA preview SVG.
        kicad_sym_svgs = kicad.get("symbol_svgs") or ([kicad["symbol_svg"]] if kicad.get("symbol_svg") else [])
        if len(kicad_sym_svgs) <= 1:
            symbol_row = (
                f'<div class="compare-row">'
                f'<div class="row-label">Symbol</div>'
                f'<div class="row-pair">'
                f'<div class="col"><div class="col-label">EasyEDA</div>'
                f"{svg_cell(easyeda.get('symbol_svg'), 'No SVG')}</div>"
                f'<div class="col"><div class="col-label">KiCad</div>'
                f"{svg_cell(kicad.get('symbol_svg'), 'Render failed')}</div>"
                f"</div></div>"
            )
        else:
            kicad_cols = ""
            for i, svg in enumerate(kicad_sym_svgs):
                kicad_cols += (
                    f'<div class="col"><div class="col-label">KiCad Unit {i + 1}</div>'
                    f"{svg_cell(svg, 'Render failed')}</div>"
                )
            symbol_row = (
                f'<div class="compare-row">'
                f'<div class="row-label">Symbol</div>'
                f'<div class="row-pair">'
                f'<div class="col"><div class="col-label">EasyEDA</div>'
                f"{svg_cell(easyeda.get('symbol_svg'), 'No SVG')}</div>"
                f"{kicad_cols}"
                f"</div></div>"
            )

        footprint_row = (
            f'<div class="compare-row">'
            f'<div class="row-label">Footprint</div>'
            f'<div class="row-pair">'
            f'<div class="col"><div class="col-label">EasyEDA</div>'
            f"{svg_cell(easyeda.get('footprint_svg'), 'No SVG')}</div>"
            f'<div class="col"><div class="col-label">KiCad</div>'
            f"{svg_cell(kicad.get('footprint_svg'), 'Render failed')}</div>"
            f"</div></div>"
        )

        # 3D Model row (top and bottom views)
        model_3d = kicad.get("model_3d")
        model_row = ""
        if model_3d and isinstance(model_3d, dict):
            model_top = model_3d.get("top")
            model_bottom = model_3d.get("bottom")
            if model_top and model_bottom:
                model_row = (
                    f'<div class="compare-row">'
                    f'<div class="row-label">3D Model</div>'
                    f'<div class="row-pair">'
                    f'<div class="col"><div class="col-label">Top View</div>'
                    f'<div class="svg-cell"><img src="{model_top}" style="width:100%;height:auto;" alt="3D Model Top"></div></div>'
                    f'<div class="col"><div class="col-label">Bottom View</div>'
                    f'<div class="svg-cell"><img src="{model_bottom}" style="width:100%;height:auto;" alt="3D Model Bottom"></div></div>'
                    f"</div></div>"
                )

        rows.append(
            f'<div class="part">'
            f"<h2>{title_text}</h2>"
            f'<div class="meta">{meta_line}</div>'
            f"{symbol_row}{footprint_row}{model_row}"
            f"</div>"
        )

    parts_html = "\n".join(rows)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EasyEDA vs KiCad Comparison</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 1em; background: #f5f5f5; color: #222; }}
  .part {{ background: #fff; border-radius: 8px; padding: 1em; margin-bottom: 1.5em;
           box-shadow: 0 1px 3px rgba(0,0,0,0.12);
           content-visibility: auto; contain-intrinsic-size: auto none; }}
  h2 {{ margin: 0 0 0.3em; font-size: 1.2em; }}
  .meta {{ color: #666; margin-bottom: 1em; font-size: 0.85em; }}
  .meta a {{ color: #0066cc; }}
  .compare-row {{ margin-bottom: 1.5em; }}
  .row-label {{ font-weight: 600; font-size: 1.1em; margin-bottom: 0.5em; }}
  .row-pair {{ display: flex; gap: 1em; }}
  .col {{ flex: 1; min-width: 0; }}
  .col-label {{ text-align: center; font-size: 0.85em; color: #888;
                margin-bottom: 0.3em; text-transform: uppercase; letter-spacing: 0.05em; }}
  .svg-cell {{ border: 1px solid #ddd; border-radius: 4px; padding: 0.5em;
               text-align: center; min-height: 100px; background: #fafafa;
               display: flex; align-items: center; justify-content: center; }}
  .svg-cell.empty {{ color: #999; font-style: italic; }}
  @media (max-width: 600px) {{
    .row-pair {{ flex-direction: column; }}
  }}
</style>
</head>
<body>
<h1>EasyEDA vs KiCad Comparison</h1>
{parts_html}
</body>
</html>"""


def compare_part(lcsc_id: str, tmp_dir: str, verbose: bool = False) -> dict:
    """Fetch, convert, and render a single part for comparison."""
    start_time = time.time()
    if verbose:
        print(f"\n--- {lcsc_id} ---")

    # Fetch EasyEDA preview SVGs
    if verbose:
        print("  Fetching EasyEDA SVGs...")
    try:
        easyeda_svgs = fetch_easyeda_svgs(lcsc_id)
    except Exception as e:
        if verbose:
            print(f"  Error fetching EasyEDA SVGs: {e}")
        easyeda_svgs = {"symbol_svg": None, "footprint_svg": None}

    # Fetch full component and convert to KiCad
    if verbose:
        print("  Fetching component data...")
    try:
        comp = fetch_full_component(lcsc_id)
    except Exception as e:
        if verbose:
            print(f"  Error fetching component: {e}")
        return {
            "metadata": {"lcsc_id": lcsc_id, "title": lcsc_id},
            "easyeda_svgs": easyeda_svgs,
            "kicad_svgs": {"symbol_svg": None, "footprint_svg": None},
        }

    part_dir = os.path.join(tmp_dir, lcsc_id)
    os.makedirs(part_dir, exist_ok=True)

    if verbose:
        print("  Converting to KiCad...")
    kicad_files = convert_to_kicad(comp, part_dir)

    # Render KiCad SVGs
    if verbose:
        print("  Rendering KiCad SVGs...")
    kicad_svgs = render_kicad_svgs(kicad_files, part_dir)

    # Render 3D model if available.
    # import_component(export_only=True) already downloaded the WRL and wrote the
    # footprint with the model reference + offsets embedded, so we just need to
    # find the WRL file and render the footprint that already references it.
    if verbose:
        print("  Checking for 3D model...")
    model_3d = None
    try:
        name = kicad_files["name"]
        wrl_path = Path(part_dir) / "3dmodels" / f"{name}.wrl"
        if wrl_path.exists() and kicad_files.get("fp_file"):
            if verbose:
                print(f"  Found 3D model: {wrl_path}")
                print("  Rendering 3D snapshots (top and bottom views)...")
            fp_content = Path(kicad_files["fp_file"]).read_text()
            # The footprint already has the model reference with correct offsets;
            # render_3d_model will skip injection when it finds "(model " present.
            # Dummy offset/rotation values won't be used.
            model_3d_top = render_3d_model(fp_content, "", part_dir, (0, 0, 0), (0, 0, 0), "top")
            model_3d_bottom = render_3d_model(fp_content, "", part_dir, (0, 0, 0), (0, 0, 0), "bottom")
            if model_3d_top and model_3d_bottom:
                model_3d = {"top": model_3d_top, "bottom": model_3d_bottom}
                if verbose:
                    print("  3D models rendered successfully")
            elif verbose:
                print("  Warning: Some 3D renders failed")
        elif verbose:
            print("  No 3D model found")
    except Exception as e:
        if verbose:
            print(f"  Error processing 3D model: {e}")

    kicad_svgs["model_3d"] = model_3d

    metadata = {
        "lcsc_id": lcsc_id,
        "title": kicad_files["title"],
        "prefix": kicad_files["prefix"],
        "manufacturer": kicad_files["manufacturer"],
        "manufacturer_part": kicad_files["manufacturer_part"],
        "datasheet": kicad_files["datasheet"],
    }

    elapsed = time.time() - start_time
    return {
        "metadata": metadata,
        "easyeda_svgs": easyeda_svgs,
        "kicad_svgs": kicad_svgs,
        "_elapsed": elapsed,
    }


def main():
    parser = argparse.ArgumentParser(description="Compare EasyEDA and KiCad renderings of JLCPCB parts")
    parser.add_argument("part_ids", nargs="+", help="LCSC part numbers (e.g. C427602)")
    parser.add_argument("--no-open", action="store_true", help="Don't open the HTML in a browser")
    parser.add_argument("--output-dir", help="Write HTML to this directory instead of a temp dir")
    parser.add_argument("--workers", type=int, default=20, help="Number of parallel workers (default: 20)")
    parser.add_argument("--verbose", action="store_true", help="Show detailed progress for each part")
    args = parser.parse_args()

    # Check kicad-cli exists
    if not os.path.isfile(KICAD_CLI):
        print(f"Error: kicad-cli not found at {KICAD_CLI}")
        sys.exit(1)

    tmp_dir = tempfile.mkdtemp(prefix="kicad_compare_")
    print(f"Working directory: {tmp_dir}")
    print(f"Processing {len(args.part_ids)} parts with {args.workers} workers...")

    if len(args.part_ids) > args.workers:
        batches = (len(args.part_ids) + args.workers - 1) // args.workers
        print(f"(will process in ~{batches} batches)")

    # Process parts in parallel
    parts = [None] * len(args.part_ids)  # Preserve order
    overall_start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        # Submit all jobs
        future_to_idx = {
            executor.submit(compare_part, part_id, tmp_dir, args.verbose): idx
            for idx, part_id in enumerate(args.part_ids)
        }
        print(f"Started {len(args.part_ids)} jobs...")

        # Collect results as they complete
        completed = 0
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            part_id = args.part_ids[idx]
            try:
                result = future.result()
                parts[idx] = result
                completed += 1
                elapsed = result.get("_elapsed", 0)
                if not args.verbose:
                    print(f"  [{completed}/{len(args.part_ids)}] {part_id} ({elapsed:.1f}s)")
            except Exception as e:
                print(f"  [{completed}/{len(args.part_ids)}] Error: {part_id}: {e}")
                parts[idx] = {
                    "metadata": {"lcsc_id": part_id, "title": part_id},
                    "easyeda_svgs": {"symbol_svg": None, "footprint_svg": None},
                    "kicad_svgs": {"symbol_svg": None, "footprint_svg": None},
                }
                completed += 1

    total_elapsed = time.time() - overall_start
    print(
        f"\nCompleted {len(args.part_ids)} parts in {total_elapsed:.1f}s ({total_elapsed / len(args.part_ids):.1f}s avg)"
    )

    # Generate HTML
    html_content = generate_html(parts)

    if args.output_dir:
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        html_path = str(out_dir / "index.html")
    else:
        html_path = os.path.join(tmp_dir, "comparison.html")

    with open(html_path, "w") as f:
        f.write(html_content)

    print(f"\nHTML written to: {html_path}")

    if not args.output_dir and not args.no_open:
        webbrowser.open(f"file://{html_path}")


if __name__ == "__main__":
    main()
