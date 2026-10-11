"""Exact event selection and NEV download boundaries use only disposable local fixtures."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date

import pytest

from miit_gonggao import collection as seed
from miit_gonggao import collection_tracking as tracking
from test_collection_inventory import InventoryFixture, product


@pytest.fixture
def data(tmp_path, monkeypatch):
    data = InventoryFixture(tmp_path)
    monkeypatch.setattr(seed, 'UPSTREAM_ROOT', data.pdf_root)
    monkeypatch.setattr(seed, 'refresh_collection_status', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tracking, 'document_image_error', lambda *_args: '')

    def forbid(*_args, **_kwargs):
        pytest.fail('Selection tests must never contact a network or download a document')

    monkeypatch.setattr(seed.core, 'post_form', forbid)
    monkeypatch.setattr(seed.core, 'query_all_pages', forbid)
    monkeypatch.setattr(seed.core, 'download_param_page', forbid)
    monkeypatch.setattr(seed, 'store_announcement', forbid)
    return data


def event(data, model, *, pid='', batch=410, legacy=False):
    with sqlite3.connect(data.db) as conn:
        event_id = tracking.register_event(
            conn, model_code=model, batch=batch, kind='formal', product_id=pid,
            source_url='https://www.miit.gov.cn/fixture', event_date=date.today().isoformat(),
            metadata={'product_name': '纯电动载货汽车'},
        )
        if legacy:
            conn.execute("UPDATE tracking_events SET kind='change_notice' WHERE event_id=?", (event_id,))
    return event_id


def test_selection_isolates_every_bucket_and_collect_updates_only_selected(data, monkeypatch):
    selected = event(data, 'TEST1000SELECTED')
    other = event(data, 'TEST1000OTHER')
    event(data, 'TEST1000LEGACY', legacy=True)
    selected_resolved = event(data, 'TEST1000RESOLVED', pid='resolved')
    other_resolved = event(data, 'TEST1000OTHERRESOLVED', pid='other-resolved')
    data.announcement(product('resolved', 'TEST1000RESOLVED', batch=410))
    data.announcement(product('other-resolved', 'TEST1000OTHERRESOLVED', batch=410))
    queries = []

    def query(**kwargs):
        queries.append(kwargs)
        return []

    monkeypatch.setattr(seed.core, 'query_all_pages', query)
    with sqlite3.connect(data.db) as conn:
        plan = tracking.retry_plan(conn, data.pdf_root, batch=410, limit=1,
                                   event_ids=[selected, selected_resolved], nev_only=True)
        assert [row['event_id'] for row in plan['pending']] == [selected]
        assert plan['resolved'] == [selected_resolved]
        assert plan['legacy_notice'] == plan['remaining'] == []
        assert plan['query_groups'] == 1 and plan['nev_only'] is True
        result = tracking.collect(conn, plan, catalog_db=data.catalog, pdf_root=data.pdf_root)
        selector = json.loads(conn.execute('SELECT selector_json FROM ingestion_runs WHERE id=?',
                                           (result['run_id'],)).fetchone()[0])
        assert selector['selected_event_ids'] == sorted([selected, selected_resolved])
        assert selector['nev_only'] is True
        assert conn.execute('SELECT event_id FROM tracking_attempts').fetchall() == [(selected,)]
        for untouched in (other, other_resolved):
            assert conn.execute('SELECT status,attempts FROM tracking_events WHERE event_id=?',
                                (untouched,)).fetchone() == ('pending', 0)
        assert conn.execute('SELECT status FROM tracking_events WHERE event_id=?',
                            (selected_resolved,)).fetchone() == ('done',)
    assert queries == [{'model_code': 'TEST1000SELECTED', 'pc': '410', 'page_size': 50}]


@pytest.mark.parametrize('invalid', ['unknown', 'batch', 'legacy', 'empty'])
def test_invalid_explicit_selection_is_rejected(data, invalid):
    known = event(data, 'TEST1000KNOWN', legacy=invalid == 'legacy')
    selected = ['unknown-id'] if invalid == 'unknown' else [] if invalid == 'empty' else [known]
    with sqlite3.connect(data.db) as conn, pytest.raises(ValueError):
        tracking.retry_plan(conn, data.pdf_root, event_ids=selected,
                            batch=409 if invalid == 'batch' else 410)


@pytest.mark.parametrize(('name', 'catalog', 'nev_only', 'accepted'), [
    ('纯电动载货汽车', [], True, True),
    ('载货汽车', ['新能源汽车', '纯电动汽车'], True, True),
    ('载货汽车', ['新能源汽车'], True, False),
    ('载货汽车', [], True, False),
    ('混合动力载货汽车', ['普通混合动力汽车'], True, False),
    ('普通混合动力载货汽车', ['插电式混合动力汽车'], True, False),
    ('非插电式混合动力载货汽车', ['插电式混合动力汽车'], True, False),
    ('载货汽车', ['纯电动汽车', '普通混合动力汽车'], True, False),
    ('载货汽车', [], False, True),
])
def test_nev_only_checks_actual_product_and_catalog_before_store(
        data, monkeypatch, name, catalog, nev_only, accepted):
    model = 'TEST1000BEV1'
    selected = event(data, model)
    for label in catalog:
        data.catalog_row(model, energy=label)
    actual = product('actual', model, name=name, batch=410)
    # Other models and batches in an API response must not enter this event's download scope.
    returned = [actual, product('wrong-model', 'OTHER1000BEV', batch=410),
                product('wrong-batch', model, batch=409)]
    monkeypatch.setattr(seed.core, 'query_all_pages', lambda **_kwargs: returned)
    stored = []

    def store(_conn, **kwargs):
        stored.append(kwargs['row']['cpid'])
        return seed.StoreOutcome(True, '')

    monkeypatch.setattr(seed, 'store_announcement', store)
    with sqlite3.connect(data.db) as conn:
        plan = tracking.retry_plan(conn, data.pdf_root, batch=410, event_ids=[selected])
        result = tracking.collect(conn, plan, catalog_db=data.catalog, pdf_root=data.pdf_root,
                                  nev_only=nev_only)
        saved = conn.execute('SELECT status,last_error FROM tracking_events WHERE event_id=?',
                             (selected,)).fetchone()
        assert saved[0] == ('done' if accepted else 'partial')
        if not accepted:
            assert '新能源范围校验未通过' in saved[1]
            assert 'actual' in saved[1]
            assert result['energy_scope_rejected'] == result['errors'] == 1
            assert result['non_pdf_documents'] == 0
        assert conn.execute('SELECT completed_at FROM ingestion_runs WHERE id=?',
                            (result['run_id'],)).fetchone()[0]
    assert stored == (['actual'] if accepted else [])


@pytest.mark.parametrize('command', ['plan', 'collect'])
def test_cli_accepts_repeated_event_ids_and_nev_only(data, monkeypatch, capsys, command):
    chosen = [event(data, 'TEST1000FIRST'), event(data, 'TEST1000SECOND')]
    untouched = event(data, 'TEST1000UNSELECTED')
    monkeypatch.setattr(seed.core, 'query_all_pages', lambda **_kwargs: [])
    monkeypatch.setattr(sys, 'argv', [
        'tracking', '--site-db', str(data.db), '--catalog-db', str(data.catalog),
        '--pdf-root', str(data.pdf_root), command, '--batch', '410', '--nev-only',
        '--event-id', chosen[0], '--event-id', chosen[1],
    ])

    assert tracking.main() == 0
    result = json.loads(capsys.readouterr().out)

    if command == 'plan':
        assert result['selected_event_ids'] == sorted(chosen)
        assert result['nev_only'] is True
        assert {row['event_id'] for row in result['pending']} == set(chosen)
    else:
        assert result['queries'] == 2
        with sqlite3.connect(data.db) as conn:
            assert {row[0] for row in conn.execute('SELECT event_id FROM tracking_attempts')} == set(chosen)
            assert conn.execute('SELECT attempts FROM tracking_events WHERE event_id=?',
                                (untouched,)).fetchone() == (0,)
