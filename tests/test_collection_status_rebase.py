"""来源基线调整必须精确授权、保留历史，并继续服从统计发布保护。"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

from miit_gonggao import collection_inventory as inventory
from miit_gonggao import collection_status as status


def cache_scope(batches):
    return {'complete_files': len(batches), 'complete_batches': list(batches),
            'batch_range': [min(batches), max(batches)] if batches else None,
            'files': [{'batch': batch, 'count': 10} for batch in batches],
            'skipped': [], 'valid_until': '2026-09-20T00:00:00+08:00'}


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / 'hub'
    db = root / 'data' / 'announcement_site.sqlite'
    catalog = root / 'data' / 'jianmian_catalog.sqlite'
    cache = root / 'downloads' / 'announcement_batches'
    output = root / 'var' / 'reports' / 'collection-status'
    db.parent.mkdir(parents=True)
    cache.mkdir(parents=True)
    db.write_bytes(b'fixture business')
    catalog.write_bytes(b'fixture catalog')
    for batch in [408, 409]:
        (cache / f'batch{batch}.json').write_text(json.dumps({'batch': batch}))
    monkeypatch.setattr(status, '_now', lambda: datetime(2026, 9, 13, 15, tzinfo=timezone.utc))
    summary = {'inputs': {'business_db': {'unfinished_runs': 0}, 'batch_caches': cache_scope([408, 409])},
               'documents': {'states': {'parsed': 7}}, 'missing': {'total': 5},
               'tracking': {'today': '2026-09-13'}}
    calls = []

    def compute(*args):
        calls.append(args)
        return copy.deepcopy(summary), {'missing': [{'product_id': 'P1'}]}

    monkeypatch.setattr(inventory, 'compute_inventory', compute)
    options = {'catalog_db': catalog, 'cache_dir': cache, 'pdf_root': root, 'out_dir': output}
    original = status.refresh_status(db, **options)['snapshot']
    return {'db': db, 'catalog': catalog, 'cache': cache, 'output': output, 'options': options,
            'summary': summary, 'calls': calls, 'original': original}


def refresh(env, **overrides):
    options = {'force': True, 'rebase_cache': True, 'expect_generation': env['original']['generation'],
               'removed_batches': [408], 'rebase_reason': '旧批次校验规则修订，已逐项核验来源范围'}
    return status.refresh_status(env['db'], **env['options'], **(options | overrides))


def shrink(env, batches=None):
    env['summary']['inputs']['batch_caches'] = cache_scope([409] if batches is None else batches)
    env['summary']['missing']['total'] = 2
    (env['cache'] / 'batch408.json').unlink()


def latest_bytes(env):
    return (env['output'] / 'latest.json').read_bytes()


def assert_old_latest(env):
    assert json.loads(latest_bytes(env)) == env['original']
    assert status.history(env['output'])[0]['generation'] == env['original']['generation']


def test_rebase_retains_history_scope_and_distinguishes_scope_delta(env):
    original_bytes = latest_bytes(env)
    old_folder = env['output'] / 'snapshots' / env['original']['generation']
    old_files = {path.name: path.read_bytes() for path in old_folder.iterdir()}
    shrink(env, [409, 410])  # 新增批次不能掩盖旧批次减少，即使文件数量相等。
    (env['cache'] / 'batch410.json').write_text('{"batch":410}')
    new = refresh(env)['snapshot']
    assert new['previous_generation'] == env['original']['generation']
    assert new['cache_rebase'] == {
        'reason': '旧批次校验规则修订，已逐项核验来源范围',
        'expected_generation': env['original']['generation'], 'removed_batches': [408], 'added_batches': [410],
        'old_scope': {'complete_files': 2, 'complete_batches': [408, 409], 'batch_range': [408, 409]},
        'new_scope': {'complete_files': 2, 'complete_batches': [409, 410], 'batch_range': [409, 410]},
    }
    assert new['delta']['missing_products'] == -3
    assert '不代表下载增减' in new['delta_note']
    report = (env['output'] / 'snapshots' / new['generation'] / 'report.md').read_text()
    assert '## 来源基线变化' in report and '相对上一记录（含来源范围变化）' in report
    assert '明确移除批次：408；新增批次：410' in report
    assert '不代表下载增减' in report
    assert {path.name: path.read_bytes() for path in old_folder.iterdir()} == old_files
    assert old_files['summary.json'] == original_bytes
    records = status.history(env['output'])
    assert [item['generation'] for item in records] == [new['generation'], env['original']['generation']]
    assert records[0]['cache_rebase'] == new['cache_rebase']
    assert records[0]['delta_note'] == new['delta_note']


def test_successful_rebase_restores_ordinary_automatic_updates_and_still_protects_next_reduction(env):
    shrink(env)
    rebased = refresh(env)['snapshot']
    count = len(env['calls'])
    assert status.refresh_status(env['db'], **env['options'])['status'] == 'unchanged'
    assert len(env['calls']) == count
    env['db'].write_bytes(b'next successful collection')
    env['summary']['documents']['states']['parsed'] = 8
    env['summary']['missing']['total'] = 1
    updated = status.refresh_status(env['db'], **env['options'], run_id=99, reason='collection')['snapshot']
    assert updated['previous_generation'] == rebased['generation']
    assert updated['delta']['parsed'] == 1 and 'cache_rebase' not in updated
    env['summary']['inputs']['batch_caches'] = cache_scope([410])
    before = latest_bytes(env)
    with pytest.raises(status.StatusError, match='完整批次缓存减少'):
        status.refresh_status(env['db'], **env['options'], force=True)
    assert latest_bytes(env) == before


def test_ordinary_refresh_requires_repair_or_explicit_rebase(env):
    shrink(env)
    with pytest.raises(status.StatusError, match='完整批次缓存减少') as error:
        status.refresh_status(env['db'], **env['options'], force=True)
    message = str(error.value)
    assert '--verify-every 1' in message and '--rebase-cache' in message
    assert '--expect-generation ' + env['original']['generation'] in message
    assert '--removed-batches 408' in message and '删除' not in message
    assert_old_latest(env)
    env['summary']['inputs']['batch_caches'] = cache_scope([408, 409])
    (env['cache'] / 'batch408.json').write_text('{"batch":408,"repaired":true}')
    repaired = status.refresh_status(env['db'], **env['options'])['snapshot']
    assert repaired['previous_generation'] == env['original']['generation']
    assert 'cache_rebase' not in repaired
    assert not (env['output'] / 'last-error.json').exists()


@pytest.mark.parametrize('changes', [
    {'expect_generation': '20260913T150000000000Z-000000000000'},
    {'removed_batches': [409]}, {'removed_batches': [408, 409]},
    {'removed_batches': []}, {'removed_batches': [408, 408]}, {'removed_batches': [True]},
    {'removed_batches': ['408']}, {'removed_batches': [-408]},
    {'rebase_reason': ''}, {'rebase_reason': '   '}, {'expect_generation': 'invalid'},
    {'force': False}, {'rebase_cache': False},
    {'expect_generation': None}, {'removed_batches': None}, {'rebase_reason': None},
])
def test_rebase_rejects_mismatches_empty_reasons_and_incomplete_parameter_combinations(env, changes):
    shrink(env)
    before = latest_bytes(env)
    with pytest.raises(status.StatusError):
        refresh(env, **changes)
    assert latest_bytes(env) == before
    assert_old_latest(env)


def test_stale_generation_rejected_before_recomputation(env):
    shrink(env)
    count = len(env['calls'])
    with pytest.raises(status.StatusError, match='旧记录代号'):
        refresh(env, expect_generation='20260913T150000000000Z-000000000000')
    assert len(env['calls']) == count
    assert_old_latest(env)


def test_removed_batch_list_cannot_omit_one_actual_reduction(env):
    # 当前完整批次集合换成另一批；授权只移除其中一批不足。
    shrink(env, [410])
    with pytest.raises(status.StatusError, match='实际减少批次'):
        refresh(env, removed_batches=[408])
    assert_old_latest(env)


def test_rebase_cannot_be_used_when_no_batch_was_removed(env):
    with pytest.raises(status.StatusError, match='实际减少批次'):
        refresh(env)
    assert_old_latest(env)


@pytest.mark.parametrize('owner', ['old', 'new'])
@pytest.mark.parametrize('field,value', [
    ('complete_batches', None), ('complete_batches', []), ('complete_batches', [409, 409]),
    ('complete_batches', ['409']), ('complete_files', 99), ('complete_files', True),
    ('batch_range', [408, 409]), ('files', None), ('files', [{'batch': 408}]),
])
def test_rebase_requires_verifiable_complete_set_metadata(env, owner, field, value):
    shrink(env)
    if owner == 'old':
        previous = copy.deepcopy(env['original'])
        previous['summary']['inputs']['batch_caches'] = cache_scope([409])
        previous['summary']['inputs']['batch_caches'][field] = value
        (env['output'] / 'latest.json').write_text(json.dumps(previous))
    else:
        env['summary']['inputs']['batch_caches'][field] = value
    before = latest_bytes(env)
    with pytest.raises(status.StatusError, match='完整批次元数据'):
        refresh(env)
    assert latest_bytes(env) == before


@pytest.mark.parametrize('failure', ['empty_cache', 'unfinished', 'input_changed', 'compute_error'])
def test_rebase_preserves_existing_publication_guards(env, monkeypatch, failure):
    shrink(env)

    def compute(*_args):
        result = copy.deepcopy(env['summary'])
        if failure == 'empty_cache':
            result['inputs']['batch_caches'] = cache_scope([])
        elif failure == 'unfinished':
            result['inputs']['business_db']['unfinished_runs'] = 1
        elif failure == 'input_changed':
            env['catalog'].write_bytes(b'concurrent catalog update')
        else:
            raise RuntimeError('fixture compute error')
        return result, {}

    monkeypatch.setattr(inventory, 'compute_inventory', compute)
    before = latest_bytes(env)
    with pytest.raises((status.StatusError, RuntimeError)):
        refresh(env)
    assert latest_bytes(env) == before
    assert (env['output'] / 'last-error.json').is_file()
    assert_old_latest(env)


def test_rebase_cannot_bypass_statistics_lock(env):
    shrink(env)
    with status._lock(env['output']):
        with pytest.raises(status.StatusError, match='已有统计更新'):
            refresh(env)
    assert_old_latest(env)


def test_rebase_publication_is_atomic(env, monkeypatch):
    shrink(env)
    atomic_json = status._atomic_json

    def fail_latest(path, value):
        if path.name == 'latest.json':
            raise OSError('fixture publication failure')
        return atomic_json(path, value)

    monkeypatch.setattr(status, '_atomic_json', fail_latest)
    with pytest.raises(OSError, match='publication failure'):
        refresh(env)
    assert_old_latest(env)
    assert len(status.history(env['output'])) == 1


@pytest.mark.parametrize('latest', [None, '{broken-json', '{"format_version":0,"generation":"old"}'])
def test_rebase_never_uses_missing_or_corrupt_old_snapshot_as_unrestricted_first_record(env, latest):
    shrink(env)
    path = env['output'] / 'latest.json'
    if latest is None:
        path.unlink()
    else:
        path.write_text(latest)
    with pytest.raises(status.StatusError):
        refresh(env)
    assert path.read_text() == latest if latest is not None else not path.exists()
    assert not list(env['output'].glob('invalid-latest-*.json'))


def cli_options(env):
    return ['--db', str(env['db']), '--catalog-db', str(env['catalog']), '--pdf-root', str(env['options']['pdf_root']),
            '--cache-dir', str(env['cache']), '--out-dir', str(env['output'])]


def test_cli_rebase_and_history_report_scope_change(env, capsys):
    shrink(env)
    assert status.main(cli_options(env) + ['--refresh', '--rebase-cache', '--expect-generation',
                      env['original']['generation'], '--removed-batches', '408', '--rebase-reason',
                      '明确核验了批次范围调整', '--json']) == 0
    new = json.loads(capsys.readouterr().out)['snapshot']
    assert new['cache_rebase']['removed_batches'] == [408]
    assert status.main(cli_options(env) + ['--history']) == 0
    records = json.loads(capsys.readouterr().out)
    assert records[0]['cache_rebase']['reason'] == '明确核验了批次范围调整'
    assert '不代表下载增减' in records[0]['delta_note']


@pytest.mark.parametrize('args', [
    ['--rebase-cache'], ['--expect-generation', 'unused'], ['--removed-batches', '408'],
    ['--rebase-reason', 'reason'], ['--history', '--rebase-cache'],
    ['--refresh', '--rebase-cache', '--removed-batches', '408,'],
    ['--refresh', '--rebase-cache', '--removed-batches', '408,408'],
    ['--refresh', '--rebase-cache', '--removed-batches', '４０８'],
])
def test_cli_rejects_incomplete_combinations_and_bad_batch_syntax(env, args):
    with pytest.raises(SystemExit) as error:
        status.main(cli_options(env) + args)
    assert error.value.code == 2
    assert_old_latest(env)
