"""verify_catalog_sources — 實查探測(離線 fixture,不對官方端點發流量)。

本 script 的價值在於「只回報觀察到的事實」。因此測試的重點是:
失敗要明說失敗、非官方 host 連請求都不該發出、缺值不得被折成成功。
"""
import dataclasses
from datetime import date

import pytest
import verify_catalog_sources as vcs

from health_opendata_mcp.catalog import AdapterKind, CatalogEntry

CLINIC = CatalogEntry(
    dataset_id="demo-clinic",
    title="示範診所",
    kind=AdapterKind.NHI_API,
    r_id="A21030000I-D21004-009",
    natural_key_columns=("醫事機構代碼",),
    update_cadence="daily",
    enabled=True,
    verified_at="2026-06-10",
    verified_note="既有。",
)
MULTI = CatalogEntry(
    dataset_id="demo-multi",
    title="示範多檔",
    kind=AdapterKind.STATIC_CSV,
    urls=("https://data.gov.tw/a.csv", "https://data.gov.tw/b.csv"),
    natural_key_columns=("年度", "縣市別"),
    column_renames={"疾病別(第10版)": "疾病別"},
    update_cadence="yearly",
    enabled=True,
    verified_at="2026-06-10",
    verified_note="既有。",
)

# 實測 info.nhi.gov.tw 回應為 UTF-8 BOM CSV
_CSV = (
    "﻿醫事機構代碼,醫事機構名稱,縣市別代碼\n"
    "0102080017,高雄市立民生醫院,2\n"
    "0401180014,衛生福利部桃園醫院,3\n"
    "0401180014,衛生福利部桃園醫院,3\n"  # 來源含重複列
    ",缺代碼,9\n"
).encode("utf-8")


def _fetch(body: bytes, status: int = 200, ctype: str = "text/csv"):
    calls: list[str] = []

    async def fetch(url: str):
        calls.append(url)
        return status, ctype, body

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


class TestSuccessfulProbe:
    async def test_reports_observable_facts(self):
        fetch = _fetch(_CSV)
        (r,) = await vcs.probe_entry(CLINIC, fetch)
        assert r.ok and r.reason is None
        assert r.http_status == 200
        assert r.content_type == "text/csv"
        assert r.byte_size == len(_CSV)
        assert r.row_count == 4
        assert r.columns == ("醫事機構代碼", "醫事機構名稱", "縣市別代碼")
        assert r.missing_key_columns == ()

    async def test_duplicate_rows_collapse_into_distinct_keys(self):
        (r,) = await vcs.probe_entry(CLINIC, _fetch(_CSV))
        # 4 列 → 3 列有鍵、其中兩列同鍵 → 相異 2;1 列無鍵
        assert (r.distinct_keys, r.rows_without_key) == (2, 1)

    async def test_probes_every_url_of_a_multi_file_entry(self):
        fetch = _fetch("﻿年度,縣市別,疾病別(第10版)\n97,總計,總計\n".encode("utf-8"))
        results = await vcs.probe_entry(MULTI, fetch)
        assert len(results) == 2
        assert fetch.calls == list(MULTI.urls)
        # column_renames 與 ingestion 一致,否則報告的欄位不是實際會用的那組
        assert results[0].columns == ("年度", "縣市別", "疾病別")


class TestFailuresAreLoud:
    async def test_non_200_is_a_failure(self):
        (r,) = await vcs.probe_entry(CLINIC, _fetch(_CSV, status=404))
        assert not r.ok
        assert r.reason == "HTTP 404"

    async def test_empty_body_is_a_failure(self):
        (r,) = await vcs.probe_entry(CLINIC, _fetch(b"   "))
        assert not r.ok and "空" in r.reason

    async def test_header_only_csv_is_a_failure(self):
        (r,) = await vcs.probe_entry(CLINIC, _fetch("﻿醫事機構代碼\n".encode()))
        assert not r.ok and r.row_count == 0

    async def test_undecodable_body_is_a_failure_not_replaced(self):
        (r,) = await vcs.probe_entry(CLINIC, _fetch(b"\xff\xfe\x00bad"))
        assert not r.ok and "UTF-8" in r.reason

    async def test_missing_natural_key_column_is_a_failure(self):
        body = "﻿機構代號,名稱\n001,某院\n".encode("utf-8")
        (r,) = await vcs.probe_entry(CLINIC, _fetch(body))
        assert not r.ok
        assert r.missing_key_columns == ("醫事機構代碼",)
        assert "醫事機構代碼" in r.reason
        # 失敗仍要保留已觀察到的事實,方便診斷
        assert r.columns == ("機構代號", "名稱")

    async def test_fetch_exception_is_reported_not_raised(self):
        async def boom(url: str):
            raise RuntimeError("connection reset")

        (r,) = await vcs.probe_entry(CLINIC, boom)
        assert not r.ok and "connection reset" in r.reason


class TestHostAllowlistIsEnforcedBeforeAnyRequest:
    async def test_non_official_host_entry_sends_no_request(self):
        rogue = dataclasses.replace(
            MULTI, dataset_id="rogue", urls=("https://evil.example/x.csv",)
        )
        fetch = _fetch(_CSV)
        results = await vcs.probe_entry(rogue, fetch)
        assert fetch.calls == []  # 連請求都不該發出
        assert all(not r.ok for r in results)
        assert "未發出請求" in results[0].reason


class TestSizeCap:
    async def _chunks(self, *parts: bytes):
        for p in parts:
            yield p

    async def test_under_cap_returns_full_body(self):
        got = await vcs._collect_capped(self._chunks(b"ab", b"cd"), max_bytes=10)
        assert got == b"abcd"

    async def test_over_cap_aborts_instead_of_truncating(self):
        with pytest.raises(ValueError, match="上限"):
            await vcs._collect_capped(self._chunks(b"ab", b"cd"), max_bytes=3)


class TestRenderNote:
    def test_note_carries_the_probe_date_and_observed_counts(self):
        results = [
            vcs.ProbeResult(
                dataset_id="demo-clinic",
                url="https://info.nhi.gov.tw/x",
                ok=True,
                http_status=200,
                content_type="text/csv",
                byte_size=100,
                row_count=24695,
                columns=("醫事機構代碼", "醫事機構名稱"),
                distinct_keys=24695,
                rows_without_key=0,
                key_columns=("醫事機構代碼",),
            )
        ]
        note = vcs.render_note(results, date(2026, 9, 7))
        assert "實查 2026-09-07" in note
        assert "24695 列" in note
        assert "2 欄" in note
        assert "醫事機構代碼" in note

    def test_no_note_when_nothing_succeeded(self):
        failed = vcs.ProbeResult(dataset_id="x", url="u", ok=False, reason="HTTP 500")
        assert vcs.render_note([failed], date(2026, 9, 7)) == ""


class TestSelection:
    def test_enabled_only_excludes_candidates(self):
        from health_opendata_mcp.catalog import CATALOG

        selected = vcs._select(None, enabled_only=True)
        assert {e.dataset_id for e in selected} == {
            e.dataset_id for e in CATALOG if e.enabled
        }

    def test_only_narrows_to_one_entry(self):
        assert [e.dataset_id for e in vcs._select("nhi-clinic", False)] == ["nhi-clinic"]

    def test_unknown_dataset_id_exits_loudly(self):
        with pytest.raises(SystemExit, match="找不到"):
            vcs._select("does-not-exist", False)
