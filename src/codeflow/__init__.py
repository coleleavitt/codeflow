"""Call trees from the code's real structure (a language server, or acorn for
JavaScript), rendered with file:line, instead of grep and guessing.

    await codeflow.tree("crates/pa-tui/src/session_ui/queue.rs", "send_now")
    await codeflow.tree("src/server.py", 120, direction="incoming", depth=2)
    await codeflow.tree("bundle/chunk.deob.js", "$9t.sendQueuedNow", depth=4)

``at`` is a symbol name (``"Class.method"`` for a JS method), a 1-based line,
or ``"line:col"``. Rust, Python and C/C++ go through the session's language
servers (``rlm.lsp`` call hierarchy; the ``lspServers`` setting); ``.js``,
``.mjs`` and ``.cjs`` files (bundles, deobfuscated chunks) through acorn, whole
file, no server. The result prints as the tree; ``.edges`` holds
``(caller, callee, "path:line")`` for further processing.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["FlowGraph", "FlowTree", "cfg", "tree"]

_JS = {".js", ".mjs", ".cjs"}
_C = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh"}
_HERE = Path(__file__).parent
_WALKER = _HERE / "jsflow.mjs"
_MAX_CHILDREN = 30
# Dependency and build trees inside a repository: treated like the standard library.
_VENDORED = {"node_modules", ".venv", "venv", "site-packages", "target", "vendor", "dist", "build", ".cargo", ".rustup"}


@dataclass
class FlowTree:
    """A rendered call tree: ``print()`` it; ``edges`` holds the raw graph."""

    root: str
    direction: str
    text: str
    edges: list[tuple[str, str, str]] = field(default_factory=list)

    def __repr__(self) -> str:
        return self.text

    __str__ = __repr__


async def tree(
    path: str | os.PathLike[str],
    at: str | int,
    *,
    direction: str = "outgoing",
    depth: int = 3,
    root: str | os.PathLike[str] | None = None,
    external: bool = False,
) -> FlowTree:
    """The call tree of the function at ``at`` in ``path``.

    ``direction``: ``"outgoing"`` (what it calls) or ``"incoming"`` (who calls
    it). ``depth`` bounds the levels. LSP trees expand only functions under
    ``root`` (default: the file's git root); with ``external=True`` calls into
    dependencies and the standard library are listed too (never expanded).
    """
    if direction not in ("outgoing", "incoming"):
        raise ValueError("direction must be 'outgoing' or 'incoming'")
    file = Path(path).expanduser().resolve()
    if not file.is_file():
        raise FileNotFoundError(file)
    if file.suffix in _JS:
        return await asyncio.to_thread(_js_tree, file, at, direction, depth)
    return await _lsp_tree(file, at, direction, depth, _root(file, root), external)


@dataclass
class FlowGraph:
    """A rendered control-flow graph: ``print()`` it; ``graph`` holds the blocks and edges."""

    function: str
    text: str
    graph: dict

    def __repr__(self) -> str:
        return self.text

    __str__ = __repr__


async def cfg(
    path: str | os.PathLike[str],
    function: str,
    *,
    level: str = "source",
    cleanup: bool = False,
    root: str | os.PathLike[str] | None = None,
) -> FlowGraph:
    """The control-flow graph of ``function`` in ``path``: its blocks with their source lines, what ends
    each (a branch condition, a loop, a call, a return, a raise), and the edges between them.

    Python uses the ``ast`` module; JavaScript acorn (``"Class.method"`` names work); C/C++ tree-sitter
    (``goto``, ``switch`` fall-through, C++ ``try``); Rust the compiler's MIR through ``rustc_public``
    (calls resolved to the concrete impl, ``?`` and match arms as real branches; it compiles the crate in
    ``target/codeflow``). Rust ``level``: ``"source"`` (default) reads like the code (each ``.await``
    one step, macro internals and drops hidden, straight-line calls merged); ``"async"`` adds the async
    state machine (state dispatch, resume edges, each ``.await``'s Ready/Pending with the suspend);
    ``"mir"`` is the compiler's graph untouched. ``cleanup=True`` adds the unwind/cleanup paths.
    """
    if level not in ("source", "async", "mir"):
        raise ValueError("level must be 'source', 'async' or 'mir'")
    from . import _cfg

    file = Path(path).expanduser().resolve()
    if not file.is_file():
        raise FileNotFoundError(file)
    if file.suffix == ".py":
        calls = _cfg.statement_calls(file, function)
        local = _cfg.local_noreturn(file)
        fallback = {(l, c) for l, c, name in calls if name in _cfg._KNOWN_NORETURN or name.split(".")[-1] in local}
        checked = await _never_returning(file, [(l, c) for l, c, _ in calls], _PY_NEVER)
        graph = await asyncio.to_thread(_cfg.py_cfg, file, function, fallback if checked is None else checked | fallback)
        graph["source"] = "ast" + ("" if checked is None else " + pyright types")
    elif file.suffix in _JS:
        walker = ["node", str(_HERE / "jscfg.mjs"), str(file), function]
        calls = await asyncio.to_thread(_run_json, [*walker, "--calls"])
        checked = await _never_returning(file, [(l, c) for l, c, _ in calls], _TS_NEVER)
        graph = await asyncio.to_thread(_run_json, [*walker, json.dumps(sorted(checked or []))])
        graph["source"] = "acorn" + ("" if checked is None else " + tsgo types")
    elif file.suffix in _C:
        from ._llvm_cfg import llvm_cfg

        try:
            graph = await asyncio.to_thread(llvm_cfg, file, function)
            reason = "no compile_commands.json" if graph is None else ""
        except (RuntimeError, LookupError) as error:
            graph, reason = None, str(error).splitlines()[0][:120]
        if graph is not None:
            graph = _cfg.ir_tidy(graph, cleanup=cleanup)
        else:
            graph = await asyncio.to_thread(_run_json, ["uv", "run", "--quiet", "--script", str(_HERE / "ccfg.py"), str(file), function])
            graph["source"] = f"tree-sitter, syntax only: {reason}"
    elif file.suffix == ".rs":
        from ._rust_cfg import rust_cfg

        graph = _cfg.ir_tidy(await asyncio.to_thread(rust_cfg, file, function), cleanup=cleanup, level=level)
        graph["source"] = "rustc MIR"
    else:
        raise ValueError(f"no control-flow backend for {file.suffix} files")
    simple = _cfg.simplify(graph, cleanup=cleanup)
    return FlowGraph(str(graph["function"]), _cfg.render(simple, _root(file, root)), simple)


_PY_NEVER = re.compile(r"->\s*(?:NoReturn|Never)\b")
_TS_NEVER = re.compile(r"(?:\)\s*:|=>)\s*never\b")
_MAX_HOVERS = 60


async def _never_returning(file: Path, positions: list[tuple[int, int]], never: re.Pattern) -> set[tuple[int, int]] | None:
    """The positions whose callee the language server types as never returning, or None when no
    server answers for the file (the caller falls back to known names)."""
    from rlm import lsp

    found: set[tuple[int, int]] = set()
    for line, col in positions[:_MAX_HOVERS]:
        try:
            answer = await lsp.hover(str(file), line, col)
        except Exception:  # noqa: BLE001 - no server, or it failed: syntax only
            return None
        if not answer.available:
            return None
        if never.search(answer.text):
            found.add((line, col))
    return found


def _run_json(command: list[str]) -> dict:
    run = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if run.returncode != 0:
        raise RuntimeError(f"{Path(command[-2]).name}: {run.stderr.strip()[-800:]}")
    return json.loads(run.stdout)


# ---- rendering -------------------------------------------------------------


def _render(title: str, children: list) -> str:
    """``children``: [(label, [grandchildren...]), ...]."""
    lines = [title]

    def walk(items: list, prefix: str) -> None:
        for index, (label, kids) in enumerate(items):
            last = index == len(items) - 1
            lines.append(f"{prefix}{'└─ ' if last else '├─ '}{label}")
            walk(kids, prefix + ("   " if last else "│  "))

    walk(children, "")
    return "\n".join(lines)


def _rel(path: str, base: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(base))
    except ValueError:
        return path


# ---- language servers ------------------------------------------------------


def _root(file: Path, root: str | os.PathLike[str] | None) -> Path:
    if root is not None:
        return Path(root).expanduser().resolve()
    try:
        top = subprocess.run(
            ["git", "-C", str(file.parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        return Path(top)
    except (OSError, subprocess.SubprocessError):
        return file.parent


_DEF = r"\b(?:fn|def|class|struct|enum|trait|impl|function)\s+{name}\b"
_CALL = r"\b{name}\s*\("
# A fresh server returns no call hierarchy until it has indexed the workspace
# (minutes for a large Rust workspace): the first lookup retries this long.
_INDEX_WAIT_SECONDS = 180


def _name_position(file: Path, name: str, near: int | None = None) -> tuple[int, int] | None:
    """The 1-based line and column of ``name``: its definition in the file, or
    (with ``near``) its first occurrence from that line on (a function's range
    starts at its attributes and docs, before the name)."""
    try:
        lines = file.read_text(errors="replace").splitlines()
    except OSError:
        return None
    short = name.split("::")[-1].split(".")[-1]
    if near is not None:
        word = re.compile(rf"\b{re.escape(short)}\b")
        for index in range(max(near - 1, 0), min(near + 60, len(lines))):
            match = word.search(lines[index])
            if match:
                return index + 1, match.start() + 1
        return None
    for pattern in (_DEF, _CALL):  # a definition first, a call site as the fallback
        compiled = re.compile(pattern.format(name=re.escape(short)))
        for index, line in enumerate(lines):
            match = compiled.search(line)
            if match:
                return index + 1, line.find(short, match.start()) + 1
    return None


async def _lsp_tree(file: Path, at: str | int, direction: str, depth: int, root: Path, external: bool) -> FlowTree:
    from rlm import lsp

    if isinstance(at, int):
        line, col = at, None
    elif isinstance(at, str) and re.fullmatch(r"\d+(:\d+)?", at):
        line, _, raw = at.partition(":")
        line, col = int(line), int(raw) if raw else None
    else:
        found = _name_position(file, str(at))
        if found is None:
            raise LookupError(f"no definition of {at!r} found in {file}")
        line, col = found
    if col is None:
        text = file.read_text(errors="replace").splitlines()[line - 1]
        col = len(text) - len(text.lstrip()) + 1
    deadline = asyncio.get_running_loop().time() + _INDEX_WAIT_SECONDS
    while True:
        prepared = await lsp.call_hierarchy(str(file), line, col, "prepare")
        if not prepared.available:
            raise RuntimeError(prepared.text)
        if prepared.items or asyncio.get_running_loop().time() > deadline:
            break
        await asyncio.sleep(3)
    if not prepared.items:
        raise LookupError(
            f"{file}:{line}:{col}: {prepared.text} (after waiting {_INDEX_WAIT_SECONDS}s for the server to index)"
        )
    top = prepared.items[0]
    edges: list[tuple[str, str, str]] = []
    expanded: set[tuple[str, int]] = set()

    async def children(item: dict, level: int, trail: frozenset) -> list:
        key = (item["path"], item["line"])
        position = _name_position(Path(item["path"]), item["name"], near=item["line"])
        if position is None:
            return []
        answer = await lsp.call_hierarchy(item["path"], position[0], position[1], direction)
        outside: list[str] = []
        result = []
        hidden = 0
        for call in answer.items:
            resolved = Path(call["path"]).resolve()
            inside = resolved.is_relative_to(root) and not _VENDORED & set(resolved.relative_to(root).parts[:-1])
            sites = ", ".join(str(n) for n in sorted({site["line"] for site in call.get("ranges", [])})[:3])
            site_file = item["path"] if direction == "outgoing" else call["path"]
            edges.append(
                (item["name"], call["name"], f"{_rel(site_file, root)}:{sites}")
                if direction == "outgoing"
                else (call["name"], item["name"], f"{_rel(site_file, root)}:{sites}")
            )
            if not inside:
                outside.append(call["name"])
                continue
            if len(result) >= _MAX_CHILDREN:
                hidden += 1
                continue
            here = f"{call['name']}  {_rel(call['path'], root)}:{call['line']}"
            if sites:
                here += f"  (at {'line' if ',' not in sites else 'lines'} {sites})"
            child_key = (call["path"], call["line"])
            if child_key in trail:
                result.append((here + "  ↺", []))
            elif level >= depth:
                result.append((here, []))
            elif child_key in expanded:
                result.append((here + "  (expanded above)", []))
            else:
                expanded.add(child_key)
                result.append((here, await children(call, level + 1, trail | {key})))
        if hidden:
            result.append((f"… {hidden} more", []))
        if outside and external:  # noqa: SIM102 - the listing is opt-in
            names = sorted(set(outside))
            result.append((f"external: {', '.join(names[:12])}{' …' if len(names) > 12 else ''}", []))
        return result

    expanded.add((top["path"], top["line"]))
    kids = await children(top, 1, frozenset())
    arrow = "calls" if direction == "outgoing" else "is called by"
    title = f"{top['name']}  {_rel(top['path'], root)}:{top['line']}   [{arrow}, depth {depth}, {prepared.server or 'lsp'}]"
    return FlowTree(top["name"], direction, _render(title, kids), edges)


# ---- JavaScript (acorn) ----------------------------------------------------


def _js_graph(file: Path) -> dict:
    run = subprocess.run(
        ["node", str(_WALKER), str(file)], capture_output=True, text=True, timeout=600,
    )
    if run.returncode != 0:
        raise RuntimeError(f"jsflow failed on {file}: {run.stderr.strip()[:500]}")
    return json.loads(run.stdout)


def _js_tree(file: Path, at: str | int, direction: str, depth: int) -> FlowTree:
    graph = _js_graph(file)
    nodes: dict[str, int] = graph["nodes"]
    calls: dict[str, list[list]] = graph["edges"]

    def resolve(target: str, owner: str) -> str | None:
        target = target.lstrip("&")
        if target in nodes:
            return target
        if "." in target:  # Obj.method with Obj a class name; ?.method unresolved
            return None
        cls = owner.split(".")[0] if "." in owner else None
        return f"{cls}.{target}" if cls and f"{cls}.{target}" in nodes else None

    if isinstance(at, int):
        start = min(
            (name for name, line in nodes.items() if line <= at),
            key=lambda name: at - nodes[name],
            default=None,
        )
    else:
        start = at if at in nodes else None
        if start is None:  # a class name: its methods are the first level
            methods = [name for name in nodes if name.startswith(f"{at}.")]
            if methods:
                kids = [(f"{m}  :{nodes[m]}", []) for m in methods]
                return FlowTree(str(at), direction, _render(f"{at}  {file.name}", kids), [])
    if start is None:
        raise LookupError(f"no function {at!r} in {file} (names look like 'fn' or 'Class.method')")

    forward: dict[str, list[tuple[str, int, bool]]] = {}
    edges: list[tuple[str, str, str]] = []
    for owner, targets in calls.items():
        seen: set[str] = set()
        for target, line in targets:
            callee = resolve(target, owner)
            if callee is None or callee == owner or callee in seen:
                continue
            seen.add(callee)
            forward.setdefault(owner, []).append((callee, line, target.startswith("&")))
            edges.append((owner, callee, f"{file.name}:{line}"))
    backward: dict[str, list[tuple[str, int, bool]]] = {}
    for owner, targets in forward.items():
        for callee, line, by_ref in targets:
            backward.setdefault(callee, []).append((owner, line, by_ref))
    links = forward if direction == "outgoing" else backward
    expanded = {start}

    def children(name: str, level: int, trail: frozenset) -> list:
        result = []
        for other, line, by_ref in links.get(name, [])[:_MAX_CHILDREN]:
            label = f"{other}  :{nodes[other]}  (at :{line}{', by reference' if by_ref else ''})"
            if other in trail:
                result.append((label + "  ↺", []))
            elif level >= depth:
                result.append((label, []))
            elif other in expanded:
                result.append((label + "  (expanded above)", []))
            else:
                expanded.add(other)
                result.append((label, children(other, level + 1, trail | {name})))
        return result

    arrow = "calls" if direction == "outgoing" else "is called by"
    title = f"{start}  {file.name}:{nodes[start]}   [{arrow}, depth {depth}, acorn]"
    return FlowTree(start, direction, _render(title, children(start, 1, frozenset({start}))), edges)
