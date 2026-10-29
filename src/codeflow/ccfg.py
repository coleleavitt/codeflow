#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["tree-sitter>=0.25", "tree-sitter-c>=0.24", "tree-sitter-cpp>=0.23"]
# ///
"""Statement-level control-flow graph of one C/C++ function (tree-sitter), in codeflow's shape.

    ccfg.py FILE NAME    ->  JSON on stdout
"""
import json
import sys
from pathlib import Path

import tree_sitter_c
import tree_sitter_cpp
from tree_sitter import Language, Parser

path, wanted = Path(sys.argv[1]), sys.argv[2]
source = path.read_bytes()
cpp = path.suffix in {".cc", ".cpp", ".cxx", ".hpp", ".hh"}
tree = Parser(Language(tree_sitter_cpp.language() if cpp else tree_sitter_c.language())).parse(source)


def text(node, limit=60):
    value = " ".join(source[node.start_byte:node.end_byte].decode(errors="replace").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


def fn_name(node):
    declarator = node.child_by_field_name("declarator")
    while declarator is not None and declarator.type not in ("function_declarator",):
        declarator = declarator.child_by_field_name("declarator")
    if declarator is None:
        return None
    name = declarator.child_by_field_name("declarator")
    return text(name, 200) if name is not None else None


if wanted.isdigit():  # a line: the function around it
    line = int(wanted)
    around = [n for n in walk(tree.root_node) if n.type == "function_definition" and n.start_point[0] + 1 <= line <= n.end_point[0] + 1]
    if not around:
        print(f"no function around {path}:{line}", file=sys.stderr)
        sys.exit(3)
    inner = min(around, key=lambda n: n.end_byte - n.start_byte)
    if "--name" in sys.argv:
        print(fn_name(inner))
        sys.exit(0)
    wanted = fn_name(inner)

target = next(
    (n for n in walk(tree.root_node) if n.type == "function_definition" and (fn_name(n) or "").split("::")[-1] in (wanted, wanted.split("::")[-1]) and (fn_name(n) == wanted or "::" not in wanted or fn_name(n).endswith(wanted))),
    None,
)
if target is None:
    print(f"no function {wanted} in {path}", file=sys.stderr)
    sys.exit(3)

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
labels = {}


def label_block(name):
    if name not in labels:
        labels[name] = block("goto", f"{name}:")
    return labels[name]


def head(cur, node, kind, label):
    if cur["lines"] is not None or cur["kind"] != "goto":
        nxt = block()
        link(cur, nxt)
        cur = nxt
    cur["kind"], cur["label"] = kind, label
    cover(cur, node)
    return cur


def seq(nodes, cur, ctx):
    for node in nodes:
        if node.type in ("comment", "{", "}") or not node.is_named:
            continue
        if cur is None:
            if node.type != "labeled_statement":
                cur = block("goto", "unreachable")
            else:
                cur = block("goto", "")
        cur = stmt(node, cur, ctx)
    return cur


def body_of(node):
    return [c for c in node.children if c.is_named] if node.type == "compound_statement" else [node]


def loop(node, cur, ctx, label, at, body):
    header = block("switch", label)
    cover(header, at)
    link(cur, header)
    after, inner = block(), block()
    link(header, inner, "loop")
    link(header, after, "done")
    end = seq(body_of(body), inner, dict(ctx, brk=after, cont=header))
    if end is not None:
        link(end, header, "next")
    return after


def stmt(node, cur, ctx):
    t = node.type
    if t == "compound_statement":
        return seq([c for c in node.children if c.is_named], cur, ctx)
    if t == "if_statement":
        cond = node.child_by_field_name("condition")
        test = head(cur, cond, "switch", f"if {text(cond)}")
        after, then = block(), block()
        link(test, then, "true")
        end = seq(body_of(node.child_by_field_name("consequence")), then, ctx)
        if end is not None:
            link(end, after)
        alternative = node.child_by_field_name("alternative")
        if alternative is not None:
            if alternative.type == "else_clause":
                alternative = alternative.named_children[0]
            other = block()
            link(test, other, "false")
            end = seq(body_of(alternative), other, ctx)
            if end is not None:
                link(end, after)
        else:
            link(test, after, "false")
        return after if reached(after) else None
    if t == "while_statement":
        cond = node.child_by_field_name("condition")
        return loop(node, cur, ctx, f"while {text(cond)}", cond, node.child_by_field_name("body"))
    if t == "do_statement":
        cond = node.child_by_field_name("condition")
        return loop(node, cur, ctx, f"do … while {text(cond)}", cond, node.child_by_field_name("body"))
    if t in ("for_statement", "for_range_loop"):
        cond = node.child_by_field_name("condition") or node.child_by_field_name("right")
        init = node.child_by_field_name("initializer")
        if init is not None:
            cover(cur, init)
        label = f"for {text(cond)}" if cond is not None else "for (;;)"
        return loop(node, cur, ctx, label, cond or node, node.child_by_field_name("body"))
    if t == "switch_statement":
        cond = node.child_by_field_name("condition")
        test = head(cur, cond, "switch", f"switch {text(cond)}")
        after = block()
        inner = dict(ctx, brk=after)
        fall, has_default = None, False
        for case in node.child_by_field_name("body").named_children:
            if case.type != "case_statement":
                continue
            value = case.child_by_field_name("value")
            arm = block()
            if value is not None:
                cover(arm, value)
            else:
                has_default = True
                arm["lines"] = [case.start_point[0] + 1, case.start_point[0] + 1]
            link(test, arm, f"case {text(value, 30)}" if value is not None else "default")
            if fall is not None:
                link(fall, arm, "fall through")
            fall = seq([c for c in case.named_children if c != value], arm, inner)
        if fall is not None:
            link(fall, after)
        if not has_default:
            link(test, after, "no case")
        return after if reached(after) else None
    if t in ("break_statement", "continue_statement"):
        cover(cur, node)
        word = "break" if t == "break_statement" else "continue"
        cur["label"] = word
        target_block = ctx.get("brk" if word == "break" else "cont")
        if target_block is not None:
            link(cur, target_block, word)
        return None
    if t == "return_statement":
        cover(cur, node)
        value = node.named_children[0] if node.named_children else None
        cur["kind"], cur["label"] = "return", f"return {text(value)}" if value is not None else "return"
        link(cur, exit_block, "return")
        return None
    if t == "goto_statement":
        cover(cur, node)
        name = text(node.child_by_field_name("label"))
        cur["label"] = f"goto {name}"
        link(cur, label_block(name), "goto")
        return None
    if t == "labeled_statement":
        name = text(node.child_by_field_name("label"))
        target_block = label_block(name)
        target_block["lines"] = [node.start_point[0] + 1, node.start_point[0] + 1]
        link(cur, target_block)
        return seq([c for c in node.named_children if c.type != "statement_identifier"], target_block, ctx)
    if t == "try_statement":
        after = block()
        start = block("goto", "try")
        start["lines"] = [node.start_point[0] + 1] * 2
        link(cur, start)
        body = block()
        link(start, body)
        end = seq(body_of(node.child_by_field_name("body")), body, ctx)
        if end is not None:
            link(end, after)
        for clause in (c for c in node.named_children if c.type == "catch_clause"):
            params = clause.child_by_field_name("parameters")
            caught = block("goto", f"catch {text(params)}" if params is not None else "catch")
            caught["lines"] = [clause.start_point[0] + 1] * 2
            link(start, caught, "exception")
            cend = seq(body_of(clause.child_by_field_name("body")), caught, ctx)
            if cend is not None:
                link(cend, after)
        return after if reached(after) else None
    if t == "expression_statement" and node.named_children and node.named_children[0].type == "throw_expression":
        cover(cur, node)
        cur["kind"], cur["label"] = "raise", text(node.named_children[0])
        link(cur, exit_block, "throw")
        return None
    cover(cur, node)
    return cur


entry = block()
end = seq([c for c in target.child_by_field_name("body").named_children], entry, {})
if end is not None:
    link(end, exit_block, "fall off")
    if end["kind"] == "goto" and not end["label"]:
        end["kind"], end["label"] = "return", "return (falls off the end)"
print(json.dumps({"function": fn_name(target), "file": str(path), "line": target.start_point[0] + 1,
                  "entry": entry["id"], "blocks": blocks}))
