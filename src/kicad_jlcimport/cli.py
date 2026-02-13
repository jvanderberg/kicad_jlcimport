#!/usr/bin/env python3
"""CLI tool for testing JLCImport search and import."""

import argparse
import os
import sys

from kicad_jlcimport.easyeda import api
from kicad_jlcimport.easyeda.api import (
    APIError,
    SSLCertError,
    filter_by_min_stock,
    filter_by_type,
    search_components,
    validate_lcsc_id,
)
from kicad_jlcimport.importer import import_component
from kicad_jlcimport.kicad.library import get_global_lib_dir, load_config
from kicad_jlcimport.kicad.version import DEFAULT_KICAD_VERSION, SUPPORTED_VERSIONS


def cmd_search(args):
    """Search for components."""
    type_filter = ""
    if args.type == "basic":
        type_filter = "Basic"
    elif args.type == "extended":
        type_filter = "Extended"

    try:
        result = search_components(args.keyword, page_size=args.count)
    except SSLCertError as e:
        print(f"  Error: {e}")
        print("  Use --insecure to bypass certificate verification.")
        sys.exit(1)

    total = result["total"]
    results = result["results"]

    results = filter_by_type(results, type_filter)
    min_stock = args.min_stock
    results = filter_by_min_stock(results, min_stock)

    results.sort(key=lambda r: r["stock"] or 0, reverse=True)

    if not args.csv:
        print(f'\n  {total} results for "{args.keyword}"', end="")
        if type_filter:
            print(f" ({type_filter} only)", end="")
        if min_stock > 0:
            print(f" (stock >= {min_stock})", end="")
        print("\n")

    if args.csv:
        import csv

        writer = csv.writer(sys.stdout)
        writer.writerow(["LCSC", "Type", "Price", "Stock", "Part", "Package", "Brand", "Description"])
        for r in results:
            writer.writerow(
                [
                    r["lcsc"],
                    r["type"],
                    r["price"] or "",
                    r["stock"] or "",
                    r["model"],
                    r["package"],
                    r["brand"],
                    r["description"],
                ]
            )
        return

    if not results:
        print("  No results found.")
        return

    # Header
    print(f"  {'#':<3} {'LCSC':<12} {'Type':<8} {'Price':>7} {'Stock':>8}  {'Part'}")
    print(f"  {'─' * 3} {'─' * 12} {'─' * 8} {'─' * 7} {'─' * 8}  {'─' * 30}")

    for i, r in enumerate(results, 1):
        price_str = f"${r['price']:.4f}" if r["price"] else "  N/A  "
        stock_str = f"{r['stock']:>8,}" if r["stock"] else "     N/A"
        print(f"  {i:<3} {r['lcsc']:<12} {r['type']:<8} {price_str:>7} {stock_str}  {r['model']}")
        print(f"      {r['description']}")
        print(f"      {r['package']}  {r['brand']}")

    print()


def _resolve_project_dir(path: str) -> str:
    if not path:
        return ""
    abs_path = os.path.abspath(path)
    if os.path.isfile(abs_path):
        return os.path.dirname(abs_path)
    return abs_path


