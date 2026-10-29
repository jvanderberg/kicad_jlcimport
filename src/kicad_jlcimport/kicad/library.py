"""Library file management - create/append symbols, save footprints, update lib-tables."""

from __future__ import annotations

import json
import os
import re
import sys

from ._fileio import atomic_write
from ._format import escape_sexpr
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
    atomic_write(path, json.dumps(config, indent=2) + "\n")


def ensure_lib_structure(base_path: str, lib_name: str = "JLCImport") -> dict:
    """Create library directory structure if needed.

    Returns dict with paths: sym_path, fp_dir, models_dir
    Raises LibraryNameError if *lib_name* is not a safe library name.
    """
    lib_name = validate_lib_name(lib_name)
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


class SymbolLibraryError(ValueError):
    """An existing .kicad_sym file could not be parsed and was left unchanged."""


def check_symbol_lib(sym_path: str) -> set[str]:
    """Return the top-level symbol names in *sym_path* (empty if it doesn't exist).

    Raises SymbolLibraryError if the file exists but can't be safely edited.
    """
    if not os.path.exists(sym_path):
        return set()
    _, symbols, _ = _read_symbol_lib(sym_path)
    return {sym_name for sym_name, _, _ in symbols}


def _read_symbol_lib(sym_path: str) -> tuple[str, list[tuple[str, int, int]], int]:
    """Read a library verbatim (line endings preserved) and locate its symbols."""
    try:
        with open(sym_path, encoding="utf-8", newline="") as f:
            lib_content = f.read()
    except UnicodeDecodeError as e:
        raise SymbolLibraryError(
            f"{sym_path} is not UTF-8 text, so it is not a KiCad symbol library. It was left unchanged."
        ) from e
    except OSError as e:
        raise SymbolLibraryError(f"{sym_path} could not be read: {e.strerror or e}") from e
    scan = _scan_symbol_lib(lib_content)
    if scan is None:
        raise SymbolLibraryError(
            f"{sym_path} is not a well-formed KiCad symbol library "
            "(unbalanced parentheses, unterminated string, or trailing data). "
            "It was left unchanged; open it in KiCad's Symbol Editor or fix it by hand."
        )
    symbols, root_close = scan
    return lib_content, symbols, root_close


def add_symbol_to_lib(
    sym_path: str,
    name: str,
    content: str,
    overwrite: bool = False,
    kicad_version: int = DEFAULT_KICAD_VERSION,
) -> bool:
    """Add a symbol to the .kicad_sym library file.

    Creates the library file if it doesn't exist.  An existing symbol with the
    same name is replaced in place, keeping its position in the file.  The
    file's line endings are preserved.
    Returns True if symbol was added/replaced, False if it already exists and
    overwrite=False.  Raises SymbolLibraryError if the existing file is not a
    well-formed library.
    """
    if not os.path.exists(sym_path):
        # Create new library with this symbol
        header = "(kicad_symbol_lib\n"
        header += f"  (version {symbol_format_version(kicad_version)})\n"
        header += '  (generator "JLCImport")\n'
        if has_generator_version(kicad_version):
            header += '  (generator_version "1.0")\n'
        atomic_write(sym_path, _checked_library(sym_path, header + content + ")\n", name))
        return True

    lib_content, symbols, root_close = _read_symbol_lib(sym_path)

    matches = [(start, end) for sym_name, start, end in symbols if sym_name == name]
    if matches and not overwrite:
        return False

    newline = "\r\n" if "\r\n" in lib_content else "\n"
    if newline != "\n":
        content = content.replace("\r\n", "\n").replace("\n", newline)

    if matches:
        # Replace the first occurrence in place and drop any duplicates
        # (earlier versions could append a second copy instead of replacing).
        pieces = []
        pos = 0
        for i, (start, end) in enumerate(matches):
            start, end = _expand_to_lines(lib_content, start, end)
            pieces.append(lib_content[pos:start])
            if i == 0:
                pieces.append(content)
            pos = end
        pieces.append(lib_content[pos:])
        new_content = "".join(pieces)
    else:
        # Insert before the closing paren of (kicad_symbol_lib ...)
        new_content = lib_content[:root_close] + content + ")" + newline

    atomic_write(sym_path, _checked_library(sym_path, new_content, name))
    return True


