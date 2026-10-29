---
name: codeflow
description: "Call trees and per-function control-flow graphs from the code's real structure, rendered with file:line: who calls a function, what it calls, and every path through it (branches, loops, early returns, raises, never-returning calls). Use it instead of grep for control flow, callers, callees, what links to what, or how a feature is wired. codeflow.tree uses the session's language servers (Rust, Python, C/C++, TypeScript/JavaScript, Go, Dart, Java, C#, Swift) and acorn for JS bundles; codeflow.cfg uses Python ast, acorn (JS/TS), LLVM IR with the project's compile flags (C/C++) and rustc MIR via rustc_public (Rust), by function name or by line, with follow=N to nest callee graphs."
---

# codeflow

```python
print(await codeflow.tree("crates/pa-tui/src/session_ui/queue.rs", "send_now"))            # what it calls
print(await codeflow.tree("src/server.py", "handle", direction="incoming", depth=2))      # who calls it
print(await codeflow.tree("chunk.deob.js", "$9t.sendQueuedNow", depth=4))                 # a JS bundle
print(await codeflow.cfg("src/rlm/repl.py", "_run_guarded"))                              # every path through one function
print(await codeflow.cfg("ggml/src/ggml.c", 1778))                                        # the function around a line
print(await codeflow.cfg("src/server.py", "handle", follow=1))                            # plus each project callee's graph
```

`cfg(path, function_or_line, *, level="source", cleanup=False, follow=0, follow_limit=20)` (.py, .js/.mjs/.cjs, .ts/.tsx/.mts/.cts, .c/.h/.cc/.cpp, .rs)
prints one line per block: `B3  :1081  if <condition>` with its edges
(`true → B4`, `loop`, `done`, `exception`, `return`, `then re-raise`, `case X`, `goto L`; `↺` a loop back-edge,
`← B1, B2` where paths join). Python `finally`, JS `finally`, C `goto`/`switch` fall-through and C++ `try` are
modelled. Rust graphs come from the compiler's MIR (`?`, match arms and drops are real blocks; calls resolve to the
concrete impl); the first call per workspace compiles it in `target/codeflow` (minutes), later calls only the crate
(10-60 s on prime-agent). Rust `level`: `"source"` reads like the code; `"async"` adds the async state machine (state
dispatch, `resume #k` edges, each `.await`'s Ready/Pending with the suspend to the executor); `"mir"` is the compiler's
graph untouched (drops, macro internals, MIR temporaries). `cleanup=True` adds unwind paths at any level.

- `at`: a symbol name (`"Class.method"` / `"Class.#private"` in JS), a 1-based line, or `"line:col"`.
- `direction`: `"outgoing"` (default) or `"incoming"`. `depth`: levels (default 3).
- LSP trees expand only functions under `root` (default: the file's git root); `external=True` lists calls into
  dependencies and std as one leaf line.
- JavaScript in a project (a `package.json`/`tsconfig.json`/`jsconfig.json` up to its git root, under 400 KB) goes
  through the TypeScript server, which resolves `obj.method()` by type; bundles and loose files through acorn;
  `backend="lsp"|"acorn"` forces one (an LSP failure falls back to acorn).
- The result prints as a tree (`name  path:line  (at <call lines>)`, `↺` a cycle, `(expanded above)` a repeat);
  `.edges` is `[(caller, callee, "path:line"), ...]`.

## Which tool for which question

| Question | Use |
|---|---|
| What does X call / who calls X / how is a feature wired | `codeflow.tree` |
| Every path through X: branches, early returns, loops, error paths | `codeflow.cfg` |
| Where is X defined, its type or signature | `rlm.lsp.definition`, `rlm.lsp.hover` |
| Every use of X | `rlm.lsp.references` |
| A file's outline | `rlm.lsp.document_symbols` |
| Guards, every-path checks, C cleanup paths, tree-sitter queries | the `ast-walk` skill (`refs.py`, `guards.py`, `paths.py`) |
| A minified bundle | deobfuscate first (`jsbeautify -d`, the `jsbeautify` skill), then `codeflow.tree` on the output |
| A string, a log message, a config key | grep is right |

## Limits

- Syntax and server facts only: dynamic dispatch, callbacks stored in data, and macros may be missing; say so when
  a conclusion depends on them. The JS walker resolves top-level functions and `this.` methods of the same class;
  `obj.method()` on other objects is left out.
- LSP trees need the language's server in `lspServers` (`await rlm.lsp.servers()`). Call trees: Rust
  (rust-analyzer), Python (pyright), C/C++ (clangd), TypeScript/JavaScript (TS 7 `tsc --lsp`), Go (gopls), Dart,
  Java (jdtls), C# (csharp-ls), Swift (sourcekit-lsp). Lua, Zig, PHP and Bash servers answer `rlm.lsp` definitions,
  references and hover but have no call hierarchy; a first call on a big Rust workspace waits for rust-analyzer to index. clangd needs a
  `compile_commands.json` (project root or `build/`) to resolve calls across files.
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
- A line instead of a name picks the innermost function around it: a JS/TS anonymous callback is
  `outer::<arrow@L>`, a Rust closure `outer::{closure#0}`. Two Rust closures on one line: `"LINE:COL"` picks the one whose body holds
  that column (a bare line names both and asks for one).
- `follow=N` appends each project callee's graph (`── calls name  path:line (at lines)`, nested with `│`), N levels,
  each function once, at most `follow_limit` per function; the rest are named in a `── … N more callees` line.
- JS/TS need `node`, `acorn` and `@sveltejs/acorn-typescript` (`npm ci` in `src/codeflow` after a fresh clone); C needs `uv` (tree-sitter grammars are pinned in
  `ccfg.py`'s script header); Rust needs the `rustc-master` toolchain (the driver rebuilds itself when it changes).
