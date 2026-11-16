"""Optional MCP SDK adapter. Each bounded request owns and closes its connection."""

import asyncio
import hashlib
import json
import os
import re
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sourcing_agent.harness.contracts import JsonObject, ToolDefinition, ToolInputError
from sourcing_agent.harness.store import encode
from sourcing_agent.mcp_config import McpConfig, McpServerConfig
from sourcing_agent.sourcing.external import ExternalObservation


def _check_schema(schema: object) -> None:
    from jsonschema import Draft202012Validator

    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("MCP 工具参数必须为 object JSON Schema")
    if len(encode(schema)) > 16000:
        raise ValueError("MCP 工具参数定义过大")
    pending = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if any(key in value for key in ("$id", "$dynamicRef", "$recursiveRef")):
                raise ValueError("MCP 参数定义不支持自定义解析范围或动态引用")
            if "$ref" in value and (
                not isinstance(value["$ref"], str) or not value["$ref"].startswith("#/")
            ):
                raise ValueError("MCP 参数定义只允许本地 JSON 引用")
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        raise ValueError("MCP 工具参数定义无效") from None


def _urls(value: object) -> list[str]:
    found: list[str] = []
    pending = [value]
    while pending and len(found) < 20:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            pending.extend(reversed(item))
        elif isinstance(item, str):
            for match in re.findall(r"https?://[^\s<>\"\[\]{}]+", item):
                url = match.rstrip(".,;)")
                parts = urlsplit(url)
                if (
                    parts.hostname
                    and not parts.username
                    and not parts.password
                    and url not in found
                ):
                    found.append(url)
    return found[:20]


def _nested_schema(schema: dict) -> dict:
    """Local references are rooted at the remote arguments, now nested in our wrapper."""
    nested = json.loads(encode(schema))
    pending = [nested]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if "$ref" in item:
                item["$ref"] = "#/properties/arguments/" + item["$ref"][2:]
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return nested


