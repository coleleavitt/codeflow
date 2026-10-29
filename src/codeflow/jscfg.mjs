// Statement-level control-flow graph of one JavaScript function (acorn), in codeflow's shape.
// Usage: node jscfg.mjs FILE NAME   (NAME: a function, a variable bound to one, or "Class.method")
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
const require = createRequire(import.meta.url);
const acorn = require("acorn");
const walk = require("acorn-walk");

const [file, wanted] = process.argv.slice(2);
const src = readFileSync(file, "utf8");
let ast;
for (const sourceType of ["module", "script"]) {
  try { ast = acorn.parse(src, { ecmaVersion: "latest", sourceType, locations: true, allowHashBang: true, allowReturnOutsideFunction: true }); break; }
  catch (error) { if (sourceType === "script") { console.error(String(error)); process.exit(2); } }
}
const key = (k) => k.type === "PrivateIdentifier" ? "#" + k.name : k.type === "Identifier" ? k.name : k.type === "Literal" ? String(k.value) : null;
const isFn = (n) => n && /Function/.test(n.type);
let target = null, targetName = wanted;
const [cls, method] = wanted.includes(".") ? wanted.split(/\.(.*)/) : [null, wanted];
walk.full(ast, (n) => {
  if (target) return;
  if (!cls) {
    if (n.type === "FunctionDeclaration" && n.id?.name === method) target = n;
    else if (n.type === "VariableDeclarator" && n.id.type === "Identifier" && n.id.name === method && isFn(n.init)) target = n.init;
  } else if ((n.type === "ClassDeclaration" || n.type === "ClassExpression") && n.id?.name === cls) {
    for (const m of n.body.body) if (key(m.key) === method && (m.type === "MethodDefinition" || isFn(m.value))) target = m.value;
  }
});
if (!target) { console.error(`no function ${wanted} in ${file}`); process.exit(3); }

const blocks = [];
const text = (n, limit = 60) => { const s = src.slice(n.start, n.end).replace(/\s+/g, " "); return s.length <= limit ? s : s.slice(0, limit - 1) + "…"; };
const block = (kind = "goto", label = "") => { const b = { id: blocks.length, lines: null, kind, label, succ: [], cleanup: false }; blocks.push(b); return b; };
const cover = (b, n) => { const [f, l] = [n.loc.start.line, n.loc.end.line]; b.lines = b.lines ? [Math.min(b.lines[0], f), Math.max(b.lines[1], l)] : [f, l]; };
const link = (a, b, label = "") => a.succ.push({ to: b.id, label });
const exit = block("exit", "exit");
const leave = (cur, to, label) => { if (to.finally) to.carries.add(label); link(cur, to, label); };
const head = (cur, n, kind, label) => {
  if (cur.lines !== null || cur.kind !== "goto") { const nxt = block(); link(cur, nxt); cur = nxt; }
  cur.kind = kind; cur.label = label; cover(cur, n); return cur;
};
const reached = (b) => blocks.some((x) => x.succ.some((e) => e.to === b.id));

