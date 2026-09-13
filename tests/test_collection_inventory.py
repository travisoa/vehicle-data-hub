from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from miit_gonggao import collection as C
from miit_gonggao import collection_inventory as I
from miit_gonggao import collection_tracking as T
from miit_gonggao.collection_coverage import (
    COVERAGE_DOWNLOADED, COVERAGE_NO_MATCH, COVERAGE_NO_PDF,
    COVERAGE_RETRY, COVERAGE_UNSEEN, COVERAGE_WAITING,
)
from scripts import announcement_catalog_gap as G

TODAY = date(2026, 9, 13)
PDF = b'%PDF-1.7\nfixture contents'


def product(pid: str, model: str | None = None, name='纯电动载货汽车', batch=409):
    return {'cpid': pid, 'clxh': model or f'TEST1000{pid}', 'clmc': name,
            'gppc': str(batch), 'dataTag': 'Z', 'cpsb': '测试牌', 'qymc': '测试公司'}


class InventoryFixture:
    def __init__(self, root: Path):
        self.db = root / 'business.sqlite'
        self.catalog = root / 'catalog.sqlite'
        self.cache = root / 'batch-cache'
        self.pdf_root = root / 'pdf-root'
        self.cache.mkdir()
        self.pdf_root.mkdir()
        with sqlite3.connect(self.db) as con:
            con.executescript(C.SCHEMA)
            T.ensure_schema(con)
        with sqlite3.connect(self.catalog) as con:
            con.execute('CREATE TABLE catalog_rows(model_code TEXT,energy_type TEXT,common_name TEXT)')

    def cache_batch(self, batch, records, **changes):
        payload = {'cache_version': G.CACHE_VERSION, 'complete': True, 'batch': batch,
                   'fetched_at': '2026-09-12T10:00:00+00:00', 'failures': [],
                   'verify_prefixes': list(G.VERIFY_PREFIXES), 'verify_missed': 0,
                   'products': records}
        payload.update(changes)
        (self.cache / f'batch{batch}.json').write_text(json.dumps(payload, ensure_ascii=False))

    def vehicle(self, model):
        with sqlite3.connect(self.db) as con:
            con.execute('INSERT OR IGNORE INTO vehicles(market_name,announcement_model_code,catalog_name,'
                        'catalog_batch,catalog_category) VALUES(?,?,?,?,?)', (model, model, '', '', '货车'))
            return con.execute('SELECT id FROM vehicles WHERE announcement_model_code=?', (model,)).fetchone()[0]

    def catalog_row(self, model, energy='纯电动', common=''):
        with sqlite3.connect(self.catalog) as con:
            con.execute('INSERT INTO catalog_rows VALUES(?,?,?)', (model, energy, common))

    def outcome(self, model, status):
        vid = self.vehicle(model)
        with sqlite3.connect(self.db) as con:
            run = con.execute("INSERT INTO ingestion_runs(started_at,completed_at,selector_json,selected_models) "
                              "VALUES('2026-09-12T10:00:00Z','2026-09-12T11:00:00Z','{}',1)").lastrowid
            con.execute('INSERT INTO run_models(run_id,sort_order,vehicle_id,status) VALUES(?,1,?,?)',
                        (run, vid, status))

    def announcement(self, row, *, body=PDF, is_pdf=1, fields=None, parse_error='', error='',
                     size=None, relative=None, document=True, stored_hash='deliberately-not-verified'):
        vid = self.vehicle(row['clxh'])
        relative = relative or f"documents/{row['cpid']}.pdf"
        if body is not None:
            path = self.pdf_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        with sqlite3.connect(self.db) as con:
            con.execute('INSERT OR IGNORE INTO batches(batch) VALUES(?)', (row['gppc'],))
            bid = con.execute('SELECT id FROM batches WHERE batch=?', (row['gppc'],)).fetchone()[0]
            aid = con.execute('INSERT INTO announcements(source_product_id,vehicle_id,batch_id,model_code,'
                              'product_name,raw_json,first_seen_at) VALUES(?,?,?,?,?,?,?)',
                              (row['cpid'], vid, bid, row['clxh'], row['clmc'], json.dumps(row), '2026-09-12')).lastrowid
            if document:
                con.execute('INSERT INTO documents(announcement_id,relative_path,is_pdf,bytes,sha256,'
                            'downloaded_at,error) VALUES(?,?,?,?,?,?,?)',
                            (aid, relative, is_pdf, size if size is not None else len(body or b''),
                             stored_hash, '2026-09-12T10:10:00Z', error))
            con.execute('INSERT INTO announcement_fields VALUES(?,?,?)',
                        (aid, json.dumps({'fuel_type': '电'} if fields is None else fields), parse_error))

    def event(self, model, batch=409, pid='', event_date='2026-09-12', next_check='',
              kind='formal', name='纯电动载货汽车'):
        with sqlite3.connect(self.db) as con:
            event_id = T.register_event(con, model_code=model, batch=batch, kind=kind,
                                        source_url='https://www.miit.gov.cn/test', product_id=pid,
                                        metadata={'clmc': name}, event_date=event_date)
            con.execute("UPDATE tracking_events SET first_seen_at='2026-09-12',next_check_at=? WHERE event_id=?",
                        (next_check, event_id))

    def compute(self):
        return I.compute_inventory(self.db, self.catalog, self.cache, self.pdf_root, today=TODAY)


