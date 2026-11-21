"""The call tree of a Rust function from the workspace's SCIP index (``_scip``): no language server is
started for the query, so a repeat query is a file read. Used before the language server; a failed
export or an unreadable index falls back to the server (``codeflow.tree``), never to an error.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from . import _scip
from ._lsp_client import _project_root

_MAX_CHILDREN = 40
_index_cache: dict[tuple[str, float], _scip.Index] = {}


def _load(index_path: Path) -> _scip.Index:
    key = (str(index_path), index_path.stat().st_mtime)
    if key not in _index_cache:
        _index_cache.clear()  # one workspace at a time: the newest export replaces the old
        _index_cache[key] = _scip.Index.load(index_path)
    return _index_cache[key]


def _start_symbol(index: _scip.Index, rel: str, line0: int, col0: int | None, name: str | None) -> str | None:
    """The callable at a position: the symbol under the cursor, else the first callable defined on the line."""
    doc = index.documents.get(rel)
    if doc is None:
        return None
    if col0 is not None:
        symbol = index.symbol_at(rel, line0, col0)
        if symbol and symbol.endswith(_scip._CALLABLE_SUFFIX):
            return symbol
    for occ in doc.ordered:
        if occ.line == line0 and occ.is_definition and occ.symbol.endswith(_scip._CALLABLE_SUFFIX):
            if name is None or _scip._descriptor_name(occ.symbol) == name:
                return occ.symbol
    return None


def _location(index: _scip.Index, symbol: str) -> tuple[str, int]:
    """(relative path, 1-based line) of a symbol's definition."""
    path, occ = index.definitions[symbol]
    return path, occ.line + 1


async def tree(file: Path, at: str | int, direction: str, depth: int, root: Path):
    """A ``FlowTree`` like the language server's, from the SCIP index of ``file``'s workspace."""
    from . import FlowTree, _name_position, _render, _rel

    def show(rel_path: str) -> str:
        return _rel(str(workspace / rel_path), root)

    workspace = _project_root(file)
    index_path = await asyncio.to_thread(_scip.cached, workspace)
    if index_path is None:
        started = await asyncio.to_thread(_scip.start_export, workspace)
        raise LookupError(
            f"the SCIP index of {workspace} is {'being built in the background' if started else 'still being built'}"
        )
    index = await asyncio.to_thread(_load, index_path)
    rel = str(file.resolve().relative_to(workspace))
    name: str | None = None
    if isinstance(at, int):
        line0, col0 = at - 1, None
    elif re.fullmatch(r"\d+", str(at)):
        line0, col0 = int(str(at)) - 1, None
    elif re.fullmatch(r"\d+:\d+", str(at)):
        line_text, _, col_text = str(at).partition(":")
        line0, col0 = int(line_text) - 1, int(col_text) - 1
    else:
        name = str(at).split("::")[-1].split(".")[-1]
        found = _name_position(file, name)
        if found is None:
            raise LookupError(f"no definition of {at!r} found in {file}")
        line0, col0 = found[0] - 1, found[1] - 1
    symbol = _start_symbol(index, rel, line0, col0, name)
    if symbol is None:
        raise LookupError(f"{file}:{line0 + 1}: no function the SCIP index knows at this position")

    def children_of(sym: str) -> list[tuple[str, str, int]]:
        """(neighbour symbol, file of the call site, 1-based line of the call site) for one level."""
        if direction == "incoming":
            return [(caller, path, line + 1) for caller, path, line, _col in index.callers(sym)]
        return [(callee, path, line + 1) for callee, path, line in index.callees(sym)]

    edges: list[tuple[str, str, str]] = []
    expanded: set[str] = {symbol}

    def build(sym: str, level: int) -> list[tuple[str, list]]:
        """One level: each neighbour once, with every site it is reached from (file and line)."""
        sites: dict[str, list[tuple[str, int]]] = {}
        for child, path, line in children_of(sym):
            sites.setdefault(child, []).append((path, line))
        result: list[tuple[str, list]] = []
        for child, found in list(sites.items())[:_MAX_CHILDREN]:
            lines = sorted({line for _, line in found})
            where = ", ".join(str(n) for n in lines[:3])
            site_path = show(found[0][0])
            if direction == "incoming":
                edges.append((index.name(child), index.name(sym), f"{site_path}:{where}"))
            else:
                edges.append((index.name(sym), index.name(child), f"{site_path}:{where}"))
            child_path, child_line = _location(index, child)
            here = f"{index.name(child)}  {show(child_path)}:{child_line}"
            if where:
                here += f"  (at {'line' if len(lines) == 1 else 'lines'} {where})"
            if child in expanded:
                result.append((here + "  (expanded above)", []))
            elif level >= depth:
                result.append((here, []))
            else:
                expanded.add(child)
                result.append((here, build(child, level + 1)))
        hidden = len(sites) - _MAX_CHILDREN
        if hidden > 0:
            result.append((f"… {hidden} more", []))
        return result

    top_path, top_line = _location(index, symbol)
    arrow = "calls" if direction == "outgoing" else "is called by"
    title = f"{index.name(symbol)}  {show(top_path)}:{top_line}   [{arrow}, depth {depth}, rust, scip]"
    kids = build(symbol, 1)
    return FlowTree(index.name(symbol), direction, _render(title, kids), edges)
