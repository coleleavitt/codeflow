"""A stand-in for ``rlm.lsp`` outside the prime-agent kernel (the ``codeflow`` CLI and MCP server).

Inside the kernel, ``rlm.lsp`` asks the host's language servers. Run on its own, codeflow talks to the
servers itself: this module starts the one configured for a file's extension (the ``lspServers`` key of
``~/.prime/agent/settings.json``, or ``$CODEFLOW_LSP_SETTINGS``; else a few defaults found on ``PATH``),
rooted at the file's git root, over JSON-RPC on stdio, and answers ``definition``, ``references``,
``hover`` and ``call_hierarchy`` with the same ``LspResult`` shape (1-based lines and columns, items as
dicts with ``path``, ``line``, ``character``; call items with ``name``, ``kind`` and ``ranges``).
A server stays up for the life of the process (the MCP server's, or one CLI call); ``shutdown()``
stops them all.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

_DIRECTIONS = ("incoming", "outgoing", "prepare")
_CONTENT_MODIFIED = -32801
_DEFAULT_TIMEOUT_S = 60.0
_DEFAULT_SERVERS: dict[str, dict[str, Any]] = {
    "rust": {"command": "rust-analyzer", "extensionToLanguage": {".rs": "rust"}},
    "python": {"command": "pyright-langserver", "args": ["--stdio"], "extensionToLanguage": {".py": "python", ".pyi": "python"}},
    "c": {"command": "clangd", "args": ["--background-index"],
          "extensionToLanguage": {".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp"}},
    "typescript": {"command": "typescript-language-server", "args": ["--stdio"],
                   "extensionToLanguage": {".ts": "typescript", ".tsx": "typescriptreact", ".mts": "typescript", ".cts": "typescript",
                                           ".js": "javascript", ".jsx": "javascriptreact", ".mjs": "javascript", ".cjs": "javascript"}},
    "go": {"command": "gopls", "args": ["serve"], "extensionToLanguage": {".go": "go"}},
}
_SYMBOL_KINDS = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class", 6: "Method", 7: "Property", 8: "Field",
    9: "Constructor", 10: "Enum", 11: "Interface", 12: "Function", 13: "Variable", 14: "Constant", 15: "String",
    16: "Number", 17: "Boolean", 18: "Array", 19: "Object", 20: "Key", 21: "Null", 22: "EnumMember", 23: "Struct",
    24: "Event", 25: "Operator", 26: "TypeParameter",
}


@dataclass(frozen=True)
class LspResult:
    """One LSP answer: ``text`` is what to read, ``items`` the results as data (as ``rlm.lsp``'s)."""

    operation: str
    text: str
    path: str | None = None
    server: str | None = None
    available: bool = True
    items: list[dict[str, Any]] = field(default_factory=list)
    result_count: int = 0
    file_count: int = 0

    def __str__(self) -> str:
        return self.text


class LspError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def configured_servers() -> dict[str, dict[str, Any]]:
    """The ``lspServers`` map from the settings file, else the defaults."""
    settings = Path(os.environ.get("CODEFLOW_LSP_SETTINGS") or Path.home() / ".prime" / "agent" / "settings.json")
    try:
        servers = json.loads(settings.read_text()).get("lspServers")
    except (OSError, ValueError, AttributeError):
        servers = None
    return servers if isinstance(servers, dict) and servers else _DEFAULT_SERVERS


def server_for(suffix: str, servers: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any], str] | None:
    """The (name, config, language id) of the server that handles ``suffix``."""
    for name, config in servers.items():
        languages = config.get("extensionToLanguage") or {}
        if isinstance(languages, dict) and suffix in languages:
            return name, config, str(languages[suffix])
    return None


def path_of(uri: str) -> str:
    return unquote(urlparse(uri).path)


