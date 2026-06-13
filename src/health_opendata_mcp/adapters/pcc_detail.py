"""PccDetailEnricher — 由 job_number 取 web.pcc 明細頁的截標/開標/預算。

流程:POST readTenderBasic(tenderId=案號)→ 結果頁找 readBulletion 明細連結
→ GET 明細頁 → _pcc_detail.extract_detail。

反爬倫理:正式邏輯絕不直接相依 httpx — HTTP 走注入的 client(DI),測試注入
fake;呼叫端(enrich script)負責節流與限量。403/429 一律 raise BlockedError。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from health_opendata_mcp.adapters import _pcc_detail as detail
from health_opendata_mcp.contracts import BlockedError

_BASE = "https://web.pcc.gov.tw"
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
        url = path if path.startswith("http") else f"{_BASE}{path}"
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
    """正式用 httpx client:持 cookie jar、合理 UA、follow redirects。"""
    import httpx

    class _HttpxClient:
        _UA = "Mozilla/5.0 (Macintosh) hcmcp-enrich/0.1 (+gov open data)"

        async def get(self, url: str) -> HttpResp:
            async with httpx.AsyncClient(
                timeout=60, follow_redirects=True, headers={"User-Agent": self._UA}
            ) as c:
                r = await c.get(url)
                return HttpResp(r.status_code, r.text)

        async def post(self, url: str, data: dict[str, str]) -> HttpResp:
            async with httpx.AsyncClient(
                timeout=60, follow_redirects=True, headers={"User-Agent": self._UA}
            ) as c:
                r = await c.post(url, data=data)
                return HttpResp(r.status_code, r.text)

    return _HttpxClient()
