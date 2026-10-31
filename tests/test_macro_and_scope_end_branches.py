"""Macro and scope-end plumbing that is not a branch of the source."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeflow._cfg import ir_tidy  # noqa: E402


def block(id, kind, label, succ, lines=None):
    return {"id": id, "lines": lines, "kind": kind, "label": label, "cleanup": False, "test": None,
            "succ": [{"to": to, "label": edge} for to, edge in succ]}


class TracingMacroWithoutArguments(unittest.TestCase):
    def test_the_level_gate_does_not_stay_behind_as_a_call(self):
        """`info!("listening")`: only the level gate carries a source line; the event after it carries none, so
        the whole macro collapses and the gate must go with it."""
        graph = {
            "file": "/nonexistent.rs",
            "entry": 0,
            "blocks": [
                block(0, "call", "call <Level as PartialOrd<LevelFilter>>::le(..)", [(1, "")], [3, 3]),
                block(1, "switch", "switch on _5", [(2, "true"), (4, "false")]),
                block(2, "call", "call DefaultCallsite::metadata(..)", [(3, "")]),
                block(3, "call", "call Arguments::from_str(..)", [(4, "")]),
                block(4, "call", "call work(..)", [(5, "")], [5, 5]),
                block(5, "return", "return", [], [6, 6]),
            ],
        }
        tidy = ir_tidy(graph)
        labels = " ".join(b["label"] for b in tidy["blocks"])
        self.assertNotIn("Level", labels)
        self.assertIn("work", labels)
        self.assertEqual([b for b in tidy["blocks"] if b["kind"] == "switch"], [])


def drop_flag_graph(source_line: str):
    """A branch on the line of a scope's closing brace whose two arms are empty and meet again, as rustc
    emits for a drop flag; with another `source_line` the same shape is an `if`."""
    graph = {
        "file": "",
        "entry": 0,
        "blocks": [
            block(0, "call", "call first(..)", [(1, "")], [2, 2]),
            block(1, "switch", "switch on _7", [(2, "true"), (3, "false")], [3, 3]),
            block(2, "goto", "goto", [(4, "")], [3, 3]),
            block(3, "goto", "goto", [(4, "")]),
            block(4, "call", "call after(..)", [(5, "")], [5, 5]),
            block(5, "return", "return", [], [6, 6]),
        ],
    }
    return graph, ["fn f() {", "    first();", source_line, "    ;", "    after();", "}"]


class ScopeEndDropFlag(unittest.TestCase):
    def tidy(self, source_line):
        graph, lines = drop_flag_graph(source_line)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.rs"
            path.write_text("\n".join(lines))
            graph["file"] = str(path)
            return ir_tidy(graph)

    def test_a_branch_on_a_closing_brace_with_empty_arms_is_dropped(self):
        tidy = self.tidy("    }")
        self.assertEqual([b for b in tidy["blocks"] if b["kind"] == "switch"], [])
        labels = " ".join(b["label"] for b in tidy["blocks"])
        self.assertIn("first", labels)
        self.assertIn("after", labels)

    def test_the_same_shape_on_an_if_line_stays_a_branch(self):
        tidy = self.tidy("    if flag {")
        self.assertEqual(len([b for b in tidy["blocks"] if b["kind"] == "switch"]), 1)


if __name__ == "__main__":
    unittest.main()
