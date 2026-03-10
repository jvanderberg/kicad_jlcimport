"""Shared import pipeline for CLI, TUI, and plugin.

Flow:
  1. Fetch component data from EasyEDA (or accept pre-fetched data).
  2. Optionally ask the user to confirm/edit metadata (description, keywords,
     manufacturer) and choose between the EasyEDA-derived footprint or an
     existing KiCad library footprint.
  3. Parse EasyEDA shapes → EESymbol / EEFootprint dataclasses.
  4. Write .kicad_sym / .kicad_mod / 3D-model files.
  5. Update library tables so KiCad discovers the imported part.
"""

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


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def _build_description(comp: dict) -> str:
    """Return a human-readable description for the component.

    Prefers the EasyEDA description field; synthesises one from package /
    manufacturer data when the description is absent or just repeats the title.
    """
    desc = comp.get("description", "")
    title = comp.get("title", "")
    if not desc or desc == title:
        parts = [v for k in ("manufacturer_part", "package", "manufacturer")
                 if (v := comp.get(k, ""))]
        desc = "; ".join(parts)
    return desc.strip()


def _build_keywords(comp: dict) -> str:
    """Return a space-separated ki_keywords string for KiCad symbol search."""
    terms = {comp.get(k, "") for k in ("lcsc_id", "manufacturer_part", "manufacturer", "package")}
    return " ".join(sorted(t for t in terms if t))


# ---------------------------------------------------------------------------
# Overwrite detection
# ---------------------------------------------------------------------------

