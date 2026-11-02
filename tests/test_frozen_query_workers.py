"""冻结查询重叠执行，证据不串流，业务库仍由调度线程单写。"""
import base64
from collections import defaultdict
import hashlib
import json
import sqlite3
import threading
import time
from urllib.parse import parse_qs

import pytest

from miit_gonggao import collection, core
from miit_gonggao.announcement_source import EvidenceOfficialSource, ScriptedSource


def arguments(tmp_path, monkeypatch, models, *, workers=4):
    monkeypatch.setattr(collection, "UPSTREAM_ROOT", tmp_path)
    monkeypatch.setattr(collection, "refresh_collection_status", lambda *_a, **_k: None)
    path = tmp_path / "models.txt"
    path.write_text("\n".join(models) + "\n")
    return ["--query-frozen-models", str(path), "--frozen-sha256",
            hashlib.sha256(path.read_bytes()).hexdigest(), "--frozen-run-dir",
            str(tmp_path / "var/runs/test"), "--limit", str(len(models)),
            "--site-db", str(tmp_path / "site.sqlite"), "--download-root", str(tmp_path / "downloads"),
            "--workers", str(workers), "--no-images", "--no-report"]


def test_workers_overlap_and_sqlite_has_every_result_in_source_order(tmp_path, monkeypatch):
    argv = arguments(tmp_path, monkeypatch, list("ABCDEF"))
    main_thread = threading.get_ident()
    writer_threads = set()
    connect = sqlite3.connect

    class WriterConnection(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            writer_threads.add(threading.get_ident())
            assert threading.get_ident() == main_thread
            return super().execute(*args, **kwargs)

    monkeypatch.setattr(collection.sqlite3, "connect", lambda *a, **k: connect(*a, **k, factory=WriterConnection))
    barrier = threading.Barrier(4)
    active = maximum = 0
    lock = threading.Lock()
    query_threads = set()

    def query(*, model_code, **_kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            query_threads.add(threading.get_ident())
        try:
            if model_code in "ABCD":
                barrier.wait(timeout=5)
            if model_code == "C":
                raise OSError("unavailable")
            return [{"clxh": model_code, "cpid": model_code}, {"clxh": model_code.lower(), "cpid": "wrong"}]
        finally:
            with lock:
                active -= 1

    assert collection.catalog_main(argv, source=ScriptedSource(on_query=query)) == 2
    assert maximum == 4 and len(query_threads) == 4 and main_thread not in query_threads
    assert writer_threads == {main_thread}
    with connect(tmp_path / "site.sqlite") as conn:
        results = conn.execute("SELECT v.announcement_model_code,r.status FROM run_models r "
                               "JOIN vehicles v ON v.id=r.vehicle_id ORDER BY r.sort_order").fetchall()
        selector = json.loads(conn.execute("SELECT selector_json FROM ingestion_runs").fetchone()[0])
    assert results == [(model, "query_failed" if model == "C" else "partial") for model in "ABCDEF"]
    assert selector["workers"] == 4 and selector["frozen_total_models"] == 6


def test_real_http_layer_preserves_pagination_evidence_and_global_pace(tmp_path, monkeypatch):
    argv = arguments(tmp_path, monkeypatch, list("ABCD"))
    monkeypatch.setattr(core, "REQUEST_MIN_INTERVAL", (0.025, 0.025))
    monkeypatch.setattr(core, "_last_request_at", 0.0)
    barrier = threading.Barrier(4)
    starts = []
    lock = threading.Lock()

    class Page:
        status = 200
        headers = {}

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self):
            return json.dumps(self.payload).encode()

    def urlopen(request, **_kwargs):
        params = parse_qs(request.data.decode())
        model, page = params["clxh"][0], int(params["pageNum"][0])
        with lock:
            starts.append(time.monotonic())
        if page == 1:
            barrier.wait(timeout=5)
        total = {"A": 2, "B": 0, "C": 1, "D": 1}[model]
        pages = 2 if model == "A" else int(bool(total))
        rows = [{"clxh": model if model != "D" else "D-OTHER", "cpid": f"{model}-{page}"}] if total else []
        return Page({"handleResult": {"respCode": 200},
                     "countResult": {"total": total, "pageNum": page, "totalPage": pages}, "cpList": rows})

    monkeypatch.setattr(core.request, "urlopen", urlopen)
    evidence = tmp_path / "evidence.jsonl"
    assert collection.catalog_main(argv, source=EvidenceOfficialSource(evidence)) == 0
    starts.sort()
    assert len(starts) == 5 and all(b - a >= 0.015 for a, b in zip(starts, starts[1:]))
    events = [json.loads(line) for line in evidence.read_text().splitlines()]
    grouped = defaultdict(list)
    for event in events:
        grouped[event["query_id"]].append(event)
    assert len(grouped) == 4
    seen = {}
    for stream in grouped.values():
        assert stream[0]["type"] == "query_started" and stream[-1]["type"] == "query_succeeded"
        model = stream[0]["model_code"]
        requests = [parse_qs(base64.b64decode(e["request_body_base64"]).decode())
                    for e in stream if e["type"] == "http_attempt"]
        assert all(p["clxh"] == [model] for p in requests)
        assert [p["pageNum"][0] for p in requests] == (["1", "2"] if model == "A" else ["1"])
        seen[model] = stream[-1]["rows"]
    assert seen == {"A": 2, "B": 0, "C": 1, "D": 1}
    with sqlite3.connect(tmp_path / "site.sqlite") as conn:
        outcomes = conn.execute("SELECT status FROM run_models ORDER BY sort_order").fetchall()
    assert outcomes == [("partial",), ("no_match",), ("partial",), ("no_match",)]


def test_failure_slows_workers_immediately_and_drains_bounded_inflight_queries(tmp_path, monkeypatch, capsys):
    argv = arguments(tmp_path, monkeypatch, list("ABCDEFGHIJ"))
    argv += ["--min-interval", "0.01", "--max-interval", "0.01"]
    original_interval = core.REQUEST_MIN_INTERVAL
    main_thread = threading.get_ident()
    slowdowns = []
    slow_down = collection.RequestPace.slow_down

    def observed(self, status):
        slow_down(self, status)
        slowdowns.append((threading.get_ident(), core.REQUEST_MIN_INTERVAL))

    monkeypatch.setattr(collection.RequestPace, "slow_down", observed)

    def fail(**_kwargs):
        raise OSError("offline")

    source = ScriptedSource(on_query=fail)
    assert collection.catalog_main(argv, source=source) == 2
    assert len(source.queries) == 6  # Three failures plus at most workers-1 already claimed.
    assert any(thread != main_thread for thread, _ in slowdowns)
    assert all(interval == original_interval for _, interval in slowdowns)
    assert core.REQUEST_MIN_INTERVAL == original_interval
    with sqlite3.connect(tmp_path / "site.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM run_models WHERE status='query_failed'").fetchone()[0] == 6
        assert conn.execute("SELECT count(*) FROM run_models WHERE status='querying'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM vehicles WHERE announcement_model_code='G'").fetchone()[0] == 0
    assert "停止派发" in capsys.readouterr().err


def test_interruption_waits_for_workers_and_finishes_the_claimed_models(tmp_path, monkeypatch):
    argv = arguments(tmp_path, monkeypatch, list("ABCDEFGH"))
    barrier = threading.Barrier(4)
    active = 0
    lock = threading.Lock()

    def query(*, model_code, **_kwargs):
        nonlocal active
        with lock:
            active += 1
        try:
            barrier.wait(timeout=5)
            if model_code == "A":
                raise KeyboardInterrupt
            time.sleep(0.02)
            return []
        finally:
            with lock:
                active -= 1

    with pytest.raises(KeyboardInterrupt):
        collection.catalog_main(argv, source=ScriptedSource(on_query=query))
    assert active == 0
    with sqlite3.connect(tmp_path / "site.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM run_models WHERE status='interrupted'").fetchone()[0] == 4
        assert conn.execute("SELECT completed_at FROM ingestion_runs").fetchone()[0]
        assert conn.execute("SELECT count(*) FROM vehicles WHERE announcement_model_code='E'").fetchone()[0] == 0


@pytest.mark.parametrize("workers", ["0", "9"])
def test_workers_are_bounded(workers):
    with pytest.raises(SystemExit):
        collection.catalog_main(["--workers", workers])


def test_workers_cannot_silently_change_other_collection_modes():
    with pytest.raises(SystemExit):
        collection.catalog_main(["--workers", "4"])
