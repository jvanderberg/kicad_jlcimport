"""Shared test fixtures for JLCImport tests."""

import os
import sys

# Add the src directory so we can import the package as 'kicad_jlcimport'
_repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src_dir = os.path.join(_repo_dir, "src")
sys.path.insert(0, _src_dir)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_disk_cache(tmp_path, monkeypatch):
    """Keep every test away from the user's EasyEDA disk cache (and from each other's)."""
    monkeypatch.setenv("JLCIMPORT_CACHE_DIR", str(tmp_path / "jlcimport-cache"))


@pytest.fixture(autouse=True)
def _isolated_kicad_dirs(tmp_path_factory, monkeypatch):
    """Point the home folder at a temp dir so no test reads or writes the real KiCad config.

    KiCad's config and data folders (and jlcimport.json) all sit under the home
    folder, so imports in tests would otherwise read the developer's own path
    variables and library tables.
    """
    # Kept out of tmp_path: some tests assert that their tmp_path holds only what they wrote.
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