def _existing_files(lib_dir: str, lib_name: str, name: str) -> list[str]:
    """Return which of footprint / symbol / 3D-model already exist on disk."""
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
        except OSError:
            pass

    models_dir = os.path.join(lib_dir, f"{lib_name}.3dshapes")
    if any(os.path.exists(os.path.join(models_dir, f"{name}{ext}"))
           for ext in (".step", ".wrl")):
        existing.append("3D model")

    return existing


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

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
    """Import an LCSC component into a KiCad library (or export raw files).

    Args:
        lcsc_id:                  Validated LCSC part number, e.g. ``"C427602"``.
        lib_dir:                  Destination directory (project dir, global lib
                                  dir, or export-only output dir).
        lib_name:                 KiCad library name, e.g. ``"JLCImport"``.
        overwrite:                Replace existing files without asking.
        use_global:               Use absolute model paths; update global tables.
        export_only:              Write raw files to a flat directory only.
        log:                      Status message callback.
        kicad_version:            Target KiCad major version (8, 9, or 10).
        search_result:            Optional search-result dict; enriches metadata
                                  with ``brand``, ``description``, ``datasheet``,
                                  and ``package`` when available.
        confirm_metadata:         Called with a metadata dict (keys: description,
                                  keywords, manufacturer; optional __package_name,
                                  __footprint_candidate_ref).  Returns the edited
                                  dict to proceed, or ``None`` to cancel.
                                  May also include ``__reuse_existing_footprint``
                                  and ``__manually_chosen_footprint`` to select an
                                  existing KiCad footprint instead of importing one.
        confirm_overwrite:        Called with ``(name, existing_items)`` when
                                  files already exist; returns True to overwrite.
                                  Falls back to the ``overwrite`` flag when absent.
        component_data:           Pre-fetched component dict; skips the API call.
        symbol_kwargs:            Extra keyword arguments forwarded to
                                  ``write_symbol()`` (e.g. ``include_pin_dots``).
        confirm_reuse_footprint:  Called with ``(package, footprint_ref)`` when an
                                  auto-match is found but ``confirm_metadata`` is
                                  not provided; returns True to reuse it.

    Returns:
        ``dict`` with keys ``title``, ``name``, ``fp_content``, ``sym_content``,
        or ``None`` if the user cancelled.
    """
    comp = component_data if component_data is not None else _fetch_component(lcsc_id, log)
    comp = _merge_search_result(comp, search_result)

    title = comp["title"]
    name = sanitize_name(title)
    log(f"Component: {title}  (name: {name})")

    # --- Footprint reuse decision ----------------------------------------
    # Find an auto-matched KiCad footprint candidate (skip in export-only mode
    # because there is no project/global library to search).
    package = comp.get("package", "")
    candidate_ref: str | None = None
    if not export_only:
        candidate_ref = find_best_matching_footprint(
            package,
            project_dir=lib_dir if not use_global else "",
            kicad_version=kicad_version,
        )

    # --- Overwrite check ----------------------------------------------------
    if not export_only and confirm_overwrite:
        existing = _existing_files(lib_dir, lib_name, name)
        if existing and not confirm_overwrite(name, existing):
            return None
        if existing:
            overwrite = True

    # --- Metadata confirmation (also handles footprint selection) -----------
    metadata = {
        "description": _build_description(comp),
        "keywords":    _build_keywords(comp),
        "manufacturer": comp.get("manufacturer", ""),
    }
    if candidate_ref:
        metadata["__package_name"] = package
        metadata["__footprint_candidate_ref"] = candidate_ref

    reuse_footprint_ref: str | None = None   # non-None means skip EasyEDA footprint

    if confirm_metadata:
        metadata = confirm_metadata(metadata)
        if metadata is None:
            return None
        reuse_footprint_ref = _extract_footprint_choice(metadata, candidate_ref)
    elif candidate_ref and confirm_reuse_footprint:
        if confirm_reuse_footprint(package, candidate_ref):
            reuse_footprint_ref = candidate_ref

    # Strip internal keys before they reach the writers
    for key in ("__package_name", "__footprint_candidate_ref",
                "__reuse_existing_footprint", "__manually_chosen_footprint"):
        metadata.pop(key, None)

    # Final footprint reference used in the symbol's Footprint property
    footprint_ref = reuse_footprint_ref or f"{lib_name}:{name}"
    if reuse_footprint_ref:
        log(f"Using existing KiCad footprint: {reuse_footprint_ref}")
    log(f"Prefix: {comp['prefix']}  |  Library ref: {footprint_ref}")

    # --- Parse footprint (skip when reusing an existing one) ----------------
    footprint = None
    wrl_source = None
    model_offset = model_rotation = (0.0, 0.0, 0.0)
    uuid_3d = ""

    if not reuse_footprint_ref:
        log("Parsing footprint…")
        fp_shapes = comp["footprint_data"]["dataStr"]["shape"]
        footprint = parse_footprint_shapes(fp_shapes, comp["fp_origin_x"], comp["fp_origin_y"])
        log(f"  {len(footprint.pads)} pads, {len(footprint.tracks)} tracks")

        uuid_3d = (footprint.model.uuid if footprint.model else "") or comp.get("uuid_3d", "")
        if uuid_3d:
            wrl_source = download_wrl_source(uuid_3d)
        if footprint.model:
            model_offset, model_rotation = compute_model_transform(
                footprint.model, comp["fp_origin_x"], comp["fp_origin_y"], wrl_source
            )
    else:
        log("Skipping footprint parse — reusing existing footprint.")

    # --- Parse symbol -------------------------------------------------------
    sym_content = _parse_symbol(comp, name, footprint_ref, lcsc_id, metadata, symbol_kwargs, log)

    # --- Write output -------------------------------------------------------
    if export_only:
        return _export_raw(
            lib_dir, name, lcsc_id, comp, footprint,
            uuid_3d, model_offset, model_rotation,
            lib_name, sym_content, title, log, kicad_version, wrl_source, metadata,
        )

    return _import_to_library(
        lib_dir, lib_name, name, lcsc_id, comp, footprint,
        uuid_3d, model_offset, model_rotation,
        use_global, overwrite, sym_content, title, log, kicad_version,
        wrl_source, metadata, reuse_footprint_ref, footprint_ref,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_component(lcsc_id: str, log: Callable) -> dict:
    log(f"Fetching component {lcsc_id}…")
    return fetch_full_component(lcsc_id)


def _merge_search_result(comp: dict, search_result: dict | None) -> dict:
    """Enrich component metadata with the richer JLCPCB search-result fields."""
    if not search_result:
        return comp
    for api_key, comp_key in (("brand", "manufacturer"), ("description", "description"),
                               ("datasheet", "datasheet")):
        if search_result.get(api_key):
            comp[comp_key] = search_result[api_key]
    # EasyEDA payloads sometimes omit package; the search result has it.
    if search_result.get("package") and not comp.get("package"):
        comp["package"] = search_result["package"]
    return comp


def _extract_footprint_choice(metadata: dict, candidate_ref: str | None) -> str | None:
    """Return the chosen existing-footprint ref from ``confirm_metadata`` output.

    Priority:
      1. ``__manually_chosen_footprint`` — user browsed and picked one explicitly.
      2. ``__reuse_existing_footprint`` True + a candidate was auto-matched.
      3. Anything else → None (generate the EasyEDA footprint as normal).
    """
    manual = metadata.get("__manually_chosen_footprint")
    if manual:
        return manual
    if metadata.get("__reuse_existing_footprint") and candidate_ref:
        return candidate_ref
    return None


# _SYMBOL_WRITER_EXPLICIT_KEYS — keys handled explicitly by write_symbol();
# filtered out of symbol_kwargs so callers can't accidentally collide.
_SYMBOL_WRITER_EXPLICIT_KEYS = frozenset({
    "symbol", "name", "prefix", "footprint_ref", "lcsc_id",
    "datasheet", "description", "keywords", "manufacturer",
    "manufacturer_part", "unit_index", "total_units",
})


def _parse_symbol(
    comp: dict,
    name: str,
    footprint_ref: str,
    lcsc_id: str,
    metadata: dict,
    symbol_kwargs: dict | None,
    log: Callable,
) -> str:
    """Parse all symbol units and return the combined S-expression content."""
    sym_data_list = comp.get("symbol_data_list", [])
    if not sym_data_list:
        log("No symbol data available.")
        return ""

    log("Parsing symbol…")

    # Multi-unit symbols: index 0 is the package overview (all pins + outline).
    # The per-unit slices that KiCad renders individually start at index 1.
    if len(sym_data_list) > 1:
        sym_data_list = sym_data_list[1:]

    total_units = len(sym_data_list)
    extra_kwargs = {k: v for k, v in (symbol_kwargs or {}).items()
                   if k not in _SYMBOL_WRITER_EXPLICIT_KEYS}

    parts: list[str] = []
    total_pins = total_rects = 0

    for unit_idx, sym_data in enumerate(sym_data_list):
        head = sym_data.get("dataStr", {}).get("head", {})
        origin_x = head.get("x", comp["sym_origin_x"])
        origin_y = head.get("y", comp["sym_origin_y"])
        symbol = parse_symbol_shapes(sym_data["dataStr"]["shape"], origin_x, origin_y)
        total_pins  += len(symbol.pins)
        total_rects += len(symbol.rectangles)

        parts.append(write_symbol(
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
            **extra_kwargs,
        ))

    log(f"  {total_pins} pins, {total_rects} rects ({total_units} unit(s))")
    return "".join(parts)


def _symbol_lib_header(kicad_version: int) -> str:
    """Return a complete .kicad_sym library file header string."""
    lines = [
        "(kicad_symbol_lib",
        f"  (version {symbol_format_version(kicad_version)})",
        '  (generator "JLCImport")',
    ]
    if has_generator_version(kicad_version):
        lines.append('  (generator_version "1.0")')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Export-only mode
# ---------------------------------------------------------------------------

def _export_raw(
    out_dir: str,
    name: str,
    lcsc_id: str,
    comp: dict,
    footprint,
    uuid_3d: str,
    model_offset: tuple,
    model_rotation: tuple,
    lib_name: str,
    sym_content: str,
    title: str,
    log: Callable,
    kicad_version: int,
    wrl_source=None,
    metadata: dict | None = None,
) -> dict:
    """Write raw .kicad_mod, .kicad_sym, and 3D-model files to a flat directory."""
    metadata = metadata or {}
    os.makedirs(out_dir, exist_ok=True)

    # Relative model path (WRL preferred — offset maths use OBJ/WRL geometry)
    model_path = f"3dmodels/{name}.wrl" if uuid_3d else ""

    fp_content = write_footprint(
        footprint, name,
        lcsc_id=lcsc_id,
        description=metadata.get("description", ""),
        keywords=metadata.get("keywords", ""),
        datasheet=comp.get("datasheet", ""),
        model_path=model_path,
        model_offset=model_offset,
        model_rotation=model_rotation,
        kicad_version=kicad_version,
    )
    _write_file(os.path.join(out_dir, f"{name}.kicad_mod"), fp_content, log)

    if sym_content:
        sym_lib = _symbol_lib_header(kicad_version) + sym_content + ")\n"
        _write_file(os.path.join(out_dir, f"{name}.kicad_sym"), sym_lib, log)

    if uuid_3d:
        models_dir = os.path.join(out_dir, "3dmodels")
        step_data = download_step(uuid_3d)
        wrl_source = wrl_source or download_wrl_source(uuid_3d)
        step_path, wrl_path = save_models(models_dir, name, step_data, wrl_source)
        if step_path:
            log(f"  Saved: {step_path}")
        if wrl_path:
            log(f"  Saved: {wrl_path}")

    return {"title": title, "name": name, "fp_content": fp_content, "sym_content": sym_content}


def _write_file(path: str, content: str, log: Callable) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"  Saved: {path}")


