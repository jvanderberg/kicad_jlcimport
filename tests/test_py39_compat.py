"""Verify all production code compiles and imports under Python 3.9 (KiCad's bundled version)."""

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


def test_source_imports_on_py39():
    """Import the package under Python 3.9 to catch runtime annotation errors."""
    result = subprocess.run(
        [
            PY39,
            "-c",
            "import kicad_jlcimport.easyeda.api; import kicad_jlcimport.easyeda.parser; "
            "import kicad_jlcimport.importer; import kicad_jlcimport.kicad.library; "
            "import kicad_jlcimport.kicad.footprint_writer; import kicad_jlcimport.kicad.symbol_writer; "
            "import kicad_jlcimport.kicad.footprint_parser; import kicad_jlcimport.kicad.model3d; "
            "import kicad_jlcimport.cli",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PYTHONPATH": "src"},
    )
    assert result.returncode == 0, f"Python 3.9 import failed:\n{result.stderr}"
