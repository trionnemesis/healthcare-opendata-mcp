"""default_http_get 的出站護欄回歸測試。

鎖住兩件事:
  1. 回應讀取有上限,且超量是在「讀取途中」中止(不是讀完才檢查)
  2. 轉址逐跳檢查 —— 非 https / 內嵌帳密 / IP literal / 無限轉址一律拒絕

同時涵蓋「正常回應行為不變」,避免護欄造成功能退化。
"""
from __future__ import annotations

import httpx
import pytest

from health_opendata_mcp.adapters._http import (
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    ResponseTooLargeError,
    UnsafeRedirectError,
    default_http_get,
)
from health_opendata_mcp.adapters.pcc_detail import default_client
from health_opendata_mcp.contracts import BlockedError

_URL = "https://info.nhi.gov.tw/api/iode0000s01/Dataset?rId=A21030000I-D21004-009"


def _patch_transport(monkeypatch, handler) -> None:
    """把 AsyncClient 的底層 transport 換成 MockTransport。

    不替換 AsyncClient 本身 —— 轉址跟隨、build_request、aiter_bytes 等
    受測邏輯都在 httpx 裡,必須讓真的 client 跑完整條路徑。
    """
    original_init = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)


class TestResponseSizeCap:
    async def test_normal_response_is_returned_unchanged(self, monkeypatch):
        body = b"<TENDER_LIST><TENDER/></TENDER_LIST>"
        _patch_transport(monkeypatch, lambda req: httpx.Response(200, content=body))
        assert await default_http_get(_URL) == body

    async def test_oversized_response_is_rejected(self, monkeypatch):
        _patch_transport(
            monkeypatch, lambda req: httpx.Response(200, content=b"x" * 2048)
        )
        with pytest.raises(ResponseTooLargeError):
            await default_http_get(_URL, max_bytes=1024)

    async def test_response_at_the_cap_is_still_accepted(self, monkeypatch):
        _patch_transport(
            monkeypatch, lambda req: httpx.Response(200, content=b"x" * 1024)
        )
        assert len(await default_http_get(_URL, max_bytes=1024)) == 1024

    async def test_declared_content_length_over_cap_is_rejected(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"x" * 10, headers={"Content-Length": "999999999"}
            )

        _patch_transport(monkeypatch, handler)
        with pytest.raises(ResponseTooLargeError):
            await default_http_get(_URL, max_bytes=1024)

    async def test_cap_aborts_before_reading_everything(self, monkeypatch):
        """超量 body 不會被整份讀進記憶體 —— 這是修正的重點,不只是回錯誤。"""
        read = 0

        async def oversized():
            nonlocal read
            for _ in range(100):
                read += 1
                yield b"x" * 512

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=oversized())

        _patch_transport(monkeypatch, handler)
        with pytest.raises(ResponseTooLargeError):
            await default_http_get(_URL, max_bytes=1024)
        assert read < 100  # 中止了,沒有把 100 個 chunk 都收完

    def test_default_cap_is_declared(self):
        assert MAX_RESPONSE_BYTES == 64 * 1024 * 1024


