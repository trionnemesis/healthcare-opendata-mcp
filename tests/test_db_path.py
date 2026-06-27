"""DB 路徑單一真實來源 — hcmcp-sync(寫)與 hcmcp server(讀)共用同一預設,
避免「server 讀 A、sync 寫 B → 同步了卻查不到」的歷史地雷(見 CHANGELOG 0.6.1)。

兩個進入點(cli.sync_main 的 --db 預設、mcp_server.__main__.main 的 db_path)
皆解析自 cli.default_db_path(),故鎖住此函式行為即同時保護兩者。
"""
from __future__ import annotations

from pathlib import Path

from health_opendata_mcp.cli import default_db_path
from health_opendata_mcp.mcp_server import __main__ as server_main


def test_default_db_path_is_home_hcmcp(monkeypatch):
    monkeypatch.delenv("HCMCP_DB", raising=False)
    # 必須是 home 下的絕對路徑,不可是 repo 相對路徑(裝在哪都一致)
    assert default_db_path() == str(Path.home() / ".hcmcp" / "hcmcp.db")


def test_default_db_path_honors_env(monkeypatch, tmp_path):
    custom = str(tmp_path / "custom.db")
    monkeypatch.setenv("HCMCP_DB", custom)
    assert default_db_path() == custom


def test_server_entrypoint_shares_single_source(monkeypatch):
    """server 進入點與 sync 必須是同一個 default_db_path(避免各自 inline 預設)。"""
    assert server_main.default_db_path is default_db_path