def cmd_import(args):
    """Import a component and show/save the output."""
    try:
        lcsc_id = validate_lcsc_id(args.part)
    except ValueError as e:
        print(f"  Error: {e}")
        return

    lib_name = args.lib_name
    kicad_version = args.kicad_version

    def log(msg):
        print(f"  {msg}")

    try:
        if getattr(args, "project", None):
            lib_dir = _resolve_project_dir(args.project)
            if not os.path.isdir(lib_dir):
                print(f"  Error: project path does not exist or is not a directory: {args.project}")
                return
            result = import_component(
                lcsc_id,
                lib_dir,
                lib_name,
                overwrite=args.overwrite,
                use_global=False,
                log=log,
                kicad_version=kicad_version,
            )
        elif getattr(args, "global_dest", False) or getattr(args, "global_lib_dir", None):
            if getattr(args, "global_lib_dir", None):
                lib_dir = os.path.abspath(args.global_lib_dir)
                if not os.path.isdir(lib_dir):
                    print(f"  Error: --global-lib-dir does not exist: {lib_dir}")
                    return
            else:
                lib_dir = get_global_lib_dir(kicad_version)
            result = import_component(
                lcsc_id,
                lib_dir,
                lib_name,
                overwrite=args.overwrite,
                use_global=True,
                log=log,
                kicad_version=kicad_version,
            )
        elif args.output:
            result = import_component(
                lcsc_id,
                args.output,
                lib_name,
                export_only=True,
                log=log,
                kicad_version=kicad_version,
            )
        else:
            # No destination: fetch, parse, and show summary without writing
            import tempfile

            with tempfile.TemporaryDirectory() as tmp_dir:
                result = import_component(
                    lcsc_id,
                    tmp_dir,
                    lib_name,
                    export_only=True,
                    log=log,
                    kicad_version=kicad_version,
                )
            if not args.show:
                fp_content = result["fp_content"]
                sym_content = result["sym_content"]
                print(f"  Footprint: {len(fp_content)} bytes")
                if sym_content:
                    print(f"  Symbol: {len(sym_content)} bytes")
                print("\n  Use --show footprint|symbol|both to see output")
                print("  Use -o <dir> to save files")
                return
    except SSLCertError as e:
        print(f"  Error: {e}")
        print("  Use --insecure to bypass certificate verification.")
        sys.exit(1)
    except APIError as e:
        print(f"  Error: {e}")
        return

    fp_content = result["fp_content"]
    sym_content = result["sym_content"]

    if args.show == "footprint" or args.show == "both":
        print("\n  ── Footprint (.kicad_mod) ──")
        print(fp_content)
    if args.show == "symbol" or args.show == "both":
        if sym_content:
            print("\n  ── Symbol (.kicad_sym fragment) ──")
            print(sym_content)
        else:
            print("\n  (No symbol data)")


def main():
    parser = argparse.ArgumentParser(
        prog="jlcimport-cli",
        description="JLCImport CLI - search and test LCSC component imports",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s search "RP2350"
  %(prog)s search "100nF 0402" -t basic
  %(prog)s search "ESP32" -t extended -n 20 --min-stock 100
  %(prog)s search "RP2350" --csv > parts.csv
  %(prog)s import C42415655
  %(prog)s import C42415655 --show footprint
  %(prog)s import C427602 --show both
  %(prog)s import C427602 -o ./output
  %(prog)s import C427602 -p /path/to/project
  %(prog)s import C427602 --global
  %(prog)s import C427602 -o ./output --kicad-version 8
""",
    )

    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (use when behind an intercepting proxy)",
    )

    sub = parser.add_subparsers(dest="command")

    # Search subcommand
    sp = sub.add_parser("search", aliases=["s"], help="Search for components")
    sp.add_argument("keyword", help="Search term")
    sp.add_argument("-n", "--count", type=int, default=10, help="Number of results (default: 10)")
    sp.add_argument(
        "-t", "--type", choices=["basic", "extended", "both"], default="both", help="Part type filter (default: both)"
    )
    sp.add_argument(
        "--min-stock", type=int, default=1, metavar="N", help="Minimum stock count filter (default: 1, use 0 for any)"
    )
    sp.add_argument("--csv", action="store_true", help="Output results as CSV")
    sp.set_defaults(func=cmd_search)

    # Import subcommand
    ip = sub.add_parser("import", aliases=["i"], help="Import a component by LCSC part number")
    ip.add_argument("part", help="LCSC part number (e.g. C427602)")
    ip.add_argument("--show", choices=["footprint", "symbol", "both"], help="Print generated output")
    dest = ip.add_mutually_exclusive_group()
    dest.add_argument("-o", "--output", help="Directory to save output files (export-only)")
    dest.add_argument("-p", "--project", help="KiCad project directory (where .kicad_pro is)")
    dest.add_argument(
        "--global", dest="global_dest", action="store_true", help="Import into KiCad global 3rd-party library"
    )
    ip.add_argument(
        "--global-lib-dir",
        metavar="DIR",
        help="Override global library directory for this run",
    )
    ip.add_argument(
        "--overwrite", action="store_true", help="Overwrite existing symbol/footprint when importing to a library"
    )
    ip.add_argument(
        "--lib-name",
        default=load_config().get("lib_name", "JLCImport"),
        help="Library name (default: from config or 'JLCImport')",
    )
    ip.add_argument(
        "--kicad-version",
        type=int,
        choices=sorted(SUPPORTED_VERSIONS),
        default=DEFAULT_KICAD_VERSION,
        help=f"Target KiCad version (default: {DEFAULT_KICAD_VERSION})",
    )
    ip.set_defaults(func=cmd_import)

    args = parser.parse_args()
    if args.insecure:
        api.allow_unverified_ssl()
    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
