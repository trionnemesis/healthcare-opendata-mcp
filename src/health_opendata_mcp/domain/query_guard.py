"""query_rows 的 SQL 安全驗證與組裝 — 純函式,零 I/O。

不變量(spec/erm.dbml #4):唯讀單一 SELECT;使用者輸入永不成為 SQL 識別符。
本模組是深度防禦第一層(語法面);第二層在 executor:
mode=ro 連線 + sqlite authorizer 白名單(僅允許讀單一物化表)。

取捨:黑名單寬鬆掃描會誤殺字面值含 `--`/REPLACE() 的罕見查詢 —
可接受,因為真正的防線是 authorizer,本層只求把明顯攻擊擋在 SQL 組裝前。
"""
from __future__ import annotations

import re

MAX_LIMIT = 200
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


def _validate(fragment: str, *, what: str) -> str:
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
    cols = ", ".join(_validate(c, what="columns") for c in columns) if columns else "*"
    parts = [f'SELECT {cols} FROM "{table}"']
    if where:
        parts.append(f"WHERE {_validate(where, what='where')}")
    if group_by:
        joined = ", ".join(_validate(g, what="group_by") for g in group_by)
        parts.append(f"GROUP BY {joined}")
    if order_by:
        parts.append(f"ORDER BY {_validate(order_by, what='order_by')}")
    effective = normalize_limit(limit)
    parts.append(f"LIMIT {effective + 1}")
    return " ".join(parts), effective
