"""Library file management - create/append symbols, save footprints, update lib-tables."""

import json
import os
import re
import sys

from .version import DEFAULT_KICAD_VERSION, has_generator_version, symbol_format_version, version_dir_name

_DEFAULT_CONFIG = {"lib_name": "JLCImport", "global_lib_dir": "", "use_global": False}


def _config_path() -> str:
    """Get path to the jlcimport config file."""
    return os.path.join(_kicad_config_base(), "jlcimport.json")


def load_config() -> dict:
    """Load config from jlcimport.json, returning defaults for missing keys.

    Auto-creates the file if missing and backfills any new default keys
    into existing files.
    """
    config = dict(_DEFAULT_CONFIG)
    path = _config_path()
    needs_write = False
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                # Check if any default keys are missing from stored config
                for key in _DEFAULT_CONFIG:
                    if key not in stored:
                        needs_write = True
                config.update(stored)
        except (json.JSONDecodeError, OSError):
            needs_write = True
    else:
        needs_write = True
    if needs_write:
        save_config(config)
    return config


def save_config(config: dict) -> None:
    """Save config to jlcimport.json."""
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def ensure_lib_structure(base_path: str, lib_name: str = "JLCImport") -> dict:
    """Create library directory structure if needed.

    Returns dict with paths: sym_path, fp_dir, models_dir
    """
    sym_path = os.path.join(base_path, f"{lib_name}.kicad_sym")
    fp_dir = os.path.join(base_path, f"{lib_name}.pretty")
    models_dir = os.path.join(base_path, f"{lib_name}.3dshapes")

    os.makedirs(fp_dir, exist_ok=True)
    os.makedirs(models_dir, exist_ok=True)

    return {
        "sym_path": sym_path,
        "fp_dir": fp_dir,
        "models_dir": models_dir,
    }


def add_symbol_to_lib(
    sym_path: str,
    name: str,
    content: str,
    overwrite: bool = False,
    kicad_version: int = DEFAULT_KICAD_VERSION,
) -> bool:
    """Add a symbol to the .kicad_sym library file.

    Creates the library file if it doesn't exist.
    Returns True if symbol was added/replaced, False if it already exists and overwrite=False.
    """
    if not os.path.exists(sym_path):
        # Create new library with this symbol
        header = "(kicad_symbol_lib\n"
        header += f"  (version {symbol_format_version(kicad_version)})\n"
        header += '  (generator "JLCImport")\n'
        if has_generator_version(kicad_version):
            header += '  (generator_version "1.0")\n'
        with open(sym_path, "w", encoding="utf-8") as f:
            f.write(header)
            f.write(content)
            f.write(")\n")
        return True

    # Read existing library
    with open(sym_path, encoding="utf-8") as f:
        lib_content = f.read()

    # Check if symbol already exists
    search_str = f'(symbol "{name}"'
    if search_str in lib_content:
        if not overwrite:
            return False
        # Remove existing symbol block
        lib_content = _remove_symbol(lib_content, name)

    # Insert before final closing paren
    last_paren = lib_content.rfind(")")
    if last_paren == -1:
        return False

    new_content = lib_content[:last_paren] + content + ")\n"
    with open(sym_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return True


def _remove_symbol(lib_content: str, name: str) -> str:
    """Remove a symbol block from library content."""
    search = f'  (symbol "{name}"'
    start = lib_content.find(search)
    if start == -1:
        return lib_content

    # Find matching closing paren by counting depth
    depth = 0
    i = start
    in_symbol = False
    while i < len(lib_content):
        c = lib_content[i]
        if c == "(":
            depth += 1
            in_symbol = True
        elif c == ")":
            depth -= 1
            if in_symbol and depth == 0:
                # Found the end - include trailing newline
                end = i + 1
                while end < len(lib_content) and lib_content[end] in ("\n", "\r"):
                    end += 1
                return lib_content[:start] + lib_content[end:]
        i += 1

    return lib_content


def save_footprint(fp_dir: str, name: str, content: str, overwrite: bool = False) -> bool:
    """Save a .kicad_mod footprint file.

    Returns True if saved, False if exists and overwrite=False.
    """
    fp_path = os.path.join(fp_dir, f"{name}.kicad_mod")
    if os.path.exists(fp_path) and not overwrite:
        return False

    with open(fp_path, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def update_project_lib_tables(project_dir: str, lib_name: str = "JLCImport") -> bool:
    """Add library entries to sym-lib-table and fp-lib-table if missing.

    Returns True if a table was newly created (requires project reopen).
    """
    sym_uri = f"${{KIPRJMOD}}/{lib_name}.kicad_sym"
    fp_uri = f"${{KIPRJMOD}}/{lib_name}.pretty"
    new_sym = _update_lib_table(os.path.join(project_dir, "sym-lib-table"), "sym_lib_table", lib_name, "KiCad", sym_uri)
    new_fp = _update_lib_table(os.path.join(project_dir, "fp-lib-table"), "fp_lib_table", lib_name, "KiCad", fp_uri)
    return new_sym or new_fp


def _detect_kicad_version() -> str:
    """Detect KiCad major.minor version string (e.g. '9.0')."""
    # Try pcbnew first (works inside KiCad)
    try:
        import pcbnew

        full = pcbnew.Version()
        # Version() returns e.g. "9.0.1" or "(9.0.1)"
        ver = full.strip("()")
        parts = ver.split(".")
        if len(parts) >= 2:
            return f"{parts[0]}.{parts[1]}"
    except Exception:
        pass

    # Fall back: find newest version directory
    base = _kicad_data_base()
    try:
        if os.path.isdir(base):
            versions = []
            for d in os.listdir(base):
                try:
                    versions.append((float(d), d))
                except ValueError:
                    continue
            if versions:
                versions.sort(reverse=True)
                return versions[0][1]
    except OSError:
        pass

    return "9.0"


def _kicad_data_base() -> str:
    """Get the base KiCad data directory (without version)."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Documents/KiCad")
    elif sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "kicad")
    else:
        return os.path.expanduser("~/.local/share/kicad")


def _kicad_config_base() -> str:
    """Get the base KiCad config directory (without version)."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Preferences/kicad")
    elif sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "kicad")
    else:
        return os.path.expanduser("~/.config/kicad")