@pytest.fixture
def data(tmp_path):
    return InventoryFixture(tmp_path)


def test_document_states_remain_distinct_and_pdf_hash_is_not_claimed(data):
    specs = {
        'parsed': {}, 'non_pdf': {'body': b'<html>missing</html>', 'is_pdf': 0},
        'zero': {'body': None, 'is_pdf': 0},
        'failure': {'body': None, 'size': 12, 'is_pdf': 0, 'error': '下载失败: timeout'},
        'unparsed': {'fields': {}}, 'parse_error': {'parse_error': 'parser failed'},
        'size_invalid': {'size': 999}, 'magic_invalid': {'body': b'not a PDF file'},
        'path_missing': {'body': None, 'size': 12}, 'no_document': {'document': False},
    }
    records = [product(pid) for pid in specs] + [product('missing')]
    for row in records[:-1]:
        data.announcement(row, **specs[row['cpid']])
    data.cache_batch(409, records)
    summary, candidates = data.compute()
    assert summary['documents']['states'] == {'parsed': 1, 'non_pdf': 1, 'download_failed': 2,
                                             'pdf_unparsed': 2, 'pdf_invalid': 3, 'no_document': 1}
    assert summary['documents']['validation_level'] == 'path/size/magic'
    assert summary['documents']['sha256_verified'] is False
    assert [row['product_id'] for row in candidates['missing']] == ['missing']
    assert len(candidates['download_failed']) == 2
    assert len(candidates['pdf_invalid_or_no_document']) == 4
    assert summary['in_scope_totals']['nev']['total'] == len(records)


def test_pdf_path_cannot_escape_the_root_or_follow_external_symlink(data, tmp_path):
    external = tmp_path / 'external.pdf'
    external.write_bytes(PDF)
    link = data.pdf_root / 'linked.pdf'
    link.symlink_to(external)
    for relative in ('../external.pdf', str(external), 'linked.pdf'):
        assert I.document_state(relative, len(PDF), 1, {'fuel_type': '电'}, '', '', data.pdf_root) == 'pdf_invalid'


def test_fuel_and_unknown_keep_authorized_batch_when_same_product_appears_in_410(data):
    older = [product('fuel', name='混合动力载货汽车'), product('unknown', name='载货汽车'), product('nev')]
    newer = [{**row, 'gppc': '410', 'clxh': row['clxh'] + 'NEW'} for row in older]
    data.cache_batch(409, older)
    data.cache_batch(410, newer)
    summary, candidates = data.compute()
    rows = {row['product_id']: row for row in candidates['missing']}
    assert summary['inputs']['batch_caches']['unique_product_ids'] == 3
    for pid, group in [('fuel', 'fuel_408_409'), ('unknown', 'unknown_408_409')]:
        assert rows[pid]['group'] == group
        assert rows[pid]['batch'] == 409
        assert rows[pid]['cached_latest_batch'] == 410
        assert rows[pid]['model_code'] == f'TEST1000{pid}'
    assert rows['nev']['batch'] == 410
    assert rows['nev']['batches'] == [409, 410]


