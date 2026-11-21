"""Block ranges and paths over a control-flow graph (the graph dict codeflow builds)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeflow import _views  # noqa: E402

# A diamond with a loop back: 0 -> 1 -> 3, 0 -> 2 -> 3, 3 -> 0.
GRAPH = {
    "blocks": [
        {"id": "0", "succ": [{"to": 1, "label": ""}, {"to": 2, "label": ""}]},
        {"id": "1", "succ": [{"to": 3, "label": ""}]},
        {"id": "2", "succ": [{"to": 3, "label": ""}]},
        {"id": "3", "succ": [{"to": 0, "label": ""}]},
    ]
}


class Blocks(unittest.TestCase):
    def test_ranges_and_lists(self):
        self.assertEqual(_views.parse_blocks("1-3"), {1, 2, 3})
        self.assertEqual(_views.parse_blocks("0,3,5-6"), {0, 3, 5, 6})

    def test_a_bad_range_is_refused(self):
        with self.assertRaises(_views.ViewError):
            _views.parse_blocks("5-2")
        with self.assertRaises(_views.ViewError):
            _views.parse_blocks("x")

    def test_a_subgraph_drops_edges_that_leave_the_range(self):
        sub = _views.subgraph(GRAPH, {0, 1})
        self.assertEqual([b["id"] for b in sub["blocks"]], ["0", "1"])
        self.assertEqual(sub["blocks"][0]["succ"], [{"to": 1, "label": ""}])
        self.assertEqual(sub["blocks"][1]["succ"], [])


class Paths(unittest.TestCase):
    def test_both_routes_shortest_first(self):
        found, more = _views.paths(GRAPH, 0, 3)
        self.assertEqual(found, [[0, 1, 3], [0, 2, 3]])
        self.assertFalse(more)

    def test_the_cap_is_reported(self):
        found, more = _views.paths(GRAPH, 0, 3, limit=1)
        self.assertEqual(len(found), 1)
        self.assertTrue(more)

    def test_no_path_and_unknown_blocks(self):
        apart = {"blocks": [{"id": "5", "succ": []}, {"id": "6", "succ": []}]}
        found, _ = _views.paths(apart, 5, 6)
        self.assertEqual(found, [])
        self.assertEqual(_views.render_paths(found, False, 5, 6), "no path from B5 to B6")
        found, _ = _views.paths(GRAPH, 1, 2)  # round the loop, so it does have one
        self.assertEqual(found, [[1, 3, 0, 2]])
        with self.assertRaises(_views.ViewError):
            _views.paths(GRAPH, 0, 9)


if __name__ == "__main__":
    unittest.main()


class DisplayedNumbers(unittest.TestCase):
    """Internal ids are not the printed B-numbers: the translation keeps the printed ones everywhere the user sees."""

    GRAPH = {
        "entry": 7,
        "blocks": [
            {"id": "7", "succ": [{"to": 12, "label": ""}]},
            {"id": "12", "succ": [{"to": 53, "label": ""}, {"to": 53, "label": "arm"}]},
            {"id": "53", "succ": []},
        ],
        "names": {"7": "B0", "12": "B1", "53": "B2"},
    }

    def test_parse_accepts_b_prefixed_numbers(self):
        self.assertEqual(_views.parse_block("B3"), 3)
        self.assertEqual(_views.parse_block("3"), 3)
        self.assertEqual(_views.parse_blocks("B1-B2,0"), {0, 1, 2})
        with self.assertRaises(_views.ViewError):
            _views.parse_block("x3")

    def test_the_displayed_graph_renumbers_ids_and_edges(self):
        out = _views.displayed(self.GRAPH)
        self.assertEqual([b["id"] for b in out["blocks"]], [0, 1, 2])
        self.assertTrue(all(isinstance(e["to"], int) for b in out["blocks"] for e in b["succ"]))
        self.assertEqual(out["blocks"][0]["succ"], [{"to": 1, "label": ""}])
        self.assertEqual(out["entry"], 0)

    def test_numbers_map_to_internal_ids_and_unknown_ones_are_reported(self):
        ids, missing = _views.internal_ids(self.GRAPH, {0, 2, 9})
        self.assertEqual(ids, {7, 53})
        self.assertEqual(missing, [9])

    def test_two_edges_to_one_block_give_one_path(self):
        found, _ = _views.paths(self.GRAPH, 7, 53)
        self.assertEqual(found, [[7, 12, 53]])
