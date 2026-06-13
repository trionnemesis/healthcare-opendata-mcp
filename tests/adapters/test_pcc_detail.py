"""PCC 明細頁解析純函式 — 截標/開標/預算 enrich(BDD: ingestion.feature)。

fixture 取自 g0VMCP(MIT)實戰頁:
- pcc_detail_synthetic.html:乾淨 th/td,含「截止投標」label
- pcc_detail_live.html:真實 web.pcc 明細頁(開標/預算在 td/td,截止投標僅在 JS)
"""
from pathlib import Path

import pytest

from health_opendata_mcp.adapters import _pcc_detail as detail

_FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


class TestParseRocDatetime:
    def test_full_datetime(self):
        assert detail.parse_roc_datetime("114/01/20 14:30") == "2025-01-20 14:30"

    def test_date_only(self):
        assert detail.parse_roc_datetime("114/05/08") == "2025-05-08"

    def test_three_digit_year(self):
        assert detail.parse_roc_datetime("99/12/31 09:05") == "2010-12-31 09:05"

    def test_empty_or_garbage(self):
        assert detail.parse_roc_datetime("") == ""
        assert detail.parse_roc_datetime("查無資料") == ""


class TestParseMoney:
    def test_comma_separated_with_unit(self):
        assert detail.parse_money("12,500,000元") == "12500000"

    def test_no_digits(self):
        assert detail.parse_money("查無資料") == ""
        assert detail.parse_money("") == ""


class TestExtractFields:
    def test_th_td_model(self):
        html = "<table><tr><th>開標時間</th><td>114/05/08 10:00</td></tr></table>"
        fields = detail.extract_fields(html)
        assert fields["開標時間"] == "114/05/08 10:00"

    def test_td_td_model(self):
        html = "<table><tr><td>預算金額</td><td>1,000元</td></tr></table>"
        assert detail.extract_fields(html)["預算金額"] == "1,000元"

    def test_first_seen_wins(self):
        html = (
            "<table><tr><th>標案名稱</th><td>甲</td></tr>"
            "<tr><th>標案名稱</th><td>乙</td></tr></table>"
        )
        assert detail.extract_fields(html)["標案名稱"] == "甲"

    def test_script_content_ignored(self):
        # JS 內的 <td> 樣字串不可污染欄位(真實頁陷阱)
        html = (
            "<script>var x='<td>開標時間</td><td>FAKE</td>';</script>"
            "<table><tr><td>開標時間</td><td>114/05/08 10:00</td></tr></table>"
        )
        assert detail.extract_fields(html)["開標時間"] == "114/05/08 10:00"


class TestExtractDetailSynthetic:
    """乾淨合成頁:截止投標/開標/預算皆在 th/td。"""

    def test_all_fields(self):
        d = detail.extract_detail(_fixture("pcc_detail_synthetic.html"))
        assert d.open_date == "2025-01-20 14:30"
        assert d.bid_deadline == "2025-01-20 12:00"
        assert d.budget == "12500000"
        assert d.title == "114年度資通安全防護服務採購案"
        assert d.procurement_attr == "財物類"


class TestExtractDetailLive:
    """真實 web.pcc 明細頁:開標/截止投標/預算皆可從表格可靠抽取。"""

    def test_open_date_and_budget(self):
        d = detail.extract_detail(_fixture("pcc_detail_live.html"))
        assert d.open_date == "2025-05-08 10:00"
        assert d.budget == "1437749369"
        assert d.agency == "衛生福利部食品藥物管理署"

    def test_bid_deadline_from_table(self):
        # 截止投標(系統截止收件)2025-05-07 17:00,早於開標 2025-05-08 10:00
        d = detail.extract_detail(_fixture("pcc_detail_live.html"))
        assert d.bid_deadline == "2025-05-07 17:00"


class TestSecurityGuards:
    def test_html4_doctype_accepted(self):
        # 真實 web.pcc 頁以 <!DOCTYPE HTML PUBLIC ...> 開頭;html.parser 不展開
        # 實體(無 XXE),須正常解析而非拒絕
        html = (
            '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN">'
            "<table><tr><td>開標時間</td><td>114/05/08 10:00</td></tr></table>"
        )
        assert detail.extract_detail(html).open_date == "2025-05-08 10:00"

    def test_rejects_oversize(self):
        with pytest.raises(ValueError):
            detail.extract_detail("<html>" + "x" * (detail.MAX_HTML_CHARS + 1))
