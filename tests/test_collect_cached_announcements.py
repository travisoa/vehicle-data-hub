"""精确清单下载入口的离线边界、恢复和旧文件保护。"""
from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from miit_gonggao import collection_manifest as cached
from miit_gonggao import collection as seed


def product(pid: str = "p1", model: str = "ABC6500EV", **overrides) -> dict:
    return {"cpid": pid, "clxh": model, "clmc": "纯电动客车", "cpsb": "示例牌", "qymc": "示例企业",
            "dataTag": "Z", "gppc": "409", "pc": "409", **overrides}


def setup(tmp_path: Path, rows: list[dict] | None = None) -> tuple[argparse.Namespace, dict]:
    rows = rows or [product()]
    manifest = {"schema_version": 1, "scope": cached.SCOPE, "expected_products": len(rows),
                "expected_models": len({row["clxh"] for row in rows}), "source_boundary": "test", "products": rows}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    args = argparse.Namespace(manifest=path, manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                              run_dir=tmp_path / "run", site_db=tmp_path / "site.sqlite",
                              pdf_root=tmp_path / "upstream", catalog_db=tmp_path / "missing-catalog.sqlite",
                              resume_run=None, apply=True)
    return args, manifest


def test_legacy_manifest_keeps_its_scope_when_resuming(tmp_path, monkeypatch):
    args, manifest = setup(tmp_path)
    manifest['scope'] = cached.LEGACY_SCOPE
    args.manifest.write_text(json.dumps(manifest), encoding='utf-8')
    args.manifest_sha256 = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    loaded = cached.load_manifest(args.manifest, args.manifest_sha256)
    fake_download(monkeypatch, [])
    assert cached.collect(args, loaded) == 0
    args.resume_run = 1
    assert cached.collect(args, loaded) == 0
    with closing(sqlite3.connect(args.site_db)) as conn:
        selector = json.loads(conn.execute('select selector_json from ingestion_runs').fetchone()[0])
        assert selector['scope'] == cached.LEGACY_SCOPE


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("离线测试不得联网或扩大查询范围")
    monkeypatch.setattr(seed.core, "post_form", fail)
    monkeypatch.setattr(seed.core, "query_all_pages", fail)
    monkeypatch.setattr(seed, "parse_pdf", lambda _path, _db: ({"model_code": "ABC6500EV"}, ""))


def fake_download(monkeypatch, calls: list[str], behavior=None):
    def download(row, target):
        calls.append(row["cpid"])
        if behavior:
            behavior(row)
        path = target / f"{row['cpid']}.pdf"
        content = b"%PDF-1.4\n" + row["cpid"].encode() + b"\n%%EOF"
        path.write_bytes(content)
        return path, True, len(content)
    monkeypatch.setattr(seed.core, "download_param_page", download)


def progress(args) -> dict:
    return json.loads((args.run_dir / "progress.json").read_text())


def models(args) -> list[tuple]:
    with closing(sqlite3.connect(args.site_db)) as conn:
        return conn.execute("SELECT v.announcement_model_code,r.status FROM run_models r "
                            "JOIN vehicles v ON v.id=r.vehicle_id ORDER BY r.sort_order").fetchall()


def seed_existing(args, row, *, parse_error: str = "") -> tuple[Path, bytes]:
    with closing(sqlite3.connect(args.site_db)) as conn:
        seed.ensure_schema(conn)
        vid = seed.upsert_vehicle(conn, {"model_code": row["clxh"], "catalog": "原始目录", "batch": "30",
                                         "category": "客车", "common_name": "原始车名",
                                         "seq": "123", "company": "原始企业"})
        seed.store_announcement(conn, row=row, vehicle_id=vid, market_name="原始车名",
                                download_root=args.pdf_root / "downloads" / "announcement_site",
                                pdf_root=args.pdf_root, catalog_db=args.catalog_db)
        if parse_error:
            conn.execute("UPDATE announcement_fields SET parse_error=?", (parse_error,))
        conn.commit()
        path = args.pdf_root / conn.execute("SELECT relative_path FROM documents").fetchone()[0]
    return path, path.read_bytes()


def test_exact_ids_and_model_aggregation_and_dry_run(tmp_path, monkeypatch):
    args, manifest = setup(tmp_path, [product("p2"), product("p1"), product("p3", "DEF6500EV")])
    calls: list[str] = []
    fake_download(monkeypatch, calls)
    assert cached.main(["--manifest", str(args.manifest), "--manifest-sha256", args.manifest_sha256,
                        "--run-dir", str(args.run_dir), "--site-db", str(args.site_db)]) == 0
    assert not args.site_db.exists() and not args.run_dir.exists() and not calls
    assert cached.collect(args, cached.load_manifest(args.manifest, args.manifest_sha256)) == 0
    assert calls == ["p2", "p1", "p3"]
    assert models(args) == [("ABC6500EV", "done"), ("DEF6500EV", "done")]
    assert progress(args)["status"] == "complete" and progress(args)["remaining"] == 0
    with closing(sqlite3.connect(args.site_db)) as conn:
        actual = conn.execute(
            "SELECT DISTINCT catalog_name,market_name,catalog_seq,catalog_category FROM vehicles").fetchall()
        assert actual == [
            ("公告正式批次清单", "", "", "客车")]
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == manifest["expected_products"]
        assert conn.execute("SELECT completed_at FROM ingestion_runs").fetchone()[0]


