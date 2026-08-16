"""NhiApiAdapter — 健保署資料開放平台(info.nhi.gov.tw)CSV API。

一級官方來源:每日更新、免 API key(實測 2026-06)。
資料集以 NhiDatasetSpec 註冊表驅動 — rId 無命名規則可推,逐一登錄。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Awaitable, Callable

from health_opendata_mcp.adapters._csv import normalize_csv
from health_opendata_mcp.adapters._http import default_http_get
from health_opendata_mcp.adapters.nhi_facility import classify_healthcare_facility
from health_opendata_mcp.contracts import (
    AccessStrategy,
    ColumnSpec,
    DatasetMeta,
    NormalizedBatch,
    RawPayload,
    Record,
    ResourceRef,
)

_BASE = "https://info.nhi.gov.tw/api/iode0000s01/Dataset"
_LICENSE = "政府資料開放授權條款 1.0"


@dataclass(frozen=True)
class NhiDatasetSpec:
    dataset_id: str
    r_id: str
    title: str
    natural_key_column: str = "醫事機構代碼"
    natural_key_columns: tuple[str, ...] | None = None  # 複合鍵;優先於單欄
    collection: str = "healthcare"

    @property
    def effective_key_columns(self) -> tuple[str, ...]:
        return self.natural_key_columns or (self.natural_key_column,)


class NhiApiAdapter:
    source_id = "nhi-opendata"
    name = "健保署資料開放平台"
    platform = "info.nhi.gov.tw"
    access_strategy = AccessStrategy.PLATFORM_API

    def __init__(
        self,
        specs: list[NhiDatasetSpec],
        http_get: Callable[[str], Awaitable[bytes]] | None = None,
    ) -> None:
        self._specs = specs
        self._http_get = http_get or default_http_get

    async def discover(self) -> list[ResourceRef]:
        return [
            ResourceRef(
                dataset=DatasetMeta(
                    id=spec.dataset_id,
                    source_id=self.source_id,
                    title=spec.title,
                    collection=spec.collection,
                    license=_LICENSE,
                ),
                url=f"{_BASE}?rId={spec.r_id}",
                fmt="csv",
                meta={"natural_key_columns": list(spec.effective_key_columns)},
            )
            for spec in self._specs
        ]

    async def fetch(self, ref: ResourceRef) -> RawPayload:
        return RawPayload(ref=ref, content=await self._http_get(ref.url))

    def normalize(self, raw: RawPayload) -> NormalizedBatch:
        return normalize_csv(raw, tuple(raw.ref.meta["natural_key_columns"]))


class NhiHealthcareFacilityAdapter(NhiApiAdapter):
    """專責：健保統一院所名冊，輸出衍生欄位並做需求 scope 篩選。"""

    _REQUIRED_FIELDS = frozenset({
        "醫事機構代碼",
        "權屬別名稱",
        "醫事機構名稱",
        "特約類別",
        "終止合約或歇業日期",
    })
    _DERIVED_FIELDS = (
        "facility_type",
        "governing_level",
        "classification_source",
        "is_active",
    )

    def normalize(self, raw: RawPayload) -> NormalizedBatch:
        batch = normalize_csv(raw, tuple(raw.ref.meta["natural_key_columns"]))
        field_names = {column.name for column in batch.dataset.columns}
        missing = self._REQUIRED_FIELDS - field_names
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise ValueError(f"健保院所名冊 schema drift，缺少欄位: {missing_list}")
        if not batch.records:
            raise ValueError("健保院所名冊沒有可識別的資料列")

        enriched_records: dict[str, Record] = {}
        for rec in batch.records:
            classified = classify_healthcare_facility(rec.payload)
            if classified is None:
                continue
            payload = dict(rec.payload)
            payload.update(
                {
                    "facility_type": classified.facility_type,
                    "governing_level": classified.governing_level,
                    "classification_source": classified.classification_source,
                    "is_active": classified.is_active,
                }
            )
            enriched_records[rec.natural_key] = Record(
                dataset_id=rec.dataset_id,
                natural_key=rec.natural_key,
                payload=payload,
            )

        if not enriched_records:
            raise ValueError("健保院所名冊沒有符合需求範圍的資料列")

        columns = (
            *batch.dataset.columns,
            ColumnSpec("facility_type"),
            ColumnSpec("governing_level"),
            ColumnSpec("classification_source"),
            ColumnSpec("is_active", "INTEGER"),
        )
        return NormalizedBatch(
            dataset=replace(batch.dataset, columns=columns),
            records=tuple(enriched_records.values()),
        )
