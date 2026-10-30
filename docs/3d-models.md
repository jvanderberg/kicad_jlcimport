# 3D Model Handling

## Model Format

EasyEDA provides 3D models in OBJ format. We convert these to:
- **WRL (VRML 2.0)**, which KiCad footprints reference
- **STEP**, saved for compatibility but not referenced

### Why WRL instead of STEP?

The placement is computed from the OBJ geometry, and the WRL keeps exactly the same geometry and coordinate system. EasyEDA's STEP files can have a different origin or orientation, so an offset computed from the OBJ would not apply to them.

## Placement

`place_model()` in `src/kicad_jlcimport/kicad/model3d.py` turns EasyEDA's `SVGNODE` data into KiCad's `(model (offset) (rotate))`. EasyEDA gives:

| Field | Meaning |
|---|---|
| `c_rotation` | Model rotation in degrees (x, y, z) |
| `outline3D` child polylines | EasyEDA's own top-view outline of the placed model |
| `c_width`, `c_height` | Size of that outline |
| `z` | Height of the model's lowest point |
| `c_origin` | A reference point; **unreliable**, sometimes hundreds of mm off |

Placement has three steps.

### 1. Rotation

EasyEDA's 3D view (three.js) composes the angles as `Rx · Ry · Rz`, so Z is applied to the model first. KiCad applies its file rotation as `Rz(-rz) · Ry(-ry) · Rx(-rx)`, so X goes first. The EasyEDA matrix is decomposed back into KiCad's order. For models rotated about Z only, which is nearly all of them, this is just the negated `c_rotation`. C2318 (a vertical XH connector, `c_rotation` 270,0,180) is the case that shows the order matters: without the conversion it renders upside down.

### 2. Anchor

EasyEDA draws the outline with only the Z rotation applied. The model's Z-rotated XY bounding box is centred on the outline's bounding box.

The outline is used only if its size matches the model's Z-rotated size to within 5%. Otherwise (a few parts have a degenerate outline) `c_origin` is used. If that would put the model clear of its own pads, it is ignored too and the model is centred on the footprint origin.

Vertically, EasyEDA lifts the unrotated model so its lowest point is at `z`, then rotates it. For Z-only rotations that leaves the lowest point at `z`.

### 3. Pin fit

The model's pins are paired with the pads they land on:
- **Through-hole pads** pair with where the model's geometry passes through the board surface.
- **Top-side SMD pads** pair with model faces resting on the board, clipped to the pad.

If shifting the model by the mean pin-to-pad error then centres every pin on its pad to within 0.05 mm RMS, the shift is applied. Through-hole pins have one physically correct position, so this corrects EasyEDA's own placement error (typically 0.05 to 0.2 mm). SMD land patterns usually extend past their terminals; they leave a larger residual and keep the anchor placement.

The import log reports the outcome:
- `pins aligned to pads`
- `placed from EasyEDA data; pins land on their pads`
- a warning when the model's pins don't reach the pads at all. EasyEDA's own model and footprint disagree in that case: for example C8760, whose model has its leads 9.8 mm apart on a diagonal while the pads are 7.5 mm apart in a line.

## Coordinates and units

- EasyEDA units: 1 unit = 10 mil = 0.254 mm.
- OBJ vertices are in mm.
- KiCad's VRML unit is 0.1 inch, so the WRL divides by 2.54.
- KiCad's model offset Y axis points up, opposite to the board's Y.

## Testing

`tests/test_model3d.py` tests each step with synthetic geometry. It also checks real parts from `testdata/`: through-hole pins must land in their holes, every model must cover its pads, C2318 must stand upright, and C8760 must be reported. `tools/compare_part.py` renders parts with KiCad for a visual check (see `VISUAL_TESTING.md`).
