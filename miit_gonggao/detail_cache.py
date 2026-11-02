"""Append-only verified detail evidence for future detail-only collection."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from miit_gonggao.images import validate_detail_identity


def ensure_detail_cache(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS verified_detail_cache (
        id INTEGER PRIMARY KEY, product_id TEXT NOT NULL, batch TEXT NOT NULL,
        model_code TEXT NOT NULL, data_tag TEXT NOT NULL, html_path TEXT NOT NULL,
        html_sha256 TEXT NOT NULL, evidence_path TEXT NOT NULL, operation_id TEXT NOT NULL,
        UNIQUE(product_id,batch,model_code,data_tag,html_sha256,operation_id)
    )""")


def _identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (str(row.get('cpid') or row.get('gid') or ''), str(row.get('gppc') or row.get('pc') or ''),
            str(row.get('clxh') or ''), str(row.get('dataTag') or 'Z').upper())


def _verify(row: dict[str, Any], html_path: Path, evidence_path: Path, operation_id: str, digest: str) -> None:
    content = html_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError('详情 HTML 哈希不符')
    validate_detail_identity(content, row)
    events = [json.loads(line) for line in evidence_path.read_text(encoding='utf-8').splitlines()]
    events = [e for e in events if e.get('operation_id') == operation_id]
    product_id, batch, model, _ = _identity(row)
    started = [e for e in events if e.get('type') == 'detail_started' and
               (str(e.get('product_id')), str(e.get('batch')), e.get('model_code')) == (product_id, batch, model)]
    http = [e for e in events if e.get('type') == 'detail_http_attempt' and
            e.get('response_status') == 200 and e.get('response_sha256') == digest]
    finished = [e for e in events if e.get('type') == 'detail_finished'
                and e.get('bytes') == len(content)]
    failed = [e for e in events if e.get('type') == 'detail_failed']
    if (len(started) != 1 or not http or len(finished) != 1 or failed
            or events[0] is not started[0] or events[-1] is not finished[0]
            or any(e.get('response_bytes', len(content)) != len(content) for e in http)):
        raise ValueError('缺少匹配的详情请求响应证据')
    blob = (evidence_path.parent / http[-1]['response_blob']).resolve()
    if not blob.is_relative_to(evidence_path.parent.resolve()) or hashlib.sha256(blob.read_bytes()).hexdigest() != digest:
        raise ValueError('详情响应证据哈希或路径不符')


def remember_detail(conn: sqlite3.Connection, row: dict[str, Any], html_path: Path,
                    evidence_path: Path, operation_id: str, *, root: Path) -> None:
    digest = hashlib.sha256(html_path.read_bytes()).hexdigest()
    _verify(row, html_path, evidence_path, operation_id, digest)
    root = root.resolve()
    html_relative = html_path.resolve().relative_to(root)
    evidence_relative = evidence_path.resolve().relative_to(root)
    ensure_detail_cache(conn)
    conn.execute('INSERT OR IGNORE INTO verified_detail_cache '
                 '(product_id,batch,model_code,data_tag,html_path,html_sha256,evidence_path,operation_id) '
                 'VALUES (?,?,?,?,?,?,?,?)', (*_identity(row), html_relative.as_posix(), digest,
                                              evidence_relative.as_posix(), operation_id))


def find_detail(conn: sqlite3.Connection, row: dict[str, Any], *, root: Path) -> Path | None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='verified_detail_cache'").fetchone():
        return None
    records = conn.execute('SELECT html_path,html_sha256,evidence_path,operation_id FROM verified_detail_cache '
                           'WHERE product_id=? AND batch=? AND model_code=? AND data_tag=? ORDER BY id DESC',
                           _identity(row)).fetchall()
    for html, digest, evidence, operation in records:
        try:
            root = root.resolve()
            html_file, evidence_file = (root / html).resolve(), (root / evidence).resolve()
            if not html_file.is_relative_to(root) or not evidence_file.is_relative_to(root):
                continue
            _verify(row, html_file, evidence_file, operation, digest)
            return html_file
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return None
