"""miit_gonggao.core 测试：统一档案加载、筛选逻辑、HTTP 重试。"""

from __future__ import annotations

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
