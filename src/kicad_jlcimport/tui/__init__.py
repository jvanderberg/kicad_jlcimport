"""TUI (Text User Interface) for JLCImport using Textual."""

from __future__ import annotations

import argparse
import os
import sys


def main():
    from .app import JLCImportTUI

    parser = argparse.ArgumentParser(
        prog="jlcimport-tui",
        description="JLCImport TUI - interactive terminal interface for JLCPCB component import",
    )
    parser.add_argument(
        "-p",
        "--project",
        help="KiCad project directory (where .kicad_pro file is)",
        default="",
    )
    parser.add_argument(
        "--kicad-version",
        type=int,
        choices=[8, 9],
        default=None,
        help="Target KiCad version (default: 9)",
    )
    parser.add_argument(
        "--global-lib-dir",
        metavar="DIR",
        help="Override global library directory for this run",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (use when behind an intercepting proxy)",
    )
    args = parser.parse_args()

    if args.insecure:
        from kicad_jlcimport.easyeda import api

        api.allow_unverified_ssl()

    project_dir = args.project
    if project_dir:
        project_dir = os.path.abspath(project_dir)

    global_lib_dir = ""
    if args.global_lib_dir:
        global_lib_dir = os.path.abspath(args.global_lib_dir)
        if not os.path.isdir(global_lib_dir):
            print(f"Error: --global-lib-dir does not exist: {global_lib_dir}", file=sys.stderr)
            sys.exit(1)

    app = JLCImportTUI(
        project_dir=project_dir,
        kicad_version=args.kicad_version,
        global_lib_dir=global_lib_dir,
    )
    app.run()
