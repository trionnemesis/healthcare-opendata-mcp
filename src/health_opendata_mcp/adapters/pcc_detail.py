"""PccDetailEnricher — 由 job_number 取 web.pcc 明細頁的截標/開標/預算。

流程:POST readTenderBasic(tenderId=案號)→ 結果頁找 readBulletion 明細連結
→ GET 明細頁 → _pcc_detail.extract_detail。

反爬倫理:正式邏輯絕不直接相依 httpx — HTTP 走注入的 client(DI),測試注入
fake;呼叫端(enrich script)負責節流與限量。403/429 一律 raise BlockedError。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import quote, urljoin, urlparse

from health_opendata_mcp.adapters import _pcc_detail as detail
from health_opendata_mcp.contracts import BlockedError

_BASE = "https://web.pcc.gov.tw"
_BASE_HOST = urlparse(_BASE).hostname or ""
_INDEX_URL = f"{_BASE}/prkms/tender/common/basic/indexTenderBasic"
_SEARCH_URL = f"{_BASE}/prkms/tender/common/basic/readTenderBasic"
_SEARCH_FORM = {
    "pageSize": "50",
    "firstSearch": "true",
    "searchType": "basic",
    "isBinding": "N",
    "isLogIn": "N",
    "dateType": "isDate",
}
_BLOCKED_STATUS = {403, 429}


def _resolve_detail_url(path: str, job_number: str) -> str:
    """把搜尋結果頁的明細連結解析成「必定落在 web.pcc.gov.tw」的絕對 URL。

    明細連結取自上游回應的 HTML,屬不可信輸入。舊寫法 `path if
    path.startswith("http") else _BASE + path` 會讓一個絕對 href 直接決定
    請求目標 —— 連線帶著共用 cookie jar 且 follow_redirects=True,等於把
    session 送往任意主機(CWE-918 SSRF)。改以 urljoin 正規化(相對路徑行為
    不變),再比對 host 白名單;非 PCC 網域一律拒絕而非靜默改寫。
    """
    url = urljoin(_BASE + "/", path)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host != _BASE_HOST:
        raise RuntimeError(
            f"unexpected detail href for {job_number}: 明細連結必須指向 {_BASE_HOST}"
        )
    return url


@dataclass(frozen=True)
class HttpResp:
    status_code: int
    text: str


@runtime_checkable
class HttpClient(Protocol):
    """DI 邊界:具 get/post 的最小 async HTTP client(測試注入 fake)。"""

    async def get(self, url: str) -> HttpResp: ...

    async def post(self, url: str, data: dict[str, str]) -> HttpResp: ...


class PccDetailEnricher:
    def __init__(self, client: HttpClient) -> None:
        self._client = client

    async def fetch_detail(self, job_number: str) -> detail.DetailFields | None:
        """取單案明細欄位;查無明細連結回 None;被封鎖 raise BlockedError。"""
        search = await self._post(_SEARCH_URL, {"tenderId": job_number, **_SEARCH_FORM})
        path = detail.find_detail_path(search.text, job_number)
        if not path:
            return None
        url = _resolve_detail_url(path, job_number)
        page = await self._get(url)
        return detail.extract_detail(page.text)

    async def _get(self, url: str) -> HttpResp:
        resp = await self._client.get(url)
        self._guard(resp, url)
        return resp

    async def _post(self, url: str, data: dict[str, str]) -> HttpResp:
        resp = await self._client.post(url, data)
        self._guard(resp, url)
        return resp

    @staticmethod
    def _guard(resp: HttpResp, url: str) -> None:
        if resp.status_code in _BLOCKED_STATUS:
            raise BlockedError(f"blocked ({resp.status_code}): {url}")
        if resp.status_code != 200:
            raise RuntimeError(f"unexpected status {resp.status_code}: {url}")


def default_client() -> HttpClient:
    """正式用 httpx client:單一持久 AsyncClient(cookie jar 跨案共用)。

    當前 PCC 搜尋需 session — 首次請求前先 GET indexTenderBasic 取得 JSESSIONID,
    否則 POST readTenderBasic 只回填表單頁、不執行搜尋(2026 實測)。

    回應一律經 _http.read_capped 串流讀取並受 MAX_RESPONSE_BYTES 約束:
    `r.text` 會把整份 body 讀進記憶體再解碼,而 _pcc_detail.MAX_HTML_CHARS
    的檢查發生在「解析階段」,救不了下載與解碼(CWE-400)。搜尋結果頁更是
    連那層 char 上限都沒有。get_tender_detail 是對外 MCP 工具,呼叫者可任意
    觸發這條路徑。
    """
    import httpx

    from health_opendata_mcp.adapters._http import read_capped

    class _HttpxClient:
        _UA = "Mozilla/5.0 (Macintosh) hcmcp-enrich/0.1 (+gov open data)"
        # 真實明細頁約 140KB;上限遠高於任何正常頁面,只擋失控大小。
        MAX_RESPONSE_BYTES = 16 * 1024 * 1024
        MAX_REDIRECTS = 5

        def __init__(self) -> None:
            self._c = httpx.AsyncClient(
                timeout=60, follow_redirects=True, headers={"User-Agent": self._UA}
            )
            self._session_ready = False

        async def _send(self, request: "httpx.Request") -> HttpResp:
            """送出請求並以位元組上限讀回。

            httpx 自動跟隨轉址時會把中間回應整份緩衝(繞過上限),故改為手動
            跟隨它自己準備好的 next_request,中間回應一律不讀就關閉 ——
            轉址目的地、方法與 cookie 行為因此與原本完全一致。
            """
            resp = await self._c.send(request, stream=True, follow_redirects=False)
            hops = 0
            while resp.next_request is not None:
                nxt = resp.next_request
                await resp.aclose()
                hops += 1
                if hops > self.MAX_REDIRECTS:
                    raise httpx.TooManyRedirects(
                        "Exceeded maximum allowed redirects.", request=request
                    )
                resp = await self._c.send(nxt, stream=True, follow_redirects=False)
            try:
                body = await read_capped(
                    resp, str(resp.request.url), self.MAX_RESPONSE_BYTES
                )
            finally:
                await resp.aclose()
            # 以 httpx.Response 重建,讓 charset 偵測/解碼行為與 r.text 相同。
            # aiter_bytes 給的是已解碼位元組,故移除 transfer 相關標頭。
            headers = [
                (k, v)
                for k, v in resp.headers.multi_items()
                if k.lower() not in ("content-encoding", "content-length")
            ]
            rebuilt = httpx.Response(
                status_code=resp.status_code,
                headers=headers,
                content=body,
                request=resp.request,
            )
            return HttpResp(rebuilt.status_code, rebuilt.text)

        async def _ensure(self) -> None:
            if not self._session_ready:
                # 建 JSESSIONID;回應內容不使用,但同樣受上限約束
                await self._send(self._c.build_request("GET", _INDEX_URL))
                self._session_ready = True

        async def get(self, url: str) -> HttpResp:
            await self._ensure()
            return await self._send(self._c.build_request("GET", url))

        async def post(self, url: str, data: dict[str, str]) -> HttpResp:
            await self._ensure()
            return await self._send(
                self._c.build_request("POST", url, data=data)
            )

    return _HttpxClient()
