"""Control-flow graphs in one shape for every language, and their rendering.

A graph is a dict: {"function", "file", "line", "entry", "blocks": [{"id", "lines": [first, last] | None,
"kind", "label", "succ": [{"to", "label"}], "cleanup"}]}. Backends: Python (``ast``, here), JavaScript
(``jsbeautify cfg``), C (clang's CFG), Rust (MIR through ``rustc_public``, ``_rust_cfg``).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

# ---- Python (ast) ------------------------------------------------------------


_KNOWN_NORETURN = {"sys.exit", "exit", "quit", "os._exit", "os.abort", "os.execv", "os.execvp", "os.execve"}


def callee_position(call: ast.Call) -> tuple[int, int]:
    """Where a language server's hover names the called function: the attribute or the name."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.end_lineno, func.end_col_offset - len(func.attr) + 1
    return func.lineno, func.col_offset + 1


def statement_calls(file: Path, name: str) -> list[tuple[int, int, str]]:
    """The calls that stand as statements in ``name`` (the ones that can end a path), with the
    position to hover and the callee text."""
    source = file.read_text(errors="replace")
    function = _find_def(ast.parse(source, str(file)), name)
    if function is None:
        return []
    found = []
    for node in ast.walk(function):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            line, col = callee_position(node.value)
            found.append((line, col, ast.get_source_segment(source, node.value.func) or ""))
    return found


