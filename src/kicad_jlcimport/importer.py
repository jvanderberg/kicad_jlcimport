"""Shared import logic for CLI, TUI, and plugin."""

from __future__ import annotations

import os
from typing import Callable

from .easyeda.api import download_step, download_wrl_source, fetch_full_component
from .easyeda.parser import parse_footprint_shapes, parse_symbol_shapes
from .kicad.footprint_writer import write_footprint
from .kicad.library import (
    add_symbol_to_lib,
    ensure_lib_structure,
    sanitize_name,
    save_footprint,
    update_global_lib_tables,
    update_project_lib_tables,
)
from .kicad.model3d import compute_model_transform, save_models
from .kicad.symbol_writer import write_symbol
from .kicad.version import DEFAULT_KICAD_VERSION, has_generator_version, symbol_format_version


def _build_description(comp: dict) -> str:
    """Build a description from component metadata.

    If the EasyEDA description is empty or just repeats the title,
    synthesize one from manufacturer_part, package, and manufacturer.
    """
    desc = comp.get("description", "")
    title = comp.get("title", "")
    if not desc or desc == title:
        parts = []
        if comp.get("manufacturer_part"):
            parts.append(comp["manufacturer_part"])
        if comp.get("package"):
            parts.append(comp["package"])
        if comp.get("manufacturer"):
            parts.append(comp["manufacturer"])
        desc = "; ".join(parts)
    return desc.strip()


def _build_keywords(comp: dict) -> str:
    """Build ki_keywords from component metadata for KiCad search."""
    terms = set()
    for key in ("lcsc_id", "manufacturer_part", "manufacturer", "package"):
        val = comp.get(key, "")
        if val:
            terms.add(val)
    return " ".join(sorted(terms))


def _check_existing_files(lib_dir: str, lib_name: str, name: str) -> list[str]:
    """Return a list of existing file types (e.g. ["footprint", "symbol", "3D model"])."""
    existing: list[str] = []
    fp_path = os.path.join(lib_dir, f"{lib_name}.pretty", f"{name}.kicad_mod")
    if os.path.exists(fp_path):
        existing.append("footprint")
    sym_path = os.path.join(lib_dir, f"{lib_name}.kicad_sym")
    if os.path.exists(sym_path):
        try:
            with open(sym_path, encoding="utf-8") as f:
                if f'(symbol "{name}"' in f.read():
                    existing.append("symbol")
        except (PermissionError, OSError):
            pass
    models_dir = os.path.join(lib_dir, f"{lib_name}.3dshapes")
    step_path = os.path.join(models_dir, f"{name}.step")
    wrl_path = os.path.join(models_dir, f"{name}.wrl")
    if os.path.exists(step_path) or os.path.exists(wrl_path):
        existing.append("3D model")
    return existing


