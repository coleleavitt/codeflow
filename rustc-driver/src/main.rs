//! `RUSTC_WORKSPACE_WRAPPER` that dumps one function's MIR control-flow graph as JSON.
//!
//! Invoked as `<driver> <rustc> <args...>`. When `CODEFLOW_FN` or `CODEFLOW_LINE` is set and the args
//! carry `--cfg codeflow_probe*`, the crate is analyzed through `rustc_public`, the CFG of the matching
//! local function or closure is written to `CODEFLOW_OUT`, and compilation stops. Otherwise the real
//! rustc runs.
#![feature(rustc_private)]

extern crate rustc_driver;
extern crate rustc_interface;
extern crate rustc_middle;
#[macro_use]
extern crate rustc_public;

use std::collections::{HashMap, HashSet, VecDeque};
use std::ops::ControlFlow;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, exit};

use rustc_public::mir::mono::Instance;
use rustc_public::mir::{
    AssertMessage, BasicBlock, Body, Local, Operand, Place, ProjectionElem, Rvalue, StatementKind,
    TerminatorKind, UnwindAction, VarDebugInfoContents,
};
use rustc_public::ty::{GenericArgs, RigidTy, Span, Ty, TyConst, TyConstKind, TyKind};
use rustc_public::mir::visit::{Location, PlaceContext, statement_location};
use rustc_public::mir::MirVisitor;
use rustc_public::visitor::{Visitable, Visitor};
use rustc_public::{CrateDef, CrateItem, ItemKind};
use serde_json::{Value, json};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let probe = args.windows(2).any(|w| w[0] == "--cfg" && w[1].starts_with("codeflow_probe"));
    let query = match (std::env::var("CODEFLOW_FN"), std::env::var("CODEFLOW_LINE")) {
        (Ok(name), _) => Some(Query::Name(name)),
        (_, Ok(at)) => Some(match at.split_once(':') {
            // `LINE:COL` picks between closures on one line: the innermost body containing that point.
            Some((line, col)) => Query::Line(
                line.parse().expect("CODEFLOW_LINE is not LINE or LINE:COL"),
                Some(col.parse().expect("CODEFLOW_LINE is not LINE or LINE:COL")),
            ),
            None => Query::Line(at.parse().expect("CODEFLOW_LINE is not LINE or LINE:COL"), None),
        }),
        _ => None,
    };
    let Some(query) = query.filter(|_| probe) else {
        let err = Command::new(&args[1]).args(&args[2..]).exec();
        eprintln!("codeflow: cannot run {}: {err}", args[1]);
        exit(1);
    };
    let file = std::env::var("CODEFLOW_FILE").ok().map(|f| canonical(Path::new(&f)));
    let out = std::env::var("CODEFLOW_OUT").expect("CODEFLOW_OUT is not set");
    // Trimmed names are what this driver prints. rustc panics when its diagnostic context is dropped
    // after `trimmed_def_paths` ran without any diagnostic printed (`Session::record_trimmed_def_paths`),
    // which a function whose callees print a type needing a trimmed path (`Zeroizing<String>`) triggers.
    // A set `RUSTC_LOG` skips that check.
    // SAFETY: nothing else runs yet; rustc starts its threads inside `run!`.
    unsafe { std::env::set_var("RUSTC_LOG", "off") };
    let result = run!(&args[1..], || -> ControlFlow<Result<Value, String>> {
        ControlFlow::Break(dump(&query, file.as_deref()))
    });
    match result {
        Err(rustc_public::CompilerError::Interrupted(Ok(graph))) => {
            std::fs::write(&out, graph.to_string()).expect("write CODEFLOW_OUT");
        }
        Err(rustc_public::CompilerError::Interrupted(Err(msg))) => {
            eprintln!("codeflow: {msg}");
            exit(2);
        }
        _ => exit(1),
    }
}

