"""Compiler-backed checks of the Rust control-flow driver."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeflow._rust_cfg import TOOLCHAIN, rust_cfg  # noqa: E402


def have_driver_toolchain() -> bool:
    if shutil.which("cargo") is None:
        return False
    return subprocess.run(["rustc", TOOLCHAIN, "-V"], capture_output=True).returncode == 0


FIXTURE = """pub enum Failure {
    Parse(std::num::ParseIntError),
}

pub fn parse(text: &str) -> Result<u32, Failure> {
    text.parse::<u32>().map_err(Failure::Parse)
}
"""


@unittest.skipUnless(have_driver_toolchain(), f"needs the {TOOLCHAIN} toolchain")
class DriverDoesNotCrashRustc(unittest.TestCase):
    def test_a_callee_given_a_variant_constructor_is_named(self):
        """`map_err(Failure::Parse)`: the callee's type arguments hold a function item, and printing it uses
        rustc's trimmed paths, whose sanity check panics when no diagnostic was printed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "Cargo.toml").write_text('[package]\nname = "fx"\nversion = "0.1.0"\nedition = "2021"\n')
            (root / "src" / "lib.rs").write_text(FIXTURE)
            graph = rust_cfg(root / "src" / "lib.rs", "parse")
        calls = " ".join(block["label"] for block in graph["blocks"])
        self.assertIn("map_err", calls)


@unittest.skipUnless(have_driver_toolchain(), f"needs the {TOOLCHAIN} toolchain")
class RustCheckoutCompilerCrates(unittest.TestCase):
    def test_a_compiler_crate_builds_without_bootstrap(self):
        """A crate in a rust-lang/rust checkout: its build script refuses to run outside bootstrap
        (like `rustc_macros`) and its source reads bootstrap's `env!("CFG_RELEASE")` (like `rustc_span`)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.py").write_text("")
            (root / "src" / "bootstrap").mkdir(parents=True)
            (root / "src" / "ci").mkdir()
            (root / "src" / "version").write_text("1.102.0\n")
            (root / "src" / "ci" / "channel").write_text("nightly\n")
            (root / "Cargo.toml").write_text('[workspace]\nmembers = ["compiler/rustc_fx"]\nresolver = "2"\n')
            krate = root / "compiler" / "rustc_fx"
            (krate / "src").mkdir(parents=True)
            (krate / "Cargo.toml").write_text('[package]\nname = "rustc_fx"\nversion = "0.0.0"\nedition = "2024"\n')
            (krate / "build.rs").write_text(
                'fn main() {\n    assert!(std::env::var_os("RUSTC_BOOTSTRAP").is_some(), "wrong command used for building");\n}\n'
            )
            (krate / "src" / "lib.rs").write_text(
                'pub fn release(n: u32) -> &\'static str {\n    if n > 0 { env!("CFG_RELEASE") } else { env!("CFG_RELEASE_CHANNEL") }\n}\n'
            )
            graph = rust_cfg(krate / "src" / "lib.rs", "release")
        self.assertGreater(len(graph["blocks"]), 1)


if __name__ == "__main__":
    unittest.main()


MUTATE = """pub fn fill(n: usize) -> usize {
    let mut v = Vec::new();
    let mut count = 0;
    for i in 0..n {
        v.push(i);
        bump(&mut count);
    }
    let r = &mut count;
    *r += 10;
    v.len() + count
}
fn bump(c: &mut usize) { *c += 1; }
"""


@unittest.skipUnless(have_driver_toolchain(), f"needs the {TOOLCHAIN} toolchain")
class MirDefUse(unittest.TestCase):
    def test_writes_through_mut_references_define_the_variable(self):
        """`bump(&mut count)`, `v.push(i)` and `*r += 10` change a variable without assigning it: the
        MIR events count them as definitions, and the read after the loop is reached from before it
        (zero iterations) and from inside it."""
        from codeflow import _analysis, _cfg

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "Cargo.toml").write_text('[package]\nname = "mx"\nversion = "0.1.0"\nedition = "2021"\n')
            (root / "src" / "lib.rs").write_text(MUTATE)
            graph = _cfg.simplify(_cfg.ir_tidy(rust_cfg(root / "src" / "lib.rs", "fill")))
        count = _analysis.mir_events(graph["dataflow"], "count", graph)
        self.assertIn((6, "def", "&mut passed to a call"), [(e.line, e.kind, e.how) for e in count])
        self.assertIn((9, "def", "through a pointer"), [(e.line, e.kind, e.how) for e in count])
        notes, _ = _analysis.def_use_notes(graph, count, "count")
        marks = [mark for block in notes.values() for mark in block]
        self.assertIn("use count :9 ← :3, :6", marks)
        v = _analysis.mir_events(graph["dataflow"], "v", graph)
        self.assertIn((5, "def", "&mut passed to a call"), [(e.line, e.kind, e.how) for e in v])


RAW = """pub struct Holder { pub p: *mut i32, pub tag: u8 }
pub struct Cfg { pub limit: u32 }

pub fn via_field(seed: i32) -> i32 {
    let mut x = seed;
    let mut h = Holder { p: &raw mut x, tag: 0 };
    h.tag = 1;
    unsafe { *h.p += 5; }
    poke(&mut h);
    x
}
fn poke(h: &mut Holder) { unsafe { *h.p = 0; } }

impl Cfg {
    pub fn adjust(&mut self, extra: u32) -> u32 {
        if extra > 3 { self.limit = extra; }
        self.limit + 1
    }
}
"""


@unittest.skipUnless(have_driver_toolchain(), f"needs the {TOOLCHAIN} toolchain")
class MirPointersKeptInFields(unittest.TestCase):
    def test_a_raw_pointer_kept_in_a_struct_field_traces_back(self):
        """`h.p = &raw mut x`, then `*h.p += 5` and `poke(&mut h)`: both change `x`, and the read at
        the end is reached from the call."""
        from codeflow import _analysis, _cfg

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "Cargo.toml").write_text('[package]\nname = "rx"\nversion = "0.1.0"\nedition = "2021"\n')
            (root / "src" / "lib.rs").write_text(RAW)
            fill = _cfg.simplify(_cfg.ir_tidy(rust_cfg(root / "src" / "lib.rs", "via_field")))
            adjust = _cfg.simplify(_cfg.ir_tidy(rust_cfg(root / "src" / "lib.rs", "Cfg::adjust")))
        x = _analysis.mir_events(fill["dataflow"], "x", fill)
        defs = [(e.line, e.how) for e in x if e.kind == "def"]
        self.assertIn((8, "through a pointer"), defs)
        self.assertIn((9, "&mut passed to a call"), defs)
        notes, _ = _analysis.def_use_notes(fill, x, "x")
        self.assertIn("use x :10 ← :9", [m for block in notes.values() for m in block])
        limit = _analysis.mir_events(adjust["dataflow"], "self.limit", adjust)
        notes, _ = _analysis.def_use_notes(adjust, limit, "self.limit")
        self.assertIn("use self.limit :17 ← :15, :16", [m for block in notes.values() for m in block])
