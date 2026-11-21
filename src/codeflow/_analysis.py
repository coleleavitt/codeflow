"""Analyses over codeflow's graphs, the same for every language backend:

- dominators: which blocks every path from the entry passes before a block (Cooper, Harvey and
  Kennedy's iterative "A Simple, Fast Dominance Algorithm": faster than Lengauer-Tarjan on graphs
  of realistic size);
- def-use: where a variable is defined and read, and which definitions reach each read (reaching
  definitions, the data-flow slice ARISE (arXiv 2605.03117) found agents gain the most from);
- references: every site that names a symbol, classified by what it does there (a call, a write,
  a read, a reference taken, a type use, an import) -- what IDA and Ghidra call xrefs. LSP
  `references` returns bare locations, so the kind comes from the site's syntax.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

# ---- dominators ---------------------------------------------------------------------


def dominators(graph: dict) -> dict[int, int | None]:
    """The immediate dominator of every reachable block (``None`` for the entry)."""
    blocks = {b["id"]: b for b in graph["blocks"]}
    entry = graph["entry"] if graph["entry"] in blocks else graph["blocks"][0]["id"]
    order: list[int] = []
    seen: set[int] = set()
    stack = [(entry, iter(blocks[entry]["succ"]))]
    seen.add(entry)
    while stack:  # postorder, iteratively (deep graphs)
        node, edges = stack[-1]
        nxt = next((e["to"] for e in edges if e["to"] in blocks and e["to"] not in seen), None)
        if nxt is None:
            order.append(node)
            stack.pop()
        else:
            seen.add(nxt)
            stack.append((nxt, iter(blocks[nxt]["succ"])))
    index = {node: i for i, node in enumerate(order)}  # postorder number
    preds: dict[int, list[int]] = {node: [] for node in order}
    for node in order:
        for edge in blocks[node]["succ"]:
            if edge["to"] in preds:
                preds[edge["to"]].append(node)
    idom: dict[int, int | None] = {entry: entry}

    def intersect(a: int, b: int) -> int:
        while a != b:
            while index[a] < index[b]:
                a = idom[a]  # type: ignore[assignment]
            while index[b] < index[a]:
                b = idom[b]  # type: ignore[assignment]
        return a

    changed = True
    while changed:
        changed = False
        for node in reversed(order):  # reverse postorder
            if node == entry:
                continue
            processed = [p for p in preds[node] if p in idom]
            if not processed:
                continue
            new = processed[0]
            for other in processed[1:]:
                new = intersect(other, new)
            if idom.get(node) != new:
                idom[node] = new
                changed = True
    idom[entry] = None
    return idom


def dominator_notes(graph: dict) -> dict[int, list[str]]:
    """Each block's chain of dominators, nearest first: the blocks every path to it runs."""
    idom = dominators(graph)
    names = graph["names"]
    notes = {}
    for block in graph["blocks"]:
        chain, node = [], idom.get(block["id"])
        while node is not None:
            chain.append(names[node])
            node = idom.get(node)
        if chain:
            notes[block["id"]] = [f"dominated by {' ← '.join(chain)}"]
    return notes


# ---- def-use -------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    line: int
    col: int
    kind: str  # "def" | "use"
    how: str = ""  # "parameter", "+=" ...
    block: int | None = None  # the graph block it happens in, when the backend knows (MIR)


# Methods that change their receiver: a call of one defines the variable (it is used first).
_MUTATING_METHODS = {
    ".rs": {"push", "push_str", "push_back", "push_front", "insert", "extend", "extend_from_slice", "append", "clear",
            "remove", "pop", "pop_front", "pop_back", "retain", "sort", "sort_by", "sort_by_key", "sort_unstable",
            "sort_unstable_by", "dedup", "truncate", "drain", "splice", "reverse", "set", "take", "replace", "swap",
            "entry", "get_mut", "iter_mut", "resize", "fill", "insert_str", "split_off", "or_insert", "store"},
    ".py": {"append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse", "update", "setdefault", "add",
            "discard", "popitem", "appendleft", "extendleft", "popleft", "rotate", "write"},
    "js": {"push", "pop", "shift", "unshift", "splice", "sort", "reverse", "fill", "set", "delete", "add", "clear",
           "copyWithin", "assign"},
}


