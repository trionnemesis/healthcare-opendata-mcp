"""Classification helpers for NHI healthcare facility dataset.

The rule set is intentionally narrow and auditable:
- use exact MOHW official facility name matching for `mohw` ownership
- infer `local_government` by eligible ownership buckets
- keep non-matching rows in `unknown` state rather than guess.
"""
from __future__ import annotations

from dataclasses import dataclass


MOHW_AFFILIATED_FACILITY_NAMES = frozenset({
    "衛生福利部基隆醫院",
    "衛生福利部臺北醫院",
    "衛生福利部八里療養院",
    "衛生福利部樂生醫院",
    "衛生福利部桃園醫院",
    "衛生福利部桃園醫院新屋分院",
    "衛生福利部桃園療養院",
    "衛生福利部苗栗醫院",
    "衛生福利部豐原醫院",
    "衛生福利部臺中醫院",
    "衛生福利部彰化醫院",
    "衛生福利部南投醫院",
    "衛生福利部草屯療養院",
    "衛生福利部嘉義醫院",
    "衛生福利部朴子醫院",
    "衛生福利部新營醫院",
    "衛生福利部臺南醫院",
    "衛生福利部臺南醫院新化分院",
    "衛生福利部胸腔病院",
    "衛生福利部嘉南療養院",
    "衛生福利部旗山醫院",
    "衛生福利部屏東醫院",
    "衛生福利部恆春旅遊醫院",
    "衛生福利部澎湖醫院",
    "衛生福利部花蓮醫院",
    "衛生福利部花蓮醫院豐濱原住民分院",
    "衛生福利部玉里醫院",
    "衛生福利部臺東醫院",
    "衛生福利部臺東醫院成功分院",
    "衛生福利部金門醫院",
})

# 來源: 衛生福利部所屬醫療機構及社福機構；名稱以 NHI 統一院所名冊的
# 醫事機構名稱（含分院）建立 exact-name mapping，不做前綴或模糊推論。
# https://www.mohw.gov.tw/cp-7432-86064-1.html


@dataclass(frozen=True)
class FacilityClassification:
    facility_type: str
    governing_level: str
    classification_source: str
    is_active: bool


def _is_mohw_affiliated(name: str) -> bool:
    return name.strip() in MOHW_AFFILIATED_FACILITY_NAMES


def classify_healthcare_facility(payload: dict[str, str]) -> FacilityClassification | None:
    """依需求範圍產出衍生欄位；不符合 scope 則回傳 None."""
    authority = (payload.get("權屬別名稱") or "").strip()
    contract_type = (payload.get("特約類別") or "").strip()
    institution_name = (payload.get("醫事機構名稱") or "").strip()
    end_date = (payload.get("終止合約或歇業日期") or "").strip()
    is_active = not bool(end_date)

    if authority == "衛生所" and contract_type == "4":
        return FacilityClassification(
            facility_type="health_center",
            governing_level="local_government",
            classification_source="authority_rule:health_center",
            is_active=is_active,
        )

    if contract_type in {"1", "2", "3"} and authority in {
        "部立及直轄市立醫院",
        "縣市立醫院",
    }:
        if _is_mohw_affiliated(institution_name):
            return FacilityClassification(
                facility_type="hospital",
                governing_level="mohw",
                classification_source="official_mapping:MOHW_AFFILIATED_FACILITY_NAMES",
                is_active=is_active,
            )
        return FacilityClassification(
            facility_type="hospital",
            governing_level="local_government",
            classification_source="authority_rule:government_local",
            is_active=is_active,
        )

    if contract_type == "4":
        return FacilityClassification(
            facility_type="clinic",
            governing_level="unknown",
            classification_source="contract_type_rule:4",
            is_active=is_active,
        )

    return None
