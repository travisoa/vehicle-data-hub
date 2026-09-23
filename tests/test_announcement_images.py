"""原图链路的会话、来源、错误和发布回归；全部请求使用离线替身。"""

import email.message
import io
import json
import sqlite3
import stat

import pytest
from PIL import Image

from miit_gonggao import collection, core, images

ROW = {"cpid": "AB123", "gppc": "408", "clxh": "ABC6500EV", "cpsb": "示例牌", "dataTag": "Z"}


def detail(*names):
    tags = "".join(f'<img src="getPic?gid=AB123&amp;pc=408&amp;zpname={name}">' for name in names)
    return (f'<title>公告产品主要技术参数</title><table><tr><td>{tags}</td></tr>'
            '<tr><td>产品ID</td><td>AB123</td><td>批次</td><td>408</td></tr>'
            '<tr><td>车辆型号</td><td>ABC6500EV</td></tr></table>').encode()


def jpeg(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (24, 16), color).save(stream, format="JPEG")
    return stream.getvalue()


def test_sparse_duplicate_links_and_identity():
    links = images.parse_image_links(detail("ab1230", "ab1234", "ab1234"), ROW)
    assert [item["name"] for item in links] == ["ab1230", "ab1234"]
    with pytest.raises(ValueError, match="产品 ID/批次"):
        images.parse_image_links(b"<title>captcha</title>", ROW)
    with pytest.raises(ValueError, match="产品 ID/批次"):
        images.parse_image_links(detail("a"), {**ROW, "cpid": "other"})
    with pytest.raises(ValueError, match="车辆型号"):
        images.parse_image_links(detail("a"), {**ROW, "clxh": "other"})


@pytest.mark.parametrize("source", [
    "getPic?gid=OTHER&pc=408&zpname=a",
    "getPic?gid=AB123&pc=407&zpname=a",
    "getPic?gid=AB123&pc=408&zpname=a&zpname=b",
])
def test_rejects_photo_links_for_another_product(source):
    html = detail("a").replace(b"getPic?gid=AB123&amp;pc=408&amp;zpname=a", source.encode())
    with pytest.raises(ValueError, match="照片链接的产品身份"):
        images.parse_image_links(html, ROW)


def test_skips_page_images_that_are_not_announcement_photos():
    """标志、装饰图和外站图片不是公告照片，略过即可，不能让整页照片清单失效。"""
    extra = (b'<img src="/miitxxgk/images/logo.png"><img src="https://other.example/getPic?gid=AB123'
             b'&amp;pc=408&amp;zpname=x"></td>')
    html = detail("a", "b").replace(b"</td>", extra, 1)
    assert [item["name"] for item in images.parse_image_links(html, ROW)] == ["a", "b"]


def test_every_set_cookie_reaches_the_image_requests(tmp_path, monkeypatch):
    headers = email.message.Message()
    headers["Set-Cookie"] = "JSESSIONID=first; Path=/miitxxgk; HttpOnly"
    headers["Set-Cookie"] = "route=second; Expires=Wed, 21 Oct 2026 07:28:00 GMT; Path=/"
    headers["Content-Type"] = "text/html"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        headers = email.message.Message()

        def read(self):
            return b""

    response = Response()
    response.headers = headers
    monkeypatch.setattr(core, "_throttle", lambda: None)
    monkeypatch.setattr(core.request, "urlopen", lambda *a, **k: response)
    _body, parsed = core.http_request("https://example.invalid/detail")
    assert parsed["set-cookie"].splitlines() == [
        "JSESSIONID=first; Path=/miitxxgk; HttpOnly",
        "route=second; Expires=Wed, 21 Oct 2026 07:28:00 GMT; Path=/"]

    monkeypatch.setattr(core, "post_form", lambda *a, **k: (detail("a"), parsed))
    sent = []
    monkeypatch.setattr(core, "http_request", lambda url, **k: sent.append(k["headers"]["Cookie"]) or (jpeg(), {}))
    assert images.download_product_images(ROW, tmp_path)["status"] == "ok"
    assert sent == ["JSESSIONID=first; route=second"]


def test_image_files_and_manifests_are_readable_like_pdfs(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "post_form", lambda *a, **k: (detail("a"), {}))
    monkeypatch.setattr(core, "http_request", lambda *a, **k: (jpeg(), {}))
    images.download_product_images(ROW, tmp_path)
    files = list(images.image_folder(ROW, tmp_path).iterdir())
    assert files and {stat.S_IMODE(path.stat().st_mode) for path in files} == {0o644}