def test_scope_and_energy_use_shared_rules_without_model_suffix_guessing(data):
    records = [product('chassis', name='纯电动载货汽车底盘'), product('moto', name='电动正三轮摩托车'),
               product('punctuation', name='纯电动载货汽车.'), product('unknown', name='载货汽车'),
               product('catalog', name='混合动力载货汽车')]
    data.catalog_row(records[-1]['clxh'], '新能源汽车')
    data.catalog_row(records[-1]['clxh'], '插电式混合动力')
    data.cache_batch(409, records)
    data.cache_batch(407, [product('outside', model='TEST1000BEV', name='载货汽车', batch=407)])
    summary, candidates = data.compute()
    rows = {row['product_id']: row for row in candidates['missing']}
    assert set(rows) == {'unknown', 'catalog'}
    assert rows['catalog']['energy_type'] == '插电式混合动力'
    assert rows['catalog']['group'] == 'nev'
    assert rows['unknown']['group'] == 'unknown_408_409'
    assert [row['product_id'] for row in candidates['scope_review']] == ['punctuation']
    assert summary['unknown_energy_outside_with_ev_model_code_not_counted'] == {'truck': 1}


def test_complete_stale_cache_is_retained_with_explicit_expiry_partial_and_absent_are_excluded(data):
    data.cache_batch(407, [product('old', batch=407)], fetched_at='2026-09-01T00:00:00+00:00')
    data.cache_batch(409, [product('new')])
    (data.cache / 'batch408.partial.json').write_text('not a complete batch, deliberately unread')
    (data.cache / 'batch410.absent.json').write_text('{}')
    data.cache_batch(411, [product('incomplete', batch=411)], complete=False)
    summary, candidates = data.compute()
    cache = summary['inputs']['batch_caches']
    assert cache['complete_files'] == 2
    assert cache['stale_files_allowed'] == 1
    assert cache['stale'] is True
    assert cache['missing_batches_in_range'] == [408]
    assert len(cache['skipped']) == 3
    assert cache['files'][0]['valid_until'] == '2026-09-08T08:00:00+08:00'
    assert {row['product_id'] for row in candidates['missing']} == {'old', 'new'}


@pytest.mark.parametrize('changes', [
    {'verify_prefixes': []}, {'failures': ['timeout']}, {'cache_version': 3},
    {'fetched_at': '2026-09-14T00:00:00+08:00'}, {'fetched_at': '2026-09-12'},
])
def test_incomplete_or_invalid_freshness_is_explicit_evidence_not_a_full_batch(data, changes):
    data.cache_batch(409, [product('unreliable')], **changes)
    summary, candidates = data.compute()
    assert summary['inputs']['batch_caches']['complete_files'] == 0
    assert summary['inputs']['batch_caches']['skipped']
    assert candidates['missing'] == []


def test_missing_product_identity_rejects_whole_batch_instead_of_claiming_partial_universe(data):
    data.cache_batch(409, [product('good'), product('')])
    summary, candidates = data.compute()
    assert summary['inputs']['batch_caches']['rows_without_product_id'] == 1
    assert summary['inputs']['batch_caches']['complete_files'] == 0
    assert summary['inputs']['batch_caches']['skipped'][0]['reason'] == 'invalid product identity or batch'
    assert candidates['missing'] == []


