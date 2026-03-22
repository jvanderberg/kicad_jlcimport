"""JLCImport - KiCad 8/9/10 LCSC Component Import Plugin."""

try:
    from .plugin import JLCImportPlugin

    JLCImportPlugin().register()
except ImportError:
    pass  # pcbnew not available (running outside KiCad)