fn canonical(path: &Path) -> PathBuf {
    std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

fn span_file(span: &Span) -> PathBuf {
    canonical(Path::new(&span.get_filename()))
}

/// Matches a full path or a path suffix; trait methods `<a::T as b::Tr>::m` also match as `T::m` and `Tr::m`.
fn matches(item: &CrateItem, query: &str) -> bool {
    let name = item.name();
    let mut forms = vec![name.clone()];
    if let Some((qualified, method)) = name.strip_prefix('<').and_then(|n| n.rsplit_once(">::"))
        && let Some((self_ty, tr)) = qualified.split_once(" as ")
    {
        forms.push(format!("{}::{method}", short_path(self_ty)));
        forms.push(format!("{}::{method}", short_path(tr)));
    }
    // Inherent methods are `m::<impl Type>::method` (in trait impls `m::<impl Tr for Type>::method`):
    // `Type::method` names them as written.
    if let Some(start) = name.find("<impl ")
        && let Some(end) = name[start..].find(">::")
    {
        let inner = &name[start + "<impl ".len()..start + end];
        let self_ty = inner.rsplit_once(" for ").map_or(inner, |(_, ty)| ty);
        forms.push(format!("{}::{}", short_path(self_ty), &name[start + end + 3..]));
    }
    forms.iter().any(|f| f == query || f.ends_with(&format!("::{query}")))
}

enum Query {
    Name(String),
    Line(usize, Option<usize>),
}

fn is_async_fn(item: &CrateItem) -> bool {
    item.ty().kind().fn_def().is_some_and(|(def, _)| def.asyncness().is_async())
}

/// The function or closure named `query` (full path or path suffix, e.g. `outer::{closure#0}`),
/// preferring those defined in `file`.
fn by_name(items: &[CrateItem], query: &str, file: Option<&Path>) -> Result<CrateItem, String> {
    let mut found: Vec<CrateItem> = items.iter().filter(|item| matches(item, query)).copied().collect();
    if let Some(file) = file
        && found.iter().any(|item| span_file(&item.span()) == file)
    {
        found.retain(|item| span_file(&item.span()) == file);
    }
    match found.as_slice() {
        [item] => Ok(*item),
        [] => Err(format!("no local function matches `{query}`")),
        many => {
            let names: Vec<String> = many.iter().map(|item| item.name()).collect();
            Err(format!("`{query}` is ambiguous: {}", names.join(", ")))
        }
    }
}

/// The innermost function, closure or async block in `file` whose body span contains `line`.
/// The coroutine of an `async fn` stands for the fn itself.
fn by_line(items: &[CrateItem], line: usize, col: Option<usize>, file: &Path) -> Result<CrateItem, String> {
    let spans: Vec<(CrateItem, [usize; 4])> = items
        .iter()
        .filter(|item| {
            let def = item.span();
            def.get_lines().start_line <= line && span_file(&def) == file
        })
        .filter_map(|item| {
            let span = item.expect_body().span;
            let l = span.get_lines();
            let inside = match col {
                Some(col) => (l.start_line, l.start_col) <= (line, col) && (line, col) <= (l.end_line, l.end_col),
                None => l.start_line <= line && line <= l.end_line,
            };
            (inside && span_file(&span) == file)
                .then_some((*item, [l.start_line, l.start_col, l.end_line, l.end_col]))
        })
        .collect();
    let contains = |a: &[usize; 4], b: &[usize; 4]| (a[0], a[1]) <= (b[0], b[1]) && (b[2], b[3]) <= (a[2], a[3]);
    let innermost: Vec<CrateItem> = spans
        .iter()
        .filter(|(item, a)| !spans.iter().any(|(other, b)| other != item && contains(a, b)))
        .map(|(item, _)| *item)
        .collect();
    let lift = |item: CrateItem| match item.def_id().parent() {
        Some(parent) if item.ty().kind().is_coroutine() => {
            items.iter().find(|fun| fun.def_id() == parent && is_async_fn(fun)).copied().unwrap_or(item)
        }
        _ => item,
    };
    match innermost.as_slice() {
        [item] => Ok(lift(*item)),
        [] if !items.iter().any(|item| span_file(&item.span()) == file) => Err(format!(
            "no code of {} is compiled in this package: the file is not in its module tree (no `mod` for it) or sits behind a cfg",
            file.display()
        )),
        [] => Err(format!("no local function or closure contains line {line} of {}", file.display())),
        many => {
            let names: Vec<String> = many.iter().map(|item| item.name()).collect();
            Err(format!("line {line} is in several closures, select one by name or with LINE:COL: {}", names.join(", ")))
        }
    }
}

fn dump(query: &Query, file: Option<&Path>) -> Result<Value, String> {
    let items: Vec<CrateItem> = rustc_public::all_local_items()
        .into_iter()
        .filter(|item| item.kind() == ItemKind::Fn && item.has_body())
        .collect();
    let item = match query {
        Query::Name(name) => by_name(&items, name, file)?,
        Query::Line(line, col) => by_line(&items, *line, *col, file.ok_or("CODEFLOW_LINE needs CODEFLOW_FILE")?)?,
    };
    // An `async fn` only builds its coroutine; the code lives in the coroutine's body.
    let body_item = if is_async_fn(&item) {
        items
            .iter()
            .find(|c| c.def_id().parent() == Some(item.def_id()) && c.ty().kind().is_coroutine())
            .copied()
            .unwrap_or(item)
    } else {
        item
    };
    let body = body_item.expect_body();
    let file = span_file(&item.span());
    let cfg = Cfg {
        body: &body,
        file: file.clone(),
        names: local_names(&body),
    };
    let cleanup = cleanup_blocks(&body);
    let blocks: Vec<Value> = body
        .blocks
        .iter()
        .enumerate()
        .map(|(id, block)| {
            let (kind, label, succ) = cfg.terminator(block);
            json!({
                "id": id,
                "lines": cfg.lines(block),
                "test": cfg.test_span(block),
                "kind": kind,
                "label": label,
                "succ": succ.into_iter().map(|(to, label)| json!({"to": to, "label": label})).collect::<Vec<_>>(),
                "cleanup": cleanup.contains(&id),
            })
        })
        .collect();
    let start_line = item.span().get_lines().start_line;
    Ok(json!({
        "function": item.name(),
        "file": file,
        "line": start_line,
        "entry": 0,
        "blocks": blocks,
        "dataflow": cfg.dataflow(start_line),
    }))
}

/// Blocks reachable from an unwind edge: cleanup blocks only lead to other cleanup blocks.
fn cleanup_blocks(body: &Body) -> HashSet<usize> {
    let mut queue: VecDeque<usize> = body
        .blocks
        .iter()
        .filter_map(|b| match b.terminator.kind.unwind() {
            Some(UnwindAction::Cleanup(t)) => Some(*t),
            _ => None,
        })
        .collect();
    let mut seen = HashSet::new();
    while let Some(b) = queue.pop_front() {
        if seen.insert(b) {
            queue.extend(body.blocks[b].terminator.successors());
        }
    }
    seen
}

fn local_names(body: &Body) -> HashMap<Local, String> {
    body.var_debug_info
        .iter()
        .filter_map(|info| match &info.value {
            VarDebugInfoContents::Place(p) if p.projection.is_empty() => Some((p.local, info.name.clone())),
            _ => None,
        })
        .collect()
}

/// Every place an rvalue or operand touches, and whether the access mutates it (`&mut x`,
/// `&raw mut x`).
struct Places {
    found: Vec<(Place, bool)>,
}

impl MirVisitor for Places {
    fn visit_place(&mut self, place: &Place, ptx: PlaceContext, _location: Location) {
        self.found.push((place.clone(), ptx.is_mutating()));
    }
}

struct Cfg<'a> {
    body: &'a Body,
    file: PathBuf,
    names: HashMap<Local, String>,
}

