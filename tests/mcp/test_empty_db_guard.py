"""啟動護欄 — 空 DB 偵測:server 起動前若無任何資料集,給明確指引而非空回應。"""
from __future__ import annotations

from health_opendata_mcp.contracts import ColumnSpec, DatasetMeta, NormalizedBatch, Record
from health_opendata_mcp.mcp_server.__main__ import empty_db_error
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository


async def test_empty_db_returns_actionable_error(tmp_path):
    db_path = str(tmp_path / "empty.db")
    repo = SqliteRepository(db_path)
    await repo.init()

    msg = await empty_db_error(repo, db_path)

    assert msg is not None
    assert "hcmcp-sync" in msg  # 指引使用者先同步
    assert db_path in msg  # 指出實際 DB 路徑


async def test_populated_db_passes(tmp_path):
    db_path = str(tmp_path / "ok.db")
    repo = SqliteRepository(db_path)
    await repo.init()
    dataset = DatasetMeta(
        id="d1",
        source_id="s1",
        title="t",
        columns=(ColumnSpec("name"),),
        collection="healthcare",
    )
    await repo.upsert_batch(
        NormalizedBatch(
            dataset=dataset,
            records=(Record(dataset_id="d1", natural_key="a", payload={"name": "a"}),),
        )
    )

    assert await empty_db_error(repo, db_path) is None
