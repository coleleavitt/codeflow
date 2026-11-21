#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["tree-sitter>=0.25", "tree-sitter-rust>=0.23"]
# ///
"""Statement-level control-flow graph of one Rust function from its syntax (tree-sitter), in
codeflow's shape: the fallback when the crate does not compile (MIR needs a build), and the
resolver that turns `Type::method` into a definition line.

    rcfg.py FILE NAME|LINE          ->  the graph as JSON on stdout
    rcfg.py FILE NAME --lines       ->  the definition lines NAME matches, one per line
"""

import json
import re
import sys
from pathlib import Path

import tree_sitter_rust
from tree_sitter import Language, Parser

path, wanted = Path(sys.argv[1]), sys.argv[2]
source = path.read_bytes()
tree = Parser(Language(tree_sitter_rust.language())).parse(source)
DIVERGING_MACROS = {"panic", "unreachable", "todo", "unimplemented", "bail"}


def text(node, limit=60):
    value = " ".join(source[node.start_byte:node.end_byte].decode(errors="replace").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


def short_type(node):
    """`Runner<T>` / `crate::a::Runner` → `Runner`."""
    return re.split(r"[<\s]", text(node, 400).split("::")[-1])[0] if node is not None else None


def qualified(fn):
    """`fn` with its owners: `Type::name`, `Trait::name`, `module::name` forms it answers to."""
    name = text(fn.child_by_field_name("name"), 200)
    forms = [name]
    owner = fn.parent
    while owner is not None:
        if owner.type == "impl_item":
            for field in ("type", "trait"):
                ty = short_type(owner.child_by_field_name(field))
                if ty:
                    forms.append(f"{ty}::{name}")
        elif owner.type in ("trait_item", "mod_item"):
            forms.append(f"{text(owner.child_by_field_name('name'), 200)}::{name}")
        owner = owner.parent
    return forms


functions = [n for n in walk(tree.root_node) if n.type == "function_item" and n.child_by_field_name("body") is not None]


def matching(query):
    tail = query.split("::")
    found = []
    for fn in functions:
        forms = qualified(fn)
        # A query matches a form exactly, or a form's trailing `Owner::name` (a longer path given).
        if query in forms or (len(tail) > 2 and "::".join(tail[-2:]) in forms):
            found.append(fn)
    return found


if "--lines" in sys.argv:
    for fn in matching(wanted):
        print(fn.start_point[0] + 1, " / ".join(qualified(fn)[1:] or qualified(fn)))
    sys.exit(0)

if wanted.split(":")[0].isdigit():  # a line (or LINE:COL): the innermost fn around it
    line = int(wanted.split(":")[0])
    around = [n for n in functions if n.start_point[0] + 1 <= line <= n.end_point[0] + 1]
    target = min(around, key=lambda n: n.end_byte - n.start_byte) if around else None
    if target is None:
        print(f"no function around {path}:{line}", file=sys.stderr)
        sys.exit(3)
else:
    found = matching(wanted)
    if not found:
        print(f"no function {wanted} in {path}", file=sys.stderr)
        sys.exit(3)
    if len(found) > 1:
        choices = ", ".join(f"{qualified(fn)[-1]} (line {fn.start_point[0] + 1})" for fn in found)
        print(f"`{wanted}` is ambiguous in {path}: {choices}; pass one of these or a line", file=sys.stderr)
        sys.exit(3)
    target = found[0]

blocks = []


def block(kind="goto", label=""):
    b = {"id": len(blocks), "lines": None, "kind": kind, "label": label, "succ": [], "cleanup": False}
    blocks.append(b)
    return b


def cover(b, node):
    first, last = node.start_point[0] + 1, node.end_point[0] + 1
    b["lines"] = [first, last] if b["lines"] is None else [min(b["lines"][0], first), max(b["lines"][1], last)]


def link(a, b, label=""):
    a["succ"].append({"to": b["id"], "label": label})


def reached(b):
    return any(e["to"] == b["id"] for x in blocks for e in x["succ"])


exit_block = block("exit", "exit")


def head(cur, node, kind, label):
    if cur["lines"] is not None or cur["kind"] != "goto":
        nxt = block()
        link(cur, nxt)
        cur = nxt
    cur["kind"], cur["label"] = kind, label
    cover(cur, node)
    return cur


# Code that does not run here: closures, async blocks and nested items have their own graphs.
_OWN_GRAPH = {"closure_expression", "async_block", "function_item", "impl_item", "trait_item", "mod_item"}


def early_exits(node):
    """`?` operators and diverging macros in an expression (not inside closures)."""
    tries, diverges = 0, None
    stack = [node]
    while stack:
        n = stack.pop()
        if n is not node and n.type in _OWN_GRAPH:
            continue
        if n.type == "try_expression":
            tries += 1
        elif n.type == "macro_invocation":
            macro = text(n.child_by_field_name("macro"), 40).split("::")[-1]
            if macro in DIVERGING_MACROS and diverges is None:
                diverges = n
        stack.extend(n.children)
    return tries, diverges


def plain(node, cur):
    """A statement or expression with no control flow of its own: its `?` edges and a diverging macro."""
    cover(cur, node)
    tries, diverges = early_exits(node)
    if tries:
        link(cur, exit_block, "? returns the error" if tries == 1 else f"? returns the error ({tries}×)")
    if diverges is not None:
        cur["kind"], cur["label"] = "raise", f"{text(diverges.child_by_field_name('macro'), 30)}!"
        link(cur, exit_block, "panics")
        return None
    return cur


def body(node, cur, ctx):
    """A block's statements (or one expression)."""
    if node is None:
        return cur
    if node.type != "block":
        return expr(node, cur, ctx)
    for child in node.named_children:
        if child.type in ("line_comment", "block_comment", "label"):
            continue
        if cur is None:
            cur = block("goto", "unreachable")
        cur = stmt(child, cur, ctx)
    return cur


def stmt(node, cur, ctx):
    t = node.type
    if t == "expression_statement":
        return expr(node.named_children[0], cur, ctx) if node.named_children else cur
    if t == "let_declaration":
        value = node.child_by_field_name("value")
        alternative = node.child_by_field_name("alternative")
        if value is not None and value.type in _CONTROL:
            cur = expr(value, cur, ctx)
            if cur is None:
                return None
            cover(cur, node)
        else:
            cur = plain(node, cur)
            if cur is None:
                return None
        if alternative is not None:  # let-else: the else block diverges
            test = head(cur, node, "switch", f"let {text(node.child_by_field_name('pattern'))} else")
            after, other = block(), block()
            link(test, after, "matches")
            link(test, other, "else")
            body(alternative, other, ctx)
            return after
        return cur
    if t in _OWN_GRAPH or t.endswith("_item") or t == "attribute_item":
        return cur
    return expr(node, cur, ctx)


def loop_label(node):
    label = next((c for c in node.children if c.type == "label"), None)
    return text(label) if label is not None else None


def loop(node, cur, ctx, label, at, inner_body):
    header = block("switch", label)
    cover(header, at)
    link(cur, header)
    after, inner = block(), block()
    link(header, inner, "loop")
    if node.type != "loop_expression":
        link(header, after, "done")
    name = loop_label(node)
    loops = dict(ctx.get("loops", {}))
    loops[None] = (after, header)
    if name:
        loops[name] = (after, header)
    end = body(inner_body, inner, dict(ctx, loops=loops))
    if end is not None:
        link(end, header, "next")
    return after if reached(after) else None


def expr(node, cur, ctx):
    t = node.type
    if t == "block":
        return body(node, cur, ctx)
    if t == "if_expression":
        cond = node.child_by_field_name("condition")
        cur = plain(cond, cur) if early_exits(cond)[0] else cur
        if cur is None:
            return None
        test = head(cur, cond, "switch", f"if {text(cond)}")
        after, then = block(), block()
        link(test, then, "true")
        end = body(node.child_by_field_name("consequence"), then, ctx)
        if end is not None:
            link(end, after)
        alternative = node.child_by_field_name("alternative")
        other = block()
        link(test, other, "false")
        if alternative is not None:
            inner = alternative.named_children[0] if alternative.type == "else_clause" else alternative
            end = expr(inner, other, ctx)
            if end is not None:
                link(end, after)
        else:
            link(other, after)
        return after if reached(after) else None
    if t == "match_expression":
        value = node.child_by_field_name("value")
        cur = plain(value, cur) if early_exits(value)[0] else cur
        if cur is None:
            return None
        test = head(cur, value, "switch", f"match {text(value)}")
        after = block()
        for arm in node.child_by_field_name("body").named_children:
            if arm.type != "match_arm":
                continue
            pattern = arm.child_by_field_name("pattern")
            start = block()
            cover(start, pattern)
            link(test, start, f"case {text(pattern, 40)}")
            end = expr(arm.child_by_field_name("value"), start, ctx)
            if end is not None:
                link(end, after)
        return after if reached(after) else None
    if t == "loop_expression":
        return loop(node, cur, ctx, "loop", node, node.child_by_field_name("body"))
    if t == "while_expression":
        cond = node.child_by_field_name("condition")
        return loop(node, cur, ctx, f"while {text(cond)}", cond, node.child_by_field_name("body"))
    if t == "for_expression":
        value = node.child_by_field_name("value")
        pattern = node.child_by_field_name("pattern")
        return loop(node, cur, ctx, f"for {text(pattern, 30)} in {text(value)}", value, node.child_by_field_name("body"))
    if t == "return_expression":
        cur = plain(node, cur)
        if cur is None:
            return None
        value = node.named_children[0] if node.named_children else None
        cur["kind"], cur["label"] = "return", f"return {text(value)}" if value is not None else "return"
        link(cur, exit_block, "return")
        return None
    if t in ("break_expression", "continue_expression"):
        cover(cur, node)
        word = "break" if t == "break_expression" else "continue"
        label = next((c for c in node.children if c.type == "label"), None)
        target_loop = ctx.get("loops", {}).get(text(label) if label is not None else None)
        cur["label"] = f"{word} {text(label)}" if label is not None else word
        if target_loop is not None:
            link(cur, target_loop[0] if word == "break" else target_loop[1], word)
        return None
    if t in _CONTROL_WRAPPERS and node.named_children:
        # `x = match ..`, `(if ..)`: the control expression inside decides the paths.
        inner = next((c for c in node.named_children if c.type in _CONTROL), None)
        if inner is not None:
            return expr(inner, cur, ctx)
    return plain(node, cur)


_CONTROL = {"if_expression", "match_expression", "loop_expression", "while_expression", "for_expression", "block",
            "return_expression", "break_expression", "continue_expression"}
_CONTROL_WRAPPERS = {"assignment_expression", "compound_assignment_expr", "parenthesized_expression"}

entry = block()
function_body = target.child_by_field_name("body")
end = body(function_body, entry, {})
if end is not None:
    link(end, exit_block, "fall off")
    tail = function_body.named_children[-1] if function_body.named_children else None
    if end["kind"] == "goto" and tail is not None and tail.type not in ("expression_statement", "let_declaration"):
        end["kind"], end["label"] = "return", f"return {text(tail)}"
forms = qualified(target)
print(json.dumps({"function": forms[-1] if len(forms) > 1 else forms[0], "file": str(path),
                  "line": target.start_point[0] + 1, "entry": entry["id"], "blocks": blocks}))
