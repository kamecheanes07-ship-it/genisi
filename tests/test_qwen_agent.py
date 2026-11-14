import json
import unittest
from unittest.mock import AsyncMock, patch

from app.agent import Agent


class QwenSpaceAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_uses_space_gateway_and_workspace_tool(self):
        agent = Agent()
        call = json.dumps({"tool_call": {"name": "list_files", "arguments": {"path": "."}}})
        responses = [{"text": call}, {"text": "وجدت ملفاً."}]
        gateway = AsyncMock(side_effect=responses)
        execute = AsyncMock(return_value={"items": [{"path": "app.py"}]})
        with patch("app.main.qwen_model_once", gateway), patch.object(agent, "_execute", execute):
            events = [event async for event in agent.stream_events("u", "w", "p", "اعرض الملفات")]
        self.assertEqual([event["type"] for event in events], ["tool_start", "tool", "answer_delta", "final"])
        self.assertEqual(events[-1]["content"], "وجدت ملفاً.")
        self.assertEqual(execute.await_args.args, ("list_files", {"path": "."}, "u", "w", "p"))
        first = gateway.await_args_list[0].args[1]["data"]
        second = gateway.await_args_list[1].args[1]["data"]
        self.assertEqual(len(first), 9)
        self.assertEqual(first[0], "اعرض الملفات")
        self.assertIn("Workspace tools", first[5])
        self.assertIn("app.py", second[0])
        self.assertEqual(second[4][-1]["content"], call)

    async def test_rejects_unrecognized_or_malformed_tool_calls(self):
        agent = Agent()
        for text in (
            'Example: {"tool_call":{"name":"list_files","arguments":{}}}',
            '{"tool_call":{"name":"unknown","arguments":{}}}',
            '{"tool_call":{"name":"write_file","arguments":{"path":"x"}}}',
            '{"tool_call":{"name":"list_files","arguments":{"path":42}}}',
            '{"tool_call":{"name":"list_files","arguments":{},"extra":1}}',
        ):
            with self.subTest(text=text):
                self.assertIsNone(agent._tool_call(text))
                with patch.object(agent, "_space_reply", AsyncMock(return_value=text)), patch.object(agent, "_execute", AsyncMock()) as execute:
                    events = [event async for event in agent.stream_events("u", "w", "p", "test")]
                    execute.assert_not_awaited()
                    self.assertEqual(events[-1]["content"], text)

    async def test_gateway_errors_propagate_and_run_uses_same_path(self):
        agent = Agent()
        with patch.object(agent, "_space_reply", AsyncMock(return_value="done")):
            self.assertEqual(await agent.run("u", "w", "p", "hi"), "done")
        with patch.object(agent, "_space_reply", AsyncMock(side_effect=RuntimeError("Space unavailable"))):
            with self.assertRaisesRegex(RuntimeError, "Space unavailable"):
                await agent.run("u", "w", "p", "hi")


class QwenStreamParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_keepalives_do_not_cancel_pending_read_and_crlf_may_split(self):
        import asyncio
        from app.main import _qwen_sse_events

        class Upstream:
            async def aiter_bytes(self):
                await asyncio.sleep(0.035)
                yield b"event: generating\r"
                yield b"\ndata: hello\r\n\r"
                yield b"\nevent: complete\r\ndata: done\r\n\r\n"

        events = [event async for event in _qwen_sse_events(Upstream(), keepalive_interval=0.01, idle_limit=1)]
        self.assertGreaterEqual(events.count(None), 1)
        self.assertEqual([(event[0], event[1]) for event in events if event], [
            ("generating", "hello"), ("complete", "done")
        ])

    async def test_idle_timeout_cancels_pending_read(self):
        import asyncio
        from app.main import _qwen_sse_events

        closed = asyncio.Event()
        class Upstream:
            async def aiter_bytes(self):
                try:
                    await asyncio.Event().wait()
                    yield b"unreachable"
                finally:
                    closed.set()

        with self.assertRaises(TimeoutError):
            async for _ in _qwen_sse_events(Upstream(), keepalive_interval=0.01, idle_limit=0.03):
                pass
        self.assertTrue(closed.is_set())