def _checked_library(sym_path: str, text: str, name: str) -> str:
    """Return *text* if it is a well-formed library containing *name* exactly once.

    A last line of defence: if the generated symbol is malformed, refuse to
    write rather than leave a library that every later import rejects.
    """
    scan = _scan_symbol_lib(text)
    if scan is None or [sym_name for sym_name, _, _ in scan[0]].count(name) != 1:
        raise SymbolLibraryError(
            f"The generated symbol {name!r} is malformed, so {sym_path} was left unchanged. "
            "Please report this part to the JLCImport issue tracker."
        )
    return text


_SYMBOL_HEAD_RE = re.compile(r'\(\s*symbol\s+"((?:[^"\\]|\\.)*)"', re.S)
# A complete quoted string, a paren, or a lone quote (an unterminated string).
_SEXPR_TOKEN_RE = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"|[()"]', re.S)


def _scan_symbol_lib(text: str) -> tuple[list[tuple[str, int, int]], int] | None:
    """Locate the top-level symbols of a .kicad_sym file.

    Returns ``(symbols, root_close)`` where *symbols* is a list of
    ``(name, start, end)`` spans covering each ``(symbol "name" ...)`` node
    directly inside ``(kicad_symbol_lib ...)`` and *root_close* is the index
    of the root node's closing paren.  Parentheses inside quoted strings are
    ignored.  Unit sub-symbols (``(symbol "X_1_1" ...)`` nested inside a
    symbol) are not reported.  Returns None if the text is not balanced or
    has content after the root node, so callers never rewrite a file they
    don't fully understand.
    """
    symbols = []
    depth = 0
    sym_start = -1
    sym_name = ""
    for m in _SEXPR_TOKEN_RE.finditer(text):
        tok = m.group()
        if tok == "(":
            depth += 1
            if depth == 2:
                head = _SYMBOL_HEAD_RE.match(text, m.start())
                if head:
                    sym_start = m.start()
                    sym_name = re.sub(r"\\(.)", r"\1", head.group(1), flags=re.S)
        elif tok == ")":
            depth -= 1
            if depth == 1 and sym_start >= 0:
                symbols.append((sym_name, sym_start, m.end()))
                sym_start = -1
            elif depth == 0:
                if text[m.end() :].strip():
                    return None
                return symbols, m.start()
            elif depth < 0:
                return None
        elif tok == '"':
            return None
    return None


def symbol_exists(sym_path: str, name: str) -> bool:
    """Return True if *sym_path* contains a top-level symbol named *name*.

    Raises SymbolLibraryError if the file exists but can't be parsed.
    """
    if not os.path.exists(sym_path):
        return False
    _, symbols, _ = _read_symbol_lib(sym_path)
    return any(sym_name == name for sym_name, _, _ in symbols)


