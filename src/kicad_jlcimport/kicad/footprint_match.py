"""Find a KiCad library footprint that has the same pads as JLC's own footprint.

The import dialog can offer an existing KiCad footprint instead of the one
converted from EasyEDA.  A footprint is only offered when it really is the
same land pattern, because JLCPCB places parts by its own footprint: a
substitute with different pads, pitch or numbering gives a board JLC can't
assemble correctly.

1. **Candidates by name.**  Package tokens (``SOT-363``, ``QFN-24``,
   ``SMA``, ``0603``, ...) are taken from JLC's package strings and matched
   against the footprint names in every library KiCad knows about.  Names
   that state a different pitch or body size are dropped.  Two-pad parts
   only consider the footprint kind their designator implies (``R_`` for
   resistors, ``LED_`` for LEDs, ...).
2. **Same pads.**  Each candidate's ``.kicad_mod`` is read.  It must have
   the same pad numbers as JLC's footprint (pads sharing a number count as
   one group), since the imported symbol's pins connect by number.
3. **Geometry fit.**  JLC's pad centres are fitted onto the candidate with
   the production solver (:func:`..production.solver.solve_placement`).  A
   candidate is kept only when the fit lands within
   ``Tolerances.high_rms`` at a 90° step and the numbering isn't mirrored,
   which is what lets production export place it with high confidence.
4. **Ranking.**  Standard variants beat HandSolder ones; then lower RMS
   residual, then closer pad sizes.

For polarized two-terminal parts the + / - side of JLC's footprint (its
polarity marks, else its symbol's pin names) is checked against KiCad's
pad-1 convention for the candidate, as production export does, and reported
in :attr:`FootprintMatch.polarity`.
"""

import functools
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..production.conventions import TOP
from ..production.kicad_pcb import footprint_from_node, parse_sexpr
from ..production.models import FootprintRecord, PadRecord
from ..production.polarity import (
    BY_CONVENTION,
    BY_FOOTPRINT_NAME,
    BY_NAMES,
    board_kind,
    footprint_name_roles,
    jlc_pad_roles,
    match_polarity,
)
from ..production.solver import (
    DEFAULT_TOLERANCES,
    MIRRORED,
    Tolerances,
    group_pads,
    normalize_pad_number,
    solve_placement,
)
from .library import _iter_footprint_libraries
from .version import DEFAULT_KICAD_VERSION

# FootprintMatch.polarity values.
POLARITY_NA = ""  # not a polarized two-terminal part
POLARITY_SAME = "same"  # pad 1 has the same polarity on both footprints
POLARITY_REVERSED = "reversed"  # KiCad's pad 1 is the other polarity from JLC's pad 1
POLARITY_UNKNOWN = "unknown"  # KiCad's footprint is polarized but JLC's doesn't say which pad is which

MAX_CANDIDATES = 60  # footprint files read per lookup, best name matches first
MAX_SIZE_ERROR = 0.5  # mean relative pad-size difference above which pads are not the same land
_SIZE_WEIGHT = 0.1  # mm of RMS residual one unit of relative pad-size difference is worth
PITCH_TOL = 0.035  # mm; JLC rounds some pitches to 0.1 mm (P1.3 for 1.27)
BODY_TOL = 0.3  # mm