impl Cfg<'_> {
    /// A place as the source spells it: the variable and its field path (`self.cfg.limit`), derefs
    /// dropped; `None` for a compiler temporary. Stops at an index or a variant downcast.
    fn place_path(&self, place: &Place) -> Option<String> {
        let mut path = self.names.get(&place.local)?.clone();
        let mut ty = self.body.locals().get(place.local)?.ty;
        for elem in &place.projection {
            match elem {
                ProjectionElem::Deref => {}
                ProjectionElem::Field(index, _) => {
                    let name = match ty.kind() {
                        TyKind::RigidTy(RigidTy::Adt(def, _)) if def.num_variants() == 1 => {
                            def.variants()[0].fields().get(*index).map(|field| field.name.clone())
                        }
                        TyKind::RigidTy(RigidTy::Tuple(_)) => Some(index.to_string()),
                        _ => None,
                    };
                    let Some(name) = name else { break };
                    path = format!("{path}.{name}");
                }
                _ => break,
            }
            let Ok(next) = elem.ty(ty) else { break };
            ty = next;
        }
        Some(path)
    }

    /// The defs and uses of the function's named variables and their fields, in MIR order, as
    /// `{"line", "col", "var", "kind": "def"|"use", "how", "block"}`. An assignment, a call's result
    /// or a parameter defines a place; so does a call it is passed to as `&mut` (`v.push(x)`) and a
    /// write through a reference or raw pointer to it, wherever that pointer was kept: a temporary,
    /// a copy of one, or a struct field (`s.p = &raw mut x; ... *s.p = 1`), and a call given the
    /// struct that holds it.
    fn dataflow(&self, start_line: usize) -> Vec<Value> {
        type Key = (Local, Vec<usize>);
        /// The field path of a place up to its first deref (where the pointer is kept), or `None`
        /// when it has none.
        fn key_before_deref(place: &Place) -> Option<Key> {
            let mut fields = Vec::new();
            for elem in &place.projection {
                match elem {
                    ProjectionElem::Field(index, _) => fields.push(*index),
                    ProjectionElem::Deref => return Some((place.local, fields)),
                    _ => return None,
                }
            }
            None
        }
        /// The place's own key when it is only fields (no deref).
        fn key(place: &Place) -> Option<Key> {
            let mut fields = Vec::new();
            for elem in &place.projection {
                match elem {
                    ProjectionElem::Field(index, _) => fields.push(*index),
                    _ => return None,
                }
            }
            Some((place.local, fields))
        }
        let body = self.body;
        let mut events = Vec::new();
        // Places that hold `&mut x` or `&raw mut x`, by where they are kept: the variable they point
        // to, and that place's own key (so pointers kept inside it are reachable through it).
        let mut borrows: HashMap<Key, (String, Option<Key>)> = HashMap::new();
        let push = |events: &mut Vec<Value>, block: usize, span: &Span, var: Option<String>, kind: &str, how: &str| {
            let Some(var) = var else { return };
            if span_file(span) != self.file {
                return;
            }
            let l = span.get_lines();
            if l.start_line == 0 {
                return;
            }
            events.push(json!({"line": l.start_line, "col": l.start_col, "var": var, "kind": kind, "how": how, "block": block}));
        };
        for local in 1..=body.arg_locals().len() {
            if let Some(name) = self.names.get(&local) {
                events.push(json!({"line": start_line, "col": 1, "var": name, "kind": "def", "how": "parameter", "block": 0}));
            }
        }
        // What a read of `place` stands for: the variable a kept pointer points to, else the place.
        let through = |borrows: &HashMap<Key, (String, Option<Key>)>, place: &Place| -> Option<String> {
            key_before_deref(place).and_then(|k| borrows.get(&k).map(|(path, _)| path.clone()))
        };
        // Every variable a call given `key` can write: what the pointers kept in it (or in its fields)
        // point to, and, through a pointer to a struct, the pointers kept inside that struct.
        let reachable = |borrows: &HashMap<Key, (String, Option<Key>)>, start: Key| -> Vec<String> {
            let mut found = Vec::new();
            let mut queue = vec![start];
            let mut seen: HashSet<Key> = HashSet::new();
            while let Some((local, fields)) = queue.pop() {
                if !seen.insert((local, fields.clone())) {
                    continue;
                }
                for ((l, f), (path, inner)) in borrows {
                    if *l == local && f.starts_with(&fields) {
                        found.push(path.clone());
                        if let Some(inner) = inner {
                            queue.push(inner.clone());
                        }
                    }
                }
            }
            found.sort();
            found.dedup();
            found
        };
        for (index, block) in body.blocks.iter().enumerate() {
            for (position, statement) in block.statements.iter().enumerate() {
                let StatementKind::Assign(destination, rvalue) = &statement.kind else { continue };
                let span = &statement.source_info.span;
                let mut places = Places { found: Vec::new() };
                places.visit_rvalue(rvalue, statement_location(body, &index, position));
                let destination_key = key(destination);
                for (place, mutating) in &places.found {
                    let target = through(&borrows, place).or_else(|| self.place_path(place));
                    if *mutating {
                        // `&mut x` / `&raw mut x` kept in the destination (a local or a field).
                        if let (Some(k), Some(target)) = (destination_key.clone(), target) {
                            let pointee = key(place).or_else(|| key_before_deref(place).and_then(|k| borrows.get(&k).and_then(|(_, inner)| inner.clone())));
                            borrows.insert(k, (target, pointee));
                        }
                    } else {
                        push(&mut events, index, span, target, "use", "");
                    }
                }
                // A copy or move of a kept pointer, or a struct built around one, still points there.
                match rvalue {
                    Rvalue::Use(Operand::Copy(source) | Operand::Move(source), _) => {
                        if let (Some(from), Some(to)) = (key(source), destination_key.clone()) {
                            let carried: Vec<(Key, (String, Option<Key>))> = borrows
                                .iter()
                                .filter(|((local, fields), _)| *local == from.0 && fields.starts_with(&from.1))
                                .map(|((_, fields), target)| {
                                    let mut moved = to.1.clone();
                                    moved.extend_from_slice(&fields[from.1.len()..]);
                                    ((to.0, moved), target.clone())
                                })
                                .collect();
                            borrows.extend(carried);
                        }
                    }
                    Rvalue::Aggregate(_, operands) => {
                        if let Some(to) = destination_key.clone() {
                            for (field, operand) in operands.iter().enumerate() {
                                if let Operand::Copy(source) | Operand::Move(source) = operand
                                    && let Some(from) = key(source)
                                    && let Some(target) = borrows.get(&from).cloned()
                                {
                                    let mut fields = to.1.clone();
                                    fields.push(field);
                                    borrows.insert((to.0, fields), target);
                                }
                            }
                        }
                    }
                    _ => {}
                }
                if destination.projection.iter().any(|e| matches!(e, ProjectionElem::Deref)) {
                    match through(&borrows, destination) {
                        Some(target) => push(&mut events, index, span, Some(target), "def", "through a pointer"),
                        None => push(&mut events, index, span, self.place_path(destination), "def", "field"),
                    }
                } else {
                    let how = if destination.projection.is_empty() { "" } else { "field" };
                    push(&mut events, index, span, self.place_path(destination), "def", how);
                }
            }
            let span = &block.terminator.source_info.span;
            match &block.terminator.kind {
                TerminatorKind::Call { args, destination, .. } => {
                    for arg in args {
                        let (Operand::Copy(place) | Operand::Move(place)) = arg else { continue };
                        // Every pointer the argument holds (itself, fields of a struct, pointers kept
                        // in what it points to) lets the callee write.
                        let held = key(place).map(|k| reachable(&borrows, k)).unwrap_or_default();
                        if held.is_empty() {
                            push(&mut events, index, span, through(&borrows, place).or_else(|| self.place_path(place)), "use", "");
                        }
                        for target in held {
                            push(&mut events, index, span, Some(target), "def", "&mut passed to a call");
                        }
                    }
                    if destination.projection.is_empty() {
                        push(&mut events, index, span, self.place_path(destination), "def", "call result");
                    }
                }
                TerminatorKind::SwitchInt { discr: Operand::Copy(place) | Operand::Move(place), .. } => {
                    push(&mut events, index, span, self.place_path(place), "use", "");
                }
                _ => {}
            }
        }
        events
    }

    fn lines(&self, block: &BasicBlock) -> Option<[usize; 2]> {
        let spans = block
            .statements
            .iter()
            .map(|s| &s.source_info.span)
            .chain([&block.terminator.source_info.span]);
        let mut range: Option<[usize; 2]> = None;
        for span in spans.filter(|s| span_file(s) == self.file) {
            let l = span.get_lines();
            if l.start_line == 0 {
                continue; // a dummy span (compiler-made statements)
            }
            range = Some(match range {
                None => [l.start_line, l.end_line],
                Some([a, b]) => [a.min(l.start_line), b.max(l.end_line)],
            });
        }
        range
    }

    /// What a branch tests, as `[start_line, start_col, end_line, end_col]` in the function's file:
    /// for `a || b` each operand's branch has its own span.
    fn test_span(&self, block: &BasicBlock) -> Option<[usize; 4]> {
        let TerminatorKind::SwitchInt { .. } = block.terminator.kind else { return None };
        let span = &block.terminator.source_info.span;
        (span_file(span) == self.file).then(|| {
            let l = span.get_lines();
            [l.start_line, l.start_col, l.end_line, l.end_col]
        })
    }

    fn place(&self, place: &Place) -> String {
        if let Some(info) = self.body.var_debug_info.iter().find(|i| i.value == VarDebugInfoContents::Place(place.clone())) {
            return info.name.clone();
        }
        let mut s = self.names.get(&place.local).cloned().unwrap_or_else(|| format!("_{}", place.local));
        for elem in &place.projection {
            match elem {
                ProjectionElem::Deref => s = format!("*{s}"),
                ProjectionElem::Field(i, _) => s = format!("{s}.{i}"),
                _ => {}
            }
        }
        s
    }

    fn operand(&self, op: &Operand) -> String {
        match op {
            Operand::Copy(p) | Operand::Move(p) => self.place(p),
            Operand::Constant(c) => format!("{}", c.const_.ty()),
            Operand::RuntimeChecks(_) => "runtime checks".into(),
        }
    }

    fn callee(&self, func: &Operand) -> String {
        let Ok(ty) = func.ty(self.body.locals()) else { return self.operand(func) };
        match ty.kind() {
            TyKind::RigidTy(RigidTy::FnDef(def, args)) => match (!has_params(&args)).then(|| Instance::resolve(def, &args)) {
                Some(Ok(instance)) => short_fn(&instance.trimmed_name()),
                _ => short_fn(&def.trimmed_name()),
            },
            _ => self.operand(func),
        }
    }

    fn terminator(&self, block: &BasicBlock) -> (&'static str, String, Vec<(usize, String)>) {
        let mut succ = Vec::new();
        let (kind, label) = match &block.terminator.kind {
            TerminatorKind::Goto { target } => {
                succ.push((*target, String::new()));
                ("goto", "goto".to_string())
            }
            TerminatorKind::SwitchInt { discr, targets } => {
                let (label, names) = self.switch(block, discr);
                let mut covered = HashSet::new();
                for (value, target) in targets.branches() {
                    covered.insert(value);
                    let name = names.iter().find(|(v, _)| *v == value).map(|(_, n)| n.clone());
                    succ.push((target, name.unwrap_or_else(|| value.to_string())));
                }
                let rest: Vec<&String> = names.iter().filter(|(v, _)| !covered.contains(v)).map(|(_, n)| n).collect();
                let other = match rest.as_slice() {
                    [only] => (*only).clone(),
                    _ => "otherwise".to_string(),
                };
                succ.push((targets.otherwise(), other));
                ("switch", label)
            }
            TerminatorKind::Return => ("return", "return".into()),
            TerminatorKind::Resume => ("resume", "resume unwinding".into()),
            TerminatorKind::Abort => ("abort", "abort".into()),
            TerminatorKind::Unreachable => ("unreachable", "unreachable".into()),
            TerminatorKind::Drop { place, target, .. } => {
                succ.push((*target, String::new()));
                ("drop", format!("drop {}", self.place(place)))
            }
            TerminatorKind::Call { func, target, .. } => {
                succ.extend(target.map(|t| (t, "ok".to_string())));
                ("call", format!("call {}(..)", self.callee(func)))
            }
            TerminatorKind::Assert { msg, target, .. } => {
                succ.push((*target, String::new()));
                ("assert", assert_label(msg))
            }
            TerminatorKind::InlineAsm { destination, .. } => {
                succ.extend(destination.map(|t| (t, "ok".to_string())));
                ("asm", "inline asm".into())
            }
        };
        if let Some(UnwindAction::Cleanup(t)) = block.terminator.kind.unwind() {
            succ.push((*t, "unwind".into()));
        }
        (kind, label, succ)
    }

    /// The switch label and, when known, a name for each switch value.
    fn switch(&self, block: &BasicBlock, discr: &Operand) -> (String, Vec<(u128, String)>) {
        let switched = self.operand(discr);
        let source = match discr {
            Operand::Copy(p) | Operand::Move(p) => block.statements.iter().rev().find_map(|s| match &s.kind {
                StatementKind::Assign(dest, Rvalue::Discriminant(src)) if dest == p => Some(src.clone()),
                _ => None,
            }),
            _ => None,
        };
        let Some(src) = source else {
            let is_bool = discr.ty(self.body.locals()).is_ok_and(|t| t.kind().is_bool());
            let names = if is_bool { vec![(0, "false".into()), (1, "true".into())] } else { vec![] };
            return (format!("switch on {switched}"), names);
        };
        let ty = src.ty(self.body.locals()).ok();
        let what = match self.place(&src) {
            _ if ty.is_some_and(|t| t.kind().is_coroutine()) => "async state".to_string(),
            name if name.starts_with('_') || name.starts_with('*') => ty.map_or(name, |t| short_path(&t.to_string())),
            name => name,
        };
        (format!("switch on {switched} (discriminant of {what})"), ty.map(variant_names).unwrap_or_default())
    }
}

