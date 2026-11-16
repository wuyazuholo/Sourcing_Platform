"""Expose fixed, read-only trade search through the official MCP server SDK."""

from pathlib import Path

from pydantic import JsonValue

from sourcing_agent.database_config import DatabaseConfig


def create_database_server(config: DatabaseConfig, state_dir: Path):
    try:
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError:
        raise RuntimeError('MCP 依赖未安装，请运行 pip install -e ".[database,mcp]"') from None
    from sourcing_agent.sourcing.database_search import search_database
    from sourcing_agent.sourcing.models import Requirements

    server = MCPServer("sourcing-readonly-sqlserver")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=True)
    def search_trade_suppliers(
        hs_prefix: str,
        start_date: str,
        end_date: str,
        market_country: str | None = None,
        supplier_country: str | None = None,
        limit: int = 5,
        max_records: int = 5000,
        scan_limit: int = 200000,
    ) -> dict[str, JsonValue]:
        """Read actual Mexico export trades (panjivaMXExport) with HS/date filters. scan_limit=0 explicitly requests
        full history; default is the latest 200000 source rows. Never executes caller SQL.
        Returns candidates and cited records from a frozen extraction; no external LLM calls.
        """
        requirements = Requirements(
            hs_prefix=hs_prefix,
            start_date=start_date,
            end_date=end_date,
            market_country=market_country,
            supplier_country=supplier_country,
            limit=limit,
        )
        report = search_database(
            config, requirements, state_dir, max_records=max_records, scan_limit=scan_limit
        )
        # Avoid duplicating the rendered report in the MCP structured payload.
        return {key: value for key, value in report.items() if key != "text"}

    return server
