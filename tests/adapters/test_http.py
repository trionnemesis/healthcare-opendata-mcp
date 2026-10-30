"""default_http_get 的下載上限護欄(CWE-400 回歸測試)。

全離線:以 httpx.MockTransport 合成回應,絕不連外網。
上限以 monkeypatch 調小,避免在測試中配置真的 128MB。
"""
from __future__ import annotations

import httpx
import pytest

from health_opendata_mcp.adapters import _http
from health_opendata_mcp.adapters._http import default_http_get, max_download_bytes
from health_opendata_mcp.contracts import BlockedError

_URL = "https://data.gov.tw/dataset.csv"


@pytest.fixture
def mock_transport(monkeypatch):
    """把 default_http_get 內部建立的 AsyncClient 換成帶 MockTransport 的版本。"""

    def install(handler) -> None:
        real_init = httpx.AsyncClient.__init__

        def patched_init(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            real_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    return install


# ── 下載上限 ────────────────────────────────────────────────────────────────


async def test_oversized_download_is_aborted(monkeypatch, mock_transport) -> None:
    monkeypatch.setattr(_http, "_DEFAULT_MAX_DOWNLOAD_BYTES", 1024)
    monkeypatch.delenv("HCMCP_MAX_DOWNLOAD_BYTES", raising=False)
    mock_transport(lambda _r: httpx.Response(200, content=b"x" * 4096))

    with pytest.raises(BlockedError, match="exceeds"):
        await default_http_get(_URL)


async def test_declared_content_length_over_limit_is_rejected(
    monkeypatch, mock_transport
) -> None:
    """宣告超量時直接拒絕,不應開始串流 body。"""
    monkeypatch.setattr(_http, "_DEFAULT_MAX_DOWNLOAD_BYTES", 1024)
    monkeypatch.delenv("HCMCP_MAX_DOWNLOAD_BYTES", raising=False)
    streamed = {"hit": False}

    async def body():
        streamed["hit"] = True
        yield b"x" * 4096

    mock_transport(
        lambda _r: httpx.Response(
            200, content=body(), headers={"Content-Length": "4096"}
        )
    )

    with pytest.raises(BlockedError, match="declared"):
        await default_http_get(_URL)
    assert streamed["hit"] is False


async def test_payload_at_limit_is_returned_intact(monkeypatch, mock_transport) -> None:
    """邊界:正好等於上限不得誤殺,且內容須完整。"""
    monkeypatch.setattr(_http, "_DEFAULT_MAX_DOWNLOAD_BYTES", 1024)
    monkeypatch.delenv("HCMCP_MAX_DOWNLOAD_BYTES", raising=False)
    mock_transport(lambda _r: httpx.Response(200, content=b"a" * 1024))

    assert await default_http_get(_URL) == b"a" * 1024


async def test_normal_payload_is_unchanged(mock_transport) -> None:
    """預設上限遠高於真實 dataset — 正常下載行為不變(回歸保護)。"""
    payload = "機構代碼,機構名稱\n0101010010,臺大醫院\n".encode()
    mock_transport(lambda _r: httpx.Response(200, content=payload))

    assert await default_http_get(_URL) == payload


# ── 既有錯誤語意不變 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [403, 429])
async def test_blocked_statuses_still_raise_blocked_error(
    status: int, mock_transport
) -> None:
    mock_transport(lambda _r: httpx.Response(status, content=b""))

    with pytest.raises(BlockedError, match="blocked"):
        await default_http_get(_URL)


async def test_other_error_status_still_raises_http_status_error(
    mock_transport,
) -> None:
    mock_transport(lambda _r: httpx.Response(500, content=b""))

    with pytest.raises(httpx.HTTPStatusError):
        await default_http_get(_URL)


# ── 上限可由環境變數覆寫 ────────────────────────────────────────────────────


def test_env_override_is_honoured(monkeypatch) -> None:
    monkeypatch.setenv("HCMCP_MAX_DOWNLOAD_BYTES", "4096")
    assert max_download_bytes() == 4096


@pytest.mark.parametrize("bad", ["", "0", "-1", "abc", "1.5"])
def test_invalid_env_override_falls_back_to_default(monkeypatch, bad: str) -> None:
    monkeypatch.setenv("HCMCP_MAX_DOWNLOAD_BYTES", bad)
    assert max_download_bytes() == _http._DEFAULT_MAX_DOWNLOAD_BYTES


class TrackedStream(httpx.AsyncByteStream):
    """Track transport reads and closes, including early aborts."""

    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.bytes_read = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.bytes_read += len(chunk)
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("declared", [True, False])
async def test_oversized_redirect_is_blocked_before_next_request(
    monkeypatch, mock_transport, declared
):
    monkeypatch.setenv("HCMCP_MAX_DOWNLOAD_BYTES", "1024")
    stream = TrackedStream([b"x" * 512] * 8)
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            headers = {"Location": "/final.csv"}
            if declared:
                headers["Content-Length"] = "4096"
            return httpx.Response(302, headers=headers, stream=stream)
        return httpx.Response(200, content=b"ok")

    mock_transport(handler)
    with pytest.raises(BlockedError, match="exceeds"):
        await default_http_get(_URL)
    assert len(requests) == 1
    assert stream.bytes_read == (0 if declared else 1536)
    assert stream.closed


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_relative_redirect_preserves_httpx_request_and_closes_streams(
    monkeypatch, mock_transport, status
):
    monkeypatch.setenv("HCMCP_MAX_DOWNLOAD_BYTES", "1024")
    redirect = TrackedStream([b"r" * 1024])
    final = TrackedStream([b"ok"])
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                status,
                headers={"Location": "next.csv", "Set-Cookie": "session=test; Path=/"},
                stream=redirect,
            )
        assert redirect.closed
        assert str(request.url) == "https://data.gov.tw/next.csv"
        assert request.method == "GET"
        assert request.headers["cookie"] == "session=test"
        return httpx.Response(200, stream=final)

    mock_transport(handler)
    assert await default_http_get(_URL) == b"ok"
    assert len(requests) == 2
    assert redirect.closed and final.closed


