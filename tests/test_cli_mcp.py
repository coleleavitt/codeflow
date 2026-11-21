"""The command line maps its flags onto cfg's keywords and finds a bare symbol's file; the MCP server
answers initialize, ping, tools/list and tools/call, ignores notifications and reports protocol errors;
the standalone LSP client turns server answers into rlm.lsp's item shapes."""

import asyncio
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeflow import _lsp_client, cli  # noqa: E402


def project(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp()).resolve()
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    return root


def call(message, tool=None):
    return asyncio.run(cli.handle(message, tool) if tool else cli.handle(message))


class Arguments(unittest.TestCase):
    def test_flags_become_cfg_keywords_and_absent_ones_stay_default(self):
        args = cli.parser().parse_args(["cfg", "a.rs", "run", "--callers", "2", "--callees", "1", "--var", "self.x",
                                        "--refs", "--dominators", "--follow", "1", "--follow-limit", "5", "--level", "mir",
                                        "--cleanup", "--root", "/r", "--json"])
        self.assertEqual(cli.cfg_kwargs(vars(args)), {"callers": 2, "callees": 1, "follow": 1, "follow_limit": 5, "var": "self.x",
                                                      "refs": True, "dominators": True, "level": "mir", "cleanup": True, "root": "/r"})
        self.assertEqual((args.target, args.function, args.json), ("a.rs", "run", True))
        bare = cli.parser().parse_args(["cfg", "a.rs", "--function", "run"])
        self.assertEqual(cli.cfg_kwargs(vars(bare)), {})
        self.assertEqual(bare.function_flag, "run")

    def test_a_line_is_an_int_and_line_col_stays_a_string(self):
        self.assertEqual(cli.function_arg("120"), 120)
        self.assertEqual(cli.function_arg("120:9"), "120:9")
        self.assertEqual(cli.function_arg("Type::run"), "Type::run")
        self.assertEqual(cli.function_arg(7), 7)
        self.assertIsNone(cli.function_arg(None))

    def test_a_bare_symbol_is_found_in_the_project(self):
        root = project({"src/a.py": "def caller():\n    return helper()\n", "src/b.py": "def helper():\n    return 1\n",
                        "src/lib.rs": "struct R;\nimpl R { fn go(&self) {} }\n"})
        self.assertEqual(cli.resolve_target("helper", None, str(root)), (root / "src/b.py", "helper"))
        self.assertEqual(cli.resolve_target("R::go", None, str(root)), (root / "src/lib.rs", "R::go"))
        self.assertEqual(cli.resolve_target(str(root / "src/a.py"), 2, None), (root / "src/a.py", 2))

    def test_a_symbol_in_two_files_or_none_asks_for_the_file(self):
        root = project({"a.py": "def h():\n    pass\n", "b.py": "def h():\n    pass\n"})
        with self.assertRaisesRegex(LookupError, r"several files \(a.py:1, b.py:1\)"):
            cli.resolve_target("h", None, str(root))
        with self.assertRaisesRegex(LookupError, "no definition of it was found"):
            cli.resolve_target("missing", None, str(root))

    def test_a_file_needs_a_function_and_a_missing_file_with_one_is_an_error(self):
        root = project({"a.py": "def f():\n    pass\n"})
        with self.assertRaisesRegex(ValueError, "name the function"):
            cli.resolve_target(str(root / "a.py"), None, None)
        with self.assertRaises(FileNotFoundError):
            cli.resolve_target(str(root / "nope.py"), "f", None)

    def test_the_command_prints_the_graph(self):
        root = project({"a.py": "def f(x):\n    if x:\n        return 1\n    return 2\n"})
        run = subprocess.run([sys.executable, "-m", "codeflow", "cfg", str(root / "a.py"), "f", "--dominators"],
                             capture_output=True, text=True, timeout=120,
                             env={"PYTHONPATH": str(Path(cli.__file__).parents[1]), "PATH": "/usr/bin:/bin",
                                  "CODEFLOW_LSP_SETTINGS": "/nonexistent"})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("f  a.py:1   [control flow", run.stdout)
        self.assertIn("if x", run.stdout)
        self.assertIn("dominated by", run.stdout)


