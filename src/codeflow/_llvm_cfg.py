"""C/C++ control-flow graphs from LLVM IR: the file compiled with its real flags (compile_commands.json)
by clang at -O0 -g. Macros are expanded, and a call to a `noreturn` function ends its path, the way
rustc's MIR does for Rust."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path

_DROP_WITH_VALUE = {"-o", "-MF", "-MT", "-MQ"}
_DROP = re.compile(r"^(-c|-MD|-MMD|-MP|-O\S*|-g\S*|-W\S*|-pedantic\S*|-flto\S*|-fsanitize\S*|-fprofile\S*)$")
_DILOC = re.compile(r"^!(\d+) = (?:distinct )?!DILocation\(line: (\d+), column: (\d+)")
_LABEL = re.compile(r"^([\w.\-$]+):(?:\s|$)")
_DBG = re.compile(r"!dbg !(\d+)")
_CALL = re.compile(r"\b(?:call|invoke)\b[^@%]*@\"?([\w.$]+)\"?\(")
_INDIRECT = re.compile(r"\b(?:call|invoke)\b[^@]*?\s%[\w.\-$]+\(")
# Exception-handling plumbing the source never spells out.
_EH_PLUMBING = {"__cxa_begin_catch", "__cxa_end_catch", "__cxa_rethrow", "__cxa_free_exception"}
_TARGET = re.compile(r"label %([\w.\-$]+)")


def compile_command(file: Path) -> tuple[Path, list[str]] | None:
    """The file's compile command from the nearest compile_commands.json (the project root, ``build*/``)."""
    for directory in file.parents:
        for database in [directory / "compile_commands.json", *sorted(directory.glob("build*/compile_commands.json"))]:
            if not database.is_file():
                continue
            for entry in json.loads(database.read_text()):
                if Path(entry["directory"], entry["file"]).resolve() == file:
                    argv = entry.get("arguments") or shlex.split(entry["command"])
                    return Path(entry["directory"]), argv
    return None


def _ir(file: Path, directory: Path, argv: list[str]) -> str:
    cpp = file.suffix in {".cc", ".cpp", ".cxx", ".hpp", ".hh"} or "++" in Path(argv[0]).name
    args, skip = [], False
    for arg in argv[1:]:
        if skip:
            skip = False
            continue
        if arg in _DROP_WITH_VALUE:
            skip = True
            continue
        if _DROP.match(arg) or Path(directory, arg).resolve() == file:
            continue
        args.append(arg)
    command = ["clang++" if cpp else "clang", *args, "-w", "-Qunused-arguments", "-O0", "-g",
               "-fno-discard-value-names", "-S", "-emit-llvm", "-o", "-", str(file)]
    run = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=600)
    if run.returncode != 0:
        raise RuntimeError("clang could not compile the file to LLVM IR:\n" + run.stderr[-1500:])
    return run.stdout


def _demangle(names: list[str]) -> dict[str, str]:
    mangled = [n for n in names if n.startswith("_Z")]
    if not mangled:
        return {}
    run = subprocess.run(["llvm-cxxfilt"], input="\n".join(mangled), capture_output=True, text=True)
    return dict(zip(mangled, run.stdout.splitlines()))


