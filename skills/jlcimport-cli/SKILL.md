---
name: jlcimport-cli
description: Search and import components (symbols, footprints, and 3D models) from the JLCPCB/LCSC parts catalog into KiCad projects using jlcimport-cli. Use when the user asks to import parts from LCSC/JLCPCB, fetch EasyEDA footprints or symbols, download 3D STEP models, or populate KiCad libraries with Cxxxxx part numbers.
---

# JLCImport CLI — KiCad Component Importer

The `jlcimport-cli` tool fetches component data from the JLCPCB and LCSC catalogs (using EasyEDA backend models) and translates them directly into native KiCad schematic symbols (`.kicad_sym`), PCB footprints (`.kicad_mod`), and 3D models (`.step`, `.wrl`). It automatically registers imported libraries in your KiCad project's `sym-lib-table` and `fp-lib-table`.

## Environment Setup & Invocation Methods

### 1. Sourcing `install.sh` (Local Development Workflow)
The repository provides [`install.sh`](../../install.sh) to automatically create the project virtual environment (`.venv`), install dependencies in editable mode, and expose the `jlcimport-cli` command.

`install.sh` must be **sourced** (not executed directly). On the first run, it creates `.venv`, activates it, and installs all dependencies (`pip install -e '.[dev,gui,tui]'`). On subsequent runs, it activates the existing `.venv`:

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
- `<project>/3dmodels/<MPN>.step` (and `.wrl`) — 3D CAD models
- Updates `<project>/sym-lib-table` and `<project>/fp-lib-table` so KiCad instantly sees `JLCImport:<MPN>`

### 2. Export to a Standalone Directory (No Project Registration)

If you just want the raw `.kicad_sym`, `.kicad_mod`, and 3D files saved to a folder:

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
   ls <project>/3dmodels/<MPN>.step
   ```

## Command Reference

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
| `--lib-name <NAME>` | Name of the library (defaults to `JLCImport`). |
| `--kicad-version {8,9,10}` | KiCad target version formatting (defaults to 8). |
| `--show {footprint,symbol,both}` | Prints generated S-expression directly to stdout. |
| `--insecure` | Skip SSL certificate validation (useful behind enterprise intercepting proxies). |

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
| `--min-stock <N>` | Filter out parts with less than N available stock (default: 1). |
| `--csv` | Output results in CSV format. |
| `--region {global,cn}` | Search global JLCPCB catalog or Chinese SZLCSC catalog. |

## Troubleshooting & Tips

- **TLS / SSL Certificate Errors**: If you encounter an `SSLCertError` when querying EasyEDA endpoints, pass `--insecure` to bypass certificate verification.
- **3D Model Pathing**: When imported into a project with `-p`, 3D models use KiCad's `${KIPRJMOD}` path variable (`${KIPRJMOD}/3dmodels/...`), ensuring the project remains fully portable across different machines and operating systems.
- **Parts Without 3D Models**: Some passive parts (or obscure ICs) may not have an associated 3D STEP model in EasyEDA; the tool will log `(No 3D model)` and successfully generate the symbol and footprint.
