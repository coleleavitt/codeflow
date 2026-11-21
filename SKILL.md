---
name: codeflow
description: "One tool, codeflow.cfg: every path through a function (branches, loops, early returns, raises, never-returning calls), who calls it (callers=N), what it calls (callees=N, follow=N), where a value comes from (var=, def-use with reaching definitions), every reference to a symbol by kind (refs=True: call, write, read, reference taken, type use; fields and constants too) and dominators, rendered with file:line from the code's real structure. Use it instead of grep for control flow, callers, callees, what links to what, or how a feature is wired. Callers and callees use the session's language servers (Rust, Python, C/C++, TypeScript/JavaScript, Go, Dart, Java, C#, Swift) and acorn for JS bundles; codeflow.cfg uses Python ast, acorn (JS/TS), LLVM IR with the project's compile flags (C/C++) and rustc MIR via rustc_public (Rust), by function name or by line, with follow=N to nest callee graphs. Triggers: codeflow, cfg, mir, acorn, ast walk, call graph, call tree, callers, callees, control flow, \"dig in\" on code structure."
---

# codeflow

One tool, `codeflow.cfg`: every path through a function, who calls it and what it calls, where a value comes from,
and every reference to any symbol, with file:line.

```python
print(await codeflow.cfg("crates/pa-tui/src/session_ui/queue.rs", "send_now"))                # every path through it
print(await codeflow.cfg("src/server.py", "handle", callers=2))                                # + who calls it, 2 levels
print(await codeflow.cfg("src/server.py", "handle", callees=1))                                # + what it calls
print(await codeflow.cfg("src/server.py", "handle", follow=1))                                 # + each project callee's graph
print(await codeflow.cfg("ggml/src/ggml.c", 1778))                                             # the function around a line
print(await codeflow.cfg("crates/x/src/lib.rs", "Runner::run_cycle"))                          # Type::method, Trait::method
print(await codeflow.cfg("src/server.py", "handle", var="total"))                              # where `total` comes from at each read
print(await codeflow.cfg("crates/x/src/lib.rs", "limit", refs=True))                           # every site naming it, by kind (a field too)
print(await codeflow.cfg("src/server.py", "handle", dominators=True))                          # what every path to each block runs first
```

`cfg(path, function_or_line, *, callers=0, callees=0, follow=0, follow_limit=20, level="source", cleanup=False)`
(.py, .js/.mjs/.cjs, .ts/.tsx/.mts/.cts, .c/.h/.cc/.cpp, .rs) prints one line per block: `B3  :1081  if <condition>`
with its edges (`true → B4`, `loop`, `done`, `exception`, `return`, `? returns the error`, `case X`, `goto L`; `↺` a
loop back-edge, `← B1, B2` where paths join).

- `callers=N`: who calls it, N levels, each call site as file:line, printed above the graph. `callees=N`: what it
  calls, N levels, below it. Both come from the language servers (acorn for JS bundles); a lookup that finds nothing
  says so in one line and the graph still prints.
- `follow=N`: each project callee's own graph, nested (`── calls name  path:line (at lines)`), N levels, each
  function once, at most `follow_limit` per function.
- A name not defined in `path` is looked up in the project: one definition elsewhere is used (the header's `note:`
  says where), several are listed with file:line.
- Rust graphs come from the compiler's MIR (`?`, match arms and drops are real blocks; calls resolve to the concrete
  impl); the first call per workspace compiles it in `target/codeflow` (minutes), later calls only the crate (10-60 s).
  A crate that does not build, or a function the build leaves out (a `#[cfg(test)]` module), gets a syntax graph
  from tree-sitter instead; its header says `syntax only` and why. Rust `level`: `"source"` reads like the code;
  `"async"` adds the async state machine; `"mir"` is the compiler's graph untouched. `cleanup=True` adds unwind paths.
- `var="x"` (or `"self.x"`): under each block, `def x :L` and `use x :L ← :L1, :L3`, the definitions that can reach
  that read along the graph's paths (reaching definitions). A read nothing in the function reaches is a value from
  outside (a parameter's caller, a field, a global), named in the closing line. What changes a variable without
  assigning it counts as a definition too: a call it is passed to as `&mut` or `&x` (an out-parameter), a mutating
  method (`v.push(x)`, `acc.append(x)`), a field or element write (`x.a = ..`, `x[i] = ..`), and a write through a
  pointer to it, wherever the pointer is kept: a variable (`let r = &mut x; *r += 1`, C `int *p = &x; *p = 1`), a
  struct field (`h = H { p: &raw mut x }; *h.p = 1`), or a struct handed to a call (`poke(&mut h)`). Rust reads the
  compiler's MIR (each event in its exact block); `var` can be a field path there (`self.limit`: a write to
  `self.limit`, to a part of it, or to all of `self` counts). Python reads `ast`; JS/TS, C and Rust that does not
  build read the syntax with the same rules.
