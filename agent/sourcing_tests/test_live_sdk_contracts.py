"""可选 SDK 合约测试：真实 SDK + 本地 MockTransport，无网络与密钥消耗。"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from sourcing_agent.adapters.models import AnthropicChatModel, OpenAIChatModel
from sourcing_agent.adapters.snapshot import demo_records, publish_snapshot
from sourcing_agent.bootstrap import create_runner

QUESTION = "查 HS 841891，2025 年全年"


class SdkContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "trades.sqlite"
        publish_snapshot(self.snapshot, demo_records(), "SYNTHETIC SDK test")

    @unittest.skipUnless(importlib.util.find_spec("openai"), "optional live SDK not installed")
    def test_real_openai_sdk_executes_complete_tool_loop(self) -> None:
        import httpx
        import openai

        requests = []

        def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            name = "find_supplier_candidates" if len(requests) == 1 else "submit_findings"
            return httpx.Response(
                200,
                json={
                    "id": "completion",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "tool-" + str(len(requests)),
                                        "type": "function",
                                        "function": {"name": name, "arguments": "{}"},
                                    }
                                ],
                            },
                        }
                    ],
                },
            )

        model = OpenAIChatModel("test", "fake-local-key", "https://example.invalid/v1")
        model.client.close()
        model.client = openai.OpenAI(
            api_key="fake-local-key",
            base_url="https://example.invalid/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_retries=0,
        )
        self.addCleanup(model.client.close)
        runner = create_runner(self.snapshot, self.root, "sdk", model)
        result = runner.run(runner.create_session().id, QUESTION)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(requests), 2)
        self.assertTrue(any(m["role"] == "tool" for m in requests[1]["messages"]))

    @unittest.skipUnless(importlib.util.find_spec("anthropic"), "optional live SDK not installed")
    def test_real_anthropic_sdk_executes_complete_tool_loop(self) -> None:
        import anthropic

        # Anthropic SDK 2.x uses httpx2; older compatible SDK uses httpx.
        try:
            import httpx2 as transport
        except ImportError:
            import httpx as transport
        requests = []

        def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            name = "find_supplier_candidates" if len(requests) == 1 else "submit_findings"
            return transport.Response(
                200,
                json={
                    "id": "message",
                    "type": "message",
                    "role": "assistant",
                    "model": "test",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-" + str(len(requests)),
                            "name": name,
                            "input": {},
                        }
                    ],
                    "stop_reason": "tool_use",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                },
            )

        model = AnthropicChatModel("test", "fake-local-key", "https://example.invalid")
        model.client.close()
        model.client = anthropic.Anthropic(
            api_key="fake-local-key",
            base_url="https://example.invalid",
            http_client=transport.Client(transport=transport.MockTransport(handler)),
            max_retries=0,
        )
        self.addCleanup(model.client.close)
        runner = create_runner(self.snapshot, self.root, "sdk", model)
        result = runner.run(runner.create_session().id, QUESTION)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(requests), 2)
        self.assertTrue(
            any(
                isinstance(m["content"], list)
                and any(b["type"] == "tool_result" for b in m["content"])
                for m in requests[1]["messages"]
            )
        )


if __name__ == "__main__":
    unittest.main()
