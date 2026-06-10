"""跨層 DTO、Enum、Protocol — DI 邊界(延續 g0VMCP contracts 哲學)。

所有層只依賴本模組型別;新增來源 = 實作一個 SourceAdapter。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class AccessStrategy(enum.Enum):
    PLATFORM_API = "PLATFORM_API"
    STATIC_FILE = "STATIC_FILE"
    APPLY_API = "APPLY_API"
    HTTP_SCRAPE = "HTTP_SCRAPE"
    HEADLESS_BROWSER = "HEADLESS_BROWSER"


class RunStatus(enum.Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ColumnSpec:
    """物化表欄位定義 — schema_json 的單元;type 限 SQLite storage class。"""

    name: str
    type: str = "TEXT"  # TEXT / INTEGER / REAL


@dataclass(frozen=True)
class DatasetMeta:
    id: str
    source_id: str
    title: str
    columns: tuple[ColumnSpec, ...] = ()
    collection: str | None = None
    license: str | None = None


@dataclass(frozen=True)
class ResourceRef:
    """discover() 產出的單一可抓資源(一個檔案 / 一個 API 端點)。"""

    dataset: DatasetMeta
    url: str
    fmt: str  # csv / xml / json
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RawPayload:
    ref: ResourceRef
    content: bytes


@dataclass(frozen=True)
class Record:
    dataset_id: str
    natural_key: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class NormalizedBatch:
    """normalize() 產出:欄位定義已補齊的 dataset + 該批 records。"""

    dataset: DatasetMeta
    records: tuple[Record, ...]


@dataclass(frozen=True)
class SourceInfo:
    id: str
    name: str
    platform: str
    access_strategy: AccessStrategy
    enabled: bool = True
    last_fetched_at: datetime | None = None


@dataclass
class IngestionRun:
    source_id: str
    started_at: datetime
    status: RunStatus = RunStatus.RUNNING
    finished_at: datetime | None = None
    fetched_count: int = 0
    error_detail: str | None = None


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    truncated: bool


class BlockedError(RuntimeError):
    """被來源限流/封鎖 — 呼叫端應退避。"""


class DatasetNotFoundError(KeyError):
    """dataset_id 不在白名單(datasets 表)中 — 使用者輸入永不直接成為表名。"""


class SourceAdapter(Protocol):
    """可插拔資料來源 — 新增來源只需實作此 Protocol。"""

    source_id: str
    name: str
    platform: str
    access_strategy: AccessStrategy

    async def discover(self) -> list[ResourceRef]: ...

    async def fetch(self, ref: ResourceRef) -> RawPayload: ...

    def normalize(self, raw: RawPayload) -> NormalizedBatch: ...