# ---------------------------------------------------------------------------
# Library-import mode
# ---------------------------------------------------------------------------

def _import_to_library(
    lib_dir: str,
    lib_name: str,
    name: str,
    lcsc_id: str,
    comp: dict,
    footprint,
    uuid_3d: str,
    model_offset: tuple,
    model_rotation: tuple,
    use_global: bool,
    overwrite: bool,
    sym_content: str,
    title: str,
    log: Callable,
    kicad_version: int,
    wrl_source=None,
    metadata: dict | None = None,
    reuse_footprint_ref: str | None = None,
    footprint_ref: str = "",
) -> dict:
    """Import component into a structured KiCad library with table updates."""
    metadata = metadata or {}
    log(f"Destination: {lib_dir}")
    paths = ensure_lib_structure(lib_dir, lib_name)

    fp_content = ""

    if reuse_footprint_ref:
        # Nothing to write for the footprint — the symbol points at an
        # existing library entry.
        log(f"Using existing footprint reference: {footprint_ref}")
    else:
        model_path = _save_3d_models(
            uuid_3d, paths["models_dir"], name, lib_name,
            use_global, overwrite, log, wrl_source,
        )

        log("Writing footprint…")
        fp_content = write_footprint(
            footprint, name,
            lcsc_id=lcsc_id,
            description=metadata.get("description", ""),
            keywords=metadata.get("keywords", ""),
            datasheet=comp.get("datasheet", ""),
            model_path=model_path,
            model_offset=model_offset,
            model_rotation=model_rotation,
            kicad_version=kicad_version,
        )
        saved = save_footprint(paths["fp_dir"], name, fp_content, overwrite)
        fp_path = os.path.join(paths["fp_dir"], f"{name}.kicad_mod")
        log(f"  {'Saved' if saved else 'Skipped (exists)'}: {fp_path}")

    if sym_content:
        sym_added = add_symbol_to_lib(paths["sym_path"], name, sym_content,
                                      overwrite, kicad_version=kicad_version)
        log(f"  {'Symbol added' if sym_added else 'Symbol skipped (exists)'}: {paths['sym_path']}")

    if use_global:
        update_global_lib_tables(lib_dir, lib_name, kicad_version=kicad_version)
        log("Global library tables updated.")
    else:
        newly_created = update_project_lib_tables(lib_dir, lib_name)
        log("Project library tables updated.")
        if newly_created:
            log("NOTE: Reopen the project for the new library tables to take effect.")

    return {"title": title, "name": name, "fp_content": fp_content, "sym_content": sym_content}


