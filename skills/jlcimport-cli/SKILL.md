---
name: jlcimport-cli
description: Search and import components (symbols, footprints, and 3D models) from the JLCPCB/LCSC parts catalog into KiCad projects using jlcimport-cli. Use when the user asks to import parts from LCSC/JLCPCB, fetch EasyEDA footprints or symbols, download 3D STEP models, or populate KiCad libraries with Cxxxxx part numbers.
---

# JLCImport CLI — KiCad Component Importer

The `jlcimport-cli` tool fetches component data from the JLCPCB and LCSC catalogs (using EasyEDA backend models) and translates them directly into native KiCad schematic symbols (`.kicad_sym`), PCB footprints (`.kicad_mod`), and 3D models (`.step`, `.wrl`). It automatically registers imported libraries in your KiCad project's `sym-lib-table` and `fp-lib-table`.

## Environment Setup & Invocation Methods

### 1. Sourcing `install.sh` (Local Development Workflow)
The repository provides [`install.sh`](../../install.sh) to automatically create the project virtual environment (`.venv`), install dependencies in editable mode, and expose the `jlcimport-cli` command.

`install.sh` must be **sourced** (not executed directly). On the first run, it creates `.venv`, activates it, and installs the package in editable mode with its dev and GUI dependencies. On subsequent runs, it activates the existing `.venv`:

```bash
# Sourcing within the repository:
source install.sh

# Or sourcing via absolute path from any working directory:
source /path/to/kicad_jlcimport/install.sh
```

Once sourced, `jlcimport-cli` is directly available in your terminal:
```bash
jlcimport-cli <command> [options]
```

You can also run commands as a one-liner:
```bash
source /path/to/kicad_jlcimport/install.sh && jlcimport-cli import C318884 -p /path/to/kicad/project
```

### 2. Global Installation via `pipx` (System-Wide Workflow)
To make `jlcimport-cli` permanently available on your system `$PATH` across all terminal sessions and projects without needing to source `install.sh`:

```bash
# Install directly from the local repository checkout:
pipx install /path/to/kicad_jlcimport

# Or install directly from GitHub:
pipx install git+https://github.com/jvanderberg/kicad_jlcimport.git
```

Once installed via `pipx`, `jlcimport-cli` is callable globally from anywhere:
```bash
jlcimport-cli import C318884 -p /path/to/kicad/project
```

## Core Workflows

### 1. Import Component into a KiCad Project (Recommended)

Imports the symbol, footprint, and 3D model into the project's library folder and automatically updates `sym-lib-table` and `fp-lib-table`:

```bash
# Import a part into a KiCad project directory (where .kicad_pro resides)
jlcimport-cli import <LCSC_ID> -p /path/to/kicad/project

# Example: Import a tactile switch (C318884)
jlcimport-cli import C318884 -p /path/to/kicad/project

# Example: Overwrite existing part if updating
jlcimport-cli import C318884 -p /path/to/kicad/project --overwrite
```

**What this creates inside the project:**
- `<project>/JLCImport.kicad_sym` — Schematic symbol library
- `<project>/JLCImport.pretty/<MPN>.kicad_mod` — PCB footprint file
- `<project>/JLCImport.3dshapes/<MPN>.step` and `.wrl` — 3D models (the footprint references the `.wrl`)
- Updates `<project>/sym-lib-table` and `<project>/fp-lib-table` so KiCad instantly sees `JLCImport:<MPN>`

### 2. Export to a Standalone Directory (No Project Registration)

If you just want the raw `.kicad_sym`, `.kicad_mod`, and 3D files saved to a folder (`<MPN>.kicad_mod`, `<MPN>.kicad_sym`, and `3dmodels/<MPN>.step` / `.wrl`):

```bash
jlcimport-cli import <LCSC_ID> -o ./output_folder

# Example with custom library name
jlcimport-cli import C318884 -o ./output_folder --lib-name MyCustomLib
```

### 3. Search LCSC / JLCPCB Catalog

