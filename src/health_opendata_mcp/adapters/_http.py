"""adapters 共用的預設 async HTTP getter(DI 預設值;測試注入 fake)。"""
from __future__ import annotations

import httpx

from health_opendata_mcp.contracts import BlockedError

_BLOCKED_STATUS = {403, 429}

# 單次下載位元組上限(CWE-400)。下游解析器各自有上限(_pcc_opendata 20M 字元、
# _pcc_detail 4M 字元),但那些檢查發生在「整包已讀進記憶體之後」;CSV 路徑
# (normalize_csv)甚至完全沒有上限。上游異常或中間人回傳超大 body 時,
# 這裡是唯一能在配置記憶體前喊停的地方。
# 128MB 遠高於任何解析器會接受的量(20M CJK 字元 ≈ 60MB),故不會擋掉正常資料。
MAX_RESPONSE_BYTES = 128 * 1024 * 1024


class ResponseTooLargeError(RuntimeError):
    """回應 body 超過 MAX_RESPONSE_BYTES,已在讀完前中止。"""


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
                if total > MAX_RESPONSE_BYTES:
                    raise ResponseTooLargeError(
                        f"回應超過 {MAX_RESPONSE_BYTES} bytes 上限,已中止: {url}"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
