"""KiCad version constants and format helpers.

Encapsulates the differences between KiCad 8 and KiCad 9 file formats so that
writers and library management code can target either version cleanly.
"""

# Supported major versions
KICAD_V8 = 8
KICAD_V9 = 9
KICAD_V10 = 10
DEFAULT_KICAD_VERSION = KICAD_V9
SUPPORTED_VERSIONS = (KICAD_V8, KICAD_V9, KICAD_V10)

# S-expression version stamps per major KiCad version
_SYMBOL_FORMAT_VERSIONS = {
    KICAD_V8: "20231120",
    KICAD_V9: "20241209",
    KICAD_V10: "20241209",
}

_FOOTPRINT_FORMAT_VERSIONS = {
    KICAD_V8: "20240108",
    KICAD_V9: "20241229",
    KICAD_V10: "20241229",
}


def validate_kicad_version(version: int) -> int:
    """Validate and return a KiCad major version number.

    Raises ValueError if the version is not supported.
    """
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(f"Unsupported KiCad version: {version}. Supported: {SUPPORTED_VERSIONS}")
    return version


def symbol_format_version(kicad_version: int = DEFAULT_KICAD_VERSION) -> str:
    """Return the symbol library format version string for a KiCad version."""
    return _SYMBOL_FORMAT_VERSIONS[kicad_version]


def footprint_format_version(kicad_version: int = DEFAULT_KICAD_VERSION) -> str:
    """Return the footprint format version string for a KiCad version."""
    return _FOOTPRINT_FORMAT_VERSIONS[kicad_version]


def has_generator_version(kicad_version: int = DEFAULT_KICAD_VERSION) -> bool:
    """Whether the format includes a (generator_version ...) field."""
    return kicad_version >= KICAD_V9


def has_embedded_fonts(kicad_version: int = DEFAULT_KICAD_VERSION) -> bool:
    """Whether the footprint format includes (embedded_fonts no)."""
    return kicad_version >= KICAD_V9


# Directory version strings used in KiCad's data/config paths
_DIR_VERSION_NAMES = {
    KICAD_V8: "8.0",
    KICAD_V9: "9.0",
    KICAD_V10: "10.0",
}


def version_dir_name(kicad_version: int = DEFAULT_KICAD_VERSION) -> str:
    """Return the directory version string (e.g. '8.0', '9.0') for path construction."""
    return _DIR_VERSION_NAMES[kicad_version]


def detect_kicad_version_from_pcbnew() -> int:
    """Try to detect the KiCad major version from the pcbnew module.

    Returns the major version number (8 or 9), or DEFAULT_KICAD_VERSION
    if detection fails.
    """
    try:
        import pcbnew

        full = pcbnew.Version()
        ver = full.strip("()")
        parts = ver.split(".")
        if parts:
            major = int(parts[0])
            if major in SUPPORTED_VERSIONS:
                return major
    except Exception:
        pass
    return DEFAULT_KICAD_VERSION