async def test_redirect_loop_is_bounded_and_all_streams_closed(mock_transport):
    streams = []

    def handler(request):
        stream = TrackedStream([b"redirect"])
        streams.append(stream)
        return httpx.Response(302, headers={"Location": "/dataset.csv"}, stream=stream)

    mock_transport(handler)
    with pytest.raises(httpx.TooManyRedirects):
        await default_http_get(_URL)
    assert len(streams) == 21  # HTTPX default: at most 20 redirects.
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.parametrize("size", [1023, 1024, 1025])
async def test_final_stream_boundary_after_redirect(
    monkeypatch, mock_transport, declared, size
):
    monkeypatch.setenv("HCMCP_MAX_DOWNLOAD_BYTES", "1024")
    redirect = TrackedStream([b"redirect"])
    final = TrackedStream([b"a" * 1023, b"b" * (size - 1023)])

    def handler(request):
        if request.url.path == "/dataset.csv":
            return httpx.Response(302, headers={"Location": "/final.csv"}, stream=redirect)
        headers = {"Content-Length": str(size)} if declared else {}
        return httpx.Response(200, headers=headers, stream=final)

    mock_transport(handler)
    if size > 1024:
        with pytest.raises(BlockedError, match="exceeds"):
            await default_http_get(_URL)
        assert final.bytes_read == (0 if declared else size)
    else:
        assert await default_http_get(_URL) == b"a" * 1023 + b"b" * (size - 1023)
    assert redirect.closed and final.closed


@pytest.mark.parametrize("status", [302, 403, 429, 404, 500])
async def test_status_contract_and_stream_close_after_redirect(mock_transport, status):
    redirect = TrackedStream([b"redirect"])
    final = TrackedStream([b"error body"])

    def handler(request):
        if request.url.path == "/dataset.csv":
            return httpx.Response(302, headers={"Location": "/final.csv"}, stream=redirect)
        # 302 without Location must still raise HTTPStatusError.
        return httpx.Response(status, stream=final)

    mock_transport(handler)
    error = BlockedError if status in (403, 429) else httpx.HTTPStatusError
    with pytest.raises(error):
        await default_http_get(_URL)
    assert final.bytes_read == 0
    assert redirect.closed and final.closed


@pytest.mark.parametrize("on_redirect", [True, False])
async def test_stream_read_failure_closes_response(mock_transport, on_redirect):
    stream = TrackedStream([b"partial"], error=httpx.ReadError("broken stream"))
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            302 if on_redirect else 200,
            headers={"Location": "/final.csv"} if on_redirect else {},
            stream=stream,
        )

    mock_transport(handler)
    with pytest.raises(httpx.ReadError, match="broken stream"):
        await default_http_get(_URL)
    assert stream.closed
    assert len(requests) == 1
