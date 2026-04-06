"""Verify all production code compiles under Python 3.9 (KiCad's bundled version)."""

import subprocess

import pytest

PY39 = "/opt/homebrew/opt/python@3.9/bin/python3.9"


@pytest.fixture(scope="module")
def py39():
    try:
        r = subprocess.run([PY39, "--version"], capture_output=True, timeout=5)
        if r.returncode == 0:
            return PY39
    except FileNotFoundError:
        pass
    pytest.skip("Python 3.9 not available (brew install python@3.9)")


def test_source_compiles_on_py39(py39):
    """Compile every .py file under src/kicad_jlcimport/ with Python 3.9."""
    result = subprocess.run(
        [py39, "-m", "py_compile", "--help"],
        capture_output=True,
    )
    # py_compile doesn't support batch mode, use compileall
    result = subprocess.run(
        [py39, "-m", "compileall", "-q", "-x", "__pycache__", "src/kicad_jlcimport/"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"Python 3.9 compilation failed:\n{result.stdout}\n{result.stderr}"
