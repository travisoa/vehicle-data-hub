import hashlib
import json
import sqlite3

import pytest

from miit_gonggao.detail_cache import find_detail, remember_detail


@pytest.fixture
def sample(tmp_path):
    row = {'cpid': 'A123', 'pc': '400', 'clxh': 'ABC123', 'dataTag': 'Z'}
    content = ('<table><tr><td>产品ID</td><td>A123</td></tr>'
               '<tr><td>批次</td><td>400</td></tr>'
               '<tr><td>车辆型号</td><td>ABC123</td></tr></table>').encode()
    digest = hashlib.sha256(content).hexdigest()
    html = tmp_path / 'detail.html'
    html.write_bytes(content)
    blob = tmp_path / 'http_blobs' / digest
    blob.parent.mkdir()
    blob.write_bytes(content)
    log = tmp_path / 'http_evidence.jsonl'
    events = [{'type': 'detail_started', 'operation_id': 'op', 'product_id': 'A123',
               'batch': '400', 'model_code': 'ABC123'},
              {'type': 'detail_http_attempt', 'operation_id': 'op', 'response_status': 200,
               'response_sha256': digest, 'response_blob': 'http_blobs/' + digest},
              {'type': 'detail_finished', 'operation_id': 'op', 'bytes': len(content)}]
    log.write_text(''.join(json.dumps(e) + '\n' for e in events))
    return row, html, log, blob


def test_verified_detail_reuse_checks_identity_and_evidence(tmp_path, sample):
    row, html, log, blob = sample
    with sqlite3.connect(tmp_path / 'test.sqlite') as conn:
        remember_detail(conn, row, html, log, 'op', root=tmp_path)
        remember_detail(conn, row, html, log, 'op', root=tmp_path)
        assert conn.execute('SELECT count(*) FROM verified_detail_cache').fetchone()[0] == 1
        assert find_detail(conn, row, root=tmp_path) == html.resolve()
        assert find_detail(conn, {**row, 'pc': '401'}, root=tmp_path) is None
        blob.write_bytes(b'altered')
        assert find_detail(conn, row, root=tmp_path) is None


def test_mismatched_or_verification_page_is_not_cached(tmp_path, sample):
    row, html, log, _ = sample
    with sqlite3.connect(tmp_path / 'test.sqlite') as conn:
        with pytest.raises(ValueError):
            remember_detail(conn, {**row, 'clxh': 'OTHER'}, html, log, 'op', root=tmp_path)
        html.write_text('<title>captcha</title>')
        with pytest.raises(ValueError):
            remember_detail(conn, row, html, log, 'op', root=tmp_path)


def test_unfinished_or_failed_operation_is_never_reused(tmp_path, sample):
    row, html, log, _ = sample
    events = [json.loads(line) for line in log.read_text().splitlines()]
    with sqlite3.connect(tmp_path / 'test.sqlite') as conn:
        log.write_text(''.join(json.dumps(e) + '\n' for e in events[:-1]))
        with pytest.raises(ValueError):
            remember_detail(conn, row, html, log, 'op', root=tmp_path)
        events.append({'type': 'detail_failed', 'operation_id': 'op'})
        log.write_text(''.join(json.dumps(e) + '\n' for e in events))
        with pytest.raises(ValueError):
            remember_detail(conn, row, html, log, 'op', root=tmp_path)


@pytest.mark.parametrize('page', [b'<title>CAPTCHA required</title>',
                                  '<title>请输入验证码</title>'.encode(),
                                  b'<form><input name="verifyCode"></form>'])
def test_access_verification_page_is_detected(page):
    from miit_gonggao.images import is_access_verification_page

    assert is_access_verification_page(page)
