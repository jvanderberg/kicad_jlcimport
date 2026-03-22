# Architecture

This document describes how JLCImport works: the system architecture, data flow, module responsibilities, and external APIs used.

## Overview

JLCImport bridges the JLCPCB/LCSC component catalog with KiCad 8, 9, and 10. It fetches component data from EasyEDA's public APIs, parses their proprietary shape format, and writes native KiCad file formats (`.kicad_sym`, `.kicad_mod`, `.step`, `.wrl`). The plugin auto-detects the running KiCad version; the standalone CLI, GUI, and TUI allow selecting the target version. The plugin runs entirely within KiCad's bundled Python environment with no external dependencies. A standalone TUI provides the same functionality in the terminal.

## Source Layout

The project uses a standard `src/` layout. The package is organized into two subpackages that separate concerns:

```
src/kicad_jlcimport/
├── easyeda/              # EasyEDA/LCSC data fetching, types, and parsing
│   ├── api.py            #   HTTP client, search, component data, images
│   ├── parser.py         #   Shape string → dataclass conversion
│   └── ee_types.py       #   Dataclasses for parsed EasyEDA primitives
├── kicad/                # KiCad file generation and library management
│   ├── symbol_writer.py  #   .kicad_sym S-expression output
│   ├── footprint_writer.py # .kicad_mod S-expression output
│   ├── model3d.py        #   STEP/WRL saving, transforms, VRML conversion
│   ├── library.py        #   Library structure and table management
│   ├── version.py        #   KiCad version constants and feature flags
│   └── _format.py        #   Float formatting, escaping, UUID helpers
├── gui/                  # Standalone wxPython GUI
├── tui/                  # Terminal UI (Textual)
├── importer.py           # Import pipeline orchestrator
├── cli.py                # Command-line interface
├── dialog.py             # KiCad plugin dialog (wxPython)
├── plugin.py             # KiCad ActionPlugin entry point
└── categories.py         # JLCPCB category tree
```

The `easyeda/` and `kicad/` subpackages have a clean dependency boundary: `kicad/` depends on `easyeda/` types but not vice versa, enabling future extraction of the KiCad conversion code as an independent module.

## Architecture Layers

The codebase is organized in layers from top to bottom:

1. **User Interface** — `plugin.py`, `dialog.py`, `cli.py`, `gui/`, and `tui/` provide the entry points (wxPython, Textual, and CLI). They handle user interaction and delegate all work downward.
2. **Orchestration** — `importer.py` coordinates the full import pipeline. All UIs call into this single module.
3. **Two core subpackages** sit below the orchestrator, side by side:
   - **`easyeda/`** handles all external data: HTTP requests, API response parsing, and shape-string-to-dataclass conversion.
   - **`kicad/`** handles all output: S-expression writers, 3D model transforms, library file management, and version-specific formatting.

## Module Responsibilities

### User Interface

- **`plugin.py`** — KiCad ActionPlugin registration. Defines the menu entry under Tools > External Plugins > JLCImport and spawns the dialog.
- **`dialog.py`** — wxPython dialog with search input, results list, detail panel, image gallery, import options, and status log. Long-running operations (search, import, image fetch) run in background threads to keep the UI responsive.
- **`cli.py`** — Command-line interface with `search` and `import` subcommands for scripted or batch use outside KiCad. Supports `--lib-name` for custom library names.
- **`gui/`** — Standalone wxPython GUI for use outside KiCad. Provides the same workflow as the plugin dialog with a directory picker for project selection.
- **`tui/`** — Terminal UI built with Textual. Provides the same search, filter, detail, gallery, and import workflow as the plugin dialog. Supports Sixel, Kitty, iTerm2, and halfcell image rendering.

### Orchestration

- **`importer.py`** — Central import pipeline. Coordinates fetching component data from the EasyEDA API, parsing shapes, writing KiCad files, downloading 3D models, and updating library tables. Called by all user interfaces.

### `easyeda/` — EasyEDA Data Fetching & Parsing

- **`easyeda/api.py`** — HTTP client that talks to the JLCPCB and EasyEDA APIs. Handles search queries, component data fetching, image scraping, and 3D model downloads. Includes SSRF protection (domain whitelist) and SSL fallback for environments with certificate issues.
- **`easyeda/parser.py`** — Converts EasyEDA's proprietary shape strings into structured Python dataclasses. Handles coordinate conversion (mils to mm), layer mapping (EasyEDA layers to KiCad layers), and geometry calculations (arc midpoints, origin offsets).
- **`easyeda/ee_types.py`** — Dataclasses representing parsed EasyEDA primitives: pads, tracks, arcs, pins, polygons, etc. These are the intermediate representation between parsing and writing.

