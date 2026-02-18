# Fixes

## `_nanosvg.pyd` — Windows SVG rendering fix

KiCad 9.x on Windows ships without a compiled `wx.svg._nanosvg` module, which
prevents the plugin from rendering symbol preview images. Adding this
pre-compiled file restores SVG support.

### Instructions

1. Close KiCad completely.
2. Find your KiCad installation's Python `wx/svg` folder. The default location
   is:

   ```
   C:\Program Files\KiCad\<version>\bin\Lib\site-packages\wx\svg\
   ```

   Replace `<version>` with your KiCad version (e.g. `9.0`).

3. Copy `_nanosvg.pyd` from this directory into that folder. You may need to
   run the copy as Administrator since `Program Files` is protected.
4. Restart KiCad. Symbol previews in JLCImport should now work.

### Verification

Open a Python console in KiCad (**Tools > Scripting Console**) and run:

```python
import wx.svg
print("wx.svg loaded OK")
```

If it prints without error, the fix is working.
