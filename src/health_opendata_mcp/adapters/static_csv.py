"""StaticCsvAdapter — 政府開放資料靜態 CSV 檔(data.gov.tw distribution)。

一個 StaticCsvSpec 可含多個 URL(如門診就診率按年度區間分檔),
discover() 對每個 URL 產出一個 ResourceRef,共用同一 dataset id,
records 經 upsert 合併至同一物化表。
column_renames 統一跨檔欄位名(如「疾病別(第10版)」→「疾病別」)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from health_opendata_mcp.adapters._csv import normalize_csv
from health_opendata_mcp.adapters._http import default_http_get
from health_opendata_mcp.contracts import (
    AccessStrategy,
    DatasetMeta,
    NormalizedBatch,
    RawPayload,
    ResourceRef,
)

_LICENSE = "政府資料開放授權條款 1.0"


@dataclass(frozen=True)
class StaticCsvSpec:
    dataset_id: str
    title: str
    urls: tuple[str, ...]
    natural_key_columns: tuple[str, ...]
    column_renames: dict[str, str] = field(default_factory=dict)
    collection: str = "healthcare"


class StaticCsvAdapter:
    source_id = "gov-static"
    name = "政府開放資料靜態檔案"
    platform = "data.gov.tw"
    access_strategy = AccessStrategy.STATIC_FILE

    def __init__(
        self,
        specs: list[StaticCsvSpec],
        http_get: Callable[[str], Awaitable[bytes]] | None = None,
    ) -> None:
        self._specs = {spec.dataset_id: spec for spec in specs}
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
                url=url,
                fmt="csv",
            )
            for spec in self._specs.values()
            for url in spec.urls
        ]

    async def fetch(self, ref: ResourceRef) -> RawPayload:
        return RawPayload(ref=ref, content=await self._http_get(ref.url))

    def normalize(self, raw: RawPayload) -> NormalizedBatch:
        spec = self._specs[raw.ref.dataset.id]
        return normalize_csv(raw, spec.natural_key_columns, spec.column_renames)
