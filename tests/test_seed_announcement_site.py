"""网站采集脚本的上游路径契约与变更扩展公示刷新测试。"""

from __future__ import annotations

import json
from contextlib import closing
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


def source(batch: str = "409") -> seed.change_notice.ChangeNoticeSource:
    return seed.change_notice.ChangeNoticeSource(
        notice_url="https://www.miit.gov.cn/change-409.html",
        title=f"第{batch}批《道路机动车辆生产企业及产品公告》变更扩展公示",
        published_at="2026-08-28 10:00",
        batch=batch,
        iframe_url="https://www.miit.gov.cn/change-409/index.html",
        unit_url="https://www.miit.gov.cn/api/unit",
        unit_params={},
    )


def notice_row(batch: str = "409", model_code: str = "ABC6500EV") -> dict[str, str]:
    return {
        "notice_title": "多用途乘用车",
        "notice_batch": batch,
        "company": "示例汽车有限公司",
        "trademark": "示例牌",
        "product_name": "多用途乘用车",
        "model_code": model_code,
        "detail_url": "https://www.miit.gov.cn/change-409/detail.html",
    }


def run_change_ingestion(
    monkeypatch,
    tmp_path: Path,
    *,
    announcement_batch: str,
    download=None,
    parse=None,
) -> tuple[int, Path, Path]:
    catalog_db = make_catalog_db(tmp_path / "catalog.sqlite")
    site_db = tmp_path / "site.sqlite"
    upstream_root = tmp_path / "vehicle-data-hub"
    download_root = upstream_root / "downloads" / "announcement_site"
    monkeypatch.setattr(seed, "UPSTREAM_ROOT", upstream_root)
    monkeypatch.setattr(seed.change_notice, "load_change_notice_source", lambda _url: source())
    monkeypatch.setattr(
        seed.change_notice,
        "query_change_notice",
        lambda *_args, **_kwargs: ([notice_row()], 1),
    )
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
                "cpid": f"product-{announcement_batch}",
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
            "--change-notice-url",
            source().notice_url,
            "--all",
            "--limit",
            "10",
            "--catalog-db",
            str(catalog_db),
            "--site-db",
            str(site_db),
            "--download-root",
            str(download_root),
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


def test_change_notice_waits_until_the_formal_batch_is_effective(monkeypatch, tmp_path: Path):
    exit_code, site_db, upstream_root = run_change_ingestion(
        monkeypatch, tmp_path, announcement_batch="408"
    )
    assert exit_code == 2
    with sqlite3.connect(site_db) as conn:
        assert conn.execute("SELECT awaiting_effective FROM ingestion_runs").fetchone()[0] == 1
        status, error = conn.execute("SELECT status, error FROM run_models").fetchone()
        assert status == "awaiting_effective"
        assert "当前最高为第408批" in error
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 0
    assert list((upstream_root / "downloads" / "announcement_site" / "_snapshots").glob("*.json"))


def test_change_notice_ingests_latest_pdf_and_records_provenance(monkeypatch, tmp_path: Path):
    def fake_download(_row, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "示例牌_ABC6500EV_409.pdf"
        path.write_bytes(b"%PDF-1.4\nwebsite-test")
        return path, True, path.stat().st_size

    exit_code, site_db, upstream_root = run_change_ingestion(
        monkeypatch, tmp_path, announcement_batch="409", download=fake_download
    )
    assert exit_code == 0
    with sqlite3.connect(site_db) as conn:
        relative_path = conn.execute("SELECT relative_path FROM documents").fetchone()[0]
        raw_json = json.loads(conn.execute("SELECT raw_json FROM announcements").fetchone()[0])
        assert relative_path.startswith("downloads/announcement_site/")
        assert raw_json["_change_notice"]["batch"] == "409"
        assert conn.execute("SELECT awaiting_effective FROM ingestion_runs").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM run_models").fetchone()[0] == "done"
    assert (upstream_root / relative_path).read_bytes().startswith(b"%PDF")


def test_change_notice_models_are_not_excluded_when_already_in_the_site_db(tmp_path: Path):
    catalog_db = make_catalog_db(tmp_path / "catalog.sqlite")
    site_db = tmp_path / "site.sqlite"
    with sqlite3.connect(site_db) as conn:
        conn.executescript(seed.SCHEMA)
        conn.execute(
            "INSERT INTO vehicles(market_name, announcement_model_code, catalog_name, catalog_batch, "
            "catalog_category, catalog_seq, catalog_company) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("旧车型名", "ABC6500EV", seed.CATALOG_NAME, "31", "乘用车", "1", "示例企业"),
        )
    selected = seed.select_change_notice_models(catalog_db, site_db, [notice_row(), notice_row()])
    assert len(selected) == 1
    assert selected[0]["model_code"] == "ABC6500EV"
    assert selected[0]["notice_batch"] == "409"


def test_candidate_lookup_does_not_leak_sqlite_connections(tmp_path):
    """`with sqlite3.connect(...)` 只管事务不关连接；变更公示模式逐行调用这两个
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
    code, db, _ = run_change_ingestion(monkeypatch, tmp_path, announcement_batch='409', download=download,
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
