# JLCImport

A KiCad plugin that imports LCSC/JLCPCB parts (symbol, footprint and 3D model) into your project or global library, and writes the files for a JLCPCB assembly order.

- Search parts with stock and Basic/Extended filters, and import them in one step.
- Use a footprint from your KiCad libraries instead of JLC's, when one has the same pads ([details](docs/usage.md#using-a-kicad-library-footprint-instead-of-jlcs)).
- Write the Gerbers, BOM and placement (CPL) file for JLCPCB assembly, with stock checked and rotations solved for each part.
- Works as a KiCad plugin, a CLI, a desktop GUI and a terminal UI, with KiCad 8, 9 and 10.

![Search and details](images/search_results.png)

## Install

1. In KiCad, open **Tools > Plugin and Content Manager**.
2. In the repository settings, add `https://github.com/jvanderberg/kicad_jlcimport/releases/latest/download/repository.json`.
3. Refresh the repositories and install **JLCImport**.

Or install `JLCImport-vX.X.X.zip` (not "Source code") from [Releases](https://github.com/jvanderberg/kicad_jlcimport/releases) with **Install from File**.

## Import a part

Open **PCB Editor > Tools > External Plugins > JLCImport**, search for a part, choose the Project or Global library, and click **Import**. If this creates `sym-lib-table` or `fp-lib-table` for the first time, reopen the project once.

## JLCPCB production files

On the **Production Files** tab, set the number of boards and click **Check JLCPCB**. Every part is looked up at JLCPCB (stock, Basic/Extended, price), and its rotation and position are solved against JLCPCB's own footprint. The summary lists the order, the fees, how well each placement solved, and any problems. **Generate** writes these files to `<project>/production/`:

| File | Contents |
| --- | --- |
| `<board>-gerbers.zip` | Gerbers and drill files (also left unzipped in `gerbers/`) |
| `<board>-bom.csv` | BOM: comment, designators, footprint, LCSC part number |
| `<board>-cpl.csv` | Placements: designator, position, rotation, side |
| `<board>-report.txt` | The check: summary, errors and warnings, and every part's stock and placement |

Before ordering, check the parts the report warns about in JLCPCB's placement preview. To correct a part for good, add a `JLCPCB Rotation Offset` or `JLCPCB Position Offset` field to its footprint. `jlcimport-cli production` does the same from a saved board, with `--json` for scripts. See the [production export guide](docs/production-export.md).

## CLI, GUI and TUI

```bash
jlcimport-cli search "100nF 0402" -t basic
jlcimport-cli import C427602 -p /path/to/project
jlcimport-cli production board.kicad_pcb -n 10
jlcimport-gui -p /path/to/project
jlcimport-tui --kicad-version 9
```

The plugin needs no extra Python packages. The GUI needs `wxPython` (`pip install -e '.[gui]'`); the TUI needs Python 3.10+ and `textual` (`pip install -e '.[tui]'`). For development, run `source install.sh` (macOS/Linux) or `. .\install.ps1` (Windows) to set up the environment, and link `src/kicad_jlcimport` into KiCad's plugin directory.

Settings (mainly the library name) are kept in `jlcimport.json` in KiCad's config directory and shared by the plugin, CLI and TUI.

On Windows with KiCad 9, symbol preview can fail if `wx.svg._nanosvg` is missing; see [fixes/README.md](fixes/README.md).

## Recent Updates

- `v1.6.7`: snap tiny EasyEDA rounding errors to metric/imperial grids, keeping exact mil values.
- `v1.6.5`: fall back to the LCSC product page URL when JLCPCB's search omits it.
- `v1.6.4`: follow KiCad 10's chained (`Table`) fp-lib-tables in the footprint browser.
- `v1.4.0`: KiCad footprint browser with live preview, footprint/3D model renaming, KiCad library footprint reuse.
- `v1.2.10`: Python 3.9 fix in library handling.
- `v1.2.9`: non-modal plugin dialog, multi-unit symbols.
- `v1.2.8`: sturdier SVG path parsing.

## Documentation

- [Usage guide](docs/usage.md)
- [JLCPCB production export](docs/production-export.md)
- [3D models](docs/3d-models.md)
- [Architecture](docs/architecture.md)
- [Visual comparison output](https://jvanderberg.github.io/kicad_jlcimport/)

## License

[MIT](LICENSE)
