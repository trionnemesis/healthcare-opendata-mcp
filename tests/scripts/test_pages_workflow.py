"""Pages 部署 workflow 的形狀 —— 用純文字解析,不引入 YAML 相依。

這裡釘的不是語法,是**失敗時的行為**:順序錯了就會把未驗證的東西部署出去,
或是在同步失敗時靜默成功。這兩者都是無聲的錯誤,回歸時不會有人察覺。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOW = Path(".github/workflows/pages.yml")


@pytest.fixture(scope="module")
def text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def step_names(text: str) -> list[str]:
    # steps 的 name 固定縮排六格;environment.name 縮排不同,不會被誤抓
    return re.findall(r"^      - name: (.+)$", text, flags=re.MULTILINE)


def _index(step_names: list[str], needle: str) -> int:
    for i, name in enumerate(step_names):
        if needle in name:
            return i
    raise AssertionError(f"workflow 沒有這個步驟: {needle!r}(現有: {step_names})")


class TestFailClosed:
    def test_artifact_is_verified_before_it_is_uploaded(self, step_names):
        assert _index(step_names, "Verify deployable artifact") < _index(
            step_names, "Upload site artifact"
        )

    def test_artifact_is_verified_before_deploy(self, step_names):
        assert _index(step_names, "Verify deployable artifact") < _index(
            step_names, "Deploy to GitHub Pages"
        )

    def test_last_known_good_is_seeded_before_the_refresh(self, step_names):
        assert _index(step_names, "Seed artifact with the committed snapshot") < _index(
            step_names, "Refresh snapshot"
        )

    def test_the_verify_step_is_not_allowed_to_continue_on_error(self, text):
        """驗證步驟若可以 continue-on-error,未通過驗證的東西就會被部署出去。"""
        block = text.split("- name: Verify deployable artifact")[1].split("- name:")[0]
        assert "continue-on-error" not in block


class TestLastKnownGood:
    def test_deploys_the_staged_site_not_the_repo_docs_directory(self, text):
        upload = text.split("- name: Upload site artifact")[1].split("- name:")[0]
        assert "path: _site" in upload

    def test_refresh_failure_does_not_abort_the_deployment(self, text):
        """同步失敗仍要部署 last-known-good,否則網站會停在更舊的版本。"""
        block = text.split("- name: Refresh snapshot")[1].split("- name:")[0]
        assert "continue-on-error: true" in block

    def test_refresh_promotes_only_after_it_verifies_the_staged_site(self, text):
        block = text.split("- name: Refresh snapshot")[1].split("- name:")[0]
        verify_at = block.index("--require-non-empty")
        promote_at = block.index("cp -r \"$STAGE\" _site")
        assert verify_at < promote_at, "未驗證就取代 _site 等於可能部署空資料"

    def test_a_failed_refresh_turns_the_run_red(self, text):
        """部署 last-known-good 之後仍要失敗 —— 靜默成功等於用舊快照冒充今日資料。"""
        block = text.split("- name: Fail the run when the snapshot could not be refreshed")[1]
        assert "steps.refresh.outcome == 'failure'" in block
        assert "exit 1" in block

    def test_the_failure_step_runs_after_the_deployment(self, step_names):
        assert _index(step_names, "Fail the run when") > _index(
            step_names, "Deploy to GitHub Pages"
        )


class TestOperationalGuards:
    def test_the_job_has_a_timeout(self, text):
        assert "timeout-minutes:" in text

    def test_deployments_do_not_run_concurrently(self, text):
        assert "concurrency:" in text

    def test_the_schedule_is_not_enabled_without_a_verified_run(self, text):
        """排程仍為註解:runner 能否穩定完成官方同步尚未實測(見 #31)。"""
        active_triggers = text.split("permissions:")[0]
        assert "\n  schedule:" not in active_triggers
        assert "# schedule:" in active_triggers

    def test_permissions_stay_least_privilege(self, text):
        """workflow 不提交任何東西,不需要 contents: write。"""
        block = text.split("permissions:")[1].split("concurrency:")[0]
        assert "contents: read" in block
        assert "contents: write" not in block
