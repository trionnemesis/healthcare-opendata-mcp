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

    def test_matches_tpam_link(self):
        # 當前 PCC(2026)結果頁明細連結為 tpam?pk=<base64>(不含 caseNo)
        html = '<a href="/prkms/urlSelector/common/tpam?pk=NzEyNDQ3MjA=">明細</a>'
        path = detail.find_detail_path(html, "115-2-013")
        assert path == "/prkms/urlSelector/common/tpam?pk=NzEyNDQ3MjA="

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


class TestDetailUrlIsAnchoredToPcc:
    """安全回歸(CWE-918):明細連結來自遠端 HTML,不得把 client 導向 web.pcc 以外主機。"""

    @pytest.mark.parametrize(
        "href",
        [
            "https://attacker.example/readBulletion",       # 絕對 URL
            "//attacker.example/readBulletion",             # protocol-relative
            "/readBulletion@attacker.example/x",            # userinfo 混淆
            "/prkms/urlSelector/common/tpam?pk=x//evil",    # 路徑內夾帶 //
        ],
    )
    async def test_offsite_detail_link_is_rejected(self, href):
        client = _FakeClient(
            search_html=f'<a href="{href}">明細</a>',
            detail_html=_fx("pcc_detail_live.html"),
        )
        with pytest.raises(ValueError):
            await PccDetailEnricher(client).fetch_detail("1130108-5")
        # 關鍵:對外請求根本沒發出去(只有搜尋那一次 post)
        assert [c[0] for c in client.calls] == ["post"]

    async def test_relative_link_still_fetches_from_pcc(self):
        client = _FakeClient(
            search_html=_fx("pcc_search_result.html"),
            detail_html=_fx("pcc_detail_live.html"),
        )
        d = await PccDetailEnricher(client).fetch_detail("1130108-5")
        assert d is not None
        get_urls = [u for kind, u in client.calls if kind == "get"]
        assert get_urls == [
            "https://web.pcc.gov.tw/tps/tender/common/bulletion/readBulletion"
            "?pkPmsMain=53000000&orgId=3.80.11&caseNo=1130108-5"
        ]
