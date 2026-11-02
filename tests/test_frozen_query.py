"""冻结清单查询仅记录可追溯结局，匹配结果仍须下载和解析。"""
import hashlib
import json
import sqlite3

import pytest

from miit_gonggao import collection
from miit_gonggao.announcement_source import ScriptedSource


def test_frozen_query_uses_full_list_slice_and_exact_match(tmp_path, monkeypatch):
    monkeypatch.setattr(collection, "UPSTREAM_ROOT", tmp_path)
    monkeypatch.setattr(collection, "refresh_collection_status", lambda *_a, **_k: None)
    models = tmp_path / "models.txt"
    models.write_text("SKIP\nAAA\nBBB\nCCC\n", encoding="utf-8")
    sha = hashlib.sha256(models.read_bytes()).hexdigest()
    site = tmp_path / "site.sqlite"
    run_dir = tmp_path / "var" / "runs" / "first"

    def query(**kwargs):
        if kwargs["model_code"] == "AAA":
            return [{"clxh": "AAA", "cpid": "p1"}, {"clxh": "aaa", "cpid": "wrong"}]
        if kwargs["model_code"] == "BBB":
            return [{"clxh": "BBB-OTHER", "cpid": "wrong"}]
        raise OSError("offline")

    source = ScriptedSource(on_query=query)
    argv = ["--query-frozen-models", str(models), "--frozen-sha256", sha,
            "--frozen-run-dir", str(run_dir), "--offset", "1", "--limit", "3",
            "--site-db", str(site), "--download-root", str(tmp_path / "downloads"),
            "--no-report", "--no-images"]
    assert collection.catalog_main(argv, source=source) == 2
    assert [query["model_code"] for query in source.queries] == ["AAA", "BBB", "CCC"]
    assert source.downloads == []
    with sqlite3.connect(site) as conn:
        results = conn.execute("SELECT v.announcement_model_code, r.status, r.error FROM run_models r "
                               "JOIN vehicles v ON v.id=r.vehicle_id ORDER BY r.sort_order").fetchall()
        selector = json.loads(conn.execute("SELECT selector_json FROM ingestion_runs").fetchone()[0])
    assert [row[:2] for row in results] == [("AAA", "partial"), ("BBB", "no_match"),
                                            ("CCC", "query_failed")]
    assert "命中 1 条" in results[0][2]
    assert selector["frozen_sha256"] == sha and selector["frozen_total_models"] == 4
    assert (run_dir / "models.txt").read_bytes() == models.read_bytes()
    with pytest.raises(SystemExit):
        collection.catalog_main(argv, source=source)


def test_frozen_query_requires_hash_limit_and_new_run_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(collection, "UPSTREAM_ROOT", tmp_path)
    models = tmp_path / "models.txt"
    models.write_text("AAA\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        collection.catalog_main(["--query-frozen-models", str(models)])


def test_frozen_query_stops_after_three_failures_without_marking_remaining(tmp_path, monkeypatch):
    monkeypatch.setattr(collection, "UPSTREAM_ROOT", tmp_path)
    monkeypatch.setattr(collection, "refresh_collection_status", lambda *_a, **_k: None)
    models = tmp_path / "models.txt"
    models.write_text("A\nB\nC\nD\n", encoding="utf-8")
    sha = hashlib.sha256(models.read_bytes()).hexdigest()
    site = tmp_path / "site.sqlite"

    def fail(**_kwargs):
        raise OSError("offline")

    source = ScriptedSource(on_query=fail)
    assert collection.catalog_main([
        "--query-frozen-models", str(models), "--frozen-sha256", sha,
        "--frozen-run-dir", str(tmp_path / "var" / "runs" / "fail"), "--limit", "4",
        "--site-db", str(site), "--download-root", str(tmp_path / "downloads"), "--no-report",
    ], source=source) == 2
    assert len(source.queries) == 3
    with sqlite3.connect(site) as conn:
        assert conn.execute("SELECT count(*) FROM run_models WHERE status='query_failed'").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM vehicles WHERE announcement_model_code='D'").fetchone()[0] == 0
