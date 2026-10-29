//! `RUSTC_WORKSPACE_WRAPPER` that dumps one function's MIR control-flow graph as JSON.
//!
//! Invoked as `<driver> <rustc> <args...>`. When `CODEFLOW_FN` is set and the args carry
//! `--cfg codeflow_probe*`, the crate is analyzed through `rustc_public`, the CFG of the matching
//! local function is written to `CODEFLOW_OUT`, and compilation stops. Otherwise the real rustc runs.
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
use rustc_public::visitor::{Visitable, Visitor};
use rustc_public::{CrateDef, CrateItem, ItemKind};
use serde_json::{Value, json};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let probe = args.windows(2).any(|w| w[0] == "--cfg" && w[1].starts_with("codeflow_probe"));
    let Some(query) = std::env::var("CODEFLOW_FN").ok().filter(|_| probe) else {
        let err = Command::new(&args[1]).args(&args[2..]).exec();
        eprintln!("codeflow: cannot run {}: {err}", args[1]);
        exit(1);
    };
    let file = std::env::var("CODEFLOW_FILE").ok().map(|f| canonical(Path::new(&f)));
    let out = std::env::var("CODEFLOW_OUT").expect("CODEFLOW_OUT is not set");
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
    forms.iter().any(|f| f == query || f.ends_with(&format!("::{query}")))
}

fn dump(query: &str, file: Option<&Path>) -> Result<Value, String> {
    let items = rustc_public::all_local_items();
    let mut found: Vec<CrateItem> = items
        .iter()
        .filter(|item| item.kind() == ItemKind::Fn && item.has_body() && matches(item, query))
        .copied()
        .collect();
    if let Some(file) = file
        && found.iter().any(|item| span_file(&item.span()) == file)
    {
        found.retain(|item| span_file(&item.span()) == file);
    }
    let item = match found.as_slice() {
        [item] => *item,
        [] => return Err(format!("no local function matches `{query}`")),
        many => {
            let names: Vec<String> = many.iter().map(|item| item.name()).collect();
            return Err(format!("`{query}` is ambiguous: {}", names.join(", ")));
        }
    };
    // An `async fn` only builds its coroutine; the code lives in the coroutine's body.
    let is_async = item.ty().kind().fn_def().is_some_and(|(def, _)| def.asyncness().is_async());
    let body_item = if is_async {
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
    Ok(json!({
        "function": item.name(),
        "file": file,
        "line": item.span().get_lines().start_line,
        "entry": 0,
        "blocks": blocks,
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

struct Cfg<'a> {
    body: &'a Body,
    file: PathBuf,
    names: HashMap<Local, String>,
}

impl Cfg<'_> {
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
