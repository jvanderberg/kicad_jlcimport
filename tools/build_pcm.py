#!/usr/bin/env python3
"""Build a development PCM ZIP artifact for local KiCad testing.

Usage:
    python tools/build_pcm.py          # builds dist/JLCImport-dev.zip
    python tools/build_pcm.py --install  # builds and installs into KiCad's 3rdparty dir
"""

import argparse
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT, "src", "kicad_jlcimport")
METADATA = os.path.join(ROOT, "metadata.json")
DIST_DIR = os.path.join(ROOT, "dist")


def _kicad_3rdparty_plugins() -> str:
    """Return the KiCad 3rdparty plugins directory for the current platform."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Documents/KiCad")
    elif sys.platform == "win32":
        base = os.path.join(os.environ.get("APPDATA", ""), "kicad")
    else:
        base = os.path.expanduser("~/.local/share/kicad")

    # Find the newest version directory
    best = "9.0"
    if os.path.isdir(base):
        for d in os.listdir(base):
            try:
                if float(d) > float(best):
                    best = d
            except ValueError:
                continue

    return os.path.join(base, best, "3rdparty", "plugins", "com_github_jvanderberg_kicad-jlcimport")


def build_zip() -> str:
    """Build the PCM ZIP and return its path."""
    os.makedirs(DIST_DIR, exist_ok=True)
    zip_path = os.path.join(DIST_DIR, "JLCImport-dev.zip")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add metadata.json at root
        zf.write(METADATA, "metadata.json")

        # Add all source files under plugins/
        for dirpath, dirnames, filenames in os.walk(SRC_DIR):
            # Skip __pycache__
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for filename in filenames:
                if filename.endswith((".pyc", ".pyo")):
                    continue
                full_path = os.path.join(dirpath, filename)
                arcname = os.path.join("plugins", os.path.relpath(full_path, SRC_DIR))
                zf.write(full_path, arcname)

    return zip_path


def install(zip_path: str) -> str:
    """Extract the PCM ZIP into KiCad's 3rdparty plugins directory."""
    dest = _kicad_3rdparty_plugins()
    plugins_dest = os.path.join(dest, "plugins")

    # Clean existing installation
    if os.path.isdir(plugins_dest):
        shutil.rmtree(plugins_dest)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest)

    return dest


def main():
    parser = argparse.ArgumentParser(description="Build a dev PCM ZIP artifact")
    parser.add_argument("--install", action="store_true", help="Install into KiCad's 3rdparty plugins directory")
    args = parser.parse_args()

    zip_path = build_zip()
    print(f"Built: {zip_path}")

    with zipfile.ZipFile(zip_path, "r") as zf:
        print(f"  {len(zf.namelist())} files, {sum(i.file_size for i in zf.infolist())} bytes")

    if args.install:
        dest = install(zip_path)
        print(f"Installed to: {dest}")
        print("Restart KiCad to load the updated plugin.")


if __name__ == "__main__":
    main()
