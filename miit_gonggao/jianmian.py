#!/usr/bin/env python3
"""减免购置税/车船税目录：抓取装备中心公告文章附件，解析车型表入库（SQLite + Excel）。

数据流：
    EIDC col1691 文章列表(dataproxy.jsp 分页)
      -> 正式发布文章(标题含《减免车辆购置税的新能源汽车车型目录》/《车船税减免》)
      -> 下载 .doc 附件(缓存到 downloads/jianmian/<art_id>/)
      -> textutil 转 HTML(表格被压平为单段、单元格以 <span class="s1"></span> 分隔)
      -> 按表头重建行(序号递增触发新行；企业名称/商标纵向合并时继承上一行)
      -> SQLite data/jianmian_catalog.sqlite + Excel 导出 output/

典型用途：新车型只知道市场名(通用名称，如"<市场名>")时，从目录反查公告车辆型号与商标，
再走公告接口下载参数页 PDF。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

from miit_gonggao import core

EIDC_BASE = "https://www.miit-eidc.org.cn"
COLUMN_URL = f"{EIDC_BASE}/col/col1691/index.html"
DATAPROXY_URL = f"{EIDC_BASE}/module/web/jpage/dataproxy.jsp"
DATAPROXY_FORM = {
    "col": "1",
    "webid": "12",
    "path": f"{EIDC_BASE}/",
    "columnid": "1691",
    "sourceContentType": "1",
    "unitid": "4638",
    "webname": "工业和信息化部装备工业发展中心",
    "permissiontype": "0",
}
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
ATTACHMENT_RE = re.compile(
    r'href="(https://(?:www|wap)\.miit\.gov\.cn/cms_files/[^"]+\.(?:docx?|xlsx?))"'
)
# textutil 把 .doc 表格压平为单个 <p>，单元格之间是空 span（class 因文档而异）；
# 空单元格与自动编号的序号列会被直接丢掉，行重建见 reconstruct_rows。
CELL_SEP_RE = re.compile(r'<span class="s\d+"></span>')
MODEL_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,5}\d{4}[A-Z0-9./-]*$")

CACHE_DIR = core.DEFAULT_OUTPUT_DIR / "jianmian"
DB_PATH = core.PROJECT_ROOT / "data" / "jianmian_catalog.sqlite"
EXPORT_DIR = core.PROJECT_ROOT / "output"
EXPORT_XLSX_PATH = EXPORT_DIR / "jianmian_catalog.xlsx"
EXPORT_BY_CATEGORY_DIR = EXPORT_DIR / "jianmian_by_category"

# 表头 -> 规范字段（按序匹配，整备质量须先于电池质量判断）；其余表头进 extra
HEADER_FIELD_RULES: list[tuple[tuple[str, ...], str]] = [
    (("序号",), "seq"),
    (("企业名称",), "company"),
    (("商标",), "trademark"),
    (("型号",), "model_code"),  # 车辆型号 / 产品型号
    (("通用名称",), "common_name"),
    (("产品名称",), "product_name"),
    (("续驶里程",), "range_km"),
    (("燃料消耗量",), "fuel_consumption"),
    (("排量",), "displacement_ml"),
    (("整备质量",), "curb_mass"),
    (("电池", "质量"), "battery_mass"),
    (("电池", "能量"), "battery_energy"),
    (("备注",), "remark"),
]

CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(text: str) -> int | None:
    """中文数字(到百位) -> int，例如 二十八 -> 28。"""
    text = text.strip()
    if text.isdigit():
        return int(text)
    total, current = 0, 0
    for ch in text:
        if ch in CN_DIGITS:
            current = CN_DIGITS[ch]
        elif ch == "十":
            total += (current or 1) * 10
            current = 0
        elif ch == "百":
            total += (current or 1) * 100
            current = 0
        else:
            return None
    return total + current


def http_get(url: str, referer: str = COLUMN_URL, timeout: int = 60) -> bytes:
    content, _headers = core.http_request(
        url,
        headers={"User-Agent": BROWSER_UA, "Referer": referer},
        timeout=timeout,
    )
    return content


def http_post(url: str, form: dict[str, str], referer: str = COLUMN_URL, timeout: int = 60) -> bytes:
    content, _headers = core.http_request(
        url,
        data=form,
        headers={
            "User-Agent": BROWSER_UA,
            "Referer": referer,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=timeout,
    )
    return content


@dataclass
class CatalogArticle:
    art_id: str
    url: str
    title: str
    pub_date: str


@dataclass
class CatalogTableContext:
    catalog: str = ""  # 减免车辆购置税的新能源汽车车型目录 / 享受车船税减免优惠的...车型目录
    batch: str = ""  # 目录批次，如 28
    part: str = ""  # 第一部分 新车型 / 第二部分 变更扩展车型
    energy_type: str = ""  # 纯电动汽车 / 插电式混合动力汽车 / 燃料电池汽车
    category: str = ""  # 乘用车 / 客车 / 货车 ...


def list_articles(max_pages: int = 10, page_size: int = 25) -> list[CatalogArticle]:
    """从 col1691 列表(dataproxy 分页)枚举文章，新→旧。"""
    articles: list[CatalogArticle] = []
    total_record: int | None = None
    for page in range(max_pages):
        start = page * page_size + 1
        end = start + page_size - 1
        if total_record is not None and start > total_record:
            break
        url = f"{DATAPROXY_URL}?startrecord={start}&endrecord={end}&perpage={page_size}"
        xml = http_post(url, DATAPROXY_FORM).decode("utf-8", "ignore")
        if total_record is None:
            match = re.search(r"<totalrecord>(\d+)</totalrecord>", xml)
            total_record = int(match.group(1)) if match else 0
        found = re.findall(r"<a href='([^']*art_1691_(\d+)\.html)'[^>]*title='([^']*)'", xml)
        for href, art_id, title in found:
            date_match = re.search(r"/art/(\d+)/(\d+)/(\d+)/", href)
            articles.append(
                CatalogArticle(
                    art_id=art_id,
                    url=href if href.startswith("http") else urljoin(EIDC_BASE, href),
                    title=title,
                    pub_date="-".join(date_match.groups()) if date_match else "",
                )
            )
        if not found:
            break
    return articles


def is_catalog_release(article: CatalogArticle) -> bool:
    """正式发布的目录文章：标题含目录名且不是拟发布公示。"""
    if "公示" in article.title:
        return False
    return "减免车辆购置税" in article.title or "车船税减免" in article.title


def article_attachments(article: CatalogArticle) -> list[str]:
    html = http_get(article.url).decode("utf-8", "ignore")
    return list(dict.fromkeys(ATTACHMENT_RE.findall(html)))


# 附件原始 URL 文件名是 cms 哈希串；重命名后靠 manifest.json 维持 URL名->本地名 映射，
# 否则 --force / 缓存检查会找不到文件而重新下载
ATTACHMENT_MANIFEST = "manifest.json"
DERIVED_SUFFIXES = (".html", ".lo.docx", ".word.docx")


def load_attachment_manifest(folder: Path) -> dict[str, str]:
    path = folder / ATTACHMENT_MANIFEST
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_attachment_manifest(folder: Path, manifest: dict[str, str]) -> None:
    path = folder / ATTACHMENT_MANIFEST
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def friendly_attachment_stem(catalog: str, batch: str) -> str:
    """目录附件的可读文件名主干：购置税目录第28批；嗅探不出目录/批次时返回空（保持原名）。"""
    if not catalog or not batch:
        return ""
    if "购置税" in catalog:
        return f"购置税目录第{batch}批"
    if "车船税" in catalog:
        return f"车船税目录第{batch}批"
    if "推荐" in catalog:
        return f"推荐目录第{batch}批"
    return ""


def stem_from_article_title(catalog: str, title: str) -> str:
    """嗅探不到批次的附件按文章标题反推命名。

    公告附件嗅探出的 catalog 是空或退化的"目录"（附件里的目录页），批次取自标题里的公告批次；
    2020~2022 推荐目录的批次格式是"2022年第5批"，结构嗅探拿不到，也从标题取。
    """
    if catalog in ("", "目录"):
        match = re.search(r"《道路机动车辆生产企业及产品》[（(]第(\d+)批[）)]", title)
        return f"公告第{match.group(1)}批附件" if match else ""
    if "推荐" in catalog:
        match = re.search(r"《新能源汽车推广应用推荐车型目录》[（(]([^）)]+)[）)]", title)
        return f"推荐目录{match.group(1)}" if match else ""
    return ""


def peek_catalog_info_docx(docx_path: Path) -> tuple[str, str]:
    """textutil 转换失败的附件改从已转换的 docx 嗅探(目录标题, 批次)。"""
    import xml.etree.ElementTree as ET
    import zipfile

    try:
        with zipfile.ZipFile(docx_path) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
    except Exception:  # noqa: BLE001
        return "", ""
    body = root.find("w:body", DOCX_NS)
    if body is None:
        return "", ""
    probe = CatalogTableContext()
    for child in body:
        tag = child.tag.split("}")[-1]
        if tag == "tbl":
            break
        if tag == "p":
            text = "".join(t.text or "" for t in child.iter(f"{{{DOCX_NS['w']}}}t")).strip()
            if text:
                apply_structural_cell(text, probe)
        if probe.catalog and probe.batch:
            break
    return probe.catalog, probe.batch


def safe_cache_child(folder: Path, name: str) -> Path | None:
    """只允许落在 folder 内的单层文件名，拒绝 ../ 与绝对路径。"""
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return None
    folder = folder.resolve()
    path = (folder / name).resolve()
    try:
        path.relative_to(folder)
    except ValueError:
        return None
    return path


def rename_attachment(doc_path: Path, url_name: str, stem: str) -> Path:
    """按给定主干重命名附件及其派生缓存(.html/.lo.docx/.word.docx)，并登记 manifest。"""
    stem = core.safe_part(stem)
    if stem == "unknown" or doc_path.stem == stem or re.fullmatch(re.escape(stem) + r"_\d+", doc_path.stem):
        return doc_path
    folder = doc_path.parent
    target_stem, index = stem, 1
    while (folder / f"{target_stem}{doc_path.suffix}").exists():
        index += 1
        target_stem = f"{stem}_{index}"
    new_doc = safe_cache_child(folder, f"{target_stem}{doc_path.suffix}")
    if new_doc is None:
        return doc_path
    doc_path.rename(new_doc)
    for suffix in DERIVED_SUFFIXES:
        derived = doc_path.with_suffix(suffix)
        if derived.exists():
            derived.rename(new_doc.with_suffix(suffix))
    manifest = load_attachment_manifest(folder)
    manifest[url_name] = new_doc.name
    save_attachment_manifest(folder, manifest)
    return new_doc


def article_folder_name(article: CatalogArticle) -> str:
    """缓存目录名：12168_公告第404批；ID 前缀是缓存键锚点，批次后缀只为可读。"""
    match = re.search(r"《道路机动车辆生产企业及产品》[（(]第(\d+)批[）)]", article.title)
    return f"{article.art_id}_公告第{match.group(1)}批" if match else article.art_id


def article_folder(article: CatalogArticle, cache_dir: Path) -> Path:
    """定位文章缓存目录；旧版纯ID目录自动迁移成可读名。"""
    preferred = cache_dir / article_folder_name(article)
    if not preferred.exists():
        legacy = cache_dir / article.art_id
        if legacy.exists() and legacy != preferred:
            legacy.rename(preferred)
    preferred.mkdir(parents=True, exist_ok=True)
    return preferred


def invalidate_derived_caches(doc_path: Path) -> None:
    for suffix in DERIVED_SUFFIXES:
        derived = doc_path.with_suffix(suffix)
        if derived.exists():
            derived.unlink()


def download_attachment(
    url: str, article: CatalogArticle, cache_dir: Path, *, force: bool = False
) -> Path:
    folder = article_folder(article, cache_dir)
    url_name = Path(url.rsplit("/", 1)[-1]).name
    mapped = load_attachment_manifest(folder).get(url_name)
    mapped_path = safe_cache_child(folder, mapped) if mapped else None
    if mapped_path and mapped_path.exists() and mapped_path.stat().st_size > 1024 and not force:
        return mapped_path
    path = safe_cache_child(folder, url_name)
    if path is None:
        raise ValueError(f"非法附件文件名: {url_name!r}")
    if path.exists() and path.stat().st_size > 1024 and not force:
        return path
    target = mapped_path if (force and mapped_path) else path
    if force:
        invalidate_derived_caches(target)
    content = http_get(url, referer=article.url, timeout=180)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


# .doc -> .docx 转换器：优先 LibreOffice（headless 无窗口），失败/超时回退本机 Word；
# 都没有时 command_sync 退回 textutil 压平文本的启发式重建
SOFFICE_PATHS = ("/Applications/LibreOffice.app/Contents/MacOS/soffice",)
# 大合刊附件（如31MB、2万行的批次26）LibreOffice 转不完，超时后交给 Word
SOFFICE_TIMEOUT = 300


def find_soffice() -> str | None:
    found = shutil.which("soffice")
    if found:
        return found
    for candidate in SOFFICE_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


def doc_to_docx_via_soffice(doc_path: Path) -> Path | None:
    """用 LibreOffice headless 把 .doc 转成 .docx（保留真实表格结构，无窗口无授权框）。"""
    docx_path = doc_path.with_suffix(".lo.docx")
    if docx_path.exists() and docx_path.stat().st_size > 0:
        return docx_path
    soffice = find_soffice()
    if not soffice:
        return None
    with tempfile.TemporaryDirectory(prefix="jianmian-lo-") as tmp_dir:
        try:
            result = subprocess.run(
                [soffice, "--headless", "--convert-to", "docx", "--outdir", tmp_dir, str(doc_path)],
                capture_output=True,
                timeout=SOFFICE_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(f"[警告] LibreOffice 转换失败: {doc_path.name} {exc}", file=sys.stderr)
            return None
        produced = Path(tmp_dir) / f"{doc_path.stem}.docx"
        if result.returncode != 0 or not produced.exists():
            print(
                f"[警告] LibreOffice 转换失败: {doc_path.name} "
                f"{result.stderr.decode(errors='ignore').strip()}",
                file=sys.stderr,
            )
            return None
        shutil.move(str(produced), docx_path)
    return docx_path


def convert_doc_to_docx(doc_path: Path, *, force: bool = False) -> Path | None:
    """统一入口：已有缓存(.lo.docx/.word.docx)直接复用；否则 LibreOffice 优先，
    转换失败/超时再回退本机 Word。`--force` 时删除派生缓存再转。"""
    if force:
        for suffix in (".lo.docx", ".word.docx"):
            cached = doc_path.with_suffix(suffix)
            if cached.exists():
                cached.unlink()
    else:
        for suffix in (".lo.docx", ".word.docx"):
            cached = doc_path.with_suffix(suffix)
            if cached.exists() and cached.stat().st_size > 0:
                return cached
    converted = doc_to_docx_via_soffice(doc_path)
    if converted:
        return converted
    return doc_to_docx_via_word(doc_path)


WORD_APP = Path("/Applications/Microsoft Word.app")
# Word 是沙盒应用，读写容器外的文件每个路径都会弹授权框；
# 在它自己的 Group Container 里转换则完全免授权
WORD_CONTAINER_TMP = Path.home() / "Library/Group Containers/UBF8T346G9.Office/vehicle-data-hub-tmp"


def doc_to_docx_via_word(doc_path: Path) -> Path | None:
    """用本机 Microsoft Word 把 .doc 转成 .docx（保留真实表格结构）。

    textutil 会把部分批次的表格压平成无分隔文本，这些文档走 Word 兜底。
    转换在 Word 的沙盒容器目录内进行，避免每个文件弹一次授权对话框。
    """
    docx_path = doc_path.with_suffix(".word.docx")
    if docx_path.exists() and docx_path.stat().st_size > 0:
        return docx_path
    if not WORD_APP.exists():
        return None

    WORD_CONTAINER_TMP.mkdir(parents=True, exist_ok=True)
    # 临时名只用原路径的哈希，不带原文件名：附件名可能含 " 或 \，
    # 直接拼进 AppleScript 源码会破坏字符串上下文（可执行注入的脚本）
    safe_stem = hashlib.sha1(str(doc_path).encode("utf-8")).hexdigest()[:16]
    tmp_doc = WORD_CONTAINER_TMP / f"{safe_stem}.doc"
    tmp_docx = tmp_doc.with_suffix(".word.docx")
    shutil.copy2(doc_path, tmp_doc)
    script = f'''
tell application "Microsoft Word"
    open POSIX file "{tmp_doc}"
    set theDoc to active document
    tell theDoc
        save as it file name "{tmp_docx}" file format format document
    end tell
    close theDoc saving no
end tell
'''
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=120)
        if result.returncode != 0 or not tmp_docx.exists():
            print(
                f"[警告] Word 转换失败: {doc_path.name} {result.stderr.decode(errors='ignore').strip()}",
                file=sys.stderr,
            )
            return None
        shutil.move(tmp_docx, docx_path)
    finally:
        tmp_doc.unlink(missing_ok=True)
        tmp_docx.unlink(missing_ok=True)
    return docx_path


DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def parse_catalog_docx(docx_path: Path) -> tuple[str, str, list[dict[str, Any]]]:
    """解析 Word 转出的 .docx：真实表格结构，按表头精确对位，vMerge 继承上一行。"""
    import xml.etree.ElementTree as ET
    import zipfile

    with zipfile.ZipFile(docx_path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    body = root.find("w:body", DOCX_NS)
    if body is None:
        raise ValueError(f"docx 缺少 w:body 节点，无法解析: {docx_path.name}")

    def text_of(element: Any) -> str:
        return "".join(t.text or "" for t in element.iter(f"{{{DOCX_NS['w']}}}t")).strip()

    ctx = CatalogTableContext()
    rows: list[dict[str, Any]] = []
    for child in body:
        tag = child.tag.split("}")[-1]
        if tag == "p":
            text = text_of(child)
            if text:
                apply_structural_cell(text, ctx)
            continue
        if tag != "tbl":
            continue
        table_rows = child.findall("w:tr", DOCX_NS)
        if len(table_rows) < 2:
            continue
        headers = [text_of(tc) for tc in table_rows[0].findall("w:tc", DOCX_NS)]
        columns = [map_header(h) for h in headers]
        # 客车/货车/专用车表没有"通用名称"列，同样入库；推荐目录(2020~2022)口径不同，不入库
        if "model_code" not in columns or "推荐" in ctx.catalog:
            continue
        if not any(col in columns for col in ("range_km", "battery_mass", "battery_energy")):
            continue
        prev_values: list[str] = [""] * len(columns)
        auto_seq = 0
        for tr in table_rows[1:]:
            cells = tr.findall("w:tc", DOCX_NS)
            values: list[str] = []
            for index, tc in enumerate(cells[: len(columns)]):
                value = text_of(tc)
                merge = tc.find("w:tcPr/w:vMerge", DOCX_NS)
                if merge is not None and merge.get(f"{{{DOCX_NS['w']}}}val", "continue") == "continue":
                    value = prev_values[index] if index < len(prev_values) else value
                values.append(value)
            values += [""] * (len(columns) - len(values))
            prev_values = values
            row: dict[str, Any] = {}
            extras: dict[str, str] = {}
            for col, value in zip(columns, values):
                if col.startswith("extra:"):
                    extras[col[6:]] = value
                else:
                    row[col] = value
            # 跳过空行与跨页重复的表头行；列结构真实，不再要求型号匹配正则
            if not row.get("model_code") or "型号" in row["model_code"]:
                continue
            auto_seq += 1
            if not row.get("seq"):  # 序号列常是自动编号域，转换后无文本
                row["seq"] = str(auto_seq)
            if extras:
                row["extra"] = extras
            row["raw_cells"] = values
            row.update(
                catalog=ctx.catalog,
                batch=ctx.batch,
                part=ctx.part or "第一部分 新车型",
                energy_type=ctx.energy_type,
                category=ctx.category,
            )
            rows.append(row)
    return ctx.catalog, ctx.batch, rows


def doc_to_html(doc_path: Path, *, force: bool = False) -> Path | None:
    """textutil(.doc -> .html)，结果缓存在附件旁边。仅 macOS。"""
    html_path = doc_path.with_suffix(".html")
    if force and html_path.exists():
        html_path.unlink()
    elif html_path.exists() and html_path.stat().st_size > 0:
        return html_path
    if not shutil.which("textutil"):
        raise SystemExit("缺少 textutil（macOS 自带）；其他平台请先人工转换 .doc 为 .html")
    result = subprocess.run(
        ["textutil", "-convert", "html", str(doc_path), "-output", str(html_path)],
        capture_output=True,
    )
    if result.returncode != 0 or not html_path.exists():
        print(f"[警告] textutil 转换失败: {doc_path.name}", file=sys.stderr)
        return None
    return html_path


def strip_tags(fragment: str) -> str:
    return re.sub(r"<[^>]+>", "", fragment).replace("&amp;", "&").replace("&nbsp;", " ").strip()


def looks_like_company(value: str) -> bool:
    return bool(re.search(r"(公司|集团|制造厂|汽车厂|研究院|研究所)", value)) and "：" not in value


def looks_like_model_code(value: str) -> bool:
    return bool(MODEL_CODE_RE.match(value)) and "国" not in value and not value.startswith("GB")


# 个别批次源文档把企业名称与型号粘在一个单元格里，如 "广汽埃安新能源汽车股份有限公司AHC7000BEVE1E"
FUSED_COMPANY_MODEL_RE = re.compile(
    r"^(.{2,}?(?:公司|集团|制造厂|汽车厂|研究院|研究所))([A-Z][A-Z0-9]{1,5}\d{4}[A-Z0-9./-]*)$"
)


def split_fused_cells(cells: list[str]) -> list[str]:
    out: list[str] = []
    for cell in cells:
        match = FUSED_COMPANY_MODEL_RE.match(cell)
        if match:
            out.extend([match.group(1), match.group(2)])
        else:
            out.append(cell)
    return out


def map_header(header: str) -> str:
    for needles, name in HEADER_FIELD_RULES:
        if all(needle in header for needle in needles):
            return name
    return f"extra:{header}"


def cell_fits_column(cell: str, column: str) -> bool:
    """单元格类型是否匹配规范字段；空单元格在源数据里被丢弃，靠这里跳列对位。"""
    if column == "seq":
        return cell.isdigit()
    if column == "company":
        return looks_like_company(cell)
    if column == "trademark":
        return "牌" in cell
    if column == "model_code":
        return looks_like_model_code(cell)
    if looks_like_company(cell) or looks_like_model_code(cell):
        return False  # 企业/型号样式的单元格只许落锚点列，否则错位行会吞掉后续行的锚点
    if column == "product_name":
        return "车" in cell
    if column in ("range_km", "fuel_consumption", "displacement_ml", "curb_mass", "battery_mass", "battery_energy"):
        return any(ch.isdigit() for ch in cell)
    return True  # common_name / remark / extra:* 接受任意文本


def reconstruct_rows(cells: list[str], columns: list[str], carry: dict[str, str]) -> list[dict[str, Any]]:
    """把压平后的单元格流重建为行。

    - 每行以 车辆型号 为锚点；序号列可能整列丢失(自动编号域)，企业名称/商标纵向合并时只出现一次
    - 新行触发条件：已填过型号后又遇到 序号==期望值 / 企业名称样式 / 型号样式 的单元格
    - 行内按表头顺序"跳列对位"：单元格不匹配当前列类型时该列置空、继续向后找
    """
    rows: list[dict[str, Any]] = []
    model_pos = columns.index("model_code")
    company_pos = columns.index("company") if "company" in columns else -1

    row: dict[str, Any] = {}
    raw: list[str] = []
    col_idx = 0
    next_seq = 1

    def close_row() -> None:
        nonlocal row, raw, col_idx, next_seq
        if row.get("model_code"):
            if not row.get("seq"):
                row["seq"] = str(next_seq)
            next_seq = int(row["seq"]) + 1 if row["seq"].isdigit() else next_seq + 1
            for key in ("company", "trademark"):
                if key in columns:
                    if row.get(key):
                        carry[key] = row[key]
                    else:
                        row[key] = carry.get(key, "")
            row["raw_cells"] = raw
            rows.append(row)
        row, raw, col_idx = {}, [], 0

    for cell in cells:
        if raw:
            has_model = bool(row.get("model_code"))
            # 行内已填序号时，下一行序号 = 本行+1；序号列整列丢失时退回全表计数
            expected_seq = str(int(row["seq"]) + 1) if str(row.get("seq", "")).isdigit() else str(next_seq)
            # 触发新行的锚点：已越过公司/型号列后又出现公司/型号样式的单元格，
            # 必属下一行；这样型号缺失的破行(源数据粘连)会被丢弃而不是吞掉表格剩余单元格
            starts_new = (
                (looks_like_company(cell) and (has_model or row.get("company") or col_idx > company_pos >= 0))
                or (looks_like_model_code(cell) and col_idx > model_pos)
                or (cell == expected_seq and has_model)
            )
            if starts_new:
                close_row()
        while col_idx < len(columns) and not cell_fits_column(cell, columns[col_idx]):
            row.setdefault(columns[col_idx], "")
            col_idx += 1
        if col_idx < len(columns):
            row[columns[col_idx]] = cell
            col_idx += 1
        else:  # 行尾多余内容并入备注，原始单元格保留在 raw_cells 供核查
            row["remark"] = " ".join(filter(None, [row.get("remark", ""), cell]))
        raw.append(cell)
    close_row()
    return rows


STRUCTURAL_TEXT_RE = re.compile(
    r"^(第[一二三四五六七八九十]+部分|[一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[）)]\D+$|注[：:])"
)


def is_structural_text(text: str) -> bool:
    return bool(STRUCTURAL_TEXT_RE.match(text))


def apply_structural_cell(text: str, ctx: CatalogTableContext) -> bool:
    """目录标题/批次/章节/表注；命中则更新上下文并返回 True。"""
    # "第X部分"标题须先于目录标题判断（如"第二部分 第四批至第六十四批《…目录》调整车型"）
    if re.match(r"^第[一二三四五六七八九十]+部分", text):
        ctx.part = text
    # 引用书名号的标题（如"第四批至第六十四批《…目录》中整改车型"、勘误说明）是
    # 跨批次汇总的章节标题，不是目录名；目录与批次沿用当前文档
    elif "《" in text and "目录" in text and not re.search(r"[，。；]", text):
        if not ctx.catalog and (match := re.search(r"《([^》]*目录)》", text)):
            ctx.catalog = match.group(1)
        ctx.part = text
    # 目录标题是不含标点的短行；政策说明长段落也含"目录/车型"字样，须排除
    elif "目录" in text and "车型" in text and len(text) <= 60 and not re.search(r"[，。；]", text):
        # 同一附件可能装订多个目录（如车船税+购置税合刊），切换目录时重置批次与章节
        if text != ctx.catalog:
            ctx.batch = ""
            ctx.part = ""
            ctx.energy_type = ""
            ctx.category = ""
        ctx.catalog = text
    # 批次标题可能带后缀，如"（第二十六批，2026年第1期）"
    elif re.match(r"[（(]第(.+?)批[，,）)]", text) and ctx.catalog and not ctx.batch:
        raw = re.match(r"[（(]第(.+?)批[，,）)]", text).group(1)
        ctx.batch = str(cn_to_int(raw) or raw)
    elif re.match(r"^[一二三四五六七八九十]+、", text):
        ctx.energy_type = re.sub(r"^[一二三四五六七八九十]+、", "", text)
        ctx.category = ""
    elif re.match(r"^[（(][一二三四五六七八九十]+[）)]\D+$", text):
        ctx.category = re.sub(r"^[（(][一二三四五六七八九十]+[）)]", "", text)
    elif text.startswith(("注：", "注:")):
        pass
    else:
        return False
    return True


def peek_catalog_info(html_path: Path) -> tuple[str, str]:
    """从 textutil 转出的 HTML 头部嗅探(目录标题, 批次)；非目录附件(公告附件1等)返回空串。"""
    html = html_path.read_text(encoding="utf-8", errors="ignore")
    body = html[html.find("<body>"):]
    probe = CatalogTableContext()
    for para in re.findall(r"<p[^>]*>(.*?)</p>", body, re.S)[:12]:
        text = strip_tags(CELL_SEP_RE.split(para)[0])
        if text:
            apply_structural_cell(text, probe)
        if probe.catalog and probe.batch:
            break
    return probe.catalog, probe.batch


def peek_catalog_title(html_path: Path) -> str:
    return peek_catalog_info(html_path)[0]


def is_non_catalog_peek(catalog_hint: str) -> bool:
    """仅在嗅探到明确非目录信号时跳过；空嗅探必须继续走完整解析。

    「目录」= 公告附件自带目录页的退化标题；「推荐」= 2020~2022 推荐车型目录（不入库）。
    """
    hint = (catalog_hint or "").strip()
    if not hint:
        return False
    return hint == "目录" or "推荐" in hint


def looks_like_failed_catalog(
    *, catalog: str, catalog_hint: str, converted: bool = True
) -> bool:
    """全文解析 0 行后是否按目录解析失败处理。

    `converted` 指 .docx 精确解析这条可信路径是否跑成功。它失败时无从判断附件是不是
    目录——textutil 压平文本的启发式提取不到标题并不能证明「不是目录」，此时必须标
    zero_rows 以便下次重试，宁可重试也不要静默漏掉一个批次。
    """
    if not converted:
        return True
    if catalog.strip():
        return True
    hint = (catalog_hint or "").strip()
    return bool(hint) and not is_non_catalog_peek(hint)


def parse_catalog_html(html_path: Path) -> tuple[str, str, list[dict[str, Any]]]:
    """解析转换后的 HTML，返回 (目录名, 目录批次, 行列表)。

    只解析新能源车型表(表头含 型号+通用名称，且含 续驶里程/电池 字段)，
    车船税目录中的节能型(非插电 HEV/LNG)表与公告附件1 不入库。
    章节标题可能独立成段，也可能作为相邻表格段落的首/尾单元格出现：
    表头前的结构单元格立即生效，数据区之后的延迟到本表行打完标签再生效。
    """
    html = html_path.read_text(encoding="utf-8", errors="ignore")
    body = html[html.find("<body>"):]
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", body, re.S)

    ctx = CatalogTableContext()
    rows: list[dict[str, Any]] = []
    for para in paragraphs:
        if len(CELL_SEP_RE.findall(para)) < 5:
            text = strip_tags(para)
            if text:
                apply_structural_cell(text, ctx)
            continue

        cells = [strip_tags(cell) for cell in CELL_SEP_RE.split(para)]
        cells = [cell for cell in cells if cell]
        index = 0
        while index < len(cells) and is_structural_text(cells[index]):
            apply_structural_cell(cells[index], ctx)
            index += 1
        # 后续结构单元格属于下一张表，攒到本表行打完标签后统一生效；
        # 即使本表不入库(客车/货车等无通用名称)也必须处理，否则章节会错位
        stream: list[str] = []
        deferred: list[str] = []
        for cell in cells[index:]:
            (deferred if is_structural_text(cell) else stream).append(cell)

        headers: list[str] = []
        data_start = 0
        for offset, cell in enumerate(stream):
            if headers and (cell == "1" or looks_like_company(cell) or looks_like_model_code(cell)):
                data_start = offset
                break
            headers.append(cell)
        columns = [map_header(header) for header in headers]
        # 客车/货车/专用车表没有"通用名称"列，同样入库；推荐目录(2020~2022)口径不同，不入库
        qualified = (
            len(headers) >= 5
            and data_start > 0
            and "model_code" in columns
            and any(col in columns for col in ("range_km", "battery_mass", "battery_energy"))
            and "推荐" not in ctx.catalog
        )
        if qualified:
            carry: dict[str, str] = {}
            for row in reconstruct_rows(split_fused_cells(stream[data_start:]), columns, carry):
                extras = {key[6:]: value for key, value in row.items() if key.startswith("extra:")}
                row = {key: value for key, value in row.items() if not key.startswith("extra:")}
                if extras:
                    row["extra"] = extras
                row.update(
                    catalog=ctx.catalog,
                    batch=ctx.batch,
                    part=ctx.part or "第一部分 新车型",
                    energy_type=ctx.energy_type,
                    category=ctx.category,
                )
                rows.append(row)
        for cell in deferred:
            apply_structural_cell(cell, ctx)
    return ctx.catalog, ctx.batch, rows


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    art_id TEXT PRIMARY KEY,
    url TEXT,
    title TEXT,
    pub_date TEXT,
    fetched_at TEXT,
    status TEXT,
    error TEXT,
    row_count INTEGER
);
CREATE TABLE IF NOT EXISTS catalog_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    art_id TEXT,
    attachment TEXT,
    catalog TEXT,
    batch TEXT,
    part TEXT,
    energy_type TEXT,
    category TEXT,
    seq TEXT,
    company TEXT,
    trademark TEXT,
    model_code TEXT,
    common_name TEXT,
    product_name TEXT,
    range_km TEXT,
    fuel_consumption TEXT,
    displacement_ml TEXT,
    curb_mass TEXT,
    battery_mass TEXT,
    battery_energy TEXT,
    remark TEXT,
    extra TEXT
);
CREATE INDEX IF NOT EXISTS idx_rows_common_name ON catalog_rows(common_name);
CREATE INDEX IF NOT EXISTS idx_rows_model_code ON catalog_rows(model_code);
CREATE INDEX IF NOT EXISTS idx_rows_company ON catalog_rows(company);
"""