def _expand_to_lines(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen a span to whole lines when it is the only thing on them."""
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start].strip():
        line_start = start
    line_end = end
    while line_end < len(text) and text[line_end] in " \t":
        line_end += 1
    if text.startswith("\r\n", line_end):
        line_end += 2
    elif text.startswith("\n", line_end):
        line_end += 1
    else:
        line_end = end
    return line_start, line_end


def save_footprint(fp_dir: str, name: str, content: str, overwrite: bool = False) -> bool:
    """Save a .kicad_mod footprint file.

    Returns True if saved, False if exists and overwrite=False.
    """
    fp_path = os.path.join(fp_dir, f"{name}.kicad_mod")
    if os.path.exists(fp_path) and not overwrite:
        return False

    atomic_write(fp_path, content)
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

    return "10.0"


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
    return _default_3rdparty_dir(kicad_version)


def _default_3rdparty_dir(kicad_version: int = DEFAULT_KICAD_VERSION) -> str:
    """Return the default 3rd-party library directory (ignoring any custom override)."""
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
    Raises LibraryNameError if *lib_name* is not a safe library name.
    """
    lib_name = validate_lib_name(lib_name)
    entry = f'  (lib (name "{lib_name}")(type "{lib_type}")(uri "{escape_sexpr(uri)}")(options "")(descr ""))'

    if os.path.exists(table_path):
        with open(table_path, encoding="utf-8") as f:
            content = f.read()
        if f'(name "{lib_name}")' in content:
            return False
        last_paren = content.rfind(")")
        if last_paren >= 0:
            atomic_write(table_path, content[:last_paren] + entry + "\n)\n")
        return False
    else:
        atomic_write(table_path, f"({table_type}\n  (version 7)\n{entry}\n)\n")
        return True


_WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$", re.IGNORECASE)
_LIB_ENTRY_RE = re.compile(
    r'\(lib\s+\(name\s+"([^"]+)"\)\s*\(type\s+"([^"]+)"\)\s*\(uri\s+"([^"]+)"\)',
    re.IGNORECASE,
)
_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _read_fp_lib_entries(table_path: str) -> list[tuple[str, str, str]]:
    """Return fp-lib-table entries as (name, type, uri)."""
    if not os.path.exists(table_path):
        return []
    try:
        with open(table_path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return []
    return [(name, lib_type, uri) for name, lib_type, uri in _LIB_ENTRY_RE.findall(content)]


def _iter_kicad_config_versions() -> list[str]:
    """Return all versioned KiCad config directories that exist on disk.

    Returns the newest version first so callers find the most relevant config
    without knowing the installed KiCad version in advance.
    """
    base = _kicad_config_base()
    dirs: list[tuple[float, str]] = []
    try:
        for entry in os.listdir(base):
            full = os.path.join(base, entry)
            if os.path.isdir(full):
                try:
                    dirs.append((float(entry), full))
                except ValueError:
                    pass
    except OSError:
        pass
    dirs.sort(reverse=True)
    return [d for _, d in dirs]


def _find_kicad_data_dir(major: int) -> str:
    """Find the KiCad data directory for a specific major version.

    Scans standard installation locations on macOS, Windows, and Linux for the
    KiCad application matching *major* and returns its data root (the directory
    containing ``footprints/``, ``3dmodels/``, ``symbols/``, etc.).

    Returns ``""`` if not found.
    """
    if sys.platform == "darwin":
        base = "/Applications"
        try:
            entries = os.listdir(base)
        except OSError:
            entries = []
        # Reverse-sort so e.g. "KiCad 9.0.7" is tried before "KiCad 9.0.3".
        for entry in sorted(entries, reverse=True):
            if not entry.startswith("KiCad"):
                continue
            m = re.search(r"(\d+)", entry)
            # Match versioned dirs ("KiCad 10") and plain "KiCad" (no digits).
            if m and int(m.group(1)) != major:
                continue
            for app in ("KiCad.app", "KiCad Nightly.app"):
                data = os.path.join(base, entry, app, "Contents", "SharedSupport")
                if os.path.isdir(data):
                    return data
    elif sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        kicad_base = os.path.join(pf, "KiCad")
        # Scan for versioned subdirs matching major (e.g. "10.0", "10.1")
        try:
            for entry in sorted(os.listdir(kicad_base), reverse=True):
                try:
                    if int(float(entry)) == major:
                        data = os.path.join(kicad_base, entry, "share", "kicad")
                        if os.path.isdir(data):
                            return data
                except ValueError:
                    continue
        except OSError:
            pass
        # Unversioned fallback: "KiCad\share\kicad"
        data = os.path.join(kicad_base, "share", "kicad")
        if os.path.isdir(data):
            return data
    else:
        # Linux: versioned installs, standard paths, and nightly
        for prefix in (
            f"/usr/share/kicad-{major}",
            "/usr/share/kicad",
            "/usr/share/kicad-nightly",
            f"/usr/local/share/kicad-{major}",
            "/usr/local/share/kicad",
        ):
            if os.path.isdir(prefix):
                return prefix
    return ""


def resolve_kicad_var(key: str, kicad_version: int = DEFAULT_KICAD_VERSION) -> str:
    """Resolve a KiCad environment variable (e.g. ``KICAD9_3DMODEL_DIR``).

    Checks, in order:
    1. The process environment (``os.environ``).
    2. ``kicad_common.json`` from the newest installed KiCad version.
    3. The installed KiCad application directory for *kicad_version* (the
       target version we are running against, which may differ from the
       version number embedded in the variable name).

    Returns the resolved directory path, or ``""`` if not found.
    """
    env_val = os.environ.get(key, "")
    if env_val and os.path.isdir(env_val):
        return env_val

    for ver_dir in _iter_kicad_config_versions():
        common = os.path.join(ver_dir, "kicad_common.json")
        try:
            with open(common, encoding="utf-8") as f:
                data = json.load(f)
            val = (data.get("environment") or {}).get("vars") or {}
            val = val.get(key, "")
            if val and os.path.isdir(val):
                return val
        except (OSError, ValueError, KeyError):
            continue

    # Derive the subdir from the variable name (e.g. KICAD9_FOOTPRINT_DIR)
    m = re.match(r"^KICAD\d+_(\w+)_DIR$", key)
    if m:
        resource = m.group(1).lower()
        subdir_map = {"footprint": "footprints", "3dmodel": "3dmodels", "symbol": "symbols", "template": "template"}
        subdir = subdir_map.get(resource, "")
        if subdir:
            # Use the target kicad_version, not the version in the variable
            # name — KiCad 10 may reference KICAD9_FOOTPRINT_DIR but the
            # actual data lives in KiCad 10's install directory.
            data_root = _find_kicad_data_dir(kicad_version)
            if data_root:
                candidate = os.path.join(data_root, subdir)
                if os.path.isdir(candidate):
                    return candidate

    return ""


def _expand_lib_uri(uri: str, project_dir: str = "", kicad_version: int = DEFAULT_KICAD_VERSION) -> str:
    """Expand common variables in a lib-table URI."""

    def _replace(match) -> str:
        key = match.group(1)
        if key == "KIPRJMOD":
            # Return the raw token when project_dir is unknown so the
            # downstream "${" guard discards this entry rather than
            # producing a bogus root-relative path like "/JLCImport.pretty".
            return project_dir if project_dir else match.group(0)
        resolved = resolve_kicad_var(key, kicad_version)
        if resolved:
            return resolved
        return match.group(0)

    expanded = _ENV_VAR_RE.sub(_replace, uri)
    expanded = os.path.expanduser(expanded)
    if "${" in expanded:
        return ""
    if expanded and not os.path.isabs(expanded) and project_dir:
        expanded = os.path.normpath(os.path.join(project_dir, expanded))
    return expanded


def _iter_footprint_libraries(
    project_dir: str,
    kicad_version: int = DEFAULT_KICAD_VERSION,
    jlc_lib_name: str = "JLCImport",
    jlc_global_lib_dir: str = "",
) -> list[tuple[str, str]]:
    """Return existing .pretty directories from project/global fp-lib-table files.

    Also injects any JLCImport .pretty directories that exist on disk but are
    not yet registered in a lib-table (e.g. immediately after a first import,
    before KiCad has been restarted to pick up the new lib-table entry).

    jlc_lib_name:       The configured JLCImport library name (default "JLCImport").
    jlc_global_lib_dir: The global JLCImport output directory, if known.
    """
    candidates: list[tuple[str, str]] = []
    # FIFO worklist so project entries are processed before global, preserving
    # project precedence when a lib_name appears in both tables.
    tables: list[tuple[str, str]] = []

    if project_dir:
        tables.append((os.path.join(project_dir, "fp-lib-table"), project_dir))
    tables.append((os.path.join(get_global_config_dir(kicad_version), "fp-lib-table"), ""))

    seen: set[tuple[str, str]] = set()
    # Track visited table files to prevent cycles in (type "Table") chains.
    seen_tables: set[str] = set()
    while tables:
        table_path, table_project_dir = tables.pop(0)
        norm_table = os.path.normpath(table_path)
        if norm_table in seen_tables:
            continue
        seen_tables.add(norm_table)
        for lib_name, lib_type, uri in _read_fp_lib_entries(table_path):
            path = _expand_lib_uri(uri, table_project_dir, kicad_version)
            if not path:
                continue
            lt = lib_type.lower()
            if lt == "kicad":
                if not path.lower().endswith(".pretty") or not os.path.isdir(path):
                    continue
                key = (lib_name, os.path.normpath(path))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(key)
            elif lt == "table":
                # KiCad 10 indirection: the URI points at another fp-lib-table.
                # Resolve relative URIs inside the nested table relative to its
                # own directory, matching KiCad's own behaviour.
                if not os.path.isfile(path):
                    continue
                tables.append((path, os.path.dirname(path)))

    # Inject JLCImport .pretty directories that exist on disk but are missing
    # from the lib-tables.  This covers the window between the first import
    # (which writes the lib-table) and the next KiCad restart (which reads it).
    # Two candidate locations are checked: the project directory and the
    # configured global output directory.
    jlc_pretty = f"{jlc_lib_name}.pretty"
    jlc_search_dirs = []
    if project_dir:
        jlc_search_dirs.append((jlc_lib_name, os.path.join(project_dir, jlc_pretty)))
    if jlc_global_lib_dir:
        jlc_search_dirs.append((jlc_lib_name, os.path.join(jlc_global_lib_dir, jlc_pretty)))

    for lib_name, path in jlc_search_dirs:
        if not os.path.isdir(path):
            continue
        key = (lib_name, os.path.normpath(path))
        if key in seen:
            continue  # already in lib-table, nothing to do
        seen.add(key)
        candidates.append(key)

    return candidates


def _footprint_match_score(package: str, footprint_name: str) -> int:
    """Heuristic score for package-to-footprint-name similarity."""

    def _extract_features(text: str) -> dict:
        t = text.upper()
        features: dict = {}

        families = (
            "QFN",
            "DFN",
            "LGA",
            "SON",
            "SOIC",
            "SSOP",
            "TSSOP",
            "SOP",
            "QFP",
            "LQFP",
            "TQFP",
            "BGA",
            "DIP",
            "SOT",
            "SOD",
        )
        for family in families:
            if re.search(rf"(^|[^A-Z0-9]){family}([^-_A-Z0-9]|[-_])", t):
                features["family"] = family
                count_m = re.search(rf"{family}[-_]?(\d+)\b", t)
                if count_m:
                    features["count"] = int(count_m.group(1))
                break

        pitch_m = re.search(r"P(?:ITCH)?\s*([0-9]+(?:\.[0-9]+)?)", t)
        if pitch_m:
            try:
                features["pitch"] = float(pitch_m.group(1))
            except ValueError:
                pass

        body_pairs: list[tuple[float, float]] = []
        for m in re.finditer(r"(?<![0-9A-Z])(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)(?:MM)?(?![0-9A-Z])", t):
            try:
                a, b = float(m.group(1)), float(m.group(2))
                body_pairs.append((a, b))
            except ValueError:
                continue
        if body_pairs:
            a, b = max(body_pairs, key=lambda p: p[0] * p[1])
            features["body"] = tuple(sorted((a, b)))
        else:
            body_lw_m = re.search(r"L(\d+(?:\.\d+)?)\b.*?W(\d+(?:\.\d+)?)\b", t)
            if body_lw_m:
                try:
                    a, b = float(body_lw_m.group(1)), float(body_lw_m.group(2))
                    features["body"] = tuple(sorted((a, b)))
                except ValueError:
                    pass

        ep_m = re.search(r"EP(\d+(?:\.\d+)?)(?:X(\d+(?:\.\d+)?))?", t)
        if ep_m:
            try:
                a = float(ep_m.group(1))
                b = float(ep_m.group(2)) if ep_m.group(2) else a
                features["ep"] = tuple(sorted((a, b)))
            except ValueError:
                pass

        return features

    def _pair_close(a: tuple[float, float], b: tuple[float, float], tol: float) -> bool:
        return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol

    pkg = package.upper()
    fp = footprint_name.upper()
    pkg_features = _extract_features(pkg)
    fp_features = _extract_features(fp)

    # Hard guards to avoid obviously wrong package matches (e.g. QFN-80 -> QFN-40).
    if pkg_features.get("family") and pkg_features.get("family") == fp_features.get("family"):
        if "count" in pkg_features and "count" in fp_features and pkg_features["count"] != fp_features["count"]:
            return 0
        if (
            "body" in pkg_features
            and "body" in fp_features
            and not _pair_close(pkg_features["body"], fp_features["body"], 0.25)
        ):
            return 0
        if (
            "pitch" in pkg_features
            and "pitch" in fp_features
            and abs(pkg_features["pitch"] - fp_features["pitch"]) > 0.05
        ):
            return 0

    pkg_norm = re.sub(r"[^A-Z0-9]+", "", pkg)
    fp_norm = re.sub(r"[^A-Z0-9]+", "", fp)
    if not pkg_norm or not fp_norm:
        return 0
    if pkg_norm == fp_norm:
        return 100
    if fp_norm.startswith(pkg_norm):
        return 95
    if pkg_norm in fp_norm:
        return 90

    pkg_tokens = [t for t in re.split(r"[^A-Z0-9]+", pkg) if t]
    fp_tokens = [t for t in re.split(r"[^A-Z0-9]+", fp) if t]
    if not pkg_tokens or not fp_tokens:
        return 0

    shared = set(pkg_tokens) & set(fp_tokens)
    if not shared:
        return 0

    score = 30 + 10 * len(shared)
    if pkg_tokens[0] in shared:
        score += 15
    pkg_digits = {t for t in pkg_tokens if t.isdigit()}
    if pkg_digits and pkg_digits.issubset(set(fp_tokens)):
        score += 10

    # Feature bonuses for high-confidence footprint matches.
    if pkg_features.get("family") and pkg_features.get("family") == fp_features.get("family"):
        score += 8
    if "count" in pkg_features and pkg_features.get("count") == fp_features.get("count"):
        score += 22
    if (
        "body" in pkg_features
        and "body" in fp_features
        and _pair_close(pkg_features["body"], fp_features["body"], 0.25)
    ):
        score += 20
    if "pitch" in pkg_features and "pitch" in fp_features and abs(pkg_features["pitch"] - fp_features["pitch"]) <= 0.02:
        score += 15
    if "ep" in pkg_features and "ep" in fp_features and _pair_close(pkg_features["ep"], fp_features["ep"], 0.25):
        score += 12

    return min(score, 100)


def find_best_matching_footprint(
    package: str,
    project_dir: str = "",
    kicad_version: int = DEFAULT_KICAD_VERSION,
    min_score: int = 60,
) -> str | None:
    """Find the best existing footprint reference for a package name.

    Returns:
        Footprint reference like ``Library:Footprint`` or ``None`` when no
        sufficiently close match is found.
    """
    if not package:
        return None

    best: tuple[int, str, str] | None = None
    for lib_name, lib_path in _iter_footprint_libraries(project_dir, kicad_version):
        try:
            entries = os.listdir(lib_path)
        except OSError:
            continue
        for entry in entries:
            if not entry.lower().endswith(".kicad_mod"):
                continue
            fp_name = entry[: -len(".kicad_mod")]
            score = _footprint_match_score(package, fp_name)
            if score < min_score:
                continue
            if best is None:
                best = (score, lib_name, fp_name)
                continue
            if score > best[0]:
                best = (score, lib_name, fp_name)
                continue
            if score == best[0]:
                if len(fp_name) < len(best[2]) or (len(fp_name) == len(best[2]) and fp_name < best[2]):
                    best = (score, lib_name, fp_name)

    if best is None:
        return None
    return f"{best[1]}:{best[2]}"


class LibraryNameError(ValueError):
    """The configured library name can't be used safely."""


_LIB_NAME_BAD_CHARS = set('/\\:"<>|?*$')


def validate_lib_name(lib_name: str) -> str:
    """Return *lib_name* stripped of surrounding whitespace, or raise LibraryNameError.

    The name becomes a KiCad library nickname, part of ``Lib:Footprint``
    references, and the stem of the .kicad_sym/.pretty/.3dshapes paths, so it
    must be a single safe filename on every OS.  Names are never silently
    rewritten: a renamed library would point imports somewhere unexpected.
    """
    name = (lib_name or "").strip()
    if not name:
        raise LibraryNameError("Library name is empty.")
    for ch in name:
        if ch in _LIB_NAME_BAD_CHARS or ord(ch) < 32 or ord(ch) == 127:
            shown = repr(ch) if ch.isprintable() else f"control character {ch!r}"
            raise LibraryNameError(
                f"Library name {name!r} contains {shown}, which can't be used in a KiCad library name or file name."
            )
    if name in (".", "..") or name.endswith("."):
        raise LibraryNameError(f"Library name {name!r} can't be '.' or '..', or end with '.'.")
    if _WINDOWS_RESERVED.match(name):
        raise LibraryNameError(f"Library name {name!r} is a reserved device name on Windows.")
    return name


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
