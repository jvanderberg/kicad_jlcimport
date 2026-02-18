# Fixes

## `_nanosvg.pyd` — Windows SVG rendering fix

KiCad 9.0.7 on Windows ships a broken `wx.svg._nanosvg` module, which prevents
the plugin from rendering symbol preview images. Replacing the file with this
working copy restores SVG support.

### Instructions

1. Close KiCad completely.
2. Copy `_nanosvg.pyd` from this directory to your KiCad Python `wx/svg` folder.
   The default location is:

   ```
   C:\Program Files\KiCad\9.0\bin\Lib\site-packages\wx\svg\_nanosvg.pyd
   ```

3. You may need to run the copy as Administrator since `Program Files` is
   protected.
4. Restart KiCad. Symbol previews in JLCImport should now work.

### Verification

Open a Python console in KiCad (**Tools > Scripting Console**) and run:

```python
import wx.svg
print("wx.svg loaded OK")
```

If it prints without error, the fix is working.
