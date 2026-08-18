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


class TestDetailUrlHostGuard:
    """明細連結來自上游 HTML,不可成為任意對外請求的跳板(CWE-918)。"""

    @pytest.mark.parametrize(
        "href",
        [
            "https://attacker.example.com/readBulletion?x=1",  # 絕對 URL 換主機
            "//attacker.example.com/readBulletion",  # protocol-relative
            "http://web.pcc.gov.tw/tps/readBulletion",  # 同主機但降級 HTTP
            "https://web.pcc.gov.tw.evil.com/readBulletion",  # 主機前綴混淆
        ],
    )
    async def test_off_host_detail_link_is_rejected(self, href):
        client = _FakeClient(search_html=f'<a href="{href}">明細</a>')
        with pytest.raises(ValueError, match="非預期來源"):
            await PccDetailEnricher(client).fetch_detail("A-1")
        # 關鍵斷言:被拒的主機完全沒有被請求過
        assert not any("attacker.example.com" in u for _, u in client.calls)

    async def test_relative_detail_link_still_followed(self):
        """相容性:正常的相對路徑行為與修正前相同。"""
        client = _FakeClient(
            search_html='<a href="/tps/tender/common/bulletion/readBulletion?pk=1">明細</a>',
            detail_html="<html></html>",
        )
        await PccDetailEnricher(client).fetch_detail("A-1")
        gets = [u for m, u in client.calls if m == "get"]
        assert gets == ["https://web.pcc.gov.tw/tps/tender/common/bulletion/readBulletion?pk=1"]

    async def test_absolute_same_host_link_still_followed(self):
        """相容性:上游若給同主機的絕對 URL,仍照舊跟進。"""
        url = "https://web.pcc.gov.tw/prkms/urlSelector/common/tpam?pk=NzEy"
        client = _FakeClient(
            search_html=f'<a href="{url}">明細</a>', detail_html="<html></html>"
        )
        await PccDetailEnricher(client).fetch_detail("A-1")
        assert ("get", url) in client.calls
