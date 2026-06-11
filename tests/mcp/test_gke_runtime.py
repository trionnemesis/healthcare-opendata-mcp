"""GKE 部署運行面 — transport 解析與 /healthz readiness probe。"""
from __future__ import annotations

import httpx

from health_opendata_mcp.contracts import ColumnSpec, DatasetMeta, NormalizedBatch, Record
from health_opendata_mcp.mcp_server.__main__ import resolve_transport
from health_opendata_mcp.mcp_server.server import build_server
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository


def test_resolve_transport_default_stdio():
    transport, kwargs = resolve_transport({})
    assert transport == "stdio"
    assert kwargs == {}


def test_resolve_transport_http_reads_host_port():
    transport, kwargs = resolve_transport(
        {"HCMCP_TRANSPORT": "http", "HCMCP_HOST": "127.0.0.1", "HCMCP_PORT": "9000"}
    )
    assert transport == "http"
    assert kwargs == {"host": "127.0.0.1", "port": 9000}


def test_resolve_transport_http_defaults():
    transport, kwargs = resolve_transport({"HCMCP_TRANSPORT": "http"})
    assert transport == "http"
    assert kwargs == {"host": "0.0.0.0", "port": 8000}


def test_resolve_transport_sse_backcompat():
    transport, kwargs = resolve_transport({"HCMCP_TRANSPORT": "sse"})
    assert transport == "sse"
    assert kwargs == {"host": "0.0.0.0", "port": 8000}


async def test_healthz_returns_ok(tmp_path):
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
    mcp = build_server(repo)

    app = mcp.http_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/healthz")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
