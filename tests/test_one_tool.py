"""cfg is the one tool: it finds a function the model named in the wrong file, falls back to a
syntax graph for a Rust crate that does not build, resolves `Type::method` in generic impls and
test modules by syntax, and names C++ methods whose return type is a reference."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import codeflow  # noqa: E402

HERE = Path(__file__).resolve().parents[1] / "src" / "codeflow"


def project(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp())
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    return root


def script(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["uv", "run", "--quiet", "--script", str(HERE / name), *map(str, args)],
                          capture_output=True, text=True, timeout=600)


class Locate(unittest.TestCase):
    def test_a_function_in_another_file_is_found_and_named(self):
        root = project({"a.py": "def caller():\n    return helper()\n", "pkg/b.py": "def helper():\n    return 1\n"})
        file, note = codeflow._locate(root / "a.py", "helper", root)
        self.assertEqual(file, (root / "pkg/b.py").resolve())
        self.assertIn("not defined in a.py; using pkg/b.py:1", note)

    def test_a_function_defined_twice_elsewhere_names_both(self):
        root = project({"a.py": "x = 1\n", "b.py": "def h():\n    pass\n", "c.py": "def h():\n    pass\n"})
        with self.assertRaisesRegex(LookupError, r"defined in b.py:1, c.py:1"):
            codeflow._locate(root / "a.py", "h", root)

    def test_a_qualified_rust_name_needs_its_owner_in_the_file(self):
        root = project({"src/lib.rs": "mod a;\n", "src/a.rs": "struct R;\nimpl R { fn go(&self) {} }\n",
                        "src/b.rs": "struct S;\nimpl S { fn go(&self) {} }\n"})
        file, note = codeflow._locate(root / "src/lib.rs", "R::go", root)
        self.assertEqual(file, (root / "src/a.rs").resolve())
        self.assertIsNotNone(note)

    def test_a_name_in_the_file_or_a_line_stays(self):
        root = project({"a.py": "def f():\n    pass\n"})
        self.assertEqual(codeflow._locate(root / "a.py", "f", root), (root / "a.py", None))
        self.assertEqual(codeflow._locate(root / "a.py", 2, root), (root / "a.py", None))


class RustSyntaxGraph(unittest.TestCase):
    SOURCE = (
        "struct Runner<T>(T);\n"
        "impl<T> Runner<T> {\n"
        "    fn run(&self, x: Option<i32>) -> Result<i32, E> {\n"
        "        let v = x.ok_or(E)?;\n"
        "        let Some(w) = Some(v) else { return Err(E) };\n"
        "        'outer: for i in 0..w {\n"
        "            match i { 0 => continue, 1 => break 'outer, _ => {} }\n"
        "        }\n"
        "        if v > 3 { panic!(\"big\") }\n"
        "        Ok(v)\n"
        "    }\n"
        "}\n"
        "#[cfg(test)]\nmod tests { fn run(&self) {} }\n"
    )

    def test_generic_impls_and_test_modules_resolve_by_syntax(self):
        file = Path(tempfile.mkdtemp()) / "lib.rs"
        file.write_text(self.SOURCE)
        self.assertEqual(script("rcfg.py", file, "Runner::run", "--lines").stdout.split()[0], "3")
        ambiguous = script("rcfg.py", file, "run")
        self.assertIn("is ambiguous", ambiguous.stderr)
        self.assertIn("tests::run (line 14)", ambiguous.stderr)

    def test_the_graph_has_the_rust_exits(self):
        file = Path(tempfile.mkdtemp()) / "lib.rs"
        file.write_text(self.SOURCE)
        graph = json.loads(script("rcfg.py", file, "Runner::run").stdout)
        labels = [edge["label"] for block in graph["blocks"] for edge in block["succ"]]
        kinds = {block["label"] for block in graph["blocks"]}
        self.assertIn("? returns the error", labels)
        self.assertIn("else", labels)  # let-else
        self.assertIn("panics", labels)
        self.assertIn("break", labels)
        self.assertIn("continue", labels)
        self.assertIn("return Ok(v)", kinds)

    def test_a_build_error_is_named_in_the_header(self):
        message = "cargo failed (101):\n   Compiling x\nerror[E0425]: cannot find value `y` in this scope\nerror: could not compile `x`"
        self.assertEqual(codeflow._build_error(message),
                         "the crate does not build (error[E0425]: cannot find value `y` in this scope)")


class CppNames(unittest.TestCase):
    def test_a_reference_returning_method_is_found(self):
        file = Path(tempfile.mkdtemp()) / "h.cpp"
        file.write_text("namespace n {\nconst S& Handler::UserAgent(bool f) {\n  if (f) { return a; }\n  return b;\n}\n}\n")
        graph = json.loads(script("ccfg.py", file, "Handler::UserAgent").stdout)
        self.assertEqual((graph["function"], graph["line"]), ("Handler::UserAgent", 2))


if __name__ == "__main__":
    unittest.main()