@pytest.mark.parametrize("bad", [
    {"clmc": "混合动力客车"}, {"clmc": "纯电动客车底盘"}, {"dataTag": "D"},
    {"clmc": "纯电动三轮摩托车"}, {"gppc": "unknown"}, {"clxh": ""}, {"cpid": ""},
])
def test_manifest_rejects_out_of_scope_or_invalid_rows(tmp_path, bad):
    args, _ = setup(tmp_path, [product(**bad)])
    with pytest.raises(ValueError):
        cached.load_manifest(args.manifest, args.manifest_sha256)
    assert not args.site_db.exists()


@pytest.mark.parametrize("change", ["hash", "scope", "products", "models", "duplicate"])
def test_manifest_rejects_hash_counts_scope_and_duplicate(tmp_path, change):
    args, manifest = setup(tmp_path)
    if change == "hash":
        args.manifest_sha256 = "0" * 64
    else:
        if change == "scope":
            manifest["scope"] = "all_vehicles"
        elif change == "duplicate":
            manifest["products"].append(product())
            manifest["expected_products"] = 2
        else:
            manifest[f"expected_{change}"] = 9
        args.manifest.write_text(json.dumps(manifest))
        args.manifest_sha256 = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        cached.load_manifest(args.manifest, args.manifest_sha256)


def test_existing_success_skip_and_vehicle_metadata_preserved(tmp_path, monkeypatch):
    args, manifest = setup(tmp_path)
    calls: list[str] = []
    fake_download(monkeypatch, calls)
    old_path, old_content = seed_existing(args, product())
    calls.clear()
    assert cached.collect(args, manifest) == 0
    assert not calls and old_path.read_bytes() == old_content
    assert progress(args)["results"] == {"skipped_existing": 1}
    with closing(sqlite3.connect(args.site_db)) as conn:
        actual = conn.execute(
            "SELECT market_name,catalog_name,catalog_batch,catalog_seq,catalog_company FROM vehicles").fetchone()
        assert actual == (
            "原始车名", "原始目录", "30", "123", "原始企业")


def test_existing_parse_problem_is_reported_without_download(tmp_path, monkeypatch):
    args, manifest = setup(tmp_path)
    calls: list[str] = []
    fake_download(monkeypatch, calls)
    seed_existing(args, product(), parse_error="既有版式异常")
    calls.clear()
    assert cached.collect(args, manifest) == 2
    assert not calls and progress(args)["results"] == {"parse_failed": 1}
    assert models(args) == [("ABC6500EV", "partial")]


def test_failed_retry_preserves_old_file_and_database(tmp_path, monkeypatch):
    args, manifest = setup(tmp_path)
    calls: list[str] = []
    fake_download(monkeypatch, calls)
    old_path, old_content = seed_existing(args, product())
    with closing(sqlite3.connect(args.site_db)) as conn:
        conn.execute("UPDATE documents SET sha256='bad-old-digest'")
        conn.commit()
        previous = conn.execute("SELECT * FROM documents").fetchone()
    def failed(_row):
        raise TimeoutError("offline timeout")
    fake_download(monkeypatch, calls, failed)
    assert cached.collect(args, manifest) == 2
    assert old_path.read_bytes() == old_content
    with closing(sqlite3.connect(args.site_db)) as conn:
        assert conn.execute("SELECT * FROM documents").fetchone() == previous
    assert progress(args)["results"] == {"download_failed": 1}


def test_multiple_ids_partial_not_done(tmp_path, monkeypatch):
    args, manifest = setup(tmp_path, [product("p1"), product("p2")])
    def fail_second(row):
        if row["cpid"] == "p2":
            raise TimeoutError("offline")
    fake_download(monkeypatch, [], fail_second)
    assert cached.collect(args, manifest) == 2
    assert models(args) == [("ABC6500EV", "partial")]
    assert progress(args)["processed"] == 2


