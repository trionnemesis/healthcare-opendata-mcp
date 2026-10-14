"""Workflow 不得把 ${{ }} 內插進 run: —— 以純文字解析,不引入 YAML 相依。

釘的是**注入面**而非語法:workflow_dispatch 的 inputs 是自由文字,直接內插
進 run: 等於把它接到 runner 的 shell 上(CWE-78),可取得 GITHUB_TOKEN 與該
job 的全部環境。正確寫法是經 env: 傳入再於 shell 內加引號展開。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(".github/workflows")

# 依 YAML 縮排區分 step 鍵與 shell 內容;支援一般 block-style workflow。
_RUN_START = re.compile(r"^ *(?:- +)?run:\s*(.*)$")


def _run_block_lines(text: str) -> list[tuple[int, str]]:
    """回傳 run scalar 的 (行號, 內容),包含單行與 - run: 寫法。"""
    out: list[tuple[int, str]] = []
    run_indent: int | None = None
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if run_indent is not None and indent > run_indent:
            out.append((n, line))
            continue
        run_indent = None
        match = _RUN_START.match(line)
        if match:
            run_indent = line.index("run:")
            value = match.group(1)
            if value and not value.startswith(("|", ">", "#")):
                out.append((n, value))
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


@pytest.mark.parametrize(
    ("text", "offending_lines"),
    [
        ('steps:\n  - name: inline\n    run: echo "${{ inputs.only }}"\n', [3]),
        ('steps:\n  - run: echo "${{ inputs.only }}"\n', [2]),
        ('steps:\n  - run: >-\n      echo "${{ inputs.only }}"\n', [3]),
        ('steps:\n  - run: |+\n      echo "${{ inputs.only }}"\n', [3]),
        ('steps:\n  - run: echo "$ONLY"\n    env:\n      ONLY: ${{ inputs.only }}\n', []),
        (
            "steps:\n  - name: heredoc\n    run: |\n      cat <<'EOF'\n"
            "      env:\n        VALUE: ${{ inputs.only }}\n      EOF\n",
            [6],
        ),
    ],
    ids=["inline", "list-inline", "folded", "keep-newline", "safe-env", "heredoc"],
)
def test_run_block_extraction(text: str, offending_lines: list[int]) -> None:
    assert [n for n, line in _run_block_lines(text) if "${{" in line] == offending_lines


@pytest.mark.skipif(shutil.which("bash") is None, reason="Workflow uses the Ubuntu Bash shell")
@pytest.mark.parametrize("enabled_only", ["false", "true"])
@pytest.mark.parametrize(
    "only",
    ["", "nhi-drug-price", "x; touch INJECTION_CANARY; #",
     "$(touch INJECTION_CANARY)", "`touch INJECTION_CANARY`", 'space * "quote"\nline'],
)
def test_verify_catalog_shell_preserves_argv(
    tmp_path: Path, only: str, enabled_only: str,
) -> None:
    """執行實際 workflow shell,但以 argv recorder 取代探測程式,不連外。"""
    text = (WORKFLOW_DIR / "verify-catalog.yml").read_text(encoding="utf-8")
    probe = text.split("- name: Probe catalog sources\n", 1)[1]
    script = textwrap.dedent("\n".join(line for _, line in _run_block_lines(probe)))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "verify_catalog_sources.py").write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", os.defpath),
             "ONLY": only, "ENABLED_ONLY": enabled_only},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    expected = ["--only", only] if only else []
    if enabled_only == "true":
        expected.append("--enabled-only")
    assert json.loads(result.stdout) == expected
    assert not (tmp_path / "INJECTION_CANARY").exists()