ROW_COLUMNS = [
    "art_id", "attachment", "catalog", "batch", "part", "energy_type", "category",
    "seq", "company", "trademark", "model_code", "common_name", "product_name",
    "range_km", "fuel_consumption", "displacement_ml", "curb_mass",
    "battery_mass", "battery_energy", "remark", "extra",
]


def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
    for column, decl in (("status", "TEXT"), ("error", "TEXT"), ("row_count", "INTEGER")):
        if column not in existing:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {column} {decl}")
    conn.commit()
    return conn


def store_article(
    conn: sqlite3.Connection,
    article: CatalogArticle,
    parsed: list[tuple[str, list[dict[str, Any]]]],
    *,
    status: str = "ok",
    error: str = "",
) -> int:
    try:
        conn.execute("DELETE FROM catalog_rows WHERE art_id = ?", (article.art_id,))
        count = 0
        for attachment, rows in parsed:
            for row in rows:
                extra_payload = {"raw_cells": row.get("raw_cells", [])}
                extra_payload.update(row.get("extra") or {})
                values = [
                    article.art_id, attachment,
                    row.get("catalog", ""), row.get("batch", ""), row.get("part", ""),
                    row.get("energy_type", ""), row.get("category", ""),
                    row.get("seq", ""), row.get("company", ""), row.get("trademark", ""),
                    row.get("model_code", ""), row.get("common_name", ""), row.get("product_name", ""),
                    row.get("range_km", ""), row.get("fuel_consumption", ""), row.get("displacement_ml", ""),
                    row.get("curb_mass", ""), row.get("battery_mass", ""), row.get("battery_energy", ""),
                    row.get("remark", ""), json.dumps(extra_payload, ensure_ascii=False),
                ]
                conn.execute(
                    f"INSERT INTO catalog_rows ({', '.join(ROW_COLUMNS)}) "
                    f"VALUES ({', '.join('?' * len(ROW_COLUMNS))})",
                    values,
                )
                count += 1
        conn.execute(
            "INSERT OR REPLACE INTO articles (art_id, url, title, pub_date, fetched_at, status, error, row_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                article.art_id, article.url, article.title, article.pub_date,
                time.strftime("%Y-%m-%d %H:%M:%S"), status, error, count,
            ),
        )
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise


