"""Views over a control-flow graph: a range of its blocks, and the paths between two of them. Both work on
the graph dict codeflow builds (``blocks`` with string ``id``s and ``succ`` edges to integer block ids),
so the same views serve the text output and the ``--json`` output.
"""

from __future__ import annotations

import re

MAX_PATHS = 50


class ViewError(ValueError):
    """A view that cannot be built from the arguments given."""


def parse_block(text: str | int) -> int:
    """A displayed block number: ``3`` or ``B3``."""
    match = re.fullmatch(r"[Bb]?(\d+)", str(text).strip())
    if match is None:
        raise ViewError(f"expected a block such as B3 or 3, not {text!r}")
    return int(match.group(1))


def display_map(graph: dict) -> dict[int, int]:
    """Displayed block number (B7 -> 7) to the internal id the graph uses for it."""
    return {int(name[1:]): int(internal) for internal, name in graph.get("names", {}).items()}


def displayed(graph: dict) -> dict:
    """The graph with block ids renumbered to the displayed numbers, so the JSON ids are the ones printed."""
    mapping = {internal: number for number, internal in display_map(graph).items()}
    blocks = []
    for block in graph.get("blocks", []):
        kept = dict(block, id=str(mapping[int(block["id"])]))
        kept["succ"] = [dict(edge, to=mapping[int(edge["to"])]) for edge in block.get("succ", []) if int(edge["to"]) in mapping]
        blocks.append(kept)
    entry = mapping.get(int(graph["entry"]), graph["entry"]) if "entry" in graph else None
    return dict(graph, blocks=blocks, entry=entry, names={str(n): f"B{n}" for n in mapping.values()})


def parse_blocks(spec: str) -> set[int]:
    """``12-30``, ``3,5,9``, ``B3-B9`` or both: the displayed block numbers named."""
    ids: set[int] = set()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        match = re.fullmatch(r"[Bb]?(\d+)(?:-[Bb]?(\d+))?", part)
        if match is None:
            raise ViewError(f"--blocks takes ids or ranges like 12-30 and 3,5,9, not {part!r}")
        low = int(match.group(1))
        high = int(match.group(2) or low)
        if high < low:
            raise ViewError(f"--blocks range {part} runs backwards")
        ids.update(range(low, high + 1))
    if not ids:
        raise ViewError("--blocks names no block")
    return ids


def block_ids(graph: dict) -> set[int]:
    return {int(block["id"]) for block in graph.get("blocks", [])}


def internal_ids(graph: dict, numbers: set[int]) -> tuple[set[int], list[int]]:
    """The internal ids of the displayed numbers, and the numbers that are not blocks of this function."""
    known = display_map(graph)
    missing = sorted(n for n in numbers if n not in known)
    return {known[n] for n in numbers if n in known}, missing


def subgraph(graph: dict, ids: set[int]) -> dict:
    """The blocks in ``ids`` with the edges between them; an edge that leaves the range is dropped."""
    blocks = []
    for block in graph.get("blocks", []):
        if int(block["id"]) not in ids:
            continue
        kept = dict(block)
        kept["succ"] = [edge for edge in block.get("succ", []) if int(edge["to"]) in ids]
        blocks.append(kept)
    return dict(graph, blocks=blocks)


def _successors(graph: dict) -> dict[int, list[int]]:
    return {int(b["id"]): [int(e["to"]) for e in b.get("succ", [])] for b in graph.get("blocks", [])}


def paths(graph: dict, source: int, target: int, limit: int = MAX_PATHS) -> tuple[list[list[int]], bool]:
    """Simple paths from ``source`` to ``target`` (no block twice), at most ``limit`` of them, shortest first.
    The flag says whether more existed than were returned."""
    succ = _successors(graph)
    if source not in succ or target not in succ:
        raise ViewError(f"B{source if source not in succ else target} is not a block of this function")
    found: dict[tuple[int, ...], None] = {}  # distinct block sequences: two edges to one block give one path
    more = False
    stack: list[tuple[int, list[int]]] = [(source, [source])]
    while stack:
        node, trail = stack.pop()
        if node == target:
            if len(found) >= limit:
                more = True
                break
            found[tuple(trail)] = None
            continue
        for nxt in reversed(succ.get(node, [])):
            if nxt not in trail:
                stack.append((nxt, trail + [nxt]))
    ordered = sorted(found, key=len)
    return [list(path) for path in ordered], more


def render_paths(found: list[list[int]], more: bool, source: int, target: int) -> str:
    """``found`` are displayed block numbers."""
    if not found:
        return f"no path from B{source} to B{target}"
    lines = [f"{len(found)}{'+' if more else ''} path(s) from B{source} to B{target}, shortest first:"]
    lines += [" -> ".join(f"B{b}" for b in path) for path in found]
    return "\n".join(lines)
