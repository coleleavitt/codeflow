"""A workspace's SCIP index (``rust-analyzer scip``) read with the standard library alone, and the
callers, callees and references it answers offline.

The export is rust-analyzer's whole-workspace index: every definition and reference occurrence with
its symbol, and for definitions the ``enclosing_range`` of the body. A caller of ``f`` is the callable
whose body encloses a reference to ``f``; a callee of ``f`` is a callable referenced inside ``f``'s
body. Both are read from the occurrences, so a lookup needs no language server, only the file.

Cached per project under the user's cache directory (``~/.cache/codeflow/scip/<hash of the project>``)
and keyed by a fingerprint of the sources (``fingerprint``): an unchanged project never re-exports, and
a changed one re-exports once, detached from the query that noticed.

Field numbers follow sourcegraph/scip ``scip.proto``: Index.documents=2; Document.relative_path=1,
occurrences=2, symbols=3; Occurrence.range=1, symbol=2, symbol_roles=3, enclosing_range=7;
SymbolInformation.symbol=1, display_name=6, enclosing_symbol=8.
"""

from __future__ import annotations

import bisect
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFINITION = 0x1
_CALLABLE_SUFFIX = ")."  # rust-analyzer descriptors of functions and methods end in "()." (or "(...)."


class ScipError(RuntimeError):
    """The index could not be read or made."""


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data):
            raise ScipError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _fields(data: bytes) -> Any:
    """(field number, wire type, value) for each field of one message; a varint is an int, the others bytes."""
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 1:
            value, pos = data[pos:pos + 8], pos + 8
        elif wire == 2:
            size, pos = _varint(data, pos)
            value, pos = data[pos:pos + size], pos + size
        elif wire == 5:
            value, pos = data[pos:pos + 4], pos + 4
        else:
            raise ScipError(f"unsupported wire type {wire}")
        yield number, wire, value


def _ints(value: Any) -> list[int]:
    """A packed repeated int32 (wire type 2) or one unpacked varint."""
    if isinstance(value, int):
        return [value]
    out: list[int] = []
    pos = 0
    while pos < len(value):
        item, pos = _varint(value, pos)
        out.append(item)
    return out


def _text(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


@dataclass
class Occurrence:
    line: int  # 0-based
    col: int  # 0-based
    end_col: int
    symbol: str
    roles: int
    enclosing: tuple[int, int, int, int] | None  # (start line, start col, end line, end col)

    @property
    def is_definition(self) -> bool:
        return bool(self.roles & DEFINITION)


@dataclass
class Document:
    path: str
    occurrences: list[Occurrence] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)  # symbol -> display name
    starts: list[int] = field(default_factory=list)  # sorted occurrence start lines, for lookups
    ordered: list[Occurrence] = field(default_factory=list)


def _range(values: list[int]) -> tuple[int, int, int]:
    if len(values) == 3:
        return values[0], values[1], values[2]
    if len(values) == 4:
        return values[0], values[1], values[3]
    raise ScipError(f"occurrence range has {len(values)} values")


def _occurrence(data: bytes) -> Occurrence:
    rng: list[int] = []
    symbol, roles, enclosing = "", 0, None
    for number, _, value in _fields(data):
        if number == 1:
            rng.extend(_ints(value))
        elif number == 2:
            symbol = _text(value)
        elif number == 3:
            roles = value
        elif number == 7:
            values = _ints(value)
            if len(values) in (3, 4):
                s_line, s_col, s_end = _range(values)
                e_line = values[2] if len(values) == 4 else s_line
                e_col = values[3] if len(values) == 4 else s_end
                enclosing = (s_line, s_col, e_line, e_col)
    line, col, end_col = _range(rng)
    return Occurrence(line, col, end_col, symbol, roles, enclosing)


def _document(data: bytes) -> Document:
    path, occurrences, names = "", [], {}
    for number, _, value in _fields(data):
        if number == 1:
            path = _text(value)
        elif number == 2:
            occurrences.append(_occurrence(value))
        elif number == 3:
            symbol, display = "", ""
            for n, _, v in _fields(value):
                if n == 1:
                    symbol = _text(v)
                elif n == 6:
                    display = _text(v)
            if symbol and display:
                names[symbol] = display
    doc = Document(path, occurrences, names)
    doc.ordered = sorted(occurrences, key=lambda o: (o.line, o.col))
    doc.starts = [o.line for o in doc.ordered]
    return doc