def load_existing_parsed(
    conn: sqlite3.Connection, art_id: str
) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    cursor = conn.execute(
        f"SELECT {', '.join(ROW_COLUMNS)} FROM catalog_rows WHERE art_id = ?",
        (art_id,),
    )
    for values in cursor:
        row = dict(zip(ROW_COLUMNS, values))
        attachment = row.pop("attachment", "") or ""
        row.pop("art_id", None)
        extra_raw = row.get("extra") or "{}"
        try:
            extra = json.loads(extra_raw) if isinstance(extra_raw, str) else dict(extra_raw)
        except json.JSONDecodeError:
            extra = {}
        raw_cells = extra.pop("raw_cells", []) if isinstance(extra, dict) else []
        row["raw_cells"] = raw_cells
        row["extra"] = extra if isinstance(extra, dict) else {}
        grouped.setdefault(attachment, []).append(row)
    return list(grouped.items())


def merge_parsed_with_existing(
    conn: sqlite3.Connection,
    art_id: str,
    parsed: list[tuple[str, list[dict[str, Any]]]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    existing = dict(load_existing_parsed(conn, art_id))
    merged: list[tuple[str, list[dict[str, Any]]]] = []
    seen: set[str] = set()
    for attachment, rows in parsed:
        merged.append((attachment, rows))
        seen.add(attachment)
    for attachment, rows in existing.items():
        if attachment not in seen:
            merged.append((attachment, rows))
    return merged


def finalize_article_sync(
    conn: sqlite3.Connection,
    article: CatalogArticle,
    parsed: list[tuple[str, list[dict[str, Any]]]],
    zero_attachments: list[str],
) -> tuple[int, str]:
    """根据本轮解析结果写入库并返回 (行数, ok|zero|skipped)。"""
    existing_count = conn.execute(
        "SELECT COUNT(*) FROM catalog_rows WHERE art_id = ?", (article.art_id,)
    ).fetchone()[0]
    if zero_attachments:
        error = f"附件解析0行: {', '.join(zero_attachments)}"
        if parsed:
            merged = merge_parsed_with_existing(conn, article.art_id, parsed)
            count = store_article(conn, article, merged, status="zero_rows", error=error)
            print(
                f"  [警告] 本次解析不完整，已写入成功附件并保留其余旧行（共 {count} 行）",
                file=sys.stderr,
            )
            return count, "zero"
        if existing_count:
            mark_article(conn, article, status="zero_rows", error=error)
            print(
                f"  [警告] 本次解析不完整，保留库内已有 {existing_count} 行不覆盖",
                file=sys.stderr,
            )
            return existing_count, "zero"
        count = store_article(conn, article, [], status="zero_rows", error=error)
        return count, "zero"
    if parsed:
        return store_article(conn, article, parsed), "ok"
    # 全部是明确的非目录附件：记 ok 以免每周重试，但不假装解析过目录
    mark_article(conn, article, status="ok", error="无目录附件")
    return 0, "skipped"


def mark_article(conn: sqlite3.Connection, article: CatalogArticle, *, status: str, error: str = "") -> None:
    """只记录文章同步状态，不动已入库的 catalog_rows（解析异常时保留旧数据）。"""
    conn.execute(
        "INSERT INTO articles (art_id, url, title, pub_date, fetched_at, status, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(art_id) DO UPDATE SET fetched_at = excluded.fetched_at, "
        "status = excluded.status, error = excluded.error",
        (
            article.art_id, article.url, article.title, article.pub_date,
            time.strftime("%Y-%m-%d %H:%M:%S"), status, error,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def command_sync(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache_dir).expanduser().resolve() if args.cache_dir else CACHE_DIR
    conn = open_db(Path(args.db).expanduser().resolve() if args.db else DB_PATH)
    # 旧库无 status 列(NULL)视为成功；zero_rows/error 状态默认重试
    known = {
        row[0]
        for row in conn.execute("SELECT art_id FROM articles WHERE status IS NULL OR status = 'ok'")
    }

    articles = [a for a in list_articles(max_pages=args.max_pages) if is_catalog_release(a)]
    if args.max_articles:
        articles = articles[: args.max_articles]
    print(f"目录文章: {len(articles)} 篇（已入库 {len(known & {a.art_id for a in articles})} 篇）")

    synced = skipped = failed = zero = 0
    for article in articles:
        if article.art_id in known and not args.force:
            skipped += 1
            continue
        print(f"\n[{article.pub_date}] {article.title}")
        try:
            parsed: list[tuple[str, list[dict[str, Any]]]] = []
            zero_attachments: list[str] = []
            for url in article_attachments(article):
                url_name = Path(url.rsplit("/", 1)[-1]).name
                try:
                    doc_path = download_attachment(url, article, cache_dir, force=args.force)
                except ValueError as exc:
                    print(f"  跳过非法附件名: {exc}", file=sys.stderr)
                    continue
                if doc_path.suffix.lower() not in (".doc", ".docx"):
                    print(f"  跳过暂不支持的附件: {doc_path.name}", file=sys.stderr)
                    continue
                # 先用 textutil 廉价嗅探：仅明确的非目录附件(公告附件/推荐目录)跳过入库
                html_path = doc_to_html(doc_path, force=args.force)
                catalog_hint, batch_hint = "", ""
                if html_path:
                    catalog_hint, batch_hint = peek_catalog_info(html_path)
                    if is_non_catalog_peek(catalog_hint):
                        stem = friendly_attachment_stem(catalog_hint, batch_hint) or stem_from_article_title(
                            catalog_hint, article.title
                        )
                        rename_attachment(doc_path, url_name, stem)
                        continue
                # 空嗅探也继续解析，避免标题靠后的真目录被永久标成 ok/0 行
                catalog, batch, rows = "", "", []
                docx_path = convert_doc_to_docx(doc_path, force=args.force)
                if docx_path:
                    catalog, batch, rows = parse_catalog_docx(docx_path)
                if not rows and html_path:
                    catalog, batch, rows = parse_catalog_html(html_path)
                if rows:
                    doc_path = rename_attachment(
                        doc_path, url_name, friendly_attachment_stem(catalog, batch)
                    )
                    print(f"  {doc_path.name}: {catalog}（第{batch}批）{len(rows)} 行")
                    parsed.append((doc_path.name, rows))
                elif looks_like_failed_catalog(
                    catalog=catalog,
                    catalog_hint=catalog_hint,
                    converted=bool(docx_path),
                ):
                    zero_attachments.append(doc_path.name)
                    print(
                        f"  [警告] {doc_path.name}: {catalog or catalog_hint or '目录附件'} 解析到 0 行，"
                        "本篇标记为 zero_rows，下次 sync 默认重试"
                        + ("（.docx 转换未成功，建议安装 LibreOffice 后重试）" if not docx_path else ""),
                        file=sys.stderr,
                    )
                else:
                    # 全文解析后仍无目录标题：按非目录附件跳过，不把整篇钉在 zero_rows
                    stem = stem_from_article_title(catalog_hint, article.title)
                    rename_attachment(doc_path, url_name, stem)
                    print(f"  跳过非目录附件: {doc_path.name}", file=sys.stderr)
            count, outcome = finalize_article_sync(conn, article, parsed, zero_attachments)
            if outcome == "ok":
                synced += 1
            elif outcome == "zero":
                zero += 1
            else:
                skipped += 1
            print(f"  入库 {count} 行")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            mark_article(conn, article, status="error", error=str(exc))
            print(f"  [失败] {exc}", file=sys.stderr)
    total = conn.execute("SELECT COUNT(*) FROM catalog_rows").fetchone()[0]
    print(
        f"\n同步完成: 新增/更新 {synced} 篇, 跳过 {skipped} 篇, "
        f"解析0行 {zero} 篇, 失败 {failed} 篇; 库内共 {total} 行"
    )
    return 0 if failed == 0 and zero == 0 else 1


def query_rows(conn: sqlite3.Connection, keyword: str, limit: int | None = None) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    like = f"%{keyword}%"
    sql = (
        "SELECT * FROM catalog_rows "
        "WHERE common_name LIKE ? OR model_code LIKE ? OR company LIKE ? OR trademark LIKE ? "
        "ORDER BY CAST(batch AS INTEGER) DESC, catalog, CAST(seq AS INTEGER)"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql, (like, like, like, like)))


def resolve_models(model_codes: Iterable[str]) -> list[dict[str, Any]]:
    """用公告接口按车辆型号反查商标/批次。"""
    resolved: list[dict[str, Any]] = []
    for model_code in model_codes:
        rows = core.query_all_pages(model_code=model_code, page_size=50)
        for row in rows:
            if (row.get("clxh") or "").upper() == model_code.upper():
                resolved.append(row)
    return resolved


def suggest_prefixes(model_codes: Iterable[str]) -> list[str]:
    return sorted({re.sub(r"\d+$", "", code) for code in model_codes if code})


def command_search(args: argparse.Namespace) -> int:
    conn = open_db(Path(args.db).expanduser().resolve() if args.db else DB_PATH)
    rows = query_rows(conn, args.keyword, limit=args.limit)
    if not rows:
        print(f"目录库中未找到 “{args.keyword}”；可先运行 jianmian sync（或加大 --max-pages）。")
        return 1
    print("目录\t批次\t企业名称\t商标\t车辆型号\t通用名称\t产品名称\t续航(km)\t电池能量(kWh)")
    for row in rows:
        print(
            "\t".join(
                [
                    ("购置税" if "购置税" in (row["catalog"] or "") else "车船税"),
                    row["batch"] or "",
                    row["company"] or "",
                    row["trademark"] or "",
                    row["model_code"] or "",
                    row["common_name"] or "",
                    row["product_name"] or "",
                    row["range_km"] or "",
                    row["battery_energy"] or "",
                ]
            )
        )

    model_codes = sorted({row["model_code"] for row in rows if row["model_code"]})
    print(f"\n涉及车辆型号: {', '.join(model_codes)}")
    print(f"建议 model_prefixes: {', '.join(suggest_prefixes(model_codes))}")

    if not args.resolve and not args.download:
        return 0

    resolved = resolve_models(model_codes)
    if not resolved:
        print("公告接口未返回匹配产品。")
        return 1
    if getattr(args, "latest_batch", False) and not getattr(args, "all_batches", False):
        resolved = core.filter_latest_batch(resolved)
    print("\n公告产品:")
    core.print_rows(resolved)
    trademarks = sorted({row.get("cpsb", "") for row in resolved if row.get("cpsb")})
    print(f"公告商标: {', '.join(trademarks)}")
    if getattr(args, "latest_batch", False) and resolved:
        print(f"最新批次: {resolved[0].get('gppc') or resolved[0].get('pc')}")

    if args.download:
        base_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else core.DEFAULT_OUTPUT_DIR
        folder = base_dir / core.safe_part(args.keyword)
        print(f"下载目录: {folder}")
        errors: list[str] = []
        non_pdf: list[str] = []
        ok_pdf_count = 0
        for row in resolved:
            label = f"{row.get('cpsb', '')} {row.get('clxh', '')}".strip()
            try:
                path, is_pdf, _ = core.download_param_page(row, folder)
            except Exception as exc:  # noqa: BLE001
                errors.append(label)
                print(f"下载失败，跳过: {label} ({exc})", file=sys.stderr)
                continue
            print(f"已下载: {path}{'' if is_pdf else ' (非PDF，需人工检查)'}")
            if is_pdf:
                ok_pdf_count += 1
            else:
                non_pdf.append(label)
        if errors:
            print(f"以下 {len(errors)} 条下载失败: {'; '.join(errors)}", file=sys.stderr)
        if non_pdf:
            print(
                f"以下 {len(non_pdf)} 条返回的不是 PDF，已存为 HTML 供人工检查: {'; '.join(non_pdf)}",
                file=sys.stderr,
            )
        code = core.download_exit_code(
            ok_pdf_count=ok_pdf_count, problem_count=len(errors) + len(non_pdf)
        )
        if code == core.EXIT_DOWNLOAD_FAILED:
            print("没有成功下载任何 PDF。", file=sys.stderr)
        elif code == core.EXIT_DOWNLOAD_PARTIAL:
            print(
                f"部分成功：已下载 {ok_pdf_count} 份 PDF，"
                f"另有 {len(errors) + len(non_pdf)} 条需人工检查。",
                file=sys.stderr,
            )
        return code
    return 0


def command_rename(args: argparse.Namespace) -> int:
    """存量附件迁移：按嗅探出的目录批次重命名，并同步更新库内 attachment 字段。"""
    cache_dir = Path(args.cache_dir).expanduser().resolve() if args.cache_dir else CACHE_DIR
    conn = open_db(Path(args.db).expanduser().resolve() if args.db else DB_PATH)
    titles = dict(conn.execute("SELECT art_id, title FROM articles"))
    renamed = skipped = 0
    for folder in sorted(cache_dir.iterdir()):
        if not folder.is_dir():
            continue
        art_id = folder.name.split("_", 1)[0]
        title = titles.get(art_id, "")
        if folder.name == art_id and title:
            # 纯ID目录迁移成可读名：12168 -> 12168_公告第404批
            preferred = cache_dir / article_folder_name(CatalogArticle(art_id, "", title, ""))
            if preferred.name != folder.name and not preferred.exists():
                folder.rename(preferred)
                print(f"目录: {art_id} -> {preferred.name}")
                folder = preferred
        for doc_path in sorted(folder.iterdir()):
            name = doc_path.name
            if doc_path.suffix.lower() not in (".doc", ".docx") or name.endswith(DERIVED_SUFFIXES):
                continue
            catalog = batch = ""
            html_path = doc_to_html(doc_path)
            if html_path:
                catalog, batch = peek_catalog_info(html_path)
            if not batch:
                # textutil 失败或嗅探不到批次：改从已转换的 docx 嗅探
                for suffix in (".lo.docx", ".word.docx"):
                    cached = doc_path.with_suffix(suffix)
                    if cached.exists() and cached.stat().st_size > 0:
                        d_catalog, d_batch = peek_catalog_info_docx(cached)
                        if d_catalog and d_batch:
                            catalog, batch = d_catalog, d_batch
                            break
            stem = friendly_attachment_stem(catalog, batch) or stem_from_article_title(catalog, title)
            new_path = rename_attachment(doc_path, name, stem)
            if new_path == doc_path:
                skipped += 1
                continue
            conn.execute(
                "UPDATE catalog_rows SET attachment = ? WHERE art_id = ? AND attachment = ?",
                (new_path.name, art_id, name),
            )
            renamed += 1
            print(f"{folder.name}: {name} -> {new_path.name}")
    conn.commit()
    print(f"\n重命名 {renamed} 个附件，保持原名 {skipped} 个（非目录附件或嗅探不到批次）")
    return 0


EXPORT_HEADERS = [
    ("catalog", "目录"), ("batch", "目录批次"), ("part", "部分"),
    ("energy_type", "能源类型"), ("category", "车辆类别"), ("seq", "序号"),
    ("company", "企业名称"), ("trademark", "产品商标"), ("model_code", "车辆型号"),
    ("common_name", "通用名称"), ("product_name", "产品名称"),
    ("range_km", "纯电续驶里程(km)"), ("fuel_consumption", "燃料消耗量(L/100km)"),
    ("displacement_ml", "发动机排量(mL)"), ("curb_mass", "整备质量(kg)"),
    ("battery_mass", "电池组总质量(kg)"), ("battery_energy", "电池组总能量(kWh)"),
    ("remark", "备注"), ("art_id", "来源文章ID"),
]


# --by-category 可能产出的全部文件名 + 旧版命名前缀。清理存量时只认这两类名字，
# 避免用户把 --xlsx 指向已有内容的目录时误删无关 Excel
CATEGORY_GROUP_NAMES = (
    "乘用车",
    "客车",
    "货车",
    "专用车",
    "纯电动商用车",
    "插混及燃料电池商用车",
    "其他",
)
EXPORT_BY_CATEGORY_FILENAMES = frozenset(f"{name}.xlsx" for name in CATEGORY_GROUP_NAMES)
LEGACY_BY_CATEGORY_PREFIX = "jianmian_by_category_"


def category_group(category: str) -> str:
    """原始类别(两套目录口径共8种)归并为6组，用于 --by-category 分文件导出。"""
    if "乘用车" in category:
        return "乘用车"
    if category == "纯电动商用车":
        return "纯电动商用车"
    if "商用车" in category:
        return "插混及燃料电池商用车"
    if "客车" in category:
        return "客车"
    if "货车" in category:
        return "货车"
    if "专用车" in category:
        return "专用车"
    return "其他"


def write_xlsx(rows: list[sqlite3.Row], out_path: Path, sheet_title: str) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_title
    sheet.append([label for _, label in EXPORT_HEADERS])
    for row in rows:
        sheet.append([row[key] for key, _ in EXPORT_HEADERS])
    sheet.freeze_panes = "A2"
    core.save_workbook_atomic(workbook, out_path)


def command_export(args: argparse.Namespace) -> int:
    conn = open_db(Path(args.db).expanduser().resolve() if args.db else DB_PATH)
    conn.row_factory = sqlite3.Row
    where, params = "", ()
    if args.keyword:
        like = f"%{args.keyword}%"
        where = "WHERE common_name LIKE ? OR model_code LIKE ? OR company LIKE ?"
        params = (like, like, like)
    rows = list(
        conn.execute(
            f"SELECT * FROM catalog_rows {where} "
            "ORDER BY catalog, CAST(batch AS INTEGER), part, energy_type, category, CAST(seq AS INTEGER)",
            params,
        )
    )
    if not rows:
        print("没有可导出的数据；请先运行 jianmian sync。")
        return 1

    if args.by_category:
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault(category_group(row["category"]), []).append(row)
        out_dir = Path(args.xlsx).expanduser().resolve() if args.xlsx else EXPORT_BY_CATEGORY_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        keep_names = {f"{name}.xlsx" for name in groups}
        # 只清理本工具自己会产出的文件名（当前分类名 + 旧版 jianmian_by_category_ 前缀），
        # --xlsx 可指向任意目录，目录里的其他 Excel 一律不碰
        for stale_path in out_dir.glob("*.xlsx"):
            if stale_path.name in keep_names or not stale_path.is_file():
                continue
            if stale_path.name in EXPORT_BY_CATEGORY_FILENAMES or stale_path.name.startswith(
                LEGACY_BY_CATEGORY_PREFIX
            ):
                stale_path.unlink()
        for name, group_rows in sorted(groups.items(), key=lambda item: -len(item[1])):
            out_path = out_dir / f"{name}.xlsx"
            write_xlsx(group_rows, out_path, name)
            print(f"已导出 {len(group_rows)} 行: {out_path}")
        return 0

    out_path = Path(args.xlsx).expanduser().resolve() if args.xlsx else EXPORT_XLSX_PATH
    write_xlsx(rows, out_path, "减免税目录车型")
    print(f"已导出 {len(rows)} 行: {out_path}")
    return 0


def register_subcommands(subparsers: argparse._SubParsersAction) -> None:
    jm_parser = subparsers.add_parser("jianmian", help="减免购置税/车船税目录：同步、检索、导出")
    jm_sub = jm_parser.add_subparsers(dest="jm_command", required=True)

    sync_parser = jm_sub.add_parser("sync", help="抓取目录文章附件并解析入库")
    sync_parser.add_argument("--max-pages", type=int, default=10, help="文章列表最多翻页数，默认 10（每页 25 篇）")
    sync_parser.add_argument("--max-articles", type=int, help="最多处理的目录文章数（新→旧）")
    sync_parser.add_argument("--force", action="store_true", help="已入库文章也重新下载并作废转换缓存后解析")
    sync_parser.add_argument("--cache-dir", help=f"附件缓存目录，默认 {CACHE_DIR}")
    sync_parser.add_argument("--db", help=f"SQLite 路径，默认 {DB_PATH}")
    sync_parser.set_defaults(func=command_sync)

    search_parser = jm_sub.add_parser("search", help="按通用名称/型号/企业/商标检索目录库")
    search_parser.add_argument("keyword", help="检索词，例如 <市场名>")
    search_parser.add_argument("--resolve", action="store_true", help="用公告接口按型号反查商标与批次")
    search_parser.add_argument("--download", action="store_true", help="反查后下载公告参数页 PDF（隐含 --resolve）")
    search_parser.add_argument("--latest-batch", action="store_true", help="--resolve/--download 时只保留最高公告批次")
    search_parser.add_argument("--all-batches", action="store_true", help="下载全部历史批次（覆盖 --latest-batch）")
    search_parser.add_argument("--limit", type=core.positive_int, help="限制展示条数")
    search_parser.add_argument("--output-dir", help=f"--download 时的下载根目录，默认 {core.DEFAULT_OUTPUT_DIR}")
    search_parser.add_argument("--db", help=f"SQLite 路径，默认 {DB_PATH}")
    search_parser.set_defaults(func=command_search)

    rename_parser = jm_sub.add_parser("rename", help="把缓存附件按目录批次重命名（含派生缓存与库内字段）")
    rename_parser.add_argument("--cache-dir", help=f"附件缓存目录，默认 {CACHE_DIR}")
    rename_parser.add_argument("--db", help=f"SQLite 路径，默认 {DB_PATH}")
    rename_parser.set_defaults(func=command_rename)

    export_parser = jm_sub.add_parser("export", help="导出目录库为 Excel")
    export_parser.add_argument("--keyword", help="只导出匹配通用名称/型号/企业的行")
    export_parser.add_argument(
        "--by-category",
        action="store_true",
        help="按车辆类别(6组)分文件导出；输出目录中同名的旧分类文件会被覆盖或清理，其他文件不受影响",
    )
    export_parser.add_argument("--xlsx", help="输出文件/目录路径，默认固定到 output/jianmian_catalog.xlsx 或 output/jianmian_by_category/")
    export_parser.add_argument("--db", help=f"SQLite 路径，默认 {DB_PATH}")
    export_parser.set_defaults(func=command_export)
