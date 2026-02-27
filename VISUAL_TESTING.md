# Visual Testing

## Comparison Testing

The `tools/compare_part.py` script fetches EasyEDA preview SVGs and renders the KiCad conversion output side-by-side in an HTML page. Use this before releases to verify that symbols and footprints convert correctly across a variety of real-world parts.

### Prerequisites

- KiCad installed at `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`
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

Run the full 101-part comparison across ICs, passives, LEDs, transistors, connectors, and other component types:

```bash
python3 tools/compare_part.py \
    C1002 C1027 C1034 C1035 C2040 C2078 C2079 C2102 C2135 C2173 \
    C2202 C2203 C2213 C2286 C2288 C2290 C2295 C2297 C2316 C2318 \
    C2319 C2320 C2321 C2322 C2474 C2476 C2478 C2479 C2560 C2561 \
    C2562 C2564 C2566 C2765186 C2914 C2957 C2960 C3099 C3105 C3116 C3117 \
    C3794 C3795 C3797 C5177 C5206 C5213 C5419 C5423 C5586 C5602 \
    C5612 C5613 C6179 C6186 C6187 C6188 C6203 C6205 C6855 C6912 \
    C6970 C7063 C7519 C7593 C7950 C8760 C8852 C9418 C10081 C12087 \
    C12889 C13885 C14529 C15134 C15157 C15331 C15999 C18901 C20953 C21235 \
    C23892 C24112 C33696 C67528 C71101 C82899 C85181 C94598 C94599 C138392 \
    C173573 C173580 C173602 C347218 C347219 C385834 C386756 C386757 C386758 C393939 C395958 \
    C52016391 C160404
```