- `refs=True`: every site that names the symbol across the project, grouped by what it does there: definition, call,
  write (an assignment, a field initializer), read, reference taken (`&x`, a function stored or passed as a value),
  type use, import; each with the function it sits in. It works for things with no graph (a struct field, a
  constant, a type): then the references are the whole answer. Sites come from the language server when one
  answers, else from a whole-word search of the project's files of that language.
- `dominators=True`: under each block, `dominated by B3 ← B1 ← B0`: the blocks every path from the entry runs before
  it (Cooper, Harvey and Kennedy's iterative algorithm).
- An ambiguous name lists each definition (`A::dup (line 9), B::dup (line 10)`): pass the qualified name or a line.

`codeflow.tree(path, at, direction="outgoing"|"incoming", depth=3)` is the same call tree as `callers=`/`callees=`
without the graph; old cells keep working. `.edges` is `[(caller, callee, "path:line"), ...]`. `root` (default: the
file's git root) bounds what counts as the project; `external=True` lists calls into dependencies as one leaf line.

## Which tool for which question

| Question | Use |
|---|---|
| Who calls X | `codeflow.cfg(file, "X", callers=2)` |
| What X calls, how a feature is wired | `codeflow.cfg(file, "X", callees=2)`, or `follow=1` for their paths too |
| Every path through X: branches, early returns, loops, error paths | `codeflow.cfg(file, "X")` |
| Which recursion in a Rust crate is not bounded by a guard (stack-depth checks) | `codeflow.recursion(file, ["check_stack"], release=False)` |
| Where a value comes from, every place it is set and read | `codeflow.cfg(file, "fn", var="x")` |
| Every use of X (a function, field, constant, type), what each does | `codeflow.cfg(file, "X", refs=True)` |
| What must run before a block | `codeflow.cfg(file, "fn", dominators=True)` |
| Where is X defined, its type or signature | `rlm.lsp.definition`, `rlm.lsp.hover` |
| A file's outline | `rlm.lsp.document_symbols` |
| A minified bundle | deobfuscate first (`jsbeautify -d`, the `jsbeautify` skill), then `codeflow.cfg` on the output |
| A string, a log message, a config key | grep is right |

## Outside the kernel: CLI and MCP

The same tool runs without prime-agent (Claude Code, a shell). Language-server lookups then go through
`_lsp_client`, which starts the servers named in `~/.prime/agent/settings.json`'s `lspServers` (or
`$CODEFLOW_LSP_SETTINGS`; else rust-analyzer, pyright, clangd, typescript-language-server, gopls on `PATH`),
rooted at each file's git root, in place of `rlm.lsp`.

```sh
codeflow cfg src/server.py handle --callers 2 --var total     # cfg's keywords as flags; --json for {function, text, graph}
codeflow cfg Runner::run_cycle --callees 1                     # a bare name: its one definition in the project (cwd's git root, or --root)
codeflow mcp                                                   # stdio MCP server: codeflow_cfg, codeflow_callers, codeflow_callees
```

Run it as `uv run --project ~/.prime/agent/skills/codeflow codeflow ...`, `python -m codeflow ...`, or the venv's
`.venv/bin/codeflow`. Flags: `--function NAME` (or a second positional: a name, a line, `LINE:COL`), `--callers N`,
`--callees N`, `--follow N`, `--follow-limit N`, `--var NAME`, `--refs`, `--dominators`, `--level`, `--cleanup`,
`--root DIR`, `--json`, and the views below. Claude Code registration (user scope):
`claude mcp add --scope user codeflow -- ~/.prime/agent/skills/codeflow/.venv/bin/codeflow mcp`. The MCP server keeps
its language servers up between calls; one CLI call starts and stops them.

### Views of a big graph

Block numbers are the displayed ones (`B7`), in the text and in `--json` (ids, `entry` and edge targets are
integers).

```sh
codeflow cfg src/lib.rs run --out /tmp/run.cfg.txt          # write the whole output to a file, print a summary
codeflow cfg src/lib.rs run --blocks 12-30                  # only these blocks (also 3,5,9 or B3-B9), edges inside the range
codeflow cfg src/lib.rs run --path-from B3 --path-to B40    # the simple paths between two blocks, shortest first (at most 50)
```

### Rust call trees from the SCIP index, and `query`

Rust callers and callees come from a SCIP index of the workspace (`rust-analyzer scip`), stored as SQLite at
`~/.cache/codeflow/scip/<workspace hash>/index.sqlite` (tables `occ` for occurrences, `names`, `meta`). A query
reads only the rows it needs (tens of milliseconds); a line inside a function body (a closure's included) names
the enclosing function. The first query on a workspace starts the export in the background and answers from
rust-analyzer meanwhile (`[... rust, scip]` in the header says which answered); a workspace whose sources or
lockfile changed is exported again. The export peaks around 6 GB of memory on a large workspace (kuna).

```sh
codeflow query "SELECT symbol, count(*) FROM occ WHERE roles & 1 = 0 GROUP BY symbol ORDER BY 2 DESC" --limit 20
codeflow query "SELECT path, line FROM occ WHERE symbol LIKE '%flatten().' AND roles & 1" --json
```

`query` runs one read-only `SELECT` (or `WITH ... SELECT`) on the index of the project it is run in (`--root`).

## Limits

- `codeflow.recursion` works on the crate's LLVM IR (`cargo rustc --emit=llvm-ir`, one codegen unit, line
  tables), so it sees every monomorphized instance, inlining in `release=True`, and calls through function
  pointers and `dyn` (sent to every address-taken function of the crate with the same LLVM type, so it can report
  cycles that cannot happen). A path is guarded once it runs a guard: a call to one, code inlined from one (by
  debug locations), or a call to a function that runs a guard before its first branch. Code in other crates that
  is not instantiated here is not seen; it can only reach this crate through an address-taken function.
- Syntax and server facts only: dynamic dispatch, callbacks stored in data, and macros may be missing; say so when
  a conclusion depends on them. The JS walker resolves top-level functions and `this.` methods of the same class;
  `obj.method()` on other objects is left out.
- LSP trees need the language's server in `lspServers` (`await rlm.lsp.servers()`). Call trees: Rust
  (rust-analyzer), Python (pyright), C/C++ (clangd), TypeScript/JavaScript (TS 7 `tsc --lsp`), Go (gopls), Dart,
  Java (jdtls), C# (csharp-ls), Swift (sourcekit-lsp). Lua, Zig, PHP and Bash servers answer `rlm.lsp` definitions,
  references and hover but have no call hierarchy; a first call on a big Rust workspace waits for rust-analyzer to index. clangd needs a
  `compile_commands.json` (project root or `build/`) to resolve calls across files; incoming calls need its
  background index, which fills in over the first minutes on a big tree. In a built Linux tree codeflow writes
  `compile_commands.json` itself (`scripts/clang-tools/gen_compile_commands.py`, about a second); elsewhere CMake
  (`-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`), Meson or `intercept-build make` make one. GCC-only flags clang rejects are
  dropped (codeflow retries without them; `~/.config/clangd/config.yaml` removes them for clangd).
- Each graph's header names its backend. C/C++ graphs come from LLVM IR (clang `-O0 -g` with the file's
  `compile_commands.json` flags): macros are expanded, a `noreturn` call ends its path, and C++ exceptions are real
  edges (each call in a `try` has an `exception` edge into its `catch`; destructor-only unwind paths are cleanup,
  shown with `cleanup=True`) (`GGML_ASSERT` is a branch
  whose failing side "never returns"); without a compile command it falls back to tree-sitter and says why. Python and
  JS ask the type checker which call statements never return (pyright `NoReturn`/`Never`, tsgo `never`), falling
  back to known names (`sys.exit`, `os._exit`, `process.exit`) and same-file `NoReturn` annotations. Untyped helpers
  too: a called function whose own graph has no normal return (it always raises, throws or exits) ends its caller's
  path ("+ callee graphs" in the header; three levels deep, so `fatal()` → `bail()` → `sys.exit()` counts; definitions
  from the language server or the same file; a recursive helper counts as returning).
