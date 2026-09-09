"""資料集目錄 — 「同步哪些政府開放資料」的單一真實來源。

## 為什麼是 data 而不是散落的常數

adapter 已經是 registry-driven 的泛型 CSV 管線,新增一個資料集的程式碼
成本接近零;真正貴的是「這個 resource id 是何時、對哪個官方端點驗證過」
這件事以前只寫在註解裡 —— 執行期看不到、測試測不到、CI 擋不住。

本模組把註冊表變成**帶出處的宣告式資料**,並以不變量把「未驗證就啟用」
變成一個會在 import 時炸掉的錯誤(loud failure,不是靜默降級)。

## 擴展方式

1. 新增一筆 `CatalogEntry`,`enabled=False`、`verified_at=None`。
2. 對官方端點實查,把觀察到的**事實**(筆數、欄位、更新頻率)寫進
   `verified_note`,填上 `verified_at`。
3. 改 `enabled=True`。

第 2 步不可省略:`enabled=True` 而沒有 `verified_at` 會讓 catalog 驗證失敗。
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit

from health_opendata_mcp.adapters.nhi import NHI_API_BASE, NhiDatasetSpec
from health_opendata_mcp.adapters.static_csv import StaticCsvSpec


class CatalogError(ValueError):
    """catalog 不變量被違反 —— 屬程式錯誤,在 import 時即失敗。"""


class AdapterKind(enum.Enum):
    """entry 由哪個 adapter 消費。決定 r_id / urls 哪一組欄位有效。"""

    NHI_API = "nhi_api"
    # 與 NHI_API 同一個端點與 r_id 形制,但由 NhiHealthcareFacilityAdapter 消費:
    # 它在 normalize 時做需求範圍篩選並產出 facility_type 等衍生欄位。
    NHI_FACILITY = "nhi_facility"
    STATIC_CSV = "static_csv"


# 官方一手來源網域白名單。catalog 的下載與說明頁 URL 只能落在這裡面 ——
# 這同時是 SSRF 邊界:entry 是資料,若不設限,新增一筆資料就等於新增一個
# 對任意主機發請求的能力。要新增網域必須改這份常數並經 review。
OFFICIAL_HOSTS = frozenset(
    {
        "info.nhi.gov.tw",
        "www.nhi.gov.tw",
        "data.gov.tw",
        "www.mohw.gov.tw",
        "dep.mohw.gov.tw",
    }
)

UPDATE_CADENCES = frozenset(
    {"daily", "weekly", "monthly", "quarterly", "yearly", "irregular", "unknown"}
)

# rId 會被字串插值進 query string(見 NhiApiAdapter.discover)。限制字元集
# 才能保證它不會挾帶 & ? # 或空白去改寫 URL 的其他參數。
_R_ID_RE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+$")


@dataclass(frozen=True)
class CatalogEntry:
    """一個資料集的宣告 —— 含出處與驗證狀態。"""

    dataset_id: str
    title: str
    kind: AdapterKind
    natural_key_columns: tuple[str, ...]
    update_cadence: str
    # 何時對官方端點實查過(YYYY-MM-DD)。None = 尚未驗證,必須 enabled=False。
    verified_at: str | None = None
    # 實查當下觀察到的事實(筆數、欄位、已知陷阱)。不寫推測。
    verified_note: str = ""
    enabled: bool = False
    collection: str = "healthcare"
    # 官方人可讀的資料集說明頁;沒有經確認的頁面就留 None,不臆造。
    landing_url: str | None = None
    r_id: str | None = None  # kind=NHI_API 專用
    urls: tuple[str, ...] = ()  # kind=STATIC_CSV 專用
    column_renames: dict[str, str] = field(default_factory=dict)

    @property
    def download_urls(self) -> tuple[str, ...]:
        """實際會被抓取的 URL —— 白名單檢查的對象。"""
        if self.kind is AdapterKind.NHI_API:
            return (f"{NHI_API_BASE}?rId={self.r_id}",)
        return self.urls


def _check_url(url: str, *, entry_id: str, label: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise CatalogError(f"{entry_id}: {label} 必須是 https — {url!r}")
    if parts.hostname not in OFFICIAL_HOSTS:
        raise CatalogError(
            f"{entry_id}: {label} 的 host 不在官方白名單 — {parts.hostname!r}"
        )


def _validate_entry(entry: CatalogEntry) -> None:
    eid = entry.dataset_id
    if not eid or not entry.title:
        raise CatalogError(f"{eid!r}: dataset_id 與 title 不可為空")
    if not entry.natural_key_columns or not all(entry.natural_key_columns):
        raise CatalogError(f"{eid}: natural_key_columns 不可為空")
    if entry.update_cadence not in UPDATE_CADENCES:
        raise CatalogError(
            f"{eid}: update_cadence 未知 — {entry.update_cadence!r}"
        )

    if entry.kind in (AdapterKind.NHI_API, AdapterKind.NHI_FACILITY):
        kind_name = entry.kind.value
        if not entry.r_id:
            raise CatalogError(f"{eid}: kind={kind_name} 必須有 r_id")
        if entry.urls:
            raise CatalogError(f"{eid}: kind={kind_name} 不應有 urls")
        if not _R_ID_RE.match(entry.r_id):
            raise CatalogError(f"{eid}: r_id 格式不合法 — {entry.r_id!r}")
    else:
        if not entry.urls:
            raise CatalogError(f"{eid}: kind=static_csv 必須有至少一個 url")
        if entry.r_id:
            raise CatalogError(f"{eid}: kind=static_csv 不應有 r_id")

    for url in entry.download_urls:
        _check_url(url, entry_id=eid, label="download url")
    if entry.landing_url:
        _check_url(entry.landing_url, entry_id=eid, label="landing_url")

    # 核心不變量:沒有驗證證據就不准啟用。
    if entry.enabled:
        if not entry.verified_at:
            raise CatalogError(f"{eid}: enabled=True 必須填 verified_at")
        if not entry.verified_note.strip():
            raise CatalogError(f"{eid}: enabled=True 必須填 verified_note")
    if entry.verified_at:
        try:
            date.fromisoformat(entry.verified_at)
        except ValueError as exc:
            raise CatalogError(
                f"{eid}: verified_at 需為 YYYY-MM-DD — {entry.verified_at!r}"
            ) from exc


def validate_catalog(entries: tuple[CatalogEntry, ...]) -> None:
    """逐筆驗證 + 全域唯一性。違反即拋 CatalogError。"""
    seen: set[str] = set()
    for entry in entries:
        _validate_entry(entry)
        if entry.dataset_id in seen:
            raise CatalogError(f"dataset_id 重複 — {entry.dataset_id}")
        seen.add(entry.dataset_id)


# ---------------------------------------------------------------------------
# 目錄本體
# ---------------------------------------------------------------------------
CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        dataset_id="nhi-clinic",
        title="健保特約醫事機構-診所",
        kind=AdapterKind.NHI_API,
        r_id="A21030000I-D21004-009",
        natural_key_columns=("醫事機構代碼",),
        update_cadence="daily",
        enabled=True,
        verified_at="2026-06-10",
        verified_note=(
            "實查:D32001-001 查無資料,正確 rId 為 D21004-009;"
            "約 24.5k 筆,回應為 UTF-8 BOM CSV,每日更新。"
        ),
    ),
    # --- 以下為候選:值取自本 repo 既有測試,尚無官方端點實查紀錄 ---
    CatalogEntry(
        dataset_id="nhi-healthcare-facility",
        title="健保特約醫療院所名冊-需求範圍",
        kind=AdapterKind.NHI_FACILITY,
        r_id="A21030000I-D2100G-001",
        natural_key_columns=("醫事機構代碼",),
        update_cadence="daily",
        enabled=False,
        verified_note=(
            "候選:adapter 與分類規則已隨 PR #15 併入並有測試覆蓋,但本 session"
            "的 egress policy 擋住 info.nhi.gov.tw(CONNECT 403),無法實查端點,"
            "故不填 verified_at、維持 enabled=False。"
            "啟用前請對官方端點實查 rId、筆數、欄位與更新頻率,把觀察到的事實"
            "寫進本欄並填 verified_at。"
        ),
    ),
    CatalogEntry(
        dataset_id="nhi-hospital-district",
        title="健保特約醫事機構-地區醫院",
        kind=AdapterKind.NHI_API,
        r_id="A21030000I-D21003-003",
        natural_key_columns=("醫事機構代碼",),
        update_cadence="unknown",
        enabled=False,
        verified_note=(
            "候選:rId 取自 tests/adapters/test_nhi.py,repo 內無實查日期紀錄。"
            "啟用前需確認 rId 有效、筆數與更新頻率。"
        ),
    ),
    CatalogEntry(
        dataset_id="nhi-hospital-bed-ratio",
        title="全民健保特約醫院之保險病床比率",
        kind=AdapterKind.NHI_API,
        r_id="A21030000I-D02001-015",
        natural_key_columns=("機構代碼", "統計年月"),
        update_cadence="unknown",
        enabled=False,
        verified_note=(
            "候選:rId 取自 tests/adapters/test_nhi.py,repo 內無實查日期紀錄。"
            "同機構多月份,故 natural key 為複合鍵。"
        ),
    ),
)

validate_catalog(CATALOG)  # import 時即失敗,不讓壞掉的目錄跑到同步階段


# ---------------------------------------------------------------------------
# 投影成各 adapter 的 spec
# ---------------------------------------------------------------------------
def enabled_entries(
    entries: tuple[CatalogEntry, ...] = CATALOG,
) -> tuple[CatalogEntry, ...]:
    return tuple(e for e in entries if e.enabled)


def enabled_nhi_specs(
    entries: tuple[CatalogEntry, ...] = CATALOG,
) -> list[NhiDatasetSpec]:
    return [
        NhiDatasetSpec(
            dataset_id=e.dataset_id,
            r_id=e.r_id or "",
            title=e.title,
            natural_key_columns=e.natural_key_columns,
            collection=e.collection,
        )
        for e in enabled_entries(entries)
        if e.kind is AdapterKind.NHI_API
    ]


def enabled_nhi_facility_specs(
    entries: tuple[CatalogEntry, ...] = CATALOG,
) -> list[NhiDatasetSpec]:
    """投影出由 NhiHealthcareFacilityAdapter 消費的 spec。

    與 enabled_nhi_specs 同型別(都是 NhiDatasetSpec),差別只在消費它的
    adapter —— facility adapter 會再做範圍篩選與衍生欄位。
    """
    return [
        NhiDatasetSpec(
            dataset_id=e.dataset_id,
            r_id=e.r_id or "",
            title=e.title,
            natural_key_columns=e.natural_key_columns,
            collection=e.collection,
        )
        for e in enabled_entries(entries)
        if e.kind is AdapterKind.NHI_FACILITY
    ]


def enabled_static_specs(
    entries: tuple[CatalogEntry, ...] = CATALOG,
) -> list[StaticCsvSpec]:
    return [
        StaticCsvSpec(
            dataset_id=e.dataset_id,
            title=e.title,
            urls=e.urls,
            natural_key_columns=e.natural_key_columns,
            column_renames=dict(e.column_renames),
            collection=e.collection,
        )
        for e in enabled_entries(entries)
        if e.kind is AdapterKind.STATIC_CSV
    ]