### `kicad/` — KiCad File Generation

- **`kicad/symbol_writer.py`** — Generates KiCad symbol blocks in S-expression format. Writes properties (Reference, Value, Footprint, Datasheet, LCSC part number) and graphics (rectangles, circles, polylines, arcs, pins). Output format adapts to the selected KiCad version.
- **`kicad/footprint_writer.py`** — Generates KiCad footprint files in S-expression format. Writes pads, tracks, arcs, circles, holes, zones, and 3D model references. Auto-detects SMD vs through-hole placement. Output format adapts to the selected KiCad version.
- **`kicad/model3d.py`** — Computes 3D model transforms (offset, rotation, scale), saves STEP files, and converts OBJ-like mesh data to VRML 2.0 format. Downloads are handled by `importer.py` via `easyeda/api.py`.
- **`kicad/library.py`** — Manages the on-disk library structure. Creates directories, appends symbols to `.kicad_sym` files, saves `.kicad_mod` footprints, and updates `sym-lib-table` / `fp-lib-table` so KiCad can find imported parts. Handles cross-platform path differences. Also manages persistent configuration (library name preference) via `jlcimport.json`.
- **`kicad/version.py`** — Central version management. Defines supported KiCad versions (8, 9, 10), format version constants, feature flags (`has_generator_version`, `has_embedded_fonts`), and directory name mapping. Auto-detection from `pcbnew` for plugin use; explicit selection for standalone tools.
- **`kicad/_format.py`** — Small helpers for float formatting, S-expression string escaping, and UUID generation.

## Data Flow

### Search

The user enters a query. `easyeda/api.py` sends the query to the JLCPCB search API and returns a JSON list of matching parts (part number, description, price, stock). The UI displays these in a sortable results list. When the user selects a part, `easyeda/api.py` scrapes the LCSC product page to extract a product image URL, then fetches the image for display as a thumbnail alongside part details.

### Import

When the user triggers an import, `importer.py` orchestrates the following steps:

1. **Fetch component data** — `easyeda/api.py` calls the EasyEDA SVGs API with the LCSC part number to get component UUIDs, then fetches the shape data (`dataStr`) for each UUID from the EasyEDA component API.
2. **Parse shapes** — `easyeda/parser.py` converts the proprietary shape strings into `EESymbol` and `EEFootprint` dataclass trees (coordinates converted from mils to mm, layers mapped to KiCad equivalents).
3. **Write KiCad files** — `kicad/symbol_writer.py` generates `.kicad_sym` S-expression text and `kicad/footprint_writer.py` generates `.kicad_mod` text, both adapting output to the target KiCad version.
4. **Download 3D models** — `kicad/model3d.py` fetches STEP binary and WRL mesh data from EasyEDA endpoints, computes transforms, and saves the files.
5. **Update library** — `kicad/library.py` appends the symbol to `<lib_name>.kicad_sym`, saves the footprint to `<lib_name>.pretty/`, and updates `sym-lib-table` / `fp-lib-table` so KiCad discovers the imported part.

## External APIs

| Endpoint | Purpose |
|----------|---------|
| `jlcpcb.com/.../selectSmtComponentList` | Search the JLCPCB parts catalog |
| `easyeda.com/api/products/{lcsc_id}/svgs` | Get component UUIDs for a given LCSC part |
| `easyeda.com/api/components/{uuid}` | Get symbol/footprint shape data |
| `modules.easyeda.com/.../{uuid}` | Download STEP 3D model |
| `easyeda.com/analyzer/api/3dmodel/{uuid}` | Download WRL source mesh |
| `lcsc.com/product-detail/{lcsc_id}.html` | Scrape product image URLs |

All network requests use Python's `urllib` with appropriate timeouts and error handling. No authentication is required.

## EasyEDA Shape Format

EasyEDA stores component geometry as semicolon/newline-delimited strings. Each line starts with a type identifier followed by tilde-separated parameters:

```
PAD~X~Y~Width~Height~Layer~PadNumber~Rotation~Shape~HoleSize~...
TRACK~StrokeWidth~Layer~Points...
ARC~StrokeWidth~Layer~Points...~SweepFlag
```

The parser splits these strings, converts coordinates from mils to millimeters, maps EasyEDA layer numbers to KiCad layer names, and produces typed dataclasses. The writers then serialize these dataclasses into KiCad's S-expression format.

## Library Structure

