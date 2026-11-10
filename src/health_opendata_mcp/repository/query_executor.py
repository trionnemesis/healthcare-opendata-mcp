"""唯讀查詢執行 — query_rows 深度防禦第二層。

第一層(query_guard)擋語法;本層以五道機制守執行面:
1. mode=ro URI 連線 — 物理上不可寫
2. sqlite authorizer 白名單 — 僅允許讀單一物化表,跨表子查詢一律 DENY
3. progress handler 步數上限 — 防失控查詢吃資源
4. SQLite 連線層 setlimit — 限制單一值/SQL 文字/運算式深度的大小
5. 取列時的累計位元組預算 — 限制整個結果集佔用的記憶體

同步 sqlite3(非 aiosqlite):authorizer/progress handler 需要原生連線,
呼叫端以 asyncio.to_thread 包裝。

為何需要 4 與 5(CWE-770 / CWE-400):query_rows 的 columns/where/order_by
是呼叫端提供的 SQL 片段(設計如此,見 server.query_rows docstring)。
機制 1~3 都管不到「配置記憶體」這件事 —— authorizer 只看讀取哪張表,
load_extension 以外的純量函式一律 SQLITE_OK;progress handler 以 VM 步數
計量,而 `hex(zeroblob(200000000))` 只花極少步數就配置數百 MB。實測
(deploy/k8s/deployment.yaml 的 memory limit 為 512Mi):單一次
`where="a='x' AND length(hex(zeroblob(200000000))) > 0"` 即讓 RSS 衝到
約 570MB,足以觸發 OOMKill;liveness probe 失敗後 pod 重啟,反覆呼叫
等於整個服務的阻斷。故在連線層限制單值大小,並在取列時設總量預算。
"""
from __future__ import annotations

import sqlite3

from health_opendata_mcp.contracts import QueryResult

# progress handler 每 granularity 個 VM 步呼叫一次;超過 invocations 上限即中斷
_PROGRESS_GRANULARITY = 100_000
_MAX_PROGRESS_INVOCATIONS = 500  # ≈ 5 千萬 VM 步

# 單一字串/BLOB 值的上限(SQLITE_LIMIT_LENGTH,預設 1e9)。物化表存的是
# 政府開放資料的單一欄位值(案號、機關名、金額字串),實際都遠小於 2MiB;
# 此上限只擋 zeroblob()/randomblob()/hex() 這類人為放大的值。
MAX_VALUE_BYTES = 2 * 1024 * 1024

# SQL 文字上限(SQLITE_LIMIT_SQL_LENGTH,預設 1e9)。service 層對 keyword /
# dataset_id 等有 200 字元上限,但 query_rows 的 where/columns/order_by 片段
# 先前無任何長度限制 —— 單次呼叫即可送進 MB 級 SQL 交給 parser。
MAX_SQL_BYTES = 256 * 1024

# 運算式嵌套深度(SQLITE_LIMIT_EXPR_DEPTH,預設 1000)。深層嵌套由 parser
# 遞迴處理;壓到 200 仍遠高於任何真實查詢。
MAX_EXPR_DEPTH = 200

# 整個結果集的累計位元組預算。單值上限 × 列數仍可相乘放大
# (`SELECT hex(zeroblob(1000000)) FROM t LIMIT 401`),故取列時逐列累計,
# 超出即中止。遠高於任何正常查詢:400 列 × 數十欄的真實資料約數百 KB。
MAX_RESULT_BYTES = 32 * 1024 * 1024


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
    """套用連線層資源上限。

    setlimit 必須在 execute 之前呼叫才對該連線生效。這些是 SQLite 自己的
    上限,違反時由引擎拋 SQLITE_TOOBIG("string or blob too big")—— 在
    配置完成「之前」就失敗,這正是我們要的行為。
    """
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES)
    conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, MAX_EXPR_DEPTH)


def _value_bytes(value: object) -> int:
    """估算單一欄位值佔用的位元組(只需量級正確,用於預算累計)。"""
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8", "replace"))
    return 8  # int / float


def _fetch_within_budget(
    cur: sqlite3.Cursor, max_rows: int, max_bytes: int, sql: str
) -> list[tuple]:
    """逐列取出至多 max_rows 列,累計位元組超出 max_bytes 即中止。

    取代 fetchall():fetchall 會把整個結果集讀進記憶體「之後」才輪到呼叫端
    截斷,預算因此救不了放大攻擊。正常查詢(遠低於預算)行為與 fetchall 相同。
    """
    rows: list[tuple] = []
    total = 0
    for _ in range(max_rows):
        row = cur.fetchone()
        if row is None:
            break
        total += sum(_value_bytes(v) for v in row)
        if total > max_bytes:
            raise QueryDeniedError(
                f"查詢結果超出 {max_bytes} bytes 預算,已中止"
            )
        rows.append(tuple(row))
    return rows


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
            # build_select 的 LIMIT 已是 effective_limit + 1(供偵測截斷),
            # 這裡取同樣的列數上限,語意不變。
            rows = _fetch_within_budget(
                cur, effective_limit + 1, MAX_RESULT_BYTES, sql
            )
        except sqlite3.DatabaseError as exc:
            msg = str(exc).lower()
            if "not authorized" in msg or "prohibited" in msg:
                raise QueryDeniedError(f"查詢觸及白名單外的資源: {exc}") from exc
            if "interrupted" in msg:
                raise QueryDeniedError("查詢超出步數上限,已中斷") from exc
            # SQLITE_TOOBIG(值/SQL 文字超限)與 parser stack overflow
            # (運算式嵌套超過 SQLITE_LIMIT_EXPR_DEPTH)都是本層上限觸發的,
            # 一律轉 QueryDeniedError,呼叫端才會收到乾淨的 ValueError
            # 而不是未處理的 sqlite3 例外。
            if (
                "too big" in msg
                or "too large" in msg
                or "stack overflow" in msg
            ):
                raise QueryDeniedError(f"查詢超出資源上限: {exc}") from exc
            raise

        columns = tuple(d[0] for d in (cur.description or ()))
        truncated = len(rows) > effective_limit
        return QueryResult(
            columns=columns,
            rows=tuple(rows[:effective_limit]),
            truncated=truncated,
        )
    finally:
        conn.close()
