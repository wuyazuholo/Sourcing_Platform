"""A real MCP server with fictional company data. No network/business data access."""

import argparse
import asyncio
import os

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("sourcing-synthetic-company-directory")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
async def lookup_company(
    company_name: str, detail_chars: int = 0, delay_seconds: float = 0
) -> dict:
    """Look up a FICTIONAL company page; all returned information is synthetic."""
    if not 0 <= detail_chars <= 100000 or not 0 <= delay_seconds <= 10:
        raise ValueError("Invalid demo parameters")
    if delay_seconds:
        await asyncio.sleep(delay_seconds)
    return {
        "synthetic": True,
        "company_name": company_name,
        "source_url": "https://example.invalid/synthetic-company",
        "products": ["SYNTHETIC refrigeration parts; not a real supplier claim"],
        "notice": "虚构企业资料，仅用于 MCP 本地联调，不代表真实企业、官网或认证",
        "details": "示例产品与企业介绍。" * (detail_chars // 10),
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=False))
def disabled_write_demo(value: str) -> str:
    """Not a lookup; must not be exposed by the sourcing client's allowlist."""
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.transport == "stdio":
        server.run()
    else:
        import uvicorn
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse

        app = server.streamable_http_app()

        async def demo_auth(request, call_next):
            expected = os.getenv("SOURCING_MCP_DEMO_AUTH")
            if expected and request.headers.get("authorization") != expected:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=demo_auth)
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="error")


if __name__ == "__main__":
    main()
