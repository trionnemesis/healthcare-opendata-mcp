"""adapters 共用的預設 async HTTP getter(DI 預設值;測試注入 fake)。"""
from __future__ import annotations

import os

import httpx

from health_opendata_mcp.contracts import BlockedError

_BLOCKED_STATUS = {403, 429}

# 單次下載上限。catalog 的來源是政府開放資料批次檔(NHI CSV、PCC 半月 XML),
# 實測為數百 KB ~ 數 MB;128MB 遠高於任何現有 dataset,正常同步不受影響。
#
# 無上限時 `resp.content` 會把整份回應讀進記憶體:上游異常、端點被換成大型
# 檔案,或 follow_redirects 把請求帶到大檔時,單次同步即可耗盡記憶體
# (CWE-400)。此路徑由 hcmcp-sync 對多個 dataset 連續呼叫,放大效果明顯。
#
# 以環境變數開放覆寫:未來官方 dataset 成長時,維運端不必改程式即可調整。
_DEFAULT_MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024


def max_download_bytes() -> int:
    """下載上限(bytes)。HCMCP_MAX_DOWNLOAD_BYTES 可覆寫;非法值回退預設。"""
    raw = os.environ.get("HCMCP_MAX_DOWNLOAD_BYTES", "")
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return _DEFAULT_MAX_DOWNLOAD_BYTES


async def default_http_get(url: str) -> bytes:
    limit = max_download_bytes()
    # HTTPX 自動跟隨轉址時會先 aread() 中間回應,繞過下方串流護欄。
    # 改用 next_request 保留 HTTPX 的 URL/cookie/header 規則,逐跳檢查。
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        request = client.build_request("GET", url)
        for _ in range(client.max_redirects + 1):
            resp = await client.send(request, stream=True)
            try:
                if resp.status_code in _BLOCKED_STATUS:
                    raise BlockedError(f"blocked ({resp.status_code}): {url}")
                next_request = resp.next_request
                if next_request is None:
                    resp.raise_for_status()

                # 每跳都檢查;已宣告超量就不必開始下載。
                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > limit:
                    raise BlockedError(
                        f"response exceeds {limit} bytes (declared Content-Length): {url}"
                    )

                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise BlockedError(
                            f"response exceeds {limit} bytes, download aborted: {url}"
                        )
                    if next_request is None:
                        chunks.append(chunk)
                if next_request is None:
                    return b"".join(chunks)
                request = next_request
            finally:
                await resp.aclose()
        raise httpx.TooManyRedirects(
            "Exceeded maximum allowed redirects.", request=request
        )
