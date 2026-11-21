"""A workspace's SCIP index as an indexed SQLite database, and the queries callers, callees and the
tree read from it.

Building reads the protobuf one document at a time and inserts its occurrences in one transaction, so
the 224 MB kuna export is never held in memory whole (the in-memory ``_scip.Index`` was the 20-second
startup). A query then reads only the rows it needs through an index: ``symbol`` for references and
definitions, ``(path, line, col)`` for the occurrence under a cursor, ``path`` for the bodies that
enclose a reference. Like codegraph-rs, which keeps its compiler symbols in indexed SQLite tables.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import _scip
from ._scip import DEFINITION, _CALLABLE_SUFFIX, _descriptor_name, _document, _fields

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE occ (
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    col INTEGER NOT NULL,
    end_col INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    roles INTEGER NOT NULL,
    el INTEGER, ec INTEGER, eel INTEGER, eec INTEGER  -- the enclosing range of a definition, else NULL
);
CREATE TABLE names (symbol TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_INDEXES = """
CREATE INDEX occ_symbol ON occ(symbol);
CREATE INDEX occ_position ON occ(path, line, col);
CREATE INDEX occ_definitions ON occ(path) WHERE roles & 1;
"""


class SqlIndex:
    """Read-only queries over a built database (``build``)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.db = sqlite3.connect(f"file:{self.path}?mode=ro&immutable=1", uri=True, check_same_thread=False)
        self.db.row_factory = sqlite3.Row

    def close(self) -> None:
        self.db.close()

    def has_path(self, path: str) -> bool:
        return self.db.execute("SELECT 1 FROM occ WHERE path = ? LIMIT 1", (path,)).fetchone() is not None

    def symbol_at(self, path: str, line: int, col: int) -> str | None:
        """The symbol of the occurrence covering (line, col), 0-based."""
        row = self.db.execute(
            "SELECT symbol FROM occ WHERE path = ? AND line = ? AND col <= ? AND end_col > ? AND symbol != '' LIMIT 1",
            (path, line, col, col),
        ).fetchone()
        return row["symbol"] if row else None

    def definition_on_line(self, path: str, line: int) -> list[str]:
        """The callable definitions starting on a line, in column order."""
        rows = self.db.execute(
            "SELECT symbol FROM occ WHERE path = ? AND line = ? AND roles & 1 ORDER BY col", (path, line)
        ).fetchall()
        return [r["symbol"] for r in rows if r["symbol"].endswith(_CALLABLE_SUFFIX)]

    def known(self, symbol: str) -> bool:
        """Whether this workspace defines the symbol."""
        return self.definition(symbol) is not None

    def definition(self, symbol: str) -> tuple[str, int, int, tuple | None] | None:
        """(path, 0-based line, 0-based col, enclosing range or None) of the first definition."""
        row = self.db.execute(
            "SELECT path, line, col, el, ec, eel, eec FROM occ WHERE symbol = ? AND roles & 1 ORDER BY path, line, col LIMIT 1",
            (symbol,),
        ).fetchone()
        if row is None:
            return None
        enclosing = None if row["el"] is None else (row["el"], row["ec"], row["eel"], row["eec"])
        return row["path"], row["line"], row["col"], enclosing

    def name(self, symbol: str) -> str:
        row = self.db.execute("SELECT name FROM names WHERE symbol = ?", (symbol,)).fetchone()
        return row["name"] if row else _descriptor_name(symbol)

    def _enclosing_callable(self, path: str, line: int, col: int) -> str | None:
        """The innermost callable whose definition body holds (line, col) in ``path``."""
        rows = self.db.execute(
            """SELECT symbol, el, ec, eel, eec FROM occ
               WHERE path = ? AND roles & 1 AND el IS NOT NULL
                 AND (el < ? OR (el = ? AND ec <= ?))
                 AND (eel > ? OR (eel = ? AND eec >= ?))
               ORDER BY (eel - el) ASC, (eec - ec) ASC""",
            (path, line, line, col, line, line, col),
        ).fetchall()
        for row in rows:
            if row["symbol"].endswith(_CALLABLE_SUFFIX):
                return row["symbol"]
        return None

    def callers(self, symbol: str) -> list[tuple[str, str, int, int]]:
        """(caller symbol, path, 0-based line, 0-based col) for each reference inside a callable's body."""
        refs = self.db.execute(
            "SELECT path, line, col FROM occ WHERE symbol = ? AND roles & 1 = 0 ORDER BY path, line, col",
            (symbol,),
        ).fetchall()
        out: list[tuple[str, str, int, int]] = []
        seen: set[tuple[str, str, int, int]] = set()
        for ref in refs:
            caller = self._enclosing_callable(ref["path"], ref["line"], ref["col"])
            if caller is None or caller == symbol:
                continue
            key = (caller, ref["path"], ref["line"], ref["col"])
            if key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def callees(self, symbol: str) -> list[tuple[str, str, int]]:
        """(callee symbol, path, 0-based line) for each callable this workspace defines, referenced in the body."""
        found = self.definition(symbol)
        if found is None or found[3] is None:
            return []
        path, _, _, (s_line, s_col, e_line, e_col) = found
        rows = self.db.execute(
            """SELECT symbol, line, col FROM occ
               WHERE path = ? AND roles & 1 = 0 AND symbol != ? AND symbol LIKE '%).'
                 AND (line > ? OR (line = ? AND col >= ?))
                 AND (line < ? OR (line = ? AND col <= ?))
               ORDER BY line, col""",
            (path, symbol, s_line, s_line, s_col, e_line, e_line, e_col),
        ).fetchall()
        out: list[tuple[str, str, int]] = []
        seen: set[tuple[str, int]] = set()
        for row in rows:
            if not self.known(row["symbol"]):
                continue  # a call into std or a dependency is outside the graph
            if (row["symbol"], row["line"]) not in seen:
                seen.add((row["symbol"], row["line"]))
                out.append((row["symbol"], path, row["line"]))
        return out


def build(scip_path: Path, db_path: Path) -> None:
    """Write the database for a SCIP export: one document at a time, one transaction, then the indexes.
    Refuses an export it cannot read before a partial database could replace a good one."""
    if db_path.exists():
        db_path.unlink()
    db = sqlite3.connect(db_path)
    try:
        db.executescript(_SCHEMA)
        db.execute("BEGIN")
        documents = 0
        with open(scip_path, "rb") as handle:
            data = handle.read()  # bounded by the export; the protobuf is walked without a full object tree
        for number, _, value in _fields(data):
            if number != 2:
                continue
            doc = _document(value)
            documents += 1
            db.executemany(
                "INSERT INTO occ VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    (
                        doc.path, o.line, o.col, o.end_col, o.symbol, o.roles,
                        *((None, None, None, None) if o.enclosing is None else o.enclosing),
                    )
                    for o in doc.occurrences
                    if o.symbol
                ),
            )
            db.executemany("INSERT OR IGNORE INTO names VALUES (?, ?)", doc.names.items())
        db.execute("COMMIT")
        db.executescript(_INDEXES)
        db.execute("INSERT INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
        db.execute("INSERT INTO meta VALUES ('documents', ?)", (str(documents),))
        db.commit()
    except Exception:
        db.close()
        db_path.unlink(missing_ok=True)
        raise
    db.close()