def _mutating(suffix: str) -> set[str]:
    return _MUTATING_METHODS.get(suffix, _MUTATING_METHODS["js"] if suffix not in (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh") else set())


_ASSIGN = r"(?:=(?![=>])|\+=|-=|\*=|/=|%=|\|=|&=|\^=|<<=|>>=|\*\*=|//=|\?\?=|\|\|=|&&=)"
_DECLARE = r"(?:\blet\s+(?:mut\s+)?|\bconst\s+|\bvar\s+|\bstatic\s+(?:mut\s+)?|\bauto\s+|\bmut\s+)"


def _blank_strings(line: str, suffix: str) -> str:
    """The line with string literals and the trailing comment blanked (columns kept)."""
    out, quote, i = [], None, 0
    comment = "#" if suffix == ".py" else "//"
    depth = 0  # inside a template literal's `${ ... }`: code
    while i < len(line):
        ch = line[i]
        if quote == "`" and line.startswith("${", i):
            out.append("  ")
            quote, depth = None, 1
            i += 2
            continue
        if depth and quote is None and ch in "{}":
            depth += 1 if ch == "{" else -1
            if depth == 0:
                out.append(" ")
                quote = "`"
                i += 1
                continue
        if quote:
            out.append(" ")
            if ch == "\\" and i + 1 < len(line):
                out.append(" ")
                i += 2
                continue
            if ch == quote:
                quote = None
        elif line.startswith(comment, i):
            break
        elif ch in "\"`" or (ch == "'" and (suffix != ".rs" or re.match(r"'(?:\\.|[^\\'])'", line[i:]))):
            quote = ch
            out.append(" ")
        else:
            out.append(ch)
        i += 1
    return "".join(out).ljust(len(line))


def python_events(file: Path, function: str, var: str) -> list[Event]:
    """Defs and uses of ``var`` (a name or a dotted attribute like ``self.x``) in a Python function."""
    tree = ast.parse(file.read_text(errors="replace"))
    short = function.split(".")[-1]
    fn = next((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == short), None)
    if fn is None:
        return []
    events: list[Event] = []
    for arg in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs, fn.args.vararg, fn.args.kwarg]:
        if arg is not None and arg.arg == var:
            events.append(Event(fn.lineno, arg.col_offset + 1, "def", "parameter"))

    def spelled(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            inner = spelled(node.value)
            return f"{inner}.{node.attr}" if inner else None
        return None

    def base_of(node: ast.AST) -> str | None:
        """The spelled variable a target like `x.a[i].b` changes."""
        while isinstance(node, (ast.Attribute, ast.Subscript)):
            if spelled(node) == var:
                return var
            node = node.value
        return spelled(node)

    for node in ast.walk(fn):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else []
        for target in targets:
            # `x.a = ..` / `x[i] = ..` change x without rebinding it: a definition of x.
            if isinstance(target, (ast.Attribute, ast.Subscript)) and spelled(target) != var and base_of(target) == var:
                events.append(Event(target.lineno, target.col_offset + 1, "def", "field" if isinstance(target, ast.Attribute) else "element"))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and spelled(node.func.value) == var
                and node.func.attr in _MUTATING_METHODS[".py"]):
            events.append(Event(node.lineno, node.col_offset + 1, "def", f"method {node.func.attr}"))
        if isinstance(node, ast.AugAssign) and spelled(node.target) == var:
            events.append(Event(node.lineno, node.col_offset + 1, "use"))
            events.append(Event(node.lineno, node.col_offset + 1, "def", "augmented"))
        elif isinstance(node, (ast.Name, ast.Attribute)) and spelled(node) == var:
            store = isinstance(node.ctx, (ast.Store, ast.Del))
            events.append(Event(node.lineno, node.col_offset + 1, "def" if store else "use"))
    seen: set[tuple] = set()
    unique = []
    # On one line the reads run first: `x = x + 1` reads the old x, then stores.
    for event in sorted(events, key=lambda e: (e.line, e.kind == "def", e.col)):
        key = (event.line, event.col, event.kind)
        if key not in seen:
            seen.add(key)
            unique.append(event)
    return unique


def mir_events(dataflow: list[dict], var: str, graph: dict | None = None) -> list[Event]:
    """The driver's MIR defs and uses of the named variable ``var``, each in its block (followed
    through the blocks the tidy pass merged), deduplicated by place."""
    merged = {int(k): v for k, v in ((graph or {}).get("merged_into") or {}).items()}
    kept = {b["id"] for b in (graph or {}).get("blocks", [])}
    seen: set[tuple] = set()
    events = []
    for item in dataflow:
        name = str(item.get("var"))
        # `self.x` is touched by an event on it, on a part of it (`self.x.y`), or on all of it (`self`).
        if not (name == var or name.startswith(var + ".") or var.startswith(name + ".")):
            continue
        how = str(item.get("how") or "")
        if name != var:
            how = ", ".join(filter(None, [how, f"via {name}"]))
        item = dict(item, how=how)
        block = item.get("block")
        if block is not None:
            block = merged.get(int(block), int(block))
            block = block if block in kept else None
        event = Event(int(item["line"]), int(item.get("col") or 1), str(item["kind"]), str(item.get("how") or ""), block)
        key = (event.line, event.kind, event.how, event.block)
        if key not in seen:
            seen.add(key)
            events.append(event)
    return events


def syntax_events(file: Path, first: int, last: int, var: str, header_line: int) -> list[Event]:
    """Defs and uses of ``var`` by syntax (Rust, JS/TS, C/C++): an assignment or declaration
    defines it, a parameter at the function's header line defines it at entry, anything else
    reads it."""
    lines = file.read_text(errors="replace").splitlines()
    c_like = file.suffix in (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh")
    mutating = _mutating(file.suffix)
    # Pointers to the variable (`p = &x`, `let r = &mut x`, `int *p = &x`): a write through one defines it.
    aliases: set[str] = set()
    holders: set[str] = set()  # variables with a pointer to it kept in a field (`h = S { p: &raw mut x }`)
    pointer = rf"&\s*(?:mut\s+|raw\s+mut\s+|raw\s+const\s+)?{re.escape(var)}(?![\w$.])"
    alias_of = re.compile(rf"\b(\w+)\s*(?::[^=]*)?=\s*{pointer}")
    kept_in = re.compile(rf"\b(\w+)\s*(?::[^=]*)?=\s*[\w:<>]*\s*\{{[^}}]*?\b(\w+)\s*:\s*{pointer}")
    field_set = re.compile(rf"\b(\w+)\s*(?:\.|->)\s*(\w+)\s*=\s*{pointer}")
    for number in range(max(first, 1), min(last, len(lines)) + 1):
        text = _blank_strings(lines[number - 1], file.suffix)
        for match in kept_in.finditer(text):
            aliases.add(f"{match.group(1)}.{match.group(2)}")
            holders.add(match.group(1))
        for match in field_set.finditer(text):
            aliases.add(f"{match.group(1)}.{match.group(2)}")
            holders.add(match.group(1))
        for match in alias_of.finditer(text):
            aliases.add(match.group(1))
    def spelled_alias(alias: str) -> str:  # `h.p` also reads `h->p` in C
        return r"\s*(?:\.|->)\s*".join(re.escape(part) for part in alias.split("."))
    through = [re.compile(rf"(?:\*\s*\(?\s*{spelled_alias(a)}(?![\w$])|(?<![\w$.]){spelled_alias(a)}\s*(?:->\s*\w+|\[[^\]]*\]))\s*{_ASSIGN}") for a in sorted(aliases)]
    # A holder (or a plain pointer) handed on lets the callee write through it.
    handed = [re.compile(rf"(?:&\s*(?:mut\s+)?{re.escape(h)}|[(,]\s*{re.escape(h)}\s*[,)])(?![\w$.])") for h in sorted(holders | {a for a in aliases if "." not in a})]
    word = re.compile(rf"(?<![\w.$]){re.escape(var)}(?![\w$])" if "." not in var else rf"(?<![\w$]){re.escape(var)}(?![\w$])")
    events: list[Event] = []
    in_params = False
    for number in range(max(first, 1), min(last, len(lines)) + 1):
        text = _blank_strings(lines[number - 1], file.suffix)
        if number == header_line:
            in_params = True
        for match in word.finditer(text):
            before, after = text[: match.start()], text[match.end():]
            col = match.start() + 1
            if in_params and number <= header_line + 6 and re.search(r"[(,]\s*(?:mut\s+|&\s*(?:mut\s+)?)?$", before) and re.match(r"\s*[:,)]", after):
                events.append(Event(number, col, "def", "parameter"))
            elif re.search(r"&\s*(?:raw\s+)?(?:mut|const)\s+$", before) or (c_like and re.search(r"&\s*$", before)):
                # A pointer taken: handing it to a call can change the variable; keeping it does not, yet.
                if before.count("(") > before.count(")") and not re.search(r"\{[^}]*$", before):
                    events.append(Event(number, col, "def", "address passed to a call" if c_like else "&mut passed on"))
            elif re.match(rf"\s*{_ASSIGN}", after):
                op = re.match(rf"\s*({_ASSIGN})", after).group(1)
                if op != "=":
                    events.append(Event(number, col, "use"))
                events.append(Event(number, col, "def", "" if op == "=" else op))
            elif re.search(rf"{_DECLARE}(?:\(\s*)?$", before) or re.search(r"\bfor\s*\(?\s*(?:const\s+|let\s+|var\s+|auto\s+)?(?:\(\s*)?$", before) or re.search(r"\b(?:Some|Ok|Err)\(\s*$", before) and re.search(r"\bif\s+let\b|\bwhile\s+let\b", before):
                events.append(Event(number, col, "def"))
            elif re.match(r"\s*(?:\+\+|--)", after) or re.search(r"(?:\+\+|--)\s*$", before):
                events.append(Event(number, col, "use"))
                events.append(Event(number, col, "def", "++/--"))
            elif re.match(rf"\s*(?:(?:\.|->)\s*\w+|\[[^\]]*\])+\s*{_ASSIGN}", after):
                events.append(Event(number, col, "use"))
                events.append(Event(number, col, "def", "field"))  # `x.a = ..`, `x[i] = ..`
            elif (method := re.match(r"\s*(?:\.|->)\s*(\w+)\s*(?:::<[^>]*>)?\s*\(", after)) and method.group(1) in mutating:
                events.append(Event(number, col, "use"))
                events.append(Event(number, col, "def", f"method {method.group(1)}"))
            else:
                events.append(Event(number, col, "use"))
        declares = alias_of.search(text) or kept_in.search(text) or field_set.search(text)
        for pattern in through if not declares else ():  # `int *p = &x` declares p, writes nothing
            for match in pattern.finditer(text):
                events.append(Event(number, match.start() + 1, "use"))
                events.append(Event(number, match.start() + 1, "def", "through a pointer"))
        for pattern in handed if not declares else ():
            for match in pattern.finditer(text):
                if text[: match.start()].count("(") > text[: match.start()].count(")"):
                    events.append(Event(number, match.start() + 1, "def", "a pointer to it passed to a call"))
        if in_params and "{" in text:
            in_params = False
    return events


def _blocks_of(graph: dict, line: int) -> list[int]:
    """The blocks whose lines contain ``line``, narrowest first (later in graph order on a tie):
    a compiler graph (MIR) spreads one source line over several blocks."""
    found = [(b["lines"][1] - b["lines"][0], -order, b["id"])
             for order, b in enumerate(graph["blocks"]) if b["lines"] and b["lines"][0] <= line <= b["lines"][1]]
    return [block for _, _, block in sorted(found)]


def def_use_notes(graph: dict, events: list[Event], var: str) -> tuple[dict[int, list[str]], list[str]]:
    """Per-block marks (``def x :L``, ``use x :L ← :L1, :L2``) from reaching definitions over the
    graph, and the uses no definition in the function reaches (a value from outside)."""
    entry = graph["entry"]
    placed: dict[int, list[Event]] = {}
    shown: dict[int, list[Event]] = {}
    for event in events:
        candidates = [event.block] if event.block is not None else _blocks_of(graph, event.line)
        if event.how == "parameter" and entry in {b["id"] for b in graph["blocks"]}:
            candidates = [entry]
        if not candidates:
            continue
        # A definition holds in every block of its line (the paths after it run there); a read is
        # placed in the narrowest one. Each is shown once.
        for block in candidates if event.kind == "def" else candidates[:1]:
            placed.setdefault(block, []).append(event)
        shown.setdefault(candidates[0], []).append(event)
    for items in placed.values():
        items.sort(key=lambda e: (e.line, e.kind == "def" and e.how != "parameter", e.col))
    gen: dict[int, set[int]] = {}
    kills: set[int] = set()
    for block, items in placed.items():
        last_def = None
        for event in items:
            if event.kind == "def":
                last_def = event.line
        if last_def is not None:
            gen[block] = {last_def}
            kills.add(block)
    blocks = {b["id"]: b for b in graph["blocks"]}
    preds: dict[int, list[int]] = {b: [] for b in blocks}
    for block in graph["blocks"]:
        for edge in block["succ"]:
            if edge["to"] in preds:
                preds[edge["to"]].append(block["id"])
    reach_in: dict[int, set[int]] = {b: set() for b in blocks}
    reach_out: dict[int, set[int]] = {b: set(gen.get(b, set())) for b in blocks}
    changed = True
    while changed:
        changed = False
        for block in blocks:
            incoming = set().union(*(reach_out[p] for p in preds[block])) if preds[block] else set()
            out = gen[block] if block in kills else incoming
            if incoming != reach_in[block] or out != reach_out[block]:
                reach_in[block], reach_out[block] = incoming, set(out)
                changed = True
    notes: dict[int, list[str]] = {}
    outside: list[str] = []
    for block, items in placed.items():
        current = set(reach_in[block])
        marks: list[str] = []
        visible = shown.get(block, [])
        for event in items:
            if event.kind == "def":
                mark = f"def {var} :{event.line}" + (f" ({event.how})" if event.how else "")
                current = {event.line}
            else:
                sources = sorted(current)
                mark = f"use {var} :{event.line} ← " + (", ".join(f":{s}" for s in sources) if sources else "(no definition in this function)")
                if not sources and event in visible:
                    outside.append(f":{event.line}")
            if event in visible:
                marks.append(mark)
        if marks:
            collapsed: list[str] = []
            for mark in marks:  # the same mark twice on a line: once, counted
                if collapsed and collapsed[-1].split("  (")[0] == mark:
                    count = int(collapsed[-1].rsplit("(", 1)[1].rstrip("×)")) + 1 if collapsed[-1].endswith("×)") else 2
                    collapsed[-1] = f"{mark}  ({count}×)"
                else:
                    collapsed.append(mark)
            notes[block] = collapsed
    return notes, sorted(set(outside), key=outside.index)


# ---- references (xrefs) ---------------------------------------------------------------

_KINDS = ("definition", "call", "write", "read", "reference taken", "type use", "import")


def classify(text: str, col: int, name: str, suffix: str, is_definition: bool, is_function: bool = False) -> str:
    """What the site at 1-based ``col`` of ``text`` does with ``name``; a function named without
    being called is taken as a value (stored, passed, registered as a callback)."""
    if is_definition:
        return "definition"
    code = _blank_strings(text, suffix)
    stripped = code.lstrip()
    if re.match(r"(?:pub(?:\([^)]*\))?\s+)?use\s|import\s|from\s+\S+\s+import\s|#\s*include\s", stripped) or re.match(r"(?:export\s+)?import\b", stripped):
        return "import"
    start = col - 1
    before, after = code[:start], code[start + len(name):]
    if re.match(r"\s*(?:::\s*<[^>]*>\s*)?!?\s*\(", after):
        return "call"
    if re.match(rf"\s*{_ASSIGN}", after) or re.match(r"\s*(?:\+\+|--)", after):
        return "write"
    if suffix != ".py" and re.match(r"\s*:(?!:)", after) and re.search(r"[{,]\s*$", before):
        return "write"  # a field initializer (`Runner { v: x }`, `{ key: value }`)
    if re.search(r"&\s*(?:mut\s+)?(?:[\w.]+\.)?$", before) or re.search(r"\baddressof\(\s*$", before):
        return "reference taken"
    if re.search(r"(?::|->|\bimpl\b[^{]*\bfor\s+|\bdyn\s+|\bimpl\s+|\bas\s+|<|\bnew\s+|\bextends\s+|\bimplements\s+)\s*(?:&\s*(?:mut\s+)?)?(?:[\w:]+::)?$", before) and name[:1].isupper():
        return "type use"
    if is_function and not re.search(r"(?:\.|::)\s*$", before):
        return "reference taken"
    return "read"


_ENCLOSING = {
    ".rs": re.compile(r"\bfn\s+(\w+)"),
    ".py": re.compile(r"^\s*(?:async\s+)?def\s+(\w+)"),
    "js": re.compile(r"\bfunction\s*\*?\s*(\w+)|^\s*(?:(?:public|private|protected|static|async|get|set)\s+)*#?(\w+)\s*\([^)]*\)\s*(?::[^={]+)?\{|\b(\w+)\s*[:=]\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>)"),
    "c": re.compile(r"^[\w][\w\s\*&:<>,~]*?\b([~\w:]+)\s*\([^;]*$"),
}


def _body_end(lines: list[str], start: int, suffix: str) -> int:
    """The last line (1-based) of the definition starting at ``start``: its indented block in
    Python, its matched braces elsewhere."""
    if suffix == ".py":
        indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
        end = start
        for index in range(start, len(lines)):
            text = lines[index]
            if text.strip() and len(text) - len(text.lstrip()) <= indent:
                break
            if text.strip():
                end = index + 1
        return end
    depth, opened = 0, False
    for index in range(start - 1, len(lines)):
        for ch in _blank_strings(lines[index], suffix):
            if ch == "{":
                depth, opened = depth + 1, True
            elif ch == "}":
                depth -= 1
                if opened and depth == 0:
                    return index + 1
        if not opened and lines[index].rstrip().endswith(";"):
            return index + 1  # a declaration without a body
    return len(lines)


def enclosing(lines: list[str], line: int, suffix: str) -> str:
    """The name of the innermost function whose body holds ``line`` (syntax only)."""
    family = suffix if suffix in (".rs", ".py") else "js" if suffix in {".js", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"} else "c"
    pattern = _ENCLOSING[family]
    for index in range(min(line, len(lines)) - 1, -1, -1):
        match = pattern.search(lines[index])
        if match:
            name = next(g for g in match.groups() if g)
            if name in ("if", "for", "while", "switch", "return", "catch", "match"):
                continue
            if index + 1 == line or _body_end(lines, index + 1, suffix) >= line:
                return name
    return "(top level)"


def render_references(name: str, sites: list[tuple[Path, int, int, str, str]], base: Path, source: str, limit: int = 40) -> str:
    """``sites``: (file, line, col, kind, enclosing function)."""
    counts = {kind: sum(1 for s in sites if s[3] == kind) for kind in _KINDS}
    summary = ", ".join(f"{n} {kind}{'s' if n != 1 and not kind.endswith('use') and kind != 'reference taken' else ''}" for kind, n in counts.items() if n)
    out = [f"references to `{name}`  [{len(sites)} sites: {summary}; {source}]"]
    for kind in _KINDS:
        group = [s for s in sites if s[3] == kind]
        if not group:
            continue
        out.append(f"{kind} ({len(group)})")
        for file, line, _col, _kind, owner in group[:limit]:
            try:
                rel = file.resolve().relative_to(base)
            except ValueError:
                rel = file
            try:
                text = " ".join(file.read_text(errors="replace").splitlines()[line - 1].split())
            except (OSError, IndexError):
                text = ""
            out.append(f"  {rel}:{line}  in {owner}  {text[:110]}")
        if len(group) > limit:
            out.append(f"  … {len(group) - limit} more")
    return "\n".join(out)