def test_retry_only_follows_a_failed_previous_attempt(tmp_path, monkeypatch):
    attempts = []
    monkeypatch.setattr(images, "download_product_images", lambda row, folder: attempts.append(folder) or {"ok": 1})
    assert images.retry_failed_images(ROW, tmp_path) is None  # 启用图片前的历史产品不补采
    folder = images.image_folder(ROW, tmp_path)
    images.write_result({**images.new_result(ROW, tmp_path), "status": "ok"}, folder)
    assert images.retry_failed_images(ROW, tmp_path) is None
    images.write_result({**images.new_result(ROW, tmp_path), "failed": 1}, folder)
    assert images.retry_failed_images(ROW, tmp_path) == {"ok": 1} and attempts == [tmp_path]
    with core.image_downloads(False):
        assert images.retry_failed_images(ROW, tmp_path) is None
    assert attempts == [tmp_path]


def test_failed_redownload_keeps_the_current_manifest(tmp_path):
    folder = images.image_folder(ROW, tmp_path)
    images.write_result({**images.new_result(ROW, tmp_path), "status": "ok"}, folder)
    staging = tmp_path / "staging"
    images.write_result({**images.new_result(ROW, staging), "failed": 1}, images.image_folder(ROW, staging))
    images.publish_images(ROW, staging, tmp_path, replace_current=False)
    assert images.read_image_result(ROW, tmp_path)["status"] == "ok"
    assert len(list(folder.glob("manifest_*.json"))) == 2  # 失败那次仍留历史
    images.publish_images(ROW, staging, tmp_path)
    assert images.read_image_result(ROW, tmp_path)["failed"] == 1


def test_download_reuses_detail_session_and_validates_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "post_form", lambda *a, **k: (
        detail("ab1230", "ab1234"), {"set-cookie": "JSESSIONID=test-session; Path=/miitxxgk; HttpOnly"}))
    requests = []

    def get(url, **kwargs):
        requests.append((url, kwargs))
        assert kwargs["headers"]["Cookie"] == "JSESSIONID=test-session"
        assert kwargs["headers"]["Referer"] == core.DETAIL_URL
        if url.endswith("ab1234"):
            return b"<html>not an image</html>", {"content-type": "image/jpeg"}
        return jpeg(), {"content-type": "application/octet-stream"}

    monkeypatch.setattr(core, "http_request", get)
    result = images.download_product_images(ROW, tmp_path)
    assert result["status"] == "partial"
    assert (result["downloaded"], result["failed"]) == (1, 1)
    folder = images.image_folder(ROW, tmp_path)
    assert len(list(folder.glob("*.jpg"))) == 1
    assert (folder / result["images"][0]["file"]).read_bytes() == jpeg()
    assert result["images"][0]["width"] == 24
    assert "test-session" not in (folder / "manifest.json").read_text()
    assert len(requests) == 2


def test_verification_stops_remaining_image_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "post_form", lambda *a, **k: (detail("a", "b", "c"), {}))
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return "<title>访问行为验证</title>".encode(), {}

    monkeypatch.setattr(core, "http_request", get)
    result = images.download_product_images(ROW, tmp_path)
    assert len(calls) == 1
    assert result["failed"] == 3
    assert {item["status"] for item in result["images"]} == {"access_verification"}
    assert not list(images.image_folder(ROW, tmp_path).glob("*.jpg"))


def test_no_images_is_distinct_from_invalid_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "post_form", lambda *a, **k: (detail(), {}))
    assert images.download_product_images(ROW, tmp_path)["status"] == "no_images"
    monkeypatch.setattr(core, "post_form", lambda *a, **k: (b"<title>error</title>", {}))
    result = images.download_product_images(ROW, tmp_path)
    assert result["status"] == "failed" and result["failed"] == 1


