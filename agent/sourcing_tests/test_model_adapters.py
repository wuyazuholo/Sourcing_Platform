"""通过替身 SDK 验收边界，确保协议与供应商扩展能跨请求回传。"""

import json
import sys
import types
import unittest
from unittest.mock import Mock, patch

from sourcing_agent.adapters.models import AnthropicChatModel, OpenAIChatModel
from sourcing_agent.harness.contracts import Message, ModelRequest, ToolDefinition


class ModelAdapterTests(unittest.TestCase):
    def test_openai_preserves_reasoning_and_pairs_tool_results(self) -> None:
        raw = {
            "role": "assistant",
            "content": None,
            "reasoning_content": "vendor reasoning",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "find_supplier_candidates", "arguments": "{}"},
                }
            ],
        }
        message = types.SimpleNamespace(
            content=None,
            tool_calls=[
                types.SimpleNamespace(
                    type="function",
                    id="call-1",
                    function=types.SimpleNamespace(name="find_supplier_candidates", arguments="{}"),
                )
            ],
            model_dump=lambda **kwargs: raw,
        )
        response = types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=message, finish_reason="tool_calls")]
        )
        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=Mock(return_value=response))
            )
        )
        module = types.ModuleType("openai")
        module.OpenAI = Mock(return_value=client)
        with patch.dict(sys.modules, {"openai": module}):
            model = OpenAIChatModel(
                "model-name",
                "private-key",
                "https://example.invalid/v1",
                options={"thinking": {"type": "disabled"}},
            )
        request = ModelRequest(
            system="fixed system",
            messages=[Message(role="user", content="query")],
            tools=[
                ToolDefinition(
                    name="find_supplier_candidates",
                    description="find",
                    parameters={"type": "object"},
                )
            ],
        )
        reply = model.generate(request)
        self.assertEqual(reply.calls[0].id, "call-1")
        self.assertEqual(reply.provider_data["message"]["reasoning_content"], "vendor reasoning")
        request.messages.extend(
            [
                Message(
                    role="assistant", tool_calls=reply.calls, provider_data=reply.provider_data
                ),
                Message(role="tool", content='{"rows":[]}', tool_call_id="call-1"),
            ]
        )
        model.generate(request)
        payload = client.chat.completions.create.call_args.kwargs
        self.assertEqual(payload["messages"][-2], raw)
        self.assertEqual(payload["messages"][-1]["tool_call_id"], "call-1")
        self.assertEqual(payload["tools"][0]["function"]["name"], "find_supplier_candidates")
        self.assertNotIn("private-key", json.dumps(payload))
        self.assertEqual(payload["extra_body"], {"thinking": {"type": "disabled"}})

    def test_openai_malformed_arguments_do_not_reach_tool_dispatch(self) -> None:
        message = types.SimpleNamespace(
            content=None,
            tool_calls=[
                types.SimpleNamespace(
                    type="function",
                    id="broken",
                    function=types.SimpleNamespace(name="query", arguments="{partial"),
                )
            ],
        )
        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(
                    create=lambda **kwargs: types.SimpleNamespace(
                        choices=[types.SimpleNamespace(message=message, finish_reason="tool_calls")]
                    )
                )
            )
        )
        module = types.ModuleType("openai")
        module.OpenAI = lambda **kwargs: client
        with patch.dict(sys.modules, {"openai": module}):
            model = OpenAIChatModel("model-name", "key")
        reply = model.generate(
            ModelRequest(system="system", messages=[Message(role="user", content="q")])
        )
        self.assertEqual(reply.finish, "length")
        self.assertEqual(reply.calls, [])

    def test_anthropic_preserves_thinking_signature_and_batches_results(self) -> None:
        thinking = {"type": "thinking", "thinking": "vendor reasoning", "signature": "signature-1"}
        tool = {"type": "tool_use", "id": "tool-1", "name": "query", "input": {}}
        response = types.SimpleNamespace(
            content=[
                types.SimpleNamespace(type="thinking", model_dump=lambda **kwargs: thinking),
                types.SimpleNamespace(
                    type="tool_use",
                    id="tool-1",
                    name="query",
                    input={},
                    model_dump=lambda **kwargs: tool,
                ),
            ],
            stop_reason="tool_use",
        )
        client = types.SimpleNamespace(
            messages=types.SimpleNamespace(create=Mock(return_value=response))
        )
        module = types.ModuleType("anthropic")
        module.Anthropic = Mock(return_value=client)
        with patch.dict(sys.modules, {"anthropic": module}):
            model = AnthropicChatModel("model-name", "private-key")
        request = ModelRequest(system="system", messages=[Message(role="user", content="q")])
        reply = model.generate(request)
        self.assertEqual(reply.finish, "tool_calls")
        request.messages.extend(
            [
                Message(
                    role="assistant", tool_calls=reply.calls, provider_data=reply.provider_data
                ),
                Message(role="tool", content="first", tool_call_id="tool-1"),
            ]
        )
        model.generate(request)
        payload = client.messages.create.call_args.kwargs
        self.assertEqual(payload["messages"][-2]["content"][0], thinking)
        self.assertEqual(payload["messages"][-1]["content"][0]["tool_use_id"], "tool-1")

    def test_history_from_other_protocol_is_rejected(self) -> None:
        module = types.ModuleType("anthropic")
        module.Anthropic = lambda **kwargs: types.SimpleNamespace()
        with patch.dict(sys.modules, {"anthropic": module}):
            model = AnthropicChatModel("model-name", "key")
        with self.assertRaisesRegex(ValueError, "协议不同"):
            model.generate(
                ModelRequest(
                    system="system",
                    messages=[
                        Message(
                            role="assistant",
                            content="old",
                            provider_data={
                                "provider": "openai",
                                "message": {"role": "assistant", "content": "old"},
                            },
                        ),
                    ],
                )
            )


if __name__ == "__main__":
    unittest.main()
