# JLCPCB Production Export

The **Production Files** tab of the JLCImport dialog (PCB Editor > Tools > External Plugins > JLCImport) produces everything a JLCPCB PCBA order needs:

- `<board>-gerbers.zip`: Gerbers for every copper layer, the masks, paste, silkscreen and Edge.Cuts, plus Excellon drill files (mm, separate PTH/NPTH)
- `<board>-bom.csv`: `Comment,Designator,Footprint,JLCPCB Part #`, grouped by value + footprint + LCSC number
- `<board>-cpl.csv`: `Designator,Mid X,Mid Y,Rotation,Layer`
- `<board>-report.txt`: the check results

Files go to `<project>/production/` unless you choose another folder. A relative folder, or one starting with `${KIPRJMOD}`, is taken relative to the board's folder, the way KiCad resolves plot output folders. The individual Gerber and drill files are also left in a `gerbers/` folder inside it.

## Workflow

1. Set **Boards to assemble**.
2. Click **Check**. Every unique LCSC part is looked up in the JLCPCB assembly library, and JLC's own footprint for that part is fetched. Untick **Check stock and prices** to skip the parts-library lookup when you only need placements; the check is then faster, and stock is reported as not checked. The table then shows, for each footprint:
   - stock status: invalid LCSC number, not found, out of stock, or stock lower than quantity per board × boards (all errors). A footprint with no LCSC number at all is a warning, since JLCPCB simply won't place it. Stock that covers the order by less than 10% (or 10 parts, whichever is more) is a warning, because JLCPCB orders extra parts to cover machine losses.
   - Basic, Preferred (Preferred Extended) or Extended. See [Part types and fees](#part-types-and-fees).
   - the solved CPL rotation, and how much to trust it (confidence plus notes)

   Rows with errors are shown in red and rows with warnings in yellow. If a part's stock lookup fails (network error, or an error answer from JLCPCB) it counts as an error, because nothing about it was checked. If JLC's footprint can't be fetched, the part keeps KiCad's own position and rotation, with a warning. Select a row to see all of its notes. The list under the table shows every error and warning, followed by the extended parts and an estimated parts cost.
3. Click **Generate**. The check runs again (cached lookups make this fast). If it finds problems, they are listed and you are asked before any files are written; you can always go ahead anyway. If the board changes while the check runs, Generate stops and asks you to click it again, so the placement file always matches the Gerbers. The Gerbers are plotted first, so if plotting fails no new BOM or CPL is written either.

The tab always works on the board that is open now, so File > Revert, Open or Save As while the dialog is open is fine; the default output folder and file names follow the board.

**Refill zones before plotting** (on by default) refills every zone on the open board, the same as pressing B in the editor, so the Gerbers match the copper. KiCad's Python API has no undo for this, so save the board afterwards if you want to keep the new fills.

DNP footprints and footprints excluded from the BOM are left out of both the BOM and the CPL. Footprints excluded from position files stay in the BOM but are left out of the CPL.

JLCPCB accepts at most 300 BOM line items per order.

### Part types and fees

On Economic PCBA, JLCPCB charges a feeder loading fee (about $3) for each unique **Extended** part. **Basic** parts and **Preferred Extended** parts (shown as "Preferred") load free. Double-sided assembly is only offered as Standard PCBA, which charges a smaller loading fee for every unique part, Basic or not; the summary says so when assembled parts sit on both sides. Fees change, so the check names the parts but doesn't price the fees. JLCPCB's [PCB assembly FAQ](https://jlcpcb.com/help/article/pcb-assembly-faqs) and [assembly cost guide](https://jlcpcb.com/blog/reduce-pcb-assembly-cost-prototype) have the current figures.

The LCSC number is read from the first non-empty footprint field named `LCSC`, `LCSC Part`, `LCSC Part #`, `LCSC Part Number`, `LCSC#`, `JLCPCB Part #`, `JLCPCB Part`, `JLCPCB`, `JLC` or `JLC Part` (case-insensitive).

## How CPL rotation and position are solved

JLCPCB places each part using the orientation and origin of **its own EasyEDA footprint** for that LCSC part. Your KiCad footprint may use a different 0° orientation (a stock KiCad library footprint, for example) and a different origin (pin 1 instead of the centre). To find the rotation and position JLCPCB needs, the plugin fits JLC's footprint onto your actual pads:

1. Pads are paired by pad number (whitespace is trimmed and case is ignored). Polarized two-terminal parts are the exception (see [Polarity of LEDs, diodes and polarized capacitors](#polarity-of-leds-diodes-and-polarized-capacitors)). Unnumbered pads are ignored. Pads that share a number (split thermal pads, several GND or shield pads) are reduced to the centroid of their group on each footprint.
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
6. If the fit is still rejected (pads differ too much, fewer than half of the pad numbers match, or the angle isn't within 1° of a 90° step), the solver's angle is **rounded to the nearest 90° step** (or KiCad's angle plus 90° steps) and used as an *estimate*. A loose fit usually still gets the orientation right, and more often than KiCad's own angle does. Estimates are low confidence and always come with a warning.

| Confidence | Meaning | CPL uses |
| --- | --- | --- |
| high | RMS residual ≤ 0.05 mm and all pad numbers match | solved values |
| medium | RMS ≤ 0.25 mm (land patterns differ slightly), some pad numbers unmatched (at least half still match), pads matched by position, or a polarized part | solved values |
| low | the fit was rejected: high residual, angle not near a 90° step, or fewer than half of the pad numbers match | the estimate (step 6), with a warning |
| unverified | mirrored numbering, fewer than 2 common pad numbers, orientation undetermined, no LCSC number, or JLC's footprint couldn't be fetched | KiCad's own pose, with a warning |
| manual | override fields present | KiCad's pose plus your offsets |

### Polarity of LEDs, diodes and polarized capacitors

Two pad centres always form a symmetric pattern, so geometry can't tell a part from its 180° turn. Polarity comes only from which pad is paired with which, and pad numbers are not reliable for that. KiCad's diode and LED symbols put the cathode on pin 1. Many JLC symbols put the anode there (the C2286 0603 red LED does), and others don't (the C64885 B5819W has its cathode on pin 1). Pairing by number would mount such a part backwards.

The same holds for an LED or diode with a third pad in the middle (a thermal pad), on either footprint. So the rules below apply to two-pad parts and to point-symmetric three-pad parts. On any other footprint, such as a SOT-23 diode, the pad geometry already fixes which pad is which, and pads are paired by number. A part whose other pin mentions a polarity too, like the `K/A` common pin of a dual diode, isn't treated as two-terminal.

For these parts the solver pairs pads by **role** (positive = anode/`+`, negative = cathode/`-`) when it can:

- **JLC side:** JLC's own symbol pin names (`A`/`K`, `Anode`/`Cathode`, `+`/`-`). The symbol is only fetched for two-pad parts and point-symmetric three-pad parts.
- **Board side:** the pad's pin function, which is the schematic pin name KiCad stores on each pad (`K`, `A_2`, …), or else pad numbers that are themselves polarity names (`A`/`K`, `+`/`-`). If neither works and the pads are exactly 1 and 2, the footprint name decides when it says so (KiCad's `_AK` suffix means pad 1 = anode, `_KA` pad 1 = cathode). Otherwise KiCad's library convention is assumed: pad 1 is the cathode when JLC names the pins anode/cathode, and `+` when JLC names them `+`/`-`. A footprint with more pads and no names is left unresolved rather than guessed.

| JLC symbol | Board pads | Result |
| --- | --- | --- |
| no polarity names (e.g. pins `1`/`2`), board footprint polarized | pin names with polarity, an `_AK`/`_KA` name, or an `LED_`/`D_`/`CP_` footprint | paired by number, medium, **warning** to check polarity |
| no polarity names, board footprint not polarized | any | paired by number, with a note |
| polarity names | pin names or pad numbers with polarity | paired by role, medium, **warning** to check polarity |
| polarity names | numbered 1/2, `_AK`/`_KA` footprint name | paired by role from the name, medium, **warning** to check polarity |
| polarity names | numbered 1/2 only, no names | paired by role using KiCad's convention, medium, **warning** to check polarity |
| polarity names | other numbering or extra pads, no names | paired by number, medium, **warning** to check orientation |
| symbol couldn't be fetched | two pads, or three pads on a polarized board footprint | paired by number, medium, **warning** to check orientation (**Check** tries the symbol again) |

When pairing by role turns out to be the reverse of the pad numbering, the report says so ("JLC numbers this part's pads the other way round").

Every polarized part handled here gets a warning to check it in JLC's placement preview, because JLC's own symbol pin names are sometimes the wrong way round, and JLC's footprint silkscreen doesn't reliably mark the cathode either. Pins named `A`/`C` are not read as anode/cathode, because on JLC parts that naming is not reliable.

## Manual overrides

If the placement preview shows a part turned or shifted, you can fix it for good in KiCad instead of correcting it in the preview on every order. Add one or both of these fields to the footprint (Footprint Properties > Fields, or on the symbol, which carries the field to the board on the next update):

| Field | Value |
| --- | --- |
| `JLCPCB Rotation Offset` | degrees added to KiCad's own rotation, e.g. `90`, `-90`, `180` |
| `JLCPCB Position Offset` | `dx, dy` in mm, in CPL coordinates (X right, Y up, viewed from the top, on both sides), e.g. `0.5, -0.25` |

If either field is present, the CPL row is **KiCad's own position and rotation plus these offsets**, and the solver result is ignored. The result therefore doesn't depend on network access or on JLC changing its footprint. A rotation offset of `0` means "trust KiCad for this part".

The offset is added to the CPL Rotation value that KiCad's orientation gives, which is not necessarily the rotation the solver wrote. The row's notes spell out the arithmetic and the solver's own suggestion, for example:

```
Manual override: rotation 90° (KiCad) -90° = 0°, position offset (+1, +2) mm
Solver suggests rotation 180° at (11.000, -18.000)
```

So if the preview looks right at some rotation R, the offset is R minus the KiCad rotation shown in the note. On the top side a positive offset turns the part counter-clockwise as seen from the top. On the bottom side JLCPCB counts rotation the other way (see [Coordinate conventions](#coordinate-conventions)), so a positive offset turns it clockwise as seen from the top.

The row shows as **manual** in the table and the report. A field whose value can't be read (not a number, or a position offset that isn't `dx, dy`) is ignored, with a warning on that row. If no override field can be read, the row keeps the solver's placement.

## Coordinate conventions

All conventions are defined in `src/kicad_jlcimport/production/conventions.py`:

- **board**: KiCad board coordinates, mm, Y down. Orientation is CCW as seen from the top.
- **cpl**: mm, Y up, viewed from the top. `cpl = (x, -y)`, which matches KiCad's `.pos` export. Gerbers are plotted without the aux origin, so the CPL and the Gerbers share the board's absolute origin.
- **local**: footprint-local coordinates, Y down. EasyEDA's canvas is Y down. `parser.py` only subtracts the origin, and `footprint_writer.py` writes those values straight into KiCad, so the EasyEDA frame and the KiCad footprint frame are the same.
- **Top-side placement model**: `p_cpl = t + R(phi) · q_up`.
- **Bottom-side placement model**: `p_cpl = t + R(phi) · diag(1, -1) · q_up`. This matches how KiCad flips footprints (it mirrors local Y and negates the orientation). As a result, a footprint whose library frame already matches JLC's gets KiCad's own orientation on both sides.

- **CPL Rotation column**: `phi` on the top side, `180° − phi` on the bottom side, because JLCPCB measures bottom-side rotations in the opposite direction. The two agree at 90° and 270° and differ by 180° at 0° and 180°. The conversion is `cpl_rotation()` in `conventions.py`.

## CLI

`jlcimport-cli production` runs the same check from a `.kicad_pcb` file, without KiCad, and writes the BOM, CPL and report. It doesn't write Gerbers. Use the plugin for those.

```bash
jlcimport-cli production board.kicad_pcb -n 10          # check + write to <project>/production
jlcimport-cli production board.kicad_pcb --check-only   # exit 1 if errors
jlcimport-cli production board.kicad_pcb --force -o out # write even with errors
jlcimport-cli production board.kicad_pcb --no-stock     # placements only, skip stock/price lookup
```

| Option | Meaning |
| --- | --- |
| `-n`, `--boards N` | boards to assemble, for the stock check (default 5) |
| `-o`, `--output DIR` | output folder (default `<project>/production`) |
| `--check-only` | print the report and write nothing; exit status 1 if there are errors (invalid LCSC number, part not found, out of stock, stock short, lookup failed, duplicate designators). A missing LCSC number is only a warning. |
| `--force` | write the files even when the check reports errors |
| `--no-stock` | skip the stock and price lookup; placements are still solved |

Pass `--insecure` before the command (`jlcimport-cli --insecure production ...`) if certificate checks fail behind a proxy.