def import_component(
    lcsc_id: str,
    lib_dir: str,
    lib_name: str,
    overwrite: bool = False,
    use_global: bool = False,
    export_only: bool = False,
    log: Callable[[str], None] = print,
    kicad_version: int = DEFAULT_KICAD_VERSION,
    search_result: dict | None = None,
    confirm_metadata: Callable[[dict], dict | None] | None = None,
    confirm_overwrite: Callable[[str, list[str]], bool] | None = None,
) -> dict | None:
    """Import an LCSC component into a KiCad library or export raw files.

    Args:
        lcsc_id: Validated LCSC part number (e.g. "C427602").
        lib_dir: Destination directory (project dir, global lib dir, or export dir).
        lib_name: Library name (e.g. "JLCImport").
        overwrite: Whether to overwrite existing files.
        use_global: If True, use absolute model paths and update global lib tables.
        export_only: If True, write raw .kicad_mod/.kicad_sym/3D files to a flat directory.
        log: Callback for status messages.
        kicad_version: Target KiCad major version (8 or 9).
        search_result: Optional search result dict with ``brand``, ``description``,
            and ``datasheet`` fields from the JLCPCB search API.
        confirm_metadata: Optional callback that receives a dict with ``description``,
            ``keywords``, and ``manufacturer`` keys.  Returns the (possibly edited)
            dict to use, or ``None`` to cancel the import.
        confirm_overwrite: Optional callback called when existing files are detected.
            Receives ``(name, existing_items)`` where *existing_items* lists what
            already exists (e.g. ``["footprint", "symbol"]``).  Returns ``True`` to
            overwrite or ``False`` to cancel.  When ``None``, the ``overwrite`` bool
            governs behavior.

    Returns:
        dict with keys: title, name, fp_content, sym_content; or None if cancelled.
    """
    log(f"Fetching component {lcsc_id}...")

    comp = fetch_full_component(lcsc_id)

    # Merge richer metadata from search result when available
    if search_result:
        if search_result.get("brand"):
            comp["manufacturer"] = search_result["brand"]
        if search_result.get("description"):
            comp["description"] = search_result["description"]
        if search_result.get("datasheet"):
            comp["datasheet"] = search_result["datasheet"]

    title = comp["title"]
    name = sanitize_name(title)

    # Check for existing files and ask user to confirm overwrite
    if not export_only and confirm_overwrite:
        existing = _check_existing_files(lib_dir, lib_name, name)
        if existing:
            if not confirm_overwrite(name, existing):
                return None
            overwrite = True

    # Compute metadata that will be written to KiCad files
    metadata = {
        "description": _build_description(comp),
        "keywords": _build_keywords(comp),
        "manufacturer": comp.get("manufacturer", ""),
    }
    if confirm_metadata:
        metadata = confirm_metadata(metadata)
        if metadata is None:
            return None
    log(f"Component: {title}")
    log(f"Prefix: {comp['prefix']}, Name: {name}")

    # Parse footprint
    log("Parsing footprint...")
    fp_shapes = comp["footprint_data"]["dataStr"]["shape"]
    footprint = parse_footprint_shapes(fp_shapes, comp["fp_origin_x"], comp["fp_origin_y"])
    log(f"  {len(footprint.pads)} pads, {len(footprint.tracks)} tracks")

    # Determine 3D model UUID and transform
    model_offset = (0.0, 0.0, 0.0)
    model_rotation = (0.0, 0.0, 0.0)
    uuid_3d = ""
    wrl_source = None
    if footprint.model:
        uuid_3d = footprint.model.uuid
    if not uuid_3d:
        uuid_3d = comp.get("uuid_3d", "")
    if uuid_3d:
        wrl_source = download_wrl_source(uuid_3d)
    if footprint.model:
        model_offset, model_rotation = compute_model_transform(
            footprint.model, comp["fp_origin_x"], comp["fp_origin_y"], wrl_source
        )

    # Parse symbol
    sym_content = ""
    if comp["symbol_data_list"]:
        log("Parsing symbol...")
        sym_data = comp["symbol_data_list"][0]
        sym_shapes = sym_data["dataStr"]["shape"]
        symbol = parse_symbol_shapes(sym_shapes, comp["sym_origin_x"], comp["sym_origin_y"])
        log(f"  {len(symbol.pins)} pins, {len(symbol.rectangles)} rects")

        footprint_ref = f"{lib_name}:{name}"
        sym_content = write_symbol(
            symbol,
            name,
            prefix=comp["prefix"],
            footprint_ref=footprint_ref,
            lcsc_id=lcsc_id,
            datasheet=comp.get("datasheet", ""),
            description=metadata["description"],
            keywords=metadata["keywords"],
            manufacturer=metadata["manufacturer"],
            manufacturer_part=comp.get("manufacturer_part", ""),
        )
    else:
        log("No symbol data available")

    if export_only:
        return _export_only(
            lib_dir,
            name,
            lcsc_id,
            comp,
            footprint,
            uuid_3d,
            model_offset,
            model_rotation,
            lib_name,
            sym_content,
            title,
            log,
            kicad_version,
            wrl_source,
            metadata,
        )

    return _import_to_library(
        lib_dir,
        lib_name,
        name,
        lcsc_id,
        comp,
        footprint,
        uuid_3d,
        model_offset,
        model_rotation,
        use_global,
        overwrite,
        sym_content,
        title,
        log,
        kicad_version,
        wrl_source,
        metadata,
    )


