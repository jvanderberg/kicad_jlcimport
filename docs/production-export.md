# JLCPCB Production Export

The **Production Files** tab of the JLCImport dialog (PCB Editor > Tools > External Plugins > JLCImport) produces everything a JLCPCB PCBA order needs:

- `<board>-gerbers.zip`: Gerbers for every copper layer, the masks, paste, silkscreen and Edge.Cuts, plus Excellon drill files (mm, separate PTH/NPTH)
- `<board>-bom.csv`: `Comment,Designator,Footprint,JLCPCB Part #`, grouped by value + footprint + LCSC number
- `<board>-cpl.csv`: `Designator,Mid X,Mid Y,Rotation,Layer`
- `<board>-report.txt`: the check results

Files go to `<project>/production/` unless you choose another folder.

## Workflow

1. Set **Boards to assemble**.
2. Click **Check**. Every unique LCSC part is looked up in the JLCPCB assembly library, and JLC's own footprint for that part is fetched. The table then shows, for each footprint:
   - stock status: missing or invalid LCSC number, not found, out of stock, or stock lower than quantity per board × boards
   - Basic or Extended (each Extended part adds a JLCPCB setup fee)
   - the solved CPL rotation, and how much to trust it (confidence plus notes)

   Rows with errors are shown in red and rows with warnings in yellow. The summary lists the extended parts and an estimated parts cost.
3. Click **Generate**. If the check found problems, you are asked before any files are written. You can always go ahead anyway.

DNP footprints and footprints excluded from the BOM are left out of both the BOM and the CPL. Footprints excluded from position files stay in the BOM but are left out of the CPL.

The LCSC number is read from the first non-empty footprint field named `LCSC`, `LCSC Part`, `LCSC Part #`, `LCSC Part Number`, `LCSC#`, `JLCPCB Part #`, `JLCPCB Part`, `JLCPCB`, `JLC` or `JLC Part` (case-insensitive).

## How CPL rotation and position are solved

JLCPCB places each part using the orientation and origin of **its own EasyEDA footprint** for that LCSC part. Your KiCad footprint may use a different 0° orientation (a stock KiCad library footprint, for example) and a different origin (pin 1 instead of the centre). This plugin doesn't use a rotation lookup table. It fits JLC's footprint onto your actual pads instead:

