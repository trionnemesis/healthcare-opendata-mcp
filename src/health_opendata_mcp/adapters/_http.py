"""adapters 共用的預設 async HTTP getter(DI 預設值;測試注入 fake)。

回應一律以串流 + 位元組上限讀取(CWE-400)。原寫法 `resp.content` 會把
整份 body 讀進記憶體後才交給呼叫端,且 follow_redirects=True 讓上游得以
把請求導向任意大小的檔案 —— ETL 容器(deploy/k8s/cronjob-sync.yaml)未設
memory limit,單一超大回應即可吃掉節點記憶體。兩個姊妹專案已採同樣作法
(g0VMCP 的 cf_http.MAX_RESPONSE_BYTES、opendataCampus 的 safe_http._read_capped)。
"""
from __future__ import annotations

import httpx

from health_opendata_mcp.contracts import BlockedError

_BLOCKED_STATUS = {403, 429}

# 單次回應讀取上限。最大的正式來源是 PCC 半月 XML 與健保署 CSV
# (實測數 MB 等級);64MiB 遠高於任何已知來源,只擋失控/惡意的回應。
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class ResponseTooLargeError(BlockedError):
    """回應超出位元組預算 —— 視為來源異常,呼叫端應退避。

    繼承 BlockedError:既有 ETL 呼叫端(pipeline)已對 BlockedError 做
    退避處理,超量回應屬同一類「本次取不到、稍後再試」的情況。
    """


async def read_capped(response: httpx.Response, url: str, max_bytes: int) -> bytes:
    """串流讀取 body,累計超過上限即中止 —— 不先讀完才檢查。

    Content-Length 若已宣告超量則連下載都不開始。
    """
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise ResponseTooLargeError(
            f"response exceeds {max_bytes} bytes (declared): {url}"
        )
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLargeError(
                f"response exceeds {max_bytes} bytes: {url}"
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def default_http_get(url: str, *, max_bytes: int | None = None) -> bytes:
    """GET 並回傳受上限約束的 body。403/429 一律 raise BlockedError。

    httpx 的自動轉址會先把中間回應整份緩衝進記憶體,繞過本上限;
    故自行跟隨 httpx 準備好的 next_request,並把中間回應的 body 直接關閉不讀。
    """
    budget = MAX_RESPONSE_BYTES if max_bytes is None else max_bytes
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        request = client.build_request("GET", url)
        redirects_used = 0
        while True:
            response = await client.send(request, stream=True)
            if response.next_request is None:
                break
            next_request = response.next_request
            await response.aclose()
            redirects_used += 1
            if redirects_used > client.max_redirects:
                raise httpx.TooManyRedirects(
                    "Exceeded maximum allowed redirects.", request=request
                )
            request = next_request
        try:
            if response.status_code in _BLOCKED_STATUS:
                raise BlockedError(f"blocked ({response.status_code}): {url}")
            response.raise_for_status()
            return await read_capped(response, url, budget)
        finally:
            await response.aclose()
