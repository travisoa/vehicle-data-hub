"""Persistent status behavior with disposable inputs and a lightweight inventory stub."""
from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from miit_gonggao import collection_inventory as inventory
from miit_gonggao import collection_status as status


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / 'hub'
    db = root / 'data' / 'announcement_site.sqlite'
    catalog = root / 'data' / 'jianmian_catalog.sqlite'
    cache = root / 'downloads' / 'announcement_batches'
    output = root / 'var' / 'reports' / 'collection-status'
    db.parent.mkdir(parents=True)
    cache.mkdir(parents=True)
    db.write_bytes(b'disposable business database fixture')
    catalog.write_bytes(b'disposable catalog database fixture')
    (cache / 'batch408.json').write_text('{"batch":408}')
    (cache / 'batch409.json').write_text('{"batch":409}')
    logic_root = tmp_path / 'logic'
    for filename in status.LOGIC_FILES:
        path = logic_root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# version one\n')
    monkeypatch.setattr(status, 'ROOT', logic_root)
    monkeypatch.setattr(status, '_now', lambda: datetime(2026, 9, 13, 15, tzinfo=timezone.utc))
    summary = {
        'generated_at': '2026-09-13T23:00:00+08:00',
        'inputs': {
            'business_db': {'unfinished_runs': 0},
            'batch_caches': {
                'complete_files': 2, 'complete_batches': [408, 409], 'batch_range': [408, 409],
                'missing_batches_in_range': [], 'skipped': [],
                'valid_until': '2026-09-16T07:04:18+08:00', 'stale_files_allowed': 0,
            },
        },
        'documents': {'states': {'parsed': 7, 'non_pdf': 1}},
        'missing': {'total': 5},
        'non_pdf': 1, 'pdf_unparsed': 0, 'pdf_invalid_or_no_document': 0,
        'refresh_candidates': {'total': 2},
        'in_scope_totals': {'nev': {'label': '新能源', 'total': 13, 'parsed': 7, 'missing': 5, 'non_pdf': 1}},
        'tracking': {'today': '2026-09-13'},
    }
    calls = []

    def compute(*args):
        calls.append(args)
        return copy.deepcopy(summary), {'missing': [{'product_id': 'P1'}]}

    monkeypatch.setattr(inventory, 'compute_inventory', compute)
    return {
        'root': root, 'db': db, 'catalog': catalog, 'cache': cache, 'output': output,
        'logic_root': logic_root, 'summary': summary, 'calls': calls,
        'options': {'catalog_db': catalog, 'cache_dir': cache, 'pdf_root': root, 'out_dir': output},
    }


def refresh(env, **kwargs):
    return status.refresh_status(env['db'], **env['options'], **kwargs)


def read(env):
    return status.read_status(env['db'], **env['options'])


def tree_contents(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob('*') if p.is_file()}


def forbid(*_args, **_kwargs):
    pytest.fail('Read-only status must not open SQLite or compute inventory')


def test_missing_status_does_not_create_directories_or_open_database(environment, monkeypatch):
    monkeypatch.setattr(sqlite3, 'connect', forbid)
    monkeypatch.setattr(inventory, 'compute_inventory', forbid)
    result = read(environment)
    assert result['status'] == 'missing' and result['snapshot'] is None
    assert not environment['output'].exists()


def test_default_status_only_reads_saved_snapshot_and_input_metadata(environment, monkeypatch):
    saved = refresh(environment)['snapshot']
    before = tree_contents(environment['root'])
    monkeypatch.setattr(sqlite3, 'connect', forbid)
    monkeypatch.setattr(inventory, 'compute_inventory', forbid)
    result = read(environment)
    assert result['status'] == 'ready'
    assert result['snapshot'] == saved
    assert not result['stale_reasons']
    assert tree_contents(environment['root']) == before


@pytest.mark.parametrize(('change', 'reason'), [
    ('business_db', 'business_db'), ('business_wal', 'business_db'),
    ('business_journal', 'business_db'), ('catalog_db', 'catalog_db'),
    ('catalog_wal', 'catalog_db'), ('catalog_journal', 'catalog_db'),
    ('cache_edit', 'batch_caches'), ('cache_add', 'batch_caches'),
    ('cache_remove', 'batch_caches'), ('logic', 'logic'),
])
def test_input_changes_are_stale_without_recomputing(environment, monkeypatch, change, reason):
    saved = refresh(environment)['snapshot']
    if change == 'business_db':
        environment['db'].write_bytes(b'changed business')
    elif change == 'catalog_db':
        environment['catalog'].write_bytes(b'changed catalog')
    elif change.endswith('_wal') or change.endswith('_journal'):
        owner, suffix = change.split('_')
        base = environment['db' if owner == 'business' else 'catalog']
        Path(str(base) + '-' + suffix).write_bytes(b'transaction data')
    elif change == 'cache_edit':
        (environment['cache'] / 'batch409.json').write_text('{"changed":true}')
    elif change == 'cache_add':
        (environment['cache'] / 'batch410.json').write_text('{"batch":410}')
    elif change == 'cache_remove':
        (environment['cache'] / 'batch408.json').unlink()
    else:
        (environment['logic_root'] / status.LOGIC_FILES[0]).write_text('# version two\n')
    monkeypatch.setattr(inventory, 'compute_inventory', forbid)
    monkeypatch.setattr(sqlite3, 'connect', forbid)
    result = read(environment)
    assert result['status'] == 'stale' and reason in result['stale_reasons']
    assert result['snapshot'] == saved


