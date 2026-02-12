#!/usr/bin/env python3
"""Standalone wxPython GUI entry point for JLCImport.

This module allows running JLCImport as a standalone application outside of KiCad.
It can be run directly or built into a binary with PyInstaller.

Usage:
    python -m kicad_jlcimport.gui                        # Opens directory picker dialog
    python -m kicad_jlcimport.gui -p /path/to/project    # Uses specified project directory
    python -m kicad_jlcimport.gui --global               # Uses global library only (no project)
"""

import argparse
import os
import sys

# Add parent directory to path so kicad_jlcimport package is importable
_script_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_script_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)


def main():
    parser = argparse.ArgumentParser(
        description="JLCImport - Import JLCPCB/LCSC components into KiCad libraries",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
When run without arguments, a directory picker dialog will be shown.
Select your KiCad project directory (containing .kicad_pro file) or
click Cancel to use only the global library.

Examples:
    %(prog)s                           # Interactive directory picker
    %(prog)s -p ~/Projects/MyBoard     # Use specified project directory
    %(prog)s --global                  # Global library only, no project
""",
    )
    parser.add_argument(
        "-p",
        "--project",
        help="KiCad project directory (where .kicad_pro file is located)",
        default=None,
    )
    parser.add_argument(
        "--global",
        dest="global_only",
        action="store_true",
        help="Use global library only (skip directory picker)",
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

    global_lib_dir = ""
    if args.global_lib_dir:
        global_lib_dir = os.path.abspath(args.global_lib_dir)
        if not os.path.isdir(global_lib_dir):
            print(f"Error: --global-lib-dir does not exist: {global_lib_dir}", file=sys.stderr)
            sys.exit(1)

    # Import wx after argument parsing to show help even without wx installed
    try:
        import wx
    except ImportError:
        print("Error: wxPython is required for the GUI.")
        print("Install it with: pip install 'kicad-jlcimport[gui]'")
        sys.exit(1)

    from kicad_jlcimport.dialog import JLCImportDialog

    app = wx.App()
    app.SetAppName("JLCImport")

    project_dir = None

    if args.project:
        # User specified a project directory
        project_dir = os.path.abspath(args.project)
        if not os.path.isdir(project_dir):
            wx.MessageBox(
                f"Project directory does not exist:\n{project_dir}",
                "Error",
                wx.OK | wx.ICON_ERROR,
            )
            sys.exit(1)
    elif not args.global_only:
        # Show directory picker dialog
        dlg = wx.DirDialog(
            None,
            "Select KiCad Project Directory\n(Cancel to use Global library only)",
            style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
        )
        if dlg.ShowModal() == wx.ID_OK:
            project_dir = dlg.GetPath()
        dlg.Destroy()

    # Create and show main dialog
    main_dlg = JLCImportDialog(
        None, board=None, project_dir=project_dir, kicad_version=args.kicad_version, global_lib_dir=global_lib_dir
    )
    main_dlg.ShowModal()
    main_dlg.Destroy()


if __name__ == "__main__":
    main()
