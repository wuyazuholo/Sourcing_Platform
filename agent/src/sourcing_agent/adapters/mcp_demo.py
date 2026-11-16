"""Offline demonstration through an actual MCP stdio subprocess."""

import json
import sys
from pathlib import Path

from sourcing_agent.harness.contracts import ModelReply, ModelRequest, ToolCall
from sourcing_agent.mcp_config import AllowedTool, McpConfig, McpServerConfig


def demo_config() -> McpConfig:
    return McpConfig(
        servers=[
            McpServerConfig(
                name="demo",
                transport="stdio",
                command=sys.executable,
                args=[str(Path(__file__).resolve().parents[1] / "mcp_demo_server.py")],
                tools=[
                    AllowedTool(
                        name="lookup_company",
                        alias="lookup",
                        description="查询虚构企业官网资料，仅作本地联调",
                        read_only=True,
                    )
                ],
            )
        ]
    )


class McpDemoModel:
    def __init__(self) -> None:
        self.counter = 0

    def generate(self, request: ModelRequest) -> ModelReply:
        self.counter += 1
        state = json.loads(request.messages[0].content)["effective_task_state"]
        if not state["queried"]:
            name, arguments = "find_supplier_candidates", {}
        elif not state.get("external_observations"):
            name = "mcp_demo_lookup"
            arguments = {
                "supplier_id": state["candidate_ids"][0],
                "arguments": {"company_name": "SYNTHETIC supplier"},
            }
        else:
            name, arguments = "submit_findings", {}
        return ModelReply(
            finish="tool_calls",
            calls=[ToolCall(id=f"mcp-demo-{self.counter}", name=name, arguments=arguments)],
        )