def test_expired_caches_preserve_previous_missing_count(environment, monkeypatch):
    refresh(environment)
    monkeypatch.setattr(status, '_now', lambda: datetime(2026, 9, 20, tzinfo=timezone.utc))
    monkeypatch.setattr(inventory, 'compute_inventory', forbid)
    result = read(environment)
    assert result['cache_expired'] and result['tracking_stale']
    assert result['snapshot']['summary']['missing']['total'] == 5
    assert any('过期' in item for item in result['warnings'])


def test_unchanged_automatic_refresh_does_not_compute_inventory(environment):
    first = refresh(environment, reason='collection', run_id=24)
    before = tree_contents(environment['output'])
    second = refresh(environment, reason='collection', run_id=24)
    assert second['status'] == 'unchanged'
    assert second['snapshot'] == first['snapshot']
    assert len(environment['calls']) == 1
    assert tree_contents(environment['output']) == before


def test_two_snapshots_are_immutable_and_history_records_delta(environment):
    first = refresh(environment, reason='manual')['snapshot']
    first_folder = environment['output'] / 'snapshots' / first['generation']
    original = tree_contents(first_folder)
    environment['db'].write_bytes(b'completed collection')
    environment['summary']['documents']['states']['parsed'] = 10
    environment['summary']['missing']['total'] = 2
    second = refresh(environment, reason='collection', run_id=25)['snapshot']
    assert second['previous_generation'] == first['generation']
    assert second['delta']['parsed'] == 3
    assert second['delta']['missing_products'] == -3
    assert second['changed_inputs'] == ['business_db']
    assert tree_contents(first_folder) == original
    assert json.loads((environment['output'] / 'latest.json').read_text()) == second
    assert json.loads((first_folder / 'candidates.json').read_text()) == {'missing': [{'product_id': 'P1'}]}
    assert '不能把变化量直接视为本轮下载量' in (first_folder / 'report.md').read_text()
    entries = status.history(environment['output'])
    assert [entry['generation'] for entry in entries] == [second['generation'], first['generation']]
    assert entries[0]['run_id'] == 25
    assert len(status.history(environment['output'], limit=1)) == 1


def test_force_refresh_revalidates_even_when_inputs_are_unchanged(environment):
    one = refresh(environment)['snapshot']
    two = refresh(environment, force=True)['snapshot']
    assert one['generation'] != two['generation']
    assert len(environment['calls']) == 2
    assert set(two['delta'].values()) == {0}


@pytest.mark.parametrize('failure', ['exception', 'input_changed', 'no_complete_cache', 'unfinished_run'])
def test_failed_refresh_preserves_latest_and_records_failure(environment, monkeypatch, failure):
    refresh(environment)
    latest = (environment['output'] / 'latest.json').read_bytes()
    previous_history = status.history(environment['output'])

    def broken(*_args):
        summary = copy.deepcopy(environment['summary'])
        if failure == 'exception':
            raise RuntimeError('fixture inventory failed')
        if failure == 'input_changed':
            environment['catalog'].write_bytes(b'concurrent writer changed this input')
        if failure == 'no_complete_cache':
            summary['inputs']['batch_caches']['complete_files'] = 0
            summary['missing']['total'] = 0
        if failure == 'unfinished_run':
            summary['inputs']['business_db']['unfinished_runs'] = 1
        return summary, {}

    monkeypatch.setattr(inventory, 'compute_inventory', broken)
    with pytest.raises((status.StatusError, RuntimeError)):
        refresh(environment, force=True)
    assert (environment['output'] / 'latest.json').read_bytes() == latest
    assert status.history(environment['output']) == previous_history
    assert (environment['output'] / 'last-error.json').is_file()
    result = read(environment)
    assert result['last_error']
    assert any('上次成功记录' in warning for warning in result['warnings'])