def _save_3d_models(
    uuid_3d: str,
    models_dir: str,
    name: str,
    lib_name: str,
    use_global: bool,
    overwrite: bool,
    log: Callable,
    wrl_source=None,
) -> str:
    """Download and save STEP/WRL 3D models; return the model path for the footprint.

    Returns an empty string when no 3D model UUID is available.
    The returned path uses an absolute path for global imports and a
    ``${KIPRJMOD}``-relative path for project imports.
    """
    if not uuid_3d:
        log("No 3D model available.")
        return ""

    step_dest = os.path.join(models_dir, f"{name}.step")
    wrl_dest  = os.path.join(models_dir, f"{name}.wrl")
    step_existed = os.path.exists(step_dest)
    wrl_existed  = os.path.exists(wrl_dest)

    log("Downloading 3D model…")
    step_data = download_step(uuid_3d) if overwrite or not step_existed else None
    if wrl_source is None and (overwrite or not wrl_existed):
        wrl_source = download_wrl_source(uuid_3d)

    step_path, wrl_path = save_models(models_dir, name, step_data, wrl_source)

    for path, existed, label in (
        (wrl_path,  wrl_existed,  "WRL"),
        (step_path, step_existed, "STEP"),
    ):
        if path:
            skipped = existed and not overwrite
            log(f"  {label} {'skipped (exists)' if skipped else 'saved'}: {path}")

    if not wrl_path:
        return ""
    if use_global:
        return os.path.join(models_dir, f"{name}.wrl").replace("\\", "/")
    return f"${{KIPRJMOD}}/{lib_name}.3dshapes/{name}.wrl"
