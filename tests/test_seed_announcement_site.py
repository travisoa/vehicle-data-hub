"""网站采集脚本的上游路径契约与正式发布重发刷新测试。"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone
import sqlite3
import sys
from pathlib import Path

import pytest

from miit_gonggao import collection as seed


@pytest.fixture(autouse=True)
def isolate_persistent_status(monkeypatch):
    monkeypatch.setattr(seed, "refresh_collection_status", lambda *_args, **_kwargs: None)


def test_database_lock_resolves_legacy_symlink(tmp_path):
    target = tmp_path / "upstream.sqlite"
    target.touch()
    legacy = tmp_path / "website.sqlite"
    legacy.symlink_to(target)
    with seed.database_lock(target), pytest.raises(RuntimeError, match="持有锁"):
        with seed.database_lock(legacy):
            pytest.fail("同一业务库的兼容路径不能绕过互斥锁")


def test_unified_cli_routes_manifest_and_catalog_without_network(monkeypatch):
    from miit_gonggao import collection_manifest, core

    calls = []
    monkeypatch.setattr(collection_manifest, "main", lambda args: calls.append(("manifest", args)) or 7)
    monkeypatch.setattr(seed, "catalog_main", lambda args: calls.append(("catalog", args)) or 8)
    assert core.main(["collect", "--manifest=approved.json"]) == 7
    assert core.main(["collect", "-f", "models.txt"]) == 8
    assert calls == [("manifest", ["--manifest=approved.json"]), ("catalog", ["-f", "models.txt"])]


def make_catalog_db(path: Path, *, model_code: str = "ABC6500EV") -> Path:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE catalog_rows (id INTEGER PRIMARY KEY, catalog TEXT, batch TEXT, "
            "category TEXT, seq TEXT, company TEXT, trademark TEXT, model_code TEXT, "
            "common_name TEXT)"
        )
        conn.execute(
            "INSERT INTO catalog_rows(catalog, batch, category, seq, company, trademark, "
            "model_code, common_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seed.CATALOG_NAME,
                "32",
                "乘用车",
                "1",
                "示例汽车有限公司",
                "示例牌",
                model_code,
                "示例车",
            ),
        )
    return path


def seed_local_announcement(site_db: Path, upstream_root: Path, *, batch: str = "407",
                            product_id: str = "product-407", model_code: str = "ABC6500EV") -> None:
    """本地已有一份有效参数页：只有这种产品才谈得上被更高批次重发后刷新。"""
    relative = f"downloads/announcement_site/示例牌/{model_code}/第{batch}批/{product_id}.pdf"
    path = upstream_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\nlocal-copy")
    with closing(sqlite3.connect(site_db)) as conn, conn:
        conn.executescript(seed.SCHEMA)
        conn.execute(
            "INSERT INTO vehicles(market_name, announcement_model_code, catalog_name, catalog_batch, "
            "catalog_category, catalog_seq, catalog_company) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("示例车", model_code, seed.CATALOG_NAME, "32", "乘用车", "1", "示例汽车有限公司"),
        )
        conn.execute("INSERT OR IGNORE INTO batches(batch) VALUES (?)", (batch,))
        conn.execute(
            "INSERT INTO announcements(source_product_id, vehicle_id, batch_id, company, trademark, "
            "model_code, product_name, raw_json, first_seen_at) SELECT ?, v.id, b.id, ?, ?, ?, ?, '{}', ? "
            "FROM vehicles v, batches b WHERE v.announcement_model_code=? AND b.batch=?",
            (product_id, "示例汽车有限公司", "示例牌", model_code, "多用途乘用车",
             "2026-09-12T00:00:00+00:00", model_code, batch),
        )
        conn.execute(
            "INSERT INTO documents(announcement_id, relative_path, is_pdf, bytes, sha256, downloaded_at, error) "
            "SELECT id, ?, 1, ?, 'x', ?, '' FROM announcements WHERE source_product_id=?",
            (relative, path.stat().st_size, "2026-09-12T00:00:00+00:00", product_id),
        )


def write_batch_cache(cache_dir: Path, batch: int, *, product_id: str = "product-407",
                      model_code: str = "ABC6500EV",
                      extra: tuple[tuple[str, str], ...] = ()) -> None:
    """正式批次枚举缓存：同一产品 ID 在更高批次再次出现即为重新发布。"""
    from scripts import announcement_catalog_gap as gap

    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"batch{batch}.json").write_text(json.dumps({
        "batch": batch, "cache_version": gap.CACHE_VERSION, "complete": True, "failures": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "verify_prefixes": list(gap.VERIFY_PREFIXES), "verify_missed": 0,
        "products": [{"cpid": pid, "clxh": code, "clmc": "多用途乘用车",
                      "gppc": str(batch), "pc": str(batch), "dataTag": "Z",
                      "cpsb": "示例牌", "qymc": "示例汽车有限公司"}
                     for pid, code in ((product_id, model_code), *extra)],
    }, ensure_ascii=False), encoding="utf-8")


def run_republished_ingestion(
    monkeypatch,
    tmp_path: Path,
    *,
    announcement_batch: str,
    recorded_batch: int = 409,
    download=None,
    parse=None,
    extra_args: tuple[str, ...] = (),
) -> tuple[int, Path, Path]:
    catalog_db = make_catalog_db(tmp_path / "catalog.sqlite")
    site_db = tmp_path / "site.sqlite"
    upstream_root = tmp_path / "vehicle-data-hub"
    download_root = upstream_root / "downloads" / "announcement_site"
    monkeypatch.setattr(seed, "UPSTREAM_ROOT", upstream_root)
    seed_local_announcement(site_db, upstream_root)
    write_batch_cache(upstream_root / "downloads" / "announcement_batches", recorded_batch)
    monkeypatch.setattr(
        seed.core,
        "query_all_pages",
        lambda **_kwargs: [
            {
                "clxh": "ABC6500EV",
                "clmc": "多用途乘用车",
                "qymc": "示例汽车有限公司",
                "cpsb": "示例牌",
                "gppc": announcement_batch,
                # 官方重发沿用同一产品 ID——正是重发判据的前提，也是刷新能收敛的原因。
                "cpid": "product-407",
                "dataTag": "Z",
            }
        ],
    )
    if download is not None:
        monkeypatch.setattr(seed.core, "download_param_page", download)
        monkeypatch.setattr(
            seed, "parse_pdf", parse or (lambda _path, _catalog: ({"model_code": "ABC6500EV"}, ""))
        )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "seed_announcement_site.py",
            "--republished-batch",
            str(recorded_batch),
            "--limit",
            "10",
            "--catalog-db",
            str(catalog_db),
            "--site-db",
            str(site_db),
            "--download-root",
            str(download_root),
            *extra_args,
        ],
    )
    return seed.main(), site_db, upstream_root


def test_relative_pdf_path_is_always_based_on_upstream_root(tmp_path: Path):
    upstream = tmp_path / "vehicle-data-hub"
    pdf = upstream / "downloads" / "announcement_site" / "demo.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4")
    assert seed.relative_pdf_path(pdf, upstream) == "downloads/announcement_site/demo.pdf"


def test_existing_business_db_gets_the_awaiting_effective_column(tmp_path: Path):
    db = tmp_path / "legacy.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE ingestion_runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, "
            "completed_at TEXT, selector_json TEXT NOT NULL, selected_models INTEGER NOT NULL, "
            "query_failures INTEGER NOT NULL DEFAULT 0, download_failures INTEGER NOT NULL DEFAULT 0, "
            "non_pdf_documents INTEGER NOT NULL DEFAULT 0)"
        )
        seed.ensure_schema(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ingestion_runs)")}
    assert "awaiting_effective" in columns


def test_republished_refresh_stops_when_the_interface_batch_is_lower(monkeypatch, tmp_path: Path):
    """清单记录第409批重发，接口却只给第408批：不能拿更旧的一版覆盖本地参数页。"""
    exit_code, site_db, upstream_root = run_republished_ingestion(
        monkeypatch, tmp_path, announcement_batch="408"
    )
    assert exit_code == 2
    with sqlite3.connect(site_db) as conn:
        assert conn.execute("SELECT awaiting_effective FROM ingestion_runs").fetchone()[0] == 1
        status, error = conn.execute(
            "SELECT status, error FROM run_models ORDER BY run_id DESC LIMIT 1").fetchone()
        assert status == "awaiting_effective"
        assert "当前最高为第408批" in error
        # 本地那份第407批的记录仍是唯一一条，没有被更旧的接口结果改写。
        batches = [row[0] for row in conn.execute(
            "SELECT b.batch FROM announcements a JOIN batches b ON b.id=a.batch_id")]
        assert batches == ["407"]
    assert list((upstream_root / "downloads" / "announcement_site" / "_snapshots").glob("*.json"))


def test_republished_refresh_ingests_the_new_batch_and_records_provenance(monkeypatch, tmp_path: Path):
    def fake_download(_row, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "示例牌_ABC6500EV_409.pdf"
        path.write_bytes(b"%PDF-1.4\nwebsite-test")
        return path, True, path.stat().st_size

    exit_code, site_db, upstream_root = run_republished_ingestion(
        monkeypatch, tmp_path, announcement_batch="409", download=fake_download
    )
    assert exit_code == 0
    with sqlite3.connect(site_db) as conn:
        relative_path, batch = conn.execute(
            "SELECT d.relative_path, b.batch FROM documents d "
            "JOIN announcements a ON a.id=d.announcement_id JOIN batches b ON b.id=a.batch_id "
            "WHERE a.source_product_id='product-407'").fetchone()
        raw_json = json.loads(conn.execute(
            "SELECT raw_json FROM announcements WHERE source_product_id='product-407'").fetchone()[0])
        assert relative_path.startswith("downloads/announcement_site/")
        assert raw_json["_republished"]["recorded_batch"] == "409"
        assert raw_json["_republished"]["local_batch"] == "407"
        # 同一产品 ID 被就地刷新到新批次，而不是另起一行。
        assert batch == "409"
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 1
        assert conn.execute("SELECT awaiting_effective FROM ingestion_runs").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM run_models ORDER BY run_id DESC LIMIT 1").fetchone()[0] == "done"
    assert (upstream_root / relative_path).read_bytes().startswith(b"%PDF")
    # 刷新后本地批次已追平，同一份缓存不再把它列为候选：重复执行会收敛，不会反复下载。
    again = seed.republished.from_batches(
        site_db, [409], upstream_root / "downloads" / "announcement_batches"
    )
    assert again.rows == []


def test_request_pace_only_covers_this_run_and_is_restored_afterwards(monkeypatch, tmp_path: Path):
    """提速是本进程的临时覆盖，采集一结束就要恢复 core 的默认间隔。"""
    default = seed.core.REQUEST_MIN_INTERVAL
    during: list[tuple[float, float]] = []

    def fake_download(_row, folder: Path):
        during.append(seed.core.REQUEST_MIN_INTERVAL)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "示例牌_ABC6500EV_409.pdf"
        path.write_bytes(b"%PDF-1.4\npaced")
        return path, True, path.stat().st_size

    exit_code, *_ = run_republished_ingestion(
        monkeypatch, tmp_path, announcement_batch="409", download=fake_download,
        extra_args=("--min-interval", "0.2", "--max-interval", "0.4"))
    assert exit_code == 0
    assert during == [(0.2, 0.4)]
    assert seed.core.REQUEST_MIN_INTERVAL == default


def test_download_failure_drops_the_run_back_to_the_default_interval(monkeypatch, tmp_path: Path, capsys):
    """下载失败是服务端压力信号：本轮后续请求必须退回默认节奏并说明。"""
    default = seed.core.REQUEST_MIN_INTERVAL

    def failing_download(_row, _folder: Path):
        raise OSError("connection reset")

    exit_code, *_ = run_republished_ingestion(
        monkeypatch, tmp_path, announcement_batch="409", download=failing_download,
        extra_args=("--min-interval", "0.2", "--max-interval", "0.4"))
    assert exit_code == 2
    printed = capsys.readouterr().out
    assert "download_failed" in printed and "恢复默认间隔" in printed
    assert seed.core.REQUEST_MIN_INTERVAL == default


def test_partial_interval_is_rejected_before_any_collection(monkeypatch, tmp_path: Path):
    """只给一半区间是配置错误，必须在采集开始前拒绝。"""
    monkeypatch.setattr(seed.core, "query_all_pages",
                        lambda **_kwargs: pytest.fail("参数错误时不得查询官方接口"))
    with pytest.raises(SystemExit) as exc:
        run_republished_ingestion(monkeypatch, tmp_path, announcement_batch="409",
                                  extra_args=("--min-interval", "0.2"))
    assert exc.value.code == 2


def test_republished_dry_run_lists_candidates_without_touching_the_network_or_db(monkeypatch, tmp_path: Path):
    catalog_db = make_catalog_db(tmp_path / "catalog.sqlite")
    site_db = tmp_path / "site.sqlite"
    upstream_root = tmp_path / "vehicle-data-hub"
    monkeypatch.setattr(seed, "UPSTREAM_ROOT", upstream_root)
    seed_local_announcement(site_db, upstream_root)
    write_batch_cache(upstream_root / "downloads" / "announcement_batches", 409)
    monkeypatch.setattr(seed.core, "query_all_pages",
                        lambda **_kwargs: pytest.fail("预演不得查询官方接口"))
    monkeypatch.setattr(seed.core, "download_param_page",
                        lambda *_args, **_kwargs: pytest.fail("预演不得下载 PDF"))
    monkeypatch.setattr(sys, "argv", [
        "seed_announcement_site.py", "--republished-batch", "409", "--dry-run",
        "--catalog-db", str(catalog_db), "--site-db", str(site_db),
        "--download-root", str(upstream_root / "downloads" / "announcement_site"),
    ])
    assert seed.main() == 0
    snapshots = list((upstream_root / "downloads" / "announcement_site" /
                      seed.core.ANNOUNCEMENT_SNAPSHOT_DIRNAME).glob("republished_seed_*.json"))
    assert len(snapshots) == 1
    payload = json.loads(snapshots[0].read_text(encoding="utf-8"))
    assert [row["product_id"] for row in payload["rows"]] == ["product-407"]
    assert payload["rows"][0]["republished_batch"] == 409
    with sqlite3.connect(site_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 1


def test_republished_limit_reports_the_round_not_the_whole_candidate_list(monkeypatch, tmp_path: Path, capsys):
    """--limit 截断后，预演统计和快照都必须描述本轮实际处理范围。"""
    catalog_db = make_catalog_db(tmp_path / "catalog.sqlite")
    site_db = tmp_path / "site.sqlite"
    upstream_root = tmp_path / "vehicle-data-hub"
    monkeypatch.setattr(seed, "UPSTREAM_ROOT", upstream_root)
    seed_local_announcement(site_db, upstream_root)
    seed_local_announcement(site_db, upstream_root, product_id="product-407b", model_code="ABC6500EV2")
    write_batch_cache(upstream_root / "downloads" / "announcement_batches", 409,
                      extra=(("product-407b", "ABC6500EV2"),))
    monkeypatch.setattr(seed.core, "query_all_pages",
                        lambda **_kwargs: pytest.fail("预演不得查询官方接口"))
    monkeypatch.setattr(sys, "argv", [
        "seed_announcement_site.py", "--republished-batch", "409", "--dry-run", "--limit", "1",
        "--catalog-db", str(catalog_db), "--site-db", str(site_db),
        "--download-root", str(upstream_root / "downloads" / "announcement_site"),
    ])
    assert seed.main() == 0
    printed = capsys.readouterr().out
    assert "本轮清单 1 个产品（候选共 2 个）" in printed
    assert "覆盖 1 个型号" in printed
    assert "其余 1 个留待后续轮次" in printed
    assert "第409批 1 个" in printed  # 批次分布按本轮清单统计，不是全部候选
    payload = json.loads(next(
        (upstream_root / "downloads" / "announcement_site" /
         seed.core.ANNOUNCEMENT_SNAPSHOT_DIRNAME).glob("republished_seed_*.json")
    ).read_text(encoding="utf-8"))
    assert payload["total"] == 1 and payload["total_candidates"] == 2
    assert [row["product_id"] for row in payload["rows"]] == ["product-407"]
    assert len(payload["selected_models"]) == 1


def test_republished_refresh_keeps_the_existing_catalog_identity(tmp_path: Path):
    """已有车型的目录归属由此前采集定下，重发只换参数页，不能被目录库的另一条记录改写。

    同型号常同时出现在购置税目录和车船税目录里，后者批次更高。按批次取目录会把
    catalog_category 改成「插电式混合动力乘用车」这类值，分类硬链接随即建不出来。
    """
    catalog_db = tmp_path / "catalog.sqlite"
    with sqlite3.connect(catalog_db) as conn:
        conn.execute("CREATE TABLE catalog_rows (id INTEGER PRIMARY KEY, catalog TEXT, batch TEXT, "
                     "category TEXT, seq TEXT, company TEXT, trademark TEXT, model_code TEXT, "
                     "common_name TEXT)")
        conn.execute("INSERT INTO catalog_rows(catalog,batch,category,seq,company,trademark,"
                     "model_code,common_name) VALUES ('减免车辆购置税的新能源汽车车型目录','31',"
                     "'乘用车','1','示例汽车有限公司','示例牌','ABC6500EV','示例车')")
        conn.execute("INSERT INTO catalog_rows(catalog,batch,category,seq,company,trademark,"
                     "model_code,common_name) VALUES ('享受车船税减免优惠的节约能源汽车车型目录','86',"
                     "'插电式混合动力乘用车','9','示例汽车有限公司','示例牌','ABC6500EV','示例车')")
    site_db = tmp_path / "site.sqlite"
    with sqlite3.connect(site_db) as conn:
        conn.executescript(seed.SCHEMA)
        conn.execute(
            "INSERT INTO vehicles(market_name, announcement_model_code, catalog_name, catalog_batch, "
            "catalog_category, catalog_seq, catalog_company) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("示例车", "ABC6500EV", "减免车辆购置税的新能源汽车车型目录", "31", "乘用车", "1", "示例汽车有限公司"),
        )
    row = {"product_id": "product-407", "model_code": "ABC6500EV", "company": "示例汽车有限公司",
           "trademark": "示例牌", "product_name": "多用途乘用车",
           "republished_batch": 409, "local_batch": 407}
    selected = seed.select_republished_models(catalog_db, site_db, [row])
    assert len(selected) == 1
    # 分类硬链接只认精确的「乘用车」及商用车类别，批次更高的车船税目录不得顶掉它。
    assert selected[0]["category"] == "乘用车"
    assert selected[0]["catalog"] == "减免车辆购置税的新能源汽车车型目录"
    assert selected[0]["batch"] == "31"


def test_verify_separates_real_republication_from_cache_noise():
    """批次枚举与按型号查询是两个接口，口径不一定一致；核实要能分出四种结局。"""
    rows = [
        {"model_code": "AA1", "republished_batch": 409, "product_id": "p1"},
        {"model_code": "BB2", "republished_batch": 382, "product_id": "p2"},
        {"model_code": "CC3", "republished_batch": 409, "product_id": "p3"},
        {"model_code": "DD4", "republished_batch": 409, "product_id": "p4"},
    ]

    def fake_query(*, model_code: str, page_size: int):
        if model_code == "AA1":  # 接口已给到记录批次：真重发
            return [{"clxh": "AA1", "gppc": "407"}, {"clxh": "AA1", "gppc": "409"}]
        if model_code == "BB2":  # 缓存说 382，接口最高只有 380：记录与源不符
            return [{"clxh": "BB2", "gppc": "380"}]
        if model_code == "CC3":  # 只有近似型号，精确型号查不到
            return [{"clxh": "CC3-L", "gppc": "409"}]
        raise RuntimeError("接口超时")

    result = seed.republished.verify_against_api(rows, query=fake_query)
    assert [row["model_code"] for row in result["confirmed"]] == ["AA1"]
    assert [row["model_code"] for row in result["stale_record"]] == ["BB2"]
    assert [row["model_code"] for row in result["missing"]] == ["CC3"]
    assert [row["model_code"] for row in result["failed"]] == ["DD4"]
    assert result["confirmed"][0]["api_latest_batch"] == 409
    assert result["stale_record"][0]["api_latest_batch"] == 380
    assert result["missing"][0]["api_batches"] == []
    # 查询失败是"未核实"，不能被当成任何一种结论
    assert "接口超时" in result["failed"][0]["verify_error"]
    assert "api_latest_batch" not in result["failed"][0]


def test_verify_needs_dry_run_because_collection_already_checks(monkeypatch, tmp_path: Path):
    """实际采集本身逐条核实接口批次，再单独查一遍只是重复请求。"""
    monkeypatch.setattr(seed.core, "query_all_pages",
                        lambda **_kwargs: pytest.fail("参数错误时不得查询官方接口"))
    with pytest.raises(SystemExit) as exc:
        run_republished_ingestion(monkeypatch, tmp_path, announcement_batch="409",
                                  extra_args=("--verify",))
    assert exc.value.code == 2


def test_dry_run_verify_records_the_api_check_in_one_snapshot(monkeypatch, tmp_path: Path):
    """核实结果与清单写在同一份快照里，且全程不下载。"""
    catalog_db = make_catalog_db(tmp_path / "catalog.sqlite")
    site_db = tmp_path / "site.sqlite"
    upstream_root = tmp_path / "vehicle-data-hub"
    monkeypatch.setattr(seed, "UPSTREAM_ROOT", upstream_root)
    seed_local_announcement(site_db, upstream_root)
    write_batch_cache(upstream_root / "downloads" / "announcement_batches", 409)
    # 缓存说第 409 批重发，接口最高只到 407：这条是枚举缓存的假阳性
    monkeypatch.setattr(seed.core, "query_all_pages",
                        lambda **_kwargs: [{"clxh": "ABC6500EV", "gppc": "407"}])
    monkeypatch.setattr(seed.core, "download_param_page",
                        lambda *_args, **_kwargs: pytest.fail("核实不得下载 PDF"))
    monkeypatch.setattr(sys, "argv", [
        "seed_announcement_site.py", "--republished-batch", "409", "--dry-run", "--verify",
        "--catalog-db", str(catalog_db), "--site-db", str(site_db),
        "--download-root", str(upstream_root / "downloads" / "announcement_site"),
    ])
    assert seed.main() == 0
    snapshots = list((upstream_root / "downloads" / "announcement_site" /
                      seed.core.ANNOUNCEMENT_SNAPSHOT_DIRNAME).glob("republished_seed_*.json"))
    assert len(snapshots) == 1  # 核实不另起一份快照
    payload = json.loads(snapshots[0].read_text(encoding="utf-8"))
    assert payload["verification"]["counts"] == {
        "confirmed": 0, "stale_record": 1, "missing": 0, "failed": 0}
    assert payload["verification"]["stale_record"][0]["api_latest_batch"] == 407
    with sqlite3.connect(site_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 0


def test_republished_models_are_not_excluded_when_already_in_the_site_db(tmp_path: Path):
    catalog_db = make_catalog_db(tmp_path / "catalog.sqlite")
    site_db = tmp_path / "site.sqlite"
    with sqlite3.connect(site_db) as conn:
        conn.executescript(seed.SCHEMA)
        conn.execute(
            "INSERT INTO vehicles(market_name, announcement_model_code, catalog_name, catalog_batch, "
            "catalog_category, catalog_seq, catalog_company) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("旧车型名", "ABC6500EV", seed.CATALOG_NAME, "31", "乘用车", "1", "示例企业"),
        )
    row = {"product_id": "product-407", "model_code": "ABC6500EV", "company": "示例汽车有限公司",
           "trademark": "示例牌", "product_name": "多用途乘用车",
           "republished_batch": 409, "local_batch": 407}
    selected = seed.select_republished_models(catalog_db, site_db, [row, dict(row)])
    assert len(selected) == 1
    assert selected[0]["model_code"] == "ABC6500EV"
    assert selected[0]["republished_batch"] == "409"


def test_candidate_lookup_does_not_leak_sqlite_connections(tmp_path):
    """`with sqlite3.connect(...)` 只管事务不关连接；重发刷新模式逐行调用这两个
    查询，泄漏的连接会随清单长度一直累积。"""
    catalog_db = make_catalog_db(tmp_path / "catalog.sqlite")
    site_db = tmp_path / "site.sqlite"
    with sqlite3.connect(site_db) as site:
        site.execute(
            "CREATE TABLE vehicles (market_name TEXT, announcement_model_code TEXT, "
            "catalog_name TEXT, catalog_batch TEXT, catalog_category TEXT, catalog_seq TEXT, "
            "catalog_company TEXT)"
        )

    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    sqlite3.connect = tracking_connect
    try:
        for _ in range(5):
            seed._catalog_item(catalog_db, "ABC6500EV")
            seed._existing_vehicle_item(site_db, "NOT-IN-DB")
    finally:
        sqlite3.connect = real_connect

    assert len(opened) == 10
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("select 1")


@pytest.mark.parametrize('failure', ['network', 'html', 'parse', 'publish'])
def test_failed_refresh_preserves_complete_previous_version(monkeypatch, tmp_path, failure):
    conn = sqlite3.connect(tmp_path / 'source.sqlite')
    seed.ensure_schema(conn)
    vehicle = seed.upsert_vehicle(conn, dict(common_name='demo', model_code='ABC6500EV',
        catalog='catalog', batch='32', category='乘用车', seq='1', company='demo'))
    row = dict(cpid='same-id', clxh='ABC6500EV', clmc='old', cpsb='brand', gppc='408')
    kwargs = dict(conn=conn, vehicle_id=vehicle, market_name='demo', download_root=tmp_path / 'downloads',
                  pdf_root=tmp_path, catalog_db=tmp_path / 'catalog.sqlite')

    def download(_row, folder):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'brand_ABC6500EV_408_same-id.pdf'
        path.write_bytes(b'%PDF old')
        return path, True, path.stat().st_size

    monkeypatch.setattr(seed.core, 'download_param_page', download)
    monkeypatch.setattr(seed, 'parse_pdf', lambda *_: ({'batch': '408', 'gross_mass': '3000'}, ''))
    assert seed.store_announcement(row=row, **kwargs) == (True, '')
    conn.commit()
    old_path = tmp_path / conn.execute('SELECT relative_path FROM documents').fetchone()[0]
    old_state = [conn.execute(f'SELECT * FROM {table}').fetchall()
                 for table in ['announcements', 'documents', 'announcement_fields']]

    def failed_download(_row, folder):
        if failure == 'network':
            raise OSError('offline')
        path = folder / old_path.name
        path.write_bytes(b'%PDF new' if failure in ('parse', 'publish') else b'<html>unavailable</html>')
        return path, failure in ('parse', 'publish'), path.stat().st_size

    monkeypatch.setattr(seed.core, 'download_param_page', failed_download)
    monkeypatch.setattr(seed, 'parse_pdf', lambda *_: ({}, 'invalid layout'))
    if failure == 'publish':
        def fail_link(*_):
            raise OSError('publish blocked')

        monkeypatch.setattr(seed.os, 'link', fail_link)
    assert not seed.store_announcement(row={**row, 'gppc': '409', 'clmc': 'new'}, **kwargs)[0]
    conn.commit()
    assert old_state == [conn.execute(f'SELECT * FROM {table}').fetchall()
                         for table in ['announcements', 'documents', 'announcement_fields']]
    assert old_path.read_bytes() == b'%PDF old'
    conn.close()


def test_successful_same_path_refresh_preserves_old_pdf(monkeypatch, tmp_path):
    conn = sqlite3.connect(tmp_path / 'source.sqlite')
    seed.ensure_schema(conn)
    vehicle = seed.upsert_vehicle(conn, dict(common_name='demo', model_code='ABC6500EV',
        catalog='catalog', batch='32', category='乘用车', seq='1', company='demo'))
    row = dict(cpid='same-id', clxh='ABC6500EV', clmc='demo', cpsb='brand', gppc='408')
    content = b'%PDF old'

    def download(_row, folder):
        path = folder / 'brand_ABC6500EV_408_same-id.pdf'
        path.write_bytes(content)
        return path, True, len(content)

    monkeypatch.setattr(seed.core, 'download_param_page', download)
    monkeypatch.setattr(seed, 'parse_pdf', lambda *_: ({'gross_mass': str(len(content))}, ''))
    kwargs = dict(conn=conn, row=row, vehicle_id=vehicle, market_name='demo',
                  download_root=tmp_path / 'downloads', pdf_root=tmp_path, catalog_db=tmp_path / 'cat.sqlite')
    assert seed.store_announcement(**kwargs)[0]
    old_path = tmp_path / conn.execute('SELECT relative_path FROM documents').fetchone()[0]
    content = b'%PDF new revision'
    assert seed.store_announcement(**kwargs)[0]
    new_path = tmp_path / conn.execute('SELECT relative_path FROM documents').fetchone()[0]
    assert old_path != new_path
    assert old_path.read_bytes() == b'%PDF old'
    assert new_path.read_bytes() == content
    assert json.loads(conn.execute('SELECT fields_json FROM announcement_fields').fetchone()[0]) == {
        'gross_mass': str(len(content))}
    conn.commit()
    saved = [conn.execute(f'SELECT * FROM {table}').fetchall()
             for table in ['announcements', 'documents', 'announcement_fields']]
    conn.execute("CREATE TRIGGER reject_document BEFORE UPDATE ON documents "
                 "BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END")
    kwargs['row'] = {**row, 'gppc': '409'}
    content = b'%PDF next revision'
    with pytest.raises(sqlite3.IntegrityError, match='simulated storage failure'):
        seed.store_announcement(**kwargs)
    conn.commit()
    assert saved == [conn.execute(f'SELECT * FROM {table}').fetchall()
                     for table in ['announcements', 'documents', 'announcement_fields']]
    assert old_path.read_bytes() == b'%PDF old'
    assert new_path.read_bytes() == b'%PDF new revision'
    conn.close()


@pytest.fixture
def stored_document(monkeypatch, tmp_path):
    with closing(sqlite3.connect(tmp_path / 'source.sqlite')) as conn:
        seed.ensure_schema(conn)
        vid = seed.upsert_vehicle(conn, dict(common_name='demo', model_code='ABC6500EV',
            catalog='catalog', batch='32', category='乘用车', seq='1', company='demo'))
        conn.commit()
        kwargs = dict(conn=conn, vehicle_id=vid, market_name='demo',
            row=dict(cpid='P1', clxh='ABC6500EV', cpsb='brand', gppc='409'),
            download_root=tmp_path / 'downloads', pdf_root=tmp_path, catalog_db=tmp_path / 'catalog.sqlite')

        def download(_row, folder):
            path = folder / 'brand_ABC6500EV_409_P1.pdf'
            path.write_bytes(b'%PDF original')
            return path, True, path.stat().st_size

        monkeypatch.setattr(seed.core, 'download_param_page', download)
        monkeypatch.setattr(seed, 'parse_pdf', lambda *_: ({'model_code': 'ABC6500EV'}, ''))
        yield conn, kwargs


def test_publication_failure_preserves_raw_file_without_false_document(monkeypatch, stored_document):
    conn, kwargs = stored_document

    def fail_link(*_):
        raise OSError('simulated publish failure')

    monkeypatch.setattr(seed.os, 'link', fail_link)
    ok, error = seed.store_announcement(**kwargs)
    assert not ok and error.startswith('发布失败：')
    retained = kwargs['pdf_root'] / error.split('原文件保留于 ')[1]
    assert retained.read_bytes() == b'%PDF original'
    assert conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM announcements').fetchone()[0] == 0


@pytest.mark.parametrize('document_type', ['pdf', 'html'])
def test_unparsed_downloads_use_durable_paths_and_clean_staging(monkeypatch, stored_document, document_type):
    conn, kwargs = stored_document
    content = b'%PDF unparsed' if document_type == 'pdf' else b'<html>response</html>'

    def download(_row, folder):
        path = folder / f'brand_ABC6500EV_409_P1.{document_type}'
        path.write_bytes(content)
        return path, document_type == 'pdf', len(content)

    monkeypatch.setattr(seed.core, 'download_param_page', download)
    monkeypatch.setattr(seed, 'parse_pdf', lambda *_: ({}, 'unsupported layout'))
    assert not seed.store_announcement(**kwargs)[0]
    path, is_pdf = conn.execute('SELECT relative_path,is_pdf FROM documents').fetchone()
    assert is_pdf == int(document_type == 'pdf')
    assert '_snapshots' not in path
    durable = kwargs['pdf_root'] / path
    assert durable.read_bytes() == content
    assert durable.parent == seed.core.build_announcement_download_dir(
        kwargs['download_root'], trademark='brand', vehicle_folder='demo', batch='409')
    assert not list((kwargs['download_root'] / '_snapshots').iterdir())


@pytest.mark.parametrize('previous', ['html', 'fields_only'])
def test_failed_retry_preserves_non_pdf_evidence(monkeypatch, stored_document, previous):
    conn, kwargs = stored_document
    seed.store_announcement(**kwargs)
    conn.execute("UPDATE documents SET relative_path='old.html',is_pdf=0,bytes=?",
                 (12034 if previous == 'html' else 0,))
    conn.execute("UPDATE announcement_fields SET fields_json=?", ('{"common_name":"旧目录车型"}',))
    conn.commit()
    tables = ['announcements', 'documents', 'announcement_fields']
    before = [conn.execute(f'SELECT * FROM {table}').fetchall() for table in tables]

    def fail_download(*_):
        raise OSError('offline')

    monkeypatch.setattr(seed.core, 'download_param_page', fail_download)
    ok, error = seed.store_announcement(**{**kwargs, 'row': {**kwargs['row'], 'gppc': '410'}})
    assert not ok and error.startswith('下载失败')
    assert before == [conn.execute(f'SELECT * FROM {table}').fetchall() for table in tables]
    assert not list((kwargs['download_root'] / '_snapshots').iterdir())


@pytest.mark.parametrize('rollback', ['ABORT', 'ROLLBACK'])
def test_storage_failure_exposes_original_exception(monkeypatch, stored_document, rollback):
    conn, kwargs = stored_document
    conn.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON announcements "
                 f"BEGIN SELECT RAISE({rollback}, 'original storage failure'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match='original storage failure'):
        seed.store_announcement(**kwargs)
    assert conn.execute('SELECT count(*) FROM announcements').fetchone()[0] == 0
    assert not list((kwargs['download_root'] / '_snapshots').iterdir())


def test_successful_publish_removes_staging(stored_document):
    conn, kwargs = stored_document
    assert seed.store_announcement(**kwargs) == (True, '')
    path = kwargs['pdf_root'] / conn.execute('SELECT relative_path FROM documents').fetchone()[0]
    assert path.read_bytes() == b'%PDF original'
    assert not list((kwargs['download_root'] / '_snapshots').iterdir())


@pytest.mark.parametrize('failure', ['download', 'parse', 'publish', 'html'])
def test_run_reports_failure_phases_separately(monkeypatch, tmp_path, failure):
    def download(_row, folder):
        if failure == 'download':
            raise OSError('offline')
        path = folder / ('demo.html' if failure == 'html' else 'demo.pdf')
        path.write_bytes(b'<html>error</html>' if failure == 'html' else b'%PDF sample')
        return path, failure != 'html', path.stat().st_size

    def fail_link(*_):
        raise OSError('publish blocked')

    if failure == 'publish':
        monkeypatch.setattr(seed.os, 'link', fail_link)
    code, db, _ = run_republished_ingestion(monkeypatch, tmp_path, announcement_batch='409', download=download,
        parse=lambda *_: ({}, 'layout unsupported' if failure == 'parse' else ''))
    assert code == 2
    with closing(sqlite3.connect(db)) as conn:
        counts = conn.execute('SELECT download_failures,parse_failures,publish_failures,non_pdf_documents '
                              'FROM ingestion_runs').fetchone()
    assert counts == tuple(int(failure == phase) for phase in ['download', 'parse', 'publish', 'html'])
    report = next((db.parent / 'reports').glob('*.md')).read_text()
    assert '| 下载失败 | ' + str(int(failure == 'download')) + ' |' in report
    assert '| 解析失败 | ' + str(int(failure == 'parse')) + ' |' in report
    assert '| 发布失败 | ' + str(int(failure == 'publish')) + ' |' in report


def test_empty_failed_record_can_be_upgraded_to_html(monkeypatch, stored_document):
    conn, kwargs = stored_document

    def fail(*_):
        raise OSError('offline')

    monkeypatch.setattr(seed.core, 'download_param_page', fail)
    seed.store_announcement(**kwargs)
    assert conn.execute('SELECT bytes FROM documents').fetchone()[0] == 0

    def html(_row, folder):
        path = folder / 'demo.html'
        path.write_bytes(b'<html>response</html>')
        return path, False, path.stat().st_size

    monkeypatch.setattr(seed.core, 'download_param_page', html)
    seed.store_announcement(**kwargs)
    path, size = conn.execute('SELECT relative_path,bytes FROM documents').fetchone()
    assert path.endswith('.html') and size > 0
    assert '_snapshots' not in path


def test_migration_preserves_unknown_historical_phase_counts(tmp_path):
    from miit_gonggao.collection_report import build_report

    with closing(sqlite3.connect(tmp_path / 'legacy.sqlite')) as conn:
        conn.execute(
            "CREATE TABLE ingestion_runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, "
            "completed_at TEXT, selector_json TEXT NOT NULL, selected_models INTEGER NOT NULL, "
            "query_failures INTEGER NOT NULL DEFAULT 0, download_failures INTEGER NOT NULL DEFAULT 0, "
            "non_pdf_documents INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("INSERT INTO ingestion_runs(id,started_at,selector_json,selected_models,download_failures) "
                     "VALUES(1,'2026-09-05','{}',1,3)")
        conn.commit()
        seed.ensure_schema(conn)
        seed.ensure_schema(conn)
        assert conn.execute('SELECT download_failures,parse_failures,publish_failures '
                            'FROM ingestion_runs').fetchone() == (3, None, None)
        report = build_report(conn, 1, tmp_path / 'missing-catalog.sqlite')
        assert '| 下载/解析失败（历史合并口径） | 3 |' in report
        assert '| 解析失败 | 未单独记录 |' in report
