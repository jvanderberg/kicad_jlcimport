#!/usr/bin/env python3
"""CLI tool for testing JLCImport search and import."""

import argparse
import json
import os
import sys

from kicad_jlcimport.easyeda import api
from kicad_jlcimport.easyeda.api import (
    APIError,
    SSLCertError,
    filter_by_min_stock,
    filter_by_type,
    search_components,
    search_components_cn,
    validate_lcsc_id,
)
from kicad_jlcimport.importer import import_component
from kicad_jlcimport.kicad.library import (
    LibraryNameError,
    SymbolLibraryError,
    config_path,
    get_global_lib_dir,
    load_config,
    validate_lib_name,
)
from kicad_jlcimport.kicad.version import DEFAULT_KICAD_VERSION, SUPPORTED_VERSIONS
from kicad_jlcimport.production.export import (
    board_basename,
    default_output_dir,
    make_caches,
    resolve_output_dir,
    write_bom_cpl,
)
from kicad_jlcimport.production.kicad_cli import (
    KicadCliError,
    find_kicad_cli,
    generate_fabrication_zip,
    not_found_message,
    supports_zone_refill,
)
from kicad_jlcimport.production.kicad_pcb import read_board_with_layers
from kicad_jlcimport.production.report import ERROR, run_check


def cmd_search(args):
    """Search for components."""
    type_filter = ""
    if args.type == "basic":
        type_filter = "Basic"
    elif args.type == "extended":
        type_filter = "Extended"

    region = getattr(args, "region", "global")
    search_fn = search_components_cn if region == "cn" else search_components
    try:
        result = search_fn(args.keyword, page_size=args.count)
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
        price_str = f"{r.get('currency', '$')}{r['price']:.4f}" if r["price"] else "  N/A  "
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

    try:
        lib_name = validate_lib_name(args.lib_name)
    except LibraryNameError as e:
        print(f"  Error: {e}")
        print(f'  Pass a different --lib-name, or change "lib_name" in {config_path()}')
        sys.exit(1)
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
    except SymbolLibraryError as e:
        print(f"  Error: {e}")
        sys.exit(1)

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


def _production_failed(args, log, message, report=None, hint=""):
    """Report a failure that stopped the run (with --json, as JSON on stdout) and exit 1."""
    print(f"  Error: {message}", file=log)
    if hint:
        print(f"  {hint}", file=log)
    _print_production_json(args, report, error=message)
    sys.exit(1)


def _print_production_json(args, report=None, files=None, error=None):
    """With --json, print the result.  *files* is None when nothing was written; *error* says why a run stopped."""
    if args.json:
        data = {"board": os.path.abspath(args.board), "error": error}
        if report is not None:
            data.update(report.to_dict())
        data["files"] = files
        print(json.dumps(data, indent=2))


def cmd_production(args):
    """Check part availability and write JLCPCB Gerbers, BOM and CPL files for a board."""
    # With --json, stdout carries only the JSON document; everything else goes to stderr.
    log = sys.stderr if args.json else sys.stdout
    try:
        footprints, copper_layers = read_board_with_layers(args.board)
    except (OSError, ValueError) as e:
        _production_failed(args, log, f"could not read {args.board}: {e}")

    gerbers = not (args.check_only or args.no_gerbers)
    kicad_cli = None
    if gerbers:
        kicad_cli = find_kicad_cli(args.kicad_cli)
        if not kicad_cli:
            _production_failed(
                args,
                log,
                not_found_message(args.kicad_cli),
                hint="Pass --kicad-cli PATH (or set KICAD_CLI), or --no-gerbers to write only BOM/CPL.",
            )

    def progress(done, total, lcsc):
        print(f"\r  Checking parts {done}/{total} ({lcsc})   ", end="", file=sys.stderr, flush=True)

    part_cache, footprint_cache = make_caches()
    try:
        report = run_check(
            footprints, args.boards, part_cache, footprint_cache, progress=progress, check_stock=not args.no_stock
        )
    except SSLCertError as e:
        print(file=sys.stderr)
        _production_failed(args, log, str(e), hint="Use --insecure to bypass certificate verification.")
    print(file=sys.stderr)
    if not args.json:
        print(report.to_text())

    errors = report.count(ERROR)
    if args.check_only:
        _print_production_json(args, report)
        sys.exit(1 if errors else 0)
    if errors and not args.force:
        _production_failed(
            args,
            log,
            "the check found errors; no files written",
            report,
            hint="Use --force to write them anyway.",
        )

    if args.output:
        out_dir = resolve_output_dir(args.output, args.board, relative_to=os.getcwd())
    else:
        out_dir = default_output_dir(args.board)
    basename = board_basename(args.board)
    files = {}
    try:
        if gerbers:
            refill = not args.no_refill_zones
            if refill and not supports_zone_refill(kicad_cli):
                print("  This kicad-cli can't refill zones (needs KiCad 10); zones are plotted as saved.", file=log)
                refill = False
            print("  Plotting Gerbers and drill files with kicad-cli...", file=log)
            # Gerbers first: if plotting fails, no new BOM/CPL sit next to an old zip.
            files["gerbers"], _ = generate_fabrication_zip(
                kicad_cli, os.path.abspath(args.board), out_dir, basename, copper_layers, refill=refill
            )
        files.update(write_bom_cpl(report, out_dir, basename))
    except (OSError, KicadCliError) as e:
        written = "Only the Gerber zip was written." if files else "No files written."
        _production_failed(args, log, str(e), report, hint=written)
    for kind in ("gerbers", "bom", "cpl", "report"):
        if kind in files:
            print(f"  {kind.upper():<8}{files[kind]}", file=log)
    _print_production_json(args, report, files)


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
  %(prog)s production board.kicad_pcb -n 10
  %(prog)s production board.kicad_pcb --check-only
  %(prog)s production board.kicad_pcb --check-only --json
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
    sp.add_argument(
        "--region",
        choices=["global", "cn"],
        default=load_config().get("region", "global"),
        help="Search region: 'global' for international JLCPCB, 'cn' for Chinese SZLCSC (default: from config)",
    )
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

    # Production subcommand
    pp = sub.add_parser(
        "production",
        aliases=["p"],
        help="Check JLCPCB part availability and write Gerbers, BOM and CPL for a .kicad_pcb",
    )
    pp.add_argument("board", help="Path to the .kicad_pcb file")
    pp.add_argument("-n", "--boards", type=int, default=5, help="Number of boards to assemble (default: 5)")
    pp.add_argument("-o", "--output", help="Output directory (default: <project>/production)")
    pp.add_argument("--check-only", action="store_true", help="Only run the check; exit 1 if there are errors")
    pp.add_argument("--force", action="store_true", help="Write files even when the check reports errors")
    pp.add_argument(
        "--no-stock",
        action="store_true",
        help="Skip the stock/price lookup (faster; placements are still solved)",
    )
    pp.add_argument("--no-gerbers", action="store_true", help="Write only BOM, CPL and report (no kicad-cli needed)")
    pp.add_argument(
        "--no-refill-zones",
        action="store_true",
        help="Plot zones as saved instead of refilling them first (refilling needs KiCad 10's kicad-cli)",
    )
    pp.add_argument("--kicad-cli", metavar="PATH", help="kicad-cli to plot Gerbers with (default: $KICAD_CLI, PATH)")
    pp.add_argument(
        "--json",
        action="store_true",
        help="Print the full result (summary, issues, parts, placements, files) as JSON on stdout",
    )
    pp.set_defaults(func=cmd_production)

    args = parser.parse_args()
    if args.insecure:
        api.allow_unverified_ssl()
    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
