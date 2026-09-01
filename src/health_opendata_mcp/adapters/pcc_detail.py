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
    """
    import httpx

    class _HttpxClient:
        _UA = "Mozilla/5.0 (Macintosh) hcmcp-enrich/0.1 (+gov open data)"

        def __init__(self) -> None:
            self._c = httpx.AsyncClient(
                timeout=60, follow_redirects=True, headers={"User-Agent": self._UA}
            )
            self._session_ready = False

        async def _ensure(self) -> None:
            if not self._session_ready:
                await self._c.get(_INDEX_URL)  # 建 JSESSIONID
                self._session_ready = True

        async def get(self, url: str) -> HttpResp:
            await self._ensure()
            r = await self._c.get(url)
            return HttpResp(r.status_code, r.text)

        async def post(self, url: str, data: dict[str, str]) -> HttpResp:
            await self._ensure()
            r = await self._c.post(url, data=data)
            return HttpResp(r.status_code, r.text)

    return _HttpxClient()