def _export_only(
    out_dir,
    name,
    lcsc_id,
    comp,
    footprint,
    uuid_3d,
    model_offset,
    model_rotation,
    lib_name,
    sym_content,
    title,
    log,
    kicad_version,
    wrl_source=None,
    metadata=None,
):
    """Write raw .kicad_mod, .kicad_sym, and 3D models to a flat directory."""
    os.makedirs(out_dir, exist_ok=True)

    # Model path for export is relative within the output dir
    # Use WRL instead of STEP for consistency with offset calculations (which use OBJ/WRL geometry)
    model_path = f"3dmodels/{name}.wrl" if uuid_3d else ""

    fp_content = write_footprint(
        footprint,
        name,
        lcsc_id=lcsc_id,
        description=metadata["description"],
        keywords=metadata["keywords"],
        datasheet=comp.get("datasheet", ""),
        model_path=model_path,
        model_offset=model_offset,
        model_rotation=model_rotation,
        kicad_version=kicad_version,
    )

    fp_path = os.path.join(out_dir, f"{name}.kicad_mod")
    with open(fp_path, "w") as f:
        f.write(fp_content)
    log(f"  Saved: {fp_path}")

    if sym_content:
        sym_path = os.path.join(out_dir, f"{name}.kicad_sym")
        sym_lib = "(kicad_symbol_lib\n"
        sym_lib += f"  (version {symbol_format_version(kicad_version)})\n"
        sym_lib += '  (generator "JLCImport")\n'
        if has_generator_version(kicad_version):
            sym_lib += '  (generator_version "1.0")\n'
        sym_lib += sym_content + ")\n"
        with open(sym_path, "w") as f:
            f.write(sym_lib)
        log(f"  Saved: {sym_path}")

    if uuid_3d:
        models_dir = os.path.join(out_dir, "3dmodels")
        step_data = download_step(uuid_3d)
        if wrl_source is None:
            wrl_source = download_wrl_source(uuid_3d)
        step_path, wrl_path = save_models(models_dir, name, step_data, wrl_source)
        if step_path:
            log(f"  Saved: {step_path}")
        if wrl_path:
            log(f"  Saved: {wrl_path}")

    return {"title": title, "name": name, "fp_content": fp_content, "sym_content": sym_content}


def _import_to_library(
    lib_dir,
    lib_name,
    name,
    lcsc_id,
    comp,
    footprint,
    uuid_3d,
    model_offset,
    model_rotation,
    use_global,
    overwrite,
    sym_content,
    title,
    log,
    kicad_version,
    wrl_source=None,
    metadata=None,
):
    """Import into KiCad library structure with lib-table updates."""
    log(f"Destination: {lib_dir}")

    paths = ensure_lib_structure(lib_dir, lib_name)

    # Download 3D models
    model_path = ""
    if uuid_3d:
        step_dest = os.path.join(paths["models_dir"], f"{name}.step")
        wrl_dest = os.path.join(paths["models_dir"], f"{name}.wrl")
        step_existed = os.path.exists(step_dest)
        wrl_existed = os.path.exists(wrl_dest)

        log("Downloading 3D model...")
        step_data = download_step(uuid_3d) if overwrite or not step_existed else None
        if wrl_source is None and (overwrite or not wrl_existed):
            wrl_source = download_wrl_source(uuid_3d)
        step_path, wrl_path = save_models(paths["models_dir"], name, step_data, wrl_source)

        # Use WRL instead of STEP for consistency with offset calculations (which use OBJ/WRL geometry)
        if wrl_path:
            if use_global:
                model_path = os.path.join(paths["models_dir"], f"{name}.wrl").replace("\\", "/")
            else:
                model_path = f"${{KIPRJMOD}}/{lib_name}.3dshapes/{name}.wrl"
            if wrl_existed and not overwrite:
                log(f"  WRL skipped: {wrl_path} (exists, overwrite=off)")
            else:
                log(f"  WRL saved: {wrl_path}")
        if step_path:
            if step_existed and not overwrite:
                log(f"  STEP skipped: {step_path} (exists, overwrite=off)")
            else:
                log(f"  STEP saved: {step_path}")
    else:
        log("No 3D model available")

    # Write footprint
    log("Writing footprint...")
    fp_content = write_footprint(
        footprint,
        name,
        lcsc_id=lcsc_id,
        description=metadata["description"],
        keywords=metadata["keywords"],
        datasheet=comp.get("datasheet", ""),
        model_path=model_path,
        model_offset=model_offset,
        model_rotation=model_rotation,
        kicad_version=kicad_version,
    )
    fp_path = os.path.join(paths["fp_dir"], f"{name}.kicad_mod")
    fp_saved = save_footprint(paths["fp_dir"], name, fp_content, overwrite)
    if fp_saved:
        log(f"  Saved: {fp_path}")
    else:
        log(f"  Skipped: {fp_path} (exists, overwrite=off)")

    # Write symbol
    if sym_content:
        sym_added = add_symbol_to_lib(paths["sym_path"], name, sym_content, overwrite, kicad_version=kicad_version)
        if sym_added:
            log(f"  Symbol added: {paths['sym_path']}")
        else:
            log(f"  Symbol skipped: {paths['sym_path']} (exists, overwrite=off)")

    # Update lib tables
    if use_global:
        update_global_lib_tables(lib_dir, lib_name, kicad_version=kicad_version)
        log("Global library tables updated.")
    else:
        newly_created = update_project_lib_tables(lib_dir, lib_name)
        log("Project library tables updated.")
        if newly_created:
            log("NOTE: Reopen project for new library tables to take effect.")

    return {"title": title, "name": name, "fp_content": fp_content, "sym_content": sym_content}