When a component is imported, the plugin creates/updates this structure in the target directory (library name is configurable, defaults to "JLCImport"):

```
<target>/
├── <lib_name>.kicad_sym          # All imported symbols
├── <lib_name>.pretty/            # Footprint library
│   ├── ComponentA.kicad_mod
│   └── ComponentB.kicad_mod
├── <lib_name>.3dshapes/          # 3D models
│   ├── ComponentA.step
│   ├── ComponentA.wrl
│   └── ...
├── sym-lib-table                 # Updated with library entry
└── fp-lib-table                  # Updated with library entry
```

The symbol library is a single file containing all imported symbols. Footprints are individual files within the `.pretty` directory. Library table files are created or updated to include the library entries so KiCad can discover them.

Pointing the tool at an existing library appends to it safely: symbols are inserted before the closing paren of the `.kicad_sym` file, and footprint files are added to the existing `.pretty` directory. Duplicate detection prevents accidental overwrites unless the overwrite flag is set.

## 3D Model Transform Calculation

3D models are positioned on footprints using offset and rotation transforms calculated by `kicad/model3d.py`.

The offset has two independent components:

1. **Geometry offset** — Compensates for the model's bounding box not being centered at the origin. Calculated in model space and rotated with the model.
2. **Origin offset** — Compensates for EasyEDA's model origin differing from the footprint origin. Calculated in footprint space (not rotated).

The Z offset uses a universal formula: `z_offset = -z_min + (model.z / 3.937)` where `model.z` is in EasyEDA units (1 unit = 10 mils = 0.254mm).

For details, see [3D Model Handling](3d-models.md).

## Configuration

Settings are persisted in `jlcimport.json` in the KiCad config base directory (one level above the version-specific config):

| OS | Path |
|----|------|
| macOS | `~/Library/Preferences/kicad/jlcimport.json` |
| Linux | `~/.config/kicad/jlcimport.json` |
| Windows | `%APPDATA%\kicad\jlcimport.json` |

The config file is shared between the plugin, TUI, and CLI. Currently stores:

```json
{"lib_name": "JLCImport"}
```

## Design Decisions

- **No external dependencies** — Only uses Python's standard library plus `wx` (bundled with KiCad). This avoids installation complexity and version conflicts. The TUI is a separate optional install with its own dependencies (Textual, Pillow).
- **Configurable library name** — Users can target any library name, including existing ones. The setting persists across sessions and is shared between all interfaces.
- **SSRF protection** — Image fetching validates URLs against a domain whitelist (`jlcpcb.com`, `lcsc.com`) to prevent server-side request forgery.
- **SSL fallback** — Attempts verified SSL connections first, falls back to unverified for environments (macOS, Windows) where KiCad's bundled Python may lack proper certificate bundles.
- **Background threading** — Network operations run in worker threads so the UI remains responsive during searches and imports.
- **Coordinate conversion at parse time** — EasyEDA uses mils; conversion to millimeters happens once during parsing rather than at write time.
- **Append-only symbol library** — New symbols are appended to the existing `.kicad_sym` file rather than rewriting the whole file, making imports non-destructive.

## Testing

Tests live in `tests/` and cover each module independently:

```
tests/
├── test_api.py              # LCSC ID validation, SSRF protection (easyeda/api.py)
├── test_api_extended.py     # SSL fallback, cert handling (easyeda/api.py)
├── test_parser.py           # Shape parsing, coordinate math (easyeda/parser.py)
├── test_parser_extended.py  # Extended parser edge cases (easyeda/parser.py)
├── test_footprint_writer.py # Footprint S-expression output, v8/v9/v10 (kicad/footprint_writer.py)
├── test_symbol_writer.py    # Symbol S-expression output, v8/v9/v10 (kicad/symbol_writer.py)
├── test_model3d.py          # 3D model transforms (kicad/model3d.py)
├── test_kicad_format.py     # Float formatting, UUID generation (kicad/format.py)
├── test_kicad_version.py    # Version constants, feature flags (kicad/version.py)
├── test_library.py          # File I/O, library table management (kicad/library.py)
├── test_library_extended.py # Global paths, config dirs (kicad/library.py)
├── test_cli.py              # CLI import/search commands (cli.py)
├── test_cli_extended.py     # CLI edge cases, filters, output formats (cli.py)
├── test_importer.py         # Component import orchestration (importer.py)
├── test_integration.py      # End-to-end integration tests
└── test_convert_all.py      # Batch conversion of test data
```

Run with:

```bash
pytest tests/ -v --cov=kicad_jlcimport --cov-report=term-missing
```
