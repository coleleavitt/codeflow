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
