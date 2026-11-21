"""The call tree of a Rust function from the workspace's SCIP index database (``_scip_db``): no language
server is started for the query, so a repeat query is a few indexed reads. Used before the language
server; a missing or stale database falls back to the server (``codeflow.tree``), never to an error.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from . import _scip
from ._lsp_client import _project_root
from ._scip_db import SqlIndex

_MAX_CHILDREN = 40
_open: dict[str, SqlIndex] = {}


def _open_index(db_path: Path) -> SqlIndex:
    """One open database per path; a rebuilt one (a new export) replaces the old handle."""
    key = str(db_path)
    if key not in _open:
        for old in _open.values():
            old.close()
        _open.clear()
        _open[key] = SqlIndex(db_path)
    return _open[key]


def _start_symbol(index: SqlIndex, rel: str, line0: int, col0: int | None, name: str | None) -> str | None:
    """The callable at a position: the symbol under the cursor, else the first callable defined on the line."""
    if col0 is not None:
        symbol = index.symbol_at(rel, line0, col0)
        if symbol and symbol.endswith(_scip._CALLABLE_SUFFIX) and index.known(symbol):
            return symbol
    for symbol in index.definition_on_line(rel, line0):
        if name is None or _scip._descriptor_name(symbol) == name:
            return symbol
    if name is None:  # a line inside a body: the function it is written in (a closure's lines included)
        return index.enclosing_callable(rel, line0, col0 or 0)
    return None


async def tree(file: Path, at: str | int, direction: str, depth: int, root: Path):
    """A ``FlowTree`` like the language server's, from the SCIP index database of ``file``'s workspace."""
    from . import FlowTree, _name_position, _render, _rel

    workspace = _project_root(file)

    def show(rel_path: str) -> str:
        return _rel(str(workspace / rel_path), root)

    db_path = await asyncio.to_thread(_scip.cached, workspace)
    if db_path is None:
        started = await asyncio.to_thread(_scip.start_export, workspace)
        raise LookupError(
            f"the SCIP index of {workspace} is {'being built in the background' if started else 'still being built'}"
        )
    index = await asyncio.to_thread(_open_index, db_path)
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
            child_name = index.name(child)
            if direction == "incoming":
                edges.append((child_name, index.name(sym), f"{site_path}:{where}"))
            else:
                edges.append((index.name(sym), child_name, f"{site_path}:{where}"))
            definition = index.definition(child)
            child_path, child_line = (definition[0], definition[1] + 1) if definition else (found[0][0], lines[0])
            here = f"{child_name}  {show(child_path)}:{child_line}"
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

    top = index.definition(symbol)
    top_path, top_line = (top[0], top[1] + 1) if top else (rel, line0 + 1)
    arrow = "calls" if direction == "outgoing" else "is called by"
    title = f"{index.name(symbol)}  {show(top_path)}:{top_line}   [{arrow}, depth {depth}, rust, scip]"
    kids = build(symbol, 1)
    return FlowTree(index.name(symbol), direction, _render(title, kids), edges)
