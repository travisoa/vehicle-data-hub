"""逐次请求证据不改变重试语义，也不能串到其他调用上下文。"""
import io
import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError

import pytest

from miit_gonggao import core
from miit_gonggao.announcement_source import EvidenceOfficialSource


class Response:
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self):
        return b'{"cpList":[]}'


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(core, "_throttle", lambda: None)
    monkeypatch.setattr(core.time, "sleep", lambda _seconds: None)


def test_attempts_include_failed_and_successful_raw_response(monkeypatch):
    calls = iter([HTTPError("https://example.test", 503, "unavailable", {}, io.BytesIO(b"unavailable")),
                  Response()])

    def urlopen(*_args, **_kwargs):
        result = next(calls)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(core.request, "urlopen", urlopen)
    events = []
    with core.observe_http_attempts(events.append):
        body, _ = core.http_request("https://example.test", data={"clxh": "ABC"},
                                    headers={"Cookie": "secret", "User-Agent": "test"})
    assert body == b'{"cpList":[]}'
    assert [event["attempt"] for event in events] == [1, 2]
    assert [event["response_status"] for event in events] == [503, 200]
    assert [event["response_body"] for event in events] == [b"unavailable", body]
    assert all(event["request_body"] == b"clxh=ABC" for event in events)
    assert all("secret" not in str(event) for event in events)
    assert events[0]["error"] and not events[1]["error"]
    assert all(event["started_at"] and event["finished_at"] for event in events)


def test_http_error_body_remains_readable(monkeypatch):
    def fail(*_args, **_kwargs):
        raise HTTPError("https://example.test", 403, "forbidden", {}, io.BytesIO(b"denied"))

    monkeypatch.setattr(core.request, "urlopen", fail)
    events = []
    with core.observe_http_attempts(events.append), pytest.raises(HTTPError) as caught:
        core.http_request("https://example.test")
    assert events[0]["response_body"] == caught.value.read() == b"denied"


def test_observers_are_isolated_between_threads(monkeypatch):
    barrier = threading.Barrier(2)

    def urlopen(*_args, **_kwargs):
        barrier.wait(timeout=5)
        return Response()

    monkeypatch.setattr(core.request, "urlopen", urlopen)

    def query(model):
        events = []
        with core.observe_http_attempts(events.append):
            core.http_request("https://example.test", data={"clxh": model})
        return events

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(query, ["A", "B"]))
    assert [event["request_body"] for event in a] == [b"clxh=A"]
    assert [event["request_body"] for event in b] == [b"clxh=B"]


def test_observer_failure_is_not_retried_or_hidden(monkeypatch):
    calls = []
    monkeypatch.setattr(core.request, "urlopen", lambda *_a, **_k: calls.append(1) or Response())

    def fail(_event):
        raise OSError("证据写入失败")

    with core.observe_http_attempts(fail), pytest.raises(OSError, match="证据写入失败"):
        core.http_request("https://example.test")
    assert calls == [1]


def test_official_source_persists_each_page_and_query_result(tmp_path, monkeypatch):
    pages = [
        {"handleResult": {"respCode": 200}, "countResult": {"total": 2, "pageNum": 1, "totalPage": 2},
         "cpList": [{"cpid": "one"}]},
        {"handleResult": {"respCode": 200}, "countResult": {"total": 2, "pageNum": 2, "totalPage": 2},
         "cpList": [{"cpid": "two"}]},
    ]

    class Page(Response):
        def read(self):
            return json.dumps(pages.pop(0)).encode()

    monkeypatch.setattr(core.request, "urlopen", lambda *_a, **_k: Page())
    path = tmp_path / "run" / "query.jsonl"
    assert [row["cpid"] for row in EvidenceOfficialSource(path).query("MODEL1")] == ["one", "two"]
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["type"] for event in events] == ["query_started", "http_attempt", "http_attempt",
                                                    "query_succeeded"]
    assert len({event["query_id"] for event in events}) == 1
    requests = [base64.b64decode(event["request_body_base64"]) for event in events[1:3]]
    assert b"clxh=MODEL1" in requests[0]
    assert b"pageNum=1" in requests[0] and b"pageNum=2" in requests[1]
    assert json.loads(base64.b64decode(events[2]["response_body_base64"]))["cpList"][0]["cpid"] == "two"
    assert events[-1]["rows"] == 2


def test_missing_http_evidence_cannot_be_a_success(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "query_all_pages", lambda **_kwargs: [])
    path = tmp_path / "query.jsonl"
    with pytest.raises(RuntimeError, match="未产生 HTTP 证据"):
        EvidenceOfficialSource(path).query("MODEL1")
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["type"] for event in events] == ["query_started", "query_failed"]


@pytest.mark.parametrize("payload", [
    {"handleResult": {"respCode": 200}, "cpList": []},
    {"handleResult": {"respCode": 200}, "countResult": {"total": 1, "pageNum": 1, "totalPage": 1},
     "cpList": []},
])
def test_malformed_or_incomplete_result_is_not_no_match(tmp_path, monkeypatch, payload):
    class Page(Response):
        def read(self):
            return json.dumps(payload).encode()

    monkeypatch.setattr(core.request, "urlopen", lambda *_a, **_k: Page())
    path = tmp_path / "query.jsonl"
    with pytest.raises(RuntimeError):
        EvidenceOfficialSource(path).query("MODEL1")
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert events[-1]["type"] == "query_failed"
    assert events[1]["response_body_base64"]
