"""唯讀查詢執行 — query_rows 深度防禦第二層。

第一層(query_guard)擋語法;本層以三道機制守執行面:
1. mode=ro URI 連線 — 物理上不可寫
2. sqlite authorizer 白名單 — 僅允許讀單一物化表,跨表子查詢一律 DENY
3. progress handler 步數上限 — 防失控查詢吃資源

同步 sqlite3(非 aiosqlite):authorizer/progress handler 需要原生連線,
呼叫端以 asyncio.to_thread 包裝。
"""
from __future__ import annotations

import sqlite3

from health_opendata_mcp.contracts import QueryResult

# progress handler 每 granularity 個 VM 步呼叫一次;超過 invocations 上限即中斷
_PROGRESS_GRANULARITY = 100_000
_MAX_PROGRESS_INVOCATIONS = 500  # ≈ 5 千萬 VM 步


class QueryDeniedError(RuntimeError):
    """查詢觸及白名單外的資源或超出資源上限,被執行層拒絕。"""


def _make_authorizer(allowed_table: str):
    def authorize(action: int, arg1, arg2, db_name, trigger) -> int:
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            return (
                sqlite3.SQLITE_OK if arg1 == allowed_table else sqlite3.SQLITE_DENY
            )
        if action == sqlite3.SQLITE_FUNCTION:
            if (arg2 or "").lower() == "load_extension":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return authorize


def execute_readonly(
    db_path: str, allowed_table: str, sql: str, effective_limit: int
) -> QueryResult:
    """執行 build_select 組出的 SQL(LIMIT=effective_limit+1)並偵測截斷。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.set_authorizer(_make_authorizer(allowed_table))
        invocations = 0

        def _progress() -> int:
            nonlocal invocations
            invocations += 1
            return 1 if invocations > _MAX_PROGRESS_INVOCATIONS else 0

        conn.set_progress_handler(_progress, _PROGRESS_GRANULARITY)

        try:
            cur = conn.execute(sql)
            rows = cur.fetchall()
        except sqlite3.DatabaseError as exc:
            msg = str(exc).lower()
            if "not authorized" in msg or "prohibited" in msg:
                raise QueryDeniedError(f"查詢觸及白名單外的資源: {exc}") from exc
            if "interrupted" in msg:
                raise QueryDeniedError("查詢超出步數上限,已中斷") from exc
            raise

        columns = tuple(d[0] for d in (cur.description or ()))
        truncated = len(rows) > effective_limit
        return QueryResult(
            columns=columns,
            rows=tuple(tuple(r) for r in rows[:effective_limit]),
            truncated=truncated,
        )
    finally:
        conn.close()
