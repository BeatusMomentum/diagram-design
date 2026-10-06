#!/usr/bin/env python3
"""Verify no diagram label mask is clipped by a node painted after it.

SKILL.md §6 keeps an arrow label 6-10px clear of its connector, and §5 fixes the
paint order as background -> zones -> arrows -> labels -> nodes. Nothing keeps a
label mask off a *node*, so a label whose mask lands partly inside a node
rectangle that is painted later gets covered by the node fill: the text renders
as a fragment sitting on the node border.

Paint order is what makes this a defect rather than a stylistic choice:

* A mask overlapping a zone container is fine - zones are painted before labels,
  so the label stays on top. Zone eyebrows rely on this.
* A mask overlapping a node declared *later* in the document is clipped by that
  node. That is the failure this check reports.

Shape heuristics follow the shipped templates:

* A node is a `<rect>` at least 60x40 - large enough for a title and sublabel.
* A label mask is a `<rect>` 20-200 wide and 8-14 tall - the masking plate that
  SKILL.md §6 prescribes (markup in references/primitives-core.md) for arrow
  labels and zone eyebrows. The width cap covers the long mono plates shipped
  in example-sequence-oauth.html (128px) and the wider plates CJK labels need
  at the same glyph count.
* A mask fully contained in a node is a badge chip (`EXT`, `EDGE`, `ORIG`) and
  is legal.

It also checks connector routing (references/primitives-core.md rule 1). A
connector is a `<path>` or `<line>` that carries an arrow marker; a node, for
this check, is a stroked `<rect>` at least 60x40, so unstroked quadrant fills
and chart bars stay out of it. Four shapes are reported:

* A straight segment that is neither horizontal nor vertical. Loop write-back
  spokes (`class="spoke"`) are the documented radial exception and skipped.
* A horizontal or vertical segment lying on a node's border. It hides behind
  the node fill, so the arrow appears to start at the corner.
* A connector endpoint within 8px of a node corner, where the rounded corner
  makes the port ambiguous.
* Two connectors leaving (or two arriving at) the same node less than 8px
  apart, the hard minimum in primitives-core.md rule 4, so neither arrow can be
  traced alone. A head-to-tail chain joint is allowed.

Usage:
    python3 scripts/verify-geometry.py --all
    python3 scripts/verify-geometry.py skills/diagram-design/assets/example-x.html
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = ROOT / "skills/diagram-design/assets"

RECT_RE = re.compile(
    r"<rect\b[^>]*?"
    r'\bx="(?P<x>-?[\d.]+)"\s+'
    r'y="(?P<y>-?[\d.]+)"\s+'
    r'width="(?P<w>[\d.]+)"\s+'
    r'height="(?P<h>[\d.]+)"',
    re.IGNORECASE,
)

NODE_MIN_W = 60.0
NODE_MIN_H = 40.0
MASK_MIN_W = 20.0
MASK_MAX_W = 200.0
MASK_MIN_H = 8.0
MASK_MAX_H = 14.0
EPSILON = 0.5


class Rect:
    __slots__ = ("x", "y", "w", "h", "line", "offset")

    def __init__(self, x, y, w, h, line, offset) -> None:
        self.x, self.y, self.w, self.h = x, y, w, h
        self.line, self.offset = line, offset

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    def __repr__(self) -> str:
        return f"({self.x:g},{self.y:g} {self.w:g}x{self.h:g})"


def parse_rects(source: str) -> list[Rect]:
    rects: list[Rect] = []
    for match in RECT_RE.finditer(source):
        rects.append(
            Rect(
                float(match.group("x")),
                float(match.group("y")),
                float(match.group("w")),
                float(match.group("h")),
                source.count("\n", 0, match.start()) + 1,
                match.start(),
            )
        )
    return rects


def overlap(a: Rect, b: Rect) -> tuple[float, float]:
    return (
        min(a.right, b.right) - max(a.x, b.x),
        min(a.bottom, b.bottom) - max(a.y, b.y),
    )


def contained(inner: Rect, outer: Rect) -> bool:
    return (
        inner.x >= outer.x - EPSILON
        and inner.y >= outer.y - EPSILON
        and inner.right <= outer.right + EPSILON
        and inner.bottom <= outer.bottom + EPSILON
    )


TAG_RE = re.compile(
    r"<(?P<close>/?)(?P<tag>g|svg|rect|path|line)\b(?P<attrs>[^>]*?)(?P<empty>/?)>",
    re.IGNORECASE,
)
TRANSLATE_RE = re.compile(
    r"^\s*translate\(\s*([-+]?[\d.]+)(?:[\s,]+([-+]?[\d.]+))?\s*\)\s*$"
)
PATH_TOKEN_RE = re.compile(
    r"[MmLlHhVvQqCcSsTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
)
# Parameters each path command consumes per repetition.
PATH_ARITY = {"M": 2, "L": 2, "H": 1, "V": 1, "Q": 4, "T": 2, "C": 6, "S": 4, "A": 7, "Z": 0}

AXIS_TOLERANCE = 0.5  # a segment this close to axis-aligned is orthogonal
BORDER_TOLERANCE = 1.0  # a segment this close to a node edge lies on it
BORDER_MIN_RUN = 4.0  # shorter shared runs are a port touching the edge, not a ride
PORT_TOLERANCE = 4.0  # an endpoint this close to a node outline attaches to it
CORNER_CLEARANCE = 8.0  # ports keep this far from a corner (primitives-core rule 1)
SHARED_PORT_MIN = 8.0  # two ports on one node keep at least this far apart (rule 4)


def attribute(attrs: str, name: str) -> str | None:
    match = re.search(rf'(?<![\w-]){re.escape(name)}\s*=\s*"([^"]*)"', attrs)
    return match.group(1) if match else None


def number(attrs: str, name: str) -> float | None:
    value = attribute(attrs, name)
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


Segment = tuple[str, float, float, float, float]  # (kind, x1, y1, x2, y2)


def path_segments(d: str) -> list[Segment] | None:
    """Flatten a path into straight and curved segments, or None if unparseable."""

    tokens = PATH_TOKEN_RE.findall(d)
    segments: list[Segment] = []
    x = y = start_x = start_y = 0.0
    command = ""
    index = 0
    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
            if command in "Zz":
                if (x, y) != (start_x, start_y):
                    segments.append(("line", x, y, start_x, start_y))
                x, y = start_x, start_y
                continue
        if not command:
            return None
        upper = command.upper()
        arity = PATH_ARITY.get(upper)
        if not arity or index + arity > len(tokens):
            return None  # unknown command, numbers after Z, or truncated arguments
        try:
            args = [float(token) for token in tokens[index : index + arity]]
        except ValueError:
            return None
        index += arity
        relative = command.islower()
        if upper == "H":
            nx, ny = (x + args[0] if relative else args[0]), y
        elif upper == "V":
            nx, ny = x, (y + args[0] if relative else args[0])
        else:
            nx, ny = args[-2], args[-1]
            if relative:
                nx, ny = x + nx, y + ny
        if upper == "M":
            x, y = start_x, start_y = nx, ny
            command = "l" if relative else "L"  # implicit lineto after moveto
            continue
        kind = "line" if upper in "LHV" else "curve"
        segments.append((kind, x, y, nx, ny))
        x, y = nx, ny
    return segments


Offset = tuple[float, float]


def translation(attrs: str) -> Offset | None:
    """Offset a `transform` applies, (0, 0) when absent, None when not a translate."""

    transform = attribute(attrs, "transform")
    if transform is None:
        return 0.0, 0.0
    match = TRANSLATE_RE.match(transform)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2) or 0.0)


def shapes(source: str):
    """Yield (tag, attrs, match start, offset) for each rect, path, and line.

    Offsets accumulate `translate()` on enclosing groups, so panels drawn with
    the same local coordinates (architecture delta snapshots) are compared in
    canvas space. Under any other transform, or inside a nested `<svg>` icon,
    the offset is None and the element is left out of connector checks.
    """

    stack: list[Offset | None] = []
    for match in TAG_RE.finditer(source):
        tag, attrs = match.group("tag").lower(), match.group("attrs")
        if tag in {"g", "svg"}:
            if match.group("close"):
                if stack:
                    stack.pop()
            elif not match.group("empty"):
                nested_svg = tag == "svg" and bool(stack)
                stack.append(None if nested_svg else translation(attrs))
            continue
        frame: Offset | None = (0.0, 0.0)
        for step in stack + [translation(attrs)]:
            if step is None or frame is None:
                frame = None
                break
            frame = (frame[0] + step[0], frame[1] + step[1])
        yield tag, attrs, match.start(), frame


def shifted(segments: list[Segment], frame: Offset) -> list[Segment]:
    dx, dy = frame
    return [(kind, x1 + dx, y1 + dy, x2 + dx, y2 + dy) for kind, x1, y1, x2, y2 in segments]


def connectors(source: str) -> list[tuple[int, str, list[Segment]]]:
    """Return (line number, short label, segments) for every arrowed connector."""

    found: list[tuple[int, str, list[Segment]]] = []
    for tag, attrs, start, frame in shapes(source):
        if tag == "rect" or frame is None:
            continue
        if attribute(attrs, "marker-end") is None and attribute(attrs, "marker-start") is None:
            continue
        if "spoke" in (attribute(attrs, "class") or "").split():
            continue
        line = source.count("\n", 0, start) + 1
        if tag == "line":
            coords = [number(attrs, key) for key in ("x1", "y1", "x2", "y2")]
            if None in coords:
                continue
            x1, y1, x2, y2 = coords  # type: ignore[misc]
            segments: list[Segment] | None = [("line", x1, y1, x2, y2)]
            label = f"<line {x1:g},{y1:g} -> {x2:g},{y2:g}>"
        else:
            d = attribute(attrs, "d") or ""
            segments = path_segments(d)
            label = f'<path d="{d if len(d) <= 48 else d[:45] + "..."}">'
        if segments:
            found.append((line, label, shifted(segments, frame)))
    return found


def stroked_nodes(source: str) -> list[Rect]:
    nodes: list[Rect] = []
    for tag, attrs, start, frame in shapes(source):
        if tag != "rect" or frame is None:
            continue
        x, y, w, h = (number(attrs, key) for key in ("x", "y", "width", "height"))
        if None in (x, y, w, h) or w < NODE_MIN_W or h < NODE_MIN_H:  # type: ignore[operator]
            continue
        stroke = (attribute(attrs, "stroke") or "none").strip().lower()
        if stroke in {"none", "transparent"} or number(attrs, "stroke-width") == 0:
            continue
        line = source.count("\n", 0, start) + 1
        nodes.append(Rect(x + frame[0], y + frame[1], w, h, line, start))
    return nodes


def rides_border(segment: Segment, node: Rect) -> str | None:
    _, x1, y1, x2, y2 = segment
    if abs(y1 - y2) <= AXIS_TOLERANCE:
        low, high = sorted((x1, x2))
        shared = min(high, node.right) - max(low, node.x)
        for edge, name in ((node.y, "top"), (node.bottom, "bottom")):
            if abs(y1 - edge) <= BORDER_TOLERANCE and shared > BORDER_MIN_RUN:
                return name
    if abs(x1 - x2) <= AXIS_TOLERANCE:
        low, high = sorted((y1, y2))
        shared = min(high, node.bottom) - max(low, node.y)
        for edge, name in ((node.x, "left"), (node.right, "right")):
            if abs(x1 - edge) <= BORDER_TOLERANCE and shared > BORDER_MIN_RUN:
                return name
    return None


def attached(px: float, py: float, node: Rect) -> bool:
    """True when an endpoint sits on (or just off) the node's outline."""

    outside_x = max(node.x - px, 0.0, px - node.right)
    outside_y = max(node.y - py, 0.0, py - node.bottom)
    inside = min(px - node.x, node.right - px, py - node.y, node.bottom - py)
    return max(outside_x, outside_y) <= PORT_TOLERANCE and inside <= PORT_TOLERANCE


