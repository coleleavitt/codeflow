"""The analyses every backend shares: dominators, def-use with reaching definitions, and
references classified by what each site does."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeflow import _analysis  # noqa: E402


def graph(edges: dict[int, list[int]], lines: dict[int, tuple[int, int]] | None = None) -> dict:
    blocks = [{"id": b, "lines": list((lines or {}).get(b, ())) or None, "kind": "goto", "label": "",
               "succ": [{"to": t, "label": ""} for t in targets], "cleanup": False} for b, targets in edges.items()]
    return {"entry": 0, "blocks": blocks, "names": {b: f"B{b}" for b in edges}, "line": 1, "function": "f"}


class Dominators(unittest.TestCase):
    def test_a_diamond_and_a_loop(self):
        # 0 → 1 → {2, 3} → 4 → 1 (loop) / 5
        idom = _analysis.dominators(graph({0: [1], 1: [2, 3], 2: [4], 3: [4], 4: [1, 5], 5: []}))
        self.assertEqual(idom, {0: None, 1: 0, 2: 1, 3: 1, 4: 1, 5: 4})


class DefUse(unittest.TestCase):
    def test_reaching_definitions_follow_the_paths(self):
        # :1 def x; B1 branches; B2 redefines at :3; B3 reads at :5
        g = graph({0: [1], 1: [2, 3], 2: [3], 3: []}, {0: (1, 1), 1: (2, 2), 2: (3, 3), 3: (5, 5)})
        events = [_analysis.Event(1, 1, "def"), _analysis.Event(3, 1, "def"), _analysis.Event(5, 1, "use")]
        notes, outside = _analysis.def_use_notes(g, events, "x")
        self.assertEqual(notes[3], ["use x :5 ← :1, :3"])
        self.assertEqual(outside, [])

    def test_events_with_a_block_land_in_it_through_merges(self):
        # Block 0's lines span the whole function (a statement at the loop's end), so placing the
        # definition at :6 by line would put it in block 0 too and kill :3 there.
        g = graph({0: [1], 1: [2, 3], 2: [1], 3: []}, {0: (2, 7), 1: (4, 4), 2: (4, 6), 3: (8, 9)})
        g["merged_into"] = {9: 2}
        events = _analysis.mir_events(
            [{"line": 3, "var": "c", "kind": "def", "block": 0},
             {"line": 6, "var": "c", "kind": "def", "how": "&mut passed to a call", "block": 9},
             {"line": 9, "var": "c", "kind": "use", "block": 3}],
            "c", g)
        self.assertEqual([e.block for e in events], [0, 2, 3])
        notes, _ = _analysis.def_use_notes(g, events, "c")
        self.assertEqual(notes[3], ["use c :9 ← :3, :6"])

    def test_a_read_with_no_definition_is_a_value_from_outside(self):
        g = graph({0: []}, {0: (1, 2)})
        notes, outside = _analysis.def_use_notes(g, [_analysis.Event(2, 1, "use")], "y")
        self.assertEqual(outside, [":2"])
        self.assertIn("no definition in this function", notes[0][0])

    def test_python_reads_before_the_store_on_one_line(self):
        file = Path(tempfile.mkdtemp()) / "m.py"
        file.write_text("def f(n):\n    t = 0\n    t = t + n\n    t += 1\n    return t\n")
        events = [(e.line, e.kind, e.how) for e in _analysis.python_events(file, "f", "t")]
        self.assertEqual(events, [(2, "def", ""), (3, "use", ""), (3, "def", ""), (4, "use", ""), (4, "def", "augmented"), (5, "use", "")])

    def test_syntax_events_see_parameters_declarations_and_template_code(self):
        file = Path(tempfile.mkdtemp()) / "m.js"
        file.write_text("function f(a, n) {\n  let t = n;\n  t += a;\n  return `${t}`;\n}\n")
        events = [(e.line, e.kind) for e in _analysis.syntax_events(file, 1, 5, "n", 1)]
        self.assertEqual(events, [(1, "def"), (2, "use")])
        t = [(e.line, e.kind) for e in _analysis.syntax_events(file, 1, 5, "t", 1)]
        self.assertEqual(t, [(2, "def"), (3, "use"), (3, "def"), (4, "use")])


class ChangesThroughPointersAndMethods(unittest.TestCase):
    def events(self, name: str, text: str, var: str, header: int = 1) -> list[tuple[int, str, str]]:
        file = Path(tempfile.mkdtemp()) / name
        file.write_text(text)
        if file.suffix == ".py":
            found = _analysis.python_events(file, "f", var)
        else:
            found = _analysis.syntax_events(file, 1, len(text.splitlines()), var, header)
        return [(e.line, e.kind, e.how) for e in found if e.kind == "def"]

    def test_c_writes_through_a_pointer_and_out_parameters(self):
        text = "int f(int s) {\n  int x = s;\n  int *p = &x;\n  *p += 2;\n  set(&x);\n  return x;\n}\n"
        self.assertEqual(self.events("m.c", text, "x"),
                         [(2, "def", ""), (4, "def", "through a pointer"), (5, "def", "address passed to a call")])

    def test_rust_pointer_kept_in_a_field(self):
        text = ("fn f(s: i32) -> i32 {\n    let mut x = s;\n    let mut h = H { p: &raw mut x, t: 0 };\n"
                "    unsafe { *h.p += 5; }\n    poke(&mut h);\n    x\n}\n")
        self.assertEqual(self.events("m.rs", text, "x"),
                         [(2, "def", ""), (4, "def", "through a pointer"), (5, "def", "a pointer to it passed to a call")])

    def test_field_element_and_method_changes(self):
        self.assertEqual(self.events("m.rs", "fn f() {\n    let mut v = Vec::new();\n    v.push(1);\n    v[0] = 2;\n}\n", "v"),
                         [(2, "def", ""), (3, "def", "method push"), (4, "def", "field")])
        self.assertEqual(self.events("m.py", "def f():\n    acc = []\n    acc.append(1)\n    acc[0] = 2\n    acc.n = 3\n", "acc"),
                         [(2, "def", ""), (3, "def", "method append"), (4, "def", "element"), (5, "def", "field")])

    def test_mir_events_match_a_field_path_its_parts_and_its_whole(self):
        dataflow = [{"line": 1, "var": "self", "kind": "def", "how": "parameter"},
                    {"line": 2, "var": "self.limit", "kind": "def", "how": "field"},
                    {"line": 3, "var": "self.limit.max", "kind": "def"},
                    {"line": 4, "var": "self.name", "kind": "def"}]
        found = [(e.line, e.how) for e in _analysis.mir_events(dataflow, "self.limit")]
        self.assertEqual(found, [(1, "parameter, via self"), (2, "field"), (3, "via self.limit.max")])


class References(unittest.TestCase):
    def test_each_site_is_classified_by_what_it_does(self):
        cases = [
            ("let r = run(x);", "run", ".rs", False, "call"),
            ("cfg.limit = 3;", "limit", ".rs", False, "write"),
            ("Runner { v: x }", "v", ".rs", False, "write"),
            ("let p = &count;", "count", ".rs", False, "reference taken"),
            ("fn new(s: Runner) -> Runner {", "Runner", ".rs", False, "type use"),
            ("use crate::a::Runner;", "Runner", ".rs", False, "import"),
            ("const handler = retry;", "retry", ".js", True, "reference taken"),
            ("if v > LIMIT:", "LIMIT", ".py", False, "read"),
        ]
        for text, name, suffix, is_function, kind in cases:
            with self.subTest(text=text):
                col = text.index(name, text.index(name) if not text.startswith("fn new") else text.index(":")) + 1
                self.assertEqual(_analysis.classify(text, col, name, suffix, False, is_function), kind)

    def test_the_enclosing_function_ends_where_its_body_ends(self):
        lines = ["function a() {", "  x();", "}", "a();"]
        self.assertEqual(_analysis.enclosing(lines, 2, ".js"), "a")
        self.assertEqual(_analysis.enclosing(lines, 4, ".js"), "(top level)")
        py = ["def a():", "    x()", "", "a()"]
        self.assertEqual(_analysis.enclosing(py, 2, ".py"), "a")
        self.assertEqual(_analysis.enclosing(py, 4, ".py"), "(top level)")


if __name__ == "__main__":
    unittest.main()
