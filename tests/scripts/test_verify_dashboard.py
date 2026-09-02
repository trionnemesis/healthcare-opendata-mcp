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
