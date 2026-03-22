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
    find_best_matching_footprint,
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


def _check_existing_files(
    lib_dir: str, lib_name: str, fp_name: str, sym_name: str = "", model_name: str = ""
) -> list[str]:
    """Return a list of existing file types (e.g. ["footprint", "symbol", "3D model"]).

    *fp_name* is the footprint filename.
    *sym_name* is the symbol name inside the .kicad_sym (defaults to *fp_name*).
    *model_name* is the 3D model filename (defaults to *fp_name*).
    """
    sym_name = sym_name or fp_name
    model_name = model_name or fp_name
    existing: list[str] = []
    fp_path = os.path.join(lib_dir, f"{lib_name}.pretty", f"{fp_name}.kicad_mod")
    if os.path.exists(fp_path):
        existing.append("footprint")
    sym_path = os.path.join(lib_dir, f"{lib_name}.kicad_sym")
    if os.path.exists(sym_path):
        try:
            with open(sym_path, encoding="utf-8") as f:
                if f'(symbol "{sym_name}"' in f.read():
                    existing.append("symbol")
        except (PermissionError, OSError):
            pass
    models_dir = os.path.join(lib_dir, f"{lib_name}.3dshapes")
    step_path = os.path.join(models_dir, f"{model_name}.step")
    wrl_path = os.path.join(models_dir, f"{model_name}.wrl")
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
    component_data: dict | None = None,
    symbol_kwargs: dict | None = None,
    confirm_reuse_footprint: Callable[[str, str], bool] | None = None,
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
            ``keywords``, and ``manufacturer`` keys. When a footprint match is
            available it also includes ``__package_name`` and
            ``__footprint_candidate_ref``. The callback may set
            ``__reuse_existing_footprint`` to request reusing that footprint.
            Returns the (possibly edited) dict to use, or ``None`` to cancel the import.
        confirm_overwrite: Optional callback called when existing files are detected.
            Receives ``(name, existing_items)`` where *existing_items* lists what
            already exists (e.g. ``["footprint", "symbol"]``).  Returns ``True`` to
            overwrite or ``False`` to cancel.  When ``None``, the ``overwrite`` bool
            governs behavior.
        component_data: Optional pre-fetched component dict.  When provided the
            API call to ``fetch_full_component`` is skipped.
        symbol_kwargs: Optional extra keyword arguments forwarded to
            ``write_symbol()`` (e.g. ``include_pin_dots``, ``hide_properties``).
        confirm_reuse_footprint: Optional callback called when a likely existing
            footprint match is found. Receives ``(package, footprint_ref)`` and
            returns ``True`` to reuse the existing footprint reference in the
            symbol, or ``False`` to generate a new footprint as before.

    Returns:
        dict with keys: title, name, fp_content, sym_content; or None if cancelled.
    """
    if component_data is not None:
        comp = component_data
    else:
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
        # Some EasyEDA payloads omit package while JLC search results include it.
        if search_result.get("package") and not comp.get("package"):
            comp["package"] = search_result["package"]

    title = comp["title"]
    name = sanitize_name(title)
    package = comp.get("package", "")
    candidate_ref = None
    reuse_existing_footprint = False
    reuse_choice_from_metadata = None
    if not export_only:
        candidate_ref = find_best_matching_footprint(
            package,
            project_dir=lib_dir if not use_global else "",
            kicad_version=kicad_version,
        )

    # Compute metadata that will be written to KiCad files
    metadata = {
        "description": _build_description(comp),
        "keywords": _build_keywords(comp),
        "manufacturer": comp.get("manufacturer", ""),
    }
    metadata["__component_name"] = name  # shown/editable in dialog as footprint & 3D name
    if candidate_ref:
        metadata["__package_name"] = package
        metadata["__footprint_candidate_ref"] = candidate_ref
    if confirm_metadata:
        metadata = confirm_metadata(metadata)
        if metadata is None:
            return None
        if metadata.get("__manually_chosen_footprint"):
            # User browsed and picked a specific footprint — override candidate_ref
            # so the existing reuse logic below routes it correctly.
            candidate_ref = metadata["__manually_chosen_footprint"]
            reuse_choice_from_metadata = True
        elif "__reuse_existing_footprint" in metadata:
            reuse_choice_from_metadata = bool(metadata["__reuse_existing_footprint"])
    metadata.pop("__package_name", None)
    metadata.pop("__footprint_candidate_ref", None)
    metadata.pop("__reuse_existing_footprint", None)
    metadata.pop("__manually_chosen_footprint", None)
    # Apply user-edited footprint / 3D-model name overrides (EasyEDA import only).
    # Check for empty strings BEFORE sanitize_name — sanitize_name("") returns
    # "unnamed" which is truthy, so the `or name` fallback would never trigger.
    raw_fp_name = metadata.pop("__footprint_name", "").strip()
    raw_model_name = metadata.pop("__model_name", "").strip()
    fp_name = sanitize_name(raw_fp_name) if raw_fp_name else name
    model_name = sanitize_name(raw_model_name) if raw_model_name else fp_name
    metadata.pop("__component_name", None)

    # Check for existing files and ask user to confirm overwrite.
    # This runs after metadata editing so fp_name reflects any user rename.
    if not export_only and confirm_overwrite:
        existing = _check_existing_files(lib_dir, lib_name, fp_name, sym_name=name, model_name=model_name)
        if existing:
            if not confirm_overwrite(fp_name, existing):
                return None
            overwrite = True

    # footprint_ref defaults to lib:fp_name (respects any user rename).
    # This is overwritten below by candidate_ref if the user chose a KiCad
    # library footprint, so no guard against reuse_existing_footprint is needed.
    footprint_ref = f"{lib_name}:{fp_name}"

    if candidate_ref:
        if reuse_choice_from_metadata is True:
            reuse_existing_footprint = True
        elif reuse_choice_from_metadata is None and confirm_reuse_footprint:
            reuse_existing_footprint = confirm_reuse_footprint(package, candidate_ref)
        if reuse_existing_footprint:
            footprint_ref = candidate_ref
            log(f"Reusing existing footprint: {footprint_ref}")
    log(f"Component: {title}")
    log(f"Prefix: {comp['prefix']}, Name: {name}")

    # Parse footprint unless using an existing KiCad footprint
    footprint = None
    if not reuse_existing_footprint:
        log("Parsing footprint...")
        fp_shapes = comp["footprint_data"]["dataStr"]["shape"]
        footprint = parse_footprint_shapes(fp_shapes, comp["fp_origin_x"], comp["fp_origin_y"])
        log(f"  {len(footprint.pads)} pads, {len(footprint.tracks)} tracks")
    else:
        log("Skipping footprint parse (existing footprint selected).")

    # Determine 3D model UUID and transform (only when generating a footprint)
    model_offset = (0.0, 0.0, 0.0)
    model_rotation = (0.0, 0.0, 0.0)
    uuid_3d = ""
    wrl_source = None
    if not reuse_existing_footprint:
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

    # Parse symbol (may have multiple units)
    sym_content = ""
    if comp["symbol_data_list"]:
        log("Parsing symbol...")
        sym_data_list = comp["symbol_data_list"]
        # Multi-unit symbols: entry 0 is the package overview (all pins, basic
        # outline).  The real per-unit data starts at index 1.
        if len(sym_data_list) > 1:
            sym_data_list = sym_data_list[1:]
        total_units = len(sym_data_list)
        sym_parts = []
        total_pins = 0
        total_rects = 0

        # Filter symbol_kwargs to keys that won't collide with explicit args
        _EXPLICIT_SYM_KEYS = frozenset(
            {
                "symbol",
                "name",
                "prefix",
                "footprint_ref",
                "lcsc_id",
                "datasheet",
                "description",
                "keywords",
                "manufacturer",
                "manufacturer_part",
                "unit_index",
                "total_units",
            }
        )
        extra_sym_kwargs = {k: v for k, v in (symbol_kwargs or {}).items() if k not in _EXPLICIT_SYM_KEYS}

        for unit_idx, sym_data in enumerate(sym_data_list):
            # Each unit may have its own origin
            origin_x = sym_data.get("dataStr", {}).get("head", {}).get("x", comp["sym_origin_x"])
            origin_y = sym_data.get("dataStr", {}).get("head", {}).get("y", comp["sym_origin_y"])
            sym_shapes = sym_data["dataStr"]["shape"]
            symbol = parse_symbol_shapes(sym_shapes, origin_x, origin_y)
            total_pins += len(symbol.pins)
            total_rects += len(symbol.rectangles)

            sym_parts.append(
                write_symbol(
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
                    unit_index=unit_idx,
                    total_units=total_units,
                    **extra_sym_kwargs,
                )
            )

        sym_content = "".join(sym_parts)
        log(f"  {total_pins} pins, {total_rects} rects ({total_units} unit(s))")
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
            fp_name=fp_name,
            model_name=model_name,
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
        reuse_existing_footprint,
        footprint_ref,
        fp_name=fp_name,
        model_name=model_name,
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
    fp_name=None,
    model_name=None,
):
    """Write raw .kicad_mod, .kicad_sym, and 3D models to a flat directory."""
    # fp_name: filename for .kicad_mod (defaults to component name)
    # model_name: filename for .step/.wrl (defaults to fp_name)
    fp_name = fp_name or name
    model_name = model_name or fp_name
    os.makedirs(out_dir, exist_ok=True)

    # Model path for export is relative within the output dir
    # Use WRL instead of STEP for consistency with offset calculations (which use OBJ/WRL geometry)
    model_path = f"3dmodels/{model_name}.wrl" if uuid_3d else ""

    fp_content = write_footprint(
        footprint,
        fp_name,
        lcsc_id=lcsc_id,
        description=metadata["description"],
        keywords=metadata["keywords"],
        datasheet=comp.get("datasheet", ""),
        model_path=model_path,
        model_offset=model_offset,
        model_rotation=model_rotation,
        kicad_version=kicad_version,
    )

    fp_path = os.path.join(out_dir, f"{fp_name}.kicad_mod")
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
        step_path, wrl_path = save_models(models_dir, model_name, step_data, wrl_source)
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
    reuse_existing_footprint=False,
    footprint_ref="",
    fp_name=None,
    model_name=None,
):
    """Import into KiCad library structure with lib-table updates.

    fp_name:    filename used for the .kicad_mod file (defaults to component name).
    model_name: filename used for .step/.wrl files (defaults to fp_name).
    The symbol is always saved under the component name so KiCad's symbol
    property ``Footprint`` can reference it consistently.
    """
    # fp_name / model_name default to the component name when not overridden
    fp_name = fp_name or name
    model_name = model_name or fp_name
    log(f"Destination: {lib_dir}")

    paths = ensure_lib_structure(lib_dir, lib_name)

    # Download 3D models
    model_path = ""
    if reuse_existing_footprint:
        log(f"Using existing footprint reference: {footprint_ref}")
        log("Skipping footprint and 3D model file generation.")
    elif uuid_3d:
        step_dest = os.path.join(paths["models_dir"], f"{model_name}.step")
        wrl_dest = os.path.join(paths["models_dir"], f"{model_name}.wrl")
        step_existed = os.path.exists(step_dest)
        wrl_existed = os.path.exists(wrl_dest)

        log("Downloading 3D model...")
        step_data = download_step(uuid_3d) if overwrite or not step_existed else None
        if wrl_source is None and (overwrite or not wrl_existed):
            wrl_source = download_wrl_source(uuid_3d)
        step_path, wrl_path = save_models(paths["models_dir"], model_name, step_data, wrl_source)

        # Use WRL instead of STEP for consistency with offset calculations (which use OBJ/WRL geometry)
        if wrl_path:
            if use_global:
                model_path = f"${{KICAD{kicad_version}_3RD_PARTY}}/{lib_name}.3dshapes/{model_name}.wrl"
            else:
                model_path = f"${{KIPRJMOD}}/{lib_name}.3dshapes/{model_name}.wrl"
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

    fp_content = ""
    if not reuse_existing_footprint:
        # Write footprint
        log("Writing footprint...")
        fp_content = write_footprint(
            footprint,
            fp_name,
            lcsc_id=lcsc_id,
            description=metadata["description"],
            keywords=metadata["keywords"],
            datasheet=comp.get("datasheet", ""),
            model_path=model_path,
            model_offset=model_offset,
            model_rotation=model_rotation,
            kicad_version=kicad_version,
        )
        fp_path = os.path.join(paths["fp_dir"], f"{fp_name}.kicad_mod")
        fp_saved = save_footprint(paths["fp_dir"], fp_name, fp_content, overwrite)
        if fp_saved:
            log(f"  Saved: {fp_path}")
        else:
            log(f"  Skipped: {fp_path} (exists, overwrite=off)")

    # Write symbol (always uses component name, not fp_name)
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
