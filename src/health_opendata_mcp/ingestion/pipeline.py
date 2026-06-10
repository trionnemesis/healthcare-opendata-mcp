"""ETL pipeline — discover → fetch → normalize → upsert。

容錯策略(BDD: ingestion.feature):
- 單一 ref 失敗:記錄後續跑(半月檔缺期、暫時性網路錯誤不應中斷整輪)
- 全部失敗:FAILED
- BlockedError:立即停止整個來源(反爬倫理 — 被擋不可硬打),標 BLOCKED
"""
from __future__ import annotations

from dataclasses import dataclass, field

from health_opendata_mcp.contracts import BlockedError, RunStatus, SourceAdapter, SourceInfo
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository


@dataclass
class RunSummary:
    source_id: str
    status: RunStatus
    fetched_count: int = 0
    errors: list[str] = field(default_factory=list)


async def run_source(adapter: SourceAdapter, repo: SqliteRepository) -> RunSummary:
    await repo.register_source(
        SourceInfo(
            id=adapter.source_id,
            name=adapter.name,
            platform=adapter.platform,
            access_strategy=adapter.access_strategy,
        )
    )
    run_id = await repo.start_run(adapter.source_id)
    summary = RunSummary(source_id=adapter.source_id, status=RunStatus.RUNNING)

    try:
        refs = await adapter.discover()
        for ref in refs:
            try:
                raw = await adapter.fetch(ref)
                batch = adapter.normalize(raw)
                if batch.records:
                    summary.fetched_count += await repo.upsert_batch(batch)
            except BlockedError as exc:
                summary.errors.append(f"{ref.url}: blocked ({exc})")
                summary.status = RunStatus.BLOCKED
                break
            except Exception as exc:  # noqa: BLE001 — 單 ref 容錯是規格行為
                summary.errors.append(f"{ref.url}: {exc}")
        if summary.status is RunStatus.RUNNING:
            all_failed = bool(refs) and len(summary.errors) == len(refs)
            summary.status = RunStatus.FAILED if all_failed else RunStatus.SUCCEEDED
    except Exception as exc:  # discover 本身失敗
        summary.errors.append(f"discover: {exc}")
        summary.status = RunStatus.FAILED

    await repo.finish_run(
        run_id,
        summary.status,
        summary.fetched_count,
        "; ".join(summary.errors[:5]) or None,
    )
    return summary
