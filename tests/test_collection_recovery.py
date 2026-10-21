"""离线站点灾难恢复：有来源标记，不覆盖资产，不伪造查询及目录历史。"""
import hashlib
import json
import sqlite3

import pytest

from miit_gonggao import collection_recovery as recovery


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    root = tmp_path / 'hub'
    pdf = root / 'downloads/announcement_site/测试牌/测试车/第410批/sample.pdf'
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b'%PDF-1.4\nrecovery fixture\n')
    database = tmp_path / 'site.sqlite'
    rows = [dict(public_id='abcdef123456', source_product_id='P1', model_code='TEST6000BEV',
                 product_name='纯电动多用途乘用车', company='测试企业', trademark='测试牌', batch='410',
                 common_name='测试车', category='乘用车', availability='published', is_pdf=1,
                 fields_json=json.dumps({'fuel_type': '电', 'common_name': '测试车'}),
                 doc_path=str(pdf.relative_to(root)), doc_bytes=pdf.stat().st_size,
                 doc_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(), series_id=1)]
    rows.append({**rows[0], 'public_id': '123456abcdef', 'source_product_id': 'tracking:E1',
                 'model_code': 'OTHER6000BEV', 'availability': 'pending_source', 'is_pdf': 0,
                 'doc_path': None, 'doc_bytes': None, 'doc_sha256': None, 'series_id': None})
    with sqlite3.connect(database) as con:
        con.execute('create table announcement_flat(id integer primary key,' + ','.join(rows[0]) + ')')
        for row in rows:
            con.execute('insert into announcement_flat(' + ','.join(row) + ') values ('
                        + ','.join('?' for _ in row) + ')', list(row.values()))
        con.execute('create table announcement_photos(public_id,announcement_public_id,ordinal,'
                    'photo_path,pdf_offset,photo_bytes,photo_sha256,width,height)')
        con.execute('create table model_series(id,name,trademark,source,aliases_json)')
        con.execute("insert into model_series values (1,'测试车','测试牌','profile','[]')")
        con.execute('create table search_fts(public_id)')
        con.executemany('insert into search_fts values (?)', [(r['public_id'],) for r in rows])
        con.execute('create table meta(key,value)')
        con.executemany('insert into meta values (?,?)', {
            'schema_version': '3', 'collection_scope': 'automotive_complete_v1', 'built_at': '2026-10-15',
            'source_rows': '2', 'published_rows': '1', 'series_count': '1',
        }.items())
    monkeypatch.setattr(recovery.collection, 'refresh_collection_status', lambda *_a, **_k: None)
    monkeypatch.setattr(recovery.core, 'http_request', lambda *_a, **_k: pytest.fail('恢复不得联网'))
    return database, root, pdf


def test_preview_is_read_only_and_apply_keeps_provenance_without_invented_history(snapshot):
    database, root, pdf = snapshot
    before = database.read_bytes(), pdf.read_bytes()
    plan = recovery.restore(database, root)
    assert plan['status'] == 'preview' and plan['pdfs'] == 1
    assert not (root / 'data').exists() and not (root / 'var').exists()
    result = recovery.restore(database, root, apply=True)
    assert result['status'] == 'restored'
    assert (database.read_bytes(), pdf.read_bytes()) == before
    with sqlite3.connect(root / 'data/announcement_site.sqlite') as con:
        assert con.execute('select count(*) from announcements').fetchone()[0] == 1
        assert con.execute('select count(*) from site_recovery_records').fetchone()[0] == 2
        assert con.execute('select count(*) from ingestion_runs').fetchone()[0] == 0
        assert con.execute('select count(*) from run_models').fetchone()[0] == 0
        assert con.execute('select count(*) from tracking_events').fetchone()[0] == 0
        assert set(recovery.recovered_records(con)) == {'P1', 'tracking:E1'}
        assert recovery.recovery_summary(con)['built_at'] == '2026-10-15'
        raw = json.loads(con.execute('select raw_json from announcements').fetchone()[0])
        assert raw[recovery.MARKER] == result['snapshot_sha256']
        con.execute("update announcement_fields set fields_json='{}'")
        assert set(recovery.recovered_records(con)) == {'tracking:E1'}
    with sqlite3.connect(root / 'data/jianmian_catalog.sqlite') as con:
        assert con.execute('select count(*) from articles').fetchone()[0] == 0
        assert con.execute('select count(*) from catalog_rows').fetchone()[0] == 0
    profile = json.loads((root / 'data/vehicle_profiles.json').read_text())[0]
    assert profile['gonggao']['model_prefixes'] == ['TEST6000BEV']
    # 原始恢复副本必须独立，后续业务写入不能污染它。
    with sqlite3.connect(result['run_directory'] + '/announcement_site.sqlite') as con:
        assert 'P1' in recovery.recovered_records(con)
    with pytest.raises(ValueError, match='禁止覆盖'):
        recovery.restore(database, root, apply=True)


@pytest.mark.parametrize('kind', ['hash', 'traversal', 'wrong_count', 'wal'])
def test_invalid_snapshot_or_asset_never_publishes_a_business_database(snapshot, kind):
    database, root, pdf = snapshot
    if kind == 'hash':
        pdf.write_bytes(b'%PDF-changed')
    elif kind == 'wal':
        database.with_name(database.name + '-wal').write_bytes(b'pending')
    else:
        with sqlite3.connect(database) as con:
            if kind == 'traversal':
                con.execute("update announcement_flat set doc_path='../outside.pdf' where is_pdf=1")
            else:
                con.execute("update meta set value='999' where key='source_rows'")
    with pytest.raises(ValueError):
        recovery.restore(database, root, apply=True)
    assert not (root / 'data/announcement_site.sqlite').exists()
    assert not (root / 'var').exists()


def test_genuine_official_replacement_stops_using_recovered_metadata(snapshot):
    database, root, _ = snapshot
    recovery.restore(database, root, apply=True)
    with sqlite3.connect(root / 'data/announcement_site.sqlite') as con:
        con.execute("update announcements set raw_json='{}'")
        assert set(recovery.recovered_records(con)) == {'tracking:E1'}


def test_partial_publication_failure_rolls_back_only_new_targets(snapshot, monkeypatch):
    database, root, pdf = snapshot
    link = recovery.os.link
    calls = []

    def fail_second(source, target):
        calls.append(target)
        if len(calls) == 2:
            raise OSError('publication fixture failure')
        return link(source, target)

    monkeypatch.setattr(recovery.os, 'link', fail_second)
    with pytest.raises(OSError, match='publication fixture failure'):
        recovery.restore(database, root, apply=True)
    assert pdf.is_file() and database.is_file()
    assert not any((root / 'data').glob('*.sqlite'))
    assert not (root / 'data/vehicle_profiles.json').exists()