# Package families written as "<family>-<pin count>[-<variant>]".
_FAMILIES = frozenset(
    """SOT SOD SOIC SOP SSOP TSSOP HTSSOP ETSSOP MSOP VSSOP TSOT QFN DFN LQFP TQFP HLQFP QFP LGA BGA TO SC
    PLCC WSON SON USON VSON HSOP ESOP PDFN UDFN VQFN WQFN TDFN VDFN WDFN UQFN SQFN TQFN HVQFN DO DIP SDIP
    POWERDI SIP SMD SO TSFP LQFN VLGA WLCSP WCSP LFBGA NFBGA TFBGA VFBGA FBGA XBGA""".split()
)
# Package names that need no pin count.
_STANDALONE = frozenset("SMA SMB SMC SMAF SMBF SMCF DPAK D2PAK MELF MINIMELF MICROMELF".split())
# Family spellings that share a footprint with another family's (same pin count).
_FAMILY_ALIASES = {
    "SOP": ("SOIC", "SO"),
    "ESOP": ("SOIC",),
    "MSOP": ("VSSOP",),
    "VSSOP": ("MSOP",),
    "LQFP": ("TQFP",),
    "TQFP": ("LQFP",),
    "HLQFP": ("LQFP",),
    "ETSSOP": ("TSSOP", "HTSSOP"),
    "VQFN": ("QFN",),
    "WQFN": ("QFN",),
    "UQFN": ("QFN",),
    "SQFN": ("QFN",),
    "TQFN": ("QFN",),
    "HVQFN": ("QFN",),
    "UDFN": ("DFN",),
    "TDFN": ("DFN",),
    "VDFN": ("DFN",),
    "WDFN": ("DFN",),
    "PDFN": ("DFN",),
    "WSON": ("SON", "DFN"),
    "VSON": ("SON", "DFN"),
    "USON": ("SON", "DFN"),
    "LQFN": ("QFN",),
    "VLGA": ("LGA",),
    "WCSP": ("WLCSP",),
    "LFBGA": ("BGA",),
    "NFBGA": ("BGA",),
    "TFBGA": ("BGA",),
    "VFBGA": ("BGA",),
    "FBGA": ("BGA",),
    "XBGA": ("BGA",),
}
# Whole package tokens with another name in KiCad's libraries.
_TOKEN_ALIASES = {
    "SOT25": ("SOT235",),
    "SOT26": ("SOT236",),
    "TSOT25": ("TSOT235", "SOT235"),
    "TSOT26": ("TSOT236", "SOT236"),
    "SOT753": ("SOT235",),
    "SC745": ("SOT235",),
    "SC74A5": ("SOT235",),
    "SC746": ("SOT236",),
    "SC753": ("SOT416",),
    "SC893": ("SOT523",),
    "SOD523F": ("SOD523",),
    "SOD123F": ("SOD123F", "SOD123"),
    "TO2522": ("DPAK",),
    "TO2632": ("D2PAK",),
    "DO214AC": ("SMA",),
    "DO214AA": ("SMB",),
    "DO214AB": ("SMC",),
}

# Two-pad parts: designator prefix -> KiCad footprint name prefixes of that kind of part.
_TWO_PAD_KINDS = {
    "R": ("R_",),
    "RN": ("R_",),
    "RT": ("R_",),
    "NTC": ("R_",),
    "C": ("C_", "CP_"),
    "E": ("CP_",),
    "CP": ("CP_",),
    "L": ("L_",),
    "FB": ("L_",),
    "D": ("D_",),
    "ZD": ("D_",),
    "TV": ("D_",),
    "LED": ("LED_",),
    "F": ("Fuse_",),
    "FU": ("Fuse_",),
    "X": ("Crystal_",),
    "Y": ("Crystal_",),
}

_VARIANT_RE = re.compile(r"HandSolder|ReverseMount|Wave|Castellated", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Z]+|\d+(?:\.\d+)?")
_PITCH_RE = re.compile(r"(?:^|[^A-Z])P(\d+(?:\.\d+)?)")
_KICAD_BODY_RE = re.compile(r"(?<![A-Z0-9.])(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)")
_JLC_BODY_RE = re.compile(r"(?:^|[^A-Z])L(\d+(?:\.\d+)?)-W(\d+(?:\.\d+)?)")
_JLC_EP_RE = re.compile(r"(?:^|[^A-Z])EP(\d+(?:\.\d+)?)(?:X(\d+(?:\.\d+)?))?")
_CHUNK_SPLIT_RE = re.compile(r"[_\s(),]+")
_MIN_PART_NUMBER = 6  # characters a part-number chunk needs to count as a match


@dataclass
class FootprintMatch:
    """A KiCad library footprint whose pads fit JLC's footprint."""

    ref: str  # "Library:Footprint"
    rms: float  # RMS distance between JLC's pad centres and the fitted KiCad pads (mm)
    max_error: float  # worst pad-centre distance (mm)
    pad_groups: int  # pad numbers on both footprints
    size_error: float  # mean relative difference in pad width/height; 0 when sizes are unknown
    polarity: str = POLARITY_NA
    flags: List[str] = field(default_factory=list)  # solver flags of the fit

    @property
    def recommended(self) -> bool:
        """True when the KiCad footprint can replace JLC's with no caveat."""
        return self.polarity in (POLARITY_NA, POLARITY_SAME)

    def describe(self) -> str:
        """One line for the import dialog: how well the footprint fits and any polarity caveat."""
        text = f"{self.pad_groups} pads, centres within {self.rms:.3f} mm RMS of JLC's footprint"
        if self.size_error:
            text += f", pad sizes within {self.size_error:.0%}"
        if self.polarity == POLARITY_SAME:
            text += "; pad 1 has the same polarity"
        elif self.polarity == POLARITY_REVERSED:
            text += "; polarity reversed: KiCad's pad 1 is JLC's pad 2, so the symbol's pins would be swapped"
        elif self.polarity == POLARITY_UNKNOWN:
            text += "; polarity can't be checked: JLC's data doesn't say which pad is + or -"
        return text


