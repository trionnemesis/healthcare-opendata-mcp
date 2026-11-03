"""部署設定的安全回歸測試 —— 以純文字解析,不引入 YAML 相依。

與 test_workflow_injection.py 同一風格:釘的是設定的**安全性質**,
而不是 YAML 語法。這些性質一旦被後續編輯悄悄拿掉,測試要紅。

驗證基礎(2026-11,本機實測,非推測):
  - 以 audit hook 記錄 hcmcp server 啟動 + 一次 /healthz 期間的所有寫入,
    唯一落在 root filesystem 的是 $HOME/.local/share/fastmcp/version_cache.json
  - 該寫入來自 FastMCP 的啟動 banner(會向 pypi.org 查版本);
    FASTMCP_SHOW_SERVER_BANNER=false 之後不再發生,/healthz 仍回 200
  - 把 $HOME 設為不可寫再啟動,server 一樣正常服務(fastmcp 吞掉 OSError)
  未在真實 K8s 叢集驗證(本環境無叢集,也無法連外)。
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


class TestServerContainerHardening:
    def test_root_filesystem_is_read_only(self):
        block = _server_container_block(DEPLOYMENT.read_text(encoding="utf-8"))
        assert "readOnlyRootFilesystem: true" in block

    def test_writable_tmp_is_mounted(self):
        """唯讀 rootfs 必須配一個可寫 /tmp,否則 Python tempfile 會炸。"""
        block = _server_container_block(DEPLOYMENT.read_text(encoding="utf-8"))
        assert "mountPath: /tmp" in block
        assert "- name: tmp" in DEPLOYMENT.read_text(encoding="utf-8")

    def test_fastmcp_banner_egress_is_disabled(self):
        """banner 會向 pypi.org 查版本 —— 正式 pod 不應有這個 egress。"""
        block = _server_container_block(DEPLOYMENT.read_text(encoding="utf-8"))
        assert "FASTMCP_SHOW_SERVER_BANNER" in block
        assert 'value: "false"' in block

    def test_privilege_escalation_and_capabilities_still_locked(self):
        """既有護欄不得在後續編輯中退場。"""
        block = _server_container_block(DEPLOYMENT.read_text(encoding="utf-8"))
        assert "allowPrivilegeEscalation: false" in block
        assert 'drop: ["ALL"]' in block

    def test_db_volume_is_mounted_read_only(self):
        block = _server_container_block(DEPLOYMENT.read_text(encoding="utf-8"))
        assert "readOnly: true" in block


class TestImageDefaults:
    def test_image_disables_banner_by_default(self):
        assert "FASTMCP_SHOW_SERVER_BANNER=false" in DOCKERFILE.read_text(
            encoding="utf-8"
        )

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
        text = path.read_text(encoding="utf-8")
        containers = len(re.findall(r"^\s*- name: \S+\n\s+image: ", text, re.MULTILINE))
        assert containers > 0, "找不到任何容器,測試會假性通過"
        assert text.count("allowPrivilegeEscalation: false") == containers
        assert text.count('drop: ["ALL"]') == containers
