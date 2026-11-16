"""Explicit MCP allowlist; configuration files contain environment variable names, not secrets."""

import json
import sys
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, model_validator

from sourcing_agent.harness.contracts import Record

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,19}$")]
EnvName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class AllowedTool(Record):
    name: str = Field(min_length=1, max_length=128)
    alias: Name
    description: str = Field(min_length=1, max_length=300)
    read_only: Literal[True]


class McpServerConfig(Record):
    name: Name
    transport: Literal["stdio", "streamable-http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list, max_length=40)
    cwd: Path | None = None
    env: dict[EnvName, EnvName] = Field(default_factory=dict)
    url: str | None = None
    headers_env: dict[str, EnvName] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30, ge=0.1, le=120)
    max_response_chars: int = Field(default=64000, ge=1024, le=256000)
    tools: list[AllowedTool] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def check_transport(self) -> Self:
        if self.transport == "stdio":
            if not self.command or self.url or self.headers_env:
                raise ValueError("stdio requires command and cannot set url/headers_env")
        else:
            if not self.url or self.command or self.args or self.cwd or self.env:
                raise ValueError("streamable-http requires url and cannot set subprocess options")
            url = urlsplit(self.url)
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError("MCP URL must use HTTP(S)")
            if url.username or url.password or url.query or url.fragment:
                raise ValueError(
                    "Use headers_env for credentials; URL credentials/query are forbidden"
                )
            if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Remote MCP endpoints require HTTPS")
        if len({t.alias for t in self.tools}) != len(self.tools):
            raise ValueError("Duplicate MCP tool alias")
        if len({t.name for t in self.tools}) != len(self.tools):
            raise ValueError("Duplicate MCP remote tool")
        return self


class McpConfig(Record):
    version: Literal[1] = 1
    servers: list[McpServerConfig] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_servers(self) -> Self:
        if len({s.name for s in self.servers}) != len(self.servers):
            raise ValueError("Duplicate MCP server name")
        return self


def load_mcp_config(path: Path) -> McpConfig:
    try:
        config = McpConfig.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError, ValidationError):
        # Pydantic errors include offending input, which could contain a mistakenly pasted key.
        raise ValueError(
            "MCP 配置无效；请参照 docs/mcp.md 检查字段，凭据仅使用环境变量引用"
        ) from None
    for server in config.servers:
        if server.command == "{python}":
            server.command = sys.executable
        if server.transport == "stdio":
            server.cwd = (path.resolve().parent / (server.cwd or ".")).resolve()
    return config
