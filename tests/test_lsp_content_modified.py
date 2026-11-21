"""A language server answers "content modified" (LSP -32801) while it re-indexes; the spec says to
ask again. Without the retry a call hierarchy came back empty and the caller tree printed nothing."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeflow import _lsp_client  # noqa: E402


class FakeServer:
    name = "rust"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def request(self, method, params, timeout=None):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class ContentModifiedRetry(unittest.TestCase):
    def test_a_modified_content_reply_is_asked_again(self):
        server = FakeServer([_lsp_client.LspError(-32801, "content modified"), [{"from": {"name": "caller"}, "fromRanges": []}]])
        calls = asyncio.run(_lsp_client._settled(server, "callHierarchy/incomingCalls", {}))
        self.assertEqual(server.calls, 2)
        self.assertEqual(calls[0]["from"]["name"], "caller")

    def test_other_errors_are_not_retried(self):
        server = FakeServer([_lsp_client.LspError(-32601, "method not found")])
        with self.assertRaises(_lsp_client.LspError):
            asyncio.run(_lsp_client._settled(server, "callHierarchy/incomingCalls", {}))
        self.assertEqual(server.calls, 1)

    def test_a_server_that_stays_busy_gives_up_after_a_few_asks(self):
        busy = [_lsp_client.LspError(-32801, "content modified")] * 10
        server = FakeServer(busy)
        with self.assertRaises(_lsp_client.LspError):
            asyncio.run(_lsp_client._settled(server, "callHierarchy/incomingCalls", {}, attempts=3, pause=0))
        self.assertEqual(server.calls, 3)


if __name__ == "__main__":
    unittest.main()
