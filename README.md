# JLCImport

A KiCad 9 Action Plugin that imports symbols, footprints, and 3D models directly from LCSC/JLCPCB into your KiCad project or global library.

![Search and details](images/search_results.png)

![Gallery view](images/gallery.png)

## Features

- Search the JLCPCB parts catalog with filtering (Basic/Extended, minimum stock)
- Preview product images with full-screen gallery view
- Import symbols, footprints, and STEP/WRL 3D models
- Sortable results by price, stock, part number, description, etc.
- Checkmark (✓) indicator on parts already imported to your library
- Configurable library name (defaults to "JLCImport", persisted across sessions)
- Import to project folder or global 3rd-party library
- Append to existing KiCad libraries without conflict
- Links to datasheets and LCSC product pages
- CLI tool for scripted/batch imports

## Installation

### Find your KiCad plugins directory

| OS | Path |
|----|------|
| macOS | `~/Documents/KiCad/9.0/scripting/plugins/` |
| Linux | `~/.local/share/kicad/9.0/scripting/plugins/` |
| Windows | `%APPDATA%\kicad\9.0\scripting\plugins\` |

> Replace `9.0` with your KiCad version if different.

### Option 1: Symlink (recommended for development)

```bash
ln -s /path/to/kicad_jlcimport <plugins-dir>/kicad_jlcimport
```

### Option 2: Copy

```bash
cp -r /path/to/kicad_jlcimport <plugins-dir>/kicad_jlcimport
```

### Activate

1. Restart KiCad
2. Open the PCB Editor
3. The plugin appears in **Tools > External Plugins > JLCImport**

## Usage

### Plugin Dialog

1. Open the PCB Editor and launch **JLCImport** from the External Plugins menu
2. Type a search query (e.g. "100nF 0402", "ESP32", "RP2350")
3. Filter by type (Basic/Extended/Both) and minimum stock level
4. Parts already in your library are marked with ✓ in the results list
5. Click a result to see details, product image, and description
6. Click the thumbnail to open the full-screen gallery with arrow navigation
7. Choose a destination:
   - **Project** — saves to the current board's directory
   - **Global** — saves to KiCad's 3rd-party library folder
8. Optionally change the **Library** name (defaults to "JLCImport", remembered across sessions). You can point this at an existing library to append to it.
9. Click **Import** to download and save the symbol, footprint, and 3D model

> **Note:** If library tables (`sym-lib-table` / `fp-lib-table`) are newly created, reopen the project for them to take effect.

### CLI

The CLI tool can be used outside KiCad for testing or scripted imports:

```bash
# Search (default: --min-stock 1, only shows parts in stock)
python3 cli.py search "100nF 0402" -t basic
python3 cli.py search "ESP32" -n 20 --min-stock 100

# Search with CSV output
python3 cli.py search "RP2350" --csv > parts.csv

# Import (prints generated output)
python3 cli.py import C427602 --show both

# Import to directory (saves .kicad_sym, .kicad_mod, and 3D models)
python3 cli.py import C427602 -o ./output

# Import using a custom library name
python3 cli.py import C427602 -o ./output --lib-name MyParts
```

### TUI

A terminal-based interface with image preview support (Sixel, Kitty, iTerm2, or halfcell fallback).

![TUI interface](images/tui.png)

```bash
# Install TUI dependencies (requires Python 3.10+)
pip install textual "textual-image[textual]" Pillow

# Run from the project directory
python3 -m tui -p /path/to/kicad/project

# Run without project (global library only)
python3 -m tui
```

Features:
- Search with sortable columns (click headers to sort)
- Filter by type (Basic/Extended) and package
- Thumbnail preview with loading skeleton animation
- Click thumbnail or press `Ctrl+G` to open full-screen gallery
- Gallery navigation with arrow keys, `Escape` to return
- Configurable library name (shared with the plugin)
- Import directly from detail view or import section
- Links to datasheets and LCSC product pages

## Configuration

Settings are stored in `jlcimport.json` in your KiCad config directory:

| OS | Path |
|----|------|
| macOS | `~/Library/Preferences/kicad/jlcimport.json` |
| Linux | `~/.config/kicad/jlcimport.json` |
| Windows | `%APPDATA%\kicad\jlcimport.json` |

Currently stores the library name preference. Shared between the plugin, TUI, and CLI.

## How It Works

JLCImport fetches component data from the EasyEDA/LCSC API, parses the proprietary shape format, and converts it to KiCad 9 file formats:

- **Symbols** → `.kicad_sym` (version 20241209)
- **Footprints** → `.kicad_mod` (version 20241229)
- **3D Models** → `.step` and `.wrl`

The plugin automatically creates and updates `sym-lib-table` and `fp-lib-table` entries so imported parts are immediately available in your schematic and PCB editors.

For a detailed look at the architecture, data flow, module responsibilities, and external APIs, see the [Architecture Documentation](docs/architecture.md).

## Requirements

- KiCad 9.0+
- Python 3 (bundled with KiCad)
- Internet connection

The plugin requires no additional Python packages — it uses only the standard library and `wx` (both bundled with KiCad). The TUI requires Python 3.10+ and additional packages (see [TUI](#tui) above).

## License

See [LICENSE](LICENSE) for details.
