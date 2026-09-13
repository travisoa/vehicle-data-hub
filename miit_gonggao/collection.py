#!/usr/bin/env python3
"""统一采集公告参数页、解析字段和下载记录，供网站只读构建派生库。

默认种子来自减免车辆购置税目录最新批次的乘用车条目；目录顺序确定，因而每次可复现。
每个公告型号会查询所有正式公告批次。PDF 路径按“产品商标/目录车型/公告批次”分类，
数据库保留目录来源、公告接口原始记录、PDF 校验结果以及坐标解析后的公告参数。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_ROOT = Path(os.environ.get(
    "VEHICLE_DATA_HUB_ROOT", str(Path(__file__).resolve().parent.parent)
)).expanduser().resolve()
if not (UPSTREAM_ROOT / "miit_gonggao").is_dir():
    raise SystemExit(
        "未找到上游 vehicle-data-hub。请设置 VEHICLE_DATA_HUB_ROOT 指向其项目根目录。"
    )
for _path in (UPSTREAM_ROOT, Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from miit_gonggao import change_notice, core  # noqa: E402
from miit_gonggao.review_export import lookup_catalog, parse_gonggao_pdf  # noqa: E402

CATALOG_NAME = "减免车辆购置税的新能源汽车车型目录"
DEFAULT_CATALOG_DB = UPSTREAM_ROOT / "data" / "jianmian_catalog.sqlite"
DEFAULT_SITE_DB = UPSTREAM_ROOT / "data" / "announcement_site.sqlite"
# 下载记录与 PDF 均由本项目维护，relative_path 相对 UPSTREAM_ROOT。
DEFAULT_DOWNLOAD_ROOT = UPSTREAM_ROOT / "downloads" / "announcement_site"


@contextmanager
def database_lock(site_db: Path):
    """所有采集命令共用同一真实数据库路径的排他锁。"""
    site_db = site_db.expanduser().resolve()
    lock_path = site_db.with_name(site_db.name + ".ingestion.lock")
    with lock_path.open("a+b") as handle:
        try:
            if os.name == "nt":
                if lock_path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"另一个采集进程持有锁：{lock_path}") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def begin_model(conn: sqlite3.Connection, run_id: int, order: int, vehicle_id: int) -> None:
    conn.execute(
        "INSERT INTO run_models(run_id,sort_order,vehicle_id,status,error) VALUES (?,?,?,'querying','') "
        "ON CONFLICT(run_id,vehicle_id) DO UPDATE SET status='querying',error='',sort_order=excluded.sort_order",
        (run_id, order, vehicle_id),
    )
    conn.commit()


def finish_model(conn: sqlite3.Connection, run_id: int, vehicle_id: int, status: str, error: str = "") -> None:
    conn.execute("UPDATE run_models SET status=?,error=? WHERE run_id=? AND vehicle_id=?",
                 (status, error, run_id, vehicle_id))
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_id: int, counts: dict, reason: str = "") -> int:
    """统一记录阶段统计与中断结局；完成时间不代表所有产品成功。"""
    columns = ("query_failures", "download_failures", "non_pdf_documents", "awaiting_effective",
               "parse_failures", "publish_failures")
    interrupted = conn.execute(
        "UPDATE run_models SET status='interrupted',error=COALESCE(NULLIF(error,''),?) "
        "WHERE run_id=? AND status='querying'",
        (reason or "本轮采集中断，未完成该型号", run_id),
    ).rowcount
    conn.execute("UPDATE ingestion_runs SET completed_at=?, " + ",".join(f"{key}=?" for key in columns)
                 + " WHERE id=?", [utc_now(), *(counts.get(key, 0) for key in columns), run_id])
    conn.commit()
    return interrupted


def refresh_collection_status(db_path: Path | None, *, catalog_db: Path, pdf_root: Path,
                              run_id: int | None = None, reason: str = "collection") -> None:
    """已提交批次的统计收尾；失败不得掩盖采集结果或原始中断异常。

    只在批次收尾调用，不放入逐产品下载事务。无文件的内存库没有持久快照。
    """
    if db_path is None:
        return
    try:
        from miit_gonggao.collection_status import refresh_after_collection

        refresh_after_collection(db_path, catalog_db=catalog_db, pdf_root=pdf_root,
                                 run_id=run_id, reason=reason)
    except Exception as exc:
        print(f"收录统计更新失败（采集记录已保留，旧统计可能过期）：{type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    selector_json TEXT NOT NULL,
    selected_models INTEGER NOT NULL,
    query_failures INTEGER NOT NULL DEFAULT 0,
    download_failures INTEGER NOT NULL DEFAULT 0,
    non_pdf_documents INTEGER NOT NULL DEFAULT 0,
    awaiting_effective INTEGER NOT NULL DEFAULT 0,
    parse_failures INTEGER NOT NULL DEFAULT 0,
    publish_failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS brands (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY,
    market_name TEXT NOT NULL,
    announcement_model_code TEXT NOT NULL UNIQUE,
    catalog_name TEXT NOT NULL,
    catalog_batch TEXT NOT NULL,
    catalog_category TEXT NOT NULL,
    catalog_seq TEXT,
    catalog_company TEXT
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    batch TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY,
    source_product_id TEXT NOT NULL UNIQUE,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
    brand_id INTEGER REFERENCES brands(id),
    batch_id INTEGER REFERENCES batches(id),
    company TEXT,
    trademark TEXT,
    model_code TEXT NOT NULL,
    product_name TEXT,
    raw_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    announcement_id INTEGER NOT NULL UNIQUE REFERENCES announcements(id),
    relative_path TEXT NOT NULL UNIQUE,
    is_pdf INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT,
    downloaded_at TEXT NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS announcement_fields (
    announcement_id INTEGER PRIMARY KEY REFERENCES announcements(id),
    fields_json TEXT NOT NULL,
    parse_error TEXT
);
CREATE TABLE IF NOT EXISTS run_models (
    run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    sort_order INTEGER NOT NULL,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    PRIMARY KEY (run_id, vehicle_id)
);
CREATE INDEX IF NOT EXISTS idx_announcements_vehicle ON announcements(vehicle_id);
CREATE INDEX IF NOT EXISTS idx_announcements_brand ON announcements(brand_id);
CREATE INDEX IF NOT EXISTS idx_announcements_batch ON announcements(batch_id);
CREATE INDEX IF NOT EXISTS idx_vehicles_market_name ON vehicles(market_name);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """创建业务库表，并为既有数据库补齐可向后兼容的运行状态列。"""
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ingestion_runs)")}
    for column in ("awaiting_effective", "parse_failures", "publish_failures"):
        if column not in columns:
            # 历史轮次没有分项统计，NULL 表示未知，不能伪造为零。
            definition = "INTEGER NOT NULL DEFAULT 0" if column == "awaiting_effective" else "INTEGER"
            conn.execute(f"ALTER TABLE ingestion_runs ADD COLUMN {column} {definition}")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_part(value: object) -> str:
    return core.safe_part(str(value or "未分类"))


def latest_catalog_batch(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT batch FROM catalog_rows WHERE catalog = ? "
        "ORDER BY CAST(batch AS INTEGER) DESC LIMIT 1",
        (CATALOG_NAME,),
    ).fetchone()
    if not row:
        raise RuntimeError(f"目录库中没有 {CATALOG_NAME}；请先运行 jianmian sync")
    return str(row[0])


def select_models(
    catalog_db: Path,
    limit: int,
    catalog_batch: str | None = None,
    *,
    offset: int = 0,
    all_categories: bool = False,
    through_earlier_batches: bool = False,
    site_db: Path | None = None,
    exclude_existing: bool = False,
) -> tuple[str, list[dict[str, str]]]:
    conn = sqlite3.connect(catalog_db)
    conn.row_factory = sqlite3.Row
    try:
        batch = catalog_batch or latest_catalog_batch(conn)
        category_clause = "" if all_categories else "AND category = '乘用车'"
        batch_clause = "CAST(batch AS INTEGER) <= CAST(? AS INTEGER)" if through_earlier_batches else "batch = ?"
        existing_clause = ""
        if exclude_existing and site_db and site_db.exists():
            conn.execute("ATTACH DATABASE ? AS announcement_site", (str(site_db),))
            existing_clause = (
                "AND NOT EXISTS (SELECT 1 FROM announcement_site.vehicles existing "
                "WHERE existing.announcement_model_code = catalog_rows.model_code)"
            )
        rows = conn.execute(
            "WITH candidates AS ("
            "  SELECT id, catalog, batch, category, seq, company, trademark, model_code, common_name, "
            "         ROW_NUMBER() OVER (PARTITION BY model_code "
            "             ORDER BY CAST(batch AS INTEGER) DESC, id) AS duplicate_rank "
            f"  FROM catalog_rows WHERE catalog = ? AND {batch_clause} AND model_code <> '' "
            f"  {category_clause}"
            f"  {existing_clause}"
            ") "
            "SELECT id, catalog, batch, category, seq, company, trademark, model_code, common_name "
            "FROM candidates WHERE duplicate_rank = 1 "
            "ORDER BY CAST(batch AS INTEGER) DESC, "
            "CASE category WHEN '乘用车' THEN 0 WHEN '专用车' THEN 1 WHEN '客车' THEN 2 "
            "WHEN '货车' THEN 3 ELSE 4 END, CAST(seq AS INTEGER), id LIMIT ? OFFSET ?",
            (CATALOG_NAME, batch, limit, offset),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        scope = f"第{batch}批及更早目录" if through_earlier_batches else f"第{batch}批目录"
        raise RuntimeError(f"{scope}中没有符合条件且未跳过的公告型号")
    label = f"{batch}及更早" if through_earlier_batches else batch
    return label, [{key: str(row[key] or "") for key in row.keys()} for row in rows]


def select_models_from_file(
    catalog_db: Path, list_path: Path, limit: int | None = None
) -> tuple[str, list[dict[str, str]]]:
    """按外部型号清单选取候选，字段与 select_models 对齐。

    清单一行一个车辆型号，`#` 开头为注释——正是 build_site_db.py 产出的
    catalog_gap_*.txt 的格式，让「盘点缺口 -> 补采」成为闭环。
    """
    wanted: list[str] = []
    seen: set[str] = set()
    for line in list_path.read_text(encoding="utf-8").splitlines():
        code = line.strip().upper()
        if not code or code.startswith("#") or code in seen:
            continue
        seen.add(code)
        wanted.append(code)
    if not wanted:
        raise RuntimeError(f"清单里没有可用的车辆型号：{list_path}")

    conn = sqlite3.connect(catalog_db)
    conn.row_factory = sqlite3.Row
    try:
        found: dict[str, dict[str, str]] = {}
        for start in range(0, len(wanted), 500):  # 分批，避免 SQL 变量数超限
            chunk = wanted[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                "WITH candidates AS (SELECT id, catalog, batch, category, seq, company, trademark, "
                "   model_code, common_name, ROW_NUMBER() OVER (PARTITION BY UPPER(model_code) "
                "     ORDER BY CAST(batch AS INTEGER) DESC, id) AS duplicate_rank "
                f"  FROM catalog_rows WHERE UPPER(model_code) IN ({placeholders})) "
                "SELECT id, catalog, batch, category, seq, company, trademark, model_code, common_name "
                "FROM candidates WHERE duplicate_rank = 1",
                chunk,
            ):
                found[str(row["model_code"] or "").upper()] = {k: str(row[k] or "") for k in row.keys()}
    finally:
        conn.close()

    missing = [code for code in wanted if code not in found]
    if missing:
        print(f"清单中 {len(missing)} 个型号不在目录库里，已跳过：{', '.join(missing[:5])}"
              f"{' ...' if len(missing) > 5 else ''}", file=sys.stderr)
    selected = [found[code] for code in wanted if code in found]
    if not selected:
        raise RuntimeError(f"清单里的型号在目录库中一个都没找到：{list_path}")
    if limit:
        selected = selected[:limit]
    return f"清单{list_path.name}", selected


def derive_catalog_category(model_code: str, product_name: str) -> str:
    """在变更公示型号不属于新能源目录时，用公告型号类别码提供最小分类。"""
    match = re.match(r"^[A-Za-z]+(\d)", model_code or "")
    if not match:
        return "未分类"
    code = match.group(1)
    if code in {"2", "7"}:
        return "乘用车"
    if code == "6":
        return "客车" if "客车" in product_name else "乘用车"
    if code in {"1", "3", "4"}:
        return "货车"
    if code == "5":
        return "专用车"
    return "未分类"


def _catalog_item(catalog_db: Path, model_code: str) -> dict[str, str] | None:
    # 用 closing 而不是 `with sqlite3.connect(...)`：后者只提交或回滚事务，从不关闭
    # 连接；本函数在变更公示模式下逐行调用，泄漏的连接与文件描述符会一直累积。
    with closing(sqlite3.connect(catalog_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT catalog, batch, category, seq, company, trademark, model_code, common_name "
            "FROM catalog_rows WHERE UPPER(model_code) = UPPER(?) "
            "ORDER BY CAST(batch AS INTEGER) DESC, id DESC LIMIT 1",
            (model_code,),
        ).fetchone()
    if not row:
        return None
    return {key: str(row[key] or "") for key in row.keys()}


def _existing_vehicle_item(site_db: Path, model_code: str) -> dict[str, str] | None:
    if not site_db.exists():
        return None
    try:
        with closing(sqlite3.connect(site_db)) as conn:
            row = conn.execute(
                "SELECT market_name, catalog_name, catalog_batch, catalog_category, "
                "catalog_seq, catalog_company FROM vehicles "
                "WHERE UPPER(announcement_model_code) = UPPER(?)",
                (model_code,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return {
        "common_name": str(row[0] or ""),
        "catalog": str(row[1] or ""),
        "batch": str(row[2] or ""),
        "category": str(row[3] or ""),
        "seq": str(row[4] or ""),
        "company": str(row[5] or ""),
        "trademark": "",
        "model_code": model_code,
    }


def select_change_notice_models(
    catalog_db: Path,
    site_db: Path,
    notice_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    """把变更扩展公示行转为采集候选；已有型号也必须保留并强制刷新。"""
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for notice in notice_rows:
        model_code = str(notice.get("model_code") or "").strip()
        model_key = model_code.upper()
        if not model_code or model_key in seen:
            continue
        seen.add(model_key)
        base = _catalog_item(catalog_db, model_code) or _existing_vehicle_item(
            site_db, model_code
        ) or {
            "catalog": "变更扩展公示",
            "batch": str(notice.get("notice_batch") or ""),
            "category": derive_catalog_category(
                model_code, str(notice.get("product_name") or "")
            ),
            "seq": "",
            "company": str(notice.get("company") or ""),
            "trademark": str(notice.get("trademark") or ""),
            "model_code": model_code,
            "common_name": model_code,
        }
        item = dict(base)
        item.update(
            {
                "model_code": model_code,
                "notice_batch": str(notice.get("notice_batch") or ""),
                "notice_title": str(notice.get("notice_title") or ""),
                "notice_detail_url": str(notice.get("detail_url") or ""),
                "notice_company": str(notice.get("company") or ""),
                "notice_trademark": str(notice.get("trademark") or ""),
                "notice_product_name": str(notice.get("product_name") or ""),
            }
        )
        selected.append(item)
    return selected


def batch_is_at_least(actual: str, expected: str) -> bool:
    """判断正式公告批次是否已达到公示批次；非数字批次仅接受完全相等。"""
    actual = actual.strip()
    expected = expected.strip()
    if actual.isdigit() and expected.isdigit():
        return int(actual) >= int(expected)
    return bool(actual and expected and actual == expected)


def write_change_notice_snapshot(
    source: change_notice.ChangeNoticeSource,
    *,
    total: int,
    rows: list[dict[str, str]],
    selected: list[dict[str, str]],
    output_root: Path,
    filters: dict[str, str],
) -> Path:
    snapshot_dir = output_root / core.ANNOUNCEMENT_SNAPSHOT_DIRNAME
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = snapshot_dir / f"change_notice_seed_batch{safe_part(source.batch)}_{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "source": {
                    "notice_url": source.notice_url,
                    "title": source.title,
                    "published_at": source.published_at,
                    "batch": source.batch,
                    "iframe_url": source.iframe_url,
                },
                "filters": filters,
                "total": total,
                "returned": len(rows),
                "selected_models": selected,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def get_or_create_id(conn: sqlite3.Connection, table: str, value: str, column: str = "name") -> int:
    conn.execute(f"INSERT OR IGNORE INTO {table}({column}) VALUES (?)", (value,))
    row = conn.execute(f"SELECT id FROM {table} WHERE {column} = ?", (value,)).fetchone()
    assert row is not None
    return int(row[0])


def upsert_vehicle(conn: sqlite3.Connection, item: dict[str, str]) -> int:
    conn.execute(
        "INSERT INTO vehicles(market_name, announcement_model_code, catalog_name, catalog_batch, "
        "catalog_category, catalog_seq, catalog_company) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(announcement_model_code) DO UPDATE SET market_name=excluded.market_name, "
        "catalog_name=excluded.catalog_name, catalog_batch=excluded.catalog_batch, "
        "catalog_category=excluded.catalog_category, catalog_seq=excluded.catalog_seq, "
        "catalog_company=excluded.catalog_company",
        (
            item["common_name"], item["model_code"], item["catalog"], item["batch"],
            item["category"], item["seq"], item["company"],
        ),
    )
    return int(
        conn.execute(
            "SELECT id FROM vehicles WHERE announcement_model_code = ?", (item["model_code"],)
        ).fetchone()[0]
    )


def source_product_id(row: dict[str, Any]) -> str:
    value = str(row.get("cpid") or row.get("gid") or "").strip()
    if not value:
        raise ValueError(f"公告接口返回记录缺少产品 ID：{row.get('clxh', '')}")
    return value


def parse_pdf(pdf_path: Path, catalog_db: Path) -> tuple[dict[str, str], str]:
    try:
        fields = parse_gonggao_pdf(pdf_path)
        for key, value in lookup_catalog(catalog_db, fields.get("model_code", "")).items():
            fields.setdefault(key, value)
        return fields, ""
    except Exception as exc:  # 单份版式异常不影响其余公告入库
        return {}, f"{type(exc).__name__}: {exc}"


def relative_pdf_path(path: Path, pdf_root: Path) -> str:
    """业务库中的文档路径始终相对上游根，拒绝产生跨根或绝对路径。"""
    resolved_path = path.expanduser().resolve()
    resolved_root = pdf_root.expanduser().resolve()
    try:
        return resolved_path.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"下载文件不在上游根目录内：{resolved_path}") from exc


def store_announcement(
    conn: sqlite3.Connection,
    *,
    row: dict[str, Any],
    vehicle_id: int,
    market_name: str,
    download_root: Path,
    pdf_root: Path,
    catalog_db: Path,
) -> tuple[bool, str]:
    product_id = source_product_id(row)
    trademark = str(row.get("cpsb") or "未标注商标")
    batch = str(row.get("gppc") or row.get("pc") or "未标注批次")
    previous = conn.execute(
        "SELECT a.id FROM announcements a LEFT JOIN documents d ON d.announcement_id=a.id "
        "LEFT JOIN announcement_fields f ON f.announcement_id=a.id "
        "WHERE a.source_product_id=? AND (d.bytes>0 OR d.is_pdf=1 "
        "OR COALESCE(f.fields_json, '{}') <> '{}')", (product_id,),
    ).fetchone()
    folder = core.build_announcement_download_dir(
        download_root, trademark=trademark, vehicle_folder=market_name, batch=batch
    )
    snapshots = download_root / core.ANNOUNCEMENT_SNAPSHOT_DIRNAME
    snapshots.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="ingest-", dir=snapshots))
    keep_staging = False
    try:
        fields: dict[str, str] = {}
        parse_error = ""
        digest = None
        is_pdf = False
        byte_count = 0
        relative_path = f"failed/{safe_part(product_id)}"
        error = ""
        try:
            path, is_pdf, byte_count = core.download_param_page(row, staging)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception as exc:
            error = f"下载失败：{type(exc).__name__}: {exc}"
            is_pdf, byte_count, digest = False, 0, None
        else:
            if is_pdf:
                try:
                    fields, parse_error = parse_pdf(path, catalog_db)
                except Exception as exc:
                    parse_error = f"{type(exc).__name__}: {exc}"
                if parse_error:
                    error = f"解析失败：{parse_error}"
            else:
                error = "接口返回非 PDF，已存为 HTML"
            # 所有完整下载都发布到上游规范目录；解析失败不改变原文件的 PDF 身份。
            try:
                folder.mkdir(parents=True, exist_ok=True)
                destination = folder / path.name
                if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                    # 保留末段产品 ID，供归档脚本识别；绝不覆盖旧站点库引用的文件。
                    destination = folder / f"{digest}_{path.name}"
                try:
                    os.link(path, destination)
                except FileExistsError:
                    if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                        raise RuntimeError("文档目标已存在且内容不同")
                relative_path = relative_pdf_path(destination, pdf_root)
                if error:
                    error = f"{error}；原文件：{relative_path}"
            except Exception as exc:
                # 发布失败不能把暂存路径作为正式文档写入；保留原文件供恢复并记录位置。
                keep_staging = True
                retained = relative_pdf_path(path, pdf_root)
                return False, f"发布失败：{type(exc).__name__}: {exc}；原文件保留于 {retained}"

        if previous and error:
            # 失败重采不改写任何旧版本，包括 HTML 和仅回填了目录字段的记录。
            return False, error

        conn.execute("SAVEPOINT store_announcement")
        try:
            brand_id = get_or_create_id(conn, "brands", trademark)
            batch_id = get_or_create_id(conn, "batches", batch, "batch")
            conn.execute(
                "INSERT INTO announcements(source_product_id, vehicle_id, brand_id, batch_id, company, trademark, "
                "model_code, product_name, raw_json, first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_product_id) DO UPDATE SET vehicle_id=excluded.vehicle_id, "
                "brand_id=excluded.brand_id, batch_id=excluded.batch_id, company=excluded.company, "
                "trademark=excluded.trademark, model_code=excluded.model_code, "
                "product_name=excluded.product_name, raw_json=excluded.raw_json",
                (product_id, vehicle_id, brand_id, batch_id, str(row.get("qymc") or ""), trademark,
                 str(row.get("clxh") or ""), str(row.get("clmc") or ""),
                 json.dumps(row, ensure_ascii=False, sort_keys=True), utc_now()),
            )
            announcement_id = int(conn.execute(
                "SELECT id FROM announcements WHERE source_product_id=?", (product_id,),
            ).fetchone()[0])
            conn.execute(
                "INSERT INTO documents(announcement_id, relative_path, is_pdf, bytes, sha256, downloaded_at, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(announcement_id) DO UPDATE SET "
                "relative_path=excluded.relative_path, is_pdf=excluded.is_pdf, bytes=excluded.bytes, "
                "sha256=excluded.sha256, downloaded_at=excluded.downloaded_at, error=excluded.error",
                (announcement_id, relative_path, int(is_pdf), byte_count, digest, utc_now(), error),
            )
            if is_pdf:
                conn.execute(
                    "INSERT INTO announcement_fields(announcement_id, fields_json, parse_error) VALUES (?, ?, ?) "
                    "ON CONFLICT(announcement_id) DO UPDATE SET fields_json=excluded.fields_json, "
                    "parse_error=excluded.parse_error",
                    (announcement_id, json.dumps(fields, ensure_ascii=False, sort_keys=True), parse_error),
                )
        except BaseException as exc:
            # RAISE(ROLLBACK) 等错误可能已经撤销整个事务；清理不能掩盖原始异常。
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK TO store_announcement")
                    conn.execute("RELEASE store_announcement")
                except sqlite3.Error as cleanup_error:
                    exc.add_note(f"保存点清理失败：{cleanup_error}")
            raise
        else:
            conn.execute("RELEASE store_announcement")
        return is_pdf and not error, error
    finally:
        if not keep_staging:
            try:
                shutil.rmtree(staging)
            except OSError as exc:
                print(f"暂存目录清理失败：{staging}: {exc}", file=sys.stderr)


def backfill_catalog_fields(conn: sqlite3.Connection, catalog_db: Path) -> int:
    """把已有公告页的目录补充字段合并进同一 JSON，便于网站只读一个参数入口。"""
    updated = 0
    rows = conn.execute(
        "SELECT f.announcement_id, f.fields_json, a.model_code FROM announcement_fields f "
        "JOIN announcements a ON a.id = f.announcement_id"
    ).fetchall()
    for announcement_id, fields_json, model_code in rows:
        fields = json.loads(fields_json)
        changed = False
        for key, value in lookup_catalog(catalog_db, model_code).items():
            if value and not fields.get(key):
                fields[key] = value
                changed = True
        if changed:
            conn.execute(
                "UPDATE announcement_fields SET fields_json = ? WHERE announcement_id = ?",
                (json.dumps(fields, ensure_ascii=False, sort_keys=True), announcement_id),
            )
            updated += 1
    return updated


def catalog_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="固定产品清单模式：main.py gonggao collect --manifest <路径> --help。默认校验，--apply 才下载。",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="限制处理的公告型号数；目录模式默认 100，变更扩展公示模式默认不限制",
    )
    parser.add_argument(
        "--catalog-batch",
        help="指定减免购置税目录批次；公告产品接口尚未同步最新目录时，传入可查询的上一批",
    )
    parser.add_argument(
        "--through-earlier-batches",
        action="store_true",
        help="从指定批次起向更早批次连续选取，用于凑足一个跨批次大组",
    )
    parser.add_argument(
        "--exclude-existing",
        action="store_true",
        help="跳过网站数据库中已经处理过的公告型号，避免跨目录批次重复采集",
    )
    parser.add_argument("--offset", type=int, default=0, help="跳过候选池开头的型号数，用于续跑下一批")
    parser.add_argument(
        "-f", "--from-file", type=Path,
        help="按型号清单采集（一行一个车辆型号，# 开头为注释）；"
             "可直接用 build_site_db.py 产出的 dist/catalog_gap_*.txt",
    )
    parser.add_argument("--all-categories", action="store_true", help="覆盖乘用车、专用车、客车和货车")
    parser.add_argument(
        "--change-notice-url",
        help="改用工信部变更扩展公示作为强制刷新清单；正式公告未达到公示批次时保持待生效",
    )
    parser.add_argument("--company", help="变更扩展公示企业名称筛选")
    parser.add_argument("--trademark", help="变更扩展公示产品商标筛选")
    parser.add_argument("--product-name", help="变更扩展公示产品名称筛选")
    parser.add_argument("--model-code", help="变更扩展公示产品型号筛选")
    parser.add_argument(
        "--all",
        dest="change_all",
        action="store_true",
        help="允许遍历整批变更扩展公示；无筛选条件时必须显式指定",
    )
    parser.add_argument(
        "--notice-page-size", type=int, default=100, help="变更扩展公示查询每页条数，默认 100"
    )
    parser.add_argument("--catalog-db", type=Path, default=DEFAULT_CATALOG_DB)
    parser.add_argument("--site-db", type=Path, default=DEFAULT_SITE_DB)
    parser.add_argument("--report-dir", type=Path,
                        help="采集报告输出目录，默认 var/reports/（用自定义 --site-db 时跟随该库）")
    parser.add_argument("--no-report", action="store_true", help="跳过本轮的 Markdown 采集报告")
    parser.add_argument("--download-root", type=Path, default=DEFAULT_DOWNLOAD_ROOT)
    args = parser.parse_args(argv)
    if (args.limit is not None and args.limit < 1) or args.offset < 0 or args.notice_page_size < 1:
        parser.error("--limit 和 --notice-page-size 必须大于 0，--offset 不得小于 0")
    if not args.catalog_db.exists():
        parser.error(f"目录数据库不存在：{args.catalog_db}")
    args.catalog_db = args.catalog_db.expanduser().resolve()
    args.site_db = args.site_db.expanduser().resolve()
    args.download_root = args.download_root.expanduser().resolve()
    try:
        args.download_root.relative_to(UPSTREAM_ROOT)
    except ValueError:
        parser.error(f"--download-root 必须位于上游项目内：{UPSTREAM_ROOT}")

    args.site_db.parent.mkdir(parents=True, exist_ok=True)
    with database_lock(args.site_db):
        if args.site_db.exists():
            with closing(sqlite3.connect(f"file:{args.site_db}?mode=ro", uri=True)) as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='ingestion_runs'").fetchone():
                    if conn.execute("SELECT 1 FROM ingestion_runs WHERE completed_at IS NULL").fetchone():
                        raise RuntimeError("存在未完成采集，不能另起一轮")
        return _collect_catalog(args, parser)


def _collect_catalog(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    change_filters = {
        "company": args.company or "",
        "trademark": args.trademark or "",
        "product_name": args.product_name or "",
        "model_code": args.model_code or "",
    }
    change_mode = bool(args.change_notice_url)
    if not change_mode and (any(change_filters.values()) or args.change_all):
        parser.error("变更扩展公示筛选参数必须与 --change-notice-url 一起使用")
    if change_mode and args.exclude_existing:
        parser.error("变更扩展公示模式会强制刷新已有型号，不能与 --exclude-existing 同时使用")
    if change_mode and not any(change_filters.values()) and not args.change_all:
        parser.error("遍历整批变更扩展公示时必须显式使用 --all")

    notice_source: change_notice.ChangeNoticeSource | None = None
    notice_total = 0
    notice_rows: list[dict[str, str]] = []
    if change_mode:
        notice_source = change_notice.load_change_notice_source(args.change_notice_url)
        notice_rows, notice_total = change_notice.query_change_notice(
            notice_source,
            **change_filters,
            page_size=args.notice_page_size,
            limit=args.limit,
        )
        selected = select_change_notice_models(args.catalog_db, args.site_db, notice_rows)
        if not selected:
            parser.error("变更扩展公示查询没有返回可采集的精确产品型号")
        batch = f"变更扩展公示{notice_source.batch}"
    elif args.from_file:
        batch, selected = select_models_from_file(args.catalog_db, args.from_file, args.limit)
    else:
        batch, selected = select_models(
            args.catalog_db,
            args.limit or 100,
            args.catalog_batch,
            offset=args.offset,
            all_categories=args.all_categories,
            through_earlier_batches=args.through_earlier_batches,
            site_db=args.site_db,
            exclude_existing=args.exclude_existing,
        )
    args.site_db.parent.mkdir(parents=True, exist_ok=True)
    args.download_root.mkdir(parents=True, exist_ok=True)
    if notice_source:
        selection_path = write_change_notice_snapshot(
            notice_source,
            total=notice_total,
            rows=notice_rows,
            selected=selected,
            output_root=args.download_root,
            filters=change_filters,
        )
    else:
        scope = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"batch{batch}".replace("及更早", "_and_earlier"))
        selection_path = args.download_root / f"seed_{scope}_{len(selected)}_offset{args.offset}.json"
        selection_path.write_text(
            json.dumps(
                {
                    "catalog": CATALOG_NAME,
                    "catalog_batch": batch,
                    "through_earlier_batches": args.through_earlier_batches,
                    "exclude_existing": args.exclude_existing,
                    "category": "全部" if args.all_categories else "乘用车",
                    "offset": args.offset,
                    "models": selected,
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    conn = sqlite3.connect(args.site_db)
    ensure_schema(conn)
    selector = {
        "mode": "change_notice" if change_mode else "catalog",
        "catalog": CATALOG_NAME,
        "catalog_batch": batch,
        "through_earlier_batches": args.through_earlier_batches,
        "exclude_existing": args.exclude_existing,
        "category": "全部" if args.all_categories else "乘用车",
        "limit": args.limit if change_mode else (args.limit or 100),
        "offset": args.offset,
    }
    if notice_source:
        selector["change_notice"] = {
            "notice_url": notice_source.notice_url,
            "title": notice_source.title,
            "published_at": notice_source.published_at,
            "batch": notice_source.batch,
            "filters": change_filters,
            "total": notice_total,
        }
    cursor = conn.execute(
        "INSERT INTO ingestion_runs(started_at, selector_json, selected_models, parse_failures, publish_failures) "
        "VALUES (?, ?, ?, 0, 0)",
        (utc_now(), json.dumps(selector, ensure_ascii=False, sort_keys=True), len(selected)),
    )
    run_id = int(cursor.lastrowid)
    backfilled = backfill_catalog_fields(conn, args.catalog_db)
    conn.commit()
    if backfilled:
        print(f"已补齐 {backfilled} 份既有公告的目录参数。", flush=True)

    query_failures = download_failures = non_pdf = parse_failures = publish_failures = 0
    awaiting_effective = announcement_count = pdf_count = 0
    try:
        for index, item in enumerate(selected, start=1):
            vehicle_id = upsert_vehicle(conn, item)
            begin_model(conn, run_id, index, vehicle_id)
            print(f"[{index}/{len(selected)}] {item['common_name']} / {item['model_code']}", flush=True)
            try:
                rows = core.query_all_pages(model_code=item["model_code"], page_size=50)
                rows = [row for row in rows if str(row.get("clxh") or "").upper() == item["model_code"].upper()]
            except Exception as exc:
                query_failures += 1
                message = f"{type(exc).__name__}: {exc}"
                conn.execute(
                    "UPDATE run_models SET status='query_failed', error=? WHERE run_id=? AND vehicle_id=?",
                    (message, run_id, vehicle_id),
                )
                conn.commit()
                print(f"  查询失败：{message}", file=sys.stderr, flush=True)
                continue
            if not rows:
                if change_mode:
                    awaiting_effective += 1
                    message = (
                        f"正式公告接口尚未返回公示第{item['notice_batch']}批的精确型号"
                    )
                    conn.execute(
                        "UPDATE run_models SET status='awaiting_effective', error=? "
                        "WHERE run_id=? AND vehicle_id=?",
                        (message, run_id, vehicle_id),
                    )
                    conn.commit()
                    print(f"  待正式生效：{message}", flush=True)
                    continue
                conn.execute(
                    "UPDATE run_models SET status='no_match', error='' WHERE run_id=? AND vehicle_id=?",
                    (run_id, vehicle_id),
                )
                conn.commit()
                print("  公告接口未找到精确型号。", flush=True)
                continue

            if change_mode:
                rows = core.filter_latest_batch(rows)
                actual_batch = str(rows[0].get("gppc") or rows[0].get("pc") or "")
                expected_batch = item["notice_batch"]
                if not batch_is_at_least(actual_batch, expected_batch):
                    awaiting_effective += 1
                    message = (
                        f"公示第{expected_batch}批，正式公告当前最高为第{actual_batch or '未知'}批"
                    )
                    conn.execute(
                        "UPDATE run_models SET status='awaiting_effective', error=? "
                        "WHERE run_id=? AND vehicle_id=?",
                        (message, run_id, vehicle_id),
                    )
                    conn.commit()
                    print(f"  待正式生效：{message}", flush=True)
                    continue
                rows = [
                    {
                        **row,
                        "_change_notice": {
                            "batch": expected_batch,
                            "title": item["notice_title"],
                            "detail_url": item["notice_detail_url"],
                            "company": item["notice_company"],
                            "trademark": item["notice_trademark"],
                            "product_name": item["notice_product_name"],
                        },
                    }
                    for row in rows
                ]

            errors: list[str] = []
            model_pdf_count = 0
            for row in rows:
                announcement_count += 1
                ok_pdf, message = store_announcement(
                    conn,
                    row=row,
                    vehicle_id=vehicle_id,
                    market_name=item["common_name"] or item["model_code"],
                    download_root=args.download_root,
                    pdf_root=UPSTREAM_ROOT,
                    catalog_db=args.catalog_db,
                )
                if ok_pdf or message.startswith("解析失败"):
                    pdf_count += 1
                    model_pdf_count += 1
                if not ok_pdf:
                    if message.startswith("下载失败"):
                        download_failures += 1
                    elif message.startswith("解析失败"):
                        parse_failures += 1
                    elif message.startswith("发布失败"):
                        publish_failures += 1
                    else:
                        non_pdf += 1
                    errors.append(message)
            status = "done" if not errors else "partial"
            finish_model(conn, run_id, vehicle_id, status, "; ".join(errors))
            print(
                f"  公告 {len(rows)} 条；PDF {model_pdf_count} 条；"
                f"异常 {len(errors)} 条",
                flush=True,
            )
    finally:
        try:
            interrupted = finish_run(conn, run_id, {
                "query_failures": query_failures, "download_failures": download_failures,
                "non_pdf_documents": non_pdf, "awaiting_effective": awaiting_effective,
                "parse_failures": parse_failures, "publish_failures": publish_failures,
            })
        finally:
            conn.close()
            refresh_collection_status(args.site_db, catalog_db=args.catalog_db,
                                      pdf_root=UPSTREAM_ROOT, run_id=run_id, reason="catalog_collection")
        if interrupted:
            print(f"本轮有 {interrupted} 个型号因中断未完成，已标记 interrupted。", file=sys.stderr, flush=True)

    print(
        f"完成：选中 {len(selected)} 个目录车型，公告 {announcement_count} 条，PDF {pdf_count} 份，"
        f"待正式生效 {awaiting_effective}，查询失败 {query_failures}，"
        f"下载失败 {download_failures}，解析失败 {parse_failures}，发布失败 {publish_failures}，非 PDF {non_pdf}。\n"
        f"数据库：{args.site_db}\n目录快照：{selection_path}",
        flush=True,
    )

    if not args.no_report:
        # 报告只是产物，生成失败不该让一轮成功的采集变成失败
        try:
            from miit_gonggao.collection_report import write_report

            # 报告跟着它描述的那个库走：用默认库时落 var/reports，指定了别的库（测试、
            # 临时库）就落在该库旁边，免得往项目目录里写与之无关的产物。
            report_dir = args.report_dir or (
                PROJECT_ROOT / "var" / "reports"
                if args.site_db.resolve() == DEFAULT_SITE_DB.resolve()
                else args.site_db.parent / "reports"
            )
            report_path = write_report(args.site_db, run_id, report_dir, args.catalog_db)
            print(f"采集报告：{report_path}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"采集报告生成失败（不影响采集结果）：{type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
    return 2 if any((awaiting_effective, query_failures, download_failures,
                     parse_failures, publish_failures, non_pdf)) else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--manifest" in argv or any(arg.startswith("--manifest=") for arg in argv):
        from .collection_manifest import main as manifest_main
        return manifest_main(argv)
    return catalog_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
