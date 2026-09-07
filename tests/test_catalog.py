"""資料集目錄 — 不變量與 adapter 投影(BDD: source-registration.feature)。

catalog 是「同步哪些政府開放資料」的宣告式來源;本檔測的是那些讓它
可以安全被持續擴充的規則,而不是某一筆資料的內容。
"""
import dataclasses

import pytest

from health_opendata_mcp.adapters.nhi import NHI_API_BASE
from health_opendata_mcp.catalog import (
    CATALOG,
    OFFICIAL_HOSTS,
    AdapterKind,
    CatalogEntry,
    CatalogError,
    enabled_nhi_specs,
    enabled_static_specs,
    validate_catalog,
)

VERIFIED_NHI = CatalogEntry(
    dataset_id="demo-nhi",
    title="示範 NHI 資料集",
    kind=AdapterKind.NHI_API,
    r_id="A21030000I-D21004-009",
    natural_key_columns=("醫事機構代碼",),
    update_cadence="daily",
    enabled=True,
    verified_at="2026-06-10",
    verified_note="實查:24.5k 筆。",
)
VERIFIED_STATIC = CatalogEntry(
    dataset_id="demo-static",
    title="示範靜態 CSV 資料集",
    kind=AdapterKind.STATIC_CSV,
    urls=("https://data.gov.tw/demo/old.csv", "https://data.gov.tw/demo/new.csv"),
    natural_key_columns=("年度", "縣市別"),
    column_renames={"疾病別(第10版)": "疾病別"},
    update_cadence="yearly",
    enabled=True,
    verified_at="2026-06-10",
    verified_note="實查:兩個年度區間檔。",
)


def _entry(**overrides) -> CatalogEntry:
    return dataclasses.replace(VERIFIED_NHI, **overrides)


class TestShippedCatalog:
    """實際出貨的目錄必須自洽 —— 這一條在 import 時也會跑一次。"""

    def test_ships_valid_catalog(self):
        validate_catalog(CATALOG)

    def test_dataset_ids_are_unique(self):
        ids = [e.dataset_id for e in CATALOG]
        assert len(ids) == len(set(ids))

    def test_clinic_dataset_is_enabled_with_verified_rid(self):
        clinic = {e.dataset_id: e for e in CATALOG}["nhi-clinic"]
        assert clinic.enabled
        # 實查 2026-06-10:D32001-001 查無資料,正確 rId 為 D21004-009
        assert clinic.r_id == "A21030000I-D21004-009"
        assert clinic.verified_at == "2026-06-10"

    def test_unverified_entries_are_not_enabled(self):
        for entry in CATALOG:
            if entry.verified_at is None:
                assert not entry.enabled, entry.dataset_id

    def test_every_download_url_targets_an_official_host(self):
        from urllib.parse import urlsplit

        for entry in CATALOG:
            for url in entry.download_urls:
                assert urlsplit(url).hostname in OFFICIAL_HOSTS, url


class TestVerificationGate:
    """核心不變量:沒有驗證證據就不准啟用。"""

    def test_enabled_without_verified_at_is_rejected(self):
        with pytest.raises(CatalogError, match="verified_at"):
            validate_catalog((_entry(verified_at=None),))

    def test_enabled_without_verified_note_is_rejected(self):
        with pytest.raises(CatalogError, match="verified_note"):
            validate_catalog((_entry(verified_note="   "),))

    def test_disabled_entry_may_stay_unverified(self):
        validate_catalog((_entry(enabled=False, verified_at=None, verified_note=""),))

    def test_verified_at_must_be_iso_date(self):
        with pytest.raises(CatalogError, match="YYYY-MM-DD"):
            validate_catalog((_entry(verified_at="2026/06/10"),))