Search parts by keyword, MPN, or description directly from the terminal:

```bash
# Basic keyword search
jlcimport-cli search "tactile switch"

# Filter for JLCPCB Basic parts only (no $3 extended feeder fee)
jlcimport-cli search "0805 red led" -t basic

# Filter by minimum in-stock quantity and result count
jlcimport-cli search "RP2350" -n 20 --min-stock 100

# Export search results to CSV
jlcimport-cli search "STM32F4" --csv > parts.csv

# Search China's SZLCSC catalog instead of JLCPCB's
jlcimport-cli search "ESP32" --region cn
```

### 4. Inspect / Dry-Run Without Saving

Preview the generated KiCad S-expression code in stdout without writing any files:

```bash
# Preview footprint only
jlcimport-cli import C318884 --show footprint

# Preview symbol only
jlcimport-cli import C318884 --show symbol

# Preview both
jlcimport-cli import C318884 --show both
```

### 5. JLCPCB Assembly Check, BOM and CPL

Check every LCSC part on a board (stock for the board quantity, Basic/Extended, price) and solve each part's CPL rotation and position against JLC's own footprint, then write `<board>-gerbers.zip`, `<board>-bom.csv`, `<board>-cpl.csv` and `<board>-report.txt`. The Gerbers and drill files are plotted with KiCad's `kicad-cli` (found on `PATH`, in the usual install folder, or via `--kicad-cli` / `$KICAD_CLI`); `--no-gerbers` skips them. Zones are refilled in memory first with KiCad 10's `kicad-cli`; the board file is never modified.

```bash
# Check for 10 boards and write files to <project>/production/
jlcimport-cli production board.kicad_pcb -n 10

# Report only; exit status 1 on errors (invalid LCSC number, not found, out of stock, ...).
# Parts with no LCSC number at all are only warnings.
jlcimport-cli production board.kicad_pcb --check-only

# Placements only, skipping the stock/price lookup
jlcimport-cli production board.kicad_pcb --no-stock

# Machine-readable result: summary, issues, per-part stock/price/links, placements, BOM/CPL rows, files written
jlcimport-cli production board.kicad_pcb --check-only --json

# BOM, CPL and report only (no kicad-cli needed)
jlcimport-cli production board.kicad_pcb --no-gerbers
```

With `--json`, stdout holds only the JSON document (progress and messages go to stderr), so it can be piped to `jq`: `... --check-only --json | jq '.issues[] | select(.severity == "error")'`.

Read the report before ordering: low-confidence placements and every polarized part (LEDs, diodes, tantalum caps) are listed for checking in JLCPCB's placement preview, and stock that only just covers the order is a warning. Per-part corrections are footprint fields (`JLCPCB Rotation Offset`, `JLCPCB Position Offset`); see `docs/production-export.md`.

## Verification

After importing a component into a KiCad project, verify the files and table registrations:

1. **Verify Footprint**:
   Check that the footprint `.kicad_mod` file exists in the pretty library:
   ```bash
   ls <project>/JLCImport.pretty/<MPN>.kicad_mod
   ```

2. **Verify Schematic Symbol**:
   Confirm the symbol definition exists in the library file:
   ```bash
   grep -E '\(symbol "<MPN>"' <project>/JLCImport.kicad_sym
   ```

3. **Verify Library Tables**:
   Confirm that the `JLCImport` library was registered in both project tables:
   ```bash
   grep 'JLCImport' <project>/sym-lib-table <project>/fp-lib-table
   ```

4. **Verify 3D Model (if available)**:
   ```bash
   ls <project>/JLCImport.3dshapes/<MPN>.step <project>/JLCImport.3dshapes/<MPN>.wrl
   ```

## Command Reference

`--insecure` is a top-level option and goes before the command: `jlcimport-cli --insecure import C318884 -p ...`. The commands have short forms: `s` (search), `i` (import) and `p` (production).

### `jlcimport-cli import`

