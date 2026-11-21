"""codeflow outside the prime-agent kernel: a command line and a stdio MCP server.

    codeflow cfg FILE [FUNCTION] [--callers N] [--callees N] [--follow N] [--var NAME] [--refs] [--dominators] [--json]
    codeflow cfg SYMBOL [...]            # a name: its definition is looked up in the project (cwd or --root)
    codeflow mcp                         # JSON-RPC 2.0 over stdin/stdout: codeflow_cfg, codeflow_callers, codeflow_callees

Language-server lookups (callers, callees, refs, never-returning calls) go through ``_lsp_client``, which
starts the servers itself, in place of the kernel's ``rlm.lsp``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Awaitable, Callable

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_VERSION = "0.1.0"


def use_standalone_lsp() -> None:
    """Answer ``from rlm import lsp`` with the standalone client: outside the kernel there is no host."""
    from . import _lsp_client

    module = types.ModuleType("rlm")
    module.lsp = _lsp_client  # type: ignore[attr-defined]
    module.__path__ = []  # type: ignore[attr-defined]
    sys.modules["rlm"] = module
    sys.modules["rlm.lsp"] = _lsp_client
    import codeflow

    # The servers are rooted at each file's git root, so no file is outside "the session's workspace".
    codeflow._SESSION_ROOT = Path("/")


# ---- arguments ---------------------------------------------------------------


def function_arg(value: str | int | None) -> str | int | None:
    """A name stays a name; ``"120"`` is a line; ``"120:9"`` stays a string (line and column)."""
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value


def find_symbol(name: str, root: Path) -> Path:
    """The one project file under ``root`` that defines function ``name`` (``Type::f``, ``Class.f`` too)."""
    import codeflow

    parts = re.split(r"::|\.", name)
    short, owner = parts[-1], (parts[-2] if len(parts) > 1 else None)
    patterns = {suffix: re.compile(pattern.format(name=re.escape(short)), re.M) for suffix, pattern in codeflow._DEFINITIONS.items()}
    patterns.update({suffix: re.compile(rf"^[\w\s\*&:<>,]*\b{re.escape(short)}\s*\([^;]*$", re.M) for suffix in codeflow._C})
    try:
        listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"], capture_output=True, text=True,
                                timeout=30, check=True).stdout.split("\0")
    except (OSError, subprocess.SubprocessError) as error:
        raise LookupError(f"`{name}` is not a file, and {root} is not a git checkout to search for it ({error})") from None
    hits: list[tuple[Path, int]] = []
    for relative in listed[: codeflow._SEARCH_FILES]:
        suffix = Path(relative).suffix
        if suffix not in patterns or codeflow._VENDORED & set(Path(relative).parts[:-1]):
            continue
        try:
            text = (root / relative).read_text(errors="replace")
        except OSError:
            continue
        if short not in text or (owner and not re.search(rf"\b{re.escape(owner)}\b", text)):
            continue
        hits.extend((root / relative, text.count("\n", 0, m.start()) + 1) for m in patterns[suffix].finditer(text))
    files = sorted({path for path, _ in hits})
    if len(files) == 1:
        return files[0]
    if not files:
        raise LookupError(f"`{name}` is not a file, and no definition of it was found under {root}: pass the file")
    places = ", ".join(f"{path.relative_to(root)}:{line}" for path, line in sorted(hits)[:10])
    raise LookupError(f"`{name}` is defined in several files ({places}): pass the file")


def resolve_target(target: str, function: str | int | None, root: str | None) -> tuple[Path, str | int]:
    """``target`` is a file (``function`` names what is in it) or, with no such file, the symbol itself."""
    path = Path(target).expanduser()
    if path.is_file():
        if function is None:
            raise ValueError(f"{target} is a file: name the function (or pass a line)")
        return path.resolve(), function
    if function is not None:
        raise FileNotFoundError(f"no such file: {target}")
    import codeflow

    base = Path(root).expanduser().resolve() if root else codeflow._root(Path.cwd() / "_", None)
    return find_symbol(target, base), target


def cfg_kwargs(options: dict[str, Any]) -> dict[str, Any]:
    """``codeflow.cfg``'s keyword arguments from the CLI's or the MCP tool's options (absent ones left at their defaults)."""
    names = ("callers", "callees", "follow", "follow_limit", "var", "refs", "dominators", "level", "cleanup", "root")
    return {name: options[name] for name in names if options.get(name) not in (None, "")}


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="codeflow", description="Control flow, callers, callees, def-use and references with file:line.")
    commands = top.add_subparsers(dest="command", required=True)
    cfg = commands.add_parser("cfg", help="every path through a function, plus callers/callees/var/refs/dominators")
    cfg.add_argument("target", help="a source file, or a function name to look up in the project")
    cfg.add_argument("function", nargs="?", help="the function in the file: a name (Type::method, Class.method), a 1-based line or LINE:COL")
    cfg.add_argument("--function", dest="function_flag", metavar="NAME", help="same as the second positional argument")
    cfg.add_argument("--callers", type=int, metavar="N", help="who calls it, N levels")
    cfg.add_argument("--callees", type=int, metavar="N", help="what it calls, N levels")
    cfg.add_argument("--follow", type=int, metavar="N", help="nest each project callee's own graph, N levels")
    cfg.add_argument("--follow-limit", dest="follow_limit", type=int, metavar="N", help="at most N followed callees per function (20)")
    cfg.add_argument("--var", metavar="NAME", help="def-use of NAME (self.x / a field path work): reaching definitions at each read")
    cfg.add_argument("--refs", action="store_true", default=None, help="every site naming the symbol across the project, by kind")
    cfg.add_argument("--dominators", action="store_true", default=None, help="each block's dominators")
    cfg.add_argument("--level", choices=("source", "async", "mir"), help="Rust graph level (source)")
    cfg.add_argument("--cleanup", action="store_true", default=None, help="add unwind/cleanup paths")
    cfg.add_argument("--root", metavar="DIR", help="the project root (default: the file's git root)")
    cfg.add_argument("--json", action="store_true", help="print {function, text, graph} as JSON")
    cfg.add_argument("--out", metavar="FILE", help="write the whole output to FILE (text, or JSON with --json) and print a summary")
    cfg.add_argument("--blocks", metavar="SPEC", help="only these blocks: 12-30, 3,5,9, or both")
    cfg.add_argument("--path-from", dest="path_from", metavar="B", type=int, help="the paths from block B ...")
    cfg.add_argument("--path-to", dest="path_to", metavar="B", type=int, help="... to block B (at most 50, shortest first)")
    query = commands.add_parser("query", help="read-only SQL over a project's SCIP index (Rust): tables occ, names")
    query.add_argument("sql", help="one SELECT (or WITH ... SELECT) statement")
    query.add_argument("--root", metavar="DIR", help="a directory of the project (default: the current one)")
    query.add_argument("--limit", type=int, default=200, metavar="N", help="at most N rows (200)")
    query.add_argument("--json", action="store_true", help="print the rows as a JSON list")
    commands.add_parser("mcp", help="serve the codeflow tools over MCP (stdio)")
    return top


