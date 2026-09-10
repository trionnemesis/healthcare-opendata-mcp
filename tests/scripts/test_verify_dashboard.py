from __future__ import annotations

import json
from pathlib import Path

import pytest

from verify_dashboard import validate_snapshot, verify_site


def test_committed_dashboard_artifact_passes_verification() -> None:
    result = verify_site(Path("docs"))
    assert result["schema_version"] == "1.0"
    assert result["snapshot_bytes"] <= 5 * 1024 * 1024


def test_malformed_snapshot_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing keys"):
        validate_snapshot({"schema_version": "1.0"})


def test_non_allowlisted_row_field_is_rejected() -> None:
    payload = json.loads(Path("docs/data/current.json").read_text(encoding="utf-8"))
    if not payload["rows"]:
        pytest.skip("committed snapshot has no row fixture")
    payload["rows"][0]["internal_path"] = "/secret/db"
    with pytest.raises(ValueError, match="allowlist"):
        validate_snapshot(payload)


def _snapshot() -> dict:
    return json.loads(Path("docs/data/current.json").read_text(encoding="utf-8"))


def _dataset(**overrides) -> dict:
    base = {
        "row_count": 3,
        "last_fetched_at": "2026-09-01T06:00:00+00:00",
        "source_url": "https://info.nhi.gov.tw/",
        "license": "政府資料開放授權條款 1.0",
    }
    base.update(overrides)
    return base


class TestOpenDatasetMatrix:
    """驗證器只驗形狀,不驗成員 —— 否則每加一個資料集都要改部署閘門。"""

    def test_unknown_dataset_key_is_accepted(self):
        payload = _snapshot()
        payload["datasets"]["nhi_hospital_district"] = _dataset()
        validate_snapshot(payload)

    def test_null_row_count_is_accepted(self):
        payload = _snapshot()
        payload["datasets"]["never_synced"] = _dataset(row_count=None)
        validate_snapshot(payload)

    def test_non_integer_row_count_is_rejected(self):
        payload = _snapshot()
        payload["datasets"]["broken"] = _dataset(row_count="3")
        with pytest.raises(ValueError, match="integer or null"):
            validate_snapshot(payload)

    def test_boolean_row_count_is_rejected(self):
        payload = _snapshot()
        payload["datasets"]["broken"] = _dataset(row_count=True)
        with pytest.raises(ValueError, match="integer or null"):
            validate_snapshot(payload)

    def test_dataset_missing_required_fields_is_rejected(self):
        payload = _snapshot()
        payload["datasets"]["thin"] = {"row_count": 1}
        with pytest.raises(ValueError, match="missing fields"):
            validate_snapshot(payload)

    def test_invalid_dataset_key_is_rejected(self):
        payload = _snapshot()
        payload["datasets"]["Bad-Key"] = _dataset()
        with pytest.raises(ValueError, match="invalid dataset key"):
            validate_snapshot(payload)

    def test_pcc_tender_is_still_mandatory(self):
        payload = _snapshot()
        payload["datasets"].pop("pcc_tender")
        with pytest.raises(ValueError, match="pcc_tender"):
            validate_snapshot(payload)

    def test_nhi_clinic_is_no_longer_mandatory(self):
        payload = _snapshot()
        payload["datasets"].pop("nhi_clinic")
        validate_snapshot(payload)


class TestRequireNonEmpty:
    """部署路徑的閘門:剛產生的空快照不得取代 last-known-good。

    empty 對契約而言是合法狀態(委入的快照走一般驗證),但在「用新快照覆蓋舊快照」
    的那一刻,空資料等於用看起來成功的東西蓋掉真實資料。
    """

    def _site(self, tmp_path, state: str):
        """複製委入的 site 並改寫快照狀態。

        以 exporter 自己的序列化器寫回,否則 published_payload_bytes 會與實際
        檔案大小不符 —— 驗證器本來就該擋下那種不一致。
        """
        import shutil

        from export_board_data import _json_text, _stabilize_size_field

        site = tmp_path / "site"
        shutil.copytree("docs", site)
        snapshot = site / "data" / "current.json"
        payload = json.loads(snapshot.read_text(encoding="utf-8"))
        payload["status"]["state"] = state
        if state == "empty":
            payload["rows"] = []
            payload["summary"]["pcc_tender"]["snapshot_row_count"] = 0
            payload["status"]["source_max_date"] = None
        _stabilize_size_field(payload, "published_payload_bytes")
        snapshot.write_text(_json_text(payload), encoding="utf-8")
        return site

    def test_empty_snapshot_is_rejected_when_gate_is_on(self, tmp_path):
        with pytest.raises(ValueError, match="last-known-good"):
            verify_site(self._site(tmp_path, "empty"), require_non_empty=True)

    def test_empty_snapshot_is_allowed_by_default(self, tmp_path):
        # 委入的快照仍可能合法地是 empty;預設路徑不得因此拒絕部署
        verify_site(self._site(tmp_path, "empty"))

    def test_non_empty_snapshot_passes_the_gate(self, tmp_path):
        verify_site(self._site(tmp_path, "fresh"), require_non_empty=True)
