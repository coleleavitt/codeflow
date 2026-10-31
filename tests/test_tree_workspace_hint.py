"""A call tree that finds nothing says when the file is outside the language server's workspace."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import codeflow  # noqa: E402


class OutsideTheWorkspace(unittest.TestCase):
    def test_a_file_elsewhere_gets_the_explanation(self):
        hint = codeflow._outside_workspace_hint(Path("/elsewhere/src/main.rs"), Path("/work/project"))
        self.assertIn("outside the language server's workspace (/work/project)", hint)

    def test_a_file_inside_gets_none(self):
        self.assertEqual(codeflow._outside_workspace_hint(Path("/work/project/src/a.rs"), Path("/work/project")), "")


if __name__ == "__main__":
    unittest.main()
