"""On-disk cache for EasyEDA component data (footprints, symbols, LCSC part -> component list).

This data changes rarely, so it is kept for :data:`MAX_AGE` and then fetched
again.  Stock and prices are never cached here.  The cache is best-effort:
an unreadable or unwritable cache just means a network fetch.

Entries live in ``<cache dir>/<namespace>/<key>.json.gz``.  The directory is
the platform's user cache folder, or ``$JLCIMPORT_CACHE_DIR`` when set.
"""

import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from typing import Any, Optional

MAX_AGE = 7 * 24 * 3600  # seconds
CACHE_DIR_ENV = "JLCIMPORT_CACHE_DIR"

_SAFE_KEY = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def cache_dir() -> str:
    """The cache root for this user."""
    override = os.environ.get(CACHE_DIR_ENV)
    if override:
        return override
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "kicad_jlcimport")


def _path(namespace: str, key: str) -> str:
    name = key if _SAFE_KEY.match(key) else hashlib.sha1(key.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir(), namespace, name + ".json.gz")


def get(namespace: str, key: str, max_age: float = MAX_AGE) -> Optional[Any]:
    """The cached value, or None when missing, expired or unreadable."""
    path = _path(namespace, key)
    try:
        if time.time() - os.path.getmtime(path) > max_age:
            return None
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, EOFError):
        return None


def put(namespace: str, key: str, value: Any) -> None:
    """Store *value* (JSON-serialisable).  Written atomically; failures are ignored."""
    path = _path(namespace, key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as raw, gzip.open(raw, "wt", encoding="utf-8") as f:
                json.dump(value, f)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except (OSError, TypeError, ValueError):
        pass


def clear() -> None:
    """Delete every cached entry."""
    shutil.rmtree(cache_dir(), ignore_errors=True)