def test_repeat_preserves_previous_images_and_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "post_form", lambda *a, **k: (detail("a"), {}))
    monkeypatch.setattr(core, "http_request", lambda *a, **k: (jpeg(), {}))
    first = images.download_product_images(ROW, tmp_path)
    monkeypatch.setattr(core, "http_request", lambda *a, **k: (jpeg("blue"), {}))
    second = images.download_product_images(ROW, tmp_path)
    monkeypatch.setattr(core, "http_request", lambda *a, **k: (b"broken", {}))
    images.download_product_images(ROW, tmp_path)
    folder = images.image_folder(ROW, tmp_path)
    assert first["images"][0]["file"] != second["images"][0]["file"]
    assert len(list(folder.glob("*.jpg"))) == 2
    assert len(list(folder.glob("manifest_*.json"))) == 3


def test_pdf_download_defaults_to_images_and_keeps_pdf_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "post_form", lambda *a, **k: (b"%PDF-1.4 test", {}))

    def fail(row, folder):
        raise OSError("disk full")

    monkeypatch.setattr(images, "download_product_images", fail)
    result = core.download_param_page(ROW, tmp_path)
    path, is_pdf, byte_count = result
    assert is_pdf and path.read_bytes().startswith(b"%PDF")
    assert byte_count == path.stat().st_size
    assert result.images["failed"] == 1
    assert "disk full" in result.images["error"]


