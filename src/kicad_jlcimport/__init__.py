"""JLCImport - KiCad 8/9/10 LCSC Component Import Plugin."""

try:
    import pcbnew  # noqa: F401
except ImportError:
    pass  # pcbnew not available (running outside KiCad)
else:
    try:
        from .easyeda.api import use_bundled_cacerts

        use_bundled_cacerts()

        from .plugin import JLCImportPlugin

        JLCImportPlugin().register()
    except Exception:
        import traceback

        traceback.print_exc()
