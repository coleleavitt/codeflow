"""The SCIP reader and the offline callers and callees it answers. ``fixtures/tiny.scip`` is the index of
``fixtures/tiny_src`` (``rust-analyzer scip``): leaf <- middle <- top, and method calls leaf."""

import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeflow import _scip  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class TinyIndex(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = _scip.Index.load(FIXTURES / "tiny.scip")
        cls.by_name = {cls.index.name(sym): sym for sym in cls.index.definitions}

    def names(self, pairs):
        return sorted(self.index.name(item[0]) for item in pairs)

    def test_callers_are_the_callables_whose_bodies_hold_a_reference(self):
        self.assertEqual(self.names(self.index.callers(self.by_name["leaf"])), ["method", "middle", "middle"])
        self.assertEqual(self.names(self.index.callers(self.by_name["middle"])), ["top"])
        self.assertEqual(self.index.callers(self.by_name["top"]), [])

    def test_callees_are_the_workspace_callables_inside_a_body(self):
        self.assertEqual([self.index.name(c) for c, _, _ in self.index.callees(self.by_name["middle"])], ["leaf"])
        self.assertEqual([self.index.name(c) for c, _, _ in self.index.callees(self.by_name["top"])], ["middle"])
        self.assertEqual(self.index.callees(self.by_name["leaf"]), [])

    def test_a_std_call_is_not_a_callee(self):
        for symbol in self.index.definitions:
            self.assertFalse(any("Add" in self.index.name(c) for c, _, _ in self.index.callees(symbol)))

    def test_the_symbol_under_a_position(self):
        self.assertEqual(self.index.symbol_at("src/lib.rs", 1, 8), self.by_name["middle"])
        self.assertIsNone(self.index.symbol_at("src/lib.rs", 1, 0))  # the `pub` keyword
        self.assertIsNone(self.index.symbol_at("nope.rs", 0, 0))

    def test_a_descriptor_name(self):
        self.assertEqual(_scip._descriptor_name("rust-analyzer cargo fix 0.0.0 lib/leaf()."), "leaf")
        self.assertEqual(_scip._descriptor_name("rust-analyzer cargo fix 0.0.0 lib/impl#[S]method()."), "method")


class TreeFromTheIndex(unittest.TestCase):
    """The tree builder against a cache filled from the fixture (the detached export is not run here)."""

    def setUp(self):
        import asyncio  # noqa: F401 - used in the tests below
        from codeflow import _scip_tree

        self.tree_module = _scip_tree
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "fix"
        shutil.copytree(FIXTURES / "tiny_src", self.root / "src")
        (self.root / "Cargo.toml").write_text('[package]\nname = "fix"\nversion = "0.0.0"\n')
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)  # a project is a git repository
        self.cache = Path(self.tmp.name) / "cache"
        self.patch = unittest.mock.patch.object(_scip, "cache_dir", lambda root: self.cache)
        self.patch.start()
        self.cache.mkdir()
        from codeflow import _scip_db

        _scip_db.build(FIXTURES / "tiny.scip", self.cache / "index.sqlite")
        (self.cache / "fingerprint").write_text(_scip.fingerprint(self.root) + "\n")

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def run_tree(self, at, direction):
        import asyncio

        return asyncio.run(self.tree_module.tree(self.root / "src" / "lib.rs", at, direction, 2, self.root))

    def test_callers_by_name_and_by_position(self):
        by_name = self.run_tree("middle", "incoming")
        self.assertIn("top  src/lib.rs:3  (at line 3)", by_name.text)
        by_position = self.run_tree("2:8", "incoming")  # the line:col form the call tree passes
        self.assertEqual(by_position.text, by_name.text)
        self.assertEqual(by_name.edges, [("top", "middle", "src/lib.rs:3")])

    def test_callees_follow_the_body(self):
        tree = self.run_tree("middle", "outgoing")
        self.assertIn("leaf  src/lib.rs:1  (at line 2)", tree.text)
        self.assertEqual(tree.edges, [("middle", "leaf", "src/lib.rs:2")])

    def test_a_stale_index_is_not_used(self):
        (self.root / "src" / "lib.rs").write_text("pub fn leaf() -> u32 { 9 }\n")
        with self.assertRaises(LookupError):
            self.run_tree("middle", "incoming")


class DatabaseAnswersLikeTheReader(unittest.TestCase):
    """The SQLite database answers the same questions as the in-memory reader, for the same export."""

    @classmethod
    def setUpClass(cls):
        from codeflow import _scip_db

        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "index.sqlite"
        _scip_db.build(FIXTURES / "tiny.scip", cls.db_path)
        cls.mem = _scip.Index.load(FIXTURES / "tiny.scip")
        cls.db = _scip_db.SqlIndex(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.tmp.cleanup()

    def test_callers_and_callees_match(self):
        for symbol in self.mem.definitions:
            self.assertEqual(
                sorted((c, p, line, col) for c, p, line, col in self.db.callers(symbol)),
                sorted((c, p, line, col) for c, p, line, col in self.mem.callers(symbol)),
            )
            self.assertEqual(
                sorted(self.db.callees(symbol)),
                sorted(self.mem.callees(symbol)),
            )

    def test_names_and_positions_match(self):
        for symbol in self.mem.definitions:
            self.assertEqual(self.db.name(symbol), self.mem.name(symbol))
            self.assertEqual(self.db.known(symbol), True)
        self.assertEqual(self.db.symbol_at("src/lib.rs", 1, 8), self.mem.symbol_at("src/lib.rs", 1, 8))
        self.assertIsNone(self.db.symbol_at("src/lib.rs", 1, 0))
        self.assertFalse(self.db.known("rust-analyzer cargo std 1.0 lib/nothing()."))


class Fingerprint(unittest.TestCase):
    def test_an_edit_changes_the_digest_and_a_target_dir_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(FIXTURES / "tiny_src", root / "src")
            (root / "Cargo.toml").write_text('[package]\nname = "fix"\nversion = "0.0.0"\n')
            before = _scip.fingerprint(root)
            (root / "target").mkdir()
            (root / "target" / "junk.rs").write_text("fn x() {}\n")
            self.assertEqual(_scip.fingerprint(root), before)
            (root / "src" / "lib.rs").write_text("pub fn leaf() -> u32 { 2 }\n")
            self.assertNotEqual(_scip.fingerprint(root), before)


if __name__ == "__main__":
    unittest.main()
