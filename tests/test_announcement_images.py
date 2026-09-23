"""原图链路的会话、来源、错误和发布回归；全部请求使用离线替身。"""

import io
import json
import sqlite3

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
    "https://other.example/getPic?gid=AB123&pc=408&zpname=a",
    "getPic?gid=OTHER&pc=408&zpname=a",
    "getPic?gid=AB123&pc=407&zpname=a",
    "getPic?gid=AB123&pc=408&zpname=a&zpname=b",
])
def test_rejects_foreign_image_links(source):
    html = detail("a").replace(b"getPic?gid=AB123&amp;pc=408&amp;zpname=a", source.encode())
    with pytest.raises(ValueError, match="图片链接"):
        images.parse_image_links(html, ROW)


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


def test_query_images_only_and_default_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "query_all_pages", lambda **k: [ROW])
    monkeypatch.setattr(core, "post_form", lambda *a, **k: (detail("a"), {}))
    monkeypatch.setattr(core, "http_request", lambda *a, **k: (jpeg(), {}))
    argv = ["query", "--model-code", ROW["clxh"], "--output-dir", str(tmp_path), "--images-only"]
    assert core.main(argv) == 0
    assert not list(tmp_path.rglob("*.pdf"))
    monkeypatch.setattr(core, "post_form", lambda *a, **k: (b"%PDF-1.4 test", {}))
    assert core.main([*argv[:-1], "--download"]) == core.EXIT_DOWNLOAD_PARTIAL
    assert list(tmp_path.rglob("*.pdf"))


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
    ok, message = collection.store_announcement(
        conn, row=ROW, vehicle_id=vehicle_id, market_name="demo", download_root=tmp_path / "downloads",
        pdf_root=tmp_path, catalog_db=tmp_path / "catalog.sqlite")
    assert ok and "图片下载不完整" in message
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
    conn.close()
