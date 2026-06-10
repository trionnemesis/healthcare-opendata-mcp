"""adapters 共用的預設 async HTTP getter(DI 預設值;測試注入 fake)。"""
from __future__ import annotations

import httpx

from health_opendata_mcp.contracts import BlockedError

_BLOCKED_STATUS = {403, 429}


async def default_http_get(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(url)
        if resp.status_code in _BLOCKED_STATUS:
            raise BlockedError(f"blocked ({resp.status_code}): {url}")
        resp.raise_for_status()
        return resp.content
