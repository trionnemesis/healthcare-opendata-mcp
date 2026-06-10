"""MCP server 進入點 — stdio(預設)/ SSE 雙 transport。"""
from __future__ import annotations

import asyncio
import os

from health_opendata_mcp.cli import default_db_path
from health_opendata_mcp.mcp_server.server import build_server
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository


def main() -> None:
    db_path = os.environ.get("HCMCP_DB", default_db_path())
    repo = SqliteRepository(db_path)
    asyncio.run(repo.init())
    mcp = build_server(repo)

    transport = os.environ.get("HCMCP_TRANSPORT", "stdio")
    if transport == "sse":
        mcp.run(
            transport="sse",
            host=os.environ.get("HCMCP_HOST", "0.0.0.0"),
            port=int(os.environ.get("HCMCP_PORT", "8000")),
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