def _git_root(file: Path) -> Path:
    try:
        top = subprocess.run(["git", "-C", str(file.parent), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        return Path(top)
    except (OSError, subprocess.SubprocessError):
        return file.parent


def _section(settings: Any, section: str | None) -> Any:
    if not section:
        return settings
    value = settings
    for part in section.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


class _Server:
    def __init__(self, name: str, config: dict[str, Any], root: Path):
        self.name, self.config, self.root = name, config, root
        self.settings = config.get("settings") or {}
        self.timeout = float(config.get("requestTimeout") or _DEFAULT_TIMEOUT_S * 1000) / 1000
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.opened: dict[str, tuple[float, int]] = {}
        self.proc: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task | None = None

    async def start(self) -> None:
        command = str(self.config["command"])
        if shutil.which(command) is None:
            raise FileNotFoundError(f"language server `{command}` ({self.name}) is not installed or not on PATH")
        env = {**os.environ, **{str(k): str(v) for k, v in (self.config.get("env") or {}).items()}}
        self.proc = await asyncio.create_subprocess_exec(
            command, *map(str, self.config.get("args") or []),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=self.root, env=env,
        )
        self.reader = asyncio.create_task(self._read())
        options = self.config.get("initializationOptions")
        if options is None and isinstance(self.settings, dict) and len(self.settings) == 1:
            options = next(iter(self.settings.values()))  # rust-analyzer reads its settings from here
        startup = float(self.config.get("startupTimeout") or 60_000) / 1000
        await self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": self.root.as_uri(),
            "rootPath": str(self.root),
            "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
            "clientInfo": {"name": "codeflow", "version": "0.1.0"},
            "initializationOptions": {} if options is None else options,
            "capabilities": {
                # As pa-lsp: a server with no settings is not told it may ask for them (basedpyright then
                # waits on its own configuration request before it answers anything).
                "workspace": {"configuration": bool(self.settings), "workspaceFolders": False},
                "window": {"workDoneProgress": True},
                "general": {"positionEncodings": ["utf-16"]},
                "textDocument": {
                    "synchronization": {"didSave": False},
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "definition": {"linkSupport": True},
                    "references": {},
                    "callHierarchy": {},
                },
            },
        }, timeout=startup)
        await self.notify("initialized", {})
        if self.settings:
            await self.notify("workspace/didChangeConfiguration", {"settings": self.settings})

    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _send(self, message: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        body = json.dumps(message).encode()
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        await self.proc.stdin.drain()

    async def notify(self, method: str, params: Any) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method: str, params: Any, timeout: float | None = None) -> Any:
        self.next_id += 1
        ident = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        await self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout or self.timeout)
        finally:
            self.pending.pop(ident, None)

    async def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        stream = self.proc.stdout
        try:
            while True:
                length = None
                while True:
                    header = await stream.readline()
                    if not header:
                        raise EOFError
                    header = header.strip()
                    if not header:
                        break
                    name, _, value = header.decode("ascii", "replace").partition(":")
                    if name.strip().lower() == "content-length":
                        length = int(value.strip())
                if length is None:
                    continue
                await self._dispatch(json.loads(await stream.readexactly(length)))
        except (EOFError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(LspError(-32099, f"{self.name} language server exited"))

    async def _dispatch(self, message: dict) -> None:
        if "id" in message and "method" not in message:
            future = self.pending.get(message["id"])
            if future is None or future.done():
                return
            if "error" in message:
                error = message["error"] or {}
                future.set_exception(LspError(int(error.get("code", 0)), str(error.get("message", "error"))))
            else:
                future.set_result(message.get("result"))
        elif "id" in message:  # a request from the server
            result: Any = None
            if message["method"] == "workspace/configuration":
                result = [_section(self.settings, item.get("section")) for item in (message.get("params") or {}).get("items", [])]
            elif message["method"] == "workspace/workspaceFolders":
                result = [{"uri": self.root.as_uri(), "name": self.root.name}]
            await self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    async def open(self, file: Path, language: str) -> None:
        """Tell the server the file's current text (opened once, re-sent when it changed on disk)."""
        key = file.as_uri()
        mtime = file.stat().st_mtime
        known = self.opened.get(key)
        if known and known[0] == mtime:
            return
        text = file.read_text(errors="replace")
        if known is None:
            self.opened[key] = (mtime, 1)
            await self.notify("textDocument/didOpen", {"textDocument": {"uri": key, "languageId": language, "version": 1, "text": text}})
        else:
            version = known[1] + 1
            self.opened[key] = (mtime, version)
            await self.notify("textDocument/didChange", {"textDocument": {"uri": key, "version": version}, "contentChanges": [{"text": text}]})

    async def stop(self) -> None:
        if not self.alive():
            return
        try:
            await self.request("shutdown", None, timeout=3)
            await self.notify("exit", None)
            await asyncio.wait_for(self.proc.wait(), 3)
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 - a stuck server is killed below
            pass
        if self.alive():
            self.proc.kill()
            await self.proc.wait()
        if self.reader is not None:
            self.reader.cancel()


_SERVERS: dict[tuple[str, str], _Server] = {}
_FAILED: dict[tuple[str, str], str] = {}


def _project_root(file: Path) -> Path:
    """The directory a language server opens for ``file``. Rust: the outermost Cargo workspace (a
    ``Cargo.toml`` with ``[workspace]``) between the file and the git root, else the file's crate;
    the git root for everything else. A repository whose workspace sits in a subdirectory (kuna keeps
    it in ``decompiler/``) would otherwise open with no crate, and rust-analyzer returns no items."""
    top = _git_root(file)
    if file.suffix != ".rs":
        return top
    crate = None
    for directory in file.parents:
        if not directory.is_relative_to(top):
            break
        manifest = directory / "Cargo.toml"
        if not manifest.is_file():
            continue
        if crate is None:
            crate = directory
        if "[workspace]" in manifest.read_text(errors="replace"):
            return directory
        if directory == top:
            break
    return crate or top


async def _server(file: Path) -> tuple[_Server | None, str, str]:
    """The running server for ``file`` (started on first use), its language id, or why there is none."""
    found = server_for(file.suffix, configured_servers())
    if found is None:
        return None, "", f"no language server is configured for {file.suffix} files"
    name, config, language = found
    key = (name, str(_project_root(file)))
    if key in _FAILED:
        return None, language, _FAILED[key]
    server = _SERVERS.get(key)
    if server is not None and server.alive():
        return server, language, ""
    server = _Server(name, config, Path(key[1]))
    try:
        await server.start()
    except (OSError, LspError, asyncio.TimeoutError) as error:
        await server.stop()
        _FAILED[key] = f"{name} language server could not start: {error or type(error).__name__}"
        return None, language, _FAILED[key]
    _SERVERS[key] = server
    return server, language, ""


async def _ask(operation: str, path: Any, method: str, params_at) -> tuple[Any, LspResult | None, _Server | None]:
    """Run ``method`` at a file position; an unavailable server or a server error is a result."""
    file = Path(os.path.abspath(os.fspath(path)))
    server, language, why = await _server(file)
    if server is None:
        return None, LspResult(operation, why, str(file), None, available=False), None
    await server.open(file, language)
    for attempt in range(5):
        try:
            return await server.request(method, params_at(file.as_uri())), None, server
        except LspError as error:
            if error.code == _CONTENT_MODIFIED and attempt < 4:
                await asyncio.sleep(1)
                continue
            return None, LspResult(operation, f"{server.name}: {error}", str(file), server.name), server
        except asyncio.TimeoutError:
            return None, LspResult(operation, f"{server.name}: {method} timed out", str(file), server.name), server
    raise AssertionError("unreachable")


def _position(uri: str, line: int, col: int) -> dict:
    return {"textDocument": {"uri": uri}, "position": {"line": int(line) - 1, "character": int(col) - 1}}


def location_items(result: Any) -> list[dict[str, Any]]:
    """``Location``, ``Location[]`` or ``LocationLink[]`` as items."""
    if result is None:
        return []
    items = []
    for location in result if isinstance(result, list) else [result]:
        uri = location.get("uri") or location.get("targetUri")
        span = location.get("range") or location.get("targetSelectionRange") or location.get("targetRange")
        if not uri or not span:
            continue
        items.append({
            "path": path_of(uri),
            "line": span["start"]["line"] + 1,
            "character": span["start"]["character"] + 1,
            "end_line": span["end"]["line"] + 1,
            "end_character": span["end"]["character"] + 1,
        })
    return items


def call_item(item: dict) -> dict[str, Any]:
    span = item.get("range") or item.get("selectionRange")
    return {
        "name": item.get("name", ""),
        "kind": _SYMBOL_KINDS.get(item.get("kind"), ""),
        "detail": item.get("detail"),
        "path": path_of(item["uri"]),
        "line": span["start"]["line"] + 1,
        "character": span["start"]["character"] + 1,
    }


def hover_text(result: Any) -> str:
    if not result:
        return ""
    contents = result.get("contents") if isinstance(result, dict) else result
    parts = contents if isinstance(contents, list) else [contents]
    texts = []
    for part in parts:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            texts.append(str(part.get("value", "")))
    return "\n".join(t for t in texts if t)


def _listing(items: list[dict[str, Any]]) -> str:
    return "\n".join(f"{i.get('name', '')}  {i['path']}:{i['line']}:{i['character']}".strip() for i in items)


def _result(operation: str, path: Any, server: _Server, items: list[dict[str, Any]], text: str | None = None) -> LspResult:
    return LspResult(operation, _listing(items) if text is None else text, os.fspath(path), server.name, True, items,
                     len(items), len({i.get("path") for i in items}))


async def definition(path: str, line: int, col: int) -> LspResult:
    """Where the symbol at ``path:line:col`` is defined."""
    raw, failed, server = await _ask("definition", path, "textDocument/definition", lambda uri: _position(uri, line, col))
    return failed or _result("definition", path, server, location_items(raw))


async def references(path: str, line: int, col: int, *, include_declaration: bool = True) -> LspResult:
    """Every reference to the symbol at ``path:line:col``."""
    raw, failed, server = await _ask("references", path, "textDocument/references", lambda uri: {
        **_position(uri, line, col), "context": {"includeDeclaration": bool(include_declaration)}})
    return failed or _result("references", path, server, location_items(raw))


async def hover(path: str, line: int, col: int) -> LspResult:
    """The server's hover text for the symbol at ``path:line:col``."""
    raw, failed, server = await _ask("hover", path, "textDocument/hover", lambda uri: _position(uri, line, col))
    return failed or _result("hover", path, server, [], hover_text(raw))


CONTENT_MODIFIED = -32801  # LSP: the server changed its index mid-request; the spec says ask again


async def _settled(server: Any, method: str, params: Any, attempts: int = 6, pause: float = 2.0) -> Any:
    """``server.request``, asked again while the server reports its content modified (it is still
    indexing); any other error, or one that persists, is raised."""
    for attempt in range(attempts):
        try:
            return await server.request(method, params)
        except LspError as error:
            if error.code != CONTENT_MODIFIED or attempt == attempts - 1:
                raise
            await asyncio.sleep(pause)
    raise AssertionError("unreachable")


async def call_hierarchy(path: str, line: int, col: int, direction: str = "incoming") -> LspResult:
    """The callers (``incoming``) or callees (``outgoing``) of the function at ``path:line:col``,
    or just its call hierarchy item (``prepare``)."""
    if direction not in _DIRECTIONS:
        raise ValueError(f"direction must be one of {', '.join(_DIRECTIONS)}")
    operation = f"call_hierarchy.{direction}"
    raw, failed, server = await _ask(operation, path, "textDocument/prepareCallHierarchy", lambda uri: _position(uri, line, col))
    if failed:
        return failed
    if not raw:
        return _result(operation, path, server, [], "No call hierarchy item found at this position")
    if direction == "prepare":
        return _result(operation, path, server, [call_item(item) for item in raw])
    method, end = ("callHierarchy/incomingCalls", "from") if direction == "incoming" else ("callHierarchy/outgoingCalls", "to")
    try:
        calls = await _settled(server, method, {"item": raw[0]}) or []
    except LspError as error:
        return LspResult(operation, f"{server.name}: {error}", os.fspath(path), server.name)
    except asyncio.TimeoutError:
        return LspResult(operation, f"{server.name}: {method} timed out", os.fspath(path), server.name)
    items = []
    for call in calls:
        item = call_item(call[end])
        item["ranges"] = [{"line": r["start"]["line"] + 1, "character": r["start"]["character"] + 1} for r in call.get("fromRanges", [])]
        items.append(item)
    return _result(operation, path, server, items)


async def shutdown() -> None:
    """Stop every server this process started."""
    servers = list(_SERVERS.values())
    _SERVERS.clear()
    await asyncio.gather(*(server.stop() for server in servers), return_exceptions=True)