async def run_cfg(target: str, function: str | int | None, options: dict[str, Any]):
    import codeflow

    path, function = resolve_target(target, function_arg(function), options.get("root"))
    return await codeflow.cfg(path, function, **cfg_kwargs(options))


async def _with_servers(work: Awaitable):
    from . import _lsp_client

    try:
        return await work
    finally:
        await _lsp_client.shutdown()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    use_standalone_lsp()
    if args.command == "mcp":
        asyncio.run(serve())
        return 0
    if args.command == "query":
        return run_query(args)
    if args.function is not None and args.function_flag is not None:
        parser().error("give the function once: positional or --function")
    function = args.function if args.function is not None else args.function_flag
    try:
        result = asyncio.run(_with_servers(run_cfg(args.target, function, vars(args))))
        graph_text, graph = apply_views(args, result)
    except (LookupError, ValueError, FileNotFoundError, RuntimeError) as error:
        print(f"codeflow: {error}", file=sys.stderr)
        return 1
    body = (json.dumps({"function": result.function, "text": graph_text, "graph": graph}, indent=1, default=str)
            if args.json else graph_text)
    if args.out:
        Path(args.out).write_text(body + "\n")
        print(f"wrote {len(body.splitlines())} lines to {args.out} ({result.function})")
    else:
        print(body)
    return 0


