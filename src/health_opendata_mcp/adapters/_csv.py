"""CSV → NormalizedBatch 共用邏輯(NhiApiAdapter / StaticCsvAdapter)。"""
from __future__ import annotations

import csv
import io
from dataclasses import replace

from health_opendata_mcp.contracts import (
    ColumnSpec,
    NormalizedBatch,
    RawPayload,
    Record,
)

NATURAL_KEY_SEP = "|"  # 複合鍵串接符(spec/erm.dbml records.natural_key)


def normalize_csv(
    raw: RawPayload,
    natural_key_columns: tuple[str, ...],
    column_renames: dict[str, str] | None = None,
) -> NormalizedBatch:
    renames = column_renames or {}
    text = raw.content.decode("utf-8-sig", errors="replace")
    if not text.strip():
        return NormalizedBatch(dataset=raw.ref.dataset, records=())
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = [
        renames.get(f.strip(), f.strip()) for f in (reader.fieldnames or [])
    ]
    dataset = replace(
        raw.ref.dataset,
        columns=tuple(ColumnSpec(name) for name in fieldnames),
    )

    def _key(payload: dict[str, str]) -> str | None:
        parts = [payload.get(col, "").strip() for col in natural_key_columns]
        return NATURAL_KEY_SEP.join(parts) if all(parts) else None

    records = []
    for row in reader:
        payload = {
            renames.get(k.strip(), k.strip()): (v or "").strip()
            for k, v in row.items()
            if k
        }
        key = _key(payload)
        if key:
            records.append(
                Record(dataset_id=dataset.id, natural_key=key, payload=payload)
            )
    return NormalizedBatch(dataset=dataset, records=tuple(records))