# ---------------------------------------------------------------------------
# Name tokens
# ---------------------------------------------------------------------------


def _family_tokens(family: str, numbers: Sequence[str]) -> Set[str]:
    out: Set[str] = set()
    for fam in (family,) + _FAMILY_ALIASES.get(family, ()):
        out.add(fam + numbers[0])
        if len(numbers) > 1:
            out.add(fam + numbers[0] + numbers[1])
    return out


@functools.cache
def package_tokens(text: str) -> frozenset:
    """Distinctive package tokens in *text*, e.g. ``{"SOT23", "SOT236"}`` for ``SOT-23-6_L2.9-W1.6``."""
    upper = (text or "").upper()
    words = _WORD_RE.findall(upper)
    out: Set[str] = set()
    for i, word in enumerate(words):
        if word in _FAMILIES:
            numbers = []
            for nxt in words[i + 1 : i + 3]:
                if not nxt[0].isdigit() or "." in nxt:
                    break
                numbers.append(nxt)
            if numbers:
                out |= _family_tokens(word, numbers)
            # "SOD-523F", "TO-277A": the letter suffix belongs to the name.
            if numbers and i + len(numbers) + 1 < len(words) and len(words[i + len(numbers) + 1]) == 1:
                suffix = words[i + len(numbers) + 1]
                if suffix in ("A", "B", "F"):
                    out.add(word + numbers[0] + suffix)
        elif word in _STANDALONE:
            out.add(word)
        elif word.isdigit() and len(word) == 4:
            out.add(word)  # chip sizes: 0402, 0603, 1206, 3528, ...
    for token in list(out):
        out.update(_TOKEN_ALIASES.get(token, ()))
    return frozenset(out)


@functools.cache
def part_number_chunks(text: str) -> frozenset:
    """Alphanumeric chunks of *text* that look like part numbers (``B4B-PH-K`` -> ``B4BPHK``)."""
    out = set()
    for chunk in _CHUNK_SPLIT_RE.split((text or "").upper()):
        norm = re.sub(r"[^A-Z0-9]", "", chunk)
        if len(norm) >= _MIN_PART_NUMBER and re.search(r"[A-Z]", norm) and re.search(r"\d", norm):
            out.add(norm)
    return frozenset(out)


def _shares_part_number(jlc_chunks: Set[str], fp_name: str) -> bool:
    """True when a part-number chunk of the footprint name starts one of JLC's (or the other way round)."""
    return any(a.startswith(b) or b.startswith(a) for a in jlc_chunks for b in part_number_chunks(fp_name))


def _pitch(text: str) -> Optional[float]:
    m = _PITCH_RE.search(text.upper())
    return float(m.group(1)) if m else None


def _kicad_body(name: str) -> Optional[Tuple[float, float]]:
    upper = name.upper()
    for m in _KICAD_BODY_RE.finditer(upper):
        if upper[max(0, m.start() - 2) : m.start()] == "EP":
            continue
        return tuple(sorted((float(m.group(1)), float(m.group(2)))))
    return None


def _jlc_body(text: str) -> Optional[Tuple[float, float]]:
    m = _JLC_BODY_RE.search(text.upper())
    return tuple(sorted((float(m.group(1)), float(m.group(2))))) if m else None


def _exposed_pad(text: str) -> Optional[Tuple[float, float]]:
    """Exposed-pad size stated in a name (``EP2.7`` or ``EP2.6x2.6mm``), sorted, else None."""
    m = _JLC_EP_RE.search(text.upper())
    if not m:
        return None
    a = float(m.group(1))
    return tuple(sorted((a, float(m.group(2)) if m.group(2) else a)))