class JsonRpc(unittest.TestCase):
    def test_initialize_echoes_a_known_protocol_version_else_offers_the_newest(self):
        answer = call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(answer["id"], 1)
        self.assertEqual(answer["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(answer["result"]["serverInfo"]["name"], "codeflow")
        self.assertIn("tools", answer["result"]["capabilities"])
        newer = call({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}})
        self.assertEqual(newer["result"]["protocolVersion"], cli.PROTOCOL_VERSIONS[0])

    def test_ping_and_tools_list(self):
        self.assertEqual(call({"jsonrpc": "2.0", "id": "p", "method": "ping"}), {"jsonrpc": "2.0", "id": "p", "result": {}})
        tools = call({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["codeflow_cfg", "codeflow_callers", "codeflow_callees"])
        schema = tools[0]["inputSchema"]["properties"]
        for name in ("path", "function", "callers", "callees", "follow", "var", "refs", "dominators", "level", "cleanup", "root"):
            self.assertIn(name, schema)

    def test_notifications_get_no_answer(self):
        self.assertIsNone(call({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(call({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}))

    def test_protocol_errors(self):
        self.assertEqual(call({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})["error"]["code"], -32601)
        self.assertEqual(call({"id": 5, "method": "ping"})["error"]["code"], -32600)
        self.assertEqual(call([1, 2])["error"], {"code": -32600, "message": "invalid request"})
        unknown = call({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(unknown["error"]["code"], -32602)

    def test_tools_call_returns_text_and_a_failure_is_an_error_result(self):
        seen = []

        async def tool(name, arguments):
            seen.append((name, arguments))
            if arguments.get("fail"):
                raise LookupError("no such function")
            return "graph text"

        ok = call({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                   "params": {"name": "codeflow_cfg", "arguments": {"path": "a.py", "function": "f", "callers": 1}}}, tool)
        self.assertEqual(ok["result"], {"content": [{"type": "text", "text": "graph text"}], "isError": False})
        self.assertEqual(seen, [("codeflow_cfg", {"path": "a.py", "function": "f", "callers": 1})])
        failed = call({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                       "params": {"name": "codeflow_callers", "arguments": {"path": "a.py", "fail": True}}}, tool)
        self.assertEqual(failed["result"], {"content": [{"type": "text", "text": "LookupError: no such function"}], "isError": True})

    def test_the_real_tool_runs_cfg(self):
        root = project({"a.py": "def f(x):\n    while x:\n        x -= 1\n    return x\n"})
        answer = call({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                       "params": {"name": "codeflow_cfg", "arguments": {"path": str(root / "a.py"), "function": "f", "var": "x"}}},
                      lambda name, arguments: _offline(cli.call_tool(name, arguments)))
        self.assertFalse(answer["result"]["isError"], answer)
        self.assertIn("`x`:", answer["result"]["content"][0]["text"])

    def test_serve_reads_lines_and_writes_one_answer_per_request(self):
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n", b"\n", b"not json\n",
                 json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode() + b"\n",
                 json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode() + b"\n", b""]

        async def read_line():
            return lines.pop(0)

        out = io.BytesIO()
        stdout = sys.stdout
        asyncio.run(cli.serve(read_line, out))
        self.assertIs(sys.stdout, stdout)
        answers = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([a.get("id") for a in answers], [1, None, 2])
        self.assertEqual(answers[1]["error"]["code"], -32700)


async def _offline(work):
    """No language server in the unit test: the standalone client answers `rlm.lsp` and reports none configured."""
    servers = _lsp_client.configured_servers
    saved = {name: sys.modules.get(name) for name in ("rlm", "rlm.lsp")}
    _lsp_client.configured_servers = lambda: {}
    cli.use_standalone_lsp()
    try:
        return await work
    finally:
        _lsp_client.configured_servers = servers
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class LspShapes(unittest.TestCase):
    def test_locations_and_links_become_one_based_items(self):
        span = {"start": {"line": 4, "character": 2}, "end": {"line": 4, "character": 7}}
        self.assertEqual(_lsp_client.location_items([{"uri": "file:///r/a%20b.rs", "range": span}]),
                         [{"path": "/r/a b.rs", "line": 5, "character": 3, "end_line": 5, "end_character": 8}])
        link = {"targetUri": "file:///r/c.rs", "targetRange": span, "targetSelectionRange": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}}
        self.assertEqual(_lsp_client.location_items(link)[0]["line"], 1)
        self.assertEqual(_lsp_client.location_items(None), [])

    def test_call_items_and_hover_text(self):
        item = {"name": "run", "kind": 12, "uri": "file:///r/a.rs", "range": {"start": {"line": 9, "character": 0}, "end": {"line": 12, "character": 1}}}
        self.assertEqual(_lsp_client.call_item(item), {"name": "run", "kind": "Function", "detail": None, "path": "/r/a.rs", "line": 10, "character": 1})
        self.assertEqual(_lsp_client.hover_text({"contents": {"kind": "markdown", "value": "fn run()"}}), "fn run()")
        self.assertEqual(_lsp_client.hover_text({"contents": ["a", {"language": "rust", "value": "b"}]}), "a\nb")
        self.assertEqual(_lsp_client.hover_text(None), "")

    def test_the_server_for_a_suffix_comes_from_the_settings(self):
        servers = {"rust": {"command": "ra", "extensionToLanguage": {".rs": "rust"}}}
        self.assertEqual(_lsp_client.server_for(".rs", servers), ("rust", servers["rust"], "rust"))
        self.assertIsNone(_lsp_client.server_for(".py", servers))

    def test_no_server_for_a_file_is_unavailable_not_an_error(self):
        root = project({"a.py": "def f():\n    pass\n"})
        answer = asyncio.run(_offline(_lsp_client.hover(str(root / "a.py"), 1, 5)))
        self.assertFalse(answer.available)
        self.assertIn("no language server is configured for .py", answer.text)

    def test_the_rlm_import_resolves_to_the_client_outside_the_kernel(self):
        saved = {name: sys.modules.get(name) for name in ("rlm", "rlm.lsp")}
        try:
            cli.use_standalone_lsp()
            from rlm import lsp

            self.assertIs(lsp, _lsp_client)
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


if __name__ == "__main__":
    unittest.main()