@dataclass
class Index:
    documents: dict[str, Document]
    # Every definition of a symbol: (path, occurrence).
    definitions: dict[str, tuple[str, Occurrence]] = field(default_factory=dict)
    # Every reference of a symbol (definitions included): (path, occurrence).
    references: dict[str, list[tuple[str, Occurrence]]] = field(default_factory=dict)

    @classmethod
    def parse(cls, data: bytes) -> "Index":
        documents: dict[str, Document] = {}
        for number, _, value in _fields(data):
            if number == 2:
                doc = _document(value)
                documents[doc.path] = doc
        index = cls(documents)
        for path, doc in documents.items():
            for occ in doc.occurrences:
                if not occ.symbol:
                    continue
                index.references.setdefault(occ.symbol, []).append((path, occ))
                if occ.is_definition:
                    index.definitions.setdefault(occ.symbol, (path, occ))
        return index

    @classmethod
    def load(cls, path: Path) -> "Index":
        return cls.parse(Path(path).read_bytes())

    def symbol_at(self, path: str, line: int, col: int) -> str | None:
        """The symbol of the occurrence covering (line, col), 0-based."""
        doc = self.documents.get(path)
        if doc is None:
            return None
        hi = bisect.bisect_right(doc.starts, line)
        for occ in reversed(doc.ordered[:hi]):
            if occ.line == line and occ.col <= col < occ.end_col and occ.symbol:
                return occ.symbol
        return None

    def name(self, symbol: str) -> str:
        for doc in self.documents.values():
            if symbol in doc.names:
                return doc.names[symbol]
        return _descriptor_name(symbol)

    def _enclosing_callable(self, path: str, line: int, col: int) -> tuple[str, Occurrence] | None:
        """The innermost callable whose definition body encloses (line, col) in ``path``."""
        best: tuple[str, Occurrence] | None = None
        best_size = None
        for symbol, (def_path, occ) in self._callable_definitions_in(path):
            if occ.enclosing is None:
                continue
            s_line, s_col, e_line, e_col = occ.enclosing
            if (line, col) < (s_line, s_col) or (line, col) > (e_line, e_col):
                continue
            size = (e_line - s_line, e_col - s_col)
            if best_size is None or size < best_size:
                best, best_size = (symbol, occ), size
        return best

    def _callable_definitions_in(self, path: str):
        doc = self.documents.get(path)
        if doc is None:
            return
        for occ in doc.occurrences:
            if occ.is_definition and occ.symbol.endswith(_CALLABLE_SUFFIX) and occ.enclosing is not None:
                yield occ.symbol, (path, occ)

    def callers(self, symbol: str) -> list[tuple[str, str, int, int]]:
        """(caller symbol, path, line, col) for every reference of ``symbol`` inside a callable's body."""
        out: list[tuple[str, str, int, int]] = []
        seen: set[tuple[str, str, int, int]] = set()
        for path, occ in self.references.get(symbol, []):
            if occ.is_definition:
                continue
            found = self._enclosing_callable(path, occ.line, occ.col)
            if found is None or found[0] == symbol:
                continue
            key = (found[0], path, occ.line, occ.col)
            if key not in seen:
                seen.add(key)
                out.append(key)
        return sorted(out, key=lambda item: (item[1], item[2], item[3]))

    def callees(self, symbol: str) -> list[tuple[str, str, int]]:
        """(callee symbol, path, line) for every callable referenced inside ``symbol``'s body."""
        definition = self.definitions.get(symbol)
        if definition is None or definition[1].enclosing is None:
            return []
        path, occ = definition
        enclosing = occ.enclosing
        s_line, s_col, e_line, e_col = enclosing
        out: list[tuple[str, str, int]] = []
        seen: set[tuple[str, int]] = set()
        for ref in self.documents[path].ordered:
            # Only callables this workspace defines: a call into std or a dependency is not part of the graph.
            if ref.is_definition or ref.symbol == symbol or ref.symbol not in self.definitions:
                continue
            if not ref.symbol.endswith(_CALLABLE_SUFFIX):
                continue
            if (ref.line, ref.col) < (s_line, s_col) or (ref.line, ref.col) > (e_line, e_col):
                continue
            if (ref.symbol, ref.line) not in seen:
                seen.add((ref.symbol, ref.line))
                out.append((ref.symbol, path, ref.line))
        return out


