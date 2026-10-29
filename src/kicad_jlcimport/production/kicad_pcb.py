"""Read footprint records from a ``.kicad_pcb`` file without pcbnew (KiCad 6+ format).

Used by the CLI.  Inside KiCad the plugin reads the live board through
:mod:`kicad_jlcimport.production_pcbnew` instead.

In the file, pad ``(at x y)`` values are footprint-local and unrotated (for
bottom-side footprints they are stored already mirrored), so a pad's board
position is ``footprint_at + R_screen(theta) * pad_at`` — KiCad's own
``RotatePoint`` in the Y-down board frame.
"""

import math
from typing import List, Tuple, Union

from .conventions import BOTTOM, TOP
from .models import FootprintRecord, PadRecord

SExpr = Union[str, List["SExpr"]]


def tokenize(text: str):
    """Yield '(' , ')' and atom/string tokens from S-expression text."""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
        elif ch in "()":
            yield ch
            i += 1
        elif ch == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                    buf.append({"n": "\n", "t": "\t"}.get(text[i], text[i]))
                else:
                    buf.append(text[i])
                i += 1
            i += 1
            yield ("str", "".join(buf))
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            yield text[i:j]
            i = j


def parse_sexpr(text: str) -> SExpr:
    """Parse S-expression text into nested lists of strings."""
    stack: List[list] = [[]]
    for tok in tokenize(text):
        if tok == "(":
            stack.append([])
        elif tok == ")":
            if len(stack) < 2:
                raise ValueError("unbalanced ')' in S-expression")
            node = stack.pop()
            stack[-1].append(node)
        else:
            stack[-1].append(tok[1] if isinstance(tok, tuple) else tok)
    if len(stack) != 1 or not stack[0]:
        raise ValueError("unbalanced or empty S-expression")
    return stack[0][0]


def _children(node: list, name: str) -> List[list]:
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == name]


def _child(node: list, name: str):
    found = _children(node, name)
    return found[0] if found else None


def _at(node: list) -> Tuple[float, float, float]:
    at = _child(node, "at")
    if not at:
        return 0.0, 0.0, 0.0
    vals = [float(v) for v in at[1:4] if isinstance(v, str) and _is_number(v)]
    vals += [0.0] * (3 - len(vals))
    return vals[0], vals[1], vals[2]


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _to_board(fx: float, fy: float, theta: float, px: float, py: float) -> Tuple[float, float]:
    rad = math.radians(theta)
    c, s = math.cos(rad), math.sin(rad)
    return fx + px * c + py * s, fy - px * s + py * c


def _yes(node) -> bool:
    return bool(node) and (len(node) < 2 or node[1] in ("yes", "true"))


def footprint_from_node(node: list) -> FootprintRecord:
    lib_id = node[1] if len(node) > 1 and isinstance(node[1], str) else ""
    fx, fy, theta = _at(node)
    layer = _child(node, "layer")
    side = BOTTOM if layer and len(layer) > 1 and layer[1].startswith("B.") else TOP

    props = {}
    for prop in _children(node, "property"):
        if len(prop) > 2 and isinstance(prop[1], str) and isinstance(prop[2], str):
            props[prop[1]] = prop[2]
    for text in _children(node, "fp_text"):  # KiCad 6/7 reference/value
        if len(text) > 2 and text[1] in ("reference", "value"):
            props.setdefault(text[1].capitalize(), text[2])

    attr = _child(node, "attr") or []
    flags = {a for a in attr[1:] if isinstance(a, str)}

    pads = []
    for pad in _children(node, "pad"):
        number = pad[1] if len(pad) > 1 and isinstance(pad[1], str) else ""
        px, py, _ = _at(pad)
        bx, by = _to_board(fx, fy, theta, px, py)
        pads.append(PadRecord(number=number, x=bx, y=by))

    return FootprintRecord(
        reference=props.get("Reference", ""),
        value=props.get("Value", ""),
        footprint=lib_id.split(":", 1)[-1],
        x=fx,
        y=fy,
        orientation=theta,
        side=side,
        pads=pads,
        properties=props,
        dnp="dnp" in flags or _yes(_child(node, "dnp")),
        exclude_from_bom="exclude_from_bom" in flags or _yes(_child(node, "exclude_from_bom")),
        exclude_from_pos="exclude_from_pos_files" in flags or _yes(_child(node, "exclude_from_pos_files")),
    )


def read_board_text(text: str) -> List[FootprintRecord]:
    root = parse_sexpr(text)
    if not isinstance(root, list) or not root or root[0] != "kicad_pcb":
        raise ValueError("not a KiCad board file")
    return [footprint_from_node(n) for n in root[1:] if isinstance(n, list) and n and n[0] in ("footprint", "module")]


def read_board(path: str) -> List[FootprintRecord]:
    with open(path, encoding="utf-8") as f:
        return read_board_text(f.read())
