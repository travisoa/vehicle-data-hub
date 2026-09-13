"""统计只在已提交批次后刷新；异常、只读状态和输入未变化不破坏收尾。"""
from __future__ import annotations

import signal
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import ModuleType

import pytest

import main as entry
import miit_gonggao
from miit_gonggao import collection as seed
from miit_gonggao import collection_manifest as manifest
from miit_gonggao import collection_report, collection_tracking as tracking
from scripts import announcement_catalog_gap as gap
from test_collect_cached_announcements import fake_download, product, setup
from test_seed_announcement_site import run_change_ingestion


@pytest.fixture
def refresh_calls(monkeypatch):
    calls = []
    module = ModuleType("miit_gonggao.collection_status")

    def refresh(db_path, **kwargs):
        completed = None
        if kwargs.get("run_id") is not None:
            with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
                completed = conn.execute("select completed_at from ingestion_runs where id=?",
                                         (kwargs["run_id"],)).fetchone()[0]
                assert completed, "刷新必须能从另一连接读到已提交的轮次收尾"
        calls.append((Path(db_path), kwargs, completed))

    module.refresh_after_collection = refresh
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(miit_gonggao, "collection_status", module, raising=False)
    monkeypatch.setattr(seed, "parse_pdf", lambda *_args: ({"model_code": "ABC6500EV"}, ""))
    monkeypatch.setattr(seed.core, "post_form", lambda *_a, **_k: pytest.fail("测试禁止联网"))
    return calls


def test_catalog_refreshes_after_commit_even_when_report_fails(tmp_path, monkeypatch, refresh_calls):
    def fail_report(*_args, **_kwargs):
        raise OSError("report unavailable")

    monkeypatch.setattr(collection_report, "write_report", fail_report)
    result, db, _ = run_change_ingestion(monkeypatch, tmp_path, announcement_batch="408")
    assert result == 2
    assert len(refresh_calls) == 1
    assert refresh_calls[0][0] == db


def test_catalog_interrupt_still_refreshes_committed_run(tmp_path, monkeypatch, refresh_calls):
    def interrupt(*_args):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run_change_ingestion(monkeypatch, tmp_path, announcement_batch="409", download=interrupt)
    assert len(refresh_calls) == 1
    with closing(sqlite3.connect(tmp_path / "site.sqlite")) as conn:
        assert conn.execute("select status from run_models").fetchone()[0] == "interrupted"


@pytest.mark.parametrize("interrupt", [False, True])
def test_manifest_refreshes_once_per_finished_or_interrupted_batch(
        tmp_path, monkeypatch, refresh_calls, interrupt):
    args, payload = setup(tmp_path, [product("p1"), product("p2")])
    stop = manifest.StopFlag()
    fake_download(monkeypatch, [], lambda _row: stop.request(signal.SIGTERM, None) if interrupt else None)
    assert manifest.collect(args, payload, stop) == (130 if interrupt else 0)
    assert len(refresh_calls) == 1
    assert refresh_calls[0][1]["run_id"] == 1


def _tracking_database(tmp_path):
    conn = sqlite3.connect(tmp_path / "site.sqlite")
    seed.ensure_schema(conn)
    tracking.ensure_schema(conn)
    event_id = tracking.register_event(
        conn, model_code="ABC6500EV", batch=409, kind="formal", product_id="p1",
        source_url="https://www.miit.gov.cn/example.html", event_date="2026-09-01",
        metadata={"product_name": "纯电动客车"})
    conn.commit()
    event = next(item for item in tracking.events(conn) if item["event_id"] == event_id)
    return conn, event


@pytest.mark.parametrize("interrupt", [False, True])
def test_tracking_refreshes_after_normal_or_exceptional_run(tmp_path, monkeypatch, refresh_calls, interrupt):
    conn, event = _tracking_database(tmp_path)
    monkeypatch.setattr(seed.core, "query_all_pages", lambda **_kwargs: [product()] if interrupt else [])
    if interrupt:
        def broken_store(*_args, **_kwargs):
            raise RuntimeError("interrupted storage")
        monkeypatch.setattr(seed, "store_announcement", broken_store)
    try:
        plan = {"pending": [event], "resolved": [], "latest_batch": 409}
        if interrupt:
            with pytest.raises(RuntimeError, match="interrupted storage"):
                tracking.collect(conn, plan, catalog_db=tmp_path / "catalog.sqlite", pdf_root=tmp_path)
        else:
            tracking.collect(conn, plan, catalog_db=tmp_path / "catalog.sqlite", pdf_root=tmp_path)
        assert len(refresh_calls) == 1
    finally:
        conn.close()