def _name_conflicts(names: Sequence[str], fp_name: str) -> bool:
    """True when a JLC package name and the footprint name state a different pitch, body or exposed pad."""
    fp_pitch = _pitch(fp_name)
    fp_body = _kicad_body(fp_name)
    fp_ep = _exposed_pad(fp_name)
    for name in names:
        ep = _exposed_pad(name)
        if ep is not None and fp_ep is not None and any(abs(a - b) > BODY_TOL for a, b in zip(ep, fp_ep)):
            return True
        pitch = _pitch(name)
        if pitch is not None and fp_pitch is not None and abs(pitch - fp_pitch) > PITCH_TOL:
            return True
        body = _jlc_body(name) or _kicad_body(name)
        if body is not None and fp_body is not None and any(abs(a - b) > BODY_TOL for a, b in zip(body, fp_body)):
            return True
    return False


def _two_pad_prefixes(prefix: str) -> Optional[Tuple[str, ...]]:
    return _TWO_PAD_KINDS.get((prefix or "").strip().upper())


def _name_candidates(
    names: Sequence[str],
    libraries: Iterable[Tuple[str, str]],
    two_pad_prefixes: Optional[Tuple[str, ...]],
) -> List[Tuple[int, str, str, str]]:
    """(score, library, footprint name, path) of footprints sharing a package token, best first."""
    tokens: Set[str] = set()
    chunks: Set[str] = set()
    for name in names:
        tokens |= package_tokens(name)
        chunks |= part_number_chunks(name)
    if not tokens and not chunks:
        return []
    found = []
    for lib_name, lib_path in libraries:
        try:
            entries = os.listdir(lib_path)
        except OSError:
            continue
        for entry in entries:
            if not entry.lower().endswith(".kicad_mod"):
                continue
            fp_name = entry[: -len(".kicad_mod")]
            if two_pad_prefixes is not None and not fp_name.startswith(two_pad_prefixes):
                continue
            score = len(tokens & package_tokens(fp_name))
            if chunks and _shares_part_number(chunks, fp_name):
                score += 2
            if not score or _name_conflicts(names, fp_name):
                continue
            found.append((score, lib_name, fp_name, os.path.join(lib_path, entry)))
    found.sort(key=lambda c: (-c[0], bool(_VARIANT_RE.search(c[2])), len(c[2]), c[2], c[1]))
    return found


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def read_footprint_file(path: str) -> Optional[FootprintRecord]:
    """Pads of a ``.kicad_mod`` file in its own frame, or None if it can't be read."""
    try:
        with open(path, encoding="utf-8") as f:
            node = parse_sexpr(f.read())
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(node, list) or not node or node[0] not in ("footprint", "module"):
        return None
    record = footprint_from_node(node)
    record.footprint = os.path.basename(path)[: -len(".kicad_mod")]
    return record


def _group_sizes(pads: Sequence[PadRecord]) -> Dict[str, Tuple[float, float]]:
    """Pad number -> (short, long) side of the group's largest pad; paste-only segments are smaller."""
    sizes: Dict[str, Tuple[float, float]] = {}
    for pad in pads:
        num = normalize_pad_number(pad.number)
        if not num or pad.width <= 0 or pad.height <= 0:
            continue
        dims = tuple(sorted((pad.width, pad.height)))
        if num not in sizes or dims[0] * dims[1] > sizes[num][0] * sizes[num][1]:
            sizes[num] = dims
    return sizes


def _drilled(pads: Sequence[PadRecord]) -> Set[str]:
    return {normalize_pad_number(p.number) for p in pads if p.drill > 0 and normalize_pad_number(p.number)}


def size_error(a: Sequence[PadRecord], b: Sequence[PadRecord]) -> float:
    """Mean relative difference of pad width and height over pad numbers both sides give a size for."""
    sa, sb = _group_sizes(a), _group_sizes(b)
    errors = [abs(sa[n][i] - sb[n][i]) / max(sa[n][i], sb[n][i]) for n in set(sa) & set(sb) for i in (0, 1)]
    return sum(errors) / len(errors) if errors else 0.0


def _polarity(
    kicad: FootprintRecord, jlc_pads: Sequence[PadRecord], pin_names: Dict[str, str], marks: Dict[str, str]
) -> Optional[str]:
    """How pad 1 of the KiCad footprint lines up with JLC's polarity (a POLARITY_* value).

    None when JLC's part is polarized but the KiCad footprint isn't one of
    KiCad's polarized footprints (LED_ / D_ / CP_ or an _AK / _KA name), so
    it shows no polarity at all.
    """
    kicad_polarized = bool(board_kind(kicad.footprint) or footprint_name_roles(kicad.footprint))
    jlc_polarized = bool(jlc_pad_roles(jlc_pads, pin_names, marks))
    if not jlc_polarized:
        return POLARITY_UNKNOWN if kicad_polarized else POLARITY_NA
    if not kicad_polarized:
        return None
    match = match_polarity(kicad.pads, jlc_pads, pin_names, footprint_name=kicad.footprint, jlc_marks=marks)
    if match.source not in (BY_NAMES, BY_FOOTPRINT_NAME, BY_CONVENTION) or not match.jlc_source:
        return POLARITY_UNKNOWN
    return POLARITY_REVERSED if match.swapped else POLARITY_SAME