def apply_views(args: argparse.Namespace, result) -> tuple[str, dict]:
    """The text and graph to print: the whole result, or the --blocks range or the --path-from/--path-to paths."""
    from . import _cfg, _views

    if args.blocks is None and args.path_from is None and args.path_to is None:
        return result.text, result.graph
    graph = result.graph
    if args.path_from is not None or args.path_to is not None:
        if args.path_from is None or args.path_to is None:
            raise ValueError("--path-from and --path-to go together")
        found, more = _views.paths(graph, args.path_from, args.path_to)
        return _views.render_paths(found, more, args.path_from, args.path_to), {"paths": found, "more": more}
    sub = _views.subgraph(graph, _views.parse_blocks(args.blocks))
    return _cfg.render(sub, Path.cwd()), sub


def run_query(args: argparse.Namespace) -> int:
    """Read-only SQL over the project's SCIP index, rows capped at --limit."""
    from . import _lsp_client, _scip, _scip_db

    if args.limit < 1:
        print("codeflow: --limit takes at least 1", file=sys.stderr)
        return 1
    statement = args.sql.strip().rstrip(";").strip()
    if ";" in statement or not re.match(r"(?is)^(select|with)\b", statement):
        print("codeflow: query takes one SELECT (or WITH ... SELECT) statement", file=sys.stderr)
        return 1
    # A directory has no suffix, so ask for a file inside it: the workspace (Cargo.toml) above it is found.
    root = _lsp_client._project_root(Path(args.root or ".").expanduser().resolve() / "query.rs")
    db_path = _scip.cached(root)
    if db_path is None:
        print(f"codeflow: no current SCIP index for {root}; a Rust cfg query there builds it", file=sys.stderr)
        return 1
    index = _scip_db.SqlIndex(db_path)
    try:
        cursor = index.db.execute(statement)
        columns = [d[0] for d in cursor.description or []]
        rows = cursor.fetchmany(args.limit + 1)
    except sqlite3.Error as error:
        print(f"codeflow: {error}", file=sys.stderr)
        return 1
    finally:
        index.close()
    more = len(rows) > args.limit
    rows = rows[: args.limit]
    if args.json:
        print(json.dumps([dict(zip(columns, tuple(r))) for r in rows], indent=1, default=str))
    else:
        print("\t".join(columns))
        for row in rows:
            print("\t".join("" if v is None else str(v) for v in tuple(row)))
        if more:
            print(f"… more rows (raise --limit above {args.limit})")
    return 0


# ---- MCP ---------------------------------------------------------------------

_LOCATION = {
    "path": {"type": "string", "description": "A source file (absolute or relative to the server's cwd), or a function name to look up in the project when `function` is omitted."},
    "function": {"type": ["string", "integer"], "description": "The function in `path`: a name (Type::method, Class.method), a 1-based line, or \"LINE:COL\"."},
    "root": {"type": "string", "description": "The project root (default: the file's git root)."},
}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "codeflow_cfg",
        "description": (
            "Every path through a function (branches, loops, early returns, ?/raise, never-returning calls) from the code's real "
            "structure: rustc MIR for Rust, Python ast, acorn for JS/TS, LLVM IR for C/C++, with tree-sitter fallbacks; each block "
            "with file:line. callers=N / callees=N add who calls it and what it calls (language servers), follow=N nests each "
            "callee's graph, var=NAME adds def-use with reaching definitions, refs=true lists every site naming the symbol by kind "
            "(works for fields, constants, types), dominators=true adds dominators. Use instead of grep for structural questions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_LOCATION,
                "callers": {"type": "integer", "minimum": 0, "description": "Who calls it, N levels."},
                "callees": {"type": "integer", "minimum": 0, "description": "What it calls, N levels."},
                "follow": {"type": "integer", "minimum": 0, "description": "Nest each project callee's graph, N levels."},
                "follow_limit": {"type": "integer", "minimum": 1, "description": "At most this many followed callees per function (20)."},
                "var": {"type": "string", "description": "Def-use of this variable or field path (self.x)."},
                "refs": {"type": "boolean", "description": "Every site naming the symbol across the project, grouped by kind."},
                "dominators": {"type": "boolean", "description": "Each block's dominators."},
                "level": {"type": "string", "enum": ["source", "async", "mir"], "description": "Rust graph level."},
                "cleanup": {"type": "boolean", "description": "Add unwind/cleanup paths."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "codeflow_callers",
        "description": "Who calls a function, as a tree with each call site's file:line (language servers; acorn for JS bundles). No graph is built, so it is cheaper than codeflow_cfg with callers=N.",
        "inputSchema": {"type": "object", "properties": {**_LOCATION, "depth": {"type": "integer", "minimum": 1, "description": "Levels (2)."}}, "required": ["path"]},
    },
    {
        "name": "codeflow_callees",
        "description": "What a function calls, as a tree with each call site's file:line (language servers; acorn for JS bundles). No graph is built, so it is cheaper than codeflow_cfg with callees=N.",
        "inputSchema": {"type": "object", "properties": {**_LOCATION, "depth": {"type": "integer", "minimum": 1, "description": "Levels (2)."}}, "required": ["path"]},
    },
]