/// Whether `args` mention a generic parameter, which `Instance::resolve` cannot resolve.
fn has_params(args: &GenericArgs) -> bool {
    struct FindParam;
    impl Visitor for FindParam {
        type Break = ();
        fn visit_ty(&mut self, ty: &Ty) -> ControlFlow<()> {
            if let TyKind::Param(_) = ty.kind() { ControlFlow::Break(()) } else { ty.super_visit(self) }
        }
        fn visit_const(&mut self, c: &TyConst) -> ControlFlow<()> {
            if let TyConstKind::Param(_) = c.kind() { ControlFlow::Break(()) } else { c.super_visit(self) }
        }
    }
    args.visit(&mut FindParam).is_break()
}

fn variant_names(ty: Ty) -> Vec<(u128, String)> {
    match ty.kind() {
        TyKind::RigidTy(RigidTy::Adt(adt, _)) => {
            adt.variants().iter().map(|v| (adt.discriminant_for_variant(v.idx()).val, v.name())).collect()
        }
        // rustc's coroutine layout: 0 unresumed, 1 returned, 2 poisoned, then one per suspend point.
        TyKind::RigidTy(RigidTy::Coroutine(..)) => {
            let fixed = ["start", "returned", "panicked"].into_iter().enumerate();
            fixed.map(|(v, n)| (v as u128, n.to_string())).chain((3..64).map(|v| (v, format!("resume #{}", v - 3)))).collect()
        }
        _ => vec![],
    }
}

