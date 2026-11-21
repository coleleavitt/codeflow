# Reference sources for codeflow

Local checkouts this tool's behaviour is checked against. Paths are relative to `~/RustProjects/forks/`.

## MIR (the `rustc-driver` graphs)

codeflow's Rust control-flow graphs come from rustc's MIR, read by the driver in `rustc-driver/src/main.rs`
(it hooks `rustc_driver` and writes the graph to `CODEFLOW_OUT`).

| What | Where |
|---|---|
| MIR data structures (basic blocks, terminators, `Body`) | `rust/compiler/rustc_middle/src/mir/` |
| Building MIR from HIR (where closures and drops get their blocks) | `rust/compiler/rustc_mir_build/` |
| MIR passes that rewrite the graph (what `--level mir` shows vs `source`) | `rust/compiler/rustc_mir_transform/` |
| Dataflow over MIR (the basis of `--var` reaching definitions) | `rust/compiler/rustc_mir_dataflow/` |
| Design notes: MIR overview, construction, dataflow, passes | `rustc-dev-guide/src/mir/` (sparse clone, `index.md`, `construction.md`, `dataflow.md`, `passes.md`) |

Facts the code relies on:
- Closures are separate bodies in MIR, named `outer::{closure#N}`. That is why a line inside a closure resolves
  to the closure's graph unless codeflow maps it to the enclosing function (`_enclosing_named` in `__init__.py`).
- Drop elaboration adds cleanup blocks; `--cleanup` shows them.

## Language server (the SCIP and call-hierarchy path)

| What | Where |
|---|---|
| Call hierarchy: incoming calls are the semantic usages of a function (`func.usages(sema).all()`) | `rust-analyzer/crates/ide/src/call_hierarchy.rs` (`incoming_calls`, `outgoing_calls`) |
| `ContentModified` (LSP -32801): returned when the request is cancelled because the workspace changed | `rust-analyzer/crates/rust-analyzer/src/handlers/dispatch.rs` (`content_modified_error`) |

Facts the code relies on:
- rust-analyzer opens at a Cargo workspace; a git root without `Cargo.toml` yields no crates and empty call
  hierarchies (`_project_root` in `_lsp_client.py`).
- The client retries `ContentModified` while the server indexes (`_settled` in `_lsp_client.py`).
- `rust-analyzer scip` writes the index the SQLite database is built from (`_scip.py`, `_scip_db.py`).