def llvm_cfg(file: Path, function: str) -> dict | None:
    """The function's graph from LLVM IR, or None when the file has no compile command."""
    found = compile_command(file)
    if found is None:
        return None
    ir = _ir(file, *found).splitlines()
    locations = {}
    for line in ir:
        match = _DILOC.match(line)
        if match:
            locations[match.group(1)] = (int(match.group(2)), int(match.group(3)))
    starts = [(i, m.group(1)) for i, line in enumerate(ir) if line.startswith("define ") and (m := re.search(r'@"?([\w.$]+)"?\(', line))]
    readable = _demangle([name for _, name in starts])
    def matches(name: str) -> bool:
        shown = readable.get(name, name).split("(")[0]
        return shown == function or shown.endswith("::" + function) or name == function
    chosen = [(i, name) for i, name in starts if matches(name)]
    if not chosen:
        raise LookupError(f"no function {function!r} in the LLVM IR of {file} (inlined away, unused static, or misspelled)")
    begin, name = chosen[0]
    end = next(i for i in range(begin, len(ir)) if ir[i] == "}")
    body = ir[begin + 1 : end]
    readable |= _demangle(sorted({m.group(1) for line in body if (m := _CALL.search(line))} - set(readable)))

    order: list[str] = []
    text: dict[str, list[str]] = {}
    current = "entry"
    for line in body:
        label = _LABEL.match(line)
        if label:
            current = label.group(1)
            continue
        if line.strip() and not line.lstrip().startswith(";"):
            if current not in text:
                order.append(current)
                text[current] = []
            text[current].append(line.strip())
    ids = {label: n for n, label in enumerate(order)}
    exit_id = len(order)
    blocks = []
    for label in order:
        lines_seen, steps, succ = [], [], []
        terminator = text[label][-1]
        noreturn = None
        for k, instr in enumerate(text[label]):
            dbg = _DBG.search(instr)
            if dbg and dbg.group(1) in locations:
                lines_seen.append(locations[dbg.group(1)][0])
            call = _CALL.search(instr)
            if call and not call.group(1).startswith("llvm.") and call.group(1) not in _EH_PLUMBING:
                callee = readable.get(call.group(1), call.group(1)).split("(")[0]
                steps.append(f"call {callee}(..)")
                if k + 1 < len(text[label]) and text[label][k + 1].startswith("unreachable"):
                    noreturn = callee
            elif call is None and _INDIRECT.search(instr):
                steps.append("call (indirect)(..)")
        dbg = _DBG.search(terminator)
        at = locations.get(dbg.group(1)) if dbg else None
        test = None
        if terminator.startswith("br i1"):
            then, other = _TARGET.findall(terminator)
            kind, label_text = "switch", ""
            succ = [{"to": ids[then], "label": "true"}, {"to": ids[other], "label": "false"}]
            # Where this branch's operand starts: the leftmost instruction on the branch's line
            # (the block holds only that operand's loads and compare).
            columns = [
                locations[m.group(1)][1]
                for instr in text[label]
                if (m := _DBG.search(instr)) and m.group(1) in locations and at and locations[m.group(1)][0] == at[0]
            ]
            test = (at[0], min(columns)) if columns else at
        elif terminator.startswith("br "):
            kind, label_text = "goto", ""
            succ = [{"to": ids[_TARGET.findall(terminator)[0]], "label": ""}]
        elif terminator.startswith("switch "):
            joined = " ".join(text[label][text[label].index(next(t for t in text[label] if t.startswith("switch "))):])
            default = _TARGET.findall(joined)[0]
            cases = re.findall(r"i\d+ (-?\d+), label %([\w.\-$]+)", joined)
            kind, label_text = "switch", ""
            succ = [{"to": ids[target], "label": f"case {value}"} for value, target in cases]
            succ.append({"to": ids[default], "label": "default"})
        elif terminator.startswith("ret"):
            kind, label_text = "return", "return"
            succ = [{"to": exit_id, "label": "return"}]
        elif terminator.startswith("unreachable"):
            kind = "raise" if noreturn else "unreachable"
            label_text = f"{noreturn} never returns" if noreturn else "unreachable"
            if noreturn:
                steps = [s for s in steps if s != f"call {noreturn}(..)"]
                succ = [{"to": exit_id, "label": "never returns"}]
        elif terminator.startswith("invoke") or "invoke" in terminator:
            normal, unwind = re.search(r"to label %([\w.\-$]+) unwind label %([\w.\-$]+)", " ".join(text[label][-2:])).groups()
            kind, label_text = "call", ""
            succ = [{"to": ids[normal], "label": ""}, {"to": ids[unwind], "label": "exception"}]
        elif terminator.startswith("resume"):
            kind, label_text = "raise", "rethrow"
            succ = [{"to": exit_id, "label": "throw"}]
        else:
            kind, label_text = "goto", terminator.split()[0]
        blocks.append({
            "id": ids[label], "lines": [min(lines_seen), max(lines_seen)] if lines_seen else None,
            "kind": kind, "label": label_text, "steps": steps, "test": [test[0], test[1], test[0], 0] if test else None,
            "succ": succ, "cleanup": False,
        })
    blocks.append({"id": exit_id, "lines": None, "kind": "exit", "label": "exit", "succ": [], "cleanup": False})
    _mark_cleanup(blocks, exit_id)
    first_line = min((b["lines"][0] for b in blocks if b["lines"]), default=0)
    return {"function": readable.get(name, name).split("(")[0], "file": str(file), "line": first_line,
            "entry": 0, "blocks": blocks, "negation_folded": False, "source": "LLVM IR -O0"}


def _mark_cleanup(blocks: list[dict], exit_id: int) -> None:
    """Landing pads that only run destructors and rethrow are cleanup (Rust's unwind paths): marked
    so the default view hides them. An exception edge into a real `catch` stays."""
    by_id = {b["id"]: b for b in blocks}
    # Blocks that can end some other way than a rethrow (return, never-returning call, a catch).
    alive = {b["id"] for b in blocks if b["kind"] in ("return", "unreachable") or (b["kind"] == "raise" and b["label"] != "rethrow")}
    changed = True
    while changed:
        changed = False
        for b in blocks:
            if b["id"] not in alive and b["id"] != exit_id and any(e["to"] in alive for e in b["succ"]):
                alive.add(b["id"])
                changed = True
    for b in blocks:
        for e in b["succ"]:
            if e["label"] == "exception" and e["to"] not in alive:
                e["label"] = "unwind"
                todo = [e["to"]]
                while todo:
                    n = todo.pop()
                    if n == exit_id or by_id[n]["cleanup"]:
                        continue
                    by_id[n]["cleanup"] = True
                    todo.extend(x["to"] for x in by_id[n]["succ"])
