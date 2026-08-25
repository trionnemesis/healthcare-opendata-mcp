"""default_http_get 的下載大小護欄(CWE-400)— 離線,以 httpx MockTransport 驅動。

回歸重點:超大 body 必須在「讀完整包之前」中止,而非讀進記憶體後才由下游
解析器(_pcc_opendata / _pcc_detail)檢查長度;CSV 路徑更是完全沒有下游上限。
"""
from __future__ import annotations

import httpx
import pytest

from health_opendata_mcp.adapters import _http
from health_opendata_mcp.contracts import BlockedError


def _install_transport(monkeypatch, handler) -> None:
    """讓 default_http_get 內部建立的 AsyncClient 走 MockTransport。"""
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(_http.httpx, "AsyncClient", _factory)


class TestResponseSizeGuard:
    async def test_normal_body_unchanged(self, monkeypatch):
        _install_transport(monkeypatch, lambda _r: httpx.Response(200, content=b"a,b\n1,2\n"))
        assert await _http.default_http_get("https://example.test/x") == b"a,b\n1,2\n"

    async def test_oversized_body_aborted_before_full_read(self, monkeypatch):
        monkeypatch.setattr(_http, "MAX_RESPONSE_BYTES", 1024)
        sent = 0

        async def _stream():
            nonlocal sent
            for _ in range(1000):
                sent += 256
                yield b"A" * 256

        _install_transport(
            monkeypatch, lambda _r: httpx.Response(200, content=_stream())
        )
        with pytest.raises(_http.ResponseTooLargeError):
            await _http.default_http_get("https://example.test/big")
        # 關鍵:停在剛越線處,沒有把 256KB 全吃進來
        assert sent <= 1024 + 256

    async def test_body_exactly_at_limit_is_accepted(self, monkeypatch):
        monkeypatch.setattr(_http, "MAX_RESPONSE_BYTES", 1024)
        _install_transport(monkeypatch, lambda _r: httpx.Response(200, content=b"A" * 1024))
        assert len(await _http.default_http_get("https://example.test/edge")) == 1024

    @pytest.mark.parametrize("status", [403, 429])
    async def test_blocked_status_still_raises_blocked_error(self, monkeypatch, status):
        _install_transport(monkeypatch, lambda _r: httpx.Response(status))
        with pytest.raises(BlockedError):
            await _http.default_http_get("https://example.test/x")

    async def test_server_error_still_raises_status_error(self, monkeypatch):
        _install_transport(monkeypatch, lambda _r: httpx.Response(500))
        with pytest.raises(httpx.HTTPStatusError):
            await _http.default_http_get("https://example.test/x")
