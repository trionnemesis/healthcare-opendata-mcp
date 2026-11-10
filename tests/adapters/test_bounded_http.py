"""HTTP 回應位元組上限(CWE-400)。

default_http_get / default_client 原先以 resp.content / r.text 讀取,
會把整份 body 讀進記憶體後才交給呼叫端。server pod 的 memory limit 是
512Mi(deploy/k8s/deployment.yaml),ETL CronJob 更未設 limit —— 一個
失控的上游回應即可耗盡記憶體。兩個姊妹專案已有同等防護,此處對齊。
"""
import httpx
import pytest

from health_opendata_mcp.adapters._http import (
    MAX_RESPONSE_BYTES,
    ResponseTooLargeError,
    default_http_get,
    read_capped,
)
from health_opendata_mcp.contracts import BlockedError

URL = "https://data.nhi.gov.tw/resource/example.csv"

# default_http_get 內部呼叫 httpx.AsyncClient();測試以 monkeypatch 換成
# 帶 MockTransport 的實例,故必須先留住真正的 class 才不會遞迴。
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _client(handler: object, **kwargs) -> httpx.AsyncClient:
    return _REAL_ASYNC_CLIENT(
        transport=httpx.MockTransport(handler), follow_redirects=False, **kwargs
    )


def _patch(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _client(handler, timeout=kw.get("timeout"))
    )


class TestReadCapped:
    async def test_body_within_budget_is_returned_intact(self):
        payload = b"a,b\n1,2\n"

        def handler(request):
            return httpx.Response(200, content=payload)

        async with _client(handler) as c:
            resp = await c.send(c.build_request("GET", URL), stream=True)
            assert await read_capped(resp, URL, 1024) == payload

    async def test_declared_content_length_over_budget_rejected(self):
        """宣告超量時連下載都不開始。"""

        def handler(request):
            return httpx.Response(
                200, content=b"x" * 100, headers={"Content-Length": "999999"}
            )

        async with _client(handler) as c:
            resp = await c.send(c.build_request("GET", URL), stream=True)
            with pytest.raises(ResponseTooLargeError, match="declared"):
                await read_capped(resp, URL, 1024)

    async def test_undeclared_body_over_budget_aborted_mid_stream(self):
        """沒有 Content-Length 的超量回應,須在串流累計時中止。"""

        async def stream():
            for _ in range(10):
                yield b"x" * 512

        def handler(request):
            # 無 Content-Length 的分塊回應:上限必須在累計過程中生效
            return httpx.Response(200, content=stream())

        async with _client(handler) as c:
            resp = await c.send(c.build_request("GET", URL), stream=True)
            with pytest.raises(ResponseTooLargeError):
                await read_capped(resp, URL, 1024)

    async def test_exactly_at_budget_accepted(self):
        def handler(request):
            return httpx.Response(200, content=b"x" * 1024)

        async with _client(handler) as c:
            resp = await c.send(c.build_request("GET", URL), stream=True)
            assert len(await read_capped(resp, URL, 1024)) == 1024

    def test_too_large_is_a_blocked_error(self):
        """ETL pipeline 既有的 BlockedError 退避路徑須涵蓋超量回應。"""
        assert issubclass(ResponseTooLargeError, BlockedError)

    def test_budget_is_well_above_real_sources(self):
        # PCC 半月 XML / 健保署 CSV 實測為數 MB 等級
        assert MAX_RESPONSE_BYTES >= 16 * 1024 * 1024


class TestDefaultHttpGet:
    async def test_normal_response_unchanged(self, monkeypatch):
        payload = b"col\nvalue\n"

        def handler(request):
            assert str(request.url) == URL
            return httpx.Response(200, content=payload)

        _patch(monkeypatch, handler)
        assert await default_http_get(URL) == payload

    async def test_oversized_response_rejected(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, content=b"x" * 4096)

        _patch(monkeypatch, handler)
        with pytest.raises(ResponseTooLargeError):
            await default_http_get(URL, max_bytes=1024)

    async def test_blocked_status_still_raises_blocked_error(self, monkeypatch):
        def handler(request):
            return httpx.Response(429, content=b"slow down")

        _patch(monkeypatch, handler)
        with pytest.raises(BlockedError):
            await default_http_get(URL)

    async def test_redirect_is_followed_without_buffering_the_hop(self, monkeypatch):
        """轉址仍須跟隨,但中間回應的 body 不得被讀進記憶體。"""
        final = "https://data.nhi.gov.tw/final.csv"
        seen: list[str] = []

        def handler(request):
            seen.append(str(request.url))
            if str(request.url) == URL:
                # 巨大的中間 body:若被讀取就會超出 max_bytes
                return httpx.Response(
                    302, content=b"y" * 8192, headers={"Location": final}
                )
            return httpx.Response(200, content=b"ok")

        _patch(monkeypatch, handler)
        assert await default_http_get(URL, max_bytes=1024) == b"ok"
        assert seen == [URL, final]

    async def test_redirect_loop_bounded(self, monkeypatch):
        def handler(request):
            return httpx.Response(302, headers={"Location": str(request.url)})

        _patch(monkeypatch, handler)
        with pytest.raises(httpx.TooManyRedirects):
            await default_http_get(URL)
