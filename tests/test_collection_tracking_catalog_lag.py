"""购置税目录滞后收录：目录事件按合刊批次查无，而官方正式公告只在更早批次。

真实例子：TZ5030TSYHEV 公告第408批（AC079100），却进了随第410批合刊发布的
《减免车辆购置税的新能源汽车车型目录》第三十四批。register_catalog 按合刊批次登记的
无产品 ID 事件按 pc=410 永远查不到，不能一直挂着「正式公告已登记，参数页暂未取得」。
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from miit_gonggao import collection as seed
from miit_gonggao import collection_tracking as tracking
from miit_gonggao.announcement_source import ScriptedSource
from test_collection_inventory import TODAY, InventoryFixture, product

MODEL = 'TEST5030TSYHEV'


@pytest.fixture
def data(tmp_path, monkeypatch):
    data = InventoryFixture(tmp_path)
    monkeypatch.setattr(seed, 'UPSTREAM_ROOT', data.pdf_root)
    monkeypatch.setattr(seed, 'refresh_collection_status', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tracking, 'document_image_error', lambda *_args: '')
    monkeypatch.setattr(seed, 'store_announcement',
                        lambda *_a, **_k: pytest.fail('目录滞后核对不下载其他批次的产品'))
    return data


def catalog_event(data, *, batch=410, pid=''):
    with sqlite3.connect(data.db) as conn:
        return tracking.register_event(
            conn, model_code=MODEL, batch=batch, kind='formal', product_id=pid,
            source_url='https://www.miit.gov.cn/fixture', event_date=date.today().isoformat(),
            metadata={'product_name': '插电式增程混合动力宿营车', 'category': '专用车'},
        )


def official(by_batch):
    """按查询参数应答：带 pc 只返回该批次的行，不带 pc 返回全部批次。"""
    calls = []

    def query(**kwargs):
        calls.append(kwargs)
        rows = [{'cpid': f'P{b}', 'clxh': MODEL, 'gppc': str(b), 'clmc': '插电式增程混合动力宿营车'}
                for b in by_batch]
        return [r for r in rows if 'pc' not in kwargs or r['gppc'] == kwargs['pc']]

    return ScriptedSource(on_query=query), calls


def run(data, source):
    with sqlite3.connect(data.db) as conn:
        plan = tracking.retry_plan(conn, data.pdf_root)
        result = tracking.collect(conn, plan, catalog_db=data.catalog, pdf_root=data.pdf_root, source=source)
        event = tracking.events(conn)[0]
        model_status = conn.execute('select status from run_models').fetchone()[0]
    return result, event, model_status


def test_event_identity_matches_registered_event_id(data):
    assert catalog_event(data) == tracking.event_identity(MODEL, 410, 'formal')
    assert catalog_event(data, pid='AC079100') == tracking.event_identity(MODEL, 410, 'formal', 'AC079100')
    assert tracking.event_identity(MODEL.lower(), 410, 'formal') == tracking.event_identity(MODEL, 410, 'formal')


def test_catalog_event_with_only_earlier_official_batch_becomes_catalog_lag(data):
    data.announcement(product('P408', MODEL, batch=408))
    catalog_event(data)
    source, calls = official([408])
    result, event, model_status = run(data, source)
    assert [c.get('pc') for c in calls] == ['410', None]
    assert event['status'] == tracking.CATALOG_LAG
    assert '第408批' in event['last_error'] and '第410批' in event['last_error']
    # 目录滞后不是本批次的产品结局，型号结局如实记为本批次查无。
    assert model_status == 'no_match'
    assert result['errors'] == 0


def test_catalog_lag_is_terminal_and_never_retried(data):
    data.announcement(product('P408', MODEL, batch=408))
    event_id = catalog_event(data)
    source, _calls = official([408])
    run(data, source)
    later = date.today() + timedelta(days=30)
    with sqlite3.connect(data.db) as conn:
        plan = tracking.retry_plan(conn, data.pdf_root, today=later)
        assert plan['catalog_lag'] == [event_id]
        assert event_id not in [e['event_id'] for e in plan['pending']]
        assert event_id not in plan['dormant']
        # 目录重新登记只刷新来源，不把终态重置回待查。
        catalog_event(data)
        assert tracking.events(conn)[0]['status'] == tracking.CATALOG_LAG


@pytest.mark.parametrize('by_batch', [[], [410]], ids=['official-empty', 'official-has-batch'])
def test_catalog_event_stays_no_match_without_official_evidence(data, by_batch):
    data.announcement(product('P408', MODEL, batch=408))
    catalog_event(data)
    source, _calls = official(by_batch)
    if by_batch == [410]:
        # 带 pc 的查询返回空、不带 pc 却有本批次：官方口径不一致时不下结论。
        source.on_query = lambda **kw: [] if 'pc' in kw else [
            {'cpid': 'P410', 'clxh': MODEL, 'gppc': '410', 'clmc': '插电式增程混合动力宿营车'},
            {'cpid': 'P408', 'clxh': MODEL, 'gppc': '408', 'clmc': '插电式增程混合动力宿营车'}]
    _result, event, _model_status = run(data, source)
    assert event['status'] == 'no_match'


def test_catalog_event_with_only_later_official_batch_is_also_catalog_lag(data):
    """官方接口每个产品只返回最近一次发布的批次：先在早批次发布、后在更晚批次重新发布的型号，
    只能查到更晚那批。目录随第410批合刊收录它，同样说明不了第410批有新版本。"""
    data.announcement(product('P411', MODEL, batch=411))
    catalog_event(data)
    source, calls = official([411])
    _result, event, _model_status = run(data, source)
    assert [c.get('pc') for c in calls] == ['410', None]
    assert event['status'] == tracking.CATALOG_LAG
    assert '第411批' in event['last_error']


def test_catalog_check_runs_only_when_business_db_holds_another_batch(data):
    catalog_event(data)
    source, calls = official([408])
    _result, event, _model_status = run(data, source)
    assert [c.get('pc') for c in calls] == ['410']
    assert event['status'] == 'no_match'


def test_product_event_never_becomes_catalog_lag(data):
    data.announcement(product('P408', MODEL, batch=408))
    catalog_event(data, pid='AC000410')
    source, calls = official([408])
    _result, event, _model_status = run(data, source)
    assert [c.get('pc') for c in calls] == ['410']
    assert event['status'] == 'no_match'


def test_failed_catalog_check_keeps_no_match_for_a_later_retry(data):
    data.announcement(product('P408', MODEL, batch=408))
    catalog_event(data)

    def query(**kwargs):
        if 'pc' in kwargs:
            return []
        raise TimeoutError('timeout')

    _result, event, _model_status = run(data, ScriptedSource(on_query=query))
    assert event['status'] == 'no_match'
    assert '目录滞后核对' in event['last_error']
    with sqlite3.connect(data.db) as conn:
        plan = tracking.retry_plan(conn, data.pdf_root, today=date.today() + timedelta(days=8))
    assert [e['event_id'] for e in plan['pending']] == [event['event_id']]


def test_inventory_counts_catalog_lag_as_its_own_terminal_bucket(data):
    data.announcement(product('P408', MODEL, batch=408))
    data.event(MODEL, batch=409)
    with sqlite3.connect(data.db) as conn:
        conn.execute("update tracking_events set status=?,last_error='官方正式公告在第408批'",
                     (tracking.CATALOG_LAG,))
    summary, _candidates = data.compute()
    assert {row['bucket']: row['n'] for row in summary['tracking']['events']} == {'catalog_lag': 1}
    assert summary['tracking']['accepted_not_done_window_until'] == []
    assert TODAY  # 与 InventoryFixture 共用同一「今天」


def product_event(data, model, batch, pid):
    with sqlite3.connect(data.db) as conn:
        return tracking.register_event(
            conn, model_code=model, batch=batch, kind='formal', product_id=pid,
            source_url='https://www.miit.gov.cn/fixture', event_date=date.today().isoformat(),
            metadata={'product_name': '纯电动载货汽车'})


def test_product_event_superseded_by_newer_publication_is_planned_apart(data):
    """同一产品 ID 已按更高正式批次入库：本批次那次发布已被重新发布取代，不再查询。
    反过来，库里只有更早批次的同一产品时，本批次是待取的新版本，照常查询。"""
    data.announcement(product('P410', 'TEST1000OLD', batch=410))
    superseded = product_event(data, 'TEST1000OLD', 409, 'P410')
    data.announcement(product('P409', 'TEST1000NEW', batch=409))
    republished = product_event(data, 'TEST1000NEW', 410, 'P409')
    with sqlite3.connect(data.db) as conn:
        plan = tracking.retry_plan(conn, data.pdf_root)
        by_id = {e['event_id']: e for e in tracking.events(conn)}
        products = tracking.product_batches(conn)
    assert tracking.superseded_batch(by_id[superseded], products) == 410
    assert tracking.superseded_batch(by_id[republished], products) is None
    assert plan['superseded'] == [superseded]
    assert [e['event_id'] for e in plan['pending']] == [republished]


def test_inventory_counts_superseded_product_event_apart(data):
    data.announcement(product('P410', 'TEST1000OLD', batch=410))
    data.event('TEST1000OLD', batch=409, pid='P410')
    summary, _candidates = data.compute()
    assert {row['bucket']: row['n'] for row in summary['tracking']['events']} == {'superseded': 1}
    assert summary['tracking']['accepted_not_done_window_until'] == []


@pytest.mark.parametrize('newer_first', [True, False])
def test_same_run_never_rolls_product_back_to_an_earlier_publication(tmp_path, monkeypatch, newer_first):
    """同一产品第409、410批的清单事件同轮待取：按批次查询两次发布都查得到，
    无论计划先处理哪一批，业务库都停在第410批，早批次那次发布不回写、也不再下载。"""
    from miit_gonggao import images

    data = InventoryFixture(tmp_path)
    pid = 'P1'

    def order_ok(model):
        first = tracking.event_identity(model, 410, 'formal', pid) < tracking.event_identity(model, 409, 'formal', pid)
        return first == newer_first

    model = next(m for m in (f'TEST{i}BEV' for i in range(1000, 1100)) if order_ok(m))
    monkeypatch.setattr(seed, 'UPSTREAM_ROOT', data.pdf_root)
    monkeypatch.setattr(seed, 'refresh_collection_status', lambda *_a, **_k: None)
    monkeypatch.setattr(seed, 'parse_pdf', lambda *_a: ({'model_code': model, 'fuel_type': '纯电动'}, ''))
    monkeypatch.setattr(images, 'publish_images', lambda *_a, **_k: None)
    monkeypatch.setattr(tracking, 'document_image_error', lambda *_a: '')

    def query(**kwargs):
        return [{'cpid': pid, 'clxh': model, 'gppc': kwargs['pc'], 'clmc': '纯电动客车', 'cpsb': '测试牌'}]

    def download(row, staging):
        path = staging / f'test_{pid}.pdf'
        path.write_bytes(f"%PDF-1.4\n{row['gppc']}\n".encode())
        return path, True, path.stat().st_size

    source = ScriptedSource(on_query=query, on_download=download)
    with sqlite3.connect(data.db) as conn:
        ids = {batch: tracking.register_event(
            conn, model_code=model, batch=batch, kind='formal', product_id=pid,
            source_url='https://www.miit.gov.cn/fixture', event_date=TODAY.isoformat(),
            metadata={'product_name': '纯电动客车', 'category': '客车'}) for batch in (409, 410)}
        conn.commit()
        plan = tracking.retry_plan(conn, data.pdf_root, today=TODAY)
        assert [e['batch'] for e in plan['pending']] == ([410, 409] if newer_first else [409, 410])
        result = tracking.collect(conn, plan, catalog_db=data.catalog, pdf_root=data.pdf_root, source=source)
        current, relative = conn.execute(
            'select b.batch,d.relative_path from announcements a join batches b on a.batch_id=b.id '
            'join documents d on d.announcement_id=a.id where a.source_product_id=?', (pid,)).fetchone()
        by_id = {e['event_id']: e for e in tracking.events(conn)}
        again = tracking.retry_plan(conn, data.pdf_root, today=TODAY)
    assert (current, result['errors']) == ('410', 0)
    assert '第410批' in relative
    if newer_first:
        assert [row['gppc'] for row in source.downloads] == ['410']
        assert '第409批发布已被取代' in by_id[ids[409]]['last_error']
    assert {e['event_id'] for e in again['pending']} == set()
    assert again['superseded'] == [ids[409]]
