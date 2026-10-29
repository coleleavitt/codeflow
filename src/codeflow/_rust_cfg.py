"""Rust control-flow graphs from rustc's MIR, through the `rustc-driver` crate next to this package."""

import json
import os
import pathlib
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
        tail = "\n".join(proc.stderr.splitlines()[-40:])
        raise RuntimeError(f"{' '.join(cmd)} failed ({proc.returncode}):\n{tail}")
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


def rust_cfg(file: pathlib.Path, function: str) -> dict:
    """The MIR control-flow graph of `function` (a name or path suffix) defined in `file`."""
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
            "CODEFLOW_FN": function,
            "CODEFLOW_FILE": str(file),
            "CODEFLOW_OUT": str(out),
            "CARGO_TARGET_DIR": str(workspace / "target" / "codeflow"),
        }
        cmd = ["cargo", TOOLCHAIN, "rustc", "-p", package, *_target(file, package_dir, package), "--profile", "check"]
        _run([*cmd, "--", "--cfg", f"codeflow_probe_{uuid.uuid4().hex[:12]}"], workspace, env)
        if not out.exists():
            raise RuntimeError(f"cargo finished but the driver wrote no graph for {function} (was {package} rebuilt?)")
        return json.loads(out.read_text())
