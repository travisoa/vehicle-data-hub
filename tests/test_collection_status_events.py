"""真实统计引擎验证新正式批次与正式重发经现有入口更新持久记录。"""
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
from miit_gonggao import republished
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


def test_republished_product_is_listed_from_formal_batches_not_notices(data, monkeypatch):
    """同一产品 ID 在更高正式批次再次出现即为重发；公示不参与这条判定。"""
    first = data.bootstrap()
    assert first['summary']['documents']['states'] == {'parsed': 1}
    reissued = product('old', model='TEST1000BEV', batch=410)
    brand_new = product('fresh', model='TEST2000BEV', batch=410)
    data.cache_batch(410, [reissued, brand_new])

    source = republished.from_batches(data.db, [410], data.cache)
    assert [row['product_id'] for row in source.rows] == ['old']
    assert source.rows[0]['republished_batch'] == 410
    assert source.rows[0]['local_batch'] == 409

    # 统计入口给出同一份候选，且不需要重新枚举缓存。
    status.refresh_status(data.db, catalog_db=data.catalog, pdf_root=data.pdf_root, reason='batch_cache')
    from_status = republished.from_status(data.db, catalog_db=data.catalog, pdf_root=data.pdf_root,
                                          cache_dir=data.cache, out_dir=data.output)
    assert [row['product_id'] for row in from_status.rows] == ['old']
    assert from_status.generation == data.read()['snapshot']['generation']


def test_legacy_notice_events_never_enter_the_retry_plan(data):
    """公示已不再登记，但旧库历史行仍会被 events() 读出：它们不能驱动查询和下载。"""
    import hashlib
    from datetime import date

    url = 'https://www.miit.gov.cn/example.html'
    with sqlite3.connect(data.db) as conn:
        tracking.register_event(conn, model_code='ZZ6000BEV', batch=410, kind='formal',
                                source_url=url, event_date='2026-09-10',
                                metadata={'product_name': '纯电动轿车'})
        legacy_id = hashlib.sha256(
            json.dumps(['ZZ7000BEV', 410, 'change_notice', ''], ensure_ascii=False).encode()
        ).hexdigest()[:24]
        conn.execute(
            "insert into tracking_events(event_id,model_code,batch,kind,product_id,source_url,"
            "title,event_date,first_seen_at,metadata_json) values (?,?,410,'change_notice','',?,?,?,?,?)",
            (legacy_id, 'ZZ7000BEV', url, '第410批变更扩展公示', '2026-09-10', '2026-09-10',
             json.dumps({'product_name': '纯电动轿车'}, ensure_ascii=False)))

    with sqlite3.connect(data.db) as conn:
        plan = tracking.retry_plan(conn, data.pdf_root, today=date(2026, 9, 16))
        # 同批同窗口下，正式事件照常待采，公示只被记录为历史，不进任何请求分组。
        assert [e['model_code'] for e in plan['pending']] == ['ZZ6000BEV']
        assert plan['legacy_notice'] == [legacy_id]
        assert plan['query_groups'] == 1
        assert legacy_id not in plan['dormant'] + plan['excluded'] + plan['scope_review']
        # 源库历史原样保留。
        assert sorted(e['kind'] for e in tracking.events(conn)) == ['change_notice', 'formal']


def test_republished_from_status_defaults_to_the_same_dir_as_the_status_record(data):
    """不显式传 out_dir 时，候选明细必须跟着业务库走，而不是项目默认目录。"""
    data.bootstrap()
    data.cache_batch(410, [product('old', model='TEST1000BEV', batch=410)])
    status.refresh_status(data.db, catalog_db=data.catalog, pdf_root=data.pdf_root, reason='batch_cache')
    assert status.status_output_dir(data.db) == data.output
    source = republished.from_status(data.db, catalog_db=data.catalog, pdf_root=data.pdf_root)
    assert [row['product_id'] for row in source.rows] == ['old']
    assert source.generation == data.read()['snapshot']['generation']


def test_republished_from_status_refuses_a_stale_record(data, monkeypatch):
    data.bootstrap()
    data.cache_batch(410, [product('old', model='TEST1000BEV', batch=410)])
    assert data.read()['status'] == 'stale'
    with pytest.raises(republished.RepublishedError, match='统计记录'):
        republished.from_status(data.db, catalog_db=data.catalog, pdf_root=data.pdf_root,
                                cache_dir=data.cache, out_dir=data.output)
    allowed = republished.from_status(data.db, catalog_db=data.catalog, pdf_root=data.pdf_root,
                                      cache_dir=data.cache, out_dir=data.output, allow_stale=True)
    assert allowed.rows == []  # 过期记录里还没有这次重发，正说明它可能漏项
