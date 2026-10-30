"""A macro's condition inside a loop keeps its `continue` edge and the loop body after it."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeflow._cfg import _summarize, _wrap_label, ir_tidy  # noqa: E402


def block(id, kind, label, succ, lines=None):
    return {"id": id, "lines": lines, "kind": kind, "label": label, "cleanup": False, "test": None,
            "succ": [{"to": to, "label": edge} for to, edge in succ]}


def loop_with_matches_and_continue():
    """`for x in it { if a || !matches!(y, 1 | 2) { continue; } body(); }` as MIR lays it out: the macro's
    value switches carry no source line, and the `continue` is the edge back to the loop head."""
    return {
        "file": "/nonexistent.rs",
        "entry": 0,
        "blocks": [
            block(0, "call", "call into_iter(..)", [(1, "")], [1, 1]),
            block(1, "switch", "switch on next", [(2, "Some"), (6, "None")], [2, 2]),
            block(2, "switch", "switch on a", [(3, "false"), (1, "true")], [3, 3]),
            block(3, "switch", "switch on y", [(5, "1"), (5, "2"), (4, "otherwise")]),
            block(4, "goto", "goto", [(6, "")]),
            block(5, "goto", "goto", [(6, "")]),
            block(6, "switch", "switch on matched", [(1, "false"), (7, "true")]),
            block(7, "call", "call body(..)", [(1, "")], [6, 6]),
            block(8, "return", "return", [], [8, 8]),
        ],
    }


class LoopWithMacroCondition(unittest.TestCase):
    def test_the_continue_edge_and_the_body_survive(self):
        graph = loop_with_matches_and_continue()
        # The loop's exit: block 1's None edge.
        graph["blocks"][1]["succ"][1]["to"] = 8
        tidy = ir_tidy(graph)
        by_id = {b["id"]: b for b in tidy["blocks"]}
        reachable_labels = " ".join(b["label"] for b in tidy["blocks"])
        self.assertIn("body", reachable_labels, "the loop body after the condition is reachable")
        condition = next(b for b in tidy["blocks"] if b["kind"] == "switch" and any(e["label"] == "false" for e in b["succ"]) and any(e["to"] == 1 for e in b["succ"]) and b["id"] != 2)
        targets = {e["label"]: e["to"] for e in condition["succ"]}
        self.assertEqual(targets["false"], 1, "the continue edge goes back to the loop head")
        self.assertNotEqual(targets["true"], 1, "the other edge leads into the body, not back to the head")
        self.assertIn(targets["true"], by_id)


def if_not_matches_with_a_body():
    """`if !matches!(v, A) { report(); }` outside any loop: the discriminant switch has a line, the bool it
    feeds has none, and the body between that bool and the join is real code."""
    return {
        "file": "/nonexistent.rs",
        "entry": 0,
        "blocks": [
            block(0, "switch", "switch on discriminant", [(1, "A"), (2, "otherwise")], [5, 5]),
            block(1, "goto", "goto", [(3, "")]),
            block(2, "goto", "goto", [(3, "")]),
            block(3, "switch", "switch on matched", [(5, "false"), (4, "true")]),
            block(4, "call", "call report(..)", [(5, "")], [6, 6]),
            block(5, "call", "call after(..)", [(6, "")], [8, 8]),
            block(6, "return", "return", [], [9, 9]),
        ],
    }


class BranchWithABodyAfterAMacroCondition(unittest.TestCase):
    def test_the_body_is_kept(self):
        tidy = ir_tidy(if_not_matches_with_a_body())
        labels = " ".join(b["label"] for b in tidy["blocks"])
        self.assertIn("report", labels, "the if body is reachable")
        condition = next(b for b in tidy["blocks"] if b["kind"] == "switch" and {e["label"] for e in b["succ"]} == {"true", "false"})
        self.assertEqual(len(condition["succ"]), 2)


class BlockLabels(unittest.TestCase):
    def test_calls_keep_their_order_and_a_repeat_shows_its_count(self):
        steps = ["call _print(..)", "call new_display(..)", "call _print(..)", "call _print(..)", "return"]
        self.assertEqual(_summarize(steps), "calls _print, new_display, _print ×2 · return")

    def test_no_call_is_cut(self):
        steps = [f"call f{i}(..)" for i in range(30)]
        label = _summarize(steps)
        self.assertTrue(all(f"f{i}" in label for i in range(30)))
        self.assertNotIn("…", label)

    def test_a_long_label_wraps_between_calls(self):
        label = _summarize([f"call <Vec<u32> as Trait>::method_{i}(..)" for i in range(12)]) + " · if cond"
        lines = _wrap_label(label, width=60)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(line) <= 60 + 40 for line in lines))
        joined = " ".join(lines).replace("· ", "").replace(", ", ",")
        for i in range(12):
            self.assertIn(f"method_{i}", joined)
        self.assertTrue(lines[-1].lstrip("· ").startswith(("if cond", "calls")) or "if cond" in " ".join(lines))

    def test_a_macros_diverging_call_sits_on_the_line_before_it(self):
        graph = {
            "file": "/nonexistent.rs",
            "entry": 0,
            "blocks": [
                block(0, "switch", "switch on cond", [(1, "false"), (2, "true")], [7, 7]),
                block(1, "call", "call panic_fmt(..)", []),
                block(2, "return", "return", [], [8, 8]),
            ],
        }
        tidy = ir_tidy(graph)
        panic = next(b for b in tidy["blocks"] if "panic_fmt" in b["label"])
        self.assertEqual(panic["lines"], [7, 7])


if __name__ == "__main__":
    unittest.main()