class TestHostAllowlist:
    """entry 是資料;不設網域白名單等於「加一筆資料 = 可對任意主機發請求」。"""

    def test_non_official_static_host_is_rejected(self):
        with pytest.raises(CatalogError, match="白名單"):
            validate_catalog(
                (dataclasses.replace(
                    VERIFIED_STATIC, urls=("https://evil.example/x.csv",)
                ),)
            )

    def test_http_static_url_is_rejected(self):
        with pytest.raises(CatalogError, match="https"):
            validate_catalog(
                (dataclasses.replace(
                    VERIFIED_STATIC, urls=("http://data.gov.tw/x.csv",)
                ),)
            )

    def test_landing_url_is_checked_too(self):
        with pytest.raises(CatalogError, match="landing_url"):
            validate_catalog((_entry(landing_url="https://evil.example/page"),))


class TestRIdShape:
    """r_id 會被插值進 query string;字元集限制擋掉改寫其他參數的可能。"""

    @pytest.mark.parametrize(
        "bad", ["A21030000I&rId=X", "A21030000I D21004", "", "A21030000I?x=1", "abc"]
    )
    def test_malformed_rid_is_rejected(self, bad):
        with pytest.raises(CatalogError):
            validate_catalog((_entry(r_id=bad),))

    def test_rid_becomes_the_download_url(self):
        assert VERIFIED_NHI.download_urls == (
            f"{NHI_API_BASE}?rId=A21030000I-D21004-009",
        )


class TestKindFieldConsistency:
    def test_nhi_entry_must_not_carry_urls(self):
        with pytest.raises(CatalogError, match="不應有 urls"):
            validate_catalog((_entry(urls=("https://data.gov.tw/x.csv",)),))

    def test_static_entry_must_not_carry_rid(self):
        with pytest.raises(CatalogError, match="不應有 r_id"):
            validate_catalog(
                (dataclasses.replace(VERIFIED_STATIC, r_id="A2103-D21004-009"),)
            )

    def test_static_entry_needs_at_least_one_url(self):
        with pytest.raises(CatalogError, match="url"):
            validate_catalog((dataclasses.replace(VERIFIED_STATIC, urls=()),))

    def test_natural_key_columns_required(self):
        with pytest.raises(CatalogError, match="natural_key_columns"):
            validate_catalog((_entry(natural_key_columns=()),))

    def test_unknown_cadence_rejected(self):
        with pytest.raises(CatalogError, match="update_cadence"):
            validate_catalog((_entry(update_cadence="每天"),))

    def test_duplicate_dataset_id_rejected(self):
        with pytest.raises(CatalogError, match="重複"):
            validate_catalog((VERIFIED_NHI, _entry(title="另一個")))


class TestProjection:
    """catalog → adapter spec。停用的 entry 不得產生任何網路請求來源。"""

    def test_only_enabled_entries_become_specs(self):
        entries = (VERIFIED_NHI, _entry(dataset_id="off", enabled=False))
        specs = enabled_nhi_specs(entries)
        assert [s.dataset_id for s in specs] == ["demo-nhi"]

    def test_nhi_projection_preserves_composite_key(self):
        entry = _entry(
            dataset_id="bed-ratio", natural_key_columns=("機構代碼", "統計年月")
        )
        spec = enabled_nhi_specs((entry,))[0]
        assert spec.effective_key_columns == ("機構代碼", "統計年月")

    def test_static_projection_carries_urls_and_renames(self):
        spec = enabled_static_specs((VERIFIED_STATIC,))[0]
        assert spec.urls == VERIFIED_STATIC.urls
        assert spec.column_renames == {"疾病別(第10版)": "疾病別"}
        assert spec.natural_key_columns == ("年度", "縣市別")

    def test_kinds_do_not_leak_into_each_other(self):
        entries = (VERIFIED_NHI, VERIFIED_STATIC)
        assert [s.dataset_id for s in enabled_nhi_specs(entries)] == ["demo-nhi"]
        assert [s.dataset_id for s in enabled_static_specs(entries)] == ["demo-static"]
