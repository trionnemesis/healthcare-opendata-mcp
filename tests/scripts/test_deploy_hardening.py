"""部署設定中「tests/test_supply_chain.py 沒有覆蓋」的那幾項。

分工刻意明確,避免同一個性質被兩個檔案各斷言一次(改一次要改兩處):

  test_supply_chain.py(採自 PR #38,以 PyYAML 解析)
    - workflow:run: 內不得內插 ${{ }}、action 一律釘 commit SHA、
      每個 workflow 都要宣告 permissions
    - k8s:自有容器(hcmcp / sync)須 readOnlyRootFilesystem + 可寫 /tmp、
      drop ALL、禁提權

  本檔(純文字解析,不需 YAML)
    - FastMCP banner 的 pypi egress 已關閉(deployment 與 Dockerfile 兩處)
    - Dockerfile 不以 root 執行
    - DB 卷仍是唯讀掛載
    - pod 層 runAsNonRoot / seccomp
    - **每一個**容器(含 gcloud / kubectl 第三方容器)都 drop ALL 且禁提權
      —— test_supply_chain 只檢查自有容器,這裡守住第三方容器不被漏掉

FASTMCP_SHOW_SERVER_BANNER 的驗證基礎(2026-11,本機實測,非推測):
  - 以 audit hook 記錄 hcmcp server 啟動 + 一次 /healthz 期間的所有寫入,
    唯一落在 root filesystem 的是 $HOME/.local/share/fastmcp/version_cache.json
  - 該寫入來自 FastMCP 的啟動 banner(會向 pypi.org 查版本);
    清掉快取後啟動即重新產生,設 FASTMCP_SHOW_SERVER_BANNER=false 後不再產生,
    /healthz 仍回 200
  - 把 $HOME 設為不可寫再啟動,server 一樣正常服務(fastmcp 吞掉 OSError)
  未在真實 K8s 叢集驗證(本環境無叢集,也無 docker daemon)。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DEPLOYMENT = Path("deploy/k8s/deployment.yaml")
CRONJOB = Path("deploy/k8s/cronjob-sync.yaml")
DOCKERFILE = Path("Dockerfile")


def _server_container_block(text: str) -> str:
    """取 `containers:` 下 `- name: hcmcp` 這個容器的 block 文字。"""
    lines = text.splitlines()
    start = None
    indent = 0
    for i, line in enumerate(lines):
        if re.match(r"^\s*- name: hcmcp\s*$", line):
            start = i
            indent = len(line) - len(line.lstrip(" "))
            break
    assert start is not None, "deployment.yaml 找不到 hcmcp 容器,測試會假性通過"
    out = [lines[start]]
    for line in lines[start + 1 :]:
        if line.strip() and (len(line) - len(line.lstrip(" "))) <= indent:
            break
        out.append(line)
    return "\n".join(out)


class TestFastMcpBannerEgressDisabled:
    """banner 會向 pypi.org 查版本 —— 正式 pod 不該有這個 egress。"""

    def test_deployment_sets_the_flag(self):
        block = _server_container_block(DEPLOYMENT.read_text(encoding="utf-8"))
        assert "FASTMCP_SHOW_SERVER_BANNER" in block
        assert 'value: "false"' in block

    def test_image_disables_it_by_default(self):
        assert "FASTMCP_SHOW_SERVER_BANNER=false" in DOCKERFILE.read_text(
            encoding="utf-8"
        )


class TestServerContainerExtras:
    def test_db_volume_is_mounted_read_only(self):
        block = _server_container_block(DEPLOYMENT.read_text(encoding="utf-8"))
        assert "readOnly: true" in block


class TestImageDefaults:
    def test_image_does_not_run_as_root(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert re.search(r"^USER 10001\s*$", text, re.MULTILINE)


class TestPodLevelInvariants:
    @pytest.mark.parametrize("path", [DEPLOYMENT, CRONJOB], ids=lambda p: p.name)
    def test_pods_run_as_non_root_with_seccomp(self, path: Path):
        text = path.read_text(encoding="utf-8")
        assert "runAsNonRoot: true" in text
        assert "type: RuntimeDefault" in text

    @pytest.mark.parametrize("path", [DEPLOYMENT, CRONJOB], ids=lambda p: p.name)
    def test_every_container_drops_all_capabilities(self, path: Path):
        """含 gcloud / kubectl 第三方容器 —— test_supply_chain 只看自有容器。"""
        text = path.read_text(encoding="utf-8")
        containers = len(re.findall(r"^\s*- name: \S+\n\s+image: ", text, re.MULTILINE))
        assert containers > 0, "找不到任何容器,測試會假性通過"
        assert text.count("allowPrivilegeEscalation: false") == containers
        assert text.count('drop: ["ALL"]') == containers