```text
usage: jlcimport-cli import [-h] [--show {footprint,symbol,both}]
                            [-o OUTPUT | -p PROJECT | --global]
                            [--global-lib-dir DIR] [--overwrite]
                            [--lib-name LIB_NAME] [--kicad-version {8,9,10}]
                            part
```

| Flag | Description |
| :--- | :--- |
| `part` | LCSC Part Number (e.g. `C318884`, `C427602`). Leading 'C' is optional. |
| `-p`, `--project <DIR>` | Target KiCad project directory (updates `sym-lib-table` & `fp-lib-table`). |
| `-o`, `--output <DIR>` | Export files to directory without updating library tables. |
| `--global` | Import into KiCad global 3rd-party library. |
| `--overwrite` | Overwrite existing symbol or footprint if already imported. |
| `--global-lib-dir <DIR>` | Use this global library folder for this run. |
| `--lib-name <NAME>` | Name of the library (default: the name saved in `jlcimport.json`, else `JLCImport`). |
| `--kicad-version {8,9,10}` | KiCad target version formatting (default: 10). |
| `--show {footprint,symbol,both}` | Prints generated S-expression directly to stdout. |

### `jlcimport-cli search`

```text
usage: jlcimport-cli search [-h] [-n COUNT] [-t {basic,extended,both}]
                            [--min-stock N] [--csv] [--region {global,cn}]
                            keyword
```

| Flag | Description |
| :--- | :--- |
| `keyword` | Search term (e.g. `"100nF 0402"`, `"TS-1187A"`, `"ESP32"`). |
| `-t`, `--type` | Filter by JLCPCB part type: `basic`, `extended`, or `both` (default: `both`). |
| `-n`, `--count` | Number of results to return (default: 10). |
| `--min-stock <N>` | Filter out parts with less than N available stock (default: 1; `0` for any). |
| `--csv` | Output results in CSV format. |
| `--region {global,cn}` | Search the global JLCPCB catalog or the Chinese SZLCSC catalog (default: the region saved in `jlcimport.json`, else `global`). |

### `jlcimport-cli production`

```text
usage: jlcimport-cli production [-h] [-n BOARDS] [-o OUTPUT] [--check-only]
                                [--force] [--no-stock] [--no-gerbers]
                                [--no-refill-zones] [--kicad-cli PATH]
                                [--json]
                                board
```

| Flag | Description |
| :--- | :--- |
| `board` | Path to the `.kicad_pcb` file. |
| `-n`, `--boards <N>` | Boards to assemble, for the stock check (default: 5). |
| `-o`, `--output <DIR>` | Output directory (default: `<project>/production`). |
| `--check-only` | Print the report and write nothing; exit 1 if there are errors. |
| `--force` | Write the files even when the check reports errors. |
| `--no-stock` | Skip the stock/price lookup; placements are still solved. |
| `--no-gerbers` | Write only BOM, CPL and report (no `kicad-cli` needed). |
| `--no-refill-zones` | Plot zones as saved instead of refilling them first (refilling needs KiCad 10's `kicad-cli`). |
| `--kicad-cli PATH` | `kicad-cli` to plot Gerbers with (default: `$KICAD_CLI`, `PATH`, usual install folders). |
| `--json` | Print the full result as JSON on stdout. |

## Troubleshooting & Tips

- **TLS / SSL Certificate Errors**: If you encounter an `SSLCertError` when querying EasyEDA endpoints, pass `--insecure` before the command (`jlcimport-cli --insecure import ...`) to bypass certificate verification.
- **3D Model Pathing**: When imported into a project with `-p`, 3D models use KiCad's `${KIPRJMOD}` path variable (`${KIPRJMOD}/JLCImport.3dshapes/<MPN>.wrl`), ensuring the project remains fully portable across different machines and operating systems.
- **Parts Without 3D Models**: Some passive parts (or obscure ICs) may not have an associated 3D STEP model in EasyEDA; the tool will log `No 3D model available` and successfully generate the symbol and footprint.
