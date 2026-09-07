"""NhiApiAdapter — 健保署資料開放平台(info.nhi.gov.tw)CSV API。

一級官方來源:每日更新、免 API key(實測 2026-06)。
資料集以 NhiDatasetSpec 註冊表驅動 — rId 無命名規則可推,逐一登錄。
"""
from __future__ import annotations

from dataclasses import dataclass
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

# public:catalog 需要它來組出「實際會被抓取的 URL」並做網域白名單檢查
NHI_API_BASE = "https://info.nhi.gov.tw/api/iode0000s01/Dataset"
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
                url=f"{NHI_API_BASE}?rId={spec.r_id}",
                fmt="csv",
                meta={"natural_key_columns": list(spec.effective_key_columns)},
            )
            for spec in self._specs
        ]

    async def fetch(self, ref: ResourceRef) -> RawPayload:
        return RawPayload(ref=ref, content=await self._http_get(ref.url))

    def normalize(self, raw: RawPayload) -> NormalizedBatch:
        return normalize_csv(raw, tuple(raw.ref.meta["natural_key_columns"]))