/// Index of the `>` closing the `<` at `open`.
fn closing(s: &str, open: usize) -> usize {
    let mut depth = 0;
    let mut prev = ' ';
    for (i, c) in s[open..].char_indices() {
        match c {
            '<' => depth += 1,
            '>' if prev == '-' => {}
            '>' if depth == 1 => return open + i,
            '>' => depth -= 1,
            _ => {}
        }
        prev = c;
    }
    s.len() - 1
}

/// `a::b::<impl T>::f::<X>` -> `T::f`; `m::<impl Tr for T>::f` -> `<T as Tr>::f`.
fn short_fn(name: &str) -> String {
    let mut name = name.to_string();
    if let Some(open) = name.rfind("<impl ") {
        let close = closing(&name, open);
        let inner = short_path(&name[open + 6..close]);
        let ty = match inner.split_once(" for ") {
            Some((tr, ty)) => format!("<{ty} as {tr}>"),
            None => inner,
        };
        name = ty + &name[close + 1..];
    }
    while let Some(start) = name.find("::<") {
        let close = closing(&name, start + 2);
        name.replace_range(start..=close, "");
    }
    name
}

/// `std::result::Result<u32, std::num::ParseIntError>` -> `Result<u32, ParseIntError>`.
fn short_path(path: &str) -> String {
    let mut out = String::new();
    let mut segment = String::new();
    let mut chars = path.chars().peekable();
    while let Some(c) = chars.next() {
        if c.is_alphanumeric() || c == '_' {
            segment.push(c);
        } else if c == ':' && chars.peek() == Some(&':') {
            chars.next();
            segment.clear();
        } else {
            out.push_str(&segment);
            segment.clear();
            out.push(c);
        }
    }
    out + &segment
}

fn assert_label(msg: &AssertMessage) -> String {
    match msg {
        AssertMessage::BoundsCheck { .. } => "assert index < len".into(),
        AssertMessage::Overflow(op, ..) => format!("assert !overflow ({op:?})"),
        AssertMessage::OverflowNeg(_) => "assert !overflow (Neg)".into(),
        AssertMessage::DivisionByZero(_) | AssertMessage::RemainderByZero(_) => "assert divisor != 0".into(),
        other => format!("assert: {}", other.description().unwrap_or("check")),
    }
}