def test_reduced_complete_cache_coverage_must_not_publish_smaller_gap(environment, monkeypatch):
    refresh(environment)
    latest = (environment['output'] / 'latest.json').read_bytes()
    environment['summary']['inputs']['batch_caches'].update({
        'complete_files': 1, 'batch_range': [409, 409],
        'skipped': [{'file': 'batch408.json', 'reason': 'cache_is_complete=False'}],
    })
    environment['summary']['missing']['total'] = 0
    with pytest.raises(status.StatusError):
        refresh(environment, force=True)
    assert (environment['output'] / 'latest.json').read_bytes() == latest


def test_replaced_batch_cannot_hide_lost_coverage_at_equal_file_count(environment):
    refresh(environment)
    original = (environment['output'] / 'latest.json').read_bytes()
    environment['summary']['inputs']['batch_caches']['complete_batches'] = [409, 410]
    with pytest.raises(status.StatusError, match='完整批次缓存减少'):
        refresh(environment, force=True)
    assert (environment['output'] / 'latest.json').read_bytes() == original


@pytest.mark.parametrize('raw', ['{broken-json', '{"format_version":0,"generation":"old"}'])
def test_explicit_refresh_preserves_invalid_record_and_rebuilds_safely(environment, raw):
    environment['output'].mkdir(parents=True)
    latest = environment['output'] / 'latest.json'
    latest.write_text(raw)
    with pytest.raises(status.StatusError):
        refresh(environment)
    assert latest.read_text() == raw
    assert not environment['calls']
    result = refresh(environment, force=True)
    assert result['status'] == 'updated'
    assert result['snapshot']['previous_generation'] is None
    saved = list(environment['output'].glob('invalid-latest-*.json'))
    assert len(saved) == 1 and saved[0].read_text() == raw
    assert not (environment['output'] / 'last-error.json').exists()
    assert read(environment)['status'] == 'ready'


def test_failed_latest_publication_does_not_replace_previous_snapshot(environment, monkeypatch):
    refresh(environment)
    original = (environment['output'] / 'latest.json').read_bytes()
    atomic = status._atomic_json

    def fail_latest(path, value):
        if path.name == 'latest.json':
            raise OSError('simulated disk error publishing latest')
        return atomic(path, value)

    monkeypatch.setattr(status, '_atomic_json', fail_latest)
    with pytest.raises(OSError, match='simulated disk error'):
        refresh(environment, force=True)
    assert (environment['output'] / 'latest.json').read_bytes() == original
    assert len(status.history(environment['output'])) == 1


def test_refresh_lock_conflict_preserves_latest(environment):
    refresh(environment)
    original = (environment['output'] / 'latest.json').read_bytes()
    with status._lock(environment['output']):
        with pytest.raises(status.StatusError, match='已有统计更新'):
            refresh(environment, force=True)
    assert (environment['output'] / 'latest.json').read_bytes() == original
    assert len(environment['calls']) == 1


@pytest.mark.parametrize('output_kind', ['business_parent', 'catalog_file', 'cache', 'cache_child', 'pdf_tree'])
def test_output_cannot_overlap_database_or_cache_inputs(environment, output_kind):
    targets = {'business_parent': environment['db'].parent, 'catalog_file': environment['catalog'],
               'cache': environment['cache'], 'cache_child': environment['cache'] / 'reports',
               'pdf_tree': environment['root'] / 'downloads/announcement_site/reports'}
    options = {**environment['options'], 'out_dir': targets[output_kind]}
    original = tree_contents(environment['root'])
    with pytest.raises(status.StatusError, match='重叠'):
        status.refresh_status(environment['db'], **options)
    assert tree_contents(environment['root']) == original
    assert not environment['calls']


def cli_options(env):
    return ['--db', str(env['db']), '--catalog-db', str(env['catalog']),
            '--pdf-root', str(env['root']), '--cache-dir', str(env['cache']),
            '--out-dir', str(env['output'])]


def test_cli_json_and_history_are_read_only(environment, monkeypatch, capsys):
    refresh(environment)
    monkeypatch.setattr(inventory, 'compute_inventory', forbid)
    monkeypatch.setattr(sqlite3, 'connect', forbid)
    assert status.main(cli_options(environment) + ['--json']) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['status'] == 'ready'
    assert value['snapshot']['summary']['missing']['total'] == 5
    assert status.main(cli_options(environment) + ['--history']) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1


def test_cli_missing_returns_two_without_creating_output(environment, capsys):
    assert status.main(cli_options(environment) + ['--json']) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'missing'
    assert not environment['output'].exists()


