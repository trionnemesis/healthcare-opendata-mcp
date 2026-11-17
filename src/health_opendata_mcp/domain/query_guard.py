"""query_rows 的 SQL 安全驗證與組裝 — 純函式,零 I/O。

不變量(spec/erm.dbml #4):唯讀單一 SELECT;使用者輸入永不成為 SQL 識別符。
本模組是深度防禦第一層(語法面);第二層在 executor:
mode=ro 連線 + sqlite authorizer 白名單(僅允許讀單一物化表)。

取捨:黑名單寬鬆掃描會誤殺字面值含 `--`/REPLACE() 的罕見查詢 —
可接受,因為真正的防線是 authorizer,本層只求把明顯攻擊擋在 SQL 組裝前。
"""
from __future__ import annotations

import re

# 400 = pcc-it-tender-board 看板單次查詢量(twinkle-hub 原行為);仍有 executor
# progress handler 兜底,放寬不影響資源護欄
MAX_LIMIT = 400
DEFAULT_LIMIT = 50


def normalize_limit(limit: int, *, max_limit: int = MAX_LIMIT) -> int:
    """將工具 limit 正規化為 1..max_limit,避免負數/過大值造成資源耗用。"""
    return max(1, min(int(limit), max_limit))


class QueryValidationError(ValueError):
    """使用者查詢參數含禁用語法。"""


_FORBIDDEN = re.compile(
    r";|--|/\*"
    r"|\b(pragma\w*|attach|detach|insert|update|delete|drop|create|alter"
    r"|replace|vacuum|reindex|load_extension)\b",
    re.IGNORECASE,
)

# 單一 SQL 片段長度上限(CWE-400)。QueryService 已對 keyword / dataset_id /
# natural_key / job_number 設 200 字元上限,但 query_rows 的 SQL 片段當時未納入
# —— 這些片段同樣是 MCP 工具的自由文字參數,且會進到 regex 掃描與 SQLite
# parser。無上限時單次呼叫即可送進 MB 級字串(實測 3MB where 可直接通過)。
# 2000 字元遠大於任何真實分析查詢(repo 內最長的 where 不到 100 字元),
# 不影響正常用法;超量者在 SQL 組裝前就被擋下。
MAX_FRAGMENT_LEN = 2000

# columns / group_by 是清單參數:逐項長度上限擋不住「很多項」。
# 64 項遠多於任何物化表的欄位數,仍讓總組裝長度有界。
MAX_FRAGMENT_COUNT = 64


def _validate_count(fragments: list[str], *, what: str) -> list[str]:
    if len(fragments) > MAX_FRAGMENT_COUNT:
        raise QueryValidationError(
            f"{what} 項數過多({len(fragments)},上限 {MAX_FRAGMENT_COUNT})"
        )
    return fragments


def _validate(fragment: str, *, what: str) -> str:
    if len(fragment) > MAX_FRAGMENT_LEN:
        raise QueryValidationError(
            f"{what} 過長({len(fragment)} 字元,上限 {MAX_FRAGMENT_LEN})"
        )
    m = _FORBIDDEN.search(fragment)
    if m:
        raise QueryValidationError(f"{what} 含禁用語法: {m.group(0)!r}")
    return fragment


def build_select(
    table: str,
    *,
    columns: list[str] | None = None,
    where: str | None = None,
    group_by: list[str] | None = None,
    order_by: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[str, int]:
    """組裝唯讀 SELECT。回傳 (sql, effective_limit)。

    table 必須由呼叫端以 dataset_id 白名單映射取得 — 本函式不接使用者原始輸入。
    SQL 的 LIMIT 為 effective_limit + 1,供 executor 偵測截斷。
    """
    cols = (
        ", ".join(
            _validate(c, what="columns")
            for c in _validate_count(columns, what="columns")
        )
        if columns
        else "*"
    )
    # table 來自 dataset_id 白名單、cols 已過 _validate,非使用者原始輸入
    parts = [f'SELECT {cols} FROM "{table}"']  # nosec B608
    if where:
        parts.append(f"WHERE {_validate(where, what='where')}")
    if group_by:
        joined = ", ".join(
            _validate(g, what="group_by")
            for g in _validate_count(group_by, what="group_by")
        )
        parts.append(f"GROUP BY {joined}")
    if order_by:
        parts.append(f"ORDER BY {_validate(order_by, what='order_by')}")
    effective = normalize_limit(limit)
    parts.append(f"LIMIT {effective + 1}")
    return " ".join(parts), effective
