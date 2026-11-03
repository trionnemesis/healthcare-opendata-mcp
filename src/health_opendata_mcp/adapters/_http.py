"""adapters 共用的預設 async HTTP getter(DI 預設值;測試注入 fake)。

本模組是 ingestion 的唯一出口 transport,因此護欄放在這裡而不是各 adapter:

1. **回應大小上限**(CWE-400)。舊寫法以 `resp.content` 一次把整份 body 讀進
   記憶體,沒有任何上限 —— 上游若回(或被導向)一個大檔,單次同步即可耗盡
   記憶體。sync job 在 GKE 上的 memory limit 是 512Mi(deploy/k8s),一個
   幾百 MB 的回應就足以讓它被 OOMKill。`scripts/verify_catalog_sources.py`
   早就用 `_collect_capped` 做了同一件事,正式路徑反而沒有;此處對齊它的
   64 MiB 上限(同一批來源已實測可接受的天花板)。

2. **轉址逐跳檢查**(CWE-918)。`catalog._check_url` 只驗「一開始那個 URL」;
   舊寫法 `follow_redirects=True` 讓上游的 302 可以把請求帶到任何地方,而
   httpx 自動跟隨時還會先把中間回應整份緩衝起來(繞過上面的上限)。改為手動
   跟隨,每一跳都重新檢查,並拒絕:
     - 非 https(含降級到 http)
     - URL 內嵌帳密
     - IP literal / localhost —— 這是真正會痛的那一類目標
       (169.254.169.254 雲端 metadata、叢集內部服務)
   刻意**不**在此加跨主機白名單:官方開放資料站台的跨網域轉址是否存在無法在
   無法連外的環境裡驗證,硬擋有打斷既有來源的風險。該項列為待人工確認。
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urljoin, urlsplit

import httpx

from health_opendata_mcp.contracts import BlockedError

_BLOCKED_STATUS = {403, 429}
_REDIRECT_STATUS = {301, 302, 303, 307, 308}

# 單一回應讀取上限。與 scripts/verify_catalog_sources.DEFAULT_MAX_BYTES 一致。
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# 轉址跳數上限(httpx 預設 20;開放資料下載不需要那麼多)。
MAX_REDIRECTS = 5


class ResponseTooLargeError(BlockedError):
    """回應超過大小上限,已在讀取途中中止。"""


class UnsafeRedirectError(BlockedError):
    """轉址目標不符合出站政策(非 https / 內嵌帳密 / IP literal)。"""


def _check_hop(url: str) -> None:
    """檢查一個即將發出的請求目標。初始 URL 與每個轉址目標都會經過這裡。"""
    try:
        parts = urlsplit(url)
        _ = parts.port  # 觸發 port 格式驗證(壞 port 會在這裡 ValueError)
    except ValueError as exc:
        raise UnsafeRedirectError(f"malformed outbound URL: {url!r}") from exc

    if parts.scheme.lower() != "https":
        raise UnsafeRedirectError(f"outbound URL must use https: {url!r}")
    if parts.username is not None or parts.password is not None:
        raise UnsafeRedirectError("outbound URL must not embed credentials")

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeRedirectError(f"outbound URL has no host: {url!r}")
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeRedirectError("outbound URL host is not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # 一般網域名稱
    else:
        # IP literal 一律拒絕:雲端 metadata 與叢集內部位址都走這條路。
        raise UnsafeRedirectError("outbound URL must not be an IP literal")


async def read_capped(response: httpx.Response, url: str, max_bytes: int) -> bytes:
    """串流讀取 body,累計超過上限即中止 —— 不是讀完才檢查。

    Content-Length 若已宣告超量就連下載都不開始。
    """
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise ResponseTooLargeError(
            f"response declares more than {max_bytes} bytes: {url}"
        )

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLargeError(
                f"response exceeds {max_bytes} bytes, aborted: {url}"
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def default_http_get(
    url: str, *, max_bytes: int = MAX_RESPONSE_BYTES
) -> bytes:
    """GET 一個官方來源 URL,回傳受大小上限約束的 body。

    成功條件與舊版相同(403/429 → BlockedError;其餘非 2xx → raise_for_status),
    正常大小的回應回傳完全相同的 bytes。
    """
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            _check_hop(current)
            request = client.build_request("GET", current)
            resp = await client.send(request, stream=True, follow_redirects=False)
            try:
                location = resp.headers.get("Location")
                if resp.status_code in _REDIRECT_STATUS and location:
                    current = urljoin(current, location)
                    continue
                if resp.status_code in _BLOCKED_STATUS:
                    raise BlockedError(f"blocked ({resp.status_code}): {current}")
                resp.raise_for_status()
                return await read_capped(resp, current, max_bytes)
            finally:
                await resp.aclose()
        raise UnsafeRedirectError(
            f"exceeded {MAX_REDIRECTS} redirects starting from {url}"
        )