1. Pads are paired by pad number (whitespace is trimmed and case is ignored). Two-pad polarized parts are the exception (see [Polarity of two-pad parts](#polarity-of-two-pad-parts)). Unnumbered pads are ignored. Pads that share a number (split thermal pads, several GND or shield pads) are reduced to the centroid of their group on each footprint.
2. The closed-form 2D least-squares rigid fit (rotation + translation, no scale) is computed:

   ```
   qc, pc = centroids of JLC points q and board points p
   a = q - qc,  b = p - pc
   phi = atan2( Σ a×b , Σ a·b )
   t   = pc - R(phi) qc
   ```

   `t` is where JLC's footprint origin lands on the board (CPL Mid X/Y), and `phi` is the CPL rotation.
3. The angle snaps to the nearest multiple of 90°, or to KiCad's own orientation plus a multiple of 90° (useful for parts placed at 45°), when it is within 1°. The fit residual is then recomputed.
4. The fit is also computed with the opposite handedness. If that fit is clearly better, the pad numbering is mirrored, and the solver flags it rather than guessing.
5. If the number-based fit is rejected (and isn't mirrored), pads are matched **by position**. This handles a JLC footprint that has been renumbered since you imported it, such as a USB-C connector whose pads became `A1B12`, `A4B9`, and so on. The pad centroids are aligned, every 90° step (and KiCad's angle plus 90° steps) is tried, and each JLC pad takes the number of the nearest board pad. The match is accepted only if exactly one numbering fits within 0.25 mm. A symmetric pattern that fits several ways round is rejected. A position match is at most medium confidence.

| Confidence | Meaning | CPL uses |
| --- | --- | --- |
| high | RMS residual ≤ 0.05 mm and all pad numbers match | solved values |
| medium | RMS ≤ 0.25 mm (land patterns differ slightly), some pad numbers unmatched, pads matched by position, or polarity from KiCad's convention or unresolved | solved values |
| unverified | the fit was rejected (high residual or wrong package, mirrored numbering, mostly unmatched pads, fewer than 2 common pad numbers, orientation undetermined), or there is no LCSC number, or the lookup failed | KiCad's own pose, with a warning |
| manual | override fields present | KiCad's pose plus your offsets |

### Polarity of two-pad parts

Two pad centres always form a symmetric pattern, so geometry can't tell a part from its 180° turn. Polarity comes only from which pad is paired with which, and pad numbers are not reliable for that. KiCad's diode and LED symbols put the cathode on pin 1. Many JLC symbols put the anode there (the C2286 0603 red LED does), and others don't (the C64885 B5819W has its cathode on pin 1). Pairing by number would mount such a part backwards.

For two-pad parts the solver pairs pads by **role** (positive = anode/`+`, negative = cathode/`-`) when it can:

- **JLC side:** JLC's own symbol pin names (`A`/`K`, `Anode`/`Cathode`, `+`/`-`). The symbol is only fetched for two-pad parts.
- **Board side:** the pad's pin function, which is the schematic pin name KiCad stores on each pad (`K`, `A_2`, …). If the board pads carry no usable names and are numbered 1 and 2, KiCad's library convention is assumed: diode/LED pad 1 = cathode, polarized capacitor pad 1 = `+`. That assumption lowers the result to medium confidence.

| JLC symbol | Board pads | Result |
| --- | --- | --- |
| no polarity names (e.g. pins `1`/`2`) | any | paired by number, with a note to check polarity |
| polarity names | pin names with polarity | paired by role, confidence unchanged |
| polarity names | numbered 1/2, no names | paired by role using KiCad's convention, medium |
| polarity names | other numbering, no names | paired by number, medium, **warning** to check orientation |

When pairing by role turns out to be the reverse of the pad numbering, the report says so ("JLC numbers this part's pads the other way round").

## Manual overrides

You can add these fields to a footprint (in footprint properties):

| Field | Value |
| --- | --- |
| `JLCPCB Rotation Offset` | degrees, CCW as viewed from the top, e.g. `90`, `-90`, `180` |
| `JLCPCB Position Offset` | `dx, dy` in mm, in CPL coordinates (X right, Y up, viewed from the top), e.g. `0.5, -0.25` |

If either field is present, the CPL row is **KiCad's own position and rotation plus these offsets**, and the solver result is ignored. The solver's suggestion is still shown in the notes. The result therefore doesn't depend on network access. A rotation offset of `0` means "trust KiCad for this part".

## Coordinate conventions

All conventions are defined in `src/kicad_jlcimport/production/conventions.py`:

- **board**: KiCad board coordinates, mm, Y down. Orientation is CCW as seen from the top.
- **cpl**: mm, Y up, viewed from the top. `cpl = (x, -y)`, which matches KiCad's `.pos` export. Gerbers are plotted without the aux origin, so the CPL and the Gerbers share the board's absolute origin.
- **local**: footprint-local coordinates, Y down. EasyEDA's canvas is Y down. `parser.py` only subtracts the origin, and `footprint_writer.py` writes those values straight into KiCad, so the EasyEDA frame and the KiCad footprint frame are the same.
- **Top-side placement model**: `p_cpl = t + R(phi) · q_up`.
- **Bottom-side placement model**: `p_cpl = t + R(phi) · diag(1, -1) · q_up`. This matches how KiCad flips footprints (it mirrors local Y and negates the orientation). As a result, a footprint whose library frame already matches JLC's gets KiCad's own orientation on both sides.

**Not verified:** whether JLCPCB reads bottom-side rotations with this same mirror axis. If an order preview shows bottom-side parts rotated by 180°, or with the angle sign flipped, change `BOTTOM_MIRROR` / `bottom_cpl_rotation` in `conventions.py`. Nothing else needs to change.

## CLI

`jlcimport-cli production` runs the same check from a `.kicad_pcb` file, without KiCad, and writes the BOM, CPL and report. It doesn't write Gerbers. Use the plugin for those.

```bash
jlcimport-cli production board.kicad_pcb -n 10          # check + write to <project>/production
jlcimport-cli production board.kicad_pcb --check-only   # exit 1 if errors
jlcimport-cli production board.kicad_pcb --force -o out # write even with errors
```