def near_corner(px: float, py: float, node: Rect) -> tuple[float, float] | None:
    if not attached(px, py, node):
        return None
    for cx in (node.x, node.right):
        for cy in (node.y, node.bottom):
            if max(abs(px - cx), abs(py - cy)) < CORNER_CLEARANCE:
                return cx, cy
    return None


def check_connectors(path: Path, source: str) -> list[str]:
    nodes = stroked_nodes(source)
    findings: list[str] = []
    ports: list[tuple[int, float, float, int, str, str, int]] = []
    for ident, (line, label, segments) in enumerate(connectors(source)):
        where = f"{path.name}:{line}: connector {label}"
        for kind, x1, y1, x2, y2 in segments:
            if kind == "line" and abs(x1 - x2) > AXIS_TOLERANCE and abs(y1 - y2) > AXIS_TOLERANCE:
                findings.append(
                    f"{where} has a diagonal segment {x1:g},{y1:g} -> {x2:g},{y2:g}"
                    f" - route it as a rounded right-angle elbow"
                )
                break
        for segment in segments:
            if segment[0] != "line":
                continue
            hit = next(((node, side) for node in nodes if (side := rides_border(segment, node))), None)
            if hit:
                node, side = hit
                findings.append(
                    f"{where} runs along the {side} border of node {node} (line {node.line})"
                    f" - leave the node perpendicular to the edge the port sits on"
                )
                break
        ends = ((segments[0][1], segments[0][2]), (segments[-1][3], segments[-1][4]))
        for px, py in ends:
            corner = next(((node, c) for node in nodes if (c := near_corner(px, py, node))), None)
            if corner:
                node, (cx, cy) = corner
                findings.append(
                    f"{where} attaches at {px:g},{py:g}, within {CORNER_CLEARANCE:g}px of the"
                    f" {cx:g},{cy:g} corner of node {node} (line {node.line})"
                    f" - move the port onto the straight part of the edge"
                )
                break
        for (px, py), role in zip(ends, ("start", "end")):
            for index, node in enumerate(nodes):
                if attached(px, py, node):
                    ports.append((index, px, py, line, label, role, ident))
    findings.extend(shared_ports(path, nodes, ports))
    return findings