def test_signal_interrupt_and_resume_skips_committed_success(tmp_path, monkeypatch):
    args, manifest = setup(tmp_path, [product("p1"), product("p2"), product("p3", "DEF6500EV")])
    stop = cached.StopFlag()
    calls: list[str] = []
    fake_download(monkeypatch, calls, lambda row: stop.request(signal.SIGTERM, None) if row["cpid"] == "p1" else None)
    assert cached.collect(args, manifest, stop) == 130
    assert calls == ["p1"]
    assert progress(args)["status"] == "interrupted" and progress(args)["remaining"] == 2
    assert models(args) == [("ABC6500EV", "interrupted")]
    with closing(sqlite3.connect(args.site_db)) as conn:
        assert conn.execute("SELECT completed_at FROM ingestion_runs").fetchone()[0]
    args.resume_run = progress(args)["run_id"]
    fake_download(monkeypatch, calls)
    assert cached.collect(args, manifest) == 0
    assert calls == ["p1", "p2", "p3"]
    assert progress(args)["results"] == {"skipped_existing": 1, "downloaded": 2}
    assert models(args) == [("ABC6500EV", "done"), ("DEF6500EV", "done")]
    with closing(sqlite3.connect(args.site_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 1


def test_three_consecutive_download_failures_stop_without_touching_rest(tmp_path, monkeypatch):
    args, manifest = setup(tmp_path, [product(f"p{n}", f"ABC650{n}EV") for n in range(5)])
    calls: list[str] = []
    def fail(_row):
        raise TimeoutError("offline")
    fake_download(monkeypatch, calls, fail)
    assert cached.collect(args, manifest) == 130
    assert calls == ["p0", "p1", "p2"]
    assert len(models(args)) == 3 and progress(args)["remaining"] == 2
    assert progress(args)["results"] == {"download_failed": 3}


def test_other_unfinished_run_and_lock_prevent_start(tmp_path, monkeypatch):
    args, manifest = setup(tmp_path)
    calls: list[str] = []
    fake_download(monkeypatch, calls)
    with closing(sqlite3.connect(args.site_db)) as conn:
        seed.ensure_schema(conn)
        conn.execute("INSERT INTO ingestion_runs(started_at,selector_json,selected_models) VALUES ('now','{}',1)")
        conn.commit()
    with pytest.raises(RuntimeError, match="未结束"):
        cached.collect(args, manifest)
    assert not calls
    with cached.database_lock(args.site_db), pytest.raises(RuntimeError, match="持有锁"):
        cached.collect(args, manifest)
    with closing(sqlite3.connect(args.site_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 1


def test_resume_rejects_other_manifest(tmp_path, monkeypatch):
    args, manifest = setup(tmp_path)
    fake_download(monkeypatch, [])
    assert cached.collect(args, manifest) == 0
    args.resume_run = progress(args)["run_id"]
    args.manifest_sha256 = "0" * 64
    with pytest.raises(RuntimeError, match="清单"):
        cached.collect(args, manifest)
    args.run_dir = tmp_path / "different-run-dir"
    with pytest.raises(RuntimeError, match="不匹配"):
        cached.collect(args, manifest)


def test_non_pdf_and_new_parse_failure_remain_distinct(tmp_path, monkeypatch):
    args, manifest = setup(tmp_path, [product("html", "ABC6500EV"), product("parse", "DEF6500EV")])
    def download(row, target):
        is_pdf = row["cpid"] == "parse"
        path = target / (row["cpid"] + (".pdf" if is_pdf else ".html"))
        content = b"%PDF-1.4\n%%EOF" if is_pdf else b"<html>not a PDF</html>"
        path.write_bytes(content)
        return path, is_pdf, len(content)
    monkeypatch.setattr(seed.core, "download_param_page", download)
    monkeypatch.setattr(seed, "parse_pdf", lambda *_args: ({}, "unsupported layout"))
    assert cached.collect(args, manifest) == 2
    assert progress(args)["results"] == {"non_pdf": 1, "parse_failed": 1}
    with closing(sqlite3.connect(args.site_db)) as conn:
        counts = conn.execute(
            "SELECT download_failures,non_pdf_documents,parse_failures FROM ingestion_runs").fetchone()
        assert counts == (0, 1, 1)
        row = conn.execute("SELECT d.relative_path,d.is_pdf FROM documents d "
                           "JOIN announcements a ON a.id=d.announcement_id "
                           "WHERE a.source_product_id='parse'").fetchone()
        assert row[1] == 1 and (args.pdf_root / row[0]).is_file()


@pytest.mark.parametrize("minimum,maximum", [
    (0.1, None), (None, 0.2), (0.0, 0.2), (-0.1, 0.2), (0.3, 0.2),
    (float("nan"), 0.2), (0.1, float("inf")),
])
def test_invalid_request_intervals(minimum, maximum):
    with pytest.raises(ValueError):
        cached.requested_interval(argparse.Namespace(min_interval=minimum, max_interval=maximum))


def test_request_pace_is_process_local_and_restored_on_exception(monkeypatch):
    monkeypatch.setattr(seed.core, "REQUEST_MIN_INTERVAL", (0.8, 1.8))
    args = argparse.Namespace(min_interval=0.12, max_interval=0.25)
    with pytest.raises(RuntimeError), cached.request_pace(args):
        assert seed.core.REQUEST_MIN_INTERVAL == (0.12, 0.25)
        raise RuntimeError("offline interruption")
    assert seed.core.REQUEST_MIN_INTERVAL == (0.8, 1.8)


def test_dry_run_reports_interval_without_changing_core(tmp_path, capsys):
    args, _manifest = setup(tmp_path)
    original = seed.core.REQUEST_MIN_INTERVAL
    assert cached.main(["--manifest", str(args.manifest), "--manifest-sha256", args.manifest_sha256,
                        "--run-dir", str(args.run_dir), "--min-interval", "0.12", "--max-interval", "0.25"]) == 0
    assert json.loads(capsys.readouterr().out)["request_interval"] == [0.12, 0.25]
    assert seed.core.REQUEST_MIN_INTERVAL == original
    assert not args.run_dir.exists()


@pytest.mark.parametrize("failure", ["download_failed", "non_pdf", "parse_failed"])
def test_remote_error_restores_default_but_parse_error_does_not(tmp_path, monkeypatch, failure):
    args, manifest = setup(tmp_path, [product("p1"), product("p2")])
    args.min_interval, args.max_interval = 0.12, 0.25
    monkeypatch.setattr(seed.core, "REQUEST_MIN_INTERVAL", (0.8, 1.8))
    intervals = []
    def download(row, target):
        intervals.append(seed.core.REQUEST_MIN_INTERVAL)
        if row["cpid"] == "p1" and failure == "download_failed":
            raise TimeoutError("offline remote failure")
        is_pdf = not (row["cpid"] == "p1" and failure == "non_pdf")
        path = target / (row["cpid"] + (".pdf" if is_pdf else ".html"))
        content = b"%PDF-1.4\n%%EOF" if is_pdf else b"<html>not a PDF</html>"
        path.write_bytes(content)
        return path, is_pdf, len(content)
    monkeypatch.setattr(seed.core, "download_param_page", download)
    if failure == "parse_failed":
        monkeypatch.setattr(seed, "parse_pdf", lambda *_args: ({}, "offline parsing error"))
    assert cached.collect(args, manifest) == 2
    expected = (0.12, 0.25) if failure == "parse_failed" else (0.8, 1.8)
    assert intervals == [(0.12, 0.25), expected]
    assert progress(args)["request_interval"] == list(expected)
    assert progress(args)["requested_interval"] == [0.12, 0.25]
    assert bool(progress(args)["pace_warning"]) == (failure != "parse_failed")
    assert seed.core.REQUEST_MIN_INTERVAL == (0.8, 1.8)


@pytest.mark.parametrize("title,detail,known", [
    ("获取参数页发生错误", "Byte data not found at location : /example/pic.jpg", True),
    ("获取参数页发生错误", "没有找到参数页", True),
    ("获取参数页发生错误", "Please retry later", False),
    ("Unknown error", "没有找到参数页", False),
    ("Unknown error", "Byte data not found at location : /example/pic.jpg", False),
])
def test_only_verified_source_generation_html_keeps_fast_pace(tmp_path, monkeypatch, title, detail, known):
    args, manifest = setup(tmp_path, [product("p1"), product("p2")])
    args.min_interval, args.max_interval = 0.12, 0.25
    monkeypatch.setattr(seed.core, "REQUEST_MIN_INTERVAL", (0.8, 1.8))
    intervals = []
    def download(row, target):
        intervals.append(seed.core.REQUEST_MIN_INTERVAL)
        is_pdf = row["cpid"] != "p1"
        path = target / (row["cpid"] + (".pdf" if is_pdf else ".html"))
        content = b"%PDF-1.4\n%%EOF" if is_pdf else f"<title>{title}</title><body>{detail}</body>".encode()
        path.write_bytes(content)
        return path, is_pdf, len(content)
    monkeypatch.setattr(seed.core, "download_param_page", download)
    assert cached.collect(args, manifest) == 2
    assert intervals == [(0.12, 0.25), (0.12, 0.25) if known else (0.8, 1.8)]
    assert progress(args)["results"] == {"non_pdf": 1, "downloaded": 1}
    assert progress(args)["source_generation_errors"] == int(known)
    assert bool(progress(args)["pace_warning"]) is not known
    result = json.loads((args.run_dir / "results.jsonl").read_text().splitlines()[0])
    assert result["source_generation_error"] is known
    with closing(sqlite3.connect(args.site_db)) as conn:
        assert conn.execute("SELECT non_pdf_documents FROM ingestion_runs").fetchone()[0] == 1