def get_global_lib_dir(kicad_version: int = DEFAULT_KICAD_VERSION) -> str:
    """Get the global KiCad 3rd-party library directory for a specific version.

    If a custom global_lib_dir is set in config, returns that path (ignoring version).
    Raises ValueError if the custom directory does not exist.
    """
    config = load_config()
    custom = config.get("global_lib_dir", "")
    if custom:
        if not os.path.isdir(custom):
            raise ValueError(f"Custom global library directory does not exist: {custom}")
        return custom
    ver = version_dir_name(kicad_version)
    return os.path.join(_kicad_data_base(), ver, "3rdparty")


def get_global_config_dir(kicad_version: int = DEFAULT_KICAD_VERSION) -> str:
    """Get the global KiCad config directory (where global lib-tables live)."""
    ver = version_dir_name(kicad_version)
    return os.path.join(_kicad_config_base(), ver)


def update_global_lib_tables(
    lib_dir: str, lib_name: str = "JLCImport", kicad_version: int = DEFAULT_KICAD_VERSION
) -> None:
    """Add library entries to the global sym-lib-table and fp-lib-table."""
    config_dir = get_global_config_dir(kicad_version)
    if not os.path.isdir(config_dir):
        os.makedirs(config_dir, exist_ok=True)

    sym_uri = os.path.join(lib_dir, f"{lib_name}.kicad_sym").replace("\\", "/")
    fp_uri = os.path.join(lib_dir, f"{lib_name}.pretty").replace("\\", "/")

    _update_lib_table(os.path.join(config_dir, "sym-lib-table"), "sym_lib_table", lib_name, "KiCad", sym_uri)
    _update_lib_table(os.path.join(config_dir, "fp-lib-table"), "fp_lib_table", lib_name, "KiCad", fp_uri)


def _update_lib_table(table_path: str, table_type: str, lib_name: str, lib_type: str, uri: str) -> bool:
    """Add an entry to a lib-table file (global or project).

    Returns True if the file was newly created.
    """
    entry = f'  (lib (name "{lib_name}")(type "{lib_type}")(uri "{uri}")(options "")(descr ""))'

    if os.path.exists(table_path):
        with open(table_path, encoding="utf-8") as f:
            content = f.read()
        if f'(name "{lib_name}")' in content:
            return False
        last_paren = content.rfind(")")
        if last_paren >= 0:
            new_content = content[:last_paren] + entry + "\n)\n"
            with open(table_path, "w", encoding="utf-8") as f:
                f.write(new_content)
        return False
    else:
        with open(table_path, "w", encoding="utf-8") as f:
            f.write(f"({table_type}\n")
            f.write("  (version 7)\n")
            f.write(entry + "\n")
            f.write(")\n")
        return True


_WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$", re.IGNORECASE)


def sanitize_name(title: str) -> str:
    """Sanitize component name for KiCad file/symbol naming.

    Strips all path separators and special characters to produce a safe
    base filename. Rejects Windows reserved device names.
    """
    # Replace any character that isn't alphanumeric, hyphen, or underscore
    name = re.sub(r"[^A-Za-z0-9_\-]", "_", title)
    # Collapse multiple underscores
    name = re.sub(r"_+", "_", name)
    name = name.strip("_")
    # Reject Windows reserved device names
    if _WINDOWS_RESERVED.match(name):
        name = "_" + name
    if not name:
        name = "unnamed"
    return name
