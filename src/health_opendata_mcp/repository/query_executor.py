"""唯讀查詢執行 — query_rows 深度防禦第二層。

第一層(query_guard)擋語法;本層以四道機制守執行面:
1. mode=ro URI 連線 — 物理上不可寫
2. sqlite authorizer 白名單 — 僅允許讀單一物化表,跨表子查詢一律 DENY
3. progress handler 步數上限 — 防失控查詢吃資源
4. SQLITE_LIMIT_LENGTH 上限 — 防單一 string/blob 配置吃掉記憶體

同步 sqlite3(非 aiosqlite):authorizer/progress handler 需要原生連線,
呼叫端以 asyncio.to_thread 包裝。
"""
from __future__ import annotations

import sqlite3

from health_opendata_mcp.contracts import QueryResult

# progress handler 每 granularity 個 VM 步呼叫一次;超過 invocations 上限即中斷
_PROGRESS_GRANULARITY = 100_000
_MAX_PROGRESS_INVOCATIONS = 500  # ≈ 5 千萬 VM 步

# 單一 string/blob 值的位元組上限(CWE-400)。
#
# progress handler 擋的是「步數多」的查詢,擋不到「一步配置很多記憶體」:
# WHERE length(hex(zeroblob(400000000))) > 0 通過 query_guard 的語法黑名單、
# 通過 authorizer(zeroblob/hex 都不是 load_extension),只花約 7 秒、數十個
# VM 步,就讓 RSS 從 22 MiB 漲到 1.1 GiB。deploy/k8s/deployment.yaml 的
# memory limit 是 512Mi,單一次 query_rows 即可讓 pod 被 OOM kill。
#
# 以 SQLITE_LIMIT_LENGTH 設上限可擋掉整類(不只具名函式):超限時 SQLite
# 立即回 "string or blob too big",不會先把記憶體配置出來。
# 8 MiB 遠大於任何真實資料列或 group_concat 聚合結果,正常查詢不受影響。
_MAX_VALUE_BYTES = 8 * 1024 * 1024


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
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_VALUE_BYTES)
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
            if "too big" in msg:
                raise QueryDeniedError(
                    "查詢產生的單一值超出大小上限,已拒絕"
                ) from exc
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