function seq(body, cur, ctx) {
  for (const s of body) { if (cur === null) cur = block("goto", "unreachable"); cur = stmt(s, cur, ctx); }
  return cur;
}
function loop(n, cur, ctx, label, at) {
  const header = block("switch", label); cover(header, at); link(cur, header);
  const after = block(), body = block();
  link(header, body, "loop"); link(header, after, "done");
  const end = seq([n.body], body, { ...ctx, loops: [...ctx.loops, { header, after, name: ctx.pendingLabel }], pendingLabel: null });
  if (end) link(end, header, "next");
  return after;
}
function stmt(n, cur, ctx) {
  switch (n.type) {
    case "BlockStatement": return seq(n.body, cur, ctx);
    case "LabeledStatement": return stmt(n.body, cur, { ...ctx, pendingLabel: n.label.name });
    case "IfStatement": {
      const test = head(cur, n.test, "switch", `if ${text(n.test)}`);
      const after = block(), then = block();
      link(test, then, "true");
      let end = seq([n.consequent], then, ctx); if (end) link(end, after);
      if (n.alternate) { const other = block(); link(test, other, "false"); end = seq([n.alternate], other, ctx); if (end) link(end, after); }
      else link(test, after, "false");
      return reached(after) ? after : null;
    }
    case "WhileStatement": return loop(n, cur, ctx, `while ${text(n.test)}`, n.test);
    case "DoWhileStatement": return loop(n, cur, ctx, `do … while ${text(n.test)}`, n.test);
    case "ForStatement": { if (n.init) cover(cur, n.init); return loop(n, cur, ctx, `for ${n.test ? text(n.test) : "(;;)"}`, n.test ?? n); }
    case "ForInStatement": case "ForOfStatement":
      return loop(n, cur, ctx, `for ${text(n.left)} ${n.type === "ForInStatement" ? "in" : "of"} ${text(n.right)}`, n.right);
    case "BreakStatement": case "ContinueStatement": {
      cover(cur, n); const word = n.type === "BreakStatement" ? "break" : "continue";
      cur.label = n.label ? `${word} ${n.label.name}` : word;
      const scope = n.label ? ctx.loops.findLast((l) => l.name === n.label.name) : ctx.loops.at(-1);
      if (scope) link(cur, word === "break" ? scope.after : scope.header, cur.label);
      return null;
    }
    case "ReturnStatement":
      cover(cur, n); cur.kind = "return"; cur.label = n.argument ? `return ${text(n.argument)}` : "return";
      leave(cur, ctx.returnTo, "return"); return null;
    case "ThrowStatement":
      cover(cur, n); cur.kind = "raise"; cur.label = `throw ${text(n.argument)}`;
      leave(cur, ctx.raiseTo, "throw"); return null;
    case "SwitchStatement": {
      const test = head(cur, n.discriminant, "switch", `switch ${text(n.discriminant)}`);
      const after = block();
      const inner = { ...ctx, loops: [...ctx.loops, { header: null, after, name: ctx.pendingLabel }], pendingLabel: null };
      let fall = null, hasDefault = false;
      for (const c of n.cases) {
        const arm = block(); if (c.test) cover(arm, c.test); else hasDefault = true;
        link(test, arm, c.test ? `case ${text(c.test, 30)}` : "default");
        if (fall) link(fall, arm, "fall through");
        fall = seq(c.consequent, arm, inner);
      }
      if (fall) link(fall, after);
      if (!hasDefault) link(test, after, "no case");
      return reached(after) ? after : null;
    }
    case "TryStatement": {
      const after = block();
      let fin = null;
      if (n.finalizer) { fin = block("goto", "finally"); fin.finally = true; fin.carries = new Set(); fin.lines = [n.finalizer.loc.start.line, n.finalizer.loc.start.line]; }
      const outer = { ...ctx, raiseTo: fin ?? ctx.raiseTo, returnTo: fin ?? ctx.returnTo };
      const start = block("goto", "try"); start.lines = [n.loc.start.line, n.loc.start.line]; link(cur, start);
      const body = block(); link(start, body);
      let catcher = null;
      if (n.handler) { catcher = block("goto", n.handler.param ? `catch ${text(n.handler.param)}` : "catch"); catcher.lines = [n.handler.loc.start.line, n.handler.loc.start.line]; link(start, catcher, "exception"); }
      let end = seq([n.block], body, n.handler ? { ...outer, raiseTo: catcher } : outer);
      if (end) link(end, fin ?? after);
      if (catcher) { const cend = seq([n.handler.body], catcher, outer); if (cend) link(cend, fin ?? after); }
      if (fin) {
        const fend = seq([n.finalizer], fin, ctx);
        if (!fend) return null;
        const normal = blocks.some((b) => b.succ.some((e) => e.to === fin.id && !fin.carries.has(e.label)));
        if (normal) link(fend, after);
        if (fin.carries.has("return")) leave(fend, ctx.returnTo, "then return");
        if (fin.carries.has("throw")) leave(fend, ctx.raiseTo, "then rethrow");
        if (!normal) return null;
      }
      return reached(after) ? after : null;
    }
    default: cover(cur, n); return cur;
  }
}
const entry = block();
const end = target.body.type === "BlockStatement"
  ? seq(target.body.body, entry, { loops: [], raiseTo: exit, returnTo: exit, pendingLabel: null })
  : (cover(entry, target.body), entry.kind = "return", entry.label = `return ${text(target.body)}`, link(entry, exit, "return"), null);
if (end) { link(end, exit, "fall off"); if (end.kind === "goto" && !end.label) { end.kind = "return"; end.label = "return undefined (falls off the end)"; } }
for (const b of blocks) { delete b.finally; delete b.carries; }
console.log(JSON.stringify({ function: targetName, file, line: target.loc.start.line, entry: entry.id, blocks }));