def test_cli_refresh_is_explicit_and_outputs_new_record(environment, capsys):
    assert status.main(cli_options(environment) + ['--refresh', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['snapshot']['summary']['missing']['total'] == 5
    assert len(environment['calls']) == 1


def test_cli_history_and_refresh_cannot_be_combined(environment):
    with pytest.raises(SystemExit) as error:
        status.main(cli_options(environment) + ['--refresh', '--history'])
    assert error.value.code == 2
    assert not environment['output'].exists()


def test_collection_hook_keeps_committed_work_when_status_fails(environment, monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise status.StatusError('fixture independent statistics failure')

    monkeypatch.setattr(status, 'refresh_status', fail)
    original = environment['db'].read_bytes()
    result = status.refresh_after_collection(environment['db'], catalog_db=environment['catalog'],
                                             pdf_root=environment['root'], run_id=24)
    assert result is None
    assert environment['db'].read_bytes() == original
    assert '收录统计更新失败' in capsys.readouterr().err


def test_custom_database_with_shared_pdf_root_cannot_replace_primary_status(environment):
    refresh(environment)
    primary = (environment['output'] / 'latest.json').read_bytes()
    alternate = environment['db'].with_name('trial.sqlite')
    alternate.write_bytes(b'independent trial database')
    result = status.refresh_status(alternate, catalog_db=environment['catalog'],
                                   cache_dir=environment['cache'], pdf_root=environment['root'])
    assert result['status'] == 'updated'
    assert (environment['output'] / 'latest.json').read_bytes() == primary
    actual = status.read_status(alternate, catalog_db=environment['catalog'],
                                cache_dir=environment['cache'], pdf_root=environment['root'])
    assert actual['snapshot']['generation'] == result['snapshot']['generation']
    assert actual['snapshot']['generation'] != read(environment)['snapshot']['generation']


def cache_diagnostics(**changes):
    values = {'format_version': 1, 'known_source_absent': [], 'partial_failed': [],
              'invalid_cache': [], 'missing_cache': [], 'superseded_evidence': [],
              'coverage_limited': False, 'has_actionable_warning': False}
    values.update(changes)
    return values


def test_known_source_absence_is_a_dated_notice_not_a_cache_failure(environment, monkeypatch):
    caches = environment['summary']['inputs']['batch_caches']
    caches['skipped'] = [{'file': 'batch210.absent.json', 'reason': 'legacy unclassified'}]
    caches['cache_diagnostics'] = cache_diagnostics(known_source_absent=[{
        'batch': 210, 'file': 'batch210.absent.json', 'reason': '上游查询接口缺表',
        'fetched_at': '2026-09-09T03:14:50+00:00', 'retained_complete_cache': False,
    }], coverage_limited=True)
    saved = refresh(environment)['snapshot']
    monkeypatch.setattr(sqlite3, 'connect', forbid)
    monkeypatch.setattr(inventory, 'compute_inventory', forbid)
    result = read(environment)
    assert not result['cache_incomplete'] and not result['warnings']
    assert any('210' in line and '2026-09-09' in line for line in result['notices'])
    assert '210' in status.render_report(saved)
    assert any('不' in line and '完整' in line for line in result['notices'])


def test_new_partial_failure_remains_visible_alongside_known_source_absence(environment):
    caches = environment['summary']['inputs']['batch_caches']
    caches['cache_diagnostics'] = cache_diagnostics(
        known_source_absent=[{'batch': 210, 'file': 'batch210.absent.json',
                              'reason': '接口缺表', 'fetched_at': '2026-09-09T00:00:00Z'}],
        partial_failed=[{'batch': 409, 'file': 'batch409.partial.json',
                         'reason': '企业枚举请求超时', 'fetched_at': '2026-09-13T00:00:00Z',
                         'retained_complete_cache': True}],
        coverage_limited=True, has_actionable_warning=True)
    refresh(environment)
    result = read(environment)
    assert result['cache_incomplete']
    assert any('409' in line and '企业枚举请求超时' in line and '保留' in line
               for line in result['warnings'])
    assert not any('210' in line for line in result['warnings'])
    assert any('210' in line for line in result['notices'])


def test_superseded_cache_evidence_is_not_a_current_warning(environment):
    caches = environment['summary']['inputs']['batch_caches']
    caches['skipped'] = [{'file': 'batch409.partial.json'}]
    caches['cache_diagnostics'] = cache_diagnostics(superseded_evidence=[{
        'batch': 409, 'file': 'batch409.partial.json', 'reason': '已被后续完整缓存替代',
        'fetched_at': '2026-09-01T00:00:00Z'}])
    refresh(environment)
    result = read(environment)
    assert not result['cache_incomplete'] and not result['warnings'] and not result['notices']


def test_unclassified_legacy_snapshot_keeps_conservative_warning(environment):
    environment['summary']['inputs']['batch_caches']['skipped'] = [{'file': 'batch210.absent.json'}]
    refresh(environment)
    result = read(environment)
    assert result['cache_incomplete']
    assert any('未分类' in line and '--refresh' in line for line in result['warnings'])