def local_noreturn(file: Path) -> set[str]:
    """Functions in the file annotated ``-> NoReturn`` / ``-> Never``."""
    names = set()
    for node in ast.walk(ast.parse(file.read_text(errors="replace"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            if re.search(r"\b(NoReturn|Never)\b", ast.unparse(node.returns)):
                names.add(node.name)
    return names


class _Builder:
    def __init__(self, source: str, noreturn: set[tuple[int, int]] = frozenset()) -> None:
        self.source = source
        self.noreturn = noreturn
        self.blocks: list[dict] = []
        self.exit = self.new("exit", "exit")

    def new(self, kind: str = "goto", label: str = "") -> dict:
        block = {"id": len(self.blocks), "lines": None, "kind": kind, "label": label, "succ": [], "cleanup": False}
        self.blocks.append(block)
        return block

    def text(self, node: ast.AST, limit: int = 60) -> str:
        segment = ast.get_source_segment(self.source, node) or ast.dump(node)[:limit]
        segment = " ".join(segment.split())
        return segment if len(segment) <= limit else segment[: limit - 1] + "…"

    @staticmethod
    def cover(block: dict, node: ast.AST) -> None:
        first, last = node.lineno, getattr(node, "end_lineno", node.lineno)
        if block["lines"] is None:
            block["lines"] = [first, last]
        else:
            block["lines"] = [min(block["lines"][0], first), max(block["lines"][1], last)]

    @staticmethod
    def link(source: dict, target: dict, label: str = "") -> None:
        source["succ"].append({"to": target["id"], "label": label})

    def seq(self, body: list[ast.stmt], cur: dict | None, ctx: dict) -> dict | None:
        """Lay out ``body`` from ``cur``; the open block after it, or None when every path left.
        ``ctx``: ``loops`` [(header, after)], ``raise_to`` (the innermost handler dispatch or finally,
        else exit) and ``return_to`` (the innermost finally, else exit)."""
        for stmt in body:
            if cur is None:  # unreachable code after return/raise/break: its own block
                cur = self.new("goto", "unreachable")
            cur = self.stmt(stmt, cur, ctx)
        return cur

    def leave(self, cur: dict, target: dict, label: str) -> None:
        """A return or raise: a finally on the way runs first and then carries the exit on."""
        if target.get("_finally"):
            target["_carries"].add(label)
        self.link(cur, target, label)

    def head(self, cur: dict, node: ast.AST, kind: str, label: str) -> dict:
        """``cur`` ends at ``node``'s test: a fresh block when ``cur`` already holds statements."""
        if cur["lines"] is not None or cur["kind"] != "goto":
            nxt = self.new()
            self.link(cur, nxt)
            cur = nxt
        cur["kind"], cur["label"] = kind, label
        self.cover(cur, node)
        return cur

    def stmt(self, node: ast.stmt, cur: dict, ctx: dict) -> dict | None:
        if isinstance(node, ast.If):
            test = self.head(cur, node.test, "switch", f"if {self.text(node.test)}")
            after = self.new()
            then = self.new()
            self.link(test, then, "true")
            end = self.seq(node.body, then, ctx)
            if end is not None:
                self.link(end, after)
            if node.orelse:
                other = self.new()
                self.link(test, other, "false")
                end = self.seq(node.orelse, other, ctx)
                if end is not None:
                    self.link(end, after)
            else:
                self.link(test, after, "false")
            return after if any(after["id"] == e["to"] for b in self.blocks for e in b["succ"]) else None
        if isinstance(node, (ast.While, ast.For, ast.AsyncFor)):
            if isinstance(node, ast.While):
                label, at = f"while {self.text(node.test)}", node.test
            else:
                label, at = f"for {self.text(node.target)} in {self.text(node.iter)}", node.iter
            header = self.new("switch", label)
            self.cover(header, at)
            self.link(cur, header)
            after = self.new()
            body = self.new()
            self.link(header, body, "loop")
            end = self.seq(node.body, body, dict(ctx, loops=ctx['loops'] + [(header, after)]))
            if end is not None:
                self.link(end, header, "next")
            if node.orelse:
                other = self.new()
                self.link(header, other, "done")
                end = self.seq(node.orelse, other, ctx)
                if end is not None:
                    self.link(end, after)
            else:
                self.link(header, after, "done")
            return after
        if isinstance(node, ast.Break):
            self.cover(cur, node)
            cur["label"] = "break"
            self.link(cur, ctx["loops"][-1][1], "break")
            return None
        if isinstance(node, ast.Continue):
            self.cover(cur, node)
            cur["label"] = "continue"
            self.link(cur, ctx["loops"][-1][0], "continue")
            return None
        if isinstance(node, ast.Return):
            self.cover(cur, node)
            cur["kind"], cur["label"] = "return", f"return {self.text(node.value)}" if node.value else "return"
            self.leave(cur, ctx["return_to"], "return")
            return None
        if isinstance(node, ast.Raise):
            self.cover(cur, node)
            cur["kind"], cur["label"] = "raise", f"raise {self.text(node.exc)}" if node.exc else "raise"
            self.leave(cur, ctx["raise_to"], "raise")
            return None
        if isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            after = self.new()
            final = None
            if node.finalbody:
                final = self.new("goto", "finally")
                final["_finally"], final["_carries"] = True, set()
            outer = dict(ctx, raise_to=final or ctx["raise_to"], return_to=final or ctx["return_to"])
            dispatch = self.new("switch", "except dispatch")
            start = self.new("goto", "try")
            start["lines"] = [node.lineno, node.lineno]
            self.link(cur, start)
            self.link(start, dispatch, "exception")
            body = self.new()
            self.link(start, body)
            end = self.seq(node.body, body, dict(outer, raise_to=dispatch))
            if node.orelse and end is not None:
                other = self.new("goto", "else")
                self.link(end, other)
                end = self.seq(node.orelse, other, outer)
            if end is not None:
                self.link(end, final or after)
            for handler in node.handlers:
                caught = self.new("goto", f"except {self.text(handler.type)}" if handler.type else "except")
                caught["lines"] = [handler.lineno, handler.lineno]
                self.link(dispatch, caught, self.text(handler.type, 30) if handler.type else "any")
                hend = self.seq(handler.body, caught, outer)
                if hend is not None:
                    self.link(hend, final or after)
            self.leave(dispatch, outer["raise_to"], "unhandled")
            if final is not None:
                final["lines"] = [node.finalbody[0].lineno - 1, node.finalbody[0].lineno - 1]
                fend = self.seq(node.finalbody, final, ctx)
                if fend is None:
                    return None
                normal = any(e["to"] == final["id"] and e["label"] not in final["_carries"] for b in self.blocks for e in b["succ"])
                if normal:
                    self.link(fend, after)
                if "return" in final["_carries"]:
                    self.leave(fend, ctx["return_to"], "then return")
                if final["_carries"] & {"raise", "unhandled", "never returns"}:
                    self.leave(fend, ctx["raise_to"], "then re-raise")
                if not normal:
                    return None
            return after
        if isinstance(node, (ast.With, ast.AsyncWith)):
            self.cover(cur, node.items[0].context_expr)
            return self.seq(node.body, cur, ctx)
        if isinstance(node, ast.Match):
            test = self.head(cur, node.subject, "switch", f"match {self.text(node.subject)}")
            after = self.new()
            for case in node.cases:
                arm = self.new()
                self.cover(arm, case.pattern)
                self.link(test, arm, self.text(case.pattern, 30) + (f" if {self.text(case.guard, 20)}" if case.guard else ""))
                end = self.seq(case.body, arm, ctx)
                if end is not None:
                    self.link(end, after)
            if not any(isinstance(c.pattern, ast.MatchAs) and c.pattern.pattern is None and c.guard is None for c in node.cases):
                self.link(test, after, "no match")
            return after
        if isinstance(node, ast.Assert):
            self.cover(cur, node)
            nxt = self.new()
            cur["kind"], cur["label"] = "assert", f"assert {self.text(node.test)}"
            self.link(cur, nxt, "ok")
            self.leave(cur, ctx["raise_to"], "AssertionError")
            return nxt
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and callee_position(node.value) in self.noreturn:
            self.cover(cur, node)
            cur["kind"], cur["label"] = "raise", f"{self.text(node.value.func)}() never returns"
            self.leave(cur, ctx["raise_to"], "never returns")
            return None
        self.cover(cur, node)  # a plain statement (nested defs and classes included)
        return cur


def name_at(file: Path, line: int) -> str:
    """The innermost function around ``line``, as ``Class.method`` or ``function``."""
    tree = ast.parse(file.read_text(errors="replace"))
    best: tuple[int, str] | None = None

    def visit(node: ast.AST, owner: str | None) -> None:
        nonlocal best
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.lineno <= line <= child.end_lineno:
                    span = child.end_lineno - child.lineno
                    if best is None or span <= best[0]:
                        best = (span, f"{owner}.{child.name}" if owner else child.name)
                visit(child, None)
            else:
                visit(child, owner)

    visit(tree, None)
    if best is None:
        raise LookupError(f"no function around {file}:{line}")
    return best[1]


def _find_def(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    parts = name.split(".")
    found = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == parts[-1]:
            if len(parts) == 1:
                return node
            found = found or node
            for klass in ast.walk(tree):
                if isinstance(klass, ast.ClassDef) and klass.name == parts[-2] and node in klass.body:
                    return node
    return found


def py_cfg(file: Path, name: str, noreturn: set[tuple[int, int]] = frozenset()) -> dict:
    """``noreturn``: the (line, col) of calls that never return (``callee_position``)."""
    source = file.read_text(errors="replace")
    function = _find_def(ast.parse(source, str(file)), name)
    if function is None:
        raise LookupError(f"no function {name!r} in {file}")
    builder = _Builder(source, set(noreturn))
    entry = builder.new("goto", "")
    end = builder.seq(function.body, entry, {"loops": [], "raise_to": builder.exit, "return_to": builder.exit})
    if end is not None:
        builder.link(end, builder.exit, "fall off")
        if end["kind"] == "goto" and not end["label"]:
            end["kind"], end["label"] = "return", "return None (falls off the end)"
    for block in builder.blocks:
        block.pop("_finally", None)
        block.pop("_carries", None)
    return {"function": function.name, "file": str(file), "line": function.lineno, "entry": entry["id"],
            "blocks": builder.blocks}


# ---- shared: simplify and render ----------------------------------------------


def simplify(graph: dict, *, cleanup: bool = False) -> dict:
    """Drop unwind-only blocks (unless ``cleanup``), unreachable blocks and empty pass-through blocks."""
    blocks = {b["id"]: dict(b, succ=list(b["succ"])) for b in graph["blocks"]}
    if not cleanup:
        for block in blocks.values():
            block["succ"] = [e for e in block["succ"] if e.get("label") != "unwind" and not blocks.get(e["to"], {}).get("cleanup")]
    # Follow empty, label-less pass-through blocks to their single target.
    def through(target: int, seen: tuple = ()) -> int:
        block = blocks.get(target)
        if block and block["lines"] is None and not block["label"] and len(block["succ"]) == 1 and target not in seen and target != graph["entry"]:
            return through(block["succ"][0]["to"], seen + (target,))
        return target
    for block in blocks.values():
        block["succ"] = [dict(e, to=through(e["to"])) for e in block["succ"]]
    order: list[int] = []
    seen: set[int] = set()
    def visit(node: int) -> None:  # iterative-enough for real functions; recursion depth = nesting
        seen.add(node)
        for edge in blocks[node]["succ"]:
            if edge["to"] in blocks and edge["to"] not in seen:
                visit(edge["to"])
        order.append(node)
    visit(graph["entry"])
    order.reverse()
    exits = [i for i in order if blocks[i]["kind"] == "exit"]
    order = [i for i in order if i not in exits] + exits
    kept = [blocks[i] for i in order]
    names = {b["id"]: f"B{n}" for n, b in enumerate(kept)}
    return dict(graph, blocks=kept, names=names)


def _wrap_label(label: str, width: int = 100) -> list[str]:
    """A block's label on lines of about ``width`` columns, broken only between calls (after a comma) or at a ` · `."""
    pieces = re.split(r"(?<=, )|(?= · )", label)
    lines, current = [], ""
    for piece in pieces:
        if current and len(current) + len(piece) > width:
            lines.append(current.rstrip())
            current = piece.lstrip(" ·").lstrip() if piece.startswith(" · ") else piece
            if piece.startswith(" · "):
                current = "· " + current
        else:
            current += piece
    lines.append(current.rstrip())
    return lines


def render(graph: dict, base: Path | None = None) -> str:
    file = graph["file"]
    if base is not None:
        try:
            file = str(Path(file).resolve().relative_to(base))
        except ValueError:
            pass
    names = graph["names"]
    preds: dict[int, list[str]] = {}
    for block in graph["blocks"]:
        for edge in block["succ"]:
            preds.setdefault(edge["to"], []).append(names[block["id"]])
    backend = f", {graph['source']}" if graph.get("source") else ""
    out = [f"{graph['function']}  {file}:{graph['line']}   [control flow, {len(graph['blocks'])} blocks{backend}]"]
    for block in graph["blocks"]:
        name = names[block["id"]]
        lines = block["lines"]
        where = (f":{lines[0]}" if lines[0] == lines[1] else f":{lines[0]}-{lines[1]}") if lines else ""
        label = block["label"] or ("exit" if block["kind"] == "exit" else "")
        wrapped = _wrap_label(label)
        head = f"{name:<4}{where:<12}{wrapped[0]}"
        if len(preds.get(block["id"], [])) > 1:
            wrapped[-1] += f"    ← {', '.join(sorted(set(preds[block['id']]), key=lambda s: int(s[1:])))}"
            head = f"{name:<4}{where:<12}{wrapped[0]}"
        out.append(head.rstrip())
        out.extend(f"{'':16}{line}".rstrip() for line in wrapped[1:])
        succ = block["succ"]
        for index, edge in enumerate(succ):
            target = names.get(edge["to"], "?")
            back = "  ↺" if edge["to"] in names and int(target[1:]) <= int(name[1:]) else ""
            text = f"{edge['label']} → {target}" if edge["label"] else f"→ {target}"
            out.append(f"    {'└─' if index == len(succ) - 1 else '├─'} {text}{back}")
    return "\n".join(out)


# ---- Rust: from MIR to a source-level graph -------------------------------------

# Calls the source never spells out: await plumbing, format_args internals, auto-deref.
_HIDDEN_CALLS = ("Pin::new_unchecked", "IntoFuture>::into_future", "{closure#", "Arguments::", "must_use(",
                 " as Deref>::deref(", " as DerefMut>::deref_mut(",
                 # `tracing`'s level gate in `info!("...")`: with no format arguments nothing else of the
                 # macro has a source line, so the gate alone would stay behind as a call of the code around it.
                 "<Level as PartialOrd<LevelFilter>>::", "LevelFilter::current(")


def _back_edges(blocks: dict[int, dict], entry: int) -> set[tuple[int, int]]:
    """The edges that close a loop (an edge to a block still on the depth-first path)."""
    back: set[tuple[int, int]] = set()
    state = {entry: 1}  # 1: on the path, 2: done
    stack = [(entry, iter(blocks[entry]["succ"]))]
    while stack:
        n, edges = stack[-1]
        for e in edges:
            m = e["to"]
            if m not in blocks:
                continue
            if state.get(m) == 1:
                back.add((n, m))
            elif m not in state:
                state[m] = 1
                stack.append((m, iter(blocks[m]["succ"])))
                break
        else:
            state[n] = 2
            stack.pop()
    return back


def _postdominators(blocks: dict[int, dict], entry: int) -> dict[int, set[int]]:
    """Each block's post-dominators (blocks every path from it to an exit passes through), leaving out the
    edges that close a loop: a `continue` and the loop body only meet again at the loop head, which is
    where a branch's paths join only if the branch has no code of its own after it."""
    back = _back_edges(blocks, entry)
    forward = {n: [e["to"] for e in blocks[n]["succ"] if e["to"] in blocks and (n, e["to"]) not in back] for n in blocks}
    nodes = list(blocks)
    exits = {n for n in nodes if not forward[n]}
    pdom = {n: ({n} if n in exits else set(nodes)) for n in nodes}
    changed = True
    while changed:
        changed = False
        for n in nodes:
            if n in exits:
                continue
            new = {n} | set.intersection(*(pdom[s] for s in forward[n]))
            if new != pdom[n]:
                pdom[n], changed = new, True
    return pdom


def _line_before(blocks: dict[int, dict], preds: dict[int, list[int]], start: int) -> int | None:
    """The last source line of the nearest block before ``start`` that has one."""
    seen, todo = {start}, list(preds.get(start, []))
    while todo:
        n = todo.pop(0)
        if n in seen or n not in blocks:
            continue
        seen.add(n)
        if blocks[n]["lines"]:
            return blocks[n]["lines"][1]
        todo.extend(preds.get(n, []))
    return None


def _closing_braces_only(block: dict, source: list[str]) -> bool:
    """Whether the block's source lines are all closing braces (`}`, `});`): the end of a scope, where rustc
    drops what it still owns and branches on a drop flag."""
    lines = block["lines"]
    if not lines or not source or lines[1] > len(source):
        return False
    return all(source[row - 1].strip() and set(source[row - 1].strip()) <= set("});,") for row in range(lines[0], lines[1] + 1))


def _code_between(blocks: dict[int, dict], start: int, join: int, source: list[str] | None = None) -> bool:
    """Whether a block with a source line lies on a path from ``start`` to ``join`` (``join`` itself aside).
    With ``source``, a block that only sits on a closing brace is no code."""
    seen, todo = {start, join}, [e["to"] for e in blocks[start]["succ"]]
    while todo:
        n = todo.pop()
        if n in seen or n not in blocks:
            continue
        seen.add(n)
        if blocks[n]["lines"] and not (source and _closing_braces_only(blocks[n], source)):
            return True
        todo.extend(e["to"] for e in blocks[n]["succ"])
    return False


def ir_tidy(graph: dict, *, cleanup: bool = False, level: str = "source") -> dict:
    """A compiler IR graph (Rust MIR, LLVM IR) in source terms. ``level="source"``: the async body from its start, each `.await` one
    step, macro internals (blocks with no source line) collapsed, drops and await plumbing hidden,
    straight-line runs merged, each branch labelled with its source line. ``level="async"``: the same,
    keeping the async state machine (the state dispatch and its resume edges; each `.await` as a
    Ready/Pending branch whose Pending edge suspends). ``level="mir"``: the compiler's graph as it is."""
    if level == "mir":
        return graph if cleanup else dict(graph, blocks=[
            dict(b, succ=[e for e in b["succ"] if e["label"] != "unwind"]) for b in graph["blocks"] if not b["cleanup"]])
    keep_async = level == "async"
    graph = json.loads(json.dumps(graph))  # the passes below edit blocks and edges in place
    try:
        source = Path(graph["file"]).read_text(errors="replace").splitlines()
    except OSError:
        source = []
    blocks = {b["id"]: dict(b, succ=list(b["succ"])) for b in graph["blocks"]}
    entry = graph["entry"]
    if not cleanup:
        for b in blocks.values():
            b["succ"] = [e for e in b["succ"] if e["label"] != "unwind" and not blocks[e["to"]]["cleanup"]]
    start = blocks[entry]
    if "async state" in start["label"]:
        if keep_async:
            start["label"] = "async state dispatch"
            for e in start["succ"]:
                if e["label"] == "otherwise":
                    e["label"] = "invalid state"
        else:
            entry = next(e["to"] for e in start["succ"] if e["label"] == "start")
    for b in blocks.values():
        targets = {e["to"]: e for e in b["succ"]}
        b["succ"] = [e for e in b["succ"] if not (e["label"] == "otherwise" and blocks[e["to"]]["kind"] == "unreachable")]
        if b["kind"] == "switch" and "of Poll<" in b["label"]:
            if keep_async:
                b["label"] = "await: poll"
                for e in b["succ"]:
                    if e["label"] == "Pending" and blocks[e["to"]]["kind"] == "return":
                        blocks[e["to"]]["label"] = "suspend (Pending): yield to the executor"
            else:
                b["succ"] = [e for e in b["succ"] if e["label"] == "Ready"]
                b["kind"], b["label"] = "goto", "await"
                for e in b["succ"]:
                    e["label"] = ""
        elif b["kind"] == "drop" or (b["kind"] == "call" and any(h in b["label"] for h in _HIDDEN_CALLS)):
            b["kind"], b["label"] = "goto", ""
        elif b["kind"] == "goto" and b["label"] == "goto":
            b["label"] = ""
        if b["kind"] in ("call", "goto", "assert"):
            for e in b["succ"]:
                if e["label"] in ("ok",):
                    e["label"] = ""
        del targets
    # Branches with no source line are a macro's internals: jump to where their paths join.
    live = _reachable(blocks, entry)
    sub = {n: blocks[n] for n in live}
    pdom = _postdominators(sub, entry)
    back = _back_edges(sub, entry)
    for n in live:
        b = blocks[n]
        # A branch with a way back to the loop head (a `continue`) is control flow, not a macro's internals.
        # (A drop-flag branch at the end of a scope sits on its closing brace: it is no source branch either.)
        macro_internal = b["lines"] is None or (b["label"].startswith("switch") and _closing_braces_only(b, source))
        if b["kind"] == "switch" and macro_internal and len(b["succ"]) > 1 and not any((n, e["to"]) in back for e in b["succ"]):
            joins = sorted(pdom[n] - {n}, key=lambda m: len(pdom[m]), reverse=True)
            # Code of its own between the branch and its join (`if !matches!(..) { call() }`) is no macro internal.
            if joins and not _code_between(blocks, n, joins[0], source):
                b["kind"], b["label"], b["succ"] = "goto", "", [{"to": joins[0], "label": ""}]
    # A branch with no source line whose paths never meet again (a macro's condition, like the `matches!`
    # in `if a && matches!(..) { continue }`) leads to different real code: keep it, under the line it follows.
    live = _reachable(blocks, entry)
    preds: dict[int, list[int]] = {}
    for n in live:
        for e in blocks[n]["succ"]:
            preds.setdefault(e["to"], []).append(n)
    for n in live:
        b = blocks[n]
        if b["kind"] == "switch" and b["lines"] is None and len(b["succ"]) > 1:
            line = _line_before(blocks, preds, n)
            if line:
                b["lines"] = [line, line]
                b["macro_condition"] = True
    # Branch labels from the source. Each MIR branch tests one operand (`a || b` is two branches),
    # and the driver gives that operand's span: the label names it. rustc lowers `!c` by branching
    # on `c` with the targets swapped, so an operand preceded by `!` gets its edges flipped back.
    for b in blocks.values():
        if b["kind"] != "switch" or not b["lines"] or not source or b["label"].startswith(("await", "async state")):
            continue
        row_no = b["test"][0] if b.get("test") else b["lines"][0]  # the branch's own line, not the block's first
        line = source[row_no - 1].strip().rstrip("{").strip()
        if not line:
            continue
        condition = re.sub(r"^(?:\}\s*else\s+)?(?:if|while)\s+", "", line)
        boolean = sorted(e["label"] for e in b["succ"]) == ["false", "true"]
        tested = None
        span = b.get("test") if graph.get("negation_folded", True) else None
        if span and span[0] == span[2] and span[0] <= len(source):
            row = source[span[0] - 1]
            tested = row[span[1] - 1 : span[3] - 1]
            if boolean and graph.get("negation_folded", True) and row[: span[1] - 1].rstrip().endswith("!"):
                tested = "!" + tested
                for e in b["succ"]:
                    e["label"] = "true" if e["label"] == "false" else "false"
        elif boolean and condition.startswith("!") and graph.get("negation_folded", True):
            for e in b["succ"]:
                e["label"] = "true" if e["label"] == "false" else "false"
        shown = line if len(line) <= 70 else line[:69] + "…"
        b["label"] = shown if not tested or tested.strip() == condition.strip() else f"{shown}   [tests {tested}]"
        if b.get("macro_condition"):
            b["label"] = f"{shown}   [tests the macro's condition]"
    if not graph.get("negation_folded", True):
        # LLVM IR: each `&&`/`||` operand is its own branch, starting at the leftmost column of its
        # block's instructions on that line; it runs to the next operator at its depth.
        for b in blocks.values():
            test = b.get("test")
            if b["kind"] != "switch" or not test or len(b["succ"]) != 2 or not source or test[0] > len(source):
                continue
            row = source[test[0] - 1]
            if not re.search(r"&&|\|\|", row):
                continue
            operand = _operand_at(row, test[1] - 1)
            # A macro's instructions carry the macro's column: a span holding an operator is no operand.
            if operand and not re.search(r"&&|\|\|", operand) and operand not in b["label"][: len(operand)]:
                b["label"] = f"{b['label']}   [tests {operand}]"
    # MIR sends every `return` through one shared block: the block that sets the value says it.
    for b in blocks.values():
        if not b["label"] and b["lines"] and source:
            last = source[b["lines"][1] - 1].strip()
            if re.match(r"return\b", last):
                b["label"] = last.rstrip(";")
    # Merge straight-line runs: a single-exit block into its single-entry successor.
    live = _reachable(blocks, entry)
    preds: dict[int, int] = {}
    for n in live:
        for e in blocks[n]["succ"]:
            preds[e["to"]] = preds.get(e["to"], 0) + 1
    steps = {n: list(blocks[n].get("steps") or []) + ([blocks[n]["label"]] if blocks[n]["label"] else []) for n in live}
    merged_into: dict[int, int] = {}
    for n in sorted(live, key=lambda m: m != entry):
        b = blocks[n]
        while (
            len(b["succ"]) == 1
            and not b["succ"][0]["label"]
            and b["kind"] in ("goto", "call", "assert")
            and preds.get(b["succ"][0]["to"], 0) == 1
            and b["succ"][0]["to"] != n
            and b["succ"][0]["to"] != entry
            and b["succ"][0]["to"] not in merged_into
        ):
            nxt = blocks[b["succ"][0]["to"]]
            merged_into[nxt["id"]] = n
            steps[n] += steps.get(nxt["id"], [])
            if nxt["lines"]:
                b["lines"] = nxt["lines"] if not b["lines"] else [min(b["lines"][0], nxt["lines"][0]), max(b["lines"][1], nxt["lines"][1])]
            b["kind"], b["succ"] = nxt["kind"], nxt["succ"]
        b["label"] = _summarize(steps[n])
    kept = [blocks[n] for n in _reachable(blocks, entry) if n not in merged_into]
    # A macro's diverging call (`panic_fmt` of `assert!`, `unreachable!`) carries no line of its own: it sits
    # on the line of the code before it.
    kept_by_id = {b["id"]: b for b in kept}
    kept_preds: dict[int, list[int]] = {}
    for b in kept:
        for e in b["succ"]:
            kept_preds.setdefault(e["to"], []).append(b["id"])
    for b in kept:
        if b["lines"] is None and b["label"] and b["kind"] != "exit":
            line = _line_before(kept_by_id, kept_preds, b["id"])
            if line:
                b["lines"] = [line, line]
    for b in kept:
        if b["kind"] == "assert" and b["label"].startswith("assert: `async fn` resumed"):
            b["kind"], b["succ"] = "raise", [{"to": -1, "label": "panic"}]
            b["label"] = "panic: " + b["label"].removeprefix("assert: ")
    exit_block = {"id": -1, "lines": None, "kind": "exit", "label": "exit", "succ": [], "cleanup": False}
    if any(b["kind"] == "exit" for b in kept):
        return dict(graph, entry=entry, blocks=kept)
    for b in kept:
        if b["kind"] == "return" and not b["succ"]:
            b["succ"] = [{"to": -1, "label": "return"}]
    return dict(graph, entry=entry, blocks=kept + [exit_block])


def _operand_at(row: str, start: int) -> str:
    """The `&&`/`||` operand starting at ``start`` (a `!` just before it included). A start on the
    operator itself (the right operand's branch) steps past it."""
    while row.startswith(("&&", "||"), start) or (start < len(row) and row[start] == " "):
        start += 2 if row.startswith(("&&", "||"), start) else 1
    begin = start
    while begin > 0 and row[begin - 1] in "!( " and row[begin - 1] != "(":
        begin -= 1
    while begin < start and row[begin] == " ":
        begin += 1
    depth, i = 0, start
    while i < len(row):
        ch = row[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and (row.startswith("&&", i) or row.startswith("||", i) or ch in "?;,"):
            break
        i += 1
    return row[begin:i].strip()


def _reachable(blocks: dict[int, dict], entry: int) -> list[int]:
    seen, todo = [], [entry]
    while todo:
        n = todo.pop()
        if n in seen or n not in blocks:
            continue
        seen.append(n)
        todo.extend(e["to"] for e in blocks[n]["succ"])
    return seen


def _summarize(steps: list[str]) -> str:
    """A merged block's steps: its calls in order (a call repeated back to back as `name ×N`), then the rest."""
    calls: list[list] = []
    rest = []
    for step in steps:
        if step.startswith("call "):
            name = step[5:].removesuffix("(..)")
            if calls and calls[-1][0] == name:
                calls[-1][1] += 1
            else:
                calls.append([name, 1])
        elif step not in ("return",):
            rest.append(step)
    parts = []
    if calls:
        parts.append("calls " + ", ".join(name if count == 1 else f"{name} ×{count}" for name, count in calls))
    parts += rest
    if "return" in steps:
        parts.append("return")
    return " · ".join(parts)


rust_tidy = ir_tidy
