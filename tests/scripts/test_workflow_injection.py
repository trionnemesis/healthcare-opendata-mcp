"""Workflow 不得把 ${{ }} 內插進 run: —— 以純文字解析,不引入 YAML 相依。

釘的是**注入面**而非語法:workflow_dispatch 的 inputs 是自由文字,直接內插
進 run: 等於把它接到 runner 的 shell 上(CWE-78),可取得 GITHUB_TOKEN 與該
job 的全部環境。正確寫法是經 env: 傳入再於 shell 內加引號展開。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(".github/workflows")

# run: 之後、直到下一個同層 step 鍵為止,都算 run 區塊內容
_STEP_KEY = re.compile(
    r"\s*(- name:|name:|env:|uses:|with:|if:|id:|shell:|working-directory:"
    r"|timeout-minutes:|continue-on-error:)"
)
_RUN_START = re.compile(r"\s*run:\s*[|>]?-?\s*$|\s*run:\s+\S")


def _run_block_lines(text: str) -> list[tuple[int, str]]:
    """回傳所有位於 run: 區塊內的 (行號, 內容)。"""
    out: list[tuple[int, str]] = []
    in_run = False
    for n, line in enumerate(text.splitlines(), start=1):
        if _RUN_START.match(line):
            in_run = True
            continue
        if in_run and _STEP_KEY.match(line):
            in_run = False
        if in_run and line.strip():
            out.append((n, line))
    return out


def _workflows() -> list[Path]:
    files = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))
    assert files, "找不到任何 workflow,測試會假性通過"
    return files


@pytest.mark.parametrize("wf", _workflows(), ids=lambda p: p.name)
def test_no_expression_interpolated_into_run(wf: Path) -> None:
    text = wf.read_text(encoding="utf-8")
    offenders = [
        f"{wf.name}:{n}: {line.strip()}"
        for n, line in _run_block_lines(text)
        if "${{" in line
    ]
    assert not offenders, (
        "run: 區塊內出現 ${{ }} 內插,請改以 env: 傳入並在 shell 內加引號:\n"
        + "\n".join(offenders)
    )


def test_verify_catalog_passes_inputs_through_env() -> None:
    """verify-catalog 的兩個 input 必須走 env,且在 shell 內以引號展開。"""
    text = (WORKFLOW_DIR / "verify-catalog.yml").read_text(encoding="utf-8")
    assert "ONLY: ${{ inputs.only }}" in text
    assert "ENABLED_ONLY: ${{ inputs.enabled_only }}" in text
    # 引號展開:未加引號的 $ONLY 會重新引入分詞/glob
    assert 'args+=(--only "$ONLY")' in text
