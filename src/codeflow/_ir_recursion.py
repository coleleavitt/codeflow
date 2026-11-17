"""Unguarded recursion in a Rust crate, from the call graph of its LLVM IR.

A cycle in the call graph is unguarded when some path around it never passes a guard: a call to a
guard function, or code inlined from one. Release builds are checked too, where inlining moves
guards into their callers, by the debug locations that inlined code keeps.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import uuid
from dataclasses import dataclass, field

from ._rust_cfg import TOOLCHAIN, _package, _run, _rust_checkout_env, _target

_DEFINE = re.compile(r'@("?[^"(\s]+"?)\(')
_CALL = re.compile(r'(?:call|invoke)\b[^@%]*?(?:@("?[^"(\s]+"?)|(%[\w.]+))\(')
_LABEL = re.compile(r"label %([\w.\-]+)")
_BLOCK = re.compile(r"^([\w.\-]+):")
_DBG = re.compile(r"!dbg (!\d+)")
_META = re.compile(r"^(!\d+) = (?:distinct )?!(\w+)\((.*)\)\s*$")
_FIELD = re.compile(r'(\w+): ("(?:[^"\\]|\\.)*"|![\d]+|[^,]+)')
_ATTRS = re.compile(
    r"\b(noundef|nonnull|zeroext|signext|noalias|nocapture|readonly|writeonly|immarg|nofree|dead_on_unwind|writable|"
    r"fastcc|coldcc|tail|internal|private|hidden|unnamed_addr|dso_local|linkonce_odr|weak_odr|"
    r"align \d+|range\([^)]*\)|dereferenceable(?:_or_null)?\(\d+\)|captures\([^)]*\)|initializes\([^)]*\)|"
    r"sret\([^)]*\)|byval\([^)]*\))"
)


@dataclass
class Function:
    name: str
    blocks: dict[str, list[str]] = field(default_factory=dict)
    signature: tuple = ()


@dataclass
class Cycle:
    """One strongly connected set of functions, with a witness call for each step around it."""

    functions: list[str]
    calls: list[tuple[str, str, str]]  # (caller, callee, "file:line")
    instances: int = 1  # monomorphized copies with the same functions

    def __str__(self) -> str:
        steps = "\n".join(f"  {caller}  →  {callee}    {loc}" for caller, callee, loc in self.calls)
        copies = f" (×{self.instances} instances)" if self.instances > 1 else ""
        return f"{', '.join(self.functions)}{copies}\n{steps}"


@dataclass
class Recursion:
    crate: str
    profile: str
    guards: tuple[str, ...]
    cycles: list[Cycle]
    indirect_calls: int
    unresolved_indirect: list[str]
    ir: pathlib.Path

    def __str__(self) -> str:
        head = (
            f"{self.crate} ({self.profile}): {len(self.cycles)} unguarded recursive cycle(s), "
            f"guards {', '.join(self.guards) or 'none'}; {self.indirect_calls} indirect call sites"
        )
        if self.unresolved_indirect:
            head += f", {len(self.unresolved_indirect)} with no address-taken target of their type"
        return "\n\n".join([head, *(f"#{i + 1} {c}" for i, c in enumerate(self.cycles))])


def _strip_generics(text: str) -> str:
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _short(name: str) -> str:
    """`Type::method` for a demangled method path (the self type's last segment, no generic
    arguments), and the path without generic arguments for anything else."""
    if name.startswith("<"):
        depth = 0
        for i, ch in enumerate(name):
            depth += ch == "<"
            depth -= ch == ">"
            if depth == 0:
                inner, rest = name[1:i], name[i + 1 :]
                break
        else:
            return name
        self_type = _strip_generics(inner.split(" as ")[0]).rsplit("::", 1)[-1]
        return f"{self_type}{_strip_generics(rest)}".rstrip(":")
    return _strip_generics(name).rstrip(":")


def _demangle(names: list[str]) -> dict[str, str]:
    proc = subprocess.run(["llvm-cxxfilt"], input="\n".join(names) + "\n", capture_output=True, text=True, check=True)
    return dict(zip(names, proc.stdout.splitlines()))


def _paren_body(line: str, start: int) -> str:
    depth = 0
    for i in range(start, len(line)):
        if line[i] == "(":
            depth += 1
        elif line[i] == ")":
            depth -= 1
            if depth == 0:
                return line[start + 1 : i]
    return ""


def _types(args: str) -> tuple:
    parts, depth, cur = [], 0, ""
    for ch in args:
        depth += ch in "([{<"
        depth -= ch in ")]}>"
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return tuple((_ATTRS.sub("", p).split() or [""])[0] for p in parts if p.strip())


def _parse(ir: pathlib.Path):
    functions: dict[str, Function] = {}
    meta: dict[str, tuple[str, dict]] = {}
    referenced: set[str] = set()
    cur = None
    block = None
    with ir.open() as fh:
        for line in fh:
            if line.startswith("define "):
                m = _DEFINE.search(line)
                cur = Function(m.group(1).strip('"'))
                ret = _ATTRS.sub("", line[: m.start()].removeprefix("define ")).replace(" ", "")
                cur.signature = (ret, _types(_paren_body(line, m.end() - 1)))
                functions[cur.name] = cur
                block = "%entry"
                cur.blocks[block] = []
            elif cur is not None:
                if line.startswith("}"):
                    cur = None
                    continue
                m = _BLOCK.match(line)
                if m:
                    if list(cur.blocks) == ["%entry"] and not cur.blocks["%entry"]:
                        del cur.blocks["%entry"]
                    block = m.group(1)
                    cur.blocks[block] = []
                    continue
                cur.blocks[block].append(line)
                called = {m.group(1) for m in _CALL.finditer(line) if m.group(1)}
                for ref in re.findall(r'@("?[_A-Za-z$.][^"(\s,\]}]*"?)', line):
                    if ref not in called:
                        referenced.add(ref.strip('"'))
            elif line.startswith("!"):
                m = _META.match(line)
                if m:
                    meta[m.group(1)] = (m.group(2), dict(_FIELD.findall(m.group(3))))
            elif not line.startswith("declare "):
                for ref in re.findall(r'@("?[_A-Za-z$.][^"(\s,\]}]*"?)', line):
                    referenced.add(ref.strip('"'))
    return functions, meta, referenced


class _Locations:
    def __init__(self, meta: dict):
        self.meta = meta

    def _subprograms(self, loc: str):
        """Subprogram names of a location and of every location it was inlined at."""
        while loc in self.meta:
            kind, fields = self.meta[loc]
            scope = fields.get("scope")
            while scope in self.meta and self.meta[scope][0] != "DISubprogram":
                scope = self.meta[scope][1].get("scope")
            if scope in self.meta:
                f = self.meta[scope][1]
                yield f.get("linkageName", f.get("name", "")).strip('"'), f.get("name", "").strip('"')
            loc = fields.get("inlinedAt")

    def inlined_from(self, loc: str) -> list[str]:
        return [_strip_generics(name) for _, name in self._subprograms(loc)]

    def file_line(self, loc: str) -> str:
        if loc not in self.meta:
            return "?"
        fields = self.meta[loc][1]
        scope = fields.get("scope")
        while scope in self.meta and "file" not in self.meta[scope][1]:
            scope = self.meta[scope][1].get("scope")
        file = self.meta.get(self.meta[scope][1]["file"], ("", {}))[1] if scope in self.meta else {}
        name = file.get("filename", '"?"').strip('"')
        directory = file.get("directory", '""').strip('"')
        path = name if name.startswith("/") else os.path.join(directory, name)
        return f"{path}:{fields.get('line', '?')}"


def _emit(file: pathlib.Path, release: bool) -> tuple[pathlib.Path, str, pathlib.Path]:
    package_dir, package = _package(file)
    workspace = pathlib.Path(
        _run(["cargo", TOOLCHAIN, "locate-project", "--workspace", "--message-format", "plain"], package_dir).strip()
    ).parent
    target_dir = workspace / "target" / "codeflow-ir"
    env = os.environ | {"CARGO_TARGET_DIR": str(target_dir)} | _rust_checkout_env(workspace)
    marker = f"codeflow_probe_{uuid.uuid4().hex[:12]}"
    cmd = ["cargo", TOOLCHAIN, "rustc", "-p", package, *_target(file, package_dir, package)]
    if release:
        cmd.append("--release")
    flags = ["--emit=llvm-ir", "-C", "codegen-units=1", "-C", "debuginfo=line-tables-only", "--cfg", marker]
    _run([*cmd, "--", *flags], workspace, env)
    crate = package.replace("-", "_")
    candidates = sorted(target_dir.rglob(f"{crate}-*.ll"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise RuntimeError(f"cargo finished but wrote no LLVM IR for {crate} under {target_dir}")
    return candidates[-1], crate, package_dir


def recursion(
    file: str | os.PathLike[str],
    guards: tuple[str, ...] | list[str] = (),
    *,
    release: bool = False,
) -> Recursion:
    """Recursive cycles in the crate of `file` that some path goes around without a guard.

    `guards` are function names (last path segment, e.g. "check_stack"). A path is guarded once it
    executes a guard: a call to one, code inlined from one, or a call to a function that runs a guard
    before its first branch. Indirect calls go to every address-taken function of the crate with the
    same LLVM type. Only cycles with a function of the crate itself are reported."""
    ir, crate, _ = _emit(pathlib.Path(file).resolve(), release)
    functions, meta, referenced = _parse(ir)
    locations = _Locations(meta)
    callees = {m.group(1).strip('"') for f in functions.values() for b in f.blocks.values() for line in b for m in _CALL.finditer(line) if m.group(1)}
    names = _demangle(sorted(set(functions) | callees))
    guard_set = set(guards)

    def last(symbol: str) -> str:
        return _short(names.get(symbol, symbol)).rsplit("::", 1)[-1]

    def guard_line(line: str, entry_guarded: set[str]) -> bool:
        for m in _CALL.finditer(line):
            if m.group(1):
                callee = m.group(1).strip('"')
                if last(callee) in guard_set or callee in entry_guarded:
                    return True
        dbg = _DBG.search(line)
        return bool(dbg) and any(name in guard_set for name in locations.inlined_from(dbg.group(1)))

    # Functions that run a guard before their first branch, to a fixed point: a call to one guards
    # the path through it like a guard does.
    entry_guarded: set[str] = set()
    changed = True
    while changed:
        changed = False
        for fn in functions.values():
            if fn.name in entry_guarded:
                continue
            for line in next(iter(fn.blocks.values()), []):
                if guard_line(line, entry_guarded):
                    entry_guarded.add(fn.name)
                    changed = True
                    break
                if line.lstrip().startswith(("br ", "switch ", "ret ", "invoke ", "unreachable")):
                    break

    taken = {name: functions[name].signature for name in referenced if name in functions}
    indirect_sites, unresolved = 0, []
    edges: dict[str, dict[str, str]] = {}
    for fn in functions.values():
        out: dict[str, str] = {}
        seen, queue = set(), list(fn.blocks)[:1]
        while queue:
            label = queue.pop()
            if label in seen:
                continue
            seen.add(label)
            lines = fn.blocks[label]
            guarded = False
            loc = "?"
            for line in lines:
                if guard_line(line, entry_guarded):
                    guarded = True
                    break
                dbg = _DBG.search(line)
                if dbg:
                    loc = locations.file_line(dbg.group(1))
                for m in _CALL.finditer(line):
                    if m.group(1):
                        out.setdefault(m.group(1).strip('"'), loc)
                    elif "asm " not in line[: m.start(2)]:
                        indirect_sites += 1
                        ret = re.sub(r"^(call|invoke)", "", m.group(0)[: m.start(2) - m.start()])
                        sig = (_ATTRS.sub("", ret).replace(" ", ""), _types(_paren_body(line, m.end() - 1)))
                        targets = [name for name, s in taken.items() if s == sig]
                        if not targets:
                            unresolved.append(f"{_short(names.get(fn.name, fn.name))}  {loc}")
                        for t in targets:
                            out.setdefault(t, f"{loc} (indirect)")
            if not guarded:
                queue.extend(lbl for line in lines for lbl in _LABEL.findall(line) if lbl in fn.blocks)
        edges[fn.name] = {c: l for c, l in out.items() if c in functions}

    cycles: dict[tuple[str, ...], Cycle] = {}
    for component in _components(edges):
        if not any(names.get(f, f).startswith(crate) or f"<{crate}::" in names.get(f, f) or f" {crate}::" in names.get(f, f) for f in component):
            continue
        members = set(component)
        calls, at = [], component[0]
        order = _witness(edges, members, at)
        for caller, callee in zip(order, order[1:]):
            calls.append((_short(names.get(caller, caller)), _short(names.get(callee, callee)), edges[caller][callee]))
        key = tuple(sorted({_short(names.get(f, f)) for f in component}))
        if key in cycles:
            cycles[key].instances += 1
        else:
            cycles[key] = Cycle(list(key), calls)
    return Recursion(crate, "release" if release else "debug", tuple(guards), list(cycles.values()), indirect_sites, unresolved, ir)


def _components(edges: dict[str, dict[str, str]]) -> list[list[str]]:
    order, seen = [], set()
    for start in edges:
        if start in seen:
            continue
        seen.add(start)
        stack = [(start, iter(edges[start]))]
        while stack:
            node, it = stack[-1]
            for nxt in it:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append((nxt, iter(edges[nxt])))
                    break
            else:
                stack.pop()
                order.append(node)
    reverse: dict[str, list[str]] = {n: [] for n in edges}
    for n, out in edges.items():
        for m in out:
            reverse[m].append(n)
    assigned, components = set(), []
    for start in reversed(order):
        if start in assigned:
            continue
        component, stack = [], [start]
        assigned.add(start)
        while stack:
            node = stack.pop()
            component.append(node)
            for prev in reverse[node]:
                if prev not in assigned:
                    assigned.add(prev)
                    stack.append(prev)
        if len(component) > 1 or start in edges[start]:
            components.append(component)
    return components


def _witness(edges: dict[str, dict[str, str]], members: set[str], start: str) -> list[str]:
    """A shortest path from `start` around the cycle back to `start`."""
    prev, queue = {start: None}, [start]
    for node in queue:
        for nxt in edges[node]:
            if nxt == start:
                path = [node]
                while prev[path[-1]] is not None:
                    path.append(prev[path[-1]])
                return [*reversed(path), start]
            if nxt in members and nxt not in prev:
                prev[nxt] = node
                queue.append(nxt)
    return [start]
