"""Rust control-flow graphs from rustc's MIR, through the `rustc-driver` crate next to this package."""

import json
import os
import pathlib
import re
import subprocess
import tempfile
import tomllib
import uuid

TOOLCHAIN = "+rustc-master"
DRIVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "rustc-driver"
DRIVER = DRIVER_DIR / "target" / "release" / "codeflow-rustc-driver"
DRIVER_COMMIT = DRIVER.with_name(DRIVER.name + ".commit")


def _run(cmd: list[str], cwd: pathlib.Path, env: dict | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        lines = proc.stderr.splitlines()
        # The driver's own explanation (no match, ambiguous, several closures) leads the error.
        reasons = [line.strip() for line in lines if "codeflow:" in line]
        tail = "\n".join(lines[-40:])
        raise RuntimeError("\n".join(reasons) + ("\n" if reasons else "") + f"{' '.join(cmd)} failed ({proc.returncode}):\n{tail}")
    return proc.stdout


def _driver() -> pathlib.Path:
    version = _run(["rustc", TOOLCHAIN, "-vV"], DRIVER_DIR)
    commit = next(line.split(": ", 1)[1] for line in version.splitlines() if line.startswith("commit-hash: "))
    if not DRIVER.exists() or not DRIVER_COMMIT.exists() or DRIVER_COMMIT.read_text() != commit:
        _run(["cargo", TOOLCHAIN, "build", "--release"], DRIVER_DIR)
        DRIVER_COMMIT.write_text(commit)
    return DRIVER


def _package(file: pathlib.Path) -> tuple[pathlib.Path, str]:
    for directory in file.parents:
        manifest = directory / "Cargo.toml"
        if manifest.exists():
            package = tomllib.loads(manifest.read_text()).get("package")
            if package:
                return directory, package["name"]
    raise RuntimeError(f"no Cargo.toml with [package] above {file}")


def _target(file: pathlib.Path, package_dir: pathlib.Path, package: str) -> list[str]:
    rel = file.relative_to(package_dir).parts
    if rel[:2] == ("src", "bin"):
        return ["--bin", rel[2].removesuffix(".rs")]
    if file.name == "main.rs":
        return ["--bin", package]
    return ["--lib"]


def rust_cfg(file: pathlib.Path, function: str | int) -> dict:
    """The MIR control-flow graph of `function` defined in `file`: a name or path suffix (closures as
    `outer::{closure#0}`), or a 1-based line, which picks the innermost fn, closure or async block around it."""
    file = file.resolve()
    driver = _driver()
    package_dir, package = _package(file)
    workspace = pathlib.Path(
        _run(["cargo", TOOLCHAIN, "locate-project", "--workspace", "--message-format", "plain"], package_dir).strip()
    ).parent
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "cfg.json"
        env = os.environ | {
            "RUSTC_WORKSPACE_WRAPPER": str(driver),
            # An int, or "LINE:COL" (between closures on one line), selects by position; else by name.
            "CODEFLOW_LINE" if isinstance(function, int) or re.fullmatch(r"\d+:\d+", str(function)) else "CODEFLOW_FN": str(function),
            "CODEFLOW_FILE": str(file),
            "CODEFLOW_OUT": str(out),
            "CARGO_TARGET_DIR": str(workspace / "target" / "codeflow"),
        }
        cmd = ["cargo", TOOLCHAIN, "rustc", "-p", package, *_target(file, package_dir, package), "--profile", "check"]
        _run([*cmd, "--", "--cfg", f"codeflow_probe_{uuid.uuid4().hex[:12]}"], workspace, env)
        if not out.exists():
            raise RuntimeError(f"cargo finished but the driver wrote no graph for {function} (was {package} rebuilt?)")
        return json.loads(out.read_text())