async def call_tool(name: str, arguments: dict[str, Any]) -> str:
    """Run one tool; its text is the answer."""
    import codeflow

    if "path" not in arguments or not isinstance(arguments["path"], str):
        raise ValueError("`path` is required")
    if name == "codeflow_cfg":
        return (await run_cfg(arguments["path"], arguments.get("function"), arguments)).text
    if name in ("codeflow_callers", "codeflow_callees"):
        path, function = resolve_target(arguments["path"], function_arg(arguments.get("function")), arguments.get("root"))
        direction = "incoming" if name == "codeflow_callers" else "outgoing"
        tree = await codeflow.tree(path, function, direction=direction, depth=int(arguments.get("depth") or 2), root=arguments.get("root"))
        return tree.text
    raise KeyError(name)


def _error(ident: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


async def handle(message: Any, tool: Callable[[str, dict], Awaitable[str]] = call_tool) -> dict | None:
    """One JSON-RPC message in, its response out (None for a notification)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        ident = message.get("id") if isinstance(message, dict) else None
        return _error(ident, -32600, "invalid request")
    if "id" not in message:
        return None  # notifications (initialized, cancelled, ...) need no answer
    ident, method, params = message["id"], message["method"], message.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": ident, "result": {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "codeflow", "version": SERVER_VERSION},
            "instructions": "codeflow_cfg answers structural questions about code (control flow, callers, callees, def-use, references) with file:line; use it instead of grep for them.",
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": ident, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": ident, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments") or {}
        if name not in {t["name"] for t in TOOLS}:
            return _error(ident, -32602, f"unknown tool: {name}")
        try:
            text, failed = await tool(name, arguments), False
        except Exception as error:  # noqa: BLE001 - a tool failure is the tool's answer, not a protocol error
            text, failed = f"{type(error).__name__}: {error}", True
        return {"jsonrpc": "2.0", "id": ident, "result": {"content": [{"type": "text", "text": text}], "isError": failed}}
    return _error(ident, -32601, f"method not found: {method}")


async def serve(read_line: Callable[[], Awaitable[bytes]] | None = None, write=None) -> None:
    """Newline-delimited JSON-RPC on stdin/stdout until stdin closes. Anything codeflow prints goes to stderr."""
    from . import _lsp_client

    out = write or sys.stdout.buffer
    stdout, sys.stdout = sys.stdout, sys.stderr
    if read_line is None:
        loop = asyncio.get_running_loop()

        async def read_line() -> bytes:
            return await loop.run_in_executor(None, sys.stdin.buffer.readline)

    try:
        while True:
            line = await read_line()
            if not line:
                break
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                response = _error(None, -32700, "parse error")
            else:
                response = await handle(message)
            if response is not None:
                out.write(json.dumps(response).encode() + b"\n")
                out.flush()
    finally:
        sys.stdout = stdout
        await _lsp_client.shutdown()


if __name__ == "__main__":
    sys.exit(main())
