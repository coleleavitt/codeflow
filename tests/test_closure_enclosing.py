"""A line inside a Rust closure also shows the graph of the function the closure is written in."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeflow import _enclosing_named  # noqa: E402


class EnclosingNamed(unittest.TestCase):
    def test_a_closure_gives_its_function(self):
        self.assertEqual(_enclosing_named("kuna_analysis::listing::mask_limit::{closure#5}"), "kuna_analysis::listing::mask_limit")

    def test_a_named_function_gives_none(self):
        self.assertIsNone(_enclosing_named("kuna_analysis::listing::mask_limit"))
        self.assertIsNone(_enclosing_named("{closure#0}"))


if __name__ == "__main__":
    unittest.main()
