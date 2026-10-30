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

1. Search by keyword or LCSC part number.
2. Filter results by part type and stock.
3. Choose destination: project library or global library.
4. Set library name if needed.
5. Import symbol, footprint, and 3D model.

If `sym-lib-table` or `fp-lib-table` is created for the first time, reopen the project.

### Using a KiCad library footprint instead of JLC's

Before an import, the metadata dialog (plugin, GUI and TUI) can offer a footprint from your KiCad libraries in place of the one converted from JLC's EasyEDA footprint. JLCPCB places parts by its own footprint, so a footprint is only offered when it has the same land pattern:

1. **Name.** Footprints whose names share a package token with JLC's package (`SOT-363`, `QFN-24`, `SMA`, `0603`, ...) or a part number (`B4B-PH-K`) are considered, unless the name states a different pitch, body size or exposed pad. For two-pad parts only the kind of footprint the symbol's designator implies is considered: `R_` for `R`, `C_` / `CP_` for `C`, `LED_` for `LED`, `D_` for `D`, and so on. Two-pad parts with any other designator get no suggestion.
2. **Pads.** The footprint must have the same pad numbers as JLC's (pads sharing a number count once) and the same through-hole pads, because the symbol's pins connect by number.
3. **Fit.** JLC's pad centres are fitted onto the footprint's pads with the same solver production export uses. The fit must be within 0.05 mm RMS at a 90° rotation step, with numbering that isn't mirrored and pad sizes within 50%. That is the fit production export needs for a high-confidence placement.

When several footprints pass, standard variants beat HandSolder ones, then the lowest residual and the closest pad sizes win. The dialog shows the fit (pad count, RMS residual, pad-size difference).

For polarized two-terminal parts (LEDs, diodes, polarized capacitors), the `+` / `-` marks on JLC's footprint, or else its symbol's pin names, are checked against KiCad's convention for the suggested footprint: pad 1 is the cathode on `LED_` / `D_` footprints and `+` on `CP_` footprints, and `_AK` / `_KA` in the name states the order. A polarized part only gets a polarized footprint. If KiCad's pad 1 is the other polarity from JLC's pad 1, the dialog says the polarity is reversed: the imported symbol's pins would land on the opposite pads.

JLC's footprint is the default choice unless the suggestion fits with no polarity caveat. When no footprint fits, none is offered and JLC's footprint is imported. **Browse…** lets you pick any footprint, but a footprint you pick yourself is not checked against JLC's pads.

## Local Development Environment

```bash
source install.sh      # macOS/Linux
. .\install.ps1        # Windows PowerShell
```

These scripts create or activate the local virtual environment and install project commands.

## CLI Reference

`jlcimport-cli` has three main commands: `search`, `import` and `production`.

### CLI Quick Index

| Task | Command Pattern |
| --- | --- |
| Search parts | `jlcimport-cli search "<query>" [options]` |
| Import to project | `jlcimport-cli import <LCSC_ID> -p /path/to/project [options]` |
| Import to global library | `jlcimport-cli import <LCSC_ID> --global [options]` |
| Export files only | `jlcimport-cli import <LCSC_ID> -o ./output [options]` |
| Print generated text | `jlcimport-cli import <LCSC_ID> --show footprint|symbol|both` |
| JLCPCB BOM/CPL + availability check | `jlcimport-cli production board.kicad_pcb [-n BOARDS] [--check-only] [--no-stock]` (see [production export](production-export.md)) |
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
```

Useful search flags:

- `-t, --type basic|extended|both` (default `both`)
- `-n, --count N` number of results (default `10`)
- `--min-stock N` minimum stock (default `1`)
- `--csv` machine-readable output

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

- `--lib-name MyParts` to use a custom library name.
- `--show footprint|symbol|both` to print generated text.
- `-o, --output DIR` export-only mode.
- `-p, --project DIR` import into project libraries.
- `--global` import into global 3rd-party libraries.
- `--global-lib-dir DIR` one-run override of global path.
- `--overwrite` replace existing entries.
- `--kicad-version 8|9|10` target version-specific formats.

### Network / TLS Example

Use `--insecure` only when TLS interception on your network breaks certificate checks:

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
```

## Standalone TUI

Install TUI dependencies:

```bash
pip install -e '.[tui]'
```

Run:

```bash
jlcimport-tui
jlcimport-tui -p /path/to/project
jlcimport-tui --kicad-version 9
```

TUI requires Python 3.10+.

## macOS Gatekeeper For Release Binaries

If macOS blocks downloaded binaries from Releases:

```bash
xattr -cr jlcimport-cli/
xattr -cr jlcimport-tui/
xattr -cr jlcimport-gui/
```

## Configuration File

Settings are stored in `jlcimport.json`:

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
