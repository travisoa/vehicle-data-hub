"""目录库同步状态测试：旧库迁移、0 行不算成功、失败可记录且不丢数据。"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from miit_gonggao import jianmian


def _article(art_id: str = "10001") -> jianmian.CatalogArticle:
    return jianmian.CatalogArticle(
        art_id=art_id,
        url=f"https://example.com/art_1691_{art_id}.html",
        title="关于减免车辆购置税的新能源汽车车型目录的公告",
        pub_date="2026-06-01",
    )


def test_open_db_migrates_legacy_schema(tmp_path):
    db_path = tmp_path / "legacy.sqlite"
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        "CREATE TABLE articles (art_id TEXT PRIMARY KEY, url TEXT, title TEXT, pub_date TEXT, fetched_at TEXT)"
    )
    legacy.execute("INSERT INTO articles (art_id) VALUES ('legacy1')")
    legacy.commit()
    legacy.close()

    conn = jianmian.open_db(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
    assert {"status", "error", "row_count"} <= columns
    # 旧记录 status 为 NULL，按成功处理（与 command_sync 的 known 查询一致）
    known = {
        row[0]
        for row in conn.execute("SELECT art_id FROM articles WHERE status IS NULL OR status = 'ok'")
    }
    assert "legacy1" in known


def test_store_article_records_status_and_rows(tmp_path):
    conn = jianmian.open_db(tmp_path / "db.sqlite")
    article = _article()
    rows = [{"model_code": "DOC7000BEV01", "catalog": "减免车辆购置税的新能源汽车车型目录", "batch": "28"}]

    count = jianmian.store_article(conn, article, [("a.doc", rows)])
    assert count == 1
    art = conn.execute("SELECT status, error, row_count FROM articles WHERE art_id = ?", (article.art_id,)).fetchone()
    assert art == ("ok", "", 1)

    # 0 行附件 -> zero_rows，不进 known 集合，下次默认重试
    count = jianmian.store_article(conn, article, [], status="zero_rows", error="附件解析0行: a.doc")
    assert count == 0
    art = conn.execute("SELECT status, row_count FROM articles WHERE art_id = ?", (article.art_id,)).fetchone()
    assert art == ("zero_rows", 0)
    known = {
        row[0]
        for row in conn.execute("SELECT art_id FROM articles WHERE status IS NULL OR status = 'ok'")
    }
    assert article.art_id not in known


def test_mark_article_keeps_catalog_rows(tmp_path):
    conn = jianmian.open_db(tmp_path / "db.sqlite")
    article = _article()
    rows = [{"model_code": "DOC7000BEV01"}]
    jianmian.store_article(conn, article, [("a.doc", rows)])

    # 解析异常只更新状态，保留已入库数据
    jianmian.mark_article(conn, article, status="error", error="boom")
    art = conn.execute("SELECT status, error FROM articles WHERE art_id = ?", (article.art_id,)).fetchone()
    assert art == ("error", "boom")
    remaining = conn.execute("SELECT COUNT(*) FROM catalog_rows WHERE art_id = ?", (article.art_id,)).fetchone()[0]
    assert remaining == 1


def test_command_export_uses_fixed_xlsx_path(tmp_path, monkeypatch):
    db_path = tmp_path / "db.sqlite"
    conn = jianmian.open_db(db_path)
    article = _article()
    rows = [
        {"seq": "1", "catalog": "减免车辆购置税的新能源汽车车型目录", "batch": "28", "part": "1", "energy_type": "纯电动", "category": "乘用车", "model_code": "DOC7000BEV01", "company": "测试公司"},
    ]
    jianmian.store_article(conn, article, [("a.doc", rows)])
    conn.close()

    out_dir = tmp_path / "output"
    monkeypatch.setattr(jianmian, "EXPORT_XLSX_PATH", out_dir / "jianmian_catalog.xlsx")
    monkeypatch.setattr(jianmian, "EXPORT_BY_CATEGORY_DIR", out_dir / "jianmian_by_category")

    rc = jianmian.command_export(SimpleNamespace(keyword=None, by_category=False, xlsx=None, db=str(db_path)))
    assert rc == 0
    assert (out_dir / "jianmian_catalog.xlsx").exists()
    assert not list(out_dir.glob("jianmian_catalog_*.xlsx"))


def test_command_export_by_category_uses_fixed_directory(tmp_path, monkeypatch):
    db_path = tmp_path / "db.sqlite"
    conn = jianmian.open_db(db_path)
    article = _article()
    rows = [
        {"seq": "1", "catalog": "减免车辆购置税的新能源汽车车型目录", "batch": "28", "part": "1", "energy_type": "纯电动", "category": "乘用车", "model_code": "DOC7000BEV01", "company": "测试公司"},
        {"seq": "2", "catalog": "减免车辆购置税的新能源汽车车型目录", "batch": "28", "part": "1", "energy_type": "纯电动", "category": "客车", "model_code": "DOC7000BEV02", "company": "测试公司"},
    ]
    jianmian.store_article(conn, article, [("a.doc", rows)])
    conn.close()

    out_dir = tmp_path / "output"
    fixed_dir = out_dir / "jianmian_by_category"
    fixed_dir.mkdir(parents=True)
    (fixed_dir / "jianmian_by_category_legacy.xlsx").write_text("stale")
    monkeypatch.setattr(jianmian, "EXPORT_XLSX_PATH", out_dir / "jianmian_catalog.xlsx")
    monkeypatch.setattr(jianmian, "EXPORT_BY_CATEGORY_DIR", fixed_dir)

    rc = jianmian.command_export(SimpleNamespace(keyword=None, by_category=True, xlsx=None, db=str(db_path)))
    assert rc == 0
    assert (fixed_dir / "乘用车.xlsx").exists()
    assert (fixed_dir / "客车.xlsx").exists()
    assert not (fixed_dir / "jianmian_by_category_legacy.xlsx").exists()
    assert not any(path.is_file() and path.name.startswith("jianmian_by_category_") for path in out_dir.iterdir())


def test_command_export_by_category_keeps_unrelated_xlsx(tmp_path):
    """--xlsx 指向用户已有目录时，只清理本工具的分类文件，其他 Excel 必须原样保留。"""
    db_path = tmp_path / "db.sqlite"
    conn = jianmian.open_db(db_path)
    rows = [
        {"seq": "1", "catalog": "减免车辆购置税的新能源汽车车型目录", "batch": "28", "part": "1", "energy_type": "纯电动", "category": "乘用车", "model_code": "DOC7000BEV01", "company": "测试公司"},
    ]
    jianmian.store_article(conn, _article(), [("a.doc", rows)])
    conn.close()

    user_dir = tmp_path / "我的文档"
    user_dir.mkdir()
    (user_dir / "季度预算.xlsx").write_text("用户自己的文件")
    (user_dir / "货车.xlsx").write_text("上一轮导出的分类文件")

    rc = jianmian.command_export(
        SimpleNamespace(keyword=None, by_category=True, xlsx=str(user_dir), db=str(db_path))
    )
    assert rc == 0
    assert (user_dir / "乘用车.xlsx").exists()
    assert (user_dir / "季度预算.xlsx").read_text() == "用户自己的文件"
    assert not (user_dir / "货车.xlsx").exists()


def test_zero_rows_retry_keeps_existing_rows(tmp_path):
    """已入库的文章重试时若附件解析出 0 行，旧行必须保留而不是被清空。"""
    conn = jianmian.open_db(tmp_path / "db.sqlite")
    article = _article()
    rows = [
        {"seq": "1", "catalog": "减免车辆购置税的新能源汽车车型目录", "batch": "28", "part": "1", "energy_type": "纯电动", "category": "乘用车", "model_code": "DOC7000BEV01", "company": "测试公司"},
    ]
    jianmian.store_article(conn, article, [("a.doc", rows)])

    # 模拟重试中转换器失败：只标状态，不覆盖 catalog_rows
    jianmian.mark_article(conn, article, status="zero_rows", error="附件解析0行: a.doc")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM catalog_rows WHERE art_id = ?", (article.art_id,)
    ).fetchone()[0]
    assert remaining == 1
    assert conn.execute(
        "SELECT status FROM articles WHERE art_id = ?", (article.art_id,)
    ).fetchone()[0] == "zero_rows"
    conn.close()


def test_is_non_catalog_peek():
    assert jianmian.is_non_catalog_peek("") is False
    assert jianmian.is_non_catalog_peek("目录") is True
    assert jianmian.is_non_catalog_peek("新能源汽车推广应用推荐车型目录") is True
    assert jianmian.is_non_catalog_peek("减免车辆购置税的新能源汽车车型目录") is False


def test_looks_like_failed_catalog():
    assert jianmian.looks_like_failed_catalog(catalog="", catalog_hint="") is False
    assert jianmian.looks_like_failed_catalog(catalog="", catalog_hint="目录") is False
    assert jianmian.looks_like_failed_catalog(
        catalog="", catalog_hint="减免车辆购置税的新能源汽车车型目录"
    ) is True
    assert jianmian.looks_like_failed_catalog(
        catalog="减免车辆购置税的新能源汽车车型目录", catalog_hint=""
    ) is True
    # .docx 精确转换失败时无法判断是不是目录，必须重试；仅有 textutil html 不算已转出
    assert jianmian.looks_like_failed_catalog(
        catalog="", catalog_hint="", converted=False
    ) is True
    assert jianmian.looks_like_failed_catalog(
        catalog="", catalog_hint="目录", converted=False
    ) is True


def test_store_article_rolls_back_delete_on_insert_failure(tmp_path):
    conn = jianmian.open_db(tmp_path / "db.sqlite")
    article = _article()
    rows = [{"model_code": "DOC7000BEV01", "catalog": "减免车辆购置税的新能源汽车车型目录", "batch": "28"}]
    jianmian.store_article(conn, article, [("a.doc", rows)])

    with pytest.raises(ValueError):
        jianmian.store_article(
            conn,
            article,
            [("b.doc", [{"model_code": "DOC7000BEV02", "extra": "not-a-dict"}])],
        )

    remaining = conn.execute(
        "SELECT COUNT(*) FROM catalog_rows WHERE art_id = ?", (article.art_id,)
    ).fetchone()[0]
    assert remaining == 1
    assert conn.execute(
        "SELECT model_code FROM catalog_rows WHERE art_id = ?", (article.art_id,)
    ).fetchone()[0] == "DOC7000BEV01"
    assert conn.execute(
        "SELECT status FROM articles WHERE art_id = ?", (article.art_id,)
    ).fetchone()[0] == "ok"


def test_finalize_skips_all_non_catalog_without_ok_zero_rows(tmp_path):
    conn = jianmian.open_db(tmp_path / "db.sqlite")
    article = _article()
    count, outcome = jianmian.finalize_article_sync(conn, article, [], [])
    assert count == 0
    assert outcome == "skipped"
    art = conn.execute(
        "SELECT status, error, row_count FROM articles WHERE art_id = ?", (article.art_id,)
    ).fetchone()
    assert art[0] == "ok"
    assert art[1] == "无目录附件"
    known = {
        row[0]
        for row in conn.execute("SELECT art_id FROM articles WHERE status IS NULL OR status = 'ok'")
    }
    assert article.art_id in known


def test_finalize_keeps_old_rows_when_partial_zero(tmp_path):
    conn = jianmian.open_db(tmp_path / "db.sqlite")
    article = _article()
    old = [{"model_code": "OLD7000BEV01", "catalog": "减免车辆购置税的新能源汽车车型目录", "batch": "27"}]
    jianmian.store_article(conn, article, [("old.doc", old)])
    new = [{"model_code": "NEW7000BEV01", "catalog": "减免车辆购置税的新能源汽车车型目录", "batch": "28"}]
    count, outcome = jianmian.finalize_article_sync(
        conn, article, [("new.doc", new)], ["bad.doc"]
    )
    assert outcome == "zero"
    codes = {
        row[0]
        for row in conn.execute(
            "SELECT model_code FROM catalog_rows WHERE art_id = ?", (article.art_id,)
        )
    }
    assert codes == {"OLD7000BEV01", "NEW7000BEV01"}
    assert count == 2
    assert conn.execute(
        "SELECT status FROM articles WHERE art_id = ?", (article.art_id,)
    ).fetchone()[0] == "zero_rows"


def test_safe_cache_child_rejects_traversal(tmp_path):
    folder = tmp_path / "cache"
    folder.mkdir()
    assert jianmian.safe_cache_child(folder, "购置税目录第28批.doc") == (folder / "购置税目录第28批.doc").resolve()
    assert jianmian.safe_cache_child(folder, "../evil.doc") is None
    assert jianmian.safe_cache_child(folder, "..") is None


def test_convert_doc_to_docx_force_drops_cache(tmp_path, monkeypatch):
    doc = tmp_path / "a.doc"
    doc.write_bytes(b"x")
    cached = tmp_path / "a.lo.docx"
    cached.write_bytes(b"old")
    assert jianmian.convert_doc_to_docx(doc) == cached
    monkeypatch.setattr(jianmian, "doc_to_docx_via_soffice", lambda path: None)
    monkeypatch.setattr(jianmian, "doc_to_docx_via_word", lambda path: None)
    assert jianmian.convert_doc_to_docx(doc, force=True) is None
    assert not cached.exists()


def test_download_attachment_rejects_empty_or_dotdot_name(tmp_path, monkeypatch):
    article = _article()
    monkeypatch.setattr(jianmian, "article_folder", lambda _article, _cache: tmp_path)
    with pytest.raises(ValueError, match="非法附件文件名"):
        jianmian.download_attachment("https://example.com/cms_files/", article, tmp_path)
    with pytest.raises(ValueError, match="非法附件文件名"):
        jianmian.download_attachment("https://example.com/foo/..", article, tmp_path)


def _search_args(tmp_path, db_path, **overrides):
    from types import SimpleNamespace

    defaults = dict(
        db=str(db_path),
        keyword="示例车型",
        limit=None,
        resolve=True,
        download=True,
        latest_batch=False,
        all_batches=False,
        output_dir=str(tmp_path),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _seed_one_row(db_path):
    conn = jianmian.open_db(db_path)
    article = jianmian.CatalogArticle(
        art_id="1", url="http://x/1", title="减免车辆购置税的新能源汽车车型目录（第31批）", pub_date="2026-08-01"
    )
    jianmian.store_article(
        conn,
        article,
        [("购置税目录第31批.doc", [{"model_code": "ABC7000BEV1", "common_name": "示例车型", "catalog": "减免车辆购置税的新能源汽车车型目录"}])],
    )
    conn.close()


def test_search_download_partial_returns_two(tmp_path, monkeypatch):
    """jianmian search --download 与 gonggao query 共用同一套退出码语义。"""
    db_path = tmp_path / "t.sqlite"
    _seed_one_row(db_path)
    monkeypatch.setattr(
        jianmian,
        "resolve_models",
        lambda codes: [
            {"cpsb": "示例牌", "clxh": "ABC1", "gppc": "406", "cpid": "1", "dataTag": "x"},
            {"cpsb": "示例牌", "clxh": "ABC2", "gppc": "406", "cpid": "2", "dataTag": "x"},
        ],
    )

    def fake_download(row, folder):
        if row["clxh"] == "ABC2":
            return folder / "x.html", False, 12
        return folder / "ok.pdf", True, 2048

    monkeypatch.setattr(jianmian.core, "download_param_page", fake_download)
    assert jianmian.command_search(_search_args(tmp_path, db_path)) == jianmian.core.EXIT_DOWNLOAD_PARTIAL


def test_search_download_all_non_pdf_is_total_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "t.sqlite"
    _seed_one_row(db_path)
    monkeypatch.setattr(
        jianmian,
        "resolve_models",
        lambda codes: [{"cpsb": "示例牌", "clxh": "ABC1", "gppc": "406", "cpid": "1", "dataTag": "x"}],
    )
    monkeypatch.setattr(
        jianmian.core, "download_param_page", lambda row, folder: (folder / "x.html", False, 12)
    )
    assert jianmian.command_search(_search_args(tmp_path, db_path)) == jianmian.core.EXIT_DOWNLOAD_FAILED


def test_search_download_all_pdf_returns_zero(tmp_path, monkeypatch):
    db_path = tmp_path / "t.sqlite"
    _seed_one_row(db_path)
    monkeypatch.setattr(
        jianmian,
        "resolve_models",
        lambda codes: [{"cpsb": "示例牌", "clxh": "ABC1", "gppc": "406", "cpid": "1", "dataTag": "x"}],
    )
    monkeypatch.setattr(
        jianmian.core, "download_param_page", lambda row, folder: (folder / "ok.pdf", True, 2048)
    )
    assert jianmian.command_search(_search_args(tmp_path, db_path)) == jianmian.core.EXIT_OK
