"""adapters 共用的預設 async HTTP getter(DI 預設值;測試注入 fake)。"""
from __future__ import annotations

import httpx

from health_opendata_mcp.contracts import BlockedError

_BLOCKED_STATUS = {403, 429}

# 單一回應 body 上限(CWE-400)。健保署 CSV 與 PCC 半月 XML 都遠小於此值;
# 無上限時 resp.content 會把整份 body 讀進記憶體,上游異常或回應被換成超大
# body 即可讓 ETL(deploy/k8s/cronjob-sync.yaml)耗盡記憶體。
# 以 stream + aiter_bytes 累計,超限立即中止連線,不先把整份內容收下來。
_MAX_RESPONSE_BYTES = 256 * 1024 * 1024


async def default_http_get(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            if resp.status_code in _BLOCKED_STATUS:
                raise BlockedError(f"blocked ({resp.status_code}): {url}")
            resp.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    raise BlockedError(
                        f"response body exceeds {_MAX_RESPONSE_BYTES} bytes: {url}"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
