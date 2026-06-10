"""NhiApiAdapter — 健保署資料開放平台(info.nhi.gov.tw)CSV API。

一級官方來源:每日更新、免 API key(實測 2026-06)。
資料集以 NhiDatasetSpec 註冊表驅動 — rId 無命名規則可推,逐一登錄。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, replace
from typing import Awaitable, Callable

from health_opendata_mcp.adapters._http import default_http_get
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
    collection: str = "healthcare"


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
                meta={"natural_key_column": spec.natural_key_column},
            )
            for spec in self._specs
        ]

    async def fetch(self, ref: ResourceRef) -> RawPayload:
        return RawPayload(ref=ref, content=await self._http_get(ref.url))

    def normalize(self, raw: RawPayload) -> NormalizedBatch:
        text = raw.content.decode("utf-8-sig", errors="replace")
        if not text.strip():
            return NormalizedBatch(dataset=raw.ref.dataset, records=())
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = [f.strip() for f in (reader.fieldnames or [])]
        nk_col = raw.ref.meta["natural_key_column"]
        dataset = replace(
            raw.ref.dataset,
            columns=tuple(ColumnSpec(name) for name in fieldnames),
        )
        records = tuple(
            Record(
                dataset_id=dataset.id,
                natural_key=(row.get(nk_col) or "").strip(),
                payload={k.strip(): (v or "").strip() for k, v in row.items() if k},
            )
            for row in reader
            if (row.get(nk_col) or "").strip()
        )
        return NormalizedBatch(dataset=dataset, records=records)
