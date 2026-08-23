"""汽车之家配置表导出命名测试（离线）。"""

from __future__ import annotations

from datetime import datetime

from autohome_cc.utils.cleaners import build_export_filename, ensure_unique_path, safe_filename_part

WHEN = datetime(2026, 7, 28, 22, 7, 9)


def test_single_model_filename():
    name = build_export_filename(["示例车型A"], prefix="汽车之家", when=WHEN)
    assert name == "汽车之家_示例车型A配置表_20260728.xlsx"


def test_multi_model_filename_uses_first_model_and_count():
    name = build_export_filename(["示例车型A", "示例车型B", "示例车型C"], prefix="汽车之家", when=WHEN)
    assert name == "汽车之家_示例车型A等3个车型配置表_20260728.xlsx"


def test_filename_without_model_names():
    assert build_export_filename([], prefix="汽车之家", when=WHEN) == "汽车之家_配置表_20260728.xlsx"


def test_filename_strips_path_separators_and_spaces():
    name = build_export_filename(["示例品牌X8 尊享/PHEV"], prefix="汽车之家", when=WHEN)
    assert name == "汽车之家_示例品牌X8尊享PHEV配置表_20260728.xlsx"


def test_safe_filename_part_drops_reserved_characters():
    assert safe_filename_part(' 示例车型M9:纯电*版 ') == "示例车型M9纯电版"


def test_ensure_unique_path_appends_index(tmp_path):
    first = tmp_path / "汽车之家_示例车型A配置表_20260728.xlsx"
    assert ensure_unique_path(first) == first

    first.write_text("stub")
    second = ensure_unique_path(first)
    assert second.name == "汽车之家_示例车型A配置表_20260728_2.xlsx"

    second.write_text("stub")
    third = ensure_unique_path(first)
    assert third.name == "汽车之家_示例车型A配置表_20260728_3.xlsx"
