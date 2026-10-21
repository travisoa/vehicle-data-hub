"""从封存的公开站点快照恢复本地资产；不访问源站，不伪造采集或目录历史。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from miit_gonggao import collection, collection_tracking, core, images, jianmian

MARKER = '_site_snapshot_recovery'


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(path: Path) -> str:
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def local_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if (path.is_absolute() or '..' in path.parts or '\\' in relative
            or path.parts[:2] != ('downloads', 'announcement_site')):
        raise ValueError(f'不安全的公告路径：{relative}')
    resolved = (root / path).resolve()
    if root.resolve() not in resolved.parents or not resolved.is_file():
        raise ValueError(f'公告文件缺失或越界：{relative}')
    return resolved


def recovery_summary(conn: sqlite3.Connection) -> dict:
    if not conn.execute("select 1 from sqlite_master where name='site_recovery_sources'").fetchone():
        return {}
    row = conn.execute('select metadata_json from site_recovery_sources limit 1').fetchone()
    return json.loads(row[0]) if row else {}


def recovered_records(conn: sqlite3.Connection) -> dict[str, dict]:
    """只向构库提供尚未被正式采集/重新解析替换的恢复证据。"""
    if not recovery_summary(conn):
        return {}
    active = {}
    for pid, raw, fields in conn.execute(
        'select a.source_product_id,a.raw_json,f.fields_json from announcements a '
        'left join announcement_fields f on f.announcement_id=a.id'
    ):
        active[pid] = (json.loads(raw).get(MARKER), json.loads(fields or '{}'))
    result = {}
    for pid, snapshot, payload in conn.execute(
        'select source_product_id,snapshot_sha256,row_json from site_recovery_records'
    ):
        row = json.loads(payload)
        if row['availability'] in {'pending_source', 'fetch_failed'}:
            result[pid] = row
        elif pid in active and active[pid] == (snapshot, json.loads(row['fields_json'] or '{}')):
            result[pid] = row
    return result


def inspect(snapshot: Path, root: Path) -> tuple[dict, list[dict], list[dict], list[dict]]:
    snapshot, root = snapshot.resolve(), root.resolve()
    if any(Path(str(snapshot) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('只接受已封存、没有 journal/WAL 的站点快照')
    snapshot_sha = digest(snapshot)
    with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro&immutable=1', uri=True)) as con:
        con.row_factory = sqlite3.Row
        if [r[0] for r in con.execute('pragma quick_check')] != ['ok']:
            raise ValueError('站点快照 quick_check 失败')
        meta = dict(con.execute('select key,value from meta'))
        if meta.get('schema_version') != '3' or meta.get('collection_scope') != 'automotive_complete_v1':
            raise ValueError('只支持 schema 3 / automotive_complete_v1 的站点快照')
        rows = [dict(row) for row in con.execute('select * from announcement_flat order by id')]
        photos = [dict(row) for row in con.execute('select * from announcement_photos order by ordinal')]
        series = [dict(row) for row in con.execute('select * from model_series order by id')]
        if (len(rows) != int(meta['source_rows']) or len(series) != int(meta['series_count'])
                or len(rows) != con.execute('select count(*) from search_fts').fetchone()[0]):
            raise ValueError('站点快照行数与 meta/FTS 不一致')
    pids, public_ids, by_public = set(), set(), {}
    files = 0
    for row in rows:
        pid, public = row['source_product_id'], row['public_id']
        if (not pid or pid in pids or public in public_ids or not re.fullmatch('[0-9a-f]{12}', public)
                or row['availability'] not in {'published', 'no_document', 'pending_source', 'fetch_failed'}):
            raise ValueError('公告身份重复、无效或状态不受支持')
        pids.add(pid)
        public_ids.add(public)
        by_public[public] = row
        if not isinstance(json.loads(row['fields_json'] or '{}'), dict):
            raise ValueError('公告参数必须是 JSON 对象')
        if row['availability'] == 'published':
            path = local_file(root, row['doc_path'])
            with path.open('rb') as handle:
                head = handle.read(4)
            if (row['is_pdf'] != 1 or head != b'%PDF' or path.stat().st_size != row['doc_bytes']
                    or digest(path) != row['doc_sha256']):
                raise ValueError(f'PDF 校验失败：{row["doc_path"]}')
            files += 1
    if files != int(meta['published_rows']):
        raise ValueError('已发布 PDF 数与 meta 不一致')
    for photo in photos:
        if photo['announcement_public_id'] not in by_public:
            raise ValueError('照片没有对应公告')
        if (photo['pdf_offset'] is not None
                and photo['photo_path'] != by_public[photo['announcement_public_id']]['doc_path']):
            raise ValueError('内嵌照片必须属于对应公告 PDF')
        path = local_file(root, photo['photo_path'])
        with path.open('rb') as handle:
            if photo['pdf_offset'] is not None:
                if photo['pdf_offset'] < 0:
                    raise ValueError('照片 PDF 偏移无效')
                handle.seek(photo['pdf_offset'])
            content = handle.read(photo['photo_bytes'])
        if (photo['photo_bytes'] <= 0 or len(content) != photo['photo_bytes']
                or hashlib.sha256(content).hexdigest() != photo['photo_sha256']
                or photo['width'] <= 0 or photo['height'] <= 0):
            raise ValueError(f'照片校验失败：{photo["public_id"]}')
        if photo['pdf_offset'] is None and path.stat().st_size != photo['photo_bytes']:
            raise ValueError('独立照片大小不符')
    metadata = {'snapshot': str(snapshot), 'snapshot_sha256': snapshot_sha, 'built_at': meta['built_at'],
                'rows': len(rows), 'pdfs': files, 'photos': len(photos), 'series': len(series),
                'availability': dict(Counter(r['availability'] for r in rows)),
                'unavailable': ['original_query_responses', 'ingestion_history', 'official_catalog_articles',
                                'complete_batch_caches', 'original_profile_query_prefixes'],
                'catalog_is_partial': True}
    if digest(snapshot) != snapshot_sha:
        raise ValueError('核验期间站点快照发生变化')
    return metadata, rows, photos, series


def photo_manifests(root: Path, rows: list[dict], photos: list[dict], metadata: dict) -> None:
    by_public = {row['public_id']: row for row in rows}
    grouped = {}
    for photo in photos:
        grouped.setdefault(photo['announcement_public_id'], []).append(photo)
    for public, items in grouped.items():
        row = by_public[public]
        identity = {'clxh': row['model_code'], 'cpid': row['source_product_id']}
        folder = images.image_folder(identity, root / Path(row['doc_path']).parent)
        if root.resolve() not in folder.resolve().parents:
            raise ValueError('恢复照片目录越界')
        entries = []
        for photo in items:
            source = local_file(root, photo['photo_path'])
            if photo['pdf_offset'] is None:
                if source.parent.resolve() != folder.resolve():
                    raise ValueError('照片不在上游 image_folder 定义的目录')
                name = source.name
            else:
                name = f'recovered_{photo["photo_sha256"]}.jpg'
                with source.open('rb') as handle:
                    handle.seek(photo['pdf_offset'])
                    content = handle.read(photo['photo_bytes'])
                destination = folder / name
                if destination.exists() and destination.read_bytes() != content:
                    raise ValueError('恢复照片目标冲突')
                if not destination.exists():
                    images.atomic_write(destination, content)
            entries.append({'status': 'ok', 'file': name, 'bytes': photo['photo_bytes'],
                            'sha256': photo['photo_sha256'], 'width': photo['width'], 'height': photo['height']})
        manifest = {'product_id': identity['cpid'], 'model_code': identity['clxh'], 'batch': row['batch'],
                    'status': 'ok', 'downloaded': len(entries), 'failed': 0, 'images': entries,
                    'recovered_from_site': metadata['snapshot_sha256'], 'observed_at': metadata['built_at']}
        path = folder / 'manifest.json'
        if path.exists() and json.loads(path.read_text()) != manifest:
            raise ValueError('已有照片索引，拒绝覆盖')
        if not path.exists():
            images.atomic_write(path, encode(manifest).encode())


def recovered_profiles(rows: list[dict], series: list[dict], metadata: dict) -> list[dict]:
    profiles = []
    for item in series:
        if item['source'] != 'profile':
            continue
        members = [r for r in rows if r.get('series_id') == item['id']]
        profiles.append({'name': item['name'], 'aliases': [],
                         'gonggao': {'trademark': item['trademark'], 'filters': {},
                                     'model_prefixes': sorted({r['model_code'] for r in members}),
                                     'exclude_model_prefixes': []},
                         'recovery': {'snapshot_sha256': metadata['snapshot_sha256'],
                                      'prefixes_basis': 'observed_exact_model_codes_only',
                                      'source_product_ids': [r['source_product_id'] for r in members],
                                      'observed_aliases': json.loads(item['aliases_json'] or '[]')}})
    return profiles


def restore(snapshot: Path, root: Path, *, apply: bool = False) -> dict:
    root = root.resolve()
    targets = [root / 'data' / name for name in
               ('announcement_site.sqlite', 'jianmian_catalog.sqlite', 'vehicle_profiles.json')]
    if any(path.exists() or path.is_symlink() for path in targets):
        raise ValueError('恢复只允许空环境；已有业务库、目录或档案须先人工核对，禁止覆盖')
    metadata, rows, photos, series = inspect(snapshot, root)
    metadata['status'] = 'preview'
    if not apply:
        return metadata
    targets[0].parent.mkdir(parents=True, exist_ok=True)
    with collection.database_lock(targets[0]):
        if any(path.exists() or path.is_symlink() for path in targets):
            raise ValueError('核验后恢复目标已出现，拒绝覆盖')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        stage = root / 'var/runs' / (stamp + '-site-recovery')
        stage.mkdir(parents=True)
        (root / 'downloads/announcement_batches').mkdir(parents=True, exist_ok=True)
        metadata.update(status='prepared', recovered_at=collection.utc_now())
        (stage / 'summary.json').write_text(encode(metadata) + '\n')
        db = stage / targets[0].name
        with closing(sqlite3.connect(db)) as con:
            collection.ensure_schema(con)
            collection_tracking.ensure_schema(con)
            con.executescript('''
                create table site_recovery_sources(snapshot_sha256 text primary key,metadata_json text not null);
                create table site_recovery_records(source_product_id text primary key,
                    snapshot_sha256 text not null,row_json text not null);
            ''')
            con.execute('insert into site_recovery_sources values (?,?)',
                        (metadata['snapshot_sha256'], encode({**metadata, 'status': 'restored'})))
            vehicle_ids = {}
            for row in rows:
                model = row['model_code']
                if model not in vehicle_ids:
                    cursor = con.execute('insert into vehicles(market_name,announcement_model_code,catalog_name,'
                                         'catalog_batch,catalog_category,catalog_company) values (?,?,?,?,?,?)',
                                         (row['common_name'] or model, model, 'recovered_site_snapshot', '',
                                          row['category'] or '', row['company'] or ''))
                    vehicle_ids[model] = cursor.lastrowid
                con.execute('insert into site_recovery_records values (?,?,?)',
                            (row['source_product_id'], metadata['snapshot_sha256'], encode(row)))
                if row['availability'] in {'pending_source', 'fetch_failed'}:
                    continue  # 占位身份不是产品 ID，也不能据它伪造 tracking_event 或查询结局。
                raw = {'cpid': row['source_product_id'], 'clxh': model, 'clmc': row['product_name'],
                       'qymc': row['company'], 'cpsb': row['trademark'], 'gppc': row['batch'],
                       'category': row['category'], MARKER: metadata['snapshot_sha256']}
                brand_id = collection.get_or_create_id(con, 'brands', row['trademark'] or '')
                batch_id = collection.get_or_create_id(con, 'batches', row['batch'] or '', 'batch')
                cursor = con.execute('insert into announcements(source_product_id,vehicle_id,brand_id,batch_id,'
                                     'company,trademark,model_code,product_name,raw_json,first_seen_at) '
                                     'values (?,?,?,?,?,?,?,?,?,?)',
                                     (row['source_product_id'], vehicle_ids[model], brand_id, batch_id,
                                      row['company'], row['trademark'], model, row['product_name'], encode(raw),
                                      ''))  # 原始首次发现时间未知；恢复时间只写来源表。
                aid = cursor.lastrowid
                published = row['availability'] == 'published'
                con.execute('insert into documents(announcement_id,relative_path,is_pdf,bytes,sha256,'
                            'downloaded_at,error) values (?,?,?,?,?,?,?)',
                            (aid, row['doc_path'] or 'unavailable/' + row['source_product_id'], int(published),
                             row['doc_bytes'] if published else 0, row['doc_sha256'] if published else None,
                             '', '' if published else '站点快照记录无 PDF；原始非 PDF 响应未恢复'))
                con.execute('insert into announcement_fields values (?,?,?)', (aid, row['fields_json'], ''))
            con.commit()
            if con.execute('pragma foreign_key_check').fetchall() or con.execute('pragma quick_check').fetchone()[0] != 'ok':
                raise ValueError('恢复业务库完整性校验失败')
        with closing(jianmian.open_db(stage / targets[1].name)) as cat:
            cat.execute('create table recovery_metadata(metadata_json text not null)')
            cat.execute('insert into recovery_metadata values (?)', (encode(metadata),))
            cat.commit()  # 空的正式目录结构；不把公告参数冒充成目录附件原文。
        profiles = recovered_profiles(rows, series, metadata)
        core.save_mappings(stage / targets[2].name, profiles)
        photo_manifests(root, rows, photos, metadata)
        if digest(snapshot.resolve()) != metadata['snapshot_sha256']:
            raise ValueError('提交前站点快照发生变化')
        published = []
        try:
            for target in targets:
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.recovery-', delete=False) as handle:
                    candidate = Path(handle.name)
                    with (stage / target.name).open('rb') as source:
                        shutil.copyfileobj(source, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    candidate.chmod(0o644)
                    os.link(candidate, target)  # 排他发布，保留独立的恢复前证据副本。
                    info = candidate.stat()
                    published.append((target, info.st_dev, info.st_ino))
                finally:
                    candidate.unlink(missing_ok=True)
        except BaseException:
            for target, device, inode in reversed(published):
                info = target.lstat()
                if (info.st_dev, info.st_ino) == (device, inode):
                    target.unlink()  # 只撤销本次新建且尚未被替换的文件。
            metadata['status'] = 'publication_failed'
            (stage / 'summary.json').write_text(encode(metadata) + '\n')
            raise
        metadata.update(status='restored', profiles=len(profiles), run_directory=str(stage))
        has_cache = any((root / 'downloads/announcement_batches').glob('batch[0-9]*.json'))
        metadata['collection_status'] = 'refresh_requested' if has_cache else 'unavailable_without_batch_cache'
        (stage / 'summary.json').write_text(encode(metadata) + '\n')
        if has_cache:
            collection.refresh_collection_status(targets[0], catalog_db=targets[1], pdf_root=root, reason='site_recovery')
        return metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=collection.UPSTREAM_ROOT)
    parser.add_argument('--apply', action='store_true', help='校验全部本地文件后恢复到空环境；默认只预览')
    args = parser.parse_args(argv)
    try:
        result = restore(args.snapshot, args.root, apply=args.apply)
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(f'站点恢复失败：{exc}')
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