@pytest.mark.parametrize('model_value', [None, ''])
def test_complete_batch_with_model_less_chassis_remains_usable(data, model_value):
    chassis = product('chassis', name='混凝土泵车底盘', batch=189)
    if model_value is None:
        chassis.pop('clxh')
    else:
        chassis['clxh'] = model_value
    chassis['dataTag'] = 'D'
    data.cache_batch(189, [chassis, product('car', batch=189)])
    summary, candidates = data.compute()
    cache = summary['inputs']['batch_caches']
    assert cache['complete_batches'] == [189]
    assert cache['complete_files'] == 1
    assert cache['unique_product_ids'] == 2
    assert cache['skipped'] == []
    assert [row['product_id'] for row in candidates['missing']] == ['car']
    assert {'group': 'excluded', 'state': 'missing', 'n': 1} in summary['universe_by_group_state']


def test_catalog_coverage_has_six_disjoint_model_buckets_and_uses_best_cross_run_outcome(data):
    for name in ('PDF', 'HTML', 'ABSENT', 'UNSEEN', 'WAIT', 'RETRY'):
        data.catalog_row(name)
    data.announcement(product('pdf', model='PDF'))
    data.announcement(product('html', model='HTML'), body=b'<html>no PDF</html>', is_pdf=0)
    data.outcome('ABSENT', 'no_match')
    data.outcome('ABSENT', 'query_failed')
    data.outcome('WAIT', 'awaiting_effective')
    data.outcome('RETRY', 'query_failed')
    summary, _ = data.compute()
    assert summary['catalog_coverage'] == {COVERAGE_DOWNLOADED: 1, COVERAGE_NO_PDF: 1, COVERAGE_NO_MATCH: 1,
                                           COVERAGE_UNSEEN: 1, COVERAGE_WAITING: 1, COVERAGE_RETRY: 1}
    assert summary['catalog_no_match']['models_absent_from_caches'] == 1


def test_historical_aliases_fix_catalog_absence_without_cross_combining_event_batches(data):
    data.catalog_row('OLD1000')
    data.outcome('OLD1000', 'no_match')
    data.cache_batch(408, [product('shared', model='OLD1000', batch=408)])
    data.cache_batch(409, [product('shared', model='NEW1000')])
    data.announcement(product('shared', model='NEW1000'))
    data.event('OLD1000', batch=408)
    data.event('OLD1000', batch=409)
    summary, candidates = data.compute()
    assert summary['catalog_no_match']['total'] == 1
    assert summary['catalog_no_match']['models_absent_from_caches'] == 0
    assert candidates['catalog_no_match_absent_from_caches'] == []
    assert summary['catalog_no_match']['cache_products_of_present_models'] == [{'group': 'nev', 'state': 'parsed', 'n': 1}]
    assert {row['official_list']: row['n'] for row in summary['tracking']['model_only_events']} == {
        'in_official_batch_list': 1, 'not_in_official_batch_list': 1}


def test_tracking_classifies_resolved_due_deferred_dormant_and_excluded(data):
    data.announcement(product('valid', model='VALID1000'))
    data.event('VALID1000', pid='valid')
    data.event('DUE1000')
    data.event('DEFER1000', next_check='2026-09-14')
    data.event('OLD1000', batch=407, event_date='2026-07-01')
    data.event('MOTO1000', name='电动正三轮摩托车')
    summary, _ = data.compute()
    assert {row['bucket']: row['n'] for row in summary['tracking']['events']} == {
        'resolved_valid_pdf': 1, 'due': 1, 'deferred': 1, 'dormant': 1, 'excluded': 1}


