"""Offline collection inventory from authoritative upstream databases and batch evidence.

The caller owns stable cross-database snapshots and persistence. This module never
writes, downloads, or hashes PDF contents. Complete stale caches remain historical
evidence; partial/absent evidence never becomes a successful batch universe.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts import announcement_catalog_gap as G
from miit_gonggao import collection_tracking as T
from miit_gonggao.collection_coverage import best_model_outcomes, coverage_buckets
from miit_gonggao.vehicle_categories import _ENERGY_KEYWORDS, derive_channel, normalize_energy
from miit_gonggao.vehicle_classification import COLLECTION_SCOPE, classify_vehicle

TZ = ZoneInfo('Asia/Shanghai')
NEV = {'纯电动', '插电式混合动力', '增程式', '燃料电池'}
FUEL = {'燃油', '混合动力'}
FUEL_BATCHES = {408, 409}
IN_SCOPE_GROUPS = {
    'nev': '新能源（历史及近期）',
    'fuel_408_409': '第408/409批燃油或普通混动',
    'unknown_408_409': '第408/409批能源未确认候选',
}
BANDS = ((173, 299), (300, 349), (350, 399), (400, 407), (408, 409))
VALID_PDF_STATES = {'parsed', 'pdf_unparsed'}
EV_MODEL_CODE = re.compile(r'(BEV|PHEV|FCEV|REEV|EREV)')
REQUIRED_TABLES = {'ingestion_runs', 'vehicles', 'batches', 'announcements', 'documents',
                   'announcement_fields', 'run_models', 'tracking_events', 'tracking_attempts'}


def band(batch: int | None) -> str:
    for low, high in BANDS:
        if batch is not None and low <= batch <= high:
            return f'{low}-{high}'
    return 'other'


def rows(counter: Counter, names: tuple[str, ...]) -> list[dict]:
    return [dict(zip(names, key, strict=True), n=value)
            for key, value in sorted(counter.items(), key=lambda kv: tuple(str(x) for x in kv[0]))]


@contextmanager
def readonly_snapshot(path: Path):
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        yield connection
    finally:
        connection.close()


def json_object(value: str | None, context: str) -> dict:
    try:
        parsed = json.loads(value or '{}')
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{context}: invalid JSON') from exc
    if not isinstance(parsed, dict):
        raise ValueError(f'{context}: expected a JSON object')
    return parsed


def load_catalog_energy(catalog_db: Path) -> dict[str, tuple[str, str]]:
    """Specific official energy descriptions take precedence over generic NEV labels."""
    result: dict[str, tuple[str, str]] = {}
    with readonly_snapshot(catalog_db) as con:
        for code, energy, common in con.execute(
                'SELECT model_code,energy_type,common_name FROM catalog_rows ORDER BY rowid'):
            key = (code or '').strip().upper()
            if not key:
                continue
            prior_energy, prior_common = result.get(key, ('', ''))
            energy, common = (energy or '').strip(), (common or '').strip()
            def specific(text):
                return any(word in text and value != '混合动力' for word, value in _ENERGY_KEYWORDS)
            if not prior_energy or (specific(energy) and not specific(prior_energy)):
                prior_energy = energy
            result[key] = prior_energy, prior_common or common
    return result


def load_caches(cache_dir: Path, observed_at: datetime):
    products: dict[str, dict] = {}
    sources, skipped = [], []
    rows_without_pid = 0
    if not cache_dir.is_dir():
        raise NotADirectoryError(cache_dir)
    for path in sorted(cache_dir.glob('batch*.json')):
        match = re.fullmatch(r'batch(\d+)\.json', path.name)
        if not match:
            skipped.append({'file': path.name, 'reason': '缺失/部分失败证据，不作为全集'})
            continue
        data = json_object(path.read_text(encoding='utf-8'), str(path))
        items = data.get('products')
        if not isinstance(items, list):
            # Legacy absent records can lack products; they remain excluded evidence.
            if data.get('absent_upstream') or not data.get('complete'):
                items = []
            else:
                raise ValueError(f'{path}: products must be a list')
        # Reuse the upstream completeness/version/prefix contract independently of
        # freshness, which is reported against the explicit inventory date below.
        if not G.cache_is_complete(data, fresh=False) or data.get('failures'):
            skipped.append({'file': path.name, 'reason': 'cache_is_complete=False',
                            'fetched_at': data.get('fetched_at'), 'count': len(items)})
            continue
        try:
            fetched = datetime.fromisoformat(data['fetched_at'])
            if fetched.utcoffset() is None:
                raise ValueError('timezone missing')
        except (KeyError, TypeError, ValueError):
            skipped.append({'file': path.name, 'reason': 'invalid fetched_at', 'count': len(items)})
            continue
        if fetched > observed_at:
            skipped.append({'file': path.name, 'reason': 'future fetched_at', 'count': len(items)})
            continue
        batch = int(data['batch'])
        if batch != int(match[1]):
            raise ValueError(f'{path}: batch disagrees with filename')
        identities: set[str] = set()
        malformed = False
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f'{path}: product row must be an object')
            pid = str(item.get('cpid') or '').strip()
            actual_batch = str(item.get('gppc') or item.get('pc') or '')
            if not pid:
                rows_without_pid += 1
            if not pid or actual_batch != str(batch) or pid in identities:
                malformed = True
            identities.add(pid)
        if malformed:
            skipped.append({'file': path.name, 'reason': 'invalid product identity or batch', 'count': len(items)})
            continue
        valid_until = fetched + timedelta(days=G.CACHE_MAX_AGE_DAYS)
        sources.append({'batch': batch, 'fetched_at': data['fetched_at'], 'count': len(items),
                        'verify_missed': data.get('verify_missed'), 'stale': observed_at > valid_until,
                        'valid_until': valid_until.astimezone(TZ).isoformat()})
        for item in items:
            pid = str(item['cpid']).strip()
            model = str(item.get('clxh') or '').strip().upper()
            entry = products.setdefault(pid, {'batch': batch, 'rec': item, 'batches': set(),
                                             'model_batches': set(), 'records_by_batch': {}})
            entry['batches'].add(batch)
            entry['model_batches'].add((model, batch))
            # Only authorized fuel occurrences need an extra full product row;
            # preserve historical identity pairs without retaining every JSON copy.
            if batch in FUEL_BATCHES:
                entry['records_by_batch'][batch] = item
            if batch >= entry['batch']:
                entry['batch'], entry['rec'] = batch, item
    return products, sources, skipped, rows_without_pid


def cache_diagnostics(cache_dir: Path, sources: list[dict], skipped: list[dict],
                      observed_at: datetime) -> dict:
    """Classify excluded evidence without changing the historical cache universe.

    Only skipped files are reread. A filename alone never proves source absence;
    the upstream missing-table digest must identify that same batch. Complete
    caches supersede older failures, while newer failures remain visible even
    when the prior complete cache can still provide historical product records.
    """
    categories = ('known_source_absent', 'partial_failed', 'invalid_cache', 'missing_cache')
    result = {'format_version': 1, **{key: [] for key in categories}, 'superseded_evidence': []}
    complete = {source['batch']: source for source in sources}
    candidates: dict[int, list[tuple[datetime | None, str, dict]]] = defaultdict(list)

    def timestamp(value):
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.utcoffset() is not None else None
        except ValueError:
            return None

    for item in skipped:
        filename = item['file']
        match = re.fullmatch(r'batch(\d+)(?:\.[^.]+)?\.json', filename)
        batch = int(match[1]) if match else None
        record = {'batch': batch, 'file': filename, 'reason': item['reason'],
                  'fetched_at': item.get('fetched_at'), 'retained_complete_cache': batch in complete}
        category = 'invalid_cache'
        fetched = None
        try:
            data = json_object((cache_dir / filename).read_text(encoding='utf-8'), filename)
            record['fetched_at'] = data.get('fetched_at')
            actual_batch = str(data.get('batch', ''))
            if (batch is None or not actual_batch.isascii() or not actual_batch.isdecimal()
                    or int(actual_batch) != batch):
                raise ValueError('evidence_batch_mismatch_or_missing')
            fetched = timestamp(data.get('fetched_at'))
            if data.get('fetched_at') is not None and fetched is None:
                raise ValueError('invalid fetched_at')
            if fetched is not None and fetched > observed_at:
                raise ValueError('future fetched_at')
            if data.get('absent_upstream') is True:
                digest = str(data.get('absent_digest') or '')
                missing_table = re.search(
                    r"\bTable\s+['`\"](?:\w+\.)?clcp_chpdpk_(\d+)['`\"]\s+doesn['’]t exist\b",
                    digest, re.IGNORECASE)
                if (not missing_table or int(missing_table[1]) != batch
                        or data.get('complete') or data.get('products')):
                    raise ValueError('unverified_or_conflicting_source_absence')
                category = 'known_source_absent'
                record['reason'] = digest
            elif (data.get('complete') is False or data.get('failures')):
                if (not isinstance(data.get('products'), list)
                        or not isinstance(data.get('failures', []), list) or fetched is None):
                    raise ValueError('invalid_partial_evidence')
                category = 'partial_failed'
                failures = data.get('failures') or []
                record['reason'] = ('; '.join(str(failure) for failure in failures)
                                    if failures else 'batch enumeration incomplete')
            else:
                record['reason'] = item['reason'] if filename == f'batch{batch}.json' else 'unrecognized_cache_evidence'
        except (OSError, UnicodeError, ValueError) as exc:
            record['reason'] = str(exc)
        if category == 'invalid_cache':
            # Corrupt/ambiguous records cannot establish a trustworthy ordering;
            # do not hide them merely because a usable complete cache also exists.
            result[category].append(record)
        else:
            candidates[batch].append((fetched, category, record))

    minimum = datetime.min.replace(tzinfo=timezone.utc)
    for batch, evidence in candidates.items():
        evidence.sort(key=lambda entry: (
            entry[0] or minimum, entry[1] == 'partial_failed', entry[2]['file'] != f'batch{batch}.json'))
        newest = evidence[-1]
        full_time = timestamp(complete.get(batch, {}).get('fetched_at'))
        for entry in evidence:
            fetched, category, record = entry
            newer_evidence = entry is not newest
            newer_complete = fetched is not None and full_time is not None and fetched < full_time
            if newer_evidence or newer_complete:
                result['superseded_evidence'].append({
                    **record, 'category': category,
                    'superseded_by': (f'batch{batch}.json' if newer_complete else newest[2]['file']),
                })
            else:
                result[category].append(record)

    if complete:
        diagnosed = {record['batch'] for key in categories for record in result[key]}
        for batch in sorted(set(range(min(complete), max(complete) + 1)) - complete.keys() - diagnosed):
            result['missing_cache'].append({'batch': batch, 'file': None,
                                            'reason': 'no_cache_or_evidence_between_complete_batches',
                                            'fetched_at': None, 'retained_complete_cache': False})
    for key in (*categories, 'superseded_evidence'):
        result[key].sort(key=lambda record: (record['batch'] is None, record['batch'] or 0, record['file'] or ''))
    result['coverage_limited'] = any(result[key] for key in categories)
    result['has_actionable_warning'] = any(result[key] for key in categories if key != 'known_source_absent')
    return result


def document_state(relative, size, is_pdf, fields: dict, parse_error, download_error, pdf_root: Path) -> str:
    if is_pdf is None:
        return 'no_document'
    if is_pdf not in (0, 1) or not isinstance(size, int) or size < 0:
        raise ValueError('invalid document is_pdf/bytes metadata')
    if str(download_error or '').startswith('下载失败') or (is_pdf == 0 and size == 0):
        return 'download_failed'
    if not relative or not isinstance(relative, str) or size == 0:
        return 'pdf_invalid'
    try:
        relative_path = Path(relative)
        path = (pdf_root / relative_path).resolve()
        if relative_path.is_absolute() or not path.is_relative_to(pdf_root.resolve()):
            return 'pdf_invalid'
        if path.stat().st_size != size:
            return 'pdf_invalid'
        with path.open('rb') as handle:
            actual_pdf = handle.read(4) == b'%PDF'
        if actual_pdf != bool(is_pdf):
            return 'pdf_invalid'
    except OSError:
        return 'pdf_invalid'
    if not is_pdf:
        return 'non_pdf'
    return 'pdf_unparsed' if str(parse_error or '').strip() or not fields else 'parsed'


def compute_inventory(db_path: Path, catalog_db: Path, cache_dir: Path, pdf_root: Path,
                      *, today: date | None = None) -> tuple[dict, dict]:
    """Return summary/candidates without side effects; supplied dates use end-of-day freshness.

    A normal business schema including tracking tables is required, even for fixtures.
    The six catalog buckets deliberately count model codes rather than product IDs.
    """
    observed_at = datetime.now(TZ) if today is None else datetime.combine(today, time.max, TZ)
    today = observed_at.date()
    products, sources, skipped, rows_without_pid = load_caches(cache_dir, observed_at)
    diagnostics = cache_diagnostics(cache_dir, sources, skipped, observed_at)
    catalog_energy = load_catalog_energy(catalog_db)

    with readonly_snapshot(db_path) as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if missing_tables := REQUIRED_TABLES - tables:
            raise ValueError(f'business database missing required tables: {sorted(missing_tables)}')
        unfinished_runs = con.execute(
            "select count(*) from ingestion_runs where completed_at is null or completed_at=''").fetchone()[0]
        last_run = con.execute(
            'select id, started_at, completed_at from ingestion_runs order by id desc limit 1').fetchone()
        ann: dict[str, dict] = {}
        by_model_batch: dict[tuple[str, int | None], list[dict]] = defaultdict(list)
        models_with_pdf: set[str] = set()
        channel_bytes: dict[str, list[int]] = defaultdict(list)
        doc_states: Counter = Counter()
        for (pid, model, name, raw_json, batch, vid, rel, size, is_pdf, fields_json, parse_error, download_error) in con.execute(
                'select a.source_product_id, a.model_code, a.product_name, a.raw_json, b.batch, a.vehicle_id, '
                'd.relative_path, d.bytes, d.is_pdf, f.fields_json, f.parse_error, d.error '
                'from announcements a left join batches b on b.id=a.batch_id '
                'left join documents d on d.announcement_id=a.id '
                'left join announcement_fields f on f.announcement_id=a.id'):
            fields = json_object(fields_json, f'announcement {pid} fields_json')
            raw = json_object(raw_json, f'announcement {pid} raw_json')
            if batch not in (None, '') and not str(batch).isdigit():
                raise ValueError(f'announcement {pid}: invalid batch {batch!r}')
            state = document_state(rel, size, is_pdf, fields, parse_error, download_error, pdf_root)
            doc_states[state] += 1
            item = {'pid': str(pid), 'model': (model or '').strip(), 'name': (name or '').strip(),
                    'batch': int(batch) if str(batch or '').isdigit() else None, 'vehicle_id': vid,
                    'state': state, 'fields': fields,
                    'relative_path': rel, 'raw': raw}
            ann[item['pid']] = item
            by_model_batch[(item['model'].upper(), item['batch'])].append(item)
            if state in VALID_PDF_STATES:
                models_with_pdf.add(item['model'].upper())
                if classify_vehicle(item['model'], item['name'], item['raw'])['inclusion_gate'] == 'accepted':
                    channel_bytes[derive_channel(item['model'], item['name'])[0]].append(size)
        vehicles = {code.strip().upper(): vid for vid, code in con.execute(
            'select id, announcement_model_code from vehicles')}
        outcomes = best_model_outcomes(con)
        events = T.events(con)
        evidence = T.scope_evidence(con)
        latest_batch = T.latest_formal_batch(con)
        coverage = coverage_buckets(con, catalog_db)
        slots = []
        if last_run:
            slots = con.execute(
                'select substr(downloaded_at,1,15) as slot, count(*) from documents '
                'where downloaded_at between ? and ? group by slot order by slot',
                (last_run[1], last_run[2] or '9999')).fetchall()

    # ---------------------------------------------------------------- 逐事件跟踪
    event_scopes = {ev['event_id']: T.event_scope(ev, evidence) for ev in events}
    cache_model_batches = {pair for entry in products.values() for pair in entry['model_batches']}
    ev_by_pid: dict[str, list[dict]] = defaultdict(list)
    tracking: Counter = Counter()
    model_only_events: Counter = Counter()
    window_until: Counter = Counter()
    for ev in events:
        scope = event_scopes[ev['event_id']]
        key = (ev['model_code'].upper(), int(ev['batch']))
        matched = [a for a in by_model_batch.get(key, [])
                   if not ev['product_id'] or a['pid'] == ev['product_id']]
        valid = bool(matched) and all(a['state'] in VALID_PDF_STATES for a in matched)
        if scope['inclusion_gate'] != 'accepted':
            bucket = scope['inclusion_gate']
        elif not T.in_window(ev, latest_batch, today=today):
            bucket = 'out_of_window_done' if ev['status'] == 'done' else 'dormant'
        elif valid:
            bucket = 'resolved_valid_pdf'
        elif ev['next_check_at'] and ev['next_check_at'][:10] > today.isoformat():
            bucket = 'deferred'
        else:
            bucket = 'due'
        identity = 'product_id' if ev['product_id'] else 'model_only'
        tracking[(ev['kind'], int(ev['batch']), identity, ev['status'], bucket)] += 1
        if ev['product_id']:
            ev_by_pid[ev['product_id']].append({'status': ev['status'], 'bucket': bucket})
        else:
            listed = 'in_official_batch_list' if key in cache_model_batches else 'not_in_official_batch_list'
            model_only_events[(bucket, listed)] += 1
        if scope['inclusion_gate'] == 'accepted' and ev['status'] != 'done':
            event_day = T.parse_date(ev['event_date'] or '')
            basis = event_day or T.parse_date(ev['first_seen_at'] or '')
            until = (basis + timedelta(days=T.DAYS)).isoformat() if basis else 'unknown'
            window_until[(int(ev['batch']), identity, 'event_date' if event_day else 'first_seen_at', until)] += 1
    # ---------------------------------------------------------------- 批次缓存产品
    universe: Counter = Counter()
    by_group_channel: Counter = Counter()
    by_group_band: Counter = Counter()
    reasons: Counter = Counter()
    tracking_of_missing: Counter = Counter()
    outside_missing: Counter = Counter()
    outside_ev_code: Counter = Counter()
    product_group_state: dict[str, tuple[str, str]] = {}
    missing, non_pdf, unparsed, invalid, refresh, scope_review, download_failed = [], [], [], [], [], [], []
    for pid, entry in products.items():
        rec, batch = entry['rec'], entry['batch']
        model = str(rec.get('clxh') or '').strip()
        name = str(rec.get('clmc') or '').strip()
        scope = classify_vehicle(model, name, rec)
        found = ann.get(pid)
        db_batch = found['batch'] if found else None
        db_path_rel = found['relative_path'] if found else None
        cat_energy = catalog_energy.get(model.upper(), ('', ''))[0]
        if found and found['fields']:
            energy, esource = normalize_energy(found['fields'].get('fuel_type', ''), found['name'] or name,
                                               found['fields'].get('model_full', ''), cat_energy)
        else:
            energy, esource = normalize_energy('', name, '', cat_energy)
        # A later out-of-scope fuel batch must not hide an authorized 408/409 occurrence.
        authorized = entry['batches'] & FUEL_BATCHES
        if energy not in NEV and authorized:
            batch = max(authorized)
            rec = entry['records_by_batch'][batch]
            model, name = str(rec.get('clxh') or '').strip(), str(rec.get('clmc') or '').strip()
            scope = classify_vehicle(model, name, rec)
        gate = scope['inclusion_gate']
        if gate == 'excluded':
            group = 'excluded'
        elif gate == 'pending_review':
            group = 'scope_review'
        elif energy in NEV:
            group = 'nev'
        elif batch in FUEL_BATCHES:
            group = 'fuel_408_409' if energy in FUEL else 'unknown_408_409'
        else:
            group = 'fuel_outside_408_409' if energy in FUEL else 'unknown_energy_outside_408_409'
        state = found['state'] if found else 'missing'
        universe[(group, state)] += 1
        product_group_state[pid] = (group, state)
        channel = derive_channel(model, name)[0]
        row = {'product_id': pid, 'model_code': model, 'product_name': name,
               'trademark': rec.get('cpsb', ''), 'company': rec.get('qymc', ''),
               'batch': batch, 'cached_latest_batch': entry['batch'], 'batches': sorted(entry['batches']),
               'model_codes': sorted({code for code, _ in entry['model_batches']}), 'channel': channel,
               'energy_type': energy, 'energy_source': esource, 'catalog_energy': cat_energy,
               'group': group, 'dataTag': rec.get('dataTag', '')}
        if group == 'scope_review':
            scope_review.append({**row, **scope, 'state': state})
            continue
        if group not in IN_SCOPE_GROUPS:
            if state == 'missing' and group != 'excluded':
                outside_missing[(group, channel)] += 1
                if group == 'unknown_energy_outside_408_409' and EV_MODEL_CODE.search(model.upper()):
                    outside_ev_code[channel] += 1
            continue
        by_group_channel[(group, channel, state)] += 1
        by_group_band[(group, band(batch), state)] += 1
        if state == 'missing':
            model_up = model.upper()
            vid = vehicles.get(model_up)
            outcome = outcomes.get(vid, '') if vid else ''
            evs = ev_by_pid.get(pid, [])
            if model_up in models_with_pdf:
                reason = '同型号已有其他产品ID的PDF，缺本产品ID'
            elif outcome == 'no_match':
                reason = '型号历史查询无匹配，但官方批次清单已出现'
            elif outcome in {'query_failed', 'interrupted', 'querying'}:
                reason = '型号历史采集失败或中断'
            elif vid is not None:
                reason = '已进入车辆采集池但无本产品结局'
            elif evs:
                reason = '已登记近期正式事件，尚未下载'
            else:
                reason = '未进入车辆采集池，也未登记跟踪事件'
            tracking_label = ','.join(sorted({f"{e['status']}/{e['bucket']}" for e in evs})) or '无跟踪事件'
            row.update({'reason': reason, 'model_outcome': outcome, 'tracking': tracking_label})
            reasons[(group, reason)] += 1
            tracking_of_missing[(group, tracking_label)] += 1
            missing.append(row)
        elif state == 'download_failed':
            download_failed.append({**row, 'db_batch': db_batch, 'relative_path': db_path_rel})
        elif state == 'non_pdf':
            non_pdf.append({**row, 'db_batch': db_batch, 'relative_path': db_path_rel})
        elif state == 'pdf_unparsed':
            unparsed.append({**row, 'db_batch': db_batch, 'relative_path': db_path_rel})
        elif state in {'pdf_invalid', 'no_document'}:
            invalid.append({**row, 'db_batch': db_batch, 'state': state, 'relative_path': db_path_rel})
        elif state == 'parsed' and db_batch is not None and db_batch < batch:
            refresh.append({**row, 'db_batch': db_batch, 'relative_path': db_path_rel})

    db_only: Counter = Counter()
    for pid, item in ann.items():
        if pid not in products:
            db_only[(band(item['batch']) if item['batch'] and item['batch'] >= 173 else 'lt173_or_none',
                     item['state'])] += 1

    cache_by_model: dict[str, list[str]] = defaultdict(list)
    for pid, entry in products.items():
        for code in {model for model, _ in entry['model_batches']}:
            cache_by_model[code].append(pid)
    no_match_key = next(k for k in coverage if '查无' in k)
    no_match_present: Counter = Counter()
    no_match_absent = []
    for code in sorted(coverage[no_match_key]):
        pids = cache_by_model.get(code, [])
        if pids:
            for pid in pids:
                no_match_present[product_group_state[pid]] += 1
        else:
            no_match_absent.append(code)

    mean_bytes = {channel: sum(values) / len(values) for channel, values in channel_bytes.items() if values}
    fetched = sorted(source['fetched_at'] for source in sources)
    cached_batches = sorted(source['batch'] for source in sources)
    valid_until = min((source['valid_until'] for source in sources), default=None)
    in_scope_totals = {}
    for group, label in IN_SCOPE_GROUPS.items():
        states = {state: count for (g, state), count in universe.items() if g == group}
        in_scope_totals[group] = {'label': label, 'total': sum(states.values()), **states}
    summary = {
        'as_of_date': today.isoformat(),
        'collection_scope': COLLECTION_SCOPE,
        'method': [
            '证据范围仅为通过完整性校验的本地批次缓存，不声明覆盖官方实时全集；过期缓存保留并单独标记。',
            '产品按产品ID去重；新能源取最大批次；燃油及能源未确认取已出现的408/409批中的最大批次。',
            '范围和能源复用classify_vehicle/normalize_energy；能源未确认保留为候选，不等于已确认符合收录条件。',
            '文档检查路径边界、实际大小、%PDF文件头和解析状态；不重算PDF SHA-256，不证明文件完整哈希一致。',
            '目录六桶复用collection_coverage，为独立型号维度和业务记录结局，不与产品ID、事件计数相加。',
            '事件复用event_scope和in_window；历史型号别名只关联其真实出现批次，不作别名与批次的交叉组合。',
        ],
        'inputs': {
            'business_db': {'path': str(db_path), 'unfinished_runs': unfinished_runs,
                            'last_run': dict(zip(('id', 'started_at', 'completed_at'), last_run, strict=True))
                            if last_run else None},
            'catalog_db': {'path': str(catalog_db), 'models_in_coverage': sum(len(v) for v in coverage.values())},
            'batch_caches': {
                'dir': str(cache_dir), 'validator': 'announcement_catalog_gap.cache_is_complete',
                'coverage_basis': 'complete_cached_batches_only', 'max_age_days': G.CACHE_MAX_AGE_DAYS,
                'freshness_evaluated_at': observed_at.isoformat(), 'valid_until': valid_until,
                'complete_files': len(sources), 'complete_batches': cached_batches,
                'stale': any(source['stale'] for source in sources),
                'stale_files_allowed': sum(source['stale'] for source in sources), 'files': sources,
                'batch_range': [cached_batches[0], cached_batches[-1]] if cached_batches else None,
                'missing_batches_in_range': sorted(set(range(cached_batches[0], cached_batches[-1] + 1))
                                                   - set(cached_batches)) if cached_batches else [],
                'fetched_at_range': [fetched[0], fetched[-1]] if fetched else None,
                'files_with_verify_missed': [{'batch': source['batch'], 'verify_missed': source['verify_missed']}
                                            for source in sources if source['verify_missed']],
                'cache_rows': sum(source['count'] for source in sources), 'unique_product_ids': len(products),
                'rows_without_product_id': rows_without_pid, 'skipped': skipped,
                'cache_diagnostics': diagnostics,
            },
        },
        'documents': {'states': dict(doc_states), 'models_with_valid_pdf': len(models_with_pdf),
                      'validation_level': 'path/size/magic', 'sha256_verified': False},
        'in_scope_totals': in_scope_totals,
        'universe_by_group_state': rows(universe, ('group', 'state')),
        'coverage_by_group_band': rows(by_group_band, ('group', 'band', 'state')),
        'missing': {
            'total': len(missing), 'by_group': dict(Counter(row['group'] for row in missing)),
            'by_group_channel': rows(Counter({key: value for key, value in by_group_channel.items()
                                              if key[2] == 'missing'}), ('group', 'channel', 'state')),
            'by_group_batch_band': rows(Counter({key: value for key, value in by_group_band.items()
                                                 if key[2] == 'missing'}), ('group', 'band', 'state')),
            'by_reason': rows(reasons, ('group', 'reason')),
            'by_tracking_status': rows(tracking_of_missing, ('group', 'tracking')),
            'nev_with_catalog_energy': sum(row['group'] == 'nev' and bool(row['catalog_energy']) for row in missing),
            'mean_pdf_bytes_by_channel': {key: round(value) for key, value in mean_bytes.items()},
            'estimated_pdf_bytes': round(sum(mean_bytes.get(row['channel'], 0) for row in missing)),
            'size_estimate_missing_sample_channels': sorted({row['channel'] for row in missing} - mean_bytes.keys()),
        },
        'non_pdf': len(non_pdf), 'download_failed': len(download_failed), 'pdf_unparsed': len(unparsed),
        'pdf_invalid_or_no_document': len(invalid),
        'refresh_candidates': {'total': len(refresh), 'by_group': dict(Counter(row['group'] for row in refresh))},
        'scope_review': {'total': len(scope_review), 'by_state': dict(Counter(row['state'] for row in scope_review))},
        'outside_scope_missing_not_counted': rows(outside_missing, ('group', 'channel')),
        'unknown_energy_outside_with_ev_model_code_not_counted': dict(outside_ev_code),
        'db_announcements_not_in_caches': rows(db_only, ('band', 'state')),
        'catalog_coverage': {key: len(value) for key, value in coverage.items()},
        'catalog_no_match': {'total': len(coverage[no_match_key]),
                             'models_absent_from_caches': len(no_match_absent),
                             'cache_products_of_present_models': rows(no_match_present, ('group', 'state'))},
        'tracking': {'latest_formal_batch': latest_batch, 'today': today.isoformat(),
                     'days': T.DAYS, 'batches': T.BATCHES,
                     'events': rows(tracking, ('kind', 'batch', 'identity', 'status', 'bucket')),
                     'model_only_events': rows(model_only_events, ('bucket', 'official_list')),
                     'accepted_not_done_window_until': rows(window_until, ('batch', 'identity', 'basis', 'until'))},
        'last_run_download_slots_utc_10min': [{'slot': slot, 'documents': count} for slot, count in slots],
    }
    def sort_key(row):
        return row['group'], row['batch'], row['model_code'], row['product_id']
    candidates = {
        'as_of_date': today.isoformat(), 'collection_scope': COLLECTION_SCOPE,
        'missing': sorted(missing, key=sort_key), 'non_pdf': sorted(non_pdf, key=sort_key),
        'download_failed': sorted(download_failed, key=sort_key), 'pdf_unparsed': sorted(unparsed, key=sort_key),
        'pdf_invalid_or_no_document': sorted(invalid, key=sort_key), 'refresh': sorted(refresh, key=sort_key),
        'scope_review': sorted(scope_review, key=sort_key), 'catalog_no_match_absent_from_caches': no_match_absent,
    }
    return summary, candidates
