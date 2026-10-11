"""公告接口 port 的两个适配器：官方接口委托 core，脚本应答不会退回真实接口。"""
import pytest

from miit_gonggao import core
from miit_gonggao.announcement_source import OfficialSource, ScriptedSource


def test_official_source_queries_one_batch_only_when_asked(monkeypatch):
    calls = []
    monkeypatch.setattr(core, "query_all_pages", lambda **kwargs: calls.append(kwargs) or [])
    source = OfficialSource()
    source.query("ABC6500EV")
    source.query("ABC6500EV", 409)
    assert calls == [{"model_code": "ABC6500EV", "page_size": 50},
                     {"model_code": "ABC6500EV", "page_size": 50, "pc": "409"}]


def test_scripted_source_records_calls_and_never_falls_back_to_the_network(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "post_form", lambda *_a, **_k: pytest.fail("脚本应答不得联网"))
    source = ScriptedSource(on_query=lambda **kwargs: [{"clxh": kwargs["model_code"], "gppc": kwargs.get("pc")}])
    assert source.query("ABC6500EV", "410") == [{"clxh": "ABC6500EV", "gppc": "410"}]
    assert source.queries == [{"model_code": "ABC6500EV", "page_size": 50, "pc": "410"}]
    with pytest.raises(AssertionError, match="未提供下载应答"):
        source.download({"cpid": "p1"}, tmp_path)
    source.on_download = lambda row, _folder: ("done", row["cpid"])
    assert source.download({"cpid": "p1"}, tmp_path) == ("done", "p1")
