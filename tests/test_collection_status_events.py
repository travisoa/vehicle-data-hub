"""真实统计引擎验证新正式批次、扩展公示经现有入口更新持久记录。"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone

import pytest

from miit_gonggao import collection as collection
from miit_gonggao import collection_inventory as inventory
from miit_gonggao import collection_status as status
from miit_gonggao import collection_tracking as tracking
from scripts import announcement_catalog_gap as gap
from test_collection_inventory import InventoryFixture, product


class StatusFixture(InventoryFixture):
    """沿用逐产品fixture，路径按生产入口的自定义上游根约定隔离。"""
    def __init__(self, root):
        self.pdf_root = root
        self.db = root / 'data/announcement_site.sqlite'
        self.catalog = root / 'data/jianmian_catalog.sqlite'
        self.cache = root / 'downloads/announcement_batches'
        self.output = root / 'var/reports/collection-status'
        self.db.parent.mkdir(parents=True)
        self.cache.mkdir(parents=True)
        with sqlite3.connect(self.db) as conn:
            conn.executescript(collection.SCHEMA)
            tracking.ensure_schema(conn)
        with sqlite3.connect(self.catalog) as conn:
            conn.execute('CREATE TABLE catalog_rows(model_code TEXT,energy_type TEXT,'
                         'common_name TEXT,catalog TEXT)')

    def read(self):
        return status.read_status(self.db, catalog_db=self.catalog, pdf_root=self.pdf_root)

    def bootstrap(self):
        old = product('old', model='TEST1000BEV', batch=409)
        self.announcement(old)
        self.cache_batch(409, [old])
        return status.refresh_status(self.db, catalog_db=self.catalog,
                                     pdf_root=self.pdf_root, reason='fixture_bootstrap')['snapshot']


@pytest.fixture
def data(tmp_path, monkeypatch):
    data = StatusFixture(tmp_path / 'isolated-hub')
    now = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(status, '_now', lambda: now)
    monkeypatch.setattr(inventory, 'datetime', FixedDateTime)
    monkeypatch.setattr(collection, 'UPSTREAM_ROOT', data.pdf_root)
    monkeypatch.setattr(gap, 'CACHE_DIR', data.cache)

    def forbid(*_args, **_kwargs):
        pytest.fail('端到端统计测试禁止网络请求和PDF下载')

    monkeypatch.setattr(collection.core, 'post_form', forbid)
    monkeypatch.setattr(collection.core, 'query_all_pages', forbid)
    monkeypatch.setattr(collection.core, 'download_param_page', forbid)
    return data


def run_tracking(data, monkeypatch, *args):
    monkeypatch.setattr(sys, 'argv', ['tracking', '--site-db', str(data.db),
                                     '--catalog-db', str(data.catalog), '--pdf-root', str(data.pdf_root),
                                     *args])
    assert tracking.main() == 0


def test_new_batch_cache_and_registration_publish_real_snapshots(data, monkeypatch):
    first = data.bootstrap()
    assert first['summary']['missing']['total'] == 0
    assert data.read()['status'] == 'ready'
    new = product('new', model='TEST1001BEV', batch=410)
    payload = {'cache_version': gap.CACHE_VERSION, 'complete': True, 'batch': 410,
               'source': 'https://www.miit.gov.cn/example.html', 'published_at': '2026-09-12',
               'fetched_at': '2026-09-13T10:00:00+00:00', 'failures': [],
               'verify_prefixes': list(gap.VERIFY_PREFIXES), 'verify_missed': 0, 'products': [new]}

    def fetch_fixture(batch, **_kwargs):
        assert batch == '410'
        gap.write_cache(data.cache / 'batch410.json', payload)
        return {'new': new}

    # 只替换源站枚举，写缓存、命令收尾hook和统计引擎均为真实实现。
    monkeypatch.setattr(gap, 'load_batch', fetch_fixture)
    assert gap.main(['--batch', '410', '--catalog-db', str(data.catalog), '--fetch-only']) == 0
    cached = data.read()
    assert cached['status'] == 'ready'
    second = cached['snapshot']
    assert second['generation'] != first['generation']
    assert second['reason'] == 'batch_cache'
    assert second['summary']['missing']['total'] == 1
    assert second['delta']['missing_products'] == 1
    candidate_file = data.output / 'snapshots' / second['generation'] / 'candidates.json'
    assert [row['product_id'] for row in json.loads(candidate_file.read_text())['missing']] == ['new']

    run_tracking(data, monkeypatch, 'register-batch', str(data.cache / 'batch410.json'))
    registered = data.read()
    assert registered['status'] == 'ready'
    third = registered['snapshot']
    assert third['generation'] != second['generation']
    assert third['reason'] == 'register-batch'
    assert third['summary']['missing']['total'] == 1
    events = third['summary']['tracking']['events']
    assert len(events) == 1
    assert events[0]['kind'] == 'formal' and events[0]['batch'] == 410
    assert events[0]['status'] == 'pending' and events[0]['bucket'] == 'due'
    history = status.history(data.output)
    assert [entry['generation'] for entry in history] == [
        third['generation'], second['generation'], first['generation']]
    assert history[1]['delta']['missing_products'] == 1


def test_new_extension_keeps_old_pdf_and_persists_unresolved_event(data, monkeypatch):
    first = data.bootstrap()
    assert first['summary']['documents']['states'] == {'parsed': 1}
    source = collection.change_notice.ChangeNoticeSource(
        notice_url='https://www.miit.gov.cn/change-410.html', title='第410批变更扩展公示',
        published_at='2026-09-12', batch='410', iframe_url='https://www.miit.gov.cn/change-410/index.html',
        unit_url='https://www.miit.gov.cn/api/unit', unit_params={})
    notice = {'model_code': 'TEST1000BEV', 'product_name': '纯电动载货汽车', 'company': '测试公司'}
    monkeypatch.setattr(collection.change_notice, 'load_change_notice_source', lambda _url: source)
    monkeypatch.setattr(collection.change_notice, 'query_change_notice', lambda _source: ([notice], 1))

    # 仅替换官方响应，原始快照、登记事务、自动刷新和统计引擎全部实际执行。
    run_tracking(data, monkeypatch, 'register-notice', '--url', source.notice_url, '--kind', 'change_notice')
    current = data.read()
    assert current['status'] == 'ready'
    second = current['snapshot']
    assert second['generation'] != first['generation']
    assert second['reason'] == 'register-notice'
    assert second['summary']['documents']['states'] == {'parsed': 1}
    events = second['summary']['tracking']['events']
    assert len(events) == 1
    assert events[0]['kind'] == 'change_notice' and events[0]['batch'] == 410
    assert events[0]['identity'] == 'model_only'
    assert events[0]['status'] == 'pending' and events[0]['bucket'] == 'due'
    assert not any(event['bucket'] == 'resolved_valid_pdf' for event in events)
    with sqlite3.connect(data.db) as conn:
        plan = tracking.retry_plan(conn, data.pdf_root, today=inventory.datetime.now(inventory.TZ).date())
        assert not plan['resolved']
        assert len(plan['pending']) == 1 and plan['pending'][0]['batch'] == 410
    assert list((data.pdf_root / 'downloads/announcement_site' /
                 collection.core.ANNOUNCEMENT_SNAPSHOT_DIRNAME).glob('change_notice_seed_batch410_*.json'))
    history = status.history(data.output)
    assert [entry['generation'] for entry in history] == [second['generation'], first['generation']]
    assert 'business_db' in second['changed_inputs']
