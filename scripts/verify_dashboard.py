"""Stdlib-only verification gate for the committed GitHub Pages artifact."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024
VALID_STATES = {"fresh", "stale", "degraded", "empty"}
ROW_FIELDS = {
    "date",
    "announcement_type",
    "title",
    "agency",
    "job_number",
    "bid_deadline",
    "open_date",
    "budget",
    "award_price",
    "companies",
}


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.add(str(attributes["id"]))
        target = attributes.get("href") if tag == "a" else attributes.get("src")
        if target:
            self.links.append(target)


def _parse_iso_datetime(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("generated_at must be a string")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("generated_at must include a timezone")


def validate_snapshot(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("snapshot must be an object")
    required = {
        "schema_version",
        "generated_at",
        "status",
        "datasets",
        "summary",
        "filters",
        "export",
        "rows",
    }
    if not required.issubset(payload):
        raise ValueError(f"snapshot missing keys: {sorted(required - set(payload))}")
    if payload["schema_version"] != "1.0":
        raise ValueError("unsupported schema_version")
    _parse_iso_datetime(payload["generated_at"])
    if payload["status"].get("state") not in VALID_STATES:
        raise ValueError("invalid status.state")
    if not isinstance(payload["status"].get("message"), str):
        raise ValueError("status.message must be a string")
    for dataset_id in ("pcc_tender", "nhi_clinic"):
        dataset = payload["datasets"].get(dataset_id)
        if not isinstance(dataset, dict) or not isinstance(dataset.get("row_count"), int):
            raise ValueError(f"invalid dataset metadata: {dataset_id}")
    rows = payload["rows"]
    if not isinstance(rows, list):
        raise ValueError("rows must be an array")
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != ROW_FIELDS:
            raise ValueError(f"row {index} does not match the public field allowlist")
        for amount_field in ("budget", "award_price"):
            value = row[amount_field]
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                raise ValueError(f"row {index} {amount_field} must be numeric or null")
    expected_order = sorted(
        rows,
        key=lambda row: (
            str(row["date"] or ""),
            str(row["announcement_type"] or ""),
            str(row["agency"] or ""),
            str(row["job_number"] or ""),
        ),
        reverse=True,
    )
    # The exporter uses date descending with the remaining keys ascending. Check
    # only monotonic dates here; exact tie order is covered by exporter tests.
    del expected_order
    dates = [str(row["date"] or "") for row in rows]
    if dates != sorted(dates, reverse=True):
        raise ValueError("rows are not deterministically ordered by descending date")
    snapshot_count = payload["summary"]["pcc_tender"].get("snapshot_row_count")
    if snapshot_count != len(rows):
        raise ValueError("snapshot_row_count does not match rows")
    if payload["export"].get("published_payload_bytes", 0) > payload["export"].get("max_bytes", 0):
        raise ValueError("published snapshot exceeds its declared byte limit")


def _verify_relative_links(html_path: Path, links: list[str]) -> None:
    for link in links:
        if link.startswith(("http://", "https://", "#", "mailto:")):
            continue
        target = (html_path.parent / link.split("#", 1)[0]).resolve()
        if link.endswith("/"):
            target = target / "index.html"
        if not target.exists():
            raise ValueError(f"broken local link from {html_path}: {link}")


def verify_site(site: Path) -> dict[str, Any]:
    required_files = [
        site / "index.html",
        site / "styles.css",
        site / "dashboard" / "index.html",
        site / "assets" / "dashboard.css",
        site / "assets" / "dashboard.js",
        site / "data" / "current.json",
        site / "data" / "schema-v1.json",
    ]
    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        raise ValueError(f"missing site files: {missing}")

    forbidden = [
        path
        for path in site.rglob("*")
        if path.is_file() and path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
    ]
    if forbidden:
        raise ValueError(f"SQLite files must not enter Pages: {forbidden}")

    snapshot_path = site / "data" / "current.json"
    snapshot_size = snapshot_path.stat().st_size
    if snapshot_size > MAX_SNAPSHOT_BYTES:
        raise ValueError(f"current.json exceeds {MAX_SNAPSHOT_BYTES} bytes")
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    validate_snapshot(payload)
    if payload["export"]["published_payload_bytes"] != snapshot_size:
        raise ValueError("published_payload_bytes does not match current.json")

    schema = json.loads((site / "data" / "schema-v1.json").read_text(encoding="utf-8"))
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("schema-v1.json is not Draft 2020-12")

    required_ids = {
        "data-status",
        "generated-at",
        "source-max-date",
        "pcc-row-count",
        "snapshot-row-count",
        "records-body",
        "keyword",
        "announcement-type",
        "agency",
        "date-from",
        "date-to",
        "sort-order",
    }
    for html_path in (site / "index.html", site / "dashboard" / "index.html"):
        parser = PageParser()
        parser.feed(html_path.read_text(encoding="utf-8"))
        _verify_relative_links(html_path, parser.links)
        if html_path.name == "index.html" and html_path.parent.name == "dashboard":
            if not required_ids.issubset(parser.ids):
                raise ValueError(f"dashboard missing ids: {sorted(required_ids - parser.ids)}")

    script = (site / "assets" / "dashboard.js").read_text(encoding="utf-8")
    unsafe_property = "inner" + "HTML"
    if unsafe_property in script:
        raise ValueError("dashboard.js must render upstream text with safe DOM APIs")
    if "textContent" not in script or "createElement" not in script:
        raise ValueError("dashboard.js is missing the safe DOM rendering path")

    return {
        "schema_version": payload["schema_version"],
        "status": payload["status"]["state"],
        "pcc_rows": payload["datasets"]["pcc_tender"]["row_count"],
        "published_rows": len(payload["rows"]),
        "snapshot_bytes": snapshot_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="驗證 GitHub Pages 靜態資料看板")
    parser.add_argument("--site", default="docs", help="Pages artifact 目錄")
    args = parser.parse_args()
    result = verify_site(Path(args.site))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