def shared_ports(
    path: Path, nodes: list[Rect], ports: list[tuple[int, float, float, int, str, str, int]]
) -> list[str]:
    """Report two connectors leaving, or two arriving, at one point on a node.

    A head-to-tail joint, where one arrow lands and the next leaves, is a chain
    the reader traces in order (Medallion's documented promotion joints), so it
    is not reported. Two starts are a fork and two ends are a merge.
    """

    findings: list[str] = []
    for i, (node_a, ax, ay, line_a, label_a, role_a, id_a) in enumerate(ports):
        for node_b, bx, by, line_b, label_b, role_b, id_b in ports[i + 1 :]:
            if node_a != node_b or id_a == id_b or role_a != role_b:
                continue
            if max(abs(ax - bx), abs(ay - by)) < SHARED_PORT_MIN:
                node = nodes[node_a]
                findings.append(
                    f"{path.name}:{line_b}: connector {label_b} shares the {bx:g},{by:g} port on"
                    f" node {node} (line {node.line}) with {label_a} at line {line_a}"
                    f" - give each connector its own attach point, >={SHARED_PORT_MIN:g}px apart"
                )
    return findings


def check(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    rects = parse_rects(source)
    nodes = [r for r in rects if r.w >= NODE_MIN_W and r.h >= NODE_MIN_H]
    masks = [
        r
        for r in rects
        if MASK_MIN_W <= r.w <= MASK_MAX_W and MASK_MIN_H <= r.h <= MASK_MAX_H
    ]

    findings: list[str] = []
    for mask in masks:
        for node in nodes:
            if node.offset <= mask.offset:
                continue  # painted before the label; the label stays on top
            dx, dy = overlap(mask, node)
            if dx <= 1.0 or dy <= 1.0 or contained(mask, node):
                continue
            findings.append(
                f"{path.name}:{mask.line}: label mask {mask} is clipped by node "
                f"{node} declared later at line {node.line} (overlap {dx:g}x{dy:g}px)"
                f" - move the label onto a free segment of its connector"
            )
            break
    findings.extend(check_connectors(path, source))
    return findings


def targets(args: argparse.Namespace) -> list[Path]:
    if args.all:
        return sorted(ASSET_DIR.glob("*.html"))
    return [Path(p) for p in args.files]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="HTML diagrams to check")
    parser.add_argument("--all", action="store_true", help="check every shipped asset")
    args = parser.parse_args()

    paths = targets(args)
    if not paths:
        parser.error("pass one or more files, or --all")

    findings: list[str] = []
    for path in paths:
        if not path.exists():
            findings.append(f"{path}: file not found")
            continue
        findings.extend(check(path))

    for finding in findings:
        print(finding)
    print(f"Summary: {len(paths)} file(s) checked, {len(findings)} finding(s).")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
