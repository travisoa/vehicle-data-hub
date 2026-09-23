"""近期正式公告的逐事件跟踪。采集记录和 PDF 统一归上游管理。

公示只用于提前了解，不登记、不驱动采集：事件一律来自正式发布。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import closing, nullcontext
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

if __package__:
    from .vehicle_classification import classify_vehicle, is_non_automotive
else:
    from miit_gonggao.vehicle_classification import classify_vehicle, is_non_automotive

DAYS = 60
BATCHES = 2
KINDS = {'formal'}  # 公示不作为入口，事件只接受正式发布
# 工信部公告里汽车、摩托车、挂车、三轮汽车、低速汽车是彼此独立的产品序列，各有各的
# 型号编制规则。本站只做汽车四频道（范围见 Website/docs/collection-boundary.md），其余序列
# 不登记、不采集、不入库。判定必须看产品名称：摩托车型号同样是「字母+数字」结构，
# 首位数字也落在 1-7，单靠 GB 9417 类别码会把「电动正三轮摩托车 AMT1200DZK-35」判成货车。
SCHEMA = """
CREATE TABLE IF NOT EXISTS tracking_events (
 event_id TEXT PRIMARY KEY, model_code TEXT NOT NULL, batch INTEGER NOT NULL,
 kind TEXT NOT NULL, product_id TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL,
 title TEXT NOT NULL DEFAULT '', event_date TEXT NOT NULL DEFAULT '',
 first_seen_at TEXT NOT NULL, metadata_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', last_checked_at TEXT, next_check_at TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tracking_model ON tracking_events(model_code,batch);
CREATE TABLE IF NOT EXISTS tracking_attempts (
 id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, run_id INTEGER NOT NULL,
 checked_at TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def scope_evidence(conn: sqlite3.Connection) -> dict[tuple[str, int], dict[str, dict[str, str]]]:
    """同型号同批次的官方产品身份优先于目录概括名称，防止底盘从目录占位入口重新进入。"""
    result: dict[tuple[str, int], dict[str, dict[str, str]]] = {}
    for model, batch, pid, name, raw_json in conn.execute(
        'select a.model_code,b.batch,a.source_product_id,a.product_name,a.raw_json '
        'from announcements a join batches b on b.id=a.batch_id'
    ):
        if not str(batch).isdigit():
            continue
        result.setdefault((model.strip().upper(), int(batch)), {})[str(pid)] = classify_vehicle(
            model, name or '', json.loads(raw_json or '{}'))
    for item in events(conn):
        if item['kind'] != 'formal' or not item['product_id']:
            continue
        result.setdefault((item['model_code'], item['batch']), {}).setdefault(item['product_id'], event_scope(item))
    return result


def event_scope(event: dict, evidence: dict | None = None) -> dict[str, str]:
    """存量事件每次按当前整车范围重判，不信任历史分类标签。"""
    known = (evidence or {}).get((event['model_code'], event['batch']), {})
    if event['product_id'] in known:
        return known[event['product_id']]
    if not event['product_id'] and known:
        gates = {d['inclusion_gate'] for d in known.values()}
        if gates == {'accepted'}:
            return {'inclusion_gate': 'accepted', 'scope_reason': 'official_product_identity'}
        if gates == {'excluded'}:
            return next(iter(known.values()))
        return {'inclusion_gate': 'pending_review', 'scope_reason': 'conflicting_scope_evidence'}
    meta = json.loads(event['metadata_json'])
    return classify_vehicle(event['model_code'], meta.get('clmc') or meta.get('product_name') or '', meta)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def table_exists(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute("select 1 from sqlite_master where name='tracking_events'").fetchone())


def events(conn: sqlite3.Connection) -> list[dict]:
    if not table_exists(conn):
        return []
    cursor = conn.execute('select * from tracking_events order by batch,model_code,event_id')
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor]


def parse_date(value: str) -> date | None:
    value = value.strip()
    match = re.match(r'^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', value)
    if match:
        try:
            return date(*(int(x) for x in match.groups()))
        except ValueError:
            return None
    for fmt in ('%Y-%m-%d', '%Y%m%d', '%Y年%m月%d日', '%Y/%m/%d'):
        try:
            return datetime.strptime(value[:10] if fmt != '%Y%m%d' else value[:8], fmt).date()
        except ValueError:
            pass
    return None


def in_window(event: dict, latest_batch: int, *, today: date | None = None) -> bool:
    today = today or date.today()
    published = parse_date(event['event_date'])
    # 缺少发布日期时按最初发现时间封顶，重新登记不延长寿命。
    if published is None:
        published = parse_date(event['first_seen_at'])
    if published is not None and not 0 <= (today - published).days <= DAYS:
        return False
    return latest_batch - BATCHES + 1 <= event['batch'] <= latest_batch


def register_event(conn: sqlite3.Connection, *, model_code: str, batch: int, kind: str,
                   source_url: str, product_id: str = '', title: str = '', event_date: str = '',
                   metadata: dict | None = None) -> str:
    if kind not in KINDS or batch < 173 or not model_code.strip():
        raise ValueError('公告事件必须包含有效类型、批次和精确型号')
    host = urlparse(source_url).hostname or ''
    if not any(host == suffix or host.endswith('.' + suffix)
               for suffix in ('miit.gov.cn', 'miit-eidc.org.cn')):
        raise ValueError('公告事件必须使用官方来源 URL')
    model_code = model_code.strip().upper()
    # 来源 URL 不参与身份，文章路径变化也不会反复重置同批同型号的完成记录。
    key = json.dumps([model_code, batch, kind, product_id], ensure_ascii=False)
    event_id = hashlib.sha256(key.encode()).hexdigest()[:24]
    conn.execute(
        'insert into tracking_events (event_id,model_code,batch,kind,product_id,source_url,title,'
        'event_date,first_seen_at,metadata_json) values (?,?,?,?,?,?,?,?,?,?) '
        'on conflict(event_id) do update set source_url=excluded.source_url,title=excluded.title,'
        "event_date=case when excluded.event_date<>'' then excluded.event_date else event_date end,"
        'metadata_json=excluded.metadata_json',
        (event_id, model_code, batch, kind, product_id, source_url, title, event_date,
         utc_now(), json.dumps(metadata or {}, ensure_ascii=False)),
    )
    return event_id


def register_batch(conn: sqlite3.Connection, payload: dict) -> int:
    # 不把不完整或未校验枚举作为“全集”；旧缓存由上游 loader 自行刷新。
    if not payload.get('complete') or payload.get('absent_upstream') or payload.get('failures'):
        raise ValueError('批次缓存不完整，先重新枚举')
    if not payload.get('verify_prefixes'):
        raise ValueError('批次缓存缺少交叉校验')
    batch = int(payload['batch'])
    count = 0
    for row in payload['products']:
        actual = str(row.get('gppc') or row.get('pc') or '')
        if actual != str(batch) or not row.get('cpid') or not row.get('clxh'):
            raise ValueError('批次缓存存在批次不符或缺少产品身份的条目')
        row = {**row, **classify_vehicle(row['clxh'], row.get('clmc') or '', row)}
        if row['inclusion_gate'] == 'excluded':
            continue  # 原始清单完整保留，非汽车及底盘/上装不登记为整车。
        register_event(conn, model_code=row['clxh'], batch=batch, kind='formal',
                       source_url=payload['source'], product_id=str(row['cpid']), metadata=row,
                       event_date=str(row.get('publish_date') or payload.get('published_at') or ''))
        count += 1
    return count


def register_catalog(conn: sqlite3.Connection, catalog_db: Path, latest_batch: int,
                     *, today: date | None = None) -> int:
    """只登记近期正式公告合刊中的目录型号，不以目录批次冒充公告批次。"""
    today = today or date.today()
    count = 0
    with closing(sqlite3.connect(f'file:{catalog_db}?mode=ro', uri=True)) as cat:
        cursor = cat.execute(
            'select a.title,a.pub_date,a.url,c.model_code,c.company,c.trademark,c.product_name,'
            'c.common_name,c.category from catalog_rows c join articles a on a.art_id=c.art_id '
            "where a.status='ok' and c.catalog like '%车辆购置税%'")
        for title, published, url, model, company, brand, product, common, category in cursor:
            match = re.search(r'道路机动车辆生产企业及产品.{0,20}?第\s*(\d+)\s*批', title or '')
            day = parse_date(published or '')
            if not match or day is None or not 0 <= (today - day).days <= DAYS:
                continue
            if is_non_automotive(product) or is_non_automotive(common):
                continue
            batch = int(match[1])
            if not latest_batch - BATCHES + 1 <= batch <= latest_batch:
                continue
            metadata = {
                'company': company, 'trademark': brand, 'product_name': product,
                'common_name': common, 'category': category,
            }
            metadata.update(classify_vehicle(model, product or '', metadata))
            if metadata['inclusion_gate'] == 'excluded':
                continue
            register_event(conn, model_code=model, batch=batch, kind='formal', source_url=url,
                           title=title, event_date=day.isoformat(), metadata=metadata)
            count += 1
    return count


def matching_documents(conn: sqlite3.Connection, event: dict) -> list[tuple]:
    condition = 'and a.source_product_id=?' if event['product_id'] else ''
    args = [event['model_code'], str(event['batch'])]
    if event['product_id']:
        args.append(event['product_id'])
    return conn.execute(
        'select a.source_product_id,d.relative_path,d.bytes,d.is_pdf from announcements a '
        'join batches b on b.id=a.batch_id left join documents d on d.announcement_id=a.id '
        f'where upper(a.model_code)=? and b.batch=? {condition}', args,
    ).fetchall()


def has_valid_documents(conn: sqlite3.Connection, event: dict, pdf_root: Path) -> bool:
    rows = matching_documents(conn, event)
    if not rows:
        return False
    for _, relative, size, is_pdf in rows:
        if is_pdf != 1 or not relative or not size:
            return False
        path = (pdf_root / relative).resolve()
        if not path.is_relative_to(pdf_root.resolve()):
            return False
        try:
            if path.stat().st_size != size:
                return False
            with path.open('rb') as f:
                if f.read(4) != b'%PDF':
                    return False
        except OSError:
            return False
    return True


def document_image_error(conn: sqlite3.Connection, event: dict, pdf_root: Path) -> str:
    """已有有效 PDF 的产品：上次图片获取失败时只重取图片（不重下 PDF），仍失败才返回异常说明。"""
    from miit_gonggao import core
    from miit_gonggao.images import read_image_result, retry_failed_images

    if not core.DOWNLOAD_IMAGES:
        return ''
    for product_id, relative, _size, _is_pdf in matching_documents(conn, event):
        if not relative:
            continue
        folder = (pdf_root / relative).resolve().parent
        if not folder.is_relative_to(pdf_root.resolve()):
            continue
        raw = json.loads(conn.execute("SELECT raw_json FROM announcements WHERE source_product_id=?",
                                      (product_id,)).fetchone()[0])
        result = retry_failed_images(raw, folder) or read_image_result(raw, folder)
        if result and result.get('failed'):
            return '图片下载不完整：已有 PDF 有效，重取图片仍未完整'
    return ''


def latest_formal_batch(conn: sqlite3.Connection) -> int:
    actual = conn.execute('select coalesce(max(cast(batch as integer)),0) from batches').fetchone()[0]
    tracked = max((e['batch'] for e in events(conn) if e['kind'] == 'formal'), default=0)
    return max(actual, tracked)


class RetryPlan(dict):
    """保留 JSON 计划格式；只在同一连接且数据未变时复用本轮范围证据。"""

    def __init__(self, values: dict, conn: sqlite3.Connection, evidence: dict, data_version: int):
        super().__init__(values)
        self._connection = conn
        self._evidence = evidence
        self._total_changes = conn.total_changes
        self._data_version = data_version

    def scope_evidence(self, conn: sqlite3.Connection) -> dict:
        if (conn is self._connection and conn.total_changes == self._total_changes
                and conn.execute('pragma data_version').fetchone()[0] == self._data_version):
            return self._evidence
        return scope_evidence(conn)


def retry_plan(conn: sqlite3.Connection, pdf_root: Path, *, today: date | None = None,
               limit: int = 400) -> dict:
    today = today or date.today()
    data_version = conn.execute('pragma data_version').fetchone()[0]
    maximum = latest_formal_batch(conn)
    pending, dormant, resolved, deferred, remaining = [], [], [], [], []
    excluded, scope_review, legacy_notice = [], [], []
    groups: set[tuple] = set()
    evidence = scope_evidence(conn)
    for event in sorted(events(conn), key=lambda e: (e['last_checked_at'] or '', e['event_id'])):
        if event['kind'] != 'formal':
            # 公示不再登记，但旧库里可能留着 new_notice/change_notice 行。它们不是正式发布，
            # 不能驱动查询和下载；历史保留在源库，只是不进计划。
            legacy_notice.append(event['event_id'])
            continue
        scope = event_scope(event, evidence)
        if scope['inclusion_gate'] != 'accepted':
            (excluded if scope['inclusion_gate'] == 'excluded' else scope_review).append(event['event_id'])
            continue
        if not in_window(event, maximum, today=today):
            if event['status'] != 'done':
                dormant.append(event['event_id'])
            continue
        if has_valid_documents(conn, event, pdf_root):
            resolved.append(event['event_id'])
            continue
        if event['next_check_at'] and event['next_check_at'][:10] > today.isoformat():
            deferred.append(event['event_id'])
            continue
        key = (event['model_code'], event['batch'])
        if key not in groups and len(groups) >= limit:
            remaining.append(event['event_id'])
            continue
        groups.add(key)
        pending.append(event)
    return RetryPlan({'latest_batch': maximum, 'days': DAYS, 'batches': BATCHES,
                      'pending': pending, 'dormant': dormant, 'resolved': resolved,
                      'deferred': deferred, 'remaining': remaining,
                      'excluded': excluded, 'scope_review': scope_review,
                      'legacy_notice': legacy_notice,
                      'query_groups': len(groups)}, conn, evidence, data_version)


def public_fingerprint(conn: sqlite3.Connection) -> str:
    """跟踪状态也是可发布数据；不以新增 PDF 数作为唯一发布条件。"""
    digest = hashlib.sha256()
    queries = [
        'select source_product_id,raw_json from announcements order by source_product_id',
        'select announcement_id,relative_path,is_pdf,bytes,sha256 from documents order by announcement_id',
        'select announcement_id,fields_json from announcement_fields order by announcement_id',
        'select announcement_model_code,market_name,catalog_category from vehicles order by id',
    ]
    if table_exists(conn):
        queries.append('select event_id,batch,kind,status,metadata_json,title,event_date '
                       'from tracking_events order by event_id')
    try:
        from .collection_coverage import best_model_outcomes
    except ImportError:
        from miit_gonggao.collection_coverage import best_model_outcomes
    digest.update(json.dumps(sorted(best_model_outcomes(conn).items())).encode())
    for sql in queries:
        for row in conn.execute(sql):
            digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
            digest.update(b'\n')
    return digest.hexdigest()


def collect(conn: sqlite3.Connection, plan: dict, *, catalog_db: Path, pdf_root: Path) -> dict:
    # 手动下载也不能绕过整车边界；在产生运行记录或网络请求之前拒绝失效/外部拼装的计划。
    evidence = plan.scope_evidence(conn) if isinstance(plan, RetryPlan) else scope_evidence(conn)
    if any(event_scope(e, evidence)['inclusion_gate'] != 'accepted' for e in plan['pending']):
        raise ValueError('采集计划包含范围外或尚未确认是汽车整车的产品')
    try:
        from . import collection as seed
    except ImportError:
        from miit_gonggao import collection as seed
    seed.ensure_schema(conn)
    db_path = next((Path(row[2]) for row in conn.execute('pragma database_list')
                    if row[1] == 'main' and row[2]), None)
    if conn.execute('select 1 from ingestion_runs where completed_at is null').fetchone():
        raise RuntimeError('存在未完成采集，不能另起一轮')
    resolved_image_failures = 0
    resolved_events = {event['event_id']: event for event in events(conn)} if plan['resolved'] else {}
    for event_id in plan['resolved']:
        image_error = document_image_error(conn, resolved_events[event_id], pdf_root)
        resolved_image_failures += bool(image_error)
        conn.execute("update tracking_events set status=?,last_error=? where event_id=?",
                     ('partial' if image_error else 'done', image_error, event_id))
    grouped: dict[tuple, list[dict]] = {}
    for event in plan['pending']:
        grouped.setdefault((event['model_code'], event['batch']), []).append(event)
    if not grouped:
        conn.commit()
        if plan['resolved']:
            seed.refresh_collection_status(db_path, catalog_db=catalog_db, pdf_root=pdf_root,
                                           reason="tracking_resolved")
        result = {'queries': 0, 'errors': resolved_image_failures}
        if resolved_image_failures:
            result['image_failures'] = resolved_image_failures
        return result
    run_id = conn.execute(
        'insert into ingestion_runs(started_at,selector_json,selected_models,parse_failures,publish_failures) '
        'values (?,?,?,0,0)', (utc_now(), json.dumps({'mode': 'recent_events', 'days': DAYS,
                                                   'batches': BATCHES}), len({key[0] for key in grouped})),
    ).lastrowid
    counts = dict(query_failures=0, download_failures=0, non_pdf_documents=0,
                  parse_failures=0, publish_failures=0, awaiting_effective=0, image_failures=0)
    scope_counts = dict(scope_excluded=0, scope_review=0)
    checked = 0
    try:
        for index, ((model, batch), group) in enumerate(grouped.items(), 1):
            meta = json.loads(group[0]['metadata_json'])
            existing = conn.execute('select id from vehicles where upper(announcement_model_code)=?',
                                    (model,)).fetchone()
            if existing:
                vehicle_id = existing[0]
            else:
                vehicle_id = seed.upsert_vehicle(conn, {
                    'model_code': model, 'common_name': model, 'catalog': '正式公告跟踪',
                    'batch': str(batch), 'category': next((json.loads(e['metadata_json']).get('category')
                        for e in group if json.loads(e['metadata_json']).get('category')), ''),
                    'seq': '', 'company': meta.get('company') or meta.get('qymc') or '',
                })
            seed.begin_model(conn, run_id, index, vehicle_id)
            status, error = 'done', ''
            try:
                # 事件只来自正式发布，登记本身就把该批次纳入最高正式批次，不存在"尚未生效"的等待。
                rows = seed.core.query_all_pages(model_code=model, pc=str(batch), page_size=50)
                rows = [r for r in rows if str(r.get('clxh') or '').strip().upper() == model
                        and str(r.get('gppc') or r.get('pc') or '') == str(batch)]
            except Exception as exc:
                rows = []
                status, error = 'query_failed', str(exc)
                counts['query_failures'] += 1
            needed = {e['product_id'] for e in group if e['product_id']}
            if rows and all(e['product_id'] for e in group):
                rows = [r for r in rows if str(r.get('cpid')) in needed]
            # 产品 ID -> (PDF 是否可用, 说明, 图片是否异常)
            results: dict[str, tuple[bool, str, bool]] = {}
            for row in rows:
                scope = classify_vehicle(model, row.get('clmc') or '', row)
                if scope['inclusion_gate'] != 'accepted':
                    key = 'scope_excluded' if scope['inclusion_gate'] == 'excluded' else 'scope_review'
                    scope_counts[key] += 1
                    results[str(row.get('cpid') or '')] = (
                        False, f"官方返回产品范围校验未通过: {model} / {row.get('cpid') or '缺少产品 ID'} / "
                        f"{row.get('clmc') or '缺少产品名称 clmc'} "
                        f"({scope['inclusion_gate']}: {scope['scope_reason']})", False,
                    )
                    continue
                probe = {**group[0], 'product_id': str(row['cpid'])}
                if has_valid_documents(conn, probe, pdf_root):
                    image_error = document_image_error(conn, probe, pdf_root)
                    counts['image_failures'] += bool(image_error)
                    results[str(row['cpid'])] = (True, image_error, bool(image_error))
                    continue
                outcome = seed.store_announcement(
                    conn, row=row, vehicle_id=vehicle_id, market_name=model,
                    download_root=pdf_root / 'downloads/announcement_site',
                    pdf_root=pdf_root, catalog_db=catalog_db,
                )
                ok, message = outcome
                image_failed = getattr(outcome, 'image_failed', False)
                results[str(row['cpid'])] = (ok or message.startswith('解析失败'), message, image_failed)
                counts['image_failures'] += image_failed
                if not ok:
                    key = ('parse_failures' if message.startswith('解析失败') else
                           'download_failures' if message.startswith('下载失败') else
                           'publish_failures' if message.startswith('发布失败') else 'non_pdf_documents')
                    counts[key] += 1
            for event in group:
                selected = ([results[event['product_id']]] if event['product_id'] in results else [])
                if not event['product_id']:
                    selected = list(results.values())
                event_status, event_error = status, error
                if status != 'query_failed':
                    if not selected:
                        event_status = 'no_match'
                    elif all(ok and not image_failed for ok, _, image_failed in selected):
                        event_status = 'done'
                    elif any(not ok for ok, _, _ in selected) and all(
                            msg.startswith('接口返回非 PDF') for ok, msg, _ in selected if not ok):
                        event_status = 'no_document'
                    else:
                        event_status = 'partial'
                    event_error = '; '.join(msg for _, msg, _ in selected if msg)
                conn.execute(
                    'update tracking_events set status=?,last_checked_at=?,next_check_at=?, '
                    'attempts=attempts+1,last_error=? where event_id=?',
                    (event_status, utc_now(), (date.today() + timedelta(days=7)).isoformat(),
                     event_error, event['event_id']),
                )
                conn.execute('insert into tracking_attempts(event_id,run_id,checked_at,status,error) '
                             'values (?,?,?,?,?)', (event['event_id'], run_id, utc_now(), event_status, event_error))
            event_outcomes = conn.execute(
                'select t.status,t.error from tracking_attempts t join tracking_events e using(event_id) '
                'where t.run_id=? and e.model_code=?', [run_id, model]).fetchall()
            event_states = [row[0] for row in event_outcomes]
            model_error = '; '.join(dict.fromkeys(row[1] for row in event_outcomes if row[1]))
            model_status = ('query_failed' if 'query_failed' in event_states else 'done'
                            if all(s == 'done' for s in event_states) else 'awaiting_effective'
                            if 'awaiting_effective' in event_states else 'no_match'
                            if all(s == 'no_match' for s in event_states) else 'partial')
            if model_status == 'awaiting_effective':
                counts['awaiting_effective'] += 1
            seed.finish_model(conn, run_id, vehicle_id, model_status, model_error)
            checked += 1
    finally:
        try:
            seed.finish_run(conn, run_id, counts)
        finally:
            seed.refresh_collection_status(db_path, catalog_db=catalog_db, pdf_root=pdf_root,
                                           run_id=run_id, reason="tracking_collection")
    errors = (sum(counts[k] for k in ('query_failures', 'download_failures', 'publish_failures', 'image_failures'))
              + sum(scope_counts.values()) + resolved_image_failures)
    return {'run_id': run_id, 'queries': checked, 'errors': errors, **counts, **scope_counts}


def main() -> int:
    try:
        from . import collection as seed
    except ImportError:
        from miit_gonggao import collection as seed
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site-db', type=Path, default=seed.DEFAULT_SITE_DB)
    parser.add_argument('--catalog-db', type=Path, default=seed.DEFAULT_CATALOG_DB)
    parser.add_argument('--pdf-root', type=Path, default=seed.UPSTREAM_ROOT)
    commands = parser.add_subparsers(dest='command', required=True)
    batch = commands.add_parser('register-batch')
    batch.add_argument('cache', type=Path)
    catalog = commands.add_parser('register-catalog')
    catalog.add_argument('--latest-batch', type=int, required=True)
    commands.add_parser('fingerprint')
    plan_parser = commands.add_parser('plan')
    plan_parser.add_argument('--limit', type=int, default=400)
    collect_parser = commands.add_parser('collect')
    collect_parser.add_argument('--limit', type=int, default=400)
    collect_parser.add_argument('--no-images', action='store_true', help='只下载 PDF，不获取或重试详情页原图')
    args = parser.parse_args()
    if hasattr(args, 'limit') and args.limit < 1:
        parser.error('--limit 必须大于 0')
    if args.pdf_root.resolve() != seed.UPSTREAM_ROOT:
        parser.error('--pdf-root 必须与 VEHICLE_DATA_HUB_ROOT 一致')
    read_only = args.command in {'plan', 'fingerprint'}
    uri = f'file:{args.site_db}?mode={"ro" if read_only else "rw"}'
    with (nullcontext() if read_only else seed.database_lock(args.site_db),
          closing(sqlite3.connect(uri, uri=True)) as conn):
        if not read_only:
            seed.ensure_schema(conn)
            ensure_schema(conn)
            if conn.execute('select 1 from ingestion_runs where completed_at is null').fetchone():
                raise RuntimeError('存在未完成采集，停止登记/下载')
        if args.command == 'register-batch':
            with conn:
                count = register_batch(conn, json.loads(args.cache.read_text()))
            print(json.dumps({'registered': count}))
        elif args.command == 'register-catalog':
            with conn:
                count = register_catalog(conn, args.catalog_db, args.latest_batch)
            print(json.dumps({'registered': count}))
        elif args.command == 'fingerprint':
            print(public_fingerprint(conn))
        elif args.command == 'plan':
            print(json.dumps(retry_plan(conn, args.pdf_root, limit=args.limit), ensure_ascii=False, indent=2))
        else:
            with seed.core.image_downloads(not args.no_images):
                result = collect(conn, retry_plan(conn, args.pdf_root, limit=args.limit),
                                 catalog_db=args.catalog_db, pdf_root=args.pdf_root)
            if result.get('run_id'):
                try:
                    try:
                        from .collection_report import write_report
                    except ImportError:
                        from miit_gonggao.collection_report import write_report
                    report_dir = (seed.PROJECT_ROOT / 'var/reports'
                                  if args.site_db.resolve() == seed.DEFAULT_SITE_DB.resolve()
                                  else args.site_db.parent / 'reports')
                    result['report'] = str(write_report(args.site_db, result['run_id'],
                                                       report_dir, args.catalog_db))
                except Exception as exc:
                    result['report_error'] = str(exc)
            print(json.dumps(result, ensure_ascii=False))
            return 2 if result['errors'] else 0
        if args.command.startswith('register-'):
            seed.refresh_collection_status(args.site_db, catalog_db=args.catalog_db,
                                           pdf_root=args.pdf_root, reason=args.command)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