class McpGateway:
    def __init__(self, config: McpConfig) -> None:
        try:
            from jsonschema import Draft202012Validator  # noqa: F401
            from mcp import Client  # noqa: F401
        except ImportError:
            raise RuntimeError('MCP 可选依赖未安装，请运行 pip install -e ".[mcp]"') from None
        self.config = config
        self._bindings: dict[str, tuple[McpServerConfig, str, dict]] = {}
        self._definitions: list[ToolDefinition] = []
        for server in config.servers:
            catalog = self._request(server)
            by_name = {tool["name"]: tool for tool in catalog}
            if len(by_name) != len(catalog):
                raise ValueError("MCP 服务返回重复工具名")
            for allowed in server.tools:
                remote = by_name.get(allowed.name)
                if remote is None:
                    raise ValueError(f"MCP 服务 {server.name} 未提供已配置工具 {allowed.name}")
                if remote.get("annotations", {}).get("readOnlyHint") is False:
                    raise ValueError("MCP 工具声明非只读，与本应用配置不符")
                schema = remote["inputSchema"]
                _check_schema(schema)
                local = f"mcp_{server.name}_{allowed.alias}"
                self._bindings[local] = (server, allowed.name, schema)
                # Remote descriptions/instructions never become system instructions.
                self._definitions.append(
                    ToolDefinition(
                        name=local,
                        description=allowed.description,
                        parameters={
                            "type": "object",
                            "properties": {
                                "supplier_id": {"type": "integer", "minimum": 1},
                                "arguments": _nested_schema(schema),
                            },
                            "required": ["supplier_id", "arguments"],
                            "additionalProperties": False,
                        },
                    )
                )
        self.identity = hashlib.sha256(
            encode(
                {
                    "config": config.model_dump(mode="json"),
                    "tools": [d.model_dump(mode="json") for d in self._definitions],
                }
            ).encode("utf-8")
        ).hexdigest()

    def definitions(self) -> list[ToolDefinition]:
        return [d.model_copy(deep=True) for d in self._definitions]

    def _request(
        self,
        server: McpServerConfig,
        tool: str | None = None,
        arguments: JsonObject | None = None,
        expected_schema: dict | None = None,
    ):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("MCP 同步入口不能在已运行的 asyncio 循环内使用")
        try:
            return asyncio.run(self._exchange(server, tool, arguments, expected_schema))
        except ToolInputError:
            raise
        except Exception:
            # Transport exceptions may include endpoint headers, URLs or server error payloads.
            raise RuntimeError(
                f"MCP 服务 {server.name} 请求失败或超时；请检查配置与服务状态"
            ) from None

    async def _exchange(self, server, tool, arguments, expected_schema):
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.client.streamable_http import streamable_http_client

        async with asyncio.timeout(server.timeout_seconds), AsyncExitStack() as stack:
            if server.transport == "stdio":
                env = {name: self._environment(value) for name, value in server.env.items()}
                env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
                params = StdioServerParameters(
                    command=server.command, args=server.args, cwd=server.cwd, env=env
                )
                # Configured subprocess stderr stays local; it never enters model context/logs.
                error_log = stack.enter_context(open(os.devnull, "w"))
                client = Client(stdio_client(params, errlog=error_log))
            else:
                import httpx2

                headers = {
                    name: self._environment(value) for name, value in server.headers_env.items()
                }
                http = await stack.enter_async_context(
                    httpx2.AsyncClient(
                        headers=headers, timeout=server.timeout_seconds, follow_redirects=False
                    )
                )
                client = Client(streamable_http_client(server.url, http_client=http))
            connected = await stack.enter_async_context(client)
            catalog = []
            cursor = None
            seen = set()
            for _ in range(16):
                page = await connected.list_tools(cursor=cursor)
                catalog.extend(
                    t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in page.tools
                )
                cursor = page.next_cursor
                if not cursor:
                    break
                if cursor in seen:
                    raise ValueError("MCP 工具目录分页未前进")
                seen.add(cursor)
            else:
                raise ValueError("MCP 工具目录分页过多")
            if len(encode(catalog)) > 256000:
                raise ValueError("MCP 工具目录过大")
            if tool is None:
                return catalog
            matches = [entry for entry in catalog if entry["name"] == tool]
            if len(matches) != 1 or matches[0]["inputSchema"] != expected_schema:
                raise RuntimeError("MCP 工具定义已变化，请重新装配运行器")
            if matches[0].get("annotations", {}).get("readOnlyHint") is False:
                raise RuntimeError("MCP 工具已不再声明只读")
            result = await connected.call_tool(tool, arguments or {})
            raw = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        # Check tool-level failures after the SDK's task groups have closed, so they remain
        # repairable ToolInputError feedback instead of transport ExceptionGroup failures.
        if raw.get("isError"):
            raise ToolInputError("MCP 工具返回失败；未保存为证据，请核对查询参数或服务状态")
        if len(encode(raw)) > server.max_response_chars:
            raise ToolInputError("MCP 返回结果超过配置大小上限，请缩小查询范围")
        if any(
            block.get("type") not in {"text", "resource_link", "resource"}
            for block in raw.get("content", [])
        ):
            raise ToolInputError("当前 MCP 接入仅支持文本、链接与文本资源结果")
        if any(
            block.get("type") == "resource" and "blob" in block.get("resource", {})
            for block in raw.get("content", [])
        ):
            raise ToolInputError("当前 MCP 接入不接收二进制资源")
        return raw

    @staticmethod
    def _environment(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise ValueError("MCP 配置引用的环境变量未设置")
        return value

    def call(self, name: str, arguments: JsonObject, supplier_id: int) -> ExternalObservation:
        from jsonschema import Draft202012Validator

        binding = self._bindings.get(name)
        if binding is None:
            raise ToolInputError("MCP 工具不在允许列表")
        server, remote, schema = binding
        if len(encode(arguments)) > 8000:
            raise ToolInputError("MCP 查询参数过长")
        try:
            valid = Draft202012Validator(schema).is_valid(arguments)
        except Exception:
            valid = False
        if not valid:
            raise ToolInputError("MCP 查询参数不符合工具 JSON Schema，请按参数定义修正")
        result = self._request(server, remote, arguments, schema)
        digest = hashlib.sha256(encode(result).encode("utf-8")).hexdigest()
        identity = encode([supplier_id, server.name, remote, arguments, digest])
        identifier = "external:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        structured = result.get("structuredContent")
        texts = [
            block["text"] for block in result.get("content", []) if block.get("type") == "text"
        ]
        excerpt = encode(structured) if structured is not None else "\n".join(texts)
        return ExternalObservation(
            id=identifier,
            supplier_id=supplier_id,
            server=server.name,
            tool=remote,
            retrieved_at=datetime.now(UTC).isoformat(),
            content_sha256=digest,
            source_urls=_urls(result),
            excerpt=excerpt[:1000],
            result=result,
        )
