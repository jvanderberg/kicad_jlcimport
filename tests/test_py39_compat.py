"""Verify all production code compiles under Python 3.9 (KiCad's bundled version)."""

import subprocess

PY39 = "/opt/homebrew/opt/python@3.9/bin/python3.9"


def test_source_compiles_on_py39():
    """Compile every .py file under src/kicad_jlcimport/ with Python 3.9."""
    result = subprocess.run(
        [PY39, "-m", "compileall", "-q", "-x", "__pycache__", "src/kicad_jlcimport/"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"Python 3.9 compilation failed:\n{result.stdout}\n{result.stderr}"
