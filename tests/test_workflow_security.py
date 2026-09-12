"""安全回歸測試 — GitHub Actions workflow 的注入面。

不變量:不可信的 workflow context(`inputs.*`、`github.event.*` 等)
永遠不得直接展開進 `run:`。`${{ }}` 是在 shell 執行「之前」由 Actions
做文字替換,故自由文字輸入等同直接拼進腳本(CWE-94 / CWE-78)。

正確作法:經 `env:` 傳遞,於 shell 內以加引號的變數引用。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# 由外部可控來源填入的 context;展開進 run: 即為注入點。
# (matrix.* / secrets.* / env.* 為維護者自行定義,不在此列。)
_UNTRUSTED_CONTEXTS = (
    "inputs.",
    "github.event",
    "github.head_ref",
)

_RUN_KEY = re.compile(r"^\s*(?:-\s+)?run:(.*)$")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _run_block_bodies(text: str) -> list[str]:
    """抽出每個 `run:` 區塊的腳本內容(以縮排判定區塊邊界)。

    同時涵蓋兩種寫法:`run: cmd`(單行)與 `run: |`(區塊純量)。
    """
    lines = text.splitlines()
    bodies: list[str] = []
    i = 0
    while i < len(lines):
        match = _RUN_KEY.match(lines[i])
        if match is None:
            i += 1
            continue

        inline = match.group(1).strip()
        indent = _indent_of(lines[i])
        i += 1

        # `run: |` / `run: >` → 內容在後續縮排更深的行;否則為單行指令
        if inline and inline not in ("|", ">", "|-", ">-"):
            bodies.append(inline)
            continue

        body: list[str] = []
        while i < len(lines):
            nxt = lines[i]
            if nxt.strip() and _indent_of(nxt) <= indent:
                break
            body.append(nxt)
            i += 1
        bodies.append("\n".join(body))
    return bodies


def _workflow_files() -> list[Path]:
    return sorted(_WORKFLOW_DIR.glob("*.yml")) + sorted(_WORKFLOW_DIR.glob("*.yaml"))


def test_workflow_dir_is_discovered() -> None:
    """測試本身的前提:找得到 workflow,否則以下檢查會空轉而假性通過。"""
    assert _workflow_files(), f"no workflow files under {_WORKFLOW_DIR}"


@pytest.mark.parametrize("workflow", _workflow_files(), ids=lambda p: p.name)
def test_no_untrusted_context_interpolated_into_run(workflow: Path) -> None:
    """`run:` 內不得出現 `${{ inputs.* }}` 之類的直接展開。"""
    offenders: list[str] = []
    for body in _run_block_bodies(workflow.read_text(encoding="utf-8")):
        for expansion in re.findall(r"\$\{\{(.*?)\}\}", body, flags=re.DOTALL):
            if any(ctx in expansion for ctx in _UNTRUSTED_CONTEXTS):
                offenders.append(expansion.strip())

    assert not offenders, (
        f"{workflow.name}: 不可信 context 被直接展開進 run:「{offenders}」。"
        " 請改以 env: 傳遞,並在 shell 內以加引號的變數引用。"
    )


class TestRunBlockExtraction:
    """抽取邏輯自身的測試 — 否則上面的斷言可能因解析失誤而假性通過。"""

    def test_detects_block_scalar_body(self) -> None:
        text = "jobs:\n  a:\n    steps:\n      - run: |\n          echo ${{ inputs.x }}\n      - uses: foo\n"
        assert "inputs.x" in "\n".join(_run_block_bodies(text))

    def test_detects_inline_run(self) -> None:
        text = "jobs:\n  a:\n    steps:\n      - run: echo ${{ inputs.x }}\n"
        assert "inputs.x" in "\n".join(_run_block_bodies(text))

    def test_does_not_capture_with_or_env_blocks(self) -> None:
        """`with:` / `env:` 的展開是安全用法,不應被誤判為 run: 內容。"""
        text = (
            "jobs:\n  a:\n    steps:\n"
            "      - uses: foo\n        with:\n          v: ${{ inputs.x }}\n"
            "      - run: echo safe\n"
        )
        assert "inputs.x" not in "\n".join(_run_block_bodies(text))

    def test_env_passthrough_pattern_is_accepted(self) -> None:
        """修正後的寫法:env: 展開 + shell 內引用變數 → run: body 不含展開。"""
        text = (
            "jobs:\n  a:\n    steps:\n"
            "      - name: probe\n        env:\n          ONLY: ${{ inputs.only }}\n"
            '        run: |\n          python x.py --only "$ONLY"\n'
        )
        bodies = "\n".join(_run_block_bodies(text))
        assert "inputs.only" not in bodies
        assert "$ONLY" in bodies
