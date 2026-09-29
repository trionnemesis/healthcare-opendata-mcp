"""CI/CD 與部署設定的安全回歸測試。

守三件修正過的事,避免日後在不經意的編輯中回退:
  1. GitHub Actions expression injection(CWE-78):inputs/外部文字不得插進 run:。
  2. 第三方 action 未釘 commit SHA(CWE-1357):可變 tag 會被上游重新指向。
  3. 容器缺 readOnlyRootFilesystem(CWE-732):本專案兩個自有容器已驗證可唯讀。

純靜態解析,不需叢集或網路。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))

# run: 內出現 ${{ }} 即為注入風險:取代發生在 shell 執行之前,
# 輸入中的 shell 元字元會被當成指令。安全做法是經 env: 傳入再引用 "$VAR"。
_EXPR = re.compile(r"\$\{\{")
# 允許清單:純數字/布林的 matrix 與 github context 無法帶入 shell 元字元。
_INERT_EXPR = re.compile(
    r"\$\{\{\s*(matrix\.python-version|github\.(sha|ref_name|run_id|run_number))\s*\}\}"
)
_USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)", re.MULTILINE)
_PINNED = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def _steps(workflow: dict):
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            yield job, step


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_expression_interpolation_in_run_blocks(path: Path):
    """inputs / 外部文字不得以 ${{ }} 插入 run:(CWE-78)。"""
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    offenders = []
    for _job, step in _steps(workflow):
        script = step.get("run")
        if not script:
            continue
        residual = _INERT_EXPR.sub("", script)
        if _EXPR.search(residual):
            offenders.append((step.get("name", "<unnamed>"), residual))
    assert not offenders, (
        f"{path.name}: run: 內含 ${{{{ }}}} 取代,請改以 env: 傳入並用 \"$VAR\" 引用 → "
        + "; ".join(name for name, _ in offenders)
    )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_third_party_actions_are_pinned_to_commit_sha(path: Path):
    """所有 uses: 必須釘 40 字元 commit SHA(CWE-1357)。"""
    unpinned = [
        ref
        for ref in _USES.findall(path.read_text(encoding="utf-8"))
        # ./ 開頭是同 repo 內的本地 action,不受上游可變 tag 影響
        if not ref.startswith("./") and not _PINNED.match(ref)
    ]
    assert not unpinned, f"{path.name}: 未釘 SHA 的 action → {unpinned}"


def test_workflows_declare_least_privilege_permissions():
    """每個 workflow 都要顯式宣告 permissions,不繼承 repo 預設(CWE-250)。"""
    missing = [
        p.name
        for p in WORKFLOWS
        if "permissions" not in (yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    ]
    assert not missing, f"缺 top-level permissions 的 workflow → {missing}"


# ── 部署清單 ────────────────────────────────────────────────────────────────

def _pod_spec(doc: dict) -> dict | None:
    if doc.get("kind") == "Deployment":
        return doc["spec"]["template"]["spec"]
    if doc.get("kind") == "CronJob":
        return doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return None


# 本專案自有映像的容器 —— 已在唯讀根檔案系統下實測可正常啟動/寫 /data
_OWN_CONTAINERS = {"hcmcp", "sync"}


def _own_containers():
    for path in sorted((REPO / "deploy" / "k8s").glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            spec = _pod_spec(doc or {})
            if not spec:
                continue
            for key in ("initContainers", "containers"):
                for container in spec.get(key) or []:
                    if container["name"] in _OWN_CONTAINERS:
                        yield path.name, container, spec


def test_own_containers_use_read_only_root_filesystem():
    """自有容器須設 readOnlyRootFilesystem(CWE-732)。"""
    bad = [
        (f, c["name"])
        for f, c, _ in _own_containers()
        if c.get("securityContext", {}).get("readOnlyRootFilesystem") is not True
    ]
    assert not bad, f"缺 readOnlyRootFilesystem → {bad}"


def test_read_only_containers_have_a_writable_tmp():
    """唯讀根檔案系統下必須另給可寫 /tmp,否則執行期暫存會失敗。"""
    for fname, container, spec in _own_containers():
        mounts = {m["name"]: m["mountPath"] for m in container.get("volumeMounts") or []}
        assert "/tmp" in mounts.values(), f"{fname}:{container['name']} 缺可寫 /tmp"
        declared = {v["name"] for v in spec.get("volumes") or []}
        assert set(mounts) <= declared, (
            f"{fname}:{container['name']} 掛載了未宣告的 volume "
            f"{set(mounts) - declared}"
        )


def test_own_containers_drop_all_capabilities_and_block_privilege_escalation():
    """既有的最小權限設定不得被本次或後續修改弄丟。"""
    for fname, container, _ in _own_containers():
        sc = container.get("securityContext", {})
        assert sc.get("allowPrivilegeEscalation") is False, f"{fname}:{container['name']}"
        assert sc.get("capabilities", {}).get("drop") == ["ALL"], f"{fname}:{container['name']}"
