"""Refresh history is evidence for one product and target batch, never a model-wide veto."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from miit_gonggao import collection_status as status
from miit_gonggao import core, republished
from test_collection_inventory import InventoryFixture, product


NON_PDF = '接口返回非 PDF，已存为 HTML；原文件：'
CHECKED_AT = '2026-09-12T11:00:00+00:00'


@pytest.fixture
def data(tmp_path):
    return InventoryFixture(tmp_path)


def evidence_path(model, pid, batch=409, *, prefix=''):
    safe_model = core.safe_part(model)
    return (f'downloads/announcement_site/测试牌/{safe_model}/第{batch}批/'
            f'{prefix}测试牌_{safe_model}_{batch}_{pid}.html')


def attempt(data, model, error, *, completed_at=CHECKED_AT, model_status='partial'):
    vid = data.vehicle(model)
    # Legacy runs 32/33 had no selected_product_ids or selection_snapshot.
    selector = json.dumps({'mode': 'republished', 'republished': {'batches': [409]}})
    with sqlite3.connect(data.db) as con:
        run_id = con.execute(
            'INSERT INTO ingestion_runs(started_at,completed_at,selector_json,selected_models) '
            'VALUES(?,?,?,1)', ('2026-09-12T10:00:00+00:00', completed_at, selector),
        ).lastrowid
        con.execute('INSERT INTO run_models(run_id,sort_order,vehicle_id,status,error) '
                    'VALUES(?,1,?,?,?)', (run_id, vid, model_status, error))
    return run_id


def existing_pdf(data, pid, model=None, batch=408):
    row = product(pid, model=model, batch=batch)
    data.announcement(row)
    return row


def ids(rows):
    return {row['product_id'] for row in rows}


def test_history_keeps_exact_identity_latest_evidence_and_old_pdf(data):
    model = 'TEST1000BEV1'
    old = existing_pdf(data, 'P1', model)
    existing_pdf(data, 'P2', model)
    data.cache_batch(409, [product('P1', model), product('P2', model)])
    path = evidence_path(model, 'P1')
    attempt(data, model, NON_PDF + path, completed_at='2026-09-12T10:30:00+00:00')
    latest_run = attempt(data, model, NON_PDF + path)

    summary, candidates = data.compute()

    assert ids(candidates['refresh']) == {'P2'}
    assert summary['refresh_candidates'] == {'total': 1, 'by_group': {'nev': 1}}
    assert summary['refresh_source_unavailable'] == {
        'total': 1, 'by_group': {'nev': 1}, 'by_batch': [{'batch': 409, 'n': 1}],
    }
    assert ids(candidates['refresh_source_unavailable']) == {'P1'}
    row = candidates['refresh_source_unavailable'][0]
    assert row['db_batch'] == 408 and row['batch'] == 409
    assert row['last_non_pdf_attempt'] == {
        'run_id': latest_run, 'checked_at': CHECKED_AT, 'product_id': 'P1', 'batch': 409,
        'model_code': model, 'evidence_path': path, 'status': 'non_pdf',
    }
    assert summary['documents']['states'] == {'parsed': 2}
    assert summary['non_pdf'] == 0
    assert candidates['missing'] == candidates['non_pdf'] == []
    assert not (data.pdf_root / path).exists()  # Historical DB evidence needs no HTML scan.
    with sqlite3.connect(f'file:{data.db}?mode=ro', uri=True) as con:
        assert con.execute('SELECT b.batch FROM announcements a JOIN batches b ON b.id=a.batch_id '
                           'WHERE a.source_product_id=?', (old['cpid'],)).fetchone() == ('408',)


def test_old_attempt_neither_hides_later_batch_nor_revives_updated_pdf(data):
    newer_model, updated_model = 'TEST1000BEV1', 'TEST1000BEV2'
    existing_pdf(data, 'newer', newer_model)
    existing_pdf(data, 'updated', updated_model, batch=409)
    data.cache_batch(409, [product('updated', updated_model)])
    data.cache_batch(410, [product('newer', newer_model, batch=410)])
    attempt(data, newer_model, NON_PDF + evidence_path(newer_model, 'newer', 409))
    attempt(data, updated_model, NON_PDF + evidence_path(updated_model, 'updated', 409))

    summary, candidates = data.compute()

    assert ids(candidates['refresh']) == {'newer'}
    assert candidates['refresh'][0]['batch'] == 410
    assert candidates['refresh_source_unavailable'] == []
    assert summary['refresh_source_unavailable']['total'] == 0


@pytest.mark.parametrize('invalid', [
    'unfinished_run', 'interrupted', 'query_failed', 'download_failed', 'image_failed', 'no_path',
    'wrong_model', 'wrong_directory_batch', 'outside_root', 'absolute_path',
])
def test_incomplete_or_mismatched_evidence_leaves_refresh_candidate(data, invalid):
    model = 'TEST1000BEV1'
    existing_pdf(data, 'P1', model)
    data.cache_batch(409, [product('P1', model)])
    path = evidence_path(model, 'P1')
    run_model = model
    error = NON_PDF + path
    options = {}
    if invalid == 'unfinished_run':
        options['completed_at'] = None
    elif invalid in {'interrupted', 'query_failed'}:
        options['model_status'] = invalid
    elif invalid == 'download_failed':
        error = '下载失败：timeout；原文件：' + path
    elif invalid == 'image_failed':
        error = '图片下载不完整：1 项失败；原文件：' + path
    elif invalid == 'no_path':
        error = '接口返回非 PDF，已存为 HTML'
    elif invalid == 'wrong_model':
        run_model = 'OTHER1000BEV1'
    elif invalid == 'wrong_directory_batch':
        error = NON_PDF + path.replace('/第409批/', '/第408批/')
    elif invalid == 'outside_root':
        error = NON_PDF + '../' + path
    elif invalid == 'absolute_path':
        error = NON_PDF + str(data.pdf_root / path)
    attempt(data, run_model, error, **options)

    _, candidates = data.compute()

    assert ids(candidates['refresh']) == {'P1'}
    assert candidates['refresh_source_unavailable'] == []


def test_multiple_product_errors_and_hash_prefixed_filename_are_distinct(data):
    model = 'TEST1000BEV1'
    for pid in ('P1', 'P2', 'P3'):
        existing_pdf(data, pid, model)
    data.cache_batch(409, [product(pid, model) for pid in ('P1', 'P2', 'P3')])
    paths = [evidence_path(model, 'P1', prefix='a' * 64 + '_'), evidence_path(model, 'P2')]
    error = '; '.join(NON_PDF + path for path in paths) + '; 图片下载不完整：1 项失败'
    attempt(data, model, error)

    _, candidates = data.compute()

    assert ids(candidates['refresh']) == {'P3'}
    assert ids(candidates['refresh_source_unavailable']) == {'P1', 'P2'}
    assert {row['last_non_pdf_attempt']['evidence_path']
            for row in candidates['refresh_source_unavailable']} == set(paths)


def test_saved_status_excludes_attempted_items_but_explicit_batch_keeps_retry(data, tmp_path):
    for pid in ('attempted', 'untried'):
        existing_pdf(data, pid)
    records = [product(pid) for pid in ('attempted', 'untried')]
    data.cache_batch(409, records, fetched_at=datetime.now(timezone.utc).isoformat())
    attempt(data, records[0]['clxh'], NON_PDF + evidence_path(records[0]['clxh'], 'attempted'))
    options = {'catalog_db': data.catalog, 'pdf_root': data.pdf_root,
               'cache_dir': data.cache, 'out_dir': tmp_path / 'status-output'}

    saved = status.refresh_status(data.db, **options)
    source = republished.from_status(data.db, **options)
    explicit = republished.from_batches(data.db, [409], data.cache)

    assert saved['snapshot']['summary']['refresh_source_unavailable']['total'] == 1
    assert ids(source.rows) == {'untried'}
    assert ids(explicit.rows) == {'attempted', 'untried'}