class TestRedirectPolicy:
    async def test_same_host_https_redirect_is_followed(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/moved"):
                return httpx.Response(
                    302, headers={"Location": "https://info.nhi.gov.tw/final"}
                )
            return httpx.Response(200, content=b"ok")

        _patch_transport(monkeypatch, handler)
        assert await default_http_get("https://info.nhi.gov.tw/moved") == b"ok"

    @pytest.mark.parametrize(
        "location",
        [
            "http://info.nhi.gov.tw/downgraded",  # https → http 降級
            "https://169.254.169.254/latest/meta-data/",  # 雲端 metadata
            "https://127.0.0.1/internal",
            "https://[::1]/internal",
            "https://localhost/internal",
            "https://user:pw@info.nhi.gov.tw/x",  # 內嵌帳密
            "file:///etc/passwd",
        ],
    )
    async def test_unsafe_redirect_target_is_rejected(self, monkeypatch, location):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": location})

        _patch_transport(monkeypatch, handler)
        with pytest.raises(UnsafeRedirectError):
            await default_http_get(_URL)

    async def test_redirect_loop_is_bounded(self, monkeypatch):
        hops = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal hops
            hops += 1
            return httpx.Response(
                302, headers={"Location": "https://info.nhi.gov.tw/loop"}
            )

        _patch_transport(monkeypatch, handler)
        with pytest.raises(UnsafeRedirectError):
            await default_http_get(_URL)
        assert hops == MAX_REDIRECTS + 1

    async def test_redirect_body_is_not_buffered_past_the_cap(self, monkeypatch):
        """轉址中間回應也受上限約束 —— httpx 自動跟隨時會繞過這點。"""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/big-redirect":
                return httpx.Response(
                    302,
                    content=b"x" * 4096,
                    headers={"Location": "https://info.nhi.gov.tw/final"},
                )
            return httpx.Response(200, content=b"ok")

        _patch_transport(monkeypatch, handler)
        assert (
            await default_http_get(
                "https://info.nhi.gov.tw/big-redirect", max_bytes=1024
            )
            == b"ok"
        )


class TestStatusHandlingUnchanged:
    @pytest.mark.parametrize("status", [403, 429])
    async def test_blocked_statuses_still_raise_blocked_error(
        self, monkeypatch, status
    ):
        _patch_transport(monkeypatch, lambda req: httpx.Response(status))
        with pytest.raises(BlockedError):
            await default_http_get(_URL)

    async def test_other_errors_still_raise_http_status_error(self, monkeypatch):
        _patch_transport(monkeypatch, lambda req: httpx.Response(500))
        with pytest.raises(httpx.HTTPStatusError):
            await default_http_get(_URL)

    def test_guard_errors_are_blocked_errors(self):
        """呼叫端(pipeline)已有的 BlockedError 處理路徑能接住新例外。"""
        assert issubclass(ResponseTooLargeError, BlockedError)
        assert issubclass(UnsafeRedirectError, BlockedError)


class TestPccDetailClientIsBounded:
    """default_client() 的回應讀取上限 —— get_tender_detail 是對外 MCP 工具。"""

    async def test_normal_page_text_is_unchanged(self, monkeypatch):
        html = "<html><body>開標時間</body></html>"
        _patch_transport(
            monkeypatch,
            lambda req: httpx.Response(
                200,
                content=html.encode(),
                headers={"Content-Type": "text/html; charset=utf-8"},
            ),
        )
        client = default_client()
        resp = await client.get("https://web.pcc.gov.tw/tps/x")
        assert resp.status_code == 200
        assert resp.text == html

    async def test_post_carries_form_data_and_is_bounded(self, monkeypatch):
        seen: dict[str, bytes] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                seen["body"] = request.content
            return httpx.Response(200, content=b"<html>ok</html>")

        _patch_transport(monkeypatch, handler)
        client = default_client()
        resp = await client.post(
            "https://web.pcc.gov.tw/tps/search", {"tenderId": "ABC-1"}
        )
        assert resp.status_code == 200
        assert b"tenderId=ABC-1" in seen["body"]

    async def test_oversized_page_is_aborted(self, monkeypatch):
        read = 0

        async def oversized():
            nonlocal read
            for _ in range(200):
                read += 1
                yield b"x" * 65536

        _patch_transport(
            monkeypatch, lambda req: httpx.Response(200, content=oversized())
        )
        client = default_client()
        client.MAX_RESPONSE_BYTES = 131072
        with pytest.raises(ResponseTooLargeError):
            await client.get("https://web.pcc.gov.tw/tps/x")
        assert read < 200  # 中止,不是整份讀完

    async def test_session_bootstrap_runs_once(self, monkeypatch):
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(200, content=b"<html>ok</html>")

        _patch_transport(monkeypatch, handler)
        client = default_client()
        await client.get("https://web.pcc.gov.tw/tps/a")
        await client.get("https://web.pcc.gov.tw/tps/b")
        # indexTenderBasic(建 JSESSIONID)只打一次,行為與修正前相同
        assert paths.count("/prkms/tender/common/basic/indexTenderBasic") == 1
        assert paths[-2:] == ["/tps/a", "/tps/b"]

    async def test_redirect_is_still_followed(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/tps/moved":
                return httpx.Response(
                    302, headers={"Location": "https://web.pcc.gov.tw/tps/final"}
                )
            return httpx.Response(200, content=b"<html>final</html>")

        _patch_transport(monkeypatch, handler)
        client = default_client()
        resp = await client.get("https://web.pcc.gov.tw/tps/moved")
        assert resp.status_code == 200
        assert "final" in resp.text

    async def test_redirect_loop_is_bounded(self, monkeypatch):
        _patch_transport(
            monkeypatch,
            lambda req: httpx.Response(
                302, headers={"Location": "https://web.pcc.gov.tw/tps/loop"}
            ),
        )
        client = default_client()
        with pytest.raises(httpx.TooManyRedirects):
            await client.get("https://web.pcc.gov.tw/tps/loop")