def _descriptor_name(symbol: str) -> str:
    """The last descriptor's name of a SCIP symbol: ``... listing/xrefs/descend().`` -> ``descend``, and a
    method's impl prefix goes: ``... impl#[S]method().`` -> ``method``."""
    match = re.search(r"([A-Za-z_]\w*)(?:\([^()]*\))?\.?$", symbol)
    return match.group(1) if match else symbol


def fingerprint(root: Path) -> str:
    """A digest of the workspace's sources and manifests: a changed file, a new one or a changed lockfile
    gives a new digest. ``target`` directories and hidden directories are not part of the project."""
    digest = hashlib.sha256()
    entries: list[tuple[str, int, int]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("target", ".git") and not d.startswith(".")]
        for name in filenames:
            if name.endswith(".rs") or name in ("Cargo.toml", "Cargo.lock"):
                path = Path(dirpath, name)
                stat = path.stat()
                entries.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
    for entry in sorted(entries):
        digest.update(repr(entry).encode())
    return digest.hexdigest()


def cache_dir(root: Path) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "codeflow" / "scip" / hashlib.sha256(str(root).encode()).hexdigest()[:16]


def cached(root: Path) -> Path | None:
    """The cached database of ``root`` when it matches the sources now, else None (no export is started)."""
    root = root.resolve()
    directory = cache_dir(root)
    db_path, stamp = directory / "index.sqlite", directory / "fingerprint"
    if db_path.is_file() and stamp.is_file() and stamp.read_text().strip() == fingerprint(root):
        return db_path
    return None


def start_export(root: Path) -> bool:
    """Export ``root`` in a detached process unless one is already running for it; True when started.
    A query never waits for a cold export: it answers from the language server meanwhile."""
    root = root.resolve()
    directory = cache_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    pid_file = directory / "exporting.pid"
    if pid_file.is_file():
        try:
            os.kill(int(pid_file.read_text().strip()), 0)
            return False  # still running
        except (ValueError, OSError):
            pass
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; from pathlib import Path; from codeflow import _scip; _scip.export(Path(sys.argv[1]))", str(root)],
        cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    pid_file.write_text(f"{process.pid}\n")
    return True


def export(root: Path, timeout: float = 900) -> Path:
    """The workspace's SCIP index database (``_scip_db``), from the cache when the sources have not changed,
    else a fresh ``rust-analyzer scip`` run whose export is read into the database and then discarded.
    Written atomically; raises ScipError on failure."""
    from . import _scip_db

    root = root.resolve()
    directory = cache_dir(root)
    digest = fingerprint(root)
    db_path = directory / "index.sqlite"
    stamp = directory / "fingerprint"
    if db_path.is_file() and stamp.is_file() and stamp.read_text().strip() == digest:
        return db_path
    directory.mkdir(parents=True, exist_ok=True)
    scip_tmp = _temp_in(directory, ".scip")
    db_tmp = _temp_in(directory, ".sqlite")
    try:
        subprocess.run(["rust-analyzer", "scip", str(root), "--output", str(scip_tmp)],
                       cwd=root, capture_output=True, text=True, timeout=timeout, check=True)
        if not scip_tmp.is_file() or scip_tmp.stat().st_size == 0:
            raise ScipError("rust-analyzer wrote no index")
        _scip_db.build(scip_tmp, db_tmp)  # refuses an export it cannot read before a database replaces the old one
        os.replace(db_tmp, db_path)
        stamp.write_text(digest + "\n")
    except (OSError, subprocess.SubprocessError, sqlite3.Error, ScipError) as error:
        raise ScipError(f"rust-analyzer scip failed for {root}: {error}") from error
    finally:
        scip_tmp.unlink(missing_ok=True)
        db_tmp.unlink(missing_ok=True)
    return db_path


def _temp_in(directory: Path, suffix: str) -> Path:
    with tempfile.NamedTemporaryFile(dir=directory, suffix=suffix, delete=False) as handle:
        return Path(handle.name)
