"""main.py fetch 对「部分成功」与「全失败」的区分。

两条 gonggao 分支都要覆盖：命中档案走 run_gonggao_for_vehicle，
未命中走 run_jianmian_fallback。档案一律用 tmp_path 显式指定，
避免依赖仓库外的 data/vehicle_profiles.json 导致本地与 CI 行为不一致。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import main as main_module  # noqa: E402
from miit_gonggao import core as gonggao_core  # noqa: E402


def _profile_file(tmp_path, *, with_conditions: bool) -> Path:
    path = tmp_path / "profiles.json"
    entry = {"name": "示例车型", "autohome": {"models": ["示例车型"]}}
    if with_conditions:
        entry["gonggao"] = {"trademark": "示例牌", "model_prefixes": ["ABC6520M"]}
    path.write_text(json.dumps([entry], ensure_ascii=False), encoding="utf-8")
    return path


def _run(tmp_path, monkeypatch, *, exit_code: int, with_conditions: bool):
    seen: dict[str, list[str]] = {}

    def fake_main(argv):
        seen["argv"] = argv
        return exit_code

    monkeypatch.setattr(main_module.gonggao_core, "main", fake_main)
    code = main_module.command_fetch(
        [
            "示例车型",
            "--source",
            "gonggao",
            "--mapping-file",
            str(_profile_file(tmp_path, with_conditions=with_conditions)),
            "--gonggao-output-dir",
            str(tmp_path),
        ]
    )
    return code, seen.get("argv", [])


@pytest.mark.parametrize("with_conditions", [True, False])
def test_fetch_treats_partial_as_success(tmp_path, monkeypatch, capsys, with_conditions):
    """12 份里 1 份非 PDF 不该把整个车型报成失败。"""
    code, argv = _run(
        tmp_path,
        monkeypatch,
        exit_code=gonggao_core.EXIT_DOWNLOAD_PARTIAL,
        with_conditions=with_conditions,
    )
    out = capsys.readouterr().out
    # 确认命中了预期分支
    assert (argv[0] == "query") is with_conditions
    assert code == 0
    assert "部分成功" in out
    assert "任务失败" not in out


@pytest.mark.parametrize("with_conditions", [True, False])
def test_fetch_reports_total_failure(tmp_path, monkeypatch, capsys, with_conditions):
    code, _ = _run(
        tmp_path,
        monkeypatch,
        exit_code=gonggao_core.EXIT_DOWNLOAD_FAILED,
        with_conditions=with_conditions,
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "任务失败" in out
    assert "部分成功" not in out


@pytest.mark.parametrize("with_conditions", [True, False])
def test_fetch_all_success(tmp_path, monkeypatch, capsys, with_conditions):
    code, _ = _run(
        tmp_path, monkeypatch, exit_code=gonggao_core.EXIT_OK, with_conditions=with_conditions
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "全部任务完成" in out
