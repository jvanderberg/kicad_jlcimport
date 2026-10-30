# Visual Testing

## Comparison Testing

The `tools/compare_part.py` script fetches EasyEDA preview SVGs and renders the KiCad conversion output side-by-side in an HTML page. Use this before releases to verify that symbols and footprints convert correctly across a variety of real-world parts.

### Prerequisites

- `kicad-cli` on `$PATH` (installed with KiCad)
- Virtual environment activated: `source install.sh`

### Running

```bash
source install.sh
python3 tools/compare_part.py <LCSC_ID> [<LCSC_ID> ...]
```

Options:
- `--no-open` — generate the HTML without opening it in a browser

The script writes an HTML comparison page to a temp directory and opens it in your default browser.

### What to check

For each part, compare the EasyEDA (source) and KiCad (output) renderings:

- **Symbols**: Pin count, pin numbers, pin names, pin placement, body shape
- **Footprints**: Pad count, pad shapes and sizes, silk screen markings (+/- polarity, pin 1 indicators), outline shape

### Pre-release regression suite

Run the full regression list (ICs, passives, LEDs, transistors, connectors and more), kept in `tools/regression_parts.txt`:

```bash
python3 tools/compare_part.py --regression
```

The page opens with a summary of the automated 3D model checks at the top, and each part shows its own result. Failed and warning parts are linked so you can go straight to them. The same checks run in CI without KiCad, see "Automated 3D model checks" below.

## Automated 3D model checks

`tests/test_model_regression.py` checks the 3D model placement of every regression part with no rendering:

- **Physical checks** (`src/kicad_jlcimport/kicad/model_checks.py`): the model must sit on the board, nothing but pins may pass through the board outside a hole (this catches upside-down parts), the model must cover its pads, and its pins must reach them.
- **Snapshot**: each part's offset, rotation and check result is stored in `testdata/models/snapshot.json`. Any change fails the test with a per-part diff.

The parts' EasyEDA data is stored in `testdata/models/` so the tests need no network. After an intentional change to placement:

```bash
python3 tools/update_model_snapshot.py   # prints what changed
git diff testdata/models/snapshot.json   # review, then commit
```

To refresh the fixtures from EasyEDA (for example after adding parts to `tools/regression_parts.txt`), run `python3 tools/fetch_model_fixtures.py`, then update the snapshot as above.
