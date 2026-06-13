"""PCC 標案明細頁解析純函式 — 補 pcc-tender 缺的截標/開標/預算,零網路 I/O。

半月 open data 招標檔無「開標/截止投標/預算金額」(見 _pcc_opendata);這些只在
web.pcc 明細頁。解析邏輯參考 g0VMCP fetcher(MIT)實戰結論:
- 開標時間 / 預算金額在真實頁是可靠的 td/td 表格欄位
- 截止投標在真實頁多半只藏於 JS 變數,表格抽不到 → 回空字串(看板以開標時間為實際投標 deadline)

刻意只用 stdlib html.parser(不引入 selectolax 重依賴);script/style 內文字一律忽略。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

MAX_HTML_CHARS = 4_000_000  # 真實明細頁約 140KB;上限兼顧資源護欄

_MONEY_RE = re.compile(r"[\d,]+")
# ROC 民國日期(時間選填):114/01/20 14:30 或 114/05/08
_ROC_DT_RE = re.compile(r"(\d{2,3})/(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?")


@dataclass(frozen=True)
class DetailFields:
    """明細頁擷取結果。全欄位 str(對齊 pcc-tender TEXT 風格),缺值為空字串。"""

    bid_deadline: str = ""  # 截止投標(ISO);真實頁常抽不到
    open_date: str = ""  # 開標時間(ISO)= 實際投標 deadline
    budget: str = ""  # 預算金額(純數字字串)
    title: str = ""
    agency: str = ""
    procurement_attr: str = ""


def parse_roc_datetime(text: str) -> str:
    """ROC 民國日期時間 → ISO 字串;無法解析回空字串。

    114/01/20 14:30 → 2025-01-20 14:30;114/05/08 → 2025-05-08(無時間)。
    """
    m = _ROC_DT_RE.search(text or "")
    if not m:
        return ""
    roc_y, mo, d, hh, mm = m.groups()
    year = int(roc_y) + 1911
    base = f"{year:04d}-{int(mo):02d}-{int(d):02d}"
    if hh is not None:
        return f"{base} {int(hh):02d}:{mm}"
    return base


def parse_money(text: str) -> str:
    """取金額純數字字串(去千分位/單位);無數字回空字串。"""
    m = _MONEY_RE.search(text or "")
    if not m:
        return ""
    return m.group(0).replace(",", "")


class _TableFieldParser(HTMLParser):
    """串流擷取 (label → value):th/td 與相鄰 td/td 兩種模型;first-seen wins。

    忽略 script/style 內文字(真實頁 JS 含 <td> 樣字串,會污染欄位)。
    巢狀表格每個 <tr> 各自成列(對齊 selectolax css('tr') 行為)。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}
        self._row: list[tuple[str, str]] = []  # 當列 cells: (tag, text)
        self._cell_tag: str | None = None
        self._buf: list[str] = []
        self._skip_depth = 0  # script/style 巢狀深度

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag == "tr":
            self._flush_row()
        elif tag in ("td", "th"):
            self._close_cell()
            self._cell_tag = tag
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._flush_row()

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and self._cell_tag is not None:
            self._buf.append(data)

    def _close_cell(self) -> None:
        if self._cell_tag is not None:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self._row.append((self._cell_tag, text))
            self._cell_tag = None
            self._buf = []

    def _flush_row(self) -> None:
        self._close_cell()
        cells = self._row
        self._row = []
        if not cells:
            return
        th = [c for c in cells if c[0] == "th"]
        td = [c for c in cells if c[0] == "td"]
        if th:
            # th 模型:該列首 th = label、首 td = value(對齊 g0vmcp)
            label = th[0][1].rstrip(":：")
            value = td[0][1] if td else ""
            self._set(label, value)
        else:
            # td/td 模型:相鄰兩 td 視為 label/value(first-seen 過濾雜訊)
            for i in range(len(cells) - 1):
                self._set(cells[i][1].rstrip(":："), cells[i + 1][1])

    def _set(self, label: str, value: str) -> None:
        if label and label not in self.fields:
            self.fields[label] = value

    def close(self) -> None:  # noqa: D102
        super().close()
        self._flush_row()


def _guard(html: str) -> None:
    # 僅資源上限:stdlib html.parser 不展開 DTD/ENTITY(無 XXE),故真實頁的
    # <!DOCTYPE HTML ...> 須正常接受;XXE 護欄只在 XML 解析(見 _pcc_opendata)。
    if len(html) > MAX_HTML_CHARS:
        raise ValueError("PCC 明細頁超過安全解析大小上限")


def extract_fields(html: str) -> dict[str, str]:
    """明細頁 → {label: value};label 首見為準。"""
    _guard(html)
    parser = _TableFieldParser()
    parser.feed(html)
    parser.close()
    return parser.fields


# 當前 PCC(2026):結果頁明細連結為 /prkms/urlSelector/common/tpam?pk=<base64>
# (GET 後 302 轉址至 /tps/QueryTender/query/searchTenderDetail);保留 readBulletion 舊格式容錯
_HREF_RE = re.compile(
    r'href="([^"]*(?:urlSelector/common/tpam\?pk=|readBulletion)[^"]*)"',
    re.IGNORECASE,
)


def find_detail_path(search_html: str, job_number: str) -> str | None:
    """從 readTenderBasic 搜尋結果頁找該案明細頁路徑(tpam?pk= 或舊 readBulletion)。

    優先 caseNo 完全匹配(readBulletion 舊格式有);tpam 連結不含 caseNo → 回首筆
    (依案號搜尋通常唯一結果)。回相對路徑;查無回 None。
    """
    hrefs = _HREF_RE.findall(search_html or "")
    if not hrefs:
        return None
    for href in hrefs:
        if f"caseNo={job_number}" in href or f"caseNo={job_number}&" in href:
            return href
    return hrefs[0]


def _parse_attr(text: str) -> str:
    """標的分類「財物類 4523 - …」→ 採購性質。"""
    for attr in ("工程類", "財物類", "勞務類"):
        if attr in text:
            return attr
    return ""


def extract_detail(html: str) -> DetailFields:
    """明細頁 HTML → DetailFields。缺欄位以空字串容錯。"""
    f = extract_fields(html)
    return DetailFields(
        bid_deadline=parse_roc_datetime(f.get("截止投標", "")),
        open_date=parse_roc_datetime(f.get("開標時間", "")),
        budget=parse_money(f.get("預算金額", "")),
        title=f.get("標案名稱", ""),
        agency=f.get("機關名稱", ""),
        procurement_attr=_parse_attr(f.get("標的分類", "")),
    )