- Rust branch labels: each MIR branch tests one operand, so `a || b` is two branches, labelled `[tests a]` and
  `[tests b]`; an operand behind `!` reads like the source (rustc branches on the operand with swapped targets). C
  `&&`/`||` operands are labelled the same way from the LLVM IR's columns (`[tests !ctx->no_alloc]`); operands inside a
  macro argument (`GGML_ASSERT(a && b)`) are labelled too: their instructions carry the spelled column (clang's
  `getMacroDebugLoc`), the branch itself the macro body's, so the operand start skips the branch.
- Rust blocks: a merged block lists its calls in the order they run, a call repeated back to back as `name ×N`, and
  never cuts the list; a long label wraps between calls. A macro's condition (`matches!` in `if a && matches!(..)`) is a
  branch of its own when its paths lead to different code (`[tests the macro's condition]`), and a diverging macro call
  (`assert!`'s `panic_fmt`, `unreachable!`) sits on the line of the code before it.
- A line instead of a name picks the innermost function around it: a JS/TS anonymous callback is
  `outer::<arrow@L>`, a Rust closure `outer::{closure#0}`. Two Rust closures on one line: `"LINE:COL"` picks the one whose body holds
  that column (a bare line names both and asks for one).
- `follow=N` appends each project callee's graph (`── calls name  path:line (at lines)`, nested with `│`), N levels,
  each function once, at most `follow_limit` per function; the rest are named in a `── … N more callees` line.
- JS/TS need `node`, `acorn` and `@sveltejs/acorn-typescript` (`npm ci` in `src/codeflow` after a fresh clone); C needs `uv` (tree-sitter grammars are pinned in
  `ccfg.py`'s script header); Rust needs the `rustc-master` toolchain (the driver rebuilds itself when it changes). The Rust MIR build
  goes to `~/.cache/codeflow/mir/<workspace hash>` (`CODEFLOW_TARGET_DIR` overrides), never to the project's
  `target/` or the user's `CARGO_TARGET_DIR`: its own rustc driver would invalidate their builds.
