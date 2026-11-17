"""唯讀查詢執行 — query_rows 深度防禦第二層。

第一層(query_guard)擋語法;本層以四道機制守執行面:
1. mode=ro URI 連線 — 物理上不可寫
2. sqlite authorizer 資源白名單 — 僅允許讀單一物化表,跨表子查詢一律 DENY
3. sqlite authorizer 函式白名單 — 只放行唯讀且不會放大輸出的內建函式
4. progress handler 步數上限 — 防失控查詢吃資源

同步 sqlite3(非 aiosqlite):authorizer/progress handler 需要原生連線,
呼叫端以 asyncio.to_thread 包裝。
"""
from __future__ import annotations

import sqlite3

from health_opendata_mcp.contracts import QueryResult

# progress handler 每 granularity 個 VM 步呼叫一次;超過 invocations 上限即中斷
_PROGRESS_GRANULARITY = 100_000
_MAX_PROGRESS_INVOCATIONS = 500  # ≈ 5 千萬 VM 步

# 函式白名單(CWE-770)。progress handler 數的是 VM 步數,擋不住「少步數、
# 大配置」的函式:randomblob(1e9) 只要一步就配置 1GB,printf('%1000000000d',1)
# 亦同。乘上 MAX_LIMIT=400 列,單一 query_rows 呼叫即可耗盡行程記憶體
# (實測 randomblob(1e9) 已達 5.7GiB RSS;GKE deployment 的 memory limit
#  為 512Mi,等同一次呼叫打掉一個 pod)。
#
# 改以白名單而非黑名單:SQLite 內建函式會隨版本新增,黑名單必然落後。
# 名單涵蓋 query_rows 對外承諾的全部分析用法(聚合、字串、日期、CAST、
# LIKE/GLOB、JSON、window function),僅排除「輸出長度不受來源資料約束」
# 的放大型函式與洩漏建置資訊的 sqlite_* 內省函式。
# 需要新函式時在此處增補,不要改回全放行。
_ALLOWED_FUNCTIONS = frozenset(
    {
        # 聚合
        "count", "sum", "total", "avg", "min", "max", "group_concat",
        # 比較運算子(SQLite 以函式回報)
        "like", "glob", "match", "regexp",
        # 字串 — 輸出長度受來源資料約束
        "length", "lower", "upper", "ltrim", "rtrim", "trim", "substr",
        "substring", "instr", "hex", "unhex", "quote", "char", "unicode",
        "concat", "concat_ws",
        # 數值
        "abs", "round", "ceil", "ceiling", "floor", "sign", "mod", "random",
        # 條件 / 型別
        "coalesce", "ifnull", "nullif", "iif", "typeof",
        # 日期時間
        "date", "time", "datetime", "julianday", "unixepoch", "strftime",
        "timediff",
        # JSON(唯讀取值)
        "json_extract", "json_type", "json_valid", "json_array_length",
        "json_quote", "json_each", "json_tree",
        # window function
        "row_number", "rank", "dense_rank", "percent_rank", "cume_dist",
        "ntile", "lag", "lead", "first_value", "last_value", "nth_value",
    }
)


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
            # arg2 為函式名;SQLite 對運算子型函式回報大寫(如 GLOB),故一律小寫比對
            return (
                sqlite3.SQLITE_OK
                if (arg2 or "").lower() in _ALLOWED_FUNCTIONS
                else sqlite3.SQLITE_DENY
            )
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
            if "not authorized to use function" in msg:
                raise QueryDeniedError(f"查詢使用了白名單外的函式: {exc}") from exc
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
