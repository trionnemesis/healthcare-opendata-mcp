"""adapters 共用的 HTTP 基礎設施:有上限的回應讀取 + 預設 async getter。

為何需要上限:`resp.content` / `resp.text` 會把整份 body 讀進記憶體才交給
下游解析。上游站台(健保署、政府電子採購網)不在本專案控制範圍內,被指向
大型檔案、或上游異常回傳巨量 body 時,單次呼叫即可耗盡記憶體(CWE-400)。
`get_tender_detail` 是 MCP 工具,此路徑由用戶端可達,不只是離線 ETL。

兩個上限分別對應兩類負載:
  MAX_BULK_BYTES — ETL 批次檔(健保署 CSV、PCC 半月 XML)。與姊妹專案
      g0vmcp 的 `_MAX_XML_LEN` 同為 100 MiB,確保既有來源不被誤擋。
  MAX_PAGE_BYTES — 單一 HTML 明細頁。與 g0vmcp `cf_http.MAX_RESPONSE_BYTES`
      同為 8 MiB(同一個上游 web.pcc.gov.tw)。
"""
from __future__ import annotations

import httpx

from health_opendata_mcp.contracts import BlockedError

_BLOCKED_STATUS = {403, 429}

MAX_BULK_BYTES = 100 * 1024 * 1024
MAX_PAGE_BYTES = 8 * 1024 * 1024


class ResponseTooLargeError(BlockedError):
    """回應超出位元組上限。

    繼承 BlockedError:呼叫端(pipeline / QueryService)既有的 BlockedError
    分支會把它當成「上游暫時不可用」處理,不會變成未處理例外。
    """


async def read_capped(response: httpx.Response, url: str, max_bytes: int) -> bytes:
    """串流讀取 body,累計超過上限即中止 —— 不先讀完才檢查。

    Content-Length 已宣告超量時直接拒絕,連下載都不開始。
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


def _rebuild(response: httpx.Response, body: bytes) -> httpx.Response:
    """以已讀取的 body 重建一般(非 stream)Response。

    aiter_bytes 回傳「已解碼」的位元組,故移除 transfer 相關標頭,避免 httpx
    對同一份 body 二次解碼或回報錯誤長度。回傳值的 `.text` / `.content` /
    `.json()` 行為與非 stream 請求一致,呼叫端無需改動。
    """
    headers = [
        (k, v)
        for k, v in response.headers.multi_items()
        if k.lower() not in ("content-encoding", "content-length")
    ]
    return httpx.Response(
        status_code=response.status_code,
        headers=headers,
        content=body,
        request=response.request,
        extensions=response.extensions,
    )


async def bounded_send(
    client: httpx.AsyncClient, request: httpx.Request, *, max_bytes: int
) -> httpx.Response:
    """送出 request 並以 max_bytes 為上限讀回 body。

    httpx 的自動轉址在 stream=True 下會先把中間回應的 body 讀完,繞過上限;
    故自行跟隨 `next_request`,並把中間回應的 body 直接關掉不讀。
    轉址上限沿用 client.max_redirects。
    """
    redirects_used = 0
    while True:
        response = await client.send(request, stream=True, follow_redirects=False)
        if not client.follow_redirects or response.next_request is None:
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
        body = await read_capped(response, str(request.url), max_bytes)
    finally:
        await response.aclose()
    return _rebuild(response, body)


async def default_http_get(url: str, *, max_bytes: int = MAX_BULK_BYTES) -> bytes:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await bounded_send(
            client, client.build_request("GET", url), max_bytes=max_bytes
        )
        if resp.status_code in _BLOCKED_STATUS:
            raise BlockedError(f"blocked ({resp.status_code}): {url}")
        resp.raise_for_status()
        return resp.content