def test_tracking_resolved_without_new_run_refreshes_once(tmp_path, refresh_calls):
    conn, event = _tracking_database(tmp_path)
    try:
        result = tracking.collect(conn, {"pending": [], "resolved": [event["event_id"]]},
                                  catalog_db=tmp_path / "catalog.sqlite", pdf_root=tmp_path)
        assert result == {"queries": 0, "errors": 0}
        assert len(refresh_calls) == 1
        with closing(sqlite3.connect(tmp_path / "site.sqlite")) as read:
            assert read.execute("select status from tracking_events").fetchone()[0] == "done"
    finally:
        conn.close()


def test_tracking_registration_refreshes_after_commit(tmp_path, monkeypatch, refresh_calls):
    db_path = tmp_path / "site.sqlite"
    with closing(sqlite3.connect(db_path)) as conn:
        seed.ensure_schema(conn)
    cache = tmp_path / "batch409.json"
    cache.write_text(json.dumps({"complete": True, "verify_prefixes": ["ABC"], "batch": "409",
                                 "source": "https://www.miit.gov.cn/example.html",
                                 "products": [product()]}))
    monkeypatch.setattr(seed, "UPSTREAM_ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["tracking", "--site-db", str(db_path),
                                     "--catalog-db", str(tmp_path / "catalog.sqlite"),
                                     "--pdf-root", str(tmp_path), "register-batch", str(cache)])
    assert tracking.main() == 0
    assert len(refresh_calls) == 1
    assert refresh_calls[0][1]["reason"] == "register-batch"
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("select count(*) from tracking_events").fetchone()[0] == 1


def test_refresh_failure_preserves_collection_and_warns(tmp_path, monkeypatch, refresh_calls, capsys):
    def broken_refresh(*_args, **_kwargs):
        raise OSError("snapshot unavailable")
    monkeypatch.setattr(miit_gonggao.collection_status, "refresh_after_collection", broken_refresh)
    args, payload = setup(tmp_path)
    fake_download(monkeypatch, [])
    assert manifest.collect(args, payload) == 0
    assert "收录统计更新失败" in capsys.readouterr().err
    with closing(sqlite3.connect(args.site_db)) as conn:
        assert conn.execute("select count(*) from documents").fetchone()[0] == 1


def test_status_route_preserves_readonly_default_and_explicit_refresh(monkeypatch, refresh_calls):
    forwarded = []
    monkeypatch.setattr(miit_gonggao.collection_status, "main",
                        lambda argv: forwarded.append(argv) or 7, raising=False)
    assert entry.main(["gonggao", "status"]) == 7
    assert entry.main(["gonggao", "status", "--refresh"]) == 7
    assert forwarded == [[], ["--refresh"]]
    assert not refresh_calls


@pytest.mark.parametrize("changed,interrupt", [(False, False), (True, False), (True, True)])
def test_catalog_sync_only_refreshes_changed_database(tmp_path, monkeypatch, refresh_calls, changed, interrupt):
    catalog = tmp_path / "data" / "jianmian_catalog.sqlite"
    catalog.parent.mkdir()
    catalog.write_bytes(b"before")
    def sync(_argv):
        if changed:
            catalog.write_bytes(b"after update")
        if interrupt:
            raise KeyboardInterrupt()
        return 2
    monkeypatch.setattr(entry.gonggao_core, "main", sync)
    argv = ["gonggao", "jianmian", "sync", "--db", str(catalog)]
    if interrupt:
        with pytest.raises(KeyboardInterrupt):
            entry.main(argv)
    else:
        assert entry.main(argv) == 2
    assert len(refresh_calls) == int(changed)
    if changed:
        assert refresh_calls[0][0] == catalog.parent / "announcement_site.sqlite"


@pytest.mark.parametrize("changed,interrupt", [(False, False), (True, False), (True, True)])
def test_batch_cache_refreshes_once_after_all_writes(tmp_path, monkeypatch, refresh_calls, changed, interrupt):
    catalog = tmp_path / "data" / "catalog.sqlite"
    catalog.parent.mkdir()
    catalog.touch()
    monkeypatch.setattr(gap, "catalog_model_codes", lambda *_args: set())
    def load(batch, **_kwargs):
        if changed:
            gap.write_cache(tmp_path / f"batch{batch}.json", {"batch": batch})
        if interrupt and batch == "409":
            raise KeyboardInterrupt()
        return {}
    monkeypatch.setattr(gap, "load_batch", load)
    argv = ["--batch", "408-409", "--catalog-db", str(catalog), "--fetch-only"]
    if interrupt:
        with pytest.raises(KeyboardInterrupt):
            gap.main(argv)
    else:
        assert gap.main(argv) == 0
    assert len(refresh_calls) == int(changed)
