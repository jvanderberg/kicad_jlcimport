# Usage Guide

This guide keeps the practical setup and command details that do not fit well in the short project README.

## Install In KiCad (PCM ZIP)

1. Download `JLCImport-vX.X.X.zip` from [Releases](https://github.com/jvanderberg/kicad_jlcimport/releases).
2. In KiCad, open `Tools > Plugin and Content Manager`.
3. Click `Install from File...`.
4. Select the ZIP and apply pending changes.

Use the packaged `JLCImport-vX.X.X.zip`, not the auto-generated source archive.

## Manual Install (Dev Workflow)

Link `src/kicad_jlcimport` into KiCad's plugin directory:

```bash
ln -s /path/to/kicad_jlcimport/src/kicad_jlcimport <plugins-dir>/kicad_jlcimport
```

Common plugin directories:

| OS | Path |
| --- | --- |
| macOS | `~/Documents/KiCad/<version>/scripting/plugins/` |
| Linux | `~/.local/share/kicad/<version>/scripting/plugins/` |
| Windows | `%APPDATA%\kicad\<version>\scripting\plugins\` |

Restart KiCad after install.

## Plugin Workflow

Open `PCB Editor > Tools > External Plugins > JLCImport`.

1. Search by keyword or LCSC part number. **Region** picks the catalog: Global (JLCPCB) or China (SZLCSC).
2. Filter results by part type and stock.
3. Choose destination: project library or global library.
4. Set library name if needed.
5. Click **Import**. The metadata dialog lets you edit the description and keywords, rename the footprint and 3D model, and use a KiCad library footprint instead of JLC's (see below). It then imports the symbol, footprint, and 3D model.

If `sym-lib-table` or `fp-lib-table` is created for the first time, reopen the project.

Opened from the PCB Editor, the dialog also has a **Production Files** tab for JLCPCB assembly orders; see [production export](production-export.md).

### Using a KiCad library footprint instead of JLC's

Before an import, the metadata dialog (plugin and GUI) can offer a footprint from your KiCad libraries in place of the one converted from JLC's EasyEDA footprint. JLCPCB places parts by its own footprint, so a footprint is only offered when it has the same land pattern:

1. **Name.** Footprints whose names share a package token with JLC's package (`SOT-363`, `QFN-24`, `SMA`, `0603`, ...) or a part number (`B4B-PH-K`) are considered, unless the name states a different pitch, body size or exposed pad. A pitch or size on its own (`P=2.54mm`) doesn't count as a part number. For two-pad parts only the kind of footprint the symbol's designator implies is considered: `R_` for `R`, `C_` / `CP_` for `C`, `LED_` for `LED`, `D_` for `D`, and so on. Two-pad parts with any other designator get no suggestion.
2. **Pads.** The footprint must have the same pad numbers as JLC's (pads sharing a number count once) and the same through-hole pads, because the symbol's pins connect by number.
3. **Fit.** JLC's pad centres are fitted onto the footprint's pads with the same solver production export uses. The fit must be within 0.05 mm RMS at a 90° rotation step, which is the fit production export needs for a high-confidence placement; a footprint numbered in mirror image can't fit that closely. The mean pad-size difference must be within 50%.
4. **Every pad.** After the fit, every pad on either footprint must lie on a pad with the same number on the other. That catches a pin that moved to another pad in a large package, which the RMS alone would average away, and a pad one footprint has and the other doesn't (an exposed pad's tie bars, say). An exposed pad split into segments on one side and whole on the other still fits.

When several footprints pass, one with no polarity caveat wins, then standard variants beat HandSolder ones, then the lowest residual and the closest pad sizes win. The dialog shows the fit (pad count, RMS residual, pad-size difference).

For polarized two-terminal parts (LEDs, diodes, polarized capacitors), the `+` / `-` marks on JLC's footprint, or else its symbol's pin names, are checked against KiCad's convention for the suggested footprint: pad 1 is the cathode on `LED_` / `D_` footprints and `+` on `CP_` footprints, and `_AK` / `_KA` in the name states the order. A polarized part only gets a polarized footprint; a capacitor counts as polarized when JLC says which pad is `+` or when its symbol draws a `+`. If KiCad's pad 1 is the other polarity from JLC's pad 1, the dialog says the polarity is reversed: the imported symbol's pins would land on the opposite pads. If JLC doesn't say which pad is which, or its footprint marks and symbol pin names disagree, the dialog says the polarity can't be checked. Parts with more pads (an RGB LED, say) have no polarity caveat: their pad numbers already fix which pad is which.

JLC's footprint is the default choice unless the suggestion fits with no polarity caveat. When no footprint fits, none is offered and JLC's footprint is imported. **Browse…** lets you pick any footprint, but a footprint you pick yourself is not checked against JLC's pads.

## Local Development Environment

```bash
source install.sh      # macOS/Linux
. .\install.ps1        # Windows PowerShell
```

These scripts create or activate the local virtual environment and install project commands.

## CLI Reference

`jlcimport-cli` has three main commands: `search`, `import` and `production` (short forms `s`, `i` and `p`).

### CLI Quick Index

| Task | Command Pattern |
| --- | --- |
| Search parts | `jlcimport-cli search "<query>" [options]` |
| Import to project | `jlcimport-cli import <LCSC_ID> -p /path/to/project [options]` |
| Import to global library | `jlcimport-cli import <LCSC_ID> --global [options]` |
| Export files only | `jlcimport-cli import <LCSC_ID> -o ./output [options]` |
| Print generated text | `jlcimport-cli import <LCSC_ID> --show footprint|symbol|both` |
| JLCPCB Gerbers/BOM/CPL + availability check | `jlcimport-cli production board.kicad_pcb [-n BOARDS] [--check-only] [--json] [--no-stock] [--no-gerbers]` (see [production export](production-export.md)) |
| Use insecure TLS mode | `jlcimport-cli --insecure <command> ...` |

### Search Examples

```bash
# Basic search
jlcimport-cli search "100nF 0402" -t basic

# Extended parts only
jlcimport-cli search "ESP32" -t extended

# More results with stock filter
jlcimport-cli search "ESP32" -n 20 --min-stock 100

# Include out-of-stock parts
jlcimport-cli search "RP2350" --min-stock 0

# Export results to CSV
jlcimport-cli search "RP2350" --csv > parts.csv

# Search China's SZLCSC catalog instead of JLCPCB's
jlcimport-cli search "ESP32" --region cn
```

Useful search flags:

- `-t, --type basic|extended|both` (default `both`)
- `-n, --count N` number of results (default `10`)
- `--min-stock N` minimum stock (default `1`)
- `--csv` machine-readable output
- `--region global|cn` catalog to search: JLCPCB (global) or SZLCSC (China). Defaults to the region last chosen in the plugin or GUI, else `global`.

### Import Examples

```bash
# Preview generated content in terminal (no files written)
jlcimport-cli import C427602 --show both

# Export-only mode to a directory
jlcimport-cli import C427602 -o ./output

# Import directly into a KiCad project
jlcimport-cli import C427602 -p /path/to/project

# Import into KiCad global 3rd-party library
jlcimport-cli import C427602 --global

# Overwrite existing symbol/footprint/models during re-import
jlcimport-cli import C427602 -p /path/to/project --overwrite

# Use a custom library name
jlcimport-cli import C427602 -p /path/to/project --lib-name MyParts

# Target specific KiCad format/path behavior
jlcimport-cli import C427602 --global --kicad-version 8
jlcimport-cli import C427602 --global --kicad-version 9
jlcimport-cli import C427602 --global --kicad-version 10

# Override global library directory for one run
jlcimport-cli import C427602 --global-lib-dir /path/to/libs
```

Useful import flags:

- `--lib-name MyParts` to use a custom library name (default: the name in `jlcimport.json`, else `JLCImport`).
- `--show footprint|symbol|both` to print generated text.
- `-o, --output DIR` export-only mode.
- `-p, --project DIR` import into project libraries.
- `--global` import into global 3rd-party libraries.
- `--global-lib-dir DIR` one-run override of global path.
- `--overwrite` replace existing entries.
- `--kicad-version 8|9|10` target version-specific formats (default `10`).

Export-only mode (`-o`) writes `<name>.kicad_mod`, `<name>.kicad_sym` and a `3dmodels/` folder with the `.step` and `.wrl`, and touches no library tables. Project and global imports write into `<lib_name>.kicad_sym`, `<lib_name>.pretty/` and `<lib_name>.3dshapes/` (see [library structure](architecture.md#library-structure)).

### Network / TLS Example

Use `--insecure` only when TLS interception on your network breaks certificate checks. It goes before the command, and `jlcimport-gui` accepts it too:

```bash
jlcimport-cli --insecure search "STM32"
jlcimport-cli --insecure import C427602 -p /path/to/project
```

## Standalone GUI

Install GUI dependencies:

```bash
pip install -e '.[gui]'
```

Run:

```bash
jlcimport-gui
jlcimport-gui -p /path/to/project
jlcimport-gui --global
jlcimport-gui --kicad-version 9
jlcimport-gui --global-lib-dir /path/to/libs
```

Without `-p` or `--global`, it asks for a project folder; Cancel uses only the global library. `--kicad-version` defaults to `10`.

## macOS Gatekeeper For Release Binaries

If macOS blocks downloaded binaries from Releases:

```bash
xattr -cr jlcimport-cli/
xattr -cr jlcimport-gui/
```

## Configuration File

Settings are stored in `jlcimport.json`, shared by the plugin, GUI and CLI. It holds the library name (`lib_name`), whether imports go to the global library (`use_global`), a custom global library folder (`global_lib_dir`, empty for KiCad's default `3rdparty` folder) and the search region (`region`, `global` or `cn`).

| OS | Path |
| --- | --- |
| macOS | `~/Library/Preferences/kicad/jlcimport.json` |
| Linux | `~/.config/kicad/jlcimport.json` |
| Windows | `%APPDATA%\kicad\jlcimport.json` |

## Download Cache

Footprint and symbol data downloaded from EasyEDA is kept for a week, so the import previews, imports and production checks don't fetch the same part twice. Stock and prices are always fetched live. To get fresh footprint data sooner, delete the folder; set `JLCIMPORT_CACHE_DIR` to put it somewhere else.

| OS | Path |
| --- | --- |
| macOS | `~/Library/Caches/kicad_jlcimport` |
| Linux | `~/.cache/kicad_jlcimport` (or `$XDG_CACHE_HOME/kicad_jlcimport`) |
| Windows | `%LOCALAPPDATA%\kicad_jlcimport` |

## Troubleshooting

- Missing symbol preview on Windows KiCad 9: see [fixes/README.md](../fixes/README.md).
- Deeper conversion and architecture notes: see [architecture.md](architecture.md).
