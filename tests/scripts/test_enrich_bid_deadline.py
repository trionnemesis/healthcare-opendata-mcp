"""enrich_bid_deadline._candidates — 候選過濾條件(含決標排除)。

決標排除是行為修正:招標與決標是兩筆獨立 record,只看招標那筆看不出案子
已結束,故先蒐集決標的 job_number 再排除。少了它,已決標但 bid_deadline
仍空的舊案會佔用 --limit 額度,排擠仍可投標的新案。
"""
import enrich_bid_deadline as ebd
import pytest

from health_opendata_mcp.adapters.pcc_tender import PCC_TENDER_COLUMNS
from health_opendata_mcp.contracts import DatasetMeta, NormalizedBatch, Record
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository

_DATASET = "pcc-tender"
_IT_TITLE = "資訊系統維護案"


def _row(
    *,
    job: str,
    ann: str = ebd._TENDER,
    date: str = "2026-06-01",
    title: str = _IT_TITLE,
    bid_deadline: str = "",
) -> Record:
    payload = {c.name: "" for c in PCC_TENDER_COLUMNS}
    payload.update(
        {
            "date": date,
            "announcement_type": ann,
            "title": title,
            "agency": "衛生福利部",
            "job_number": job,
            "bid_deadline": bid_deadline,
        }
    )
    return Record(
        dataset_id=_DATASET,
        natural_key=f"{job}|{ann}|{date}",
        payload=payload,
    )


@pytest.fixture
async def repo(tmp_path):
    r = SqliteRepository(str(tmp_path / "test.db"))
    await r.init()
    return r


async def _seed(repo: SqliteRepository, records: list[Record]) -> None:
    dataset = DatasetMeta(
        id=_DATASET,
        source_id="pcc",
        title="PCC 標案",
        columns=PCC_TENDER_COLUMNS,
        collection="procurement",
    )
    await repo.upsert_batch(NormalizedBatch(dataset=dataset, records=tuple(records)))


def _jobs(cands: list[tuple[str, dict]]) -> list[str]:
    return [p["job_number"] for _, p in cands]


class TestAwardExclusion:
    async def test_tender_with_award_is_excluded(self, repo):
        """核心修正:同 job_number 已有決標公告 → 不再列為候選。"""
        await _seed(
            repo,
            [
                _row(job="A-1"),
                _row(job="A-1", ann=ebd._AWARD, date="2026-06-20"),
            ],
        )
        assert _jobs(await ebd._candidates(repo, "2026-01-01")) == []

    async def test_tender_without_award_is_kept(self, repo):
        await _seed(repo, [_row(job="A-1")])
        assert _jobs(await ebd._candidates(repo, "2026-01-01")) == ["A-1"]

    async def test_award_for_other_job_does_not_exclude(self, repo):
        """排除必須精確比對 job_number,不能因為存在任何決標就整批濾掉。"""
        await _seed(
            repo,
            [
                _row(job="A-1"),
                _row(job="B-2", ann=ebd._AWARD, date="2026-06-20"),
            ],
        )
        assert _jobs(await ebd._candidates(repo, "2026-01-01")) == ["A-1"]

    async def test_award_record_is_never_a_candidate(self, repo):
        await _seed(repo, [_row(job="A-1", ann=ebd._AWARD)])
        assert _jobs(await ebd._candidates(repo, "2026-01-01")) == []


class TestExistingFilters:
    """修正不得破壞原有四項條件。"""

    async def test_already_enriched_is_excluded(self, repo):
        await _seed(repo, [_row(job="A-1", bid_deadline="2026-06-16")])
        assert _jobs(await ebd._candidates(repo, "2026-01-01")) == []

    async def test_older_than_threshold_is_excluded(self, repo):
        await _seed(repo, [_row(job="A-1", date="2025-12-31")])
        assert _jobs(await ebd._candidates(repo, "2026-01-01")) == []

    async def test_non_it_title_is_excluded(self, repo):
        await _seed(repo, [_row(job="A-1", title="醫材採購案")])
        assert _jobs(await ebd._candidates(repo, "2026-01-01")) == []

    async def test_sorted_newest_first(self, repo):
        await _seed(
            repo,
            [
                _row(job="OLD", date="2026-05-01"),
                _row(job="NEW", date="2026-06-01"),
            ],
        )
        assert _jobs(await ebd._candidates(repo, "2026-01-01")) == ["NEW", "OLD"]
