"""MCP server 進入點 — stdio(預設)/ SSE 雙 transport。"""
from __future__ import annotations

import asyncio
import os
import sys

from health_opendata_mcp.cli import default_db_path
from health_opendata_mcp.mcp_server.server import build_server
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository


async def empty_db_error(repo: SqliteRepository, db_path: str) -> str | None:
    """DB 無任何資料集時回傳指引訊息;否則 None。"""
    if await repo.list_datasets():
        return None
    return (
        f"hcmcp: DB 無任何資料集({db_path})。"
        "請先執行 `hcmcp-sync` 同步資料(或以 HCMCP_DB 指向已同步的 DB)再啟動 server。"
    )


def main() -> None:
    db_path = os.environ.get("HCMCP_DB", default_db_path())
    from pathlib import Path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    repo = SqliteRepository(db_path)
    asyncio.run(repo.init())
    error = asyncio.run(empty_db_error(repo, db_path))
    if error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
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