def test_inventory_uses_readonly_connections_and_does_not_mutate_inputs(data, monkeypatch):
    data.cache_batch(409, [product('missing')])
    paths = [data.db, data.catalog, data.cache / 'batch409.json']
    before = {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in paths}
    connect = sqlite3.connect
    calls = []
    statements = []

    def readonly_spy(database, *args, **kwargs):
        calls.append((database, kwargs))
        connection = connect(database, *args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(I.sqlite3, 'connect', readonly_spy)
    first = data.compute()
    second = data.compute()
    assert first == second
    assert all('mode=ro' in database and kwargs['uri'] for database, kwargs in calls)
    assert statements.count('BEGIN') == 4
    assert before == {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in paths}
    assert not list(data.pdf_root.iterdir())


@pytest.mark.parametrize('bad_value', ['{broken', '[]'])
def test_bad_business_json_fails_explicitly(data, bad_value):
    data.announcement(product('bad'))
    with sqlite3.connect(data.db) as con:
        con.execute('UPDATE announcement_fields SET fields_json=?', (bad_value,))
    with pytest.raises(ValueError, match='announcement bad fields_json'):
        data.compute()


def test_missing_tracking_schema_is_not_silently_reported_as_no_events(data):
    with sqlite3.connect(data.db) as con:
        con.execute('DROP TABLE tracking_events')
    with pytest.raises(ValueError, match='missing required tables.*tracking_events'):
        data.compute()


def test_missing_database_is_not_created(data):
    data.db.unlink()
    with pytest.raises(sqlite3.OperationalError):
        data.compute()
    assert not data.db.exists()


def absent_evidence(target_batch, *, fetched_at='2026-09-12T12:00:00+00:00', **changes):
    evidence = {'batch': str(target_batch), 'absent_upstream': True, 'complete': False, 'products': [],
                'absent_digest': f"Table 'gonggao_xxgk.clcp_chpdpk_{target_batch}' doesn't exist"}
    if fetched_at is not None:
        evidence['fetched_at'] = fetched_at
    return {**evidence, **changes}


def write_evidence(data, filename, payload):
    (data.cache / filename).write_text(json.dumps(payload))


def diagnostics(data):
    summary, _ = data.compute()
    return summary['inputs']['batch_caches']['cache_diagnostics']


def test_confirmed_source_absence_deduplicates_legacy_record_and_does_not_become_empty_batch(data):
    # Deliberately use a batch other than the real historical gap; no hard-coded exemption.
    data.cache_batch(406, [product('before', batch=406)])
    data.cache_batch(408, [product('after', batch=408)])
    write_evidence(data, 'batch407.json', absent_evidence(407, fetched_at=None))
    write_evidence(data, 'batch407.absent.json', absent_evidence(407))
    summary, candidates = data.compute()
    cache = summary['inputs']['batch_caches']
    result = cache['cache_diagnostics']
    assert cache['complete_batches'] == [406, 408]
    assert cache['missing_batches_in_range'] == [407]
    assert len(cache['skipped']) == 2
    assert {row['product_id'] for row in candidates['missing']} == {'before', 'after'}
    assert [row['file'] for row in result['known_source_absent']] == ['batch407.absent.json']
    assert result['known_source_absent'][0]['batch'] == 407
    assert result['known_source_absent'][0]['fetched_at'] == '2026-09-12T12:00:00+00:00'
    assert result['missing_cache'] == []
    assert result['has_actionable_warning'] is False
    assert result['coverage_limited'] is True
    assert result['superseded_evidence'][0]['file'] == 'batch407.json'
    assert result['superseded_evidence'][0]['superseded_by'] == 'batch407.absent.json'


@pytest.mark.parametrize('changes', [
    {'absent_upstream': False},
    {'absent_digest': 'request timed out'},
    {'absent_digest': "Table 'gonggao_xxgk.clcp_chpdpk_999' doesn't exist"},
    {'batch': '999'},
    {'products': [product('conflicting', batch=407)]},
    {'complete': True},
    {'fetched_at': 'invalid'},
])
def test_absent_suffix_or_flag_alone_does_not_suppress_real_warnings(data, changes):
    write_evidence(data, 'batch407.absent.json', absent_evidence(407, **changes))
    result = diagnostics(data)
    assert result['known_source_absent'] == []
    assert result['has_actionable_warning'] is True
    assert len(result['invalid_cache']) + len(result['partial_failed']) == 1


def test_partial_evidence_is_classified_by_content_even_with_absent_filename(data):
    write_evidence(data, 'batch407.absent.json', {
        'batch': '407', 'complete': False, 'products': [product('partial', batch=407)],
        'failures': ['company query timed out'], 'fetched_at': '2026-09-12T12:00:00+00:00',
    })
    result = diagnostics(data)
    assert result['known_source_absent'] == []
    assert len(result['partial_failed']) == 1
    assert result['partial_failed'][0]['reason'] == 'company query timed out'
    assert result['has_actionable_warning'] is True


def test_new_complete_batch_supersedes_older_partial_and_absence_sidecars(data):
    data.cache_batch(409, [product('current')], fetched_at='2026-09-13T00:00:00+00:00')
    write_evidence(data, 'batch409.absent.json', absent_evidence(409, fetched_at='2026-09-11T00:00:00+00:00'))
    write_evidence(data, 'batch409.partial.json', {
        'batch': '409', 'complete': False, 'products': [], 'failures': ['timeout'],
        'fetched_at': '2026-09-12T00:00:00+00:00',
    })
    result = diagnostics(data)
    assert result['known_source_absent'] == result['partial_failed'] == result['invalid_cache'] == []
    assert result['coverage_limited'] is False
    assert result['has_actionable_warning'] is False
    assert len(result['superseded_evidence']) == 2
    assert all(row['superseded_by'] == 'batch409.json' for row in result['superseded_evidence'])


def test_newer_refresh_failure_remains_visible_with_old_complete_cache(data):
    data.cache_batch(409, [product('historical')], fetched_at='2026-09-10T00:00:00+00:00')
    write_evidence(data, 'batch409.partial.json', {
        'batch': '409', 'complete': False, 'products': [], 'failures': ['new request timeout'],
        'fetched_at': '2026-09-12T00:00:00+00:00',
    })
    summary, candidates = data.compute()
    result = summary['inputs']['batch_caches']['cache_diagnostics']
    assert result['partial_failed'][0]['retained_complete_cache'] is True
    assert result['partial_failed'][0]['reason'] == 'new request timeout'
    assert result['has_actionable_warning'] is True
    assert summary['inputs']['batch_caches']['complete_batches'] == [409]
    assert [row['product_id'] for row in candidates['missing']] == ['historical']


def test_newer_failed_probe_replaces_old_absence_rather_than_hiding_failure(data):
    write_evidence(data, 'batch407.absent.json', absent_evidence(407, fetched_at='2026-09-10T00:00:00+00:00'))
    write_evidence(data, 'batch407.partial.json', {
        'batch': '407', 'complete': False, 'products': [], 'failures': ['timeout'],
        'fetched_at': '2026-09-12T00:00:00+00:00',
    })
    result = diagnostics(data)
    assert result['known_source_absent'] == []
    assert len(result['partial_failed']) == 1
    assert result['superseded_evidence'][0]['category'] == 'known_source_absent'
    assert result['has_actionable_warning'] is True


def test_corrupt_or_unrecognized_sidecars_remain_visible_even_with_complete_cache(data):
    data.cache_batch(409, [product('current')])
    (data.cache / 'batch409.partial.json').write_text('{broken')
    write_evidence(data, 'batch409.absent.json', {})
    result = diagnostics(data)
    assert len(result['invalid_cache']) == 2
    assert all(row['retained_complete_cache'] for row in result['invalid_cache'])
    assert result['has_actionable_warning'] is True


def test_missing_cache_holes_distinguish_unexplained_gap_from_source_absence(data):
    data.cache_batch(406, [product('first', batch=406)])
    data.cache_batch(410, [product('last', batch=410)])
    write_evidence(data, 'batch407.absent.json', absent_evidence(407))
    result = diagnostics(data)
    assert [row['batch'] for row in result['known_source_absent']] == [407]
    assert [row['batch'] for row in result['missing_cache']] == [408, 409]
    assert all(row['file'] is None and row['fetched_at'] is None for row in result['missing_cache'])
    assert result['has_actionable_warning'] is True


def test_malformed_primary_cache_still_refuses_inventory_instead_of_swallowing_error(data):
    (data.cache / 'batch409.json').write_text('{broken')
    with pytest.raises(ValueError, match='invalid JSON'):
        data.compute()
