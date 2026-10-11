"""采集轮次的接口：认领、型号结局、单产品计数与降速、收尾。"""
import sqlite3
from contextlib import closing

import pytest

from miit_gonggao import collection as seed
from miit_gonggao.collection_run import CollectionRun, RunConflict


class Pace:
    def __init__(self):
        self.calls = []

    def slow_down(self, status):
        self.calls.append(status)


@pytest.fixture
def conn(tmp_path):
    with closing(sqlite3.connect(tmp_path / "site.sqlite")) as connection:
        seed.ensure_schema(connection)
        connection.commit()
        yield connection


def vehicle(conn, code):
    return seed.upsert_vehicle(conn, {"model_code": code, "common_name": code, "catalog": "测试", "batch": "409",
                                      "category": "乘用车", "seq": "", "company": ""})


def new_run(conn, tmp_path, pace=None):
    return CollectionRun(conn, catalog_db=tmp_path / "catalog.sqlite", pdf_root=tmp_path,
                         reason="test_collection", pace=pace)


def test_only_one_unfinished_run_and_resume_marks_stale_models_interrupted(conn, tmp_path):
    first = new_run(conn, tmp_path)
    run_id = first.claim({"mode": "manifest", "scope": "a"}, 2)
    first.begin_model(1, vehicle(conn, "ABC6500EV"))

    with pytest.raises(RunConflict, match="未结束"):
        new_run(conn, tmp_path).claim({"mode": "manifest"}, 1)
    with pytest.raises(RunConflict, match="未完成采集"):
        new_run(conn, tmp_path).ensure_idle()
    with pytest.raises(RunConflict, match="scope 不符"):
        new_run(conn, tmp_path).claim({}, 1, resume_run=run_id, resume_matches=lambda old: old["scope"] == "b",
                                      mismatch_message="scope 不符")

    resumed = new_run(conn, tmp_path)
    assert resumed.claim({}, 1, resume_run=run_id, resume_matches=lambda old: old["scope"] == "a") == run_id
    assert conn.execute("SELECT status, error FROM run_models WHERE run_id=?", (run_id,)).fetchone() == (
        "interrupted", "上次进程未完成该型号")


def test_live_failures_slow_down_but_existing_files_only_count(conn, tmp_path):
    pace = Pace()
    run = new_run(conn, tmp_path, pace)
    run.claim({"mode": "test"}, 1)
    run.record_product("download_failed", message="下载失败：timeout")
    run.record_product("skipped_existing", image_failed=True, live=False)
    run.record_product("parse_failed", live=False)
    run.record(seed.StoreOutcome(True, "图片下载不完整：1 项失败", True))
    assert pace.calls == ["download_failed", "image_failed"]
    assert run.counts["download_failures"] == 1
    assert run.counts["parse_failures"] == 1
    assert run.counts["image_failures"] == 2


def test_verified_source_generation_error_is_counted_without_slowing_down(conn, tmp_path):
    pace = Pace()
    run = new_run(conn, tmp_path, pace)
    run.claim({"mode": "test"}, 1)
    page = tmp_path / "downloads" / "p.html"
    page.parent.mkdir()
    page.write_text("获取参数页发生错误：没有找到参数页", encoding="utf-8")
    verified = "接口返回非 PDF，已存为 HTML；原文件：downloads/p.html"

    assert run.record_product("non_pdf", message=verified) is True
    assert run.record_product("non_pdf", message="接口返回非 PDF，已存为 HTML；原文件：downloads/other.html") is False
    assert pace.calls == ["non_pdf"]
    assert run.counts["non_pdf_documents"] == 2


def test_finish_writes_counts_interrupts_open_models_and_refreshes_once(conn, tmp_path, monkeypatch):
    refreshes = []
    monkeypatch.setattr(seed, "refresh_collection_status", lambda _db_path, **kwargs: refreshes.append(kwargs))
    run = new_run(conn, tmp_path)
    first, second, third = (vehicle(conn, code) for code in ("A1", "B2", "C3"))
    run_id = run.claim({"mode": "test"}, 3)
    run.begin_model(1, first)
    run.finish_model(first, "done")
    run.begin_model(2, second)
    run.query_failed(second, "HTTP 500")
    run.begin_model(3, third)
    run.count("awaiting_effective")

    assert run.finish("进程中断") == 1
    assert conn.execute("SELECT status, error FROM run_models ORDER BY sort_order").fetchall() == [
        ("done", ""), ("query_failed", "HTTP 500"), ("interrupted", "进程中断")]
    row = conn.execute("SELECT completed_at IS NOT NULL, query_failures, awaiting_effective FROM ingestion_runs "
                       "WHERE id=?", (run_id,)).fetchone()
    assert row == (1, 1, 1)
    assert refreshes == [{"catalog_db": tmp_path / "catalog.sqlite", "pdf_root": tmp_path, "run_id": run_id,
                          "reason": "test_collection"}]


def test_outcome_vocabularies_are_closed(conn, tmp_path):
    run = new_run(conn, tmp_path)
    run.claim({"mode": "test"}, 1)
    with pytest.raises(ValueError, match="型号结局"):
        run.finish_model(1, "querying")
    with pytest.raises(ValueError, match="单产品结局"):
        run.record_product("unknown")
    with pytest.raises(ValueError, match="不一致"):
        seed.StoreOutcome(False, "下载失败：timeout")
    assert seed.StoreOutcome(False, "解析失败：x", status="parse_failed").has_pdf
    assert not seed.StoreOutcome(False, "接口返回非 PDF", status="non_pdf").has_pdf
