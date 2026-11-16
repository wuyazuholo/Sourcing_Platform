"""SDK 仅在模型适配器使用；供应商消息扩展随原生响应保留。"""

import json
import os

from sourcing_agent.config import request_options
from sourcing_agent.harness.contracts import (
    ChatModel,
    JsonObject,
    ModelReply,
    ModelRequest,
    ToolCall,
)


class AnthropicChatModel:
    def __init__(
        self, model: str, api_key: str, base_url: str | None = None, timeout: float = 60
    ) -> None:
        try:
            from anthropic import Anthropic
        except ImportError as error:
            raise RuntimeError("请先安装项目的 live 依赖：pip install -e .[live]") from error
        self.model = model
        self.client = Anthropic(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)

    def generate(self, request: ModelRequest) -> ModelReply:
        messages = []
        for message in request.messages:
            if message.role == "tool":
                content = [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.content,
                    }
                ]
                role = "user"
                if messages and messages[-1]["role"] == "user":
                    previous = messages[-1]["content"]
                    if isinstance(previous, str):
                        previous = [{"type": "text", "text": previous}]
                    messages[-1]["content"] = [*previous, *content]
                    continue
            elif message.role == "assistant":
                role = "assistant"
                if message.provider_data:
                    if message.provider_data.get("provider") != "anthropic":
                        raise ValueError("历史模型协议不同，请创建新会话")
                    content = message.provider_data["content"]
                else:
                    content = []
                    if message.content:
                        content.append({"type": "text", "text": message.content})
                    content.extend(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                        for call in message.tool_calls
                    )
            else:
                role, content = "user", message.content
            messages.append({"role": role, "content": content})
        kwargs = {
            "model": self.model,
            "system": request.system,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
        }
        if request.tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in request.tools
            ]
        response = self.client.messages.create(**kwargs)
        blocks = [b.model_dump(mode="json", exclude_none=True) for b in response.content]
        return ModelReply(
            text="\n".join(b.text for b in response.content if b.type == "text"),
            calls=[
                ToolCall(id=b.id, name=b.name, arguments=b.input)
                for b in response.content
                if b.type == "tool_use"
            ],
            finish={
                "end_turn": "stop",
                "stop_sequence": "stop",
                "tool_use": "tool_calls",
                "max_tokens": "length",
            }.get(response.stop_reason, "length"),
            provider_data={"provider": "anthropic", "content": blocks},
        )


class OpenAIChatModel:
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 60,
        options: JsonObject | None = None,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as error:
            raise RuntimeError("请先安装项目的 live 依赖：pip install -e .[live]") from error
        self.model = model
        self.options = options or {}
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)

    def generate(self, request: ModelRequest) -> ModelReply:
        messages = [{"role": "system", "content": request.system}]
        for message in request.messages:
            if message.role == "assistant" and message.provider_data:
                if message.provider_data.get("provider") != "openai":
                    raise ValueError("历史模型协议不同，请创建新会话")
                messages.append(message.provider_data["message"])
                continue
            item = {"role": message.role, "content": message.content}
            if message.tool_call_id:
                item["tool_call_id"] = message.tool_call_id
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": json.dumps(c.arguments, ensure_ascii=False),
                        },
                    }
                    for c in message.tool_calls
                ]
            messages.append(item)
        kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
        }
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
        if self.options:
            kwargs["extra_body"] = self.options
        response = self.client.chat.completions.create(**kwargs)
        if len(response.choices) != 1:
            raise ValueError("模型必须返回一个候选")
        choice = response.choices[0]
        message = choice.message
        calls = []
        for call in message.tool_calls or []:
            if call.type != "function":
                raise ValueError("只支持 function 工具调用")
            try:
                arguments = json.loads(call.function.arguments)
                calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))
            except (ValueError, TypeError):
                # 不向工具分派半个 JSON；让 Loop 有界请求模型重发完整动作。
                return ModelReply(finish="length")
        return ModelReply(
            text=message.content or "",
            calls=calls,
            finish={"stop": "stop", "tool_calls": "tool_calls", "length": "length"}.get(
                choice.finish_reason, "length"
            ),
            provider_data={
                "provider": "openai",
                "message": message.model_dump(mode="json", exclude_none=True, exclude_unset=True),
            },
        )


def configured_model(provider: str) -> ChatModel:
    if provider == "anthropic":
        key = os.getenv("ANTHROPIC_API_KEY")
        model = os.getenv("MODEL_ID")
        if not key or not model:
            raise ValueError("请设置 ANTHROPIC_API_KEY 和 MODEL_ID")
        return AnthropicChatModel(model, key, os.getenv("ANTHROPIC_BASE_URL"))
    key = os.getenv("SOURCING_MODEL_API_KEY")
    model = os.getenv("SOURCING_MODEL_NAME")
    if not key or not model:
        raise ValueError("请设置 SOURCING_MODEL_API_KEY 和 SOURCING_MODEL_NAME")
    return OpenAIChatModel(
        model, key, os.getenv("SOURCING_MODEL_BASE_URL"), options=request_options()
    )
