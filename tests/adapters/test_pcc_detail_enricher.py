"""PccDetailEnricher — search → detail 串接(fake client,零網路)。"""
from pathlib import Path

import pytest

from health_opendata_mcp.adapters import _pcc_detail as detail
from health_opendata_mcp.adapters.pcc_detail import HttpResp, PccDetailEnricher
from health_opendata_mcp.contracts import BlockedError

_FIXTURES = Path(__file__).parent / "fixtures"


def _fx(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


class _FakeClient:
    """search URL 回搜尋頁;其餘(明細頁)回 detail 頁。可設 status 模擬封鎖。"""

    def __init__(self, search_html="", detail_html="", status=200):
        self.search_html = search_html
        self.detail_html = detail_html
        self.status = status
        self.calls: list[str] = []

    async def get(self, url: str) -> HttpResp:
        self.calls.append(("get", url))
        return HttpResp(self.status, self.detail_html)

    async def post(self, url: str, data) -> HttpResp:
        self.calls.append(("post", url))
        return HttpResp(self.status, self.search_html)


class TestFindDetailPath:
    def test_matches_case_no(self):
        path = detail.find_detail_path(_fx("pcc_search_result.html"), "1130108-5")
        assert path is not None and "readBulletion" in path
        assert "caseNo=1130108-5" in path

    def test_no_link_returns_none(self):
        assert detail.find_detail_path("<html>查無資料</html>", "X") is None


class TestEnricher:
    async def test_fetch_detail_happy_path(self):
        client = _FakeClient(
            search_html=_fx("pcc_search_result.html"),
            detail_html=_fx("pcc_detail_live.html"),
        )
        enr = PccDetailEnricher(client)
        d = await enr.fetch_detail("1130108-5")
        assert d is not None
        assert d.open_date == "2025-05-08 10:00"
        assert d.bid_deadline == "2025-05-07 17:00"
        assert d.budget == "1437749369"
        # 確實走了 search(post)再 detail(get)
        assert [c[0] for c in client.calls] == ["post", "get"]

    async def test_no_detail_link_returns_none(self):
        client = _FakeClient(search_html="<html>查無資料</html>")
        d = await PccDetailEnricher(client).fetch_detail("NOPE")
        assert d is None

    async def test_blocked_status_raises(self):
        client = _FakeClient(search_html="x", status=429)
        with pytest.raises(BlockedError):
            await PccDetailEnricher(client).fetch_detail("X")
