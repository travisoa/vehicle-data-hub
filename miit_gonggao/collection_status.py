"""持久化公告收录汇总：查询只读快照，采集收尾或显式 --refresh 更新。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

ROOT = Path(__file__).resolve().parents[1]
FORMAT_VERSION = 1
TZ = ZoneInfo('Asia/Shanghai')
LOGIC_FILES = (
    'miit_gonggao/collection_status.py', 'miit_gonggao/collection_inventory.py',
    'miit_gonggao/collection_coverage.py', 'miit_gonggao/collection_tracking.py',
    'miit_gonggao/vehicle_categories.py', 'miit_gonggao/vehicle_classification.py',
    'scripts/announcement_catalog_gap.py',
)
GENERATION = re.compile(r'\d{8}T\d{12}Z-[0-9a-f]{12}\Z')


class StatusError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stat(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {'path': str(path.absolute()), 'exists': False}
    return {'path': str(path.resolve()), 'exists': True, 'size': stat.st_size,
            'mtime_ns': stat.st_mtime_ns, 'ctime_ns': stat.st_ctime_ns,
            'device': stat.st_dev, 'inode': stat.st_ino}


def input_signature(db_path: Path, catalog_db: Path, cache_dir: Path, pdf_root: Path) -> dict:
    """只读取输入的文件元数据；不扫描业务表、解析批次 JSON 或遍历 PDF。"""
    def database(path: Path) -> dict:
        # WAL 变化可能尚未合并进主文件，不能只比较 .sqlite 的时间/大小。
        resolved = path.resolve()
        return {'file': _stat(path), 'wal': _stat(Path(str(resolved) + '-wal')),
                'journal': _stat(Path(str(resolved) + '-journal'))}

    logic = {}
    for name in LOGIC_FILES:
        path = ROOT / name
        logic[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return {'format_version': FORMAT_VERSION, 'business_db': database(db_path),
            'catalog_db': database(catalog_db), 'pdf_root': str(pdf_root.resolve()),
            'cache_dir': str(cache_dir.resolve()), 'batch_caches': {
                p.name: _stat(p) for p in sorted(cache_dir.glob('batch*.json'))}, 'logic': logic}


def _paths(db_path: Path, catalog_db: Path | None = None, pdf_root: Path | None = None,
           cache_dir: Path | None = None, out_dir: Path | None = None) -> tuple[Path, Path, Path, Path, Path]:
    db = db_path.expanduser().resolve()
    root = pdf_root.expanduser().resolve() if pdf_root else db.parent.parent
    # 自定义业务库不得把共享 PDF 根对应的主库统计覆盖成测试/临时库结果。
    default_output = (db.parent.parent / 'var/reports/collection-status'
                      if db.name == 'announcement_site.sqlite' and db.parent.name == 'data'
                      else db.parent / (db.stem + '-collection-status'))
    return (db, (catalog_db or root / 'data/jianmian_catalog.sqlite').expanduser().resolve(),
            (cache_dir or root / 'downloads/announcement_batches').expanduser().resolve(), root,
            (out_dir or default_output).expanduser().resolve())


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise StatusError(f'无法读取统计记录 {path}: {exc}') from exc
    if not isinstance(value, dict):
        raise StatusError(f'统计记录不是 JSON 对象：{path}')
    return value


def _snapshot(out_dir: Path) -> dict | None:
    latest = out_dir / 'latest.json'
    if not latest.exists():
        return None
    value = _read_json(latest)
    if value.get('format_version') != FORMAT_VERSION or not GENERATION.fullmatch(value.get('generation', '')):
        raise StatusError('统计记录格式不受支持，请显式 --refresh 重建')
    return value


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def cache_messages(caches: dict) -> tuple[list[str], list[str], bool]:
    """Render saved diagnostics only; never reopen source cache evidence on reads."""
    warnings, notices = [], []
    diagnostics = caches.get('cache_diagnostics')
    if not isinstance(diagnostics, dict) or diagnostics.get('format_version') != 1:
        incomplete = bool(caches.get('skipped') or caches.get('missing_batches_in_range')
                          or not caches.get('complete_files'))
        if incomplete:
            warnings.append('旧版缓存证据尚未分类，请显式 --refresh 更新诊断；当前统计仅覆盖已保存的完整批次')
        return warnings, notices, incomplete

    def detail(record: dict) -> str:
        label = f"第 {record['batch']} 批" if record.get('batch') is not None else '批次未识别'
        observed = record.get('fetched_at') or '时间未记录'
        text = f"{label}（{record.get('file') or '无缓存文件'}；{observed}）：{record.get('reason') or '原因未记录'}"
        if record.get('retained_complete_cache'):
            text += '；保留该批旧完整缓存作为历史基线'
        return text

    for record in diagnostics.get('known_source_absent', []):
        notices.append('上游接口不可用，属于来源范围说明：' + detail(record)
                       + '；不作为成功的完整空批次，也不能据此认定该批公告不存在')
    for category, label in (('partial_failed', '部分枚举失败'), ('invalid_cache', '缓存校验失败'),
                            ('missing_cache', '缓存缺失且无来源证据')):
        warnings.extend(f'{label}：{detail(record)}' for record in diagnostics.get(category, []))
    if not caches.get('complete_files'):
        warnings.append('没有可用的完整批次缓存')
    return warnings, notices, bool(warnings)


def read_status(db_path: Path = ROOT / 'data/announcement_site.sqlite', *, catalog_db: Path | None = None,
                pdf_root: Path | None = None, cache_dir: Path | None = None,
                out_dir: Path | None = None) -> dict:
    db, catalog, cache, root, output = _paths(db_path, catalog_db, pdf_root, cache_dir, out_dir)
    snapshot = _snapshot(output)
    if snapshot is None:
        return {'status': 'missing', 'snapshot': None, 'stale_reasons': ['尚无统计记录；请执行 gonggao status --refresh']}
    current = input_signature(db, catalog, cache, root)
    previous = snapshot.get('input_signature', {})
    stale = [key for key, value in current.items() if value != previous.get(key)]
    summary = snapshot['summary']
    caches = summary.get('inputs', {}).get('batch_caches', {})
    until = _date(caches.get('valid_until'))
    cache_expired = bool(until and _now() >= until)
    cache_warnings, notices, cache_incomplete = cache_messages(caches)
    tracking_date = summary.get('tracking', {}).get('today')
    tracking_stale = bool(tracking_date and tracking_date != _now().astimezone(TZ).date().isoformat())
    warnings = []
    if cache_expired or caches.get('stale_files_allowed'):
        warnings.append('批次缓存已过期，保留历史基线，不代表官方当前全集')
    warnings.extend(cache_warnings)
    if tracking_stale:
        warnings.append('事件检查窗口截至记录生成日；时间变化未触发查询时重算')
    last_error = _read_json(output / 'last-error.json') if (output / 'last-error.json').is_file() else None
    if last_error:
        warnings.append('最近一次统计更新失败，当前显示上次成功记录：' + last_error.get('error', '未知错误'))
    return {'status': 'stale' if stale else 'ready', 'snapshot': snapshot, 'stale_reasons': stale,
            'last_error': last_error,
            'cache_expired': cache_expired, 'cache_incomplete': cache_incomplete,
            'tracking_stale': tracking_stale, 'warnings': warnings, 'notices': notices,
            'pdf_validation': '生成记录时核对路径、字节数和文件头；查看时不遍历 PDF，也不重算文件 SHA-256'}


def _atomic_json(path: Path, value: dict) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def _atomic_text(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def _lock(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.refresh.lock').open('a+b') as stream:
        try:
            if os.name == 'nt':
                if stream.tell() == 0:
                    stream.write(b'\0')
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise StatusError('已有统计更新进行中；保留当前记录') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _totals(summary: dict) -> dict[str, int]:
    states = summary.get('documents', {}).get('states', {})
    return {'business_announcements': sum(states.values()),
            'parsed': states.get('parsed', 0),
            'missing_products': summary.get('missing', {}).get('total', 0),
            'non_pdf': summary.get('non_pdf', 0),
            'pdf_unparsed': summary.get('pdf_unparsed', 0),
            'pdf_invalid_or_no_document': summary.get('pdf_invalid_or_no_document', 0),
            'download_failed': summary.get('download_failed', 0),
            'refresh_candidates': summary.get('refresh_candidates', {}).get('total', 0)}


def _parse_removed_batches(value: str) -> list[int]:
    parts = [part.strip() for part in value.split(',')]
    if not parts or any(not part.isascii() or not part.isdecimal() or int(part) <= 0 for part in parts):
        raise ValueError('移除批次须为逗号分隔的正整数，例如 408,409')
    batches = [int(part) for part in parts]
    if len(set(batches)) != len(batches):
        raise ValueError('移除批次不能重复')
    return batches


def _validate_rebase_request(*, force: bool, rebase_cache: bool, expect_generation: str | None,
                             removed_batches: list[int] | None, rebase_reason: str | None) -> None:
    supplied = (expect_generation, removed_batches, rebase_reason)
    if not rebase_cache:
        if any(value is not None for value in supplied):
            raise StatusError('基线调整参数必须与 --refresh --rebase-cache 一起使用')
        return
    if not force or any(value is None for value in supplied):
        raise StatusError('基线调整须同时提供 --refresh --rebase-cache --expect-generation '
                          '--removed-batches --rebase-reason')
    if not isinstance(expect_generation, str) or not GENERATION.fullmatch(expect_generation):
        raise StatusError('--expect-generation 必须是待核验的完整旧记录代号')
    if (not isinstance(removed_batches, list) or not removed_batches
            or any(type(batch) is not int or batch <= 0 for batch in removed_batches)
            or len(set(removed_batches)) != len(removed_batches)):
        raise StatusError('--removed-batches 必须是无重复的正整数批次列表')
    if not isinstance(rebase_reason, str) or not rebase_reason.strip():
        raise StatusError('--rebase-reason 不能为空；须说明主动调整来源基线的依据')


def _verified_cache_scope(cache: dict, label: str) -> dict:
    """受控重建要求旧、新完整集合都有可交叉核验的元数据，不能凭文件数猜范围。"""
    batches = cache.get('complete_batches')
    count = cache.get('complete_files')
    files = cache.get('files')
    if (not isinstance(batches, list) or not batches
            or any(type(batch) is not int or batch <= 0 for batch in batches)
            or len(set(batches)) != len(batches)
            or type(count) is not int or count != len(batches)
            or cache.get('batch_range') != [min(batches), max(batches)]
            or not isinstance(files, list) or len(files) != count
            or any(not isinstance(item, dict) or type(item.get('batch')) is not int for item in files)
            or sorted(item['batch'] for item in files) != sorted(batches)):
        raise StatusError(f'{label}完整批次元数据缺失或不一致，不能受控重建；请恢复可核验的完整缓存/记录')
    return {'complete_files': count, 'complete_batches': sorted(batches),
            'batch_range': [min(batches), max(batches)]}


def _cache_reduction_error(previous: dict, removed: set[int]) -> StatusError:
    batches = ','.join(map(str, sorted(removed))) or '<逐项核验移除批次>'
    return StatusError(
        f'完整批次缓存减少 {sorted(removed)}，保留上一统计，不能按缺失输入缩小缺口。'
        '请先恢复完整缓存（重新枚举须使用默认 --verify-every 1），再 --refresh；'
        '若确需主动调整来源基线，核验批次及依据后使用 '
        f'--refresh --rebase-cache --expect-generation {previous["generation"]} '
        f'--removed-batches {batches} --rebase-reason "说明调整依据"；原历史记录将保留。')


def render_report(snapshot: dict) -> str:
    summary = snapshot['summary']
    lines = ['# 公告收录统计记录', '', f"生成时间：{snapshot['generated_at']}；触发原因：{snapshot['reason']}；"
             f"采集轮次：{snapshot.get('run_id') or '—'}。", '',
             '统计来自上游业务库与已保存的官方批次缓存；产品 ID、目录型号和网站占位分别统计。',
             '能源未确认的第 408/409 批整车属于待判定候选，未并入新能源或燃油板块。', '',
             '| 分组 | 缓存内产品 ID | 已下载并解析 | 未下载 | 源站非 PDF |',
             '| --- | ---: | ---: | ---: | ---: |']
    for key, item in summary.get('in_scope_totals', {}).items():
        lines.append(f"| {item.get('label', key)} | {item.get('total', 0):,} | {item.get('parsed', 0):,} | "
                     f"{item.get('missing', 0):,} | {item.get('non_pdf', 0):,} |")
    cache_warnings, notices, _ = cache_messages(summary.get('inputs', {}).get('batch_caches', {}))
    if notices or cache_warnings:
        lines += ['', '## 来源说明与缓存诊断', '']
        lines += ['- 说明：' + message for message in notices]
        lines += ['- 警告：' + message for message in cache_warnings]
    rebase = snapshot.get('cache_rebase')
    if rebase:
        old, new = rebase['old_scope'], rebase['new_scope']
        lines += ['', '## 来源基线变化', '', f"调整依据：{rebase['reason']}",
                  f"旧范围：第 {old['batch_range'][0]}–{old['batch_range'][1]} 批，"
                  f"共 {old['complete_files']} 个完整批次；新范围："
                  f"第 {new['batch_range'][0]}–{new['batch_range'][1]} 批，共 {new['complete_files']} 个完整批次。",
                  f"明确移除批次：{', '.join(map(str, rebase['removed_batches']))}；"
                  f"新增批次：{', '.join(map(str, rebase['added_batches'])) or '无'}。",
                  '以下计数差额包含来源范围变化，不代表下载增减；旧记录和完整批次集合仍保留在历史中。']
    delta_label = '相对上一记录（含来源范围变化）' if rebase else '相对上一记录'
    lines += ['', '## 已保存的统计与变化', '', f'| 指标 | 当前值 | {delta_label} |', '| --- | ---: | ---: |']
    totals = _totals(summary)
    labels = {'business_announcements': '业务库公告数', 'parsed': '有效文件且已解析',
              'missing_products': '缓存范围内未下载产品 ID', 'non_pdf': '源站返回非 PDF',
              'pdf_unparsed': '有效 PDF 待解析', 'pdf_invalid_or_no_document': '文件异常或文档缺失',
              'download_failed': '下载失败', 'refresh_candidates': '已有 PDF 但缓存批次较新'}
    for key, value in totals.items():
        delta = snapshot.get('delta', {}).get(key)
        lines.append(f"| {labels[key]} | {value:,} | {'首次记录' if delta is None else f'{delta:+,}'} |")
    lines += ['', '计数变化可能来自采集结果、目录/批次输入或规则变化；不能把变化量直接视为本轮下载量。',
              f"变化输入：{', '.join(snapshot.get('changed_inputs', [])) or '首次记录/手动核验'}。", '',
              '## 口径与使用', '',
              '- 日常使用 `main.py gonggao status` 读取记录，不运行全库 SQL、不解析批次缓存、不遍历 PDF。',
              '- 采集批次提交后自动更新；`--refresh` 用于首次建立、输入变化后补记或人工复核。',
              '- 每次生成保留 summary/candidates/本报告；`--history` 查看历次记录。',
              '- 文件状态只包含本次路径、大小、PDF 文件头及解析字段检查，不含 SHA-256 全库复验。',
              '- 完整但过期的批次缓存保留基线并标记过期；部分缓存不能用于断言官方无源。',
              '- 业务库/目录库/WAL、批次文件或统计规则变化时，查询显示过期原因；不自动重算。',
              '- 单独更改 PDF 文件而未改业务记录时，请显式 `--refresh`；查询不会重新遍历文件。',
              '- 网站发布状态需要按实际站点库单独对账；本记录不把已采集等同于已上线。', '']
    return '\n'.join(lines)


def refresh_status(db_path: Path = ROOT / 'data/announcement_site.sqlite', *, catalog_db: Path | None = None,
                   pdf_root: Path | None = None, cache_dir: Path | None = None, out_dir: Path | None = None,
                   run_id: int | None = None, reason: str = 'manual', force: bool = False,
                   rebase_cache: bool = False, expect_generation: str | None = None,
                   removed_batches: list[int] | None = None, rebase_reason: str | None = None) -> dict:
    _validate_rebase_request(force=force, rebase_cache=rebase_cache, expect_generation=expect_generation,
                             removed_batches=removed_batches, rebase_reason=rebase_reason)
    db, catalog, cache, root, output = _paths(db_path, catalog_db, pdf_root, cache_dir, out_dir)
    if not db.is_file() or not catalog.is_file():
        raise StatusError('公告业务库或目录库不存在，未生成统计')
    # 防止错误参数覆盖输入；快照目录只能位于独立的输出树。
    for source in (db, catalog, cache, root / 'downloads/announcement_site'):
        if output == source or output.is_relative_to(source) or source.is_relative_to(output):
            raise StatusError(f'统计输出目录与输入重叠：{source}')
    with _lock(output):
        try:
            try:
                previous = _snapshot(output)
            except StatusError:
                if not force or rebase_cache:
                    raise
                # 显式重建保留损坏记录原文，默认查看/自动刷新不会静默覆盖它。
                _atomic_text(output / ('invalid-latest-' + uuid.uuid4().hex + '.json'),
                             (output / 'latest.json').read_text(encoding='utf-8'))
                previous = None
            old_scope = None
            if rebase_cache:
                if previous is None or previous['generation'] != expect_generation:
                    raise StatusError('旧记录代号与 --expect-generation 不符；请重新读取当前记录，核验后重试')
                old_scope = _verified_cache_scope(previous['summary'].get('inputs', {}).get('batch_caches', {}),
                                                   '旧记录')
            before = input_signature(db, catalog, cache, root)
            if not force and previous and previous.get('input_signature') == before:
                return {'status': 'unchanged', 'snapshot': previous}
            # 延迟导入：默认查询不加载统计/解析引擎。
            from miit_gonggao.collection_inventory import compute_inventory
            summary, candidates = compute_inventory(db, catalog, cache, root)
            after = input_signature(db, catalog, cache, root)
            if before != after:
                raise StatusError('统计期间输入发生变化，已保留上一份记录；待写入结束后重试')
            if summary.get('inputs', {}).get('business_db', {}).get('unfinished_runs'):
                raise StatusError('仍有未完成采集轮次，未将中间结果发布为最新统计')
            if not summary.get('inputs', {}).get('batch_caches', {}).get('complete_files'):
                raise StatusError('没有完整批次缓存，保留上一份统计，不发布零缺口')
            cache_rebase = None
            if previous:
                old_cache = previous['summary'].get('inputs', {}).get('batch_caches', {})
                new_cache = summary.get('inputs', {}).get('batch_caches', {})
                if rebase_cache:
                    new_scope = _verified_cache_scope(new_cache, '新统计')
                    old_batches = set(old_scope['complete_batches'])
                    new_batches = set(new_scope['complete_batches'])
                else:
                    old_batches = set(old_cache.get('complete_batches', []))
                    new_batches = set(new_cache.get('complete_batches', []))
                removed = old_batches - new_batches
                if rebase_cache:
                    if not removed or removed != set(removed_batches):
                        raise StatusError(f'实际减少批次 {sorted(removed)} 与 --removed-batches '
                                          f'{sorted(removed_batches)} 不符；须逐项核验，不能遗漏或多报')
                    cache_rebase = {'reason': rebase_reason.strip(), 'expected_generation': expect_generation,
                                    'removed_batches': sorted(removed), 'added_batches': sorted(new_batches - old_batches),
                                    'old_scope': old_scope, 'new_scope': new_scope}
                elif removed or new_cache.get('complete_files', 0) < old_cache.get('complete_files', 0):
                    raise _cache_reduction_error(previous, removed)
            now = _now()
            generation = now.strftime('%Y%m%dT%H%M%S%fZ-') + uuid.uuid4().hex[:12]
            previous_totals = _totals(previous['summary']) if previous else {}
            snapshot = {'format_version': FORMAT_VERSION, 'generation': generation,
                        'previous_generation': previous['generation'] if previous else None,
                        'generated_at': now.astimezone(TZ).isoformat(timespec='seconds'),
                        'reason': reason, 'run_id': run_id, 'input_signature': before, 'summary': summary,
                        'changed_inputs': [key for key, value in before.items()
                                           if previous and value != previous.get('input_signature', {}).get(key)],
                        'delta': {key: value - previous_totals[key] for key, value in _totals(summary).items()
                                  if key in previous_totals}}
            if cache_rebase:
                snapshot['cache_rebase'] = cache_rebase
                snapshot['delta_note'] = '来源基线变化：计数差额包含范围变化，不代表下载增减'
            folder = output / 'snapshots' / generation
            folder.mkdir(parents=True)
            _atomic_json(folder / 'summary.json', snapshot)
            _atomic_json(folder / 'candidates.json', candidates)
            _atomic_text(folder / 'report.md', render_report(snapshot))
            # 多文件完成后才发布入口；异常或中断不会暴露半份汇总。
            _atomic_json(output / 'latest.json', snapshot)
            (output / 'last-error.json').unlink(missing_ok=True)
            return {'status': 'updated', 'snapshot': snapshot}
        except Exception as exc:
            _atomic_json(output / 'last-error.json', {'failed_at': _now().isoformat(),
                         'reason': reason, 'run_id': run_id, 'error': f'{type(exc).__name__}: {exc}'})
            raise


def refresh_after_collection(db_path: Path, *, catalog_db: Path | None = None, pdf_root: Path | None = None,
                             run_id: int | None = None, reason: str = 'collection') -> dict | None:
    """在业务事务提交后调用；统计失败不能修改或掩盖已提交的采集结局。"""
    try:
        result = refresh_status(db_path, catalog_db=catalog_db, pdf_root=pdf_root,
                                run_id=run_id, reason=reason)
    except Exception as exc:
        print(f'收录统计更新失败，保留上一记录：{type(exc).__name__}: {exc}', file=sys.stderr)
        return None
    print(f"收录统计：{result['status']}，记录 {result['snapshot']['generation']}", file=sys.stderr)
    return result


def history(out_dir: Path, limit: int = 10) -> list[dict]:
    snapshot = _snapshot(out_dir)
    result = []
    while snapshot and len(result) < limit:
        result.append({key: snapshot.get(key) for key in
                       ('generation', 'generated_at', 'reason', 'run_id', 'delta', 'cache_rebase', 'delta_note')})
        generation = snapshot.get('previous_generation')
        if not generation:
            break
        if not GENERATION.fullmatch(generation) or generation in {r['generation'] for r in result}:
            raise StatusError('历史记录链无效')
        snapshot = _read_json(out_dir / 'snapshots' / generation / 'summary.json')
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'data/announcement_site.sqlite')
    parser.add_argument('--catalog-db', type=Path)
    parser.add_argument('--pdf-root', type=Path)
    parser.add_argument('--cache-dir', type=Path)
    parser.add_argument('--out-dir', type=Path)
    parser.add_argument('--refresh', action='store_true', help='显式重新生成统计；默认仅查看已保存的记录')
    parser.add_argument('--rebase-cache', action='store_true', help='与 --refresh 一起显式调整完整批次来源基线，保留历史')
    parser.add_argument('--expect-generation', help='须精确匹配当前统计记录代号，防止覆盖未核验的新记录')
    parser.add_argument('--removed-batches', type=_parse_removed_batches, help='明确移除的完整批次，逗号分隔且不得遗漏或多报')
    parser.add_argument('--rebase-reason', help='主动调整来源基线的具体依据，不能为空')
    parser.add_argument('--json', action='store_true', help='输出机器可读结果')
    parser.add_argument('--history', action='store_true', help='只读最近十次统计变化')
    args = parser.parse_args(argv)
    if args.refresh and args.history:
        parser.error('--history 与 --refresh 不能同时使用')
    if not args.refresh and (args.rebase_cache or args.expect_generation is not None
                            or args.removed_batches is not None or args.rebase_reason is not None):
        parser.error('基线调整参数必须与 --refresh 一起使用')
    options = dict(catalog_db=args.catalog_db, pdf_root=args.pdf_root,
                   cache_dir=args.cache_dir, out_dir=args.out_dir)
    try:
        if args.history:
            output = _paths(args.db, **options)[-1]
            print(json.dumps(history(output), ensure_ascii=False, indent=2))
            return 0
        if args.refresh:
            refresh_status(args.db, **options, force=True, rebase_cache=args.rebase_cache,
                           expect_generation=args.expect_generation, removed_batches=args.removed_batches,
                           rebase_reason=args.rebase_reason)
        result = read_status(args.db, **options)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"统计记录状态：{result['status']}")
            for message in result.get('stale_reasons', []) + result.get('warnings', []):
                print(f'提示：{message}')
            snapshot = result['snapshot']
            if snapshot:
                print(render_report(snapshot))
                output = _paths(args.db, **options)[-1]
                print(f"完整记录：{output / 'snapshots' / snapshot['generation'] / 'report.md'}")
        return 2 if result['status'] == 'missing' else 0
    except (StatusError, OSError, ValueError, sqlite3.Error) as exc:
        print(f'收录统计：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
