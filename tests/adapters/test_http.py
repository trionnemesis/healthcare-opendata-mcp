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
