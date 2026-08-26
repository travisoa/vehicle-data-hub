"""减免税目录解析边界测试：textutil 压平 HTML 启发式重建 + Word docx 精确解析。"""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

from miit_gonggao import jianmian

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_parse_catalog_html_sample():
    catalog, batch, rows = jianmian.parse_catalog_html(FIXTURES / "jianmian_sample.html")
    assert catalog == "减免车辆购置税的新能源汽车车型目录"
    assert batch == "28"
    assert len(rows) == 3

    first, second, third = rows
    assert first["seq"] == "1"
    assert first["model_code"] == "CSX7000BEV01"
    assert first["common_name"] == "测试一号"
    assert first["range_km"] == "600"
    assert first["battery_energy"] == "77"
    assert first["energy_type"] == "纯电动汽车"
    assert first["category"] == "乘用车"

    # 第二行企业名称/商标纵向合并被压平丢失，应继承上一行
    assert second["seq"] == "2"
    assert second["model_code"] == "CSX7000BEV02"
    assert second["company"] == first["company"] == "测试汽车有限公司"
    assert second["trademark"] == first["trademark"] == "测试牌"

    # 表尾延迟生效的"二、插电式混合动力汽车"章节应落到下一张表
    assert third["model_code"] == "HSX6520PHEV01"
    assert third["energy_type"] == "插电式混合动力汽车"
    assert third["category"] == "乘用车"

    # 节能型(无续航/电池列)表不入库
    assert all(row["model_code"] != "NEN6470HEV01" for row in rows)


def test_peek_catalog_title_sample():
    title = jianmian.peek_catalog_title(FIXTURES / "jianmian_sample.html")
    assert title == "减免车辆购置税的新能源汽车车型目录"


def _docx_cell(text: str, merge_continue: bool = False) -> str:
    merge = '<w:tcPr><w:vMerge w:val="continue"/></w:tcPr>' if merge_continue else ""
    run = f"<w:r><w:t>{text}</w:t></w:r>" if text else ""
    return f"<w:tc>{merge}<w:p>{run}</w:p></w:tc>"


