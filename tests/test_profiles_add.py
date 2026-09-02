"""统一车型档案写入：条目组装的保守规则、精确匹配 upsert、原子写盘与格式保持。

全部离线：反查网络调用留在 main.command_profiles_add 里，这里只测可确定性验证的纯逻辑。
"""

from __future__ import annotations

import json

from miit_gonggao import core


def _rows(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"cpsb": trademark, "clmc": name} for trademark, name in pairs]


def test_build_profile_entry_single_trademark():
    entry, notes = core.build_profile_entry(
        "示例车型",
        model_prefixes=["ABC6520M"],
        resolved_rows=_rows(("示例牌", "多用途乘用车"), ("示例牌", "多用途乘用车")),
    )
    assert entry["name"] == "示例车型"
    assert entry["aliases"] == ["示例车型"]
    assert entry["gonggao"]["trademark"] == "示例牌"
    assert entry["gonggao"]["filters"] == {"clmc": "多用途乘用车"}
    assert entry["gonggao"]["model_prefixes"] == ["ABC6520M"]
    # 不猜同名异车，该键根本不写入
    assert "exclude_model_prefixes" not in entry["gonggao"]
    assert any("autohome.models" in note for note in notes)


def test_build_profile_entry_leaves_ambiguous_fields_blank():
    """商标或车辆名称不唯一时留空并点名，不能挑一个写进去。"""
    entry, notes = core.build_profile_entry(
        "示例车型",
        model_prefixes=["ABC6520M"],
        resolved_rows=_rows(("甲牌", "多用途乘用车"), ("乙牌", "轿车")),
    )
    assert entry["gonggao"]["trademark"] == ""
    assert "filters" not in entry["gonggao"]
    assert any("公告商标未写入" in note for note in notes)
    assert any("车辆名称不唯一" in note for note in notes)


def test_build_profile_entry_without_resolved_rows():
    entry, notes = core.build_profile_entry("示例车型", model_prefixes=[], resolved_rows=[])
    assert entry["gonggao"]["trademark"] == ""
    assert entry["gonggao"]["model_prefixes"] == []
    assert any("model_prefixes" in note for note in notes)


def test_build_profile_entry_aliases_deduped_and_name_first():
    entry, _ = core.build_profile_entry(
        "示例车型",
        aliases=["  示例车型 ", "example", "example", ""],
        model_prefixes=["ABC1", "ABC1"],
    )
    assert entry["aliases"] == ["示例车型", "example"]
    assert entry["gonggao"]["model_prefixes"] == ["ABC1"]


def test_upsert_mapping_exact_match_only():
    """「豹5」不该因为子串关系覆盖「豹5智驾版」——精确匹配才是 upsert 的语义。"""
    existing = [{"name": "示例车型智驾版", "aliases": ["示例车型智驾版"]}]
    entry = {"name": "示例车型", "aliases": ["示例车型"]}
    result, action = core.upsert_mapping(existing, entry)
    assert action == "added"
    assert len(result) == 2


def test_upsert_mapping_skip_and_overwrite():
    existing = [{"name": "示例车型", "aliases": ["示例车型", "example"], "gonggao": {"trademark": "旧牌"}}]
    entry = {"name": "示例车型", "aliases": ["示例车型"], "gonggao": {"trademark": "新牌"}}

    result, action = core.upsert_mapping(existing, entry)
    assert action == "skipped"
    assert result[0]["gonggao"]["trademark"] == "旧牌"

    result, action = core.upsert_mapping(existing, entry, overwrite=True)
    assert action == "updated"
    assert result[0]["gonggao"]["trademark"] == "新牌"
    assert len(result) == 1
    # 原列表不被就地改写
    assert existing[0]["gonggao"]["trademark"] == "旧牌"


def test_mapping_exists_matches_upsert_semantics():
    """短路检查必须和 upsert 判定一致，否则会出现「说跳过却又写进去」。"""
    existing = [{"name": "示例车型", "aliases": ["示例车型", "example"]}]
    assert core.mapping_exists(existing, "示例车型")
    assert core.mapping_exists(existing, "别的名字", ["EXAMPLE"])
    assert not core.mapping_exists(existing, "示例车型智驾版")
    assert not core.mapping_exists([], "示例车型")


def test_upsert_mapping_matches_by_alias():
    existing = [{"name": "示例车型", "aliases": ["示例车型", "example"]}]
    entry = {"name": "另一个名字", "aliases": ["另一个名字", "EXAMPLE"]}
    _, action = core.upsert_mapping(existing, entry)
    assert action == "skipped"


def test_save_mappings_roundtrip_and_format(tmp_path):
    path = tmp_path / "profiles.json"
    entry, _ = core.build_profile_entry(
        "示例车型",
        model_prefixes=["ABC6520M"],
        resolved_rows=_rows(("示例牌", "多用途乘用车")),
    )
    core.save_mappings(path, [entry])

    text = path.read_text(encoding="utf-8")
    assert "示例牌" in text  # 中文不转义为 \uXXXX
    assert not text.endswith("\n")  # 沿用既有档案文件的无末尾换行风格
    assert json.loads(text) == [entry]

    # 存回同一份内容应字节不变，避免无意义的 git diff
    core.save_mappings(path, json.loads(text))
    assert path.read_text(encoding="utf-8") == text

    # 写入的档案能被现有加载器读回
    loaded = core.load_mappings(path)
    assert loaded[0].trademark == "示例牌"
    assert loaded[0].model_prefixes == ["ABC6520M"]


def test_save_mappings_leaves_no_temp_file(tmp_path):
    path = tmp_path / "profiles.json"
    core.save_mappings(path, [{"name": "示例车型", "aliases": []}])
    assert [p.name for p in tmp_path.iterdir()] == ["profiles.json"]


def test_load_raw_mappings_missing_file(tmp_path):
    assert core.load_raw_mappings(tmp_path / "absent.json") == []


def test_save_mappings_keeps_existing_file_mode(tmp_path):
    """原子写盘不得收紧档案权限：NamedTemporaryFile 建的是 0600，直接 replace 会带过去。"""
    path = tmp_path / "profiles.json"
    path.write_text("[]", encoding="utf-8")
    path.chmod(0o644)

    core.save_mappings(path, [{"name": "示例车型", "aliases": []}])
    assert path.stat().st_mode & 0o777 == 0o644

    # 新建文件用 0644 而不是 0600
    fresh = tmp_path / "new.json"
    core.save_mappings(fresh, [{"name": "另一个", "aliases": []}])
    assert fresh.stat().st_mode & 0o777 == 0o644