def test_collection_publishes_images_and_keeps_pdf_status(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    collection.ensure_schema(conn)
    vehicle_id = collection.upsert_vehicle(conn, {
        "common_name": "demo", "model_code": ROW["clxh"], "catalog": "catalog",
        "batch": "32", "category": "乘用车", "seq": "1", "company": "demo"})
    monkeypatch.setattr(collection, "parse_pdf", lambda *a: ({"model_code": ROW["clxh"]}, ""))
    monkeypatch.setattr(core, "post_form", lambda url, *a, **k: (
        b"%PDF-1.4 test" if url == core.PDF_URL else detail("a", "b"), {}))
    monkeypatch.setattr(core, "http_request", lambda url, **k: (jpeg() if url.endswith("=a") else b"broken", {}))
    outcome = collection.store_announcement(
        conn, row=ROW, vehicle_id=vehicle_id, market_name="demo", download_root=tmp_path / "downloads",
        pdf_root=tmp_path, catalog_db=tmp_path / "catalog.sqlite")
    ok, message = outcome
    assert ok and outcome.image_failed and "图片下载不完整" in message
    pdf_path = tmp_path / conn.execute("SELECT relative_path FROM documents").fetchone()[0]
    assert pdf_path.exists()
    folder = images.image_folder(ROW, pdf_path.parent)
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["failed"] == 1
    assert (folder / manifest["images"][0]["file"]).exists()
    assert not list((tmp_path / "downloads" / "_snapshots").glob("ingest-*"))
    from miit_gonggao.collection_manifest import local_document

    assert local_document(conn, ROW["cpid"], tmp_path)[0] == "image_failed"
    from miit_gonggao import collection_tracking as tracking

    tracking.ensure_schema(conn)
    monkeypatch.setattr(collection, "refresh_collection_status", lambda *a, **k: None)
    event_id = tracking.register_event(conn, model_code=ROW["clxh"], batch=408, kind="formal",
        product_id=ROW["cpid"], source_url="https://www.miit.gov.cn/example.html")
    conn.commit()
    result = tracking.collect(conn, {"pending": [], "resolved": [event_id]},
                              catalog_db=tmp_path / "catalog.sqlite", pdf_root=tmp_path)
    assert result["image_failures"] == result["errors"] == 1
    assert conn.execute("SELECT status FROM tracking_events").fetchone()[0] == "partial"
    # 已有有效 PDF：下一轮只重取图片，不重下 PDF；取全后事件完成
    monkeypatch.setattr(core, "post_form", lambda url, *a, **k: (
        pytest.fail("不应重下 PDF") if url == core.PDF_URL else detail("a", "b"), {}))
    monkeypatch.setattr(core, "http_request", lambda url, **k: (jpeg(), {}))
    result = tracking.collect(conn, {"pending": [], "resolved": [event_id]},
                              catalog_db=tmp_path / "catalog.sqlite", pdf_root=tmp_path)
    assert result["errors"] == 0 and "image_failures" not in result
    assert conn.execute("SELECT status FROM tracking_events").fetchone()[0] == "done"
    assert local_document(conn, ROW["cpid"], tmp_path)[0] == "skipped_existing"
    conn.close()


def test_image_publish_failure_is_recorded_for_retry_and_staging_is_removed(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    collection.ensure_schema(conn)
    vehicle_id = collection.upsert_vehicle(conn, {
        "common_name": "demo", "model_code": ROW["clxh"], "catalog": "catalog",
        "batch": "32", "category": "乘用车", "seq": "1", "company": "demo"})
    monkeypatch.setattr(collection, "parse_pdf", lambda *a: ({"model_code": ROW["clxh"]}, ""))
    monkeypatch.setattr(core, "post_form", lambda url, *a, **k: (
        b"%PDF-1.4 test" if url == core.PDF_URL else detail("a"), {}))
    monkeypatch.setattr(core, "http_request", lambda url, **k: (jpeg(), {}))

    def refuse(*args, **kwargs):
        raise RuntimeError("图片目标已存在且内容不同")

    monkeypatch.setattr(images, "publish_images", refuse)
    outcome = collection.store_announcement(
        conn, row=ROW, vehicle_id=vehicle_id, market_name="demo", download_root=tmp_path / "downloads",
        pdf_root=tmp_path, catalog_db=tmp_path / "catalog.sqlite")
    assert outcome[0] and outcome.image_failed and "暂存目录" not in outcome[1]
    pdf_path = tmp_path / conn.execute("SELECT relative_path FROM documents").fetchone()[0]
    assert images.read_image_result(ROW, pdf_path.parent)["failed"] == 1
    assert not list((tmp_path / "downloads" / "_snapshots").glob("ingest-*"))
    conn.close()


def test_query_images_only_and_default_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "query_all_pages", lambda **k: [ROW])
    monkeypatch.setattr(core, "post_form", lambda *a, **k: (detail("a"), {}))
    monkeypatch.setattr(core, "http_request", lambda *a, **k: (jpeg(), {}))
    argv = ["query", "--model-code", ROW["clxh"], "--output-dir", str(tmp_path), "--images-only"]
    # 目标目录没有该产品 PDF：图片会与 PDF 分离，拒绝补图
    assert core.main(argv) == core.EXIT_DOWNLOAD_FAILED
    assert not list(tmp_path.rglob("images"))
    monkeypatch.setattr(core, "post_form", lambda *a, **k: (b"%PDF-1.4 test", {}))
    assert core.main([*argv[:-1], "--download"]) == core.EXIT_DOWNLOAD_PARTIAL
    pdf, = tmp_path.rglob("*.pdf")
    monkeypatch.setattr(core, "post_form", lambda *a, **k: (detail("a"), {}))
    assert core.main(argv) == core.EXIT_OK
    assert images.read_image_result(ROW, pdf.parent)["status"] == "ok"


def test_query_download_can_skip_images(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "query_all_pages", lambda **k: [ROW])
    monkeypatch.setattr(core, "post_form", lambda url, *a, **k: (
        b"%PDF-1.4 test" if url == core.PDF_URL else pytest.fail("--no-images 不应请求详情页"), {}))
    argv = ["query", "--model-code", ROW["clxh"], "--output-dir", str(tmp_path), "--download", "--no-images"]
    assert core.main(argv) == core.EXIT_OK and core.DOWNLOAD_IMAGES
    assert list(tmp_path.rglob("*.pdf")) and not list(tmp_path.rglob("images"))
    assert core.main([*argv[:-2], "--images-only", "--no-images"]) == core.EXIT_DOWNLOAD_FAILED


def test_images_only_exit_code_counts_products_verified_without_photos(tmp_path, monkeypatch):
    other = {**ROW, "cpid": "AB124", "clxh": "ABC6500EVB"}
    monkeypatch.setattr(core, "query_all_pages", lambda **k: [ROW, other])
    for row in (ROW, other):
        folder = core.build_announcement_download_dir(tmp_path, trademark=row["cpsb"],
                                                      vehicle_folder="demo", batch=row["gppc"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{core.param_page_stem(row)}.pdf").write_bytes(b"%PDF-1.4 test")

    def page(url, data, **kwargs):
        # ROW 的详情页核验无图；other 的详情页返回访问验证页
        return (detail() if data["gid"] == ROW["cpid"] else b"<title>captcha</title>"), {}

    monkeypatch.setattr(core, "post_form", page)
    argv = ["query", "--trademark", ROW["cpsb"], "--output-dir", str(tmp_path), "--images-only",
            "--vehicle-folder", "demo"]
    assert core.main(argv) == core.EXIT_DOWNLOAD_PARTIAL
