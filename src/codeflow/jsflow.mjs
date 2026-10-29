// Function-level call graph of one JavaScript file (acorn), for codeflow.
// Usage: node jsflow.mjs FILE  ->  {"nodes": {name: line}, "edges": {name: [[callee, line], ...]}}
// Node names: top-level functions and function-valued variables by name, class
// methods as "Class.method" ("Class.#private" for private ones). A call inside a
// nested closure counts for the enclosing named function. Edges are calls plus
// methods passed by reference (setTimeout(this.#tick), subscribe(this.#onChange)).
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
const require = createRequire(import.meta.url);
const acorn = require("acorn");
const walk = require("acorn-walk");

const src = readFileSync(process.argv[2], "utf8");
let ast;
for (const sourceType of ["module", "script"]) {
  try { ast = acorn.parse(src, { ecmaVersion: "latest", sourceType, locations: true, allowHashBang: true, allowReturnOutsideFunction: true }); break; }
  catch (error) { if (sourceType === "script") { console.error(String(error)); process.exit(2); } }
}
const nodes = {}, edges = {};
const key = (k) => k.type === "PrivateIdentifier" ? "#" + k.name : k.type === "Identifier" ? k.name : k.type === "Literal" ? String(k.value) : null;
const isFn = (n) => n && /Function/.test(n.type);

function member(expr, cls) {
  if (expr.type !== "MemberExpression") return null;
  const prop = key(expr.property);
  if (!prop || expr.computed && expr.property.type !== "Literal") return null;
  if (expr.object.type === "ThisExpression") return cls ? cls + "." + prop : null;
  if (expr.object.type === "Identifier") return expr.object.name + "." + prop;
  return "?." + prop;
}
// Names bound inside a function (its parameters and local declarations, nested
// functions included): a call to one of them is local, never a top-level function.
function locals(fnNode) {
  const names = new Set();
  const bind = (p) => {
    if (!p) return;
    if (p.type === "Identifier") names.add(p.name);
    else if (p.type === "AssignmentPattern") bind(p.left);
    else if (p.type === "RestElement") bind(p.argument);
    else if (p.type === "ArrayPattern") p.elements.forEach(bind);
    else if (p.type === "ObjectPattern") p.properties.forEach((q) => bind(q.type === "RestElement" ? q.argument : q.value));
  };
  walk.full(fnNode, (n) => {
    if (isFn(n)) { n.params.forEach(bind); if (n !== fnNode && n.id) names.add(n.id.name); }
    else if (n.type === "VariableDeclarator") bind(n.id);
    else if (n.type === "CatchClause") bind(n.param);
    else if (n.type === "ClassDeclaration" && n.id) names.add(n.id.name);
  });
  return names;
}
function collect(name, fnNode, cls) {
  nodes[name] = fnNode.loc.start.line;
  const out = (edges[name] ??= []);
  const own = locals(fnNode);
  const target = (e) => e.type === "Identifier" ? (own.has(e.name) ? null : e.name) : member(e, cls);
  walk.full(fnNode.body ?? fnNode, (n) => {
    if (n.type === "CallExpression" || n.type === "NewExpression") {
      const callee = target(n.callee);
      if (callee) out.push([callee, n.loc.start.line]);
      for (const arg of n.arguments) {
        const ref = target(arg);
        if (ref) out.push(["&" + ref, n.loc.start.line]);
      }
    }
  });
}
function visitClass(cls, body) {
  for (const m of body.body) {
    const k = key(m.key);
    if (!k) continue;
    const fn = m.type === "MethodDefinition" ? m.value : isFn(m.value) ? m.value : null;
    if (fn) collect(cls + "." + (k === "constructor" ? "constructor" : k), fn, cls);
  }
}
// Top-level functions only (a nested one belongs to its enclosing function);
// classes wherever they are declared.
for (let stmt of ast.body) {
  if (stmt.type === "ExportNamedDeclaration" || stmt.type === "ExportDefaultDeclaration") stmt = stmt.declaration ?? stmt;
  if (stmt?.type === "FunctionDeclaration" && stmt.id) collect(stmt.id.name, stmt, null);
  if (stmt?.type === "VariableDeclaration")
    for (const d of stmt.declarations)
      if (d.id.type === "Identifier" && isFn(d.init)) collect(d.id.name, d.init, null);
}
walk.full(ast, (n) => {
  if ((n.type === "ClassDeclaration" || n.type === "ClassExpression") && n.id) visitClass(n.id.name, n.body);
  else if (n.type === "VariableDeclarator" && n.id.type === "Identifier" && n.init?.type === "ClassExpression" && !n.init.id) visitClass(n.id.name, n.init.body);
});
console.log(JSON.stringify({ nodes, edges }));
