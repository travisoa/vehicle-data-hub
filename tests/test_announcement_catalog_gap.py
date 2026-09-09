from datetime import datetime, timezone
import json

import pytest

from scripts import announcement_catalog_gap as gap


def test_partial_query_cannot_be_reused_as_success(tmp_path, monkeypatch):
    monkeypatch.setattr(gap, 'CACHE_DIR', tmp_path)
    monkeypatch.setattr(gap, 'probe_batch', lambda batch: None)
    def query(**kwargs):
        if kwargs.get('company') == '公司':
            raise TimeoutError('timeout')
        return []
    monkeypatch.setattr(gap.core, 'query_all_pages', query)
    with pytest.raises(RuntimeError, match='不完整'):
        gap.load_batch('409')
    assert not (tmp_path / 'batch409.json').exists()
    payload = json.loads((tmp_path / 'batch409.partial.json').read_text())
    assert not payload['complete']
    assert payload['failures']
    calls = []
    monkeypatch.setattr(gap.core, 'query_all_pages', lambda **kw: calls.append(kw) or [])
    gap.load_batch('409')
    assert calls
    assert json.loads((tmp_path / 'batch409.json').read_text())['complete']


def test_absent_batch_is_reprobed_and_old_cache_not_destroyed(tmp_path, monkeypatch):
    monkeypatch.setattr(gap, 'CACHE_DIR', tmp_path)
    old = {'cache_version': 3, 'products': [{'cpid': 'old'}]}
    (tmp_path / 'batch410.json').write_text(json.dumps(old))
    monkeypatch.setattr(gap, 'probe_batch', lambda batch: (_ for _ in ()).throw(gap.BatchAbsent('missing')))
    assert gap.load_batch('410') == {}
    assert json.loads((tmp_path / 'batch410.json').read_text()) == old
    calls = []
    monkeypatch.setattr(gap, 'probe_batch', lambda batch: calls.append(batch))
    monkeypatch.setattr(gap.core, 'query_all_pages', lambda **kw: [])
    gap.load_batch('410')
    assert calls == ['410']


def test_unverified_and_expired_cache_require_refresh():
    payload = dict(cache_version=gap.CACHE_VERSION, complete=True, products=[],
                   fetched_at=datetime.now(timezone.utc).isoformat(), verify_prefixes=[])
    assert not gap.cache_is_complete(payload)
    assert gap.cache_is_complete(payload, verify=False)
    payload['verify_prefixes'] = list(gap.VERIFY_PREFIXES)
    assert gap.cache_is_complete(payload)
    payload['fetched_at'] = '2020-01-01T00:00:00+00:00'
    assert not gap.cache_is_complete(payload)
