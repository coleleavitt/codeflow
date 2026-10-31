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


if __name__ == "__main__":
    unittest.main()
