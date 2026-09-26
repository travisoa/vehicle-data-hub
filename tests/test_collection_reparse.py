"""存量解析字段刷新：只换 PDF 解析键、核对登记哈希、默认只读、写入后统计收尾一次。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

import main as entry
from miit_gonggao import collection as seed
from miit_gonggao import collection_reparse as reparse

STALE = {  # 旧解析器处理改装车版式的结果：底盘名称当型号、底盘编号混进 VIN
    "model_code": "XGH5041TXSSBEV", "chassis": "二类纯电动载货汽车底盘",
    "vin": "13803677SH1047PCEVNZY52341", "gross_mass": "4495",
    "range_km": "260", "common_name": "",  # 目录补充键，不属于 PDF 解析
}
CONVERTED = {
    "model_code": "XGH5041TXSSBEV", "chassis": "SH1047PCEVNZY5", "vin": "", "gross_mass": "4495",
    "chassis_references": json.dumps([{"product_id": "3803677", "model_code": "SH1047PCEVNZY5",
                                       "category": "二类", "product_name": "纯电动载货汽车底盘"}],
                                      ensure_ascii=False),
    "pdf_layout": "converted_vehicle", "source_file": "a.pdf",
}


def add(conn, root: Path, product_id: str, fields: dict, *, content: bytes, register: bytes | None = None):
    path = root / "downloads" / f"{product_id}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    vehicle = conn.execute("INSERT INTO vehicles(market_name, announcement_model_code, catalog_name, catalog_batch, "
                           "catalog_category) VALUES ('', ?, '', '', '')", (product_id,)).lastrowid
    announcement = conn.execute(
        "INSERT INTO announcements(source_product_id, vehicle_id, model_code, raw_json, first_seen_at) "
        "VALUES (?, ?, ?, '{}', '2026-09-01')", (product_id, vehicle, fields.get("model_code", ""))).lastrowid
    conn.execute("INSERT INTO documents(announcement_id, relative_path, is_pdf, bytes, sha256, downloaded_at) "
                 "VALUES (?, ?, 1, ?, ?, '2026-09-01')",
                 (announcement, path.relative_to(root).as_posix(), len(content),
                  hashlib.sha256(register if register is not None else content).hexdigest()))
    conn.execute("INSERT INTO announcement_fields(announcement_id, fields_json, parse_error) VALUES (?, ?, '')",
                 (announcement, json.dumps(fields, ensure_ascii=False, sort_keys=True)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "data" / "announcement_site.sqlite"
    db.parent.mkdir()
    with closing(sqlite3.connect(db)) as conn:
        seed.ensure_schema(conn)
        add(conn, tmp_path, "STALE1", STALE, content=b"%PDF converted")
        add(conn, tmp_path, "PLAIN1", {"model_code": "BJ1045EV", "chassis": "BJ1045"}, content=b"%PDF plain")
        add(conn, tmp_path, "SWAPPED", dict(STALE, model_code="SWAP"), content=b"%PDF converted swapped",
            register=b"registered version")
        add(conn, tmp_path, "BROKEN", dict(STALE, model_code="BROKEN"), content=b"%PDF broken")
        conn.commit()
    parsed = {"STALE1": CONVERTED, "PLAIN1": {"model_code": "BJ1045EV", "chassis": "BJ1045"},
              "SWAPPED": dict(CONVERTED, model_code="SWAP")}

    def parser(path: Path):
        if path.stem == "BROKEN":
            raise ValueError("改装车底盘表缺少必要表头")
        return dict(parsed[path.stem])

    refreshes = []
    monkeypatch.setattr(seed, "refresh_collection_status", lambda *args, **kwargs: refreshes.append(kwargs))
    return {"db": db, "root": tmp_path, "parser": parser, "refreshes": refreshes}


def fields(db: Path) -> dict[str, dict]:
    with closing(sqlite3.connect(db)) as conn:
        return {pid: json.loads(value) for pid, value in conn.execute(
            "SELECT a.source_product_id, f.fields_json FROM announcement_fields f "
            "JOIN announcements a ON a.id = f.announcement_id")}


def execute(env, *, apply: bool, limit=None):
    return reparse.run(env["db"], layout="converted_vehicle", apply=apply, pdf_root=env["root"],
                       catalog_db=env["root"] / "catalog.sqlite", runs_dir=env["root"] / "runs",
                       parser=env["parser"], limit=limit,
                       now=lambda: datetime(2026, 9, 26, tzinfo=timezone.utc))


def test_preview_reports_changes_without_writing(env):
    before = fields(env["db"])
    result = execute(env, apply=False)
    summary = json.loads(result["summary"].read_text(encoding="utf-8"))
    assert fields(env["db"]) == before
    assert summary["mode"] == "preview" and summary["changed"] == 1
    assert summary["skipped"] == {"hash_mismatch": 1, "parse_error": 1}
    assert set(summary["changed_keys"]) == {"chassis", "chassis_references", "pdf_layout", "source_file", "vin"}
    change = json.loads((result["summary"].parent / "changes.jsonl").read_text(encoding="utf-8"))
    assert change["before"]["vin"] == "13803677SH1047PCEVNZY52341" and change["after"]["vin"] == ""
    assert json.loads(change["original_fields_json"]) == STALE  # 可按原样恢复
    assert env["refreshes"] == []


def test_apply_replaces_parser_keys_and_keeps_other_sources(env):
    result = execute(env, apply=True)
    assert result["applied"] == 1
    stored = fields(env["db"])
    assert stored["STALE1"]["chassis"] == "SH1047PCEVNZY5"
    assert stored["STALE1"]["pdf_layout"] == "converted_vehicle"
    assert stored["STALE1"]["vin"] == ""
    assert stored["STALE1"]["range_km"] == "260"  # 目录补充键原样保留
    # 标准版式、登记哈希不符和解析失败的记录都不动
    assert stored["PLAIN1"] == {"model_code": "BJ1045EV", "chassis": "BJ1045"}
    assert stored["SWAPPED"]["chassis"] == "二类纯电动载货汽车底盘"
    assert stored["BROKEN"]["chassis"] == "二类纯电动载货汽车底盘"
    assert len(env["refreshes"]) == 1 and env["refreshes"][0]["reason"] == "reparse"
    # 再跑一次已没有待刷新记录，也不再刷新统计
    again = execute(env, apply=True)
    assert again["applied"] == 0 and len(env["refreshes"]) == 1


def test_apply_refuses_while_a_collection_run_is_open(env):
    with closing(sqlite3.connect(env["db"])) as conn:
        conn.execute("INSERT INTO ingestion_runs(started_at, selector_json, selected_models) VALUES ('x', '{}', 1)")
        conn.commit()
    with pytest.raises(RuntimeError, match="未完成采集"):
        execute(env, apply=True)
    assert fields(env["db"])["STALE1"]["chassis"] == "二类纯电动载货汽车底盘"


def test_concurrent_change_rolls_back_the_whole_batch(env, monkeypatch):
    original = reparse.build_plan

    def plan_then_edit(conn, *args, **kwargs):
        plan = original(conn, *args, **kwargs)
        conn.execute("UPDATE announcement_fields SET fields_json = '{\"changed\": 1}' WHERE announcement_id = "
                     "(SELECT id FROM announcements WHERE source_product_id = 'STALE1')")
        return plan

    monkeypatch.setattr(reparse, "build_plan", plan_then_edit)
    with pytest.raises(RuntimeError, match="整体回滚"):
        execute(env, apply=True)
    assert env["refreshes"] == []


def test_main_entry_dispatches_reparse_preview(monkeypatch):
    calls = []
    monkeypatch.setattr(reparse, "main", lambda argv: calls.append(argv) or 0)
    assert entry.main(["gonggao", "reparse", "--limit", "5"]) == 0
    assert calls == [["--limit", "5"]]


def test_backup_failure_prevents_database_write(env, monkeypatch):
    before = fields(env["db"])

    def fail_backup(*args, **kwargs):
        raise OSError("cannot persist rollback fields")

    monkeypatch.setattr(reparse, "write_run", fail_backup)
    with pytest.raises(OSError, match="rollback fields"):
        execute(env, apply=True)
    assert fields(env["db"]) == before
    assert env["refreshes"] == []


def test_original_fields_and_error_are_saved_before_apply(env, monkeypatch):
    with closing(sqlite3.connect(env["db"])) as conn:
        conn.execute("UPDATE announcement_fields SET parse_error='old parser failed' "
                     "WHERE announcement_id=(SELECT id FROM announcements WHERE source_product_id='STALE1')")
        conn.commit()
    original = reparse.apply_plan

    def check_backup_then_apply(conn, plan):
        saved = list((env["root"] / "runs").glob("*/changes.jsonl"))
        assert len(saved) == 1
        record = json.loads(saved[0].read_text(encoding="utf-8"))
        assert json.loads(record["original_fields_json"]) == STALE
        assert record["original_parse_error"] == "old parser failed"
        summary = json.loads(saved[0].with_name("summary.json").read_text(encoding="utf-8"))
        assert summary["phase"] == "prepared" and "applied" not in summary
        return original(conn, plan)

    monkeypatch.setattr(reparse, "apply_plan", check_backup_then_apply)
    result = execute(env, apply=True)
    summary = json.loads(result["summary"].read_text(encoding="utf-8"))
    assert summary["phase"] == "completed" and summary["applied"] == 1
    assert len(list((env["root"] / "runs").iterdir())) == 1


def test_changed_document_registration_prevents_stale_write(env, monkeypatch):
    original = reparse.build_plan

    def plan_then_replace_document(conn, *args, **kwargs):
        plan = original(conn, *args, **kwargs)
        with closing(sqlite3.connect(env["db"])) as other:
            other.execute("UPDATE documents SET sha256='new version' WHERE announcement_id=?",
                          (plan.changes[0]["announcement_id"],))
            other.commit()
        return plan

    before = fields(env["db"])
    monkeypatch.setattr(reparse, "build_plan", plan_then_replace_document)
    with pytest.raises(RuntimeError, match="文档登记.*整体回滚"):
        execute(env, apply=True)
    assert fields(env["db"]) == before
    assert env["refreshes"] == []
    saved = next((env["root"] / "runs").glob("*/summary.json"))
    assert json.loads(saved.read_text(encoding="utf-8"))["phase"] == "failed"


def test_later_conflict_rolls_back_earlier_field_updates(env):
    with closing(sqlite3.connect(env["db"])) as conn:
        plan = reparse.build_plan(conn, env["root"], "converted_vehicle", limit=2,
                                  parser=lambda path: dict(CONVERTED))
        assert len(plan.changes) == 2
        second_id = plan.changes[1]["announcement_id"]
        conn.execute("UPDATE announcement_fields SET fields_json='{}' WHERE announcement_id=?", (second_id,))
        conn.commit()
        with pytest.raises(RuntimeError, match="整体回滚"):
            reparse.apply_plan(conn, plan)
    # 第一条虽先执行 UPDATE，第二条冲突后也必须恢复；外部已提交的第二条仍保留。
    assert fields(env["db"])["STALE1"] == STALE
    assert fields(env["db"])["PLAIN1"] == {}


def test_parser_keys_cover_every_parsed_field():
    words = []  # 空页面也会产出的派生键
    produced = set(reparse.review_export.parse_words(words))
    assert produced <= reparse.PARSER_KEYS
    assert {"range_km", "common_name", "battery_energy", "fuel_consumption"}.isdisjoint(reparse.PARSER_KEYS)
