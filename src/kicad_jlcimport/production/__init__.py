"""JLCPCB production export: availability check, placement solver, BOM/CPL output.

Everything in this package is plain Python with no ``pcbnew`` dependency.
The KiCad-specific pieces (reading the live board, plotting Gerbers and
drill files) live in :mod:`kicad_jlcimport.production_pcbnew`.
"""