def fit_footprint(
    kicad: FootprintRecord,
    jlc_pads: Sequence[PadRecord],
    pin_names: Optional[Dict[str, str]] = None,
    marks: Optional[Dict[str, str]] = None,
    ref: str = "",
    tol: Tolerances = DEFAULT_TOLERANCES,
) -> Optional[FootprintMatch]:
    """The fit of JLC's pads onto *kicad*'s, or None unless it is the same land pattern.

    Same pad numbers and through-hole pads, JLC's pad centres within
    ``tol.high_rms`` RMS after a rotation by a 90° step, numbering not
    mirrored, pad sizes within :data:`MAX_SIZE_ERROR`, and a polarized
    footprint for a polarized part.  *pin_names* and *marks* are JLC's
    symbol pin names and footprint + / - marks, used to check polarity.
    """
    jlc_groups = group_pads(jlc_pads)
    kicad_groups = group_pads(kicad.pads)
    if len(jlc_groups) < 2 or set(jlc_groups) != set(kicad_groups):
        return None
    if _drilled(jlc_pads) != _drilled(kicad.pads):
        return None
    fit = solve_placement(kicad.pads, jlc_pads, TOP, kicad_orientation=0.0, tol=tol)
    if not fit.snapped or MIRRORED in fit.flags or fit.rms > tol.high_rms:
        return None
    sizes = size_error(jlc_pads, kicad.pads)
    if sizes > MAX_SIZE_ERROR:
        return None
    polarity = _polarity(kicad, jlc_pads, pin_names or {}, marks or {})
    if polarity is None:
        return None
    return FootprintMatch(
        ref=ref or kicad.footprint,
        rms=fit.rms,
        max_error=fit.max_error,
        pad_groups=len(jlc_groups),
        size_error=sizes,
        polarity=polarity,
        flags=list(fit.flags),
    )


def find_matching_footprint(
    jlc_pads: Sequence[PadRecord],
    package_names: Sequence[str],
    prefix: str = "",
    pin_names: Optional[Dict[str, str]] = None,
    marks: Optional[Dict[str, str]] = None,
    project_dir: str = "",
    kicad_version: int = DEFAULT_KICAD_VERSION,
    libraries: Optional[Sequence[Tuple[str, str]]] = None,
) -> Optional[FootprintMatch]:
    """The best KiCad library footprint with the same pads as JLC's footprint, or None.

    *jlc_pads* are JLC's footprint pads (footprint-local mm, with sizes).
    *package_names* are JLC's package strings (the part's package, its
    footprint's title) and its manufacturer part number.  *prefix* is the symbol's designator prefix, which
    picks the kind of footprint for two-pad parts; with no known prefix a
    two-pad part gets no match.  *libraries* defaults to the libraries in
    the project and global fp-lib-tables.
    """
    names = [n for n in dict.fromkeys(package_names) if n]
    groups = group_pads(jlc_pads)
    if not names or len(groups) < 2:
        return None
    two_pad_prefixes = None
    if len(groups) == 2:
        two_pad_prefixes = _two_pad_prefixes(prefix)
        if two_pad_prefixes is None:
            return None
    if libraries is None:
        libraries = _iter_footprint_libraries(project_dir, kicad_version)

    best: Optional[Tuple[tuple, FootprintMatch]] = None
    for _, lib_name, fp_name, path in _name_candidates(names, libraries, two_pad_prefixes)[:MAX_CANDIDATES]:
        record = read_footprint_file(path)
        if record is None:
            continue
        match = fit_footprint(record, jlc_pads, pin_names, marks, ref=f"{lib_name}:{fp_name}")
        if match is None:
            continue
        key = (
            not match.recommended,
            bool(_VARIANT_RE.search(fp_name)),
            match.rms + _SIZE_WEIGHT * match.size_error,
            len(fp_name),
            fp_name,
        )
        if best is None or key < best[0]:
            best = (key, match)
    return best[1] if best else None
