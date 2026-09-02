"""miit_gonggao.core 测试：统一档案加载、筛选逻辑、HTTP 重试。"""

from __future__ import annotations

import argparse
import json

import pytest

from miit_gonggao import core


def test_load_mappings_nested_and_flat(tmp_path):
    mapping_file = tmp_path / "profiles.json"
    mapping_file.write_text(
        json.dumps(
            [
                {
                    "name": "示例车型",
                    "aliases": ["denza d9"],
                    "autohome": {"models": ["示例车型"]},
                    "gonggao": {
                        "trademark": "示例牌",
                        "filters": {"clmc": "多用途乘用车"},
                        "model_prefixes": ["ABC6520M"],
                        "exclude_model_prefixes": ["ABC649"],
                    },
                },
                {
                    "name": "旧版条目",
                    "trademark": "旧版牌",
                    "model_prefixes": ["OLD653"],
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    profiles = core.load_mappings(mapping_file)
    nested, flat = profiles
    assert nested.trademark == "示例牌"
    assert nested.autohome_models == ["示例车型"]
    assert nested.filters == {"clmc": "多用途乘用车"}
    assert flat.trademark == "旧版牌"
    assert flat.model_prefixes == ["OLD653"]
    assert flat.autohome_models == []

    assert core.find_mapping("示例车型", profiles) is nested
    assert core.find_mapping("DENZA D9", profiles) is nested
    assert core.find_mapping("不存在的车", profiles) is None


def test_model_matches_prefixes():
    assert core.model_matches_prefixes("ABC6520MT6HEV8", include_prefixes=["ABC6520M"])
    assert not core.model_matches_prefixes("ABC6490ST", include_prefixes=["ABC6520M"])
    assert not core.model_matches_prefixes(
        "ABC6520AP1", include_prefixes=["ABC6520"], exclude_prefixes=["ABC6520AP"]
    )
    assert core.model_matches_prefixes("abc6520mt", include_prefixes=["ABC6520M"])


def test_filter_latest_batch():
    rows = [
        {"clxh": "A", "gppc": "393"},
        {"clxh": "B", "gppc": "405"},
        {"clxh": "C", "pc": "405"},
    ]
    latest = core.filter_latest_batch(rows)
    assert {row["clxh"] for row in latest} == {"B", "C"}


def test_filter_latest_batch_handles_zero_padded():
    """接口若返回零填充批次，按整数归一后仍要认成同一批次。"""
    rows = [
        {"clxh": "A", "gppc": "0393"},
        {"clxh": "B", "gppc": "0405"},
        {"clxh": "C", "gppc": "405"},
    ]
    latest = core.filter_latest_batch(rows)
    assert {row["clxh"] for row in latest} == {"B", "C"}


def test_safe_part():
    assert core.safe_part("示例车型 2025款") == "示例车型_2025款"
    assert core.safe_part(" / : * ") == "unknown"
    assert core.safe_part("..") == "unknown"
    assert core.safe_part(".") == "unknown"
    assert core.safe_part("../downloads") == "downloads"


def test_build_announcement_download_dir(tmp_path):
    assert core.build_announcement_download_dir(
        tmp_path,
        trademark="示例牌",
        vehicle_folder="示例车型 2026款",
        batch="408",
    ) == (tmp_path / "示例牌" / "示例车型_2026款" / "第408批")


def test_is_pdf_bytes():
    assert core.is_pdf_bytes(b"%PDF-1.4 rest")
    assert not core.is_pdf_bytes(b"<html>application/pdf</html>")


def test_apply_row_filters_include_and_exclude():
    rows = [
        {"clxh": "ABC6520MT", "clmc": "多用途乘用车"},
        {"clxh": "ABC6520AP1", "clmc": "多用途乘用车"},
        {"clxh": "XYZ1234", "clmc": "轿车"},
    ]
    filtered = core.apply_row_filters(
        rows,
        include_prefixes=["ABC6520"],
        exclude_prefixes=["ABC6520AP"],
        row_filters={"clmc": "多用途"},
    )
    assert [row["clxh"] for row in filtered] == ["ABC6520MT"]


def test_find_mapping_prefers_longest_fuzzy_key(tmp_path):
    mapping_file = tmp_path / "p.json"
    mapping_file.write_text(
        json.dumps(
            [
                {"name": "其它D9", "gonggao": {"trademark": "甲牌"}},
                {"name": "示例D9旗舰", "gonggao": {"trademark": "乙牌"}},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded = core.load_mappings(mapping_file)
    hit = core.find_mapping("D9", loaded)
    assert hit is not None
    assert hit.name == "示例D9旗舰"


def test_download_param_page_requires_magic(tmp_path, monkeypatch):
    monkeypatch.setattr(
        core,
        "post_form",
        lambda *args, **kwargs: (b"<html>captcha</html>", {"content-type": "application/pdf"}),
    )
    path, is_pdf, size = core.download_param_page(
        {"cpsb": "示例牌", "clxh": "ABC1", "gppc": "406", "cpid": "1", "dataTag": "x"},
        tmp_path,
    )
    assert is_pdf is False
    assert path.suffix == ".html"
    assert size == len(b"<html>captcha</html>")


def test_positive_int_rejects_zero_and_negative():
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        core.positive_int("0")
    with pytest.raises(argparse.ArgumentTypeError):
        core.positive_int("-1")
    assert core.positive_int("3") == 3


class _FakeResponse:
    headers: dict[str, str] = {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return b"ok"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_http_request_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        if calls["n"] < 3:
            raise core.error.URLError("connection reset")
        return _FakeResponse()

    monkeypatch.setattr(core.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(core.time, "sleep", lambda seconds: None)
    content, headers = core.http_request("https://example.com/api", data={"a": "1"})
    assert content == b"ok"
    assert headers["content-type"] == "application/json"
    assert calls["n"] == 3


def test_http_request_gives_up_after_retries(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        raise core.error.URLError("down")

    monkeypatch.setattr(core.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(core.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="已重试"):
        core.http_request("https://example.com/api")
    assert calls["n"] == core.REQUEST_RETRIES


def test_http_request_no_retry_on_4xx(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        raise core.error.HTTPError("https://example.com/api", 404, "not found", None, None)

    monkeypatch.setattr(core.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(core.time, "sleep", lambda seconds: None)
    with pytest.raises(core.error.HTTPError):
        core.http_request("https://example.com/api")
    assert calls["n"] == 1


def _query_args(tmp_path, **overrides):
    from types import SimpleNamespace

    defaults = dict(
        mapping_file=str(tmp_path / "missing.json"),
        vehicle=None,
        trademark="示例牌",
        company="",
        model_code="",
        model_prefix=[],
        vehicle_name="",
        page_size=50,
        pc="",
        row_filter=[],
        all_pages=True,
        download=True,
        latest_batch=False,
        exclude_model_prefix=[],
        limit=None,
        output_dir=str(tmp_path),
        flat_output=True,
        vehicle_folder="",
        detail_html=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_command_query_download_all_non_pdf_is_total_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "load_mappings", lambda path: [])
    monkeypatch.setattr(
        core,
        "query_all_pages",
        lambda **kwargs: [{"cpsb": "示例牌", "clxh": "ABC1", "gppc": "406", "cpid": "1", "dataTag": "x"}],
    )
    monkeypatch.setattr(
        core,
        "download_param_page",
        lambda row, folder: (folder / "x.html", False, 12),
    )
    assert core.command_query(_query_args(tmp_path)) == core.EXIT_DOWNLOAD_FAILED


def test_command_query_download_empty_results(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "load_mappings", lambda path: [])
    monkeypatch.setattr(core, "query_all_pages", lambda **kwargs: [])
    assert core.command_query(_query_args(tmp_path)) == core.EXIT_DOWNLOAD_FAILED


def test_download_exit_code_matrix():
    """0 全部成功 / 1 一份 PDF 都没拿到 / 2 拿到了但有条目需人工检查。"""
    assert core.download_exit_code(ok_pdf_count=3, problem_count=0) == core.EXIT_OK
    assert core.download_exit_code(ok_pdf_count=0, problem_count=3) == core.EXIT_DOWNLOAD_FAILED
    assert core.download_exit_code(ok_pdf_count=11, problem_count=1) == core.EXIT_DOWNLOAD_PARTIAL
    # 没有下载任务时不算失败
    assert core.download_exit_code(ok_pdf_count=0, problem_count=0) == core.EXIT_OK


def _rows(count):
    return [
        {"cpsb": "示例牌", "clxh": f"ABC{i}", "gppc": "406", "cpid": str(i), "dataTag": "x"}
        for i in range(count)
    ]


def test_command_query_download_partial_returns_two(tmp_path, monkeypatch):
    """11 份 PDF + 1 份非 PDF 是部分成功，不能和全失败共用退出码 1。"""
    monkeypatch.setattr(core, "load_mappings", lambda path: [])
    monkeypatch.setattr(core, "query_all_pages", lambda **kwargs: _rows(12))

    def fake_download(row, folder):
        if row["clxh"] == "ABC7":
            return folder / "x.html", False, 12
        return folder / f"{row['clxh']}.pdf", True, 2048

    monkeypatch.setattr(core, "download_param_page", fake_download)
    assert core.command_query(_query_args(tmp_path)) == core.EXIT_DOWNLOAD_PARTIAL


def test_command_query_download_partial_on_exception(tmp_path, monkeypatch):
    """单条抛异常但其余拿到 PDF，同样是部分成功。"""
    monkeypatch.setattr(core, "load_mappings", lambda path: [])
    monkeypatch.setattr(core, "query_all_pages", lambda **kwargs: _rows(3))

    def fake_download(row, folder):
        if row["clxh"] == "ABC0":
            raise RuntimeError("boom")
        return folder / f"{row['clxh']}.pdf", True, 2048

    monkeypatch.setattr(core, "download_param_page", fake_download)
    assert core.command_query(_query_args(tmp_path)) == core.EXIT_DOWNLOAD_PARTIAL


def test_command_query_download_all_pdf_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "load_mappings", lambda path: [])
    monkeypatch.setattr(core, "query_all_pages", lambda **kwargs: _rows(2))
    monkeypatch.setattr(
        core,
        "download_param_page",
        lambda row, folder: (folder / f"{row['clxh']}.pdf", True, 2048),
    )
    assert core.command_query(_query_args(tmp_path)) == core.EXIT_OK


def test_download_manifest_records_failures_and_non_pdf(tmp_path, monkeypatch):
    """下载索引必须同时记成功、非 PDF 和失败三类。

    过去 manifest 只 append 成功项，失败的只打到 stderr——命令一结束失败清单就没了，
    无法回答「这批里哪几条没拿到」。
    """
    rows = [
        {"cpsb": "甲牌", "clxh": "OK1", "gppc": "408", "cpid": "1", "dataTag": "t"},
        {"cpsb": "乙牌", "clxh": "HTML1", "gppc": "408", "cpid": "2", "dataTag": "t"},
        {"cpsb": "丙牌", "clxh": "BOOM1", "gppc": "408", "cpid": "3", "dataTag": "t"},
    ]

    def fake_post_form(url, payload, **kwargs):
        if payload["gid"] == "3":
            raise RuntimeError("连接被重置")
        body = b"%PDF-1.4 ok" if payload["gid"] == "1" else "<html>没有找到参数页</html>".encode()
        return body, {}

    monkeypatch.setattr(core, "post_form", fake_post_form)
    monkeypatch.setattr(core, "query_all_pages", lambda **kwargs: rows)

    args = argparse.Namespace(
        vehicle=None, vehicle_name="", mapping_file=str(tmp_path / "absent.json"),
        trademark="甲牌", company="", model_code="", model_prefix=[], exclude_model_prefix=[],
        row_filter=[], pc="", page_size=50, all_pages=False, latest_batch=False,
        limit=None, download=True, detail_html=False, output_dir=str(tmp_path),
        flat_output=False, vehicle_folder="测试车",
    )
    code = core.command_query(args)

    manifests = sorted((tmp_path / core.ANNOUNCEMENT_SNAPSHOT_DIRNAME).glob("manifest_*.json"))
    assert len(manifests) == 1
    entries = json.loads(manifests[0].read_text(encoding="utf-8"))
    by_model = {entry["clxh"]: entry for entry in entries}

    # 三条全部入索引，而不是只有成功的那条
    assert set(by_model) == {"OK1", "HTML1", "BOOM1"}
    assert by_model["OK1"]["status"] == "ok"
    assert by_model["OK1"]["ok_pdf"] is True
    assert by_model["HTML1"]["status"] == "not_pdf"
    assert by_model["HTML1"]["ok_pdf"] is False
    assert by_model["BOOM1"]["status"] == "download_failed"
    assert by_model["BOOM1"]["file"] == ""
    assert "连接被重置" in by_model["BOOM1"]["error"]
    # 拿到 1 份 PDF + 2 条需人工检查 -> 部分成功
    assert code == core.EXIT_DOWNLOAD_PARTIAL
