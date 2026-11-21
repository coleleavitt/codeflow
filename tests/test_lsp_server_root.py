"""The language server opens at the project a file belongs to, not the git root: a Rust workspace in a
subdirectory (kuna keeps its Cargo workspace in decompiler/) has no Cargo.toml at the git root, so
rust-analyzer finds no crate and the call hierarchy comes back empty."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeflow import _lsp_client  # noqa: E402


class ServerRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        ws = self.repo / "decompiler"
        (ws / "crates" / "app" / "src").mkdir(parents=True)
        (ws / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
        (ws / "crates" / "app" / "Cargo.toml").write_text('[package]\nname = "app"\nversion = "0.0.0"\n')
        self.file = ws / "crates" / "app" / "src" / "lib.rs"
        self.file.write_text("pub fn f() {}\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_rust_file_opens_at_its_cargo_workspace(self):
        self.assertEqual(_lsp_client._project_root(self.file), self.repo / "decompiler")

    def test_a_crate_without_a_workspace_opens_at_its_crate(self):
        (self.repo / "decompiler" / "Cargo.toml").unlink()
        self.assertEqual(_lsp_client._project_root(self.file), self.repo / "decompiler" / "crates" / "app")

    def test_other_languages_keep_the_git_root(self):
        py = self.repo / "tool.py"
        py.write_text("x = 1\n")
        self.assertEqual(_lsp_client._project_root(py), self.repo)

    def test_no_repo_falls_back_to_the_file_directory(self):
        loose = Path(self.tmp.name) / "loose.rs"
        loose.write_text("fn x() {}\n")
        self.assertEqual(_lsp_client._project_root(loose), Path(self.tmp.name))


if __name__ == "__main__":
    unittest.main()
