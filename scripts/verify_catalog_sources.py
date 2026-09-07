"""verify_catalog_sources — 對 catalog entry 的官方端點實查,輸出觀察到的事實。

`catalog.py` 的驗證閘門要求 `enabled=True` 必須有 `verified_at` 與
`verified_note`。本 script 把「實查」從人工看網頁寫心得,變成可重跑、
只輸出可觀察事實的程序 —— 筆數、欄位、natural key 是否成立與是否唯一。

**本 script 只讀不寫。** 它永遠不會修改 `catalog.py`,也不會翻 `enabled`。
啟用一個資料集仍然是人工 review 後的 PR 編輯;這裡只提供貼上用的素材。

兩個用途:
1. **啟用候選前**(issue #25 Slice 2)——取得 `verified_at` / `verified_note`。
2. **上游漂移檢查** —— 已啟用的資料集若欄位或 natural key 不再成立,離開碼非零。

用法:
  verify_catalog_sources.py                    # 探測目錄中全部 entry
  verify_catalog_sources.py --only nhi-clinic  # 只探一筆
  verify_catalog_sources.py --enabled-only     # 只探已啟用者(漂移檢查)
  verify_catalog_sources.py --json             # 機器可讀輸出

離開碼:全部成功 0;任何一筆失敗 1。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import AsyncIterator, Awaitable, Callable

import httpx

from health_opendata_mcp.adapters._csv import NATURAL_KEY_SEP
from health_opendata_mcp.catalog import CATALOG, CatalogEntry, CatalogError, validate_catalog

# 單一回應的硬上限。上游若因故回傳巨大內容,寧可明確失敗也不要把維運者的
# 記憶體吃光;超過即中止,不截斷後假裝解析成功。
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

# (status, content_type, body) — 比 adapters 的 http_get 多回 status 與
# content-type,因為那兩項本身就是要記錄的實查事實。
Fetcher = Callable[[str], Awaitable[tuple[int, str, bytes]]]


@dataclass(frozen=True)
class ProbeResult:
    dataset_id: str
    url: str
    ok: bool
    reason: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    byte_size: int | None = None
    row_count: int | None = None
    columns: tuple[str, ...] = ()
    missing_key_columns: tuple[str, ...] = ()
    distinct_keys: int | None = None
    rows_without_key: int | None = None
    key_columns: tuple[str, ...] = ()


async def _collect_capped(chunks: AsyncIterator[bytes], max_bytes: int) -> bytes:
    """邊收邊記量,超過上限即中止。截斷後假裝解析成功比失敗更糟。"""
    out: list[bytes] = []
    total = 0
    async for chunk in chunks:
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"回應超過 {max_bytes} bytes 上限,已中止")
        out.append(chunk)
    return b"".join(out)


def _capped_fetch(max_bytes: int = DEFAULT_MAX_BYTES) -> Fetcher:
    async def fetch(url: str) -> tuple[int, str, bytes]:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                body = await _collect_capped(resp.aiter_bytes(), max_bytes)
                return resp.status_code, resp.headers.get("content-type", ""), body

    return fetch


def _parse_csv(
    body: bytes, entry: CatalogEntry
) -> tuple[tuple[str, ...], int, int, int]:
    """回 (columns, row_count, distinct_keys, rows_without_key)。

    欄位處理刻意與 adapters/_csv.normalize_csv 一致(strip + column_renames),
    否則報告出來的欄位名不是 ingestion 實際會用的那組。
    """
    text = body.decode("utf-8-sig")  # 解不開就是事實,不用 errors="replace" 掩蓋
    reader = csv.DictReader(io.StringIO(text))
    renames = entry.column_renames
    columns = tuple(
        renames.get(f.strip(), f.strip()) for f in (reader.fieldnames or [])
    )
    keys: set[str] = set()
    rows = 0
    without_key = 0
    for row in reader:
        rows += 1
        payload = {
            renames.get(k.strip(), k.strip()): (v or "").strip()
            for k, v in row.items()
            if k
        }
        parts = [payload.get(c, "") for c in entry.natural_key_columns]
        if all(parts):
            keys.add(NATURAL_KEY_SEP.join(parts))
        else:
            without_key += 1
    return columns, rows, len(keys), without_key


async def probe_entry(entry: CatalogEntry, fetch: Fetcher) -> list[ProbeResult]:
    """對一筆 entry 的每個下載 URL 實查。entry 本身不合法時不發出任何請求。"""
    try:
        validate_catalog((entry,))
    except CatalogError as exc:
        return [
            ProbeResult(
                dataset_id=entry.dataset_id,
                url=url,
                ok=False,
                reason=f"catalog 驗證失敗,未發出請求:{exc}",
                key_columns=entry.natural_key_columns,
            )
            for url in entry.download_urls
        ]

    results = []
    for url in entry.download_urls:
        results.append(await _probe_url(entry, url, fetch))
    return results


async def _probe_url(entry: CatalogEntry, url: str, fetch: Fetcher) -> ProbeResult:
    base = dict(
        dataset_id=entry.dataset_id, url=url, key_columns=entry.natural_key_columns
    )
    try:
        status, ctype, body = await fetch(url)
    except Exception as exc:  # noqa: BLE001 — 任何抓取失敗都是要回報的事實
        # 帶上例外類別:ProxyError 403 是本地 egress 政策擋的,不是上游限流。
        # 兩者都會顯示 "403 Forbidden",分不出來就會誤判資料源已失效。
        return ProbeResult(
            ok=False, reason=f"抓取失敗:{type(exc).__name__}: {exc}", **base
        )

    meta = dict(http_status=status, content_type=ctype or None, byte_size=len(body))
    if status != 200:
        return ProbeResult(ok=False, reason=f"HTTP {status}", **base, **meta)
    if not body.strip():
        return ProbeResult(ok=False, reason="回應為空", **base, **meta)
    try:
        columns, rows, distinct, without_key = _parse_csv(body, entry)
    except UnicodeDecodeError as exc:
        return ProbeResult(ok=False, reason=f"無法以 UTF-8 解碼:{exc}", **base, **meta)

    missing = tuple(c for c in entry.natural_key_columns if c not in columns)
    parsed = dict(
        row_count=rows,
        columns=columns,
        missing_key_columns=missing,
        distinct_keys=distinct,
        rows_without_key=without_key,
    )
    if missing:
        return ProbeResult(
            ok=False,
            reason=f"natural key 欄位不存在於回應:{'、'.join(missing)}",
            **base,
            **meta,
            **parsed,
        )
    if rows == 0:
        return ProbeResult(ok=False, reason="CSV 無資料列", **base, **meta, **parsed)
    return ProbeResult(ok=True, **base, **meta, **parsed)


def render_note(results: list[ProbeResult], probed_on: date) -> str:
    """由實查事實產生可貼進 catalog.py 的 verified_note。不加入任何推測。"""
    ok = [r for r in results if r.ok]
    if not ok:
        return ""
    day = probed_on.isoformat()
    total_rows = sum(r.row_count or 0 for r in ok)
    total_keys = sum(r.distinct_keys or 0 for r in ok)
    first = ok[0]
    parts = [
        f"實查 {day}:HTTP 200",
        f"{len(ok)} 個檔案" if len(ok) > 1 else (first.content_type or "無 content-type"),
        f"{total_rows} 列",
        f"{len(first.columns)} 欄",
        f"natural key {'、'.join(first.key_columns)} 相異 {total_keys} 筆",
    ]
    return "；".join(parts) + "。"


def _render_text(entry_results: list[tuple[CatalogEntry, list[ProbeResult]]]) -> str:
    today = datetime.now(timezone.utc).date()
    lines: list[str] = []
    for entry, results in entry_results:
        mark = "OK  " if all(r.ok for r in results) else "FAIL"
        lines.append(f"[{mark}] {entry.dataset_id}  (enabled={entry.enabled})")
        for r in results:
            lines.append(f"       url: {r.url}")
            if r.ok:
                lines.append(
                    f"       HTTP {r.http_status} · {r.content_type} · "
                    f"{r.byte_size} bytes · {r.row_count} 列 · {len(r.columns)} 欄"
                )
                lines.append(
                    f"       natural key {'|'.join(r.key_columns)} · "
                    f"相異 {r.distinct_keys} · 無鍵列 {r.rows_without_key}"
                )
                lines.append(f"       欄位: {'、'.join(r.columns)}")
            else:
                lines.append(f"       ✗ {r.reason}")
        note = render_note(results, today)
        if note and all(r.ok for r in results):
            lines.append("       建議填入 catalog.py：")
            lines.append(f'           verified_at="{today.isoformat()}",')
            lines.append(f'           verified_note="{note}",')
        lines.append("")
    lines.append("注意:本 script 不會修改 catalog.py。enabled 由人工 review 後手動翻。")
    return "\n".join(lines)


def _select(only: str | None, enabled_only: bool) -> list[CatalogEntry]:
    entries = [e for e in CATALOG if not enabled_only or e.enabled]
    if only:
        entries = [e for e in entries if e.dataset_id == only]
        if not entries:
            raise SystemExit(f"catalog 中找不到 dataset_id={only!r}")
    return entries


async def _run(args: argparse.Namespace) -> int:
    fetch = _capped_fetch(args.max_bytes)
    entry_results = [(e, await probe_entry(e, fetch)) for e in _select(args.only, args.enabled_only)]
    if args.json:
        print(
            json.dumps(
                {
                    "probed_at": datetime.now(timezone.utc).isoformat(),
                    "entries": [
                        {
                            "dataset_id": e.dataset_id,
                            "enabled": e.enabled,
                            "results": [asdict(r) for r in rs],
                        }
                        for e, rs in entry_results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(_render_text(entry_results))
    return 0 if all(r.ok for _, rs in entry_results for r in rs) else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="對 catalog 的官方端點實查,輸出 verified_note 素材(只讀)"
    )
    parser.add_argument("--only", help="只探測此 dataset_id")
    parser.add_argument(
        "--enabled-only", action="store_true", help="只探測已啟用的 entry(漂移檢查)"
    )
    parser.add_argument("--json", action="store_true", help="機器可讀輸出")
    parser.add_argument(
        "--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="單一回應大小上限"
    )
    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
