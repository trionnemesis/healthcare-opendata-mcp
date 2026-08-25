"""唯讀查詢執行 — query_rows 深度防禦第二層。

第一層(query_guard)擋語法;本層以四道機制守執行面:
1. mode=ro URI 連線 — 物理上不可寫
2. sqlite authorizer 白名單 — 僅允許讀單一物化表,跨表子查詢一律 DENY
3. progress handler 步數上限 — 防失控查詢吃資源
4. sqlite 執行期 limit — 防「步數少但記憶體大」的查詢(見 _apply_limits)

同步 sqlite3(非 aiosqlite):authorizer/progress handler 需要原生連線,
呼叫端以 asyncio.to_thread 包裝。
"""
from __future__ import annotations

import sqlite3

from health_opendata_mcp.contracts import QueryResult

# progress handler 每 granularity 個 VM 步呼叫一次;超過 invocations 上限即中斷
_PROGRESS_GRANULARITY = 100_000
_MAX_PROGRESS_INVOCATIONS = 500  # ≈ 5 千萬 VM 步

# 單一字串/BLOB 值上限。progress handler 擋不住 `hex(zeroblob(2e8))` 這類
# 「VM 步數極少、單步配置數百 MB」的查詢(CWE-770);SQLite 預設上限 1GB,
# 對 memory limit 512Mi 的 pod 等同一發 OOMKill。8MB 遠高於本專案任何實際
# 欄位值(標案標題/廠商名皆 <1KB),壓不到正常查詢。
_MAX_VALUE_BYTES = 8_000_000
# SQL 文字上限:where/order_by 由使用者提供,不需要到 SQLite 預設的 1MB
_MAX_SQL_BYTES = 100_000
# LIKE/GLOB pattern 上限(SQLite 預設 50000):壓低 pattern 比對的最壞成本
_MAX_LIKE_PATTERN_BYTES = 4_000


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


def _apply_limits(conn: sqlite3.Connection) -> None:
    """收緊本連線的 SQLite 執行期上限,擋記憶體型資源耗用。

    超限時 SQLite 回 `string or blob too big`(sqlite3.DataError),由
    execute_readonly 轉為 QueryDeniedError,與 authorizer/步數上限一致。
    """
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_VALUE_BYTES)
    conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, _MAX_SQL_BYTES)
    conn.setlimit(sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, _MAX_LIKE_PATTERN_BYTES)


def execute_readonly(
    db_path: str, allowed_table: str, sql: str, effective_limit: int
) -> QueryResult:
    """執行 build_select 組出的 SQL(LIMIT=effective_limit+1)並偵測截斷。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        _apply_limits(conn)
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
            if "too big" in msg or "too complex" in msg:
                # _apply_limits 觸發:值/SQL/LIKE pattern 超出本連線上限
                raise QueryDeniedError("查詢超出大小上限,已拒絕") from exc
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