def _build_docx(path: Path) -> Path:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    headers = ["序号", "企业名称", "商标", "车辆型号", "通用名称", "纯电动续驶里程（km）", "动力蓄电池总能量（kWh）"]
    header_tr = "<w:tr>" + "".join(_docx_cell(h) for h in headers) + "</w:tr>"
    # 序号列是自动编号域(无文本)；第二行企业名称 vMerge 继承
    row1 = "<w:tr>" + "".join(
        [_docx_cell(""), _docx_cell("文档汽车有限公司"), _docx_cell("文档牌"),
         _docx_cell("DOC7000BEV01"), _docx_cell("文档一号"), _docx_cell("700"), _docx_cell("90")]
    ) + "</w:tr>"
    row2 = "<w:tr>" + "".join(
        [_docx_cell(""), _docx_cell("", merge_continue=True), _docx_cell("文档牌"),
         _docx_cell("DOC7000BEV02"), _docx_cell("文档一号"), _docx_cell("750"), _docx_cell("95")]
    ) + "</w:tr>"
    document = (
        f'<w:document xmlns:w="{ns}"><w:body>'
        "<w:p><w:r><w:t>减免车辆购置税的新能源汽车车型目录</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>（第二十六批，2026年第1期）</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>一、纯电动汽车</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>（一）乘用车</w:t></w:r></w:p>"
        f"<w:tbl>{header_tr}{row1}{row2}</w:tbl>"
        "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
    return path


def test_parse_catalog_docx_minimal(tmp_path):
    docx_path = _build_docx(tmp_path / "sample.word.docx")
    catalog, batch, rows = jianmian.parse_catalog_docx(docx_path)
    assert catalog == "减免车辆购置税的新能源汽车车型目录"
    assert batch == "26"
    assert len(rows) == 2
    first, second = rows
    # 自动编号域无文本，按表内计数补序号
    assert first["seq"] == "1" and second["seq"] == "2"
    # vMerge continue 继承上一行企业名称
    assert second["company"] == first["company"] == "文档汽车有限公司"
    assert second["model_code"] == "DOC7000BEV02"
    assert first["energy_type"] == "纯电动汽车"
    assert first["category"] == "乘用车"
    assert first["part"] == "第一部分 新车型"


def test_cn_to_int():
    assert jianmian.cn_to_int("二十八") == 28
    assert jianmian.cn_to_int("三十") == 30
    assert jianmian.cn_to_int("十") == 10
    assert jianmian.cn_to_int("一百零三") == 103
    assert jianmian.cn_to_int("26") == 26
    assert jianmian.cn_to_int("第") is None


def test_split_fused_cells():
    cells = ["1", "广汽埃安新能源汽车股份有限公司AHC7000BEVE1E", "埃安牌"]
    assert jianmian.split_fused_cells(cells) == [
        "1",
        "广汽埃安新能源汽车股份有限公司",
        "AHC7000BEVE1E",
        "埃安牌",
    ]


def test_suggest_prefixes():
    assert jianmian.suggest_prefixes(["ABC7150DREEV11", "ABC7150DREEV12"]) == ["ABC7150DREEV"]


def test_apply_structural_cell_catalog_switch():
    ctx = jianmian.CatalogTableContext()
    assert jianmian.apply_structural_cell("减免车辆购置税的新能源汽车车型目录", ctx)
    assert jianmian.apply_structural_cell("（第二十八批）", ctx)
    assert jianmian.apply_structural_cell("一、纯电动汽车", ctx)
    assert jianmian.apply_structural_cell("（一）乘用车", ctx)
    assert (ctx.catalog, ctx.batch, ctx.energy_type, ctx.category) == (
        "减免车辆购置税的新能源汽车车型目录", "28", "纯电动汽车", "乘用车",
    )
    # 合刊附件切换目录时重置批次/章节
    assert jianmian.apply_structural_cell("享受车船税减免优惠的节约能源使用新能源汽车车型目录", ctx)
    assert ctx.batch == "" and ctx.category == ""
    # 政策说明长句不得误判为目录标题
    assert not jianmian.apply_structural_cell("自发布之日起，列入目录的车型可享受相关政策。", ctx)


def test_convert_doc_to_docx_prefers_cache_then_soffice(tmp_path, monkeypatch):
    doc_path = tmp_path / "catalog.doc"
    doc_path.write_bytes(b"fake doc")

    # 已有缓存（任一转换器产物）直接复用，不再调用转换器
    lo_cache = doc_path.with_suffix(".lo.docx")
    lo_cache.write_bytes(b"cached lo docx")
    monkeypatch.setattr(jianmian, "find_soffice", lambda: None)
    monkeypatch.setattr(jianmian, "WORD_APP", tmp_path / "no-word.app")
    assert jianmian.convert_doc_to_docx(doc_path) == lo_cache

    lo_cache.unlink()
    word_cache = doc_path.with_suffix(".word.docx")
    word_cache.write_bytes(b"cached word docx")
    assert jianmian.convert_doc_to_docx(doc_path) == word_cache
    word_cache.unlink()

    # 无缓存：LibreOffice 成功时走 soffice 路径，不碰 Word
    calls = []
    monkeypatch.setattr(jianmian, "doc_to_docx_via_soffice", lambda p: calls.append(p) or p.with_suffix(".lo.docx"))
    monkeypatch.setattr(jianmian, "doc_to_docx_via_word", lambda p: (_ for _ in ()).throw(AssertionError("不应调用 Word")))
    assert jianmian.convert_doc_to_docx(doc_path) == doc_path.with_suffix(".lo.docx")
    assert calls == [doc_path]

    # LibreOffice 失败/超时（返回 None）时回退 Word
    word_calls = []
    monkeypatch.setattr(jianmian, "doc_to_docx_via_soffice", lambda p: None)
    monkeypatch.setattr(jianmian, "doc_to_docx_via_word", lambda p: word_calls.append(p) or p.with_suffix(".word.docx"))
    assert jianmian.convert_doc_to_docx(doc_path) == doc_path.with_suffix(".word.docx")
    assert word_calls == [doc_path]

    # LibreOffice/Word 都不可用 -> None（sync 退回 textutil 启发式）
    monkeypatch.setattr(jianmian, "doc_to_docx_via_word", lambda p: None)
    assert jianmian.convert_doc_to_docx(doc_path) is None


def test_peek_catalog_info_returns_batch():
    catalog, batch = jianmian.peek_catalog_info(FIXTURES / "jianmian_sample.html")
    assert catalog == "减免车辆购置税的新能源汽车车型目录"
    assert batch == "28"


def test_friendly_attachment_stem():
    assert jianmian.friendly_attachment_stem("减免车辆购置税的新能源汽车车型目录", "28") == "购置税目录第28批"
    assert jianmian.friendly_attachment_stem("享受车船税减免优惠的…车型目录", "83") == "车船税目录第83批"
    assert jianmian.friendly_attachment_stem("", "28") == ""
    assert jianmian.friendly_attachment_stem("减免车辆购置税…目录", "") == ""
    assert jianmian.friendly_attachment_stem("新能源汽车推广应用推荐车型目录", "5") == "推荐目录第5批"


def test_stem_from_article_title():
    title = "《道路机动车辆生产企业及产品》（第404批）、《减免车辆购置税的新能源汽车车型目录》（第二十八批）"
    # 公告附件：嗅探结果为空或退化的"目录"
    assert jianmian.stem_from_article_title("", title) == "公告第404批附件"
    assert jianmian.stem_from_article_title("目录", title) == "公告第404批附件"
    # 推荐目录批次在标题里是"2022年第5批"格式
    title2022 = "《道路机动车辆生产企业及产品》（第356批）、《新能源汽车推广应用推荐车型目录》（2022年第5批）"
    assert jianmian.stem_from_article_title("新能源汽车推广应用推荐车型目录", title2022) == "推荐目录2022年第5批"
    # 正常目录附件不走标题反推
    assert jianmian.stem_from_article_title("减免车辆购置税的新能源汽车车型目录", title) == ""


def test_rename_attachment_with_derived_and_manifest(tmp_path):
    doc = tmp_path / "8bc15bdcc247439791fe2fe157259344.doc"
    doc.write_bytes(b"doc")
    doc.with_suffix(".html").write_text("html")
    doc.with_suffix(".lo.docx").write_bytes(b"lo")

    new_doc = jianmian.rename_attachment(doc, doc.name, "购置税目录第28批")
    assert new_doc.name == "购置税目录第28批.doc"
    assert new_doc.with_suffix(".html").exists()
    assert new_doc.with_suffix(".lo.docx").exists()
    assert not doc.exists()
    manifest = jianmian.load_attachment_manifest(tmp_path)
    assert manifest["8bc15bdcc247439791fe2fe157259344.doc"] == "购置税目录第28批.doc"

    # 已是目标名时幂等；同批次第二个附件加序号；空主干保持原名
    assert jianmian.rename_attachment(new_doc, doc.name, "购置税目录第28批") == new_doc
    doc2 = tmp_path / "another.doc"
    doc2.write_bytes(b"doc2")
    assert jianmian.rename_attachment(doc2, doc2.name, "购置税目录第28批").name == "购置税目录第28批_2.doc"
    doc3 = tmp_path / "keep.doc"
    doc3.write_bytes(b"doc3")
    assert jianmian.rename_attachment(doc3, doc3.name, "") == doc3


def test_peek_catalog_info_docx(tmp_path):
    docx_path = _build_docx(tmp_path / "sample.word.docx")
    assert jianmian.peek_catalog_info_docx(docx_path) == ("减免车辆购置税的新能源汽车车型目录", "26")


def test_download_attachment_uses_manifest(tmp_path, monkeypatch):
    article = jianmian.CatalogArticle(art_id="12168", url="https://example.com/a.html", title="t", pub_date="d")
    folder = tmp_path / "12168"
    folder.mkdir()
    renamed = folder / "购置税目录第28批.doc"
    renamed.write_bytes(b"x" * 2048)
    jianmian.save_attachment_manifest(folder, {"8bc15bdcc247439791fe2fe157259344.doc": renamed.name})

    def no_download(*args, **kwargs):
        raise AssertionError("重命名后不应重新下载")

    monkeypatch.setattr(jianmian, "http_get", no_download)
    url = "https://www.miit.gov.cn/cms_files/8bc15bdcc247439791fe2fe157259344.doc"
    assert jianmian.download_attachment(url, article, tmp_path) == renamed


def test_article_folder_name():
    article = jianmian.CatalogArticle(
        art_id="12168", url="", pub_date="",
        title="《道路机动车辆生产企业及产品》（第404批）、《减免车辆购置税的新能源汽车车型目录》（第二十八批）",
    )
    assert jianmian.article_folder_name(article) == "12168_公告第404批"
    # 标题无公告批次时退回纯ID
    plain = jianmian.CatalogArticle(art_id="999", url="", title="某公告", pub_date="")
    assert jianmian.article_folder_name(plain) == "999"


def test_article_folder_migrates_legacy_dir(tmp_path):
    article = jianmian.CatalogArticle(
        art_id="12168", url="", pub_date="",
        title="《道路机动车辆生产企业及产品》（第404批）",
    )
    legacy = tmp_path / "12168"
    legacy.mkdir()
    (legacy / "购置税目录第28批.doc").write_bytes(b"doc")

    folder = jianmian.article_folder(article, tmp_path)
    assert folder.name == "12168_公告第404批"
    assert (folder / "购置税目录第28批.doc").exists()
    assert not legacy.exists()
    # 再次调用幂等
    assert jianmian.article_folder(article, tmp_path) == folder


def test_doc_to_html_degrades_without_textutil(tmp_path, monkeypatch):
    """非 macOS 平台没有 textutil：返回 None 让流程改走 .docx，而不是中止整个 sync。"""
    monkeypatch.setattr(jianmian.shutil, "which", lambda name: None)
    doc = tmp_path / "购置税目录第31批.doc"
    doc.write_bytes(b"stub")
    assert jianmian.doc_to_html(doc) is None


def test_doc_to_html_reuses_html_cache_on_any_platform(tmp_path, monkeypatch):
    """已有 .html 缓存时任何平台都直接复用，不依赖 textutil 是否存在。"""
    monkeypatch.setattr(jianmian.shutil, "which", lambda name: None)
    doc = tmp_path / "购置税目录第31批.doc"
    doc.write_bytes(b"stub")
    html = doc.with_suffix(".html")
    html.write_text("<html>cached</html>", encoding="utf-8")
    assert jianmian.doc_to_html(doc) == html


def test_word_channel_skipped_off_darwin(tmp_path, monkeypatch):
    """AppleScript 通道仅 macOS 可用，其他平台直接返回 None，不调用 osascript。"""
    called = []
    monkeypatch.setattr(jianmian.subprocess, "run", lambda *a, **k: called.append(a))
    monkeypatch.setattr(jianmian.sys, "platform", "win32")
    doc = tmp_path / "购置税目录第31批.doc"
    doc.write_bytes(b"stub")
    assert jianmian.doc_to_docx_via_word(doc) is None
    assert called == []


def test_soffice_paths_cover_three_platforms():
    """LibreOffice 候选路径需覆盖三平台——Windows 版安装后默认不写入 PATH。"""
    joined = " ".join(jianmian.SOFFICE_PATHS)
    assert "/Applications/LibreOffice.app" in joined          # macOS
    assert "Program Files" in joined                          # Windows
    assert "/usr/bin/soffice" in joined                       # Linux


def test_find_soffice_falls_back_to_candidate_paths(tmp_path, monkeypatch):
    """PATH 里没有 soffice 时（Windows 的常态）应回退到候选安装路径。"""
    fake = tmp_path / "soffice.exe"
    fake.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(jianmian.shutil, "which", lambda name: None)
    monkeypatch.setattr(jianmian, "SOFFICE_PATHS", (str(fake),))
    assert jianmian.find_soffice() == str(fake)


def _make_docx(path, with_document_xml=True):
    import zipfile

    with zipfile.ZipFile(path, "w") as archive:
        if with_document_xml:
            archive.writestr("word/document.xml", "<w:document/>")
        else:
            archive.writestr("junk.txt", "not a docx")
    return path


def test_is_valid_docx(tmp_path):
    good = _make_docx(tmp_path / "good.docx")
    bad_zip = _make_docx(tmp_path / "bad.docx", with_document_xml=False)
    not_zip = tmp_path / "raw.docx"
    not_zip.write_bytes(b"not a zip")
    empty = tmp_path / "empty.docx"
    empty.write_bytes(b"")
    assert jianmian.is_valid_docx(good)
    assert not jianmian.is_valid_docx(bad_zip)
    assert not jianmian.is_valid_docx(not_zip)
    assert not jianmian.is_valid_docx(empty)
    assert not jianmian.is_valid_docx(tmp_path / "missing.docx")


def test_native_docx_skips_converter(tmp_path, monkeypatch):
    """原生 .docx 附件直接返回自身，不送进 LibreOffice/Word 空转一遍。"""
    called = []
    monkeypatch.setattr(jianmian, "doc_to_docx_via_soffice", lambda p: called.append(p))
    monkeypatch.setattr(jianmian, "doc_to_docx_via_word", lambda p: called.append(p))
    native = _make_docx(tmp_path / "购置税目录第31批.docx")
    assert jianmian.convert_doc_to_docx(native) == native
    assert called == []


def test_soffice_rejects_invalid_product(tmp_path, monkeypatch):
    """转换器返回 0 但产物不是有效 DOCX 时，按失败处理而不是把垃圾缓存下来。"""
    doc = tmp_path / "购置税目录第31批.doc"
    doc.write_bytes(b"stub")
    monkeypatch.setattr(jianmian, "find_soffice", lambda: "/fake/soffice")

    def fake_run(cmd, **kwargs):
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        (outdir / f"{doc.stem}.docx").write_bytes(b"not a zip")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(jianmian.subprocess, "run", fake_run)
    assert jianmian.doc_to_docx_via_soffice(doc) is None
    assert not doc.with_suffix(".lo.docx").exists()


def test_soffice_timeout_configurable(monkeypatch):
    """无 Word 通道的平台遇到大附件需要能调大超时。"""
    import importlib

    monkeypatch.setenv("SOFFICE_TIMEOUT", "900")
    reloaded = importlib.reload(jianmian)
    assert reloaded.SOFFICE_TIMEOUT == 900
    monkeypatch.delenv("SOFFICE_TIMEOUT")
    importlib.reload(jianmian)
