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
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass
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

from miit_gonggao import core, republished  # noqa: E402
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
               "parse_failures", "publish_failures", "image_failures")
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
    publish_failures INTEGER NOT NULL DEFAULT 0,
    image_failures INTEGER NOT NULL DEFAULT 0
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
    for column in ("awaiting_effective", "parse_failures", "publish_failures", "image_failures"):
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


def requested_interval(args: argparse.Namespace) -> tuple[float, float] | None:
    minimum, maximum = getattr(args, "min_interval", None), getattr(args, "max_interval", None)
    if minimum is None and maximum is None:
        return None
    if minimum is None or maximum is None:
        raise ValueError("--min-interval 和 --max-interval 必须同时提供")
    if not (math.isfinite(minimum) and math.isfinite(maximum) and 0 < minimum <= maximum):
        raise ValueError("请求间隔必须为有限数且满足 0 < min <= max")
    return minimum, maximum


@dataclass
class RequestPace:
    requested: tuple[float, float] | None
    default: tuple[float, float]
    warning: str = ""

    def slow_down(self, status: str) -> None:
        core.REQUEST_MIN_INTERVAL = self.default
        self.warning = f"发生 {status}，本次执行后续请求恢复默认间隔 {self.default[0]}–{self.default[1]} 秒"


@contextmanager
def request_pace(args: argparse.Namespace):
    """只覆盖本进程的请求间隔，退出时一律恢复；不改 core.py 默认值。"""
    original = core.REQUEST_MIN_INTERVAL
    pace = RequestPace(requested_interval(args), original)
    try:
        if pace.requested is not None:
            core.REQUEST_MIN_INTERVAL = pace.requested
        yield pace
    finally:
        core.REQUEST_MIN_INTERVAL = original


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


def select_republished_models(
    catalog_db: Path,
    site_db: Path,
    republished_rows: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """把正式发布的重发产品转为采集候选；已有型号必须保留并强制刷新。"""
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in republished_rows:
        model_code = str(row.get("model_code") or "").strip()
        model_key = model_code.upper()
        if not model_code or model_key in seen:
            continue
        seen.add(model_key)
        # 重发对象一定已在业务库里，它的目录归属是此前采集定下的，本轮只换参数页，
        # 不能让目录库的另一条同型号记录（如车船税目录）改写 catalog_* 和分类依据。
        base = _existing_vehicle_item(site_db, model_code) or _catalog_item(
            catalog_db, model_code
        ) or {
            "catalog": "正式发布重发",
            "batch": str(row.get("republished_batch") or ""),
            "category": derive_catalog_category(
                model_code, str(row.get("product_name") or "")
            ),
            "seq": "",
            "company": str(row.get("company") or ""),
            "trademark": str(row.get("trademark") or ""),
            "model_code": model_code,
            "common_name": model_code,
        }
        item = dict(base)
        item.update(
            {
                "model_code": model_code,
                "republished_batch": str(row.get("republished_batch") or ""),
                "local_batch": str(row.get("local_batch") or ""),
                "republished_product_id": str(row.get("product_id") or ""),
                "republished_company": str(row.get("company") or ""),
                "republished_trademark": str(row.get("trademark") or ""),
                "republished_product_name": str(row.get("product_name") or ""),
            }
        )
        selected.append(item)
    return selected


def batch_is_at_least(actual: str, expected: str) -> bool:
    """判断正式公告接口批次是否已达到记录批次；非数字批次仅接受完全相等。"""
    actual = actual.strip()
    expected = expected.strip()
    if actual.isdigit() and expected.isdigit():
        return int(actual) >= int(expected)
    return bool(actual and expected and actual == expected)


def write_republished_snapshot(
    source: republished.RepublishedSource,
    *,
    selected: list[dict[str, str]],
    output_root: Path,
    verification: dict[str, list[dict[str, Any]]] | None = None,
) -> Path:
    snapshot_dir = output_root / core.ANNOUNCEMENT_SNAPSHOT_DIRNAME
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = snapshot_dir / f"republished_seed_{safe_part(source.snapshot_scope())}_{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "source": {
                    "origin": source.origin,
                    "generation": source.generation,
                    "batches": source.batches,
                    "stale_batches": source.stale_batches,
                    "notes": source.notes,
                },
                "total": len(source.rows),
                "total_candidates": source.candidate_total,
                "rows": source.rows,
                "selected_models": selected,
                **({"verification": {
                    "counts": {name: len(items) for name, items in verification.items()},
                    **verification,
                }} if verification is not None else {}),
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


class StoreOutcome(tuple):
    """store_announcement 的 (ok, message) 两项结果；image_failed 单列图片异常，调用方不从文案里查找。"""

    image_failed: bool

    def __new__(cls, ok: bool, message: str, image_failed: bool = False) -> "StoreOutcome":
        outcome = super().__new__(cls, (ok, message))
        outcome.image_failed = image_failed
        return outcome


def store_announcement(
    conn: sqlite3.Connection,
    *,
    row: dict[str, Any],
    vehicle_id: int,
    market_name: str,
    download_root: Path,
    pdf_root: Path,
    catalog_db: Path,
) -> StoreOutcome:
    from miit_gonggao.images import publish_images, record_image_failure

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
        image_error = ""
        try:
            downloaded = core.download_param_page(row, staging)
            path, is_pdf, byte_count = downloaded
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            image_result = getattr(downloaded, "images", None)
            if image_result and image_result["failed"]:
                image_error = f"图片下载不完整：{image_result['failed']} 项失败；{image_result.get('error', '')}"
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
                return StoreOutcome(False, f"发布失败：{type(exc).__name__}: {exc}；原文件保留于 {retained}")

            # 失败重采不改写旧版本：图片与历史索引照常发布，已有的当前图片索引保持不变。
            replace_current = not (previous and error)
            try:
                publish_images(row, staging, folder, replace_current=replace_current)
            except Exception as exc:
                image_error = f"图片下载不完整：图片发布失败 {type(exc).__name__}: {exc}"
                # 写入失败索引后，后续采集按图片异常只重取图片；索引也写不进时才保留暂存目录待人工处理。
                if not record_image_failure(row, folder, image_error, replace_current=replace_current):
                    keep_staging = True
                    image_error += f"；暂存目录：{staging}"

        if previous and error:
            # 失败重采不改写任何旧版本，包括 HTML 和仅回填了目录字段的记录。
            return StoreOutcome(False, "; ".join(value for value in (error, image_error) if value),
                                bool(image_error))

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
        # PDF 状态不因图片失败回滚；上层按 image_failed 单独统计图片异常。
        return StoreOutcome(is_pdf and not error, "; ".join(value for value in (error, image_error) if value),
                            bool(image_error))
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
        help="限制本轮处理量；目录模式按型号计、默认 100，"
             "正式发布重发模式按候选产品计、默认不限制（去重后型号数可能更少）",
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
        "--republished-from-status",
        action="store_true",
        help="按正式发布重发清单强制刷新：取自已落盘的收录统计记录，不重算；记录过期时拒绝",
    )
    parser.add_argument(
        "--republished-batch",
        action="append",
        default=[],
        metavar="批次",
        help="按正式公告批次现算重发清单，支持 409、405-409、405,407；可重复",
    )
    parser.add_argument(
        "--allow-stale-status",
        action="store_true",
        help="允许在统计记录过期时按历史记录执行重发刷新，结果可能漏项",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只生成重发清单快照并输出统计，不查询、不下载、不写业务库",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="配合 --dry-run：按型号查正式接口核实清单，分辨真重发与枚举缓存的假阳性；只读不下载",
    )
    parser.add_argument(
        "--min-interval", type=float,
        help="本进程请求最小间隔（秒），须与最大间隔同时给出；默认沿用 core 的 0.8–1.8",
    )
    parser.add_argument(
        "--max-interval", type=float, help="本进程请求最大间隔（秒），须与最小间隔同时给出",
    )
    parser.add_argument("--no-images", action="store_true", help="只下载 PDF，不获取详情页原图")
    parser.add_argument("--catalog-db", type=Path, default=DEFAULT_CATALOG_DB)
    parser.add_argument("--site-db", type=Path, default=DEFAULT_SITE_DB)
    parser.add_argument("--report-dir", type=Path,
                        help="采集报告输出目录，默认 var/reports/（用自定义 --site-db 时跟随该库）")
    parser.add_argument("--no-report", action="store_true", help="跳过本轮的 Markdown 采集报告")
    parser.add_argument("--download-root", type=Path, default=DEFAULT_DOWNLOAD_ROOT)
    args = parser.parse_args(argv)
    if (args.limit is not None and args.limit < 1) or args.offset < 0:
        parser.error("--limit 必须大于 0，--offset 不得小于 0")
    try:
        requested_interval(args)
    except ValueError as exc:
        parser.error(str(exc))
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
    republish_mode = bool(args.republished_from_status or args.republished_batch)
    if args.republished_from_status and args.republished_batch:
        parser.error("--republished-from-status 与 --republished-batch 只能选一个来源")
    if args.allow_stale_status and not args.republished_from_status:
        parser.error("--allow-stale-status 只在 --republished-from-status 下有意义")
    if republish_mode and args.exclude_existing:
        parser.error("正式发布重发模式会强制刷新已有型号，不能与 --exclude-existing 同时使用")
    if republish_mode and args.from_file:
        parser.error("正式发布重发模式自带清单，不能与 -f/--from-file 同时使用")
    if args.dry_run and not republish_mode:
        parser.error("--dry-run 只在正式发布重发模式下可用")
    if args.verify and not args.dry_run:
        # 真正采集时 batch_is_at_least 已经逐条核实，再单独查一遍只是重复请求。
        parser.error("--verify 只在 --dry-run 下可用；实际采集本身就会核实接口批次")

    republished_source: republished.RepublishedSource | None = None
    if republish_mode:
        try:
            if args.republished_from_status:
                republished_source = republished.from_status(
                    args.site_db, catalog_db=args.catalog_db, pdf_root=UPSTREAM_ROOT,
                    allow_stale=args.allow_stale_status,
                )
            else:
                republished_source = republished.from_batches(
                    args.site_db,
                    republished.parse_batches(args.republished_batch),
                    UPSTREAM_ROOT / "downloads" / "announcement_batches",
                )
        except republished.RepublishedError as exc:
            parser.error(str(exc))
        republished_source = republished_source.limited(args.limit)
        for note in republished_source.notes:
            print(f"说明：{note}", flush=True)
        selected = select_republished_models(
            args.catalog_db, args.site_db, republished_source.rows
        )
        if not selected:
            parser.error("正式发布重发清单为空：本地没有被更高批次重新发布的产品")
        batch = republished_source.label
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
    if republished_source is not None:
        verification = None
        if args.verify:
            print(f"开始按型号核实 {len(republished_source.rows)} 条候选（只读查询，不下载）……",
                  flush=True)

            def _report(index: int, total: int, model: str, bucket: str, _outcome: dict) -> None:
                if bucket != "confirmed":  # 真重发是常态，只报需要人看的三类
                    print(f"  [{index}/{total}] {model}：{bucket}", flush=True)

            with request_pace(args):
                verification = republished.verify_against_api(
                    republished_source.rows, on_result=_report)
            print("核实结果：" + "、".join(
                f"{name} {len(items)} 条" for name, items in verification.items()), flush=True)
        selection_path = write_republished_snapshot(
            republished_source,
            selected=selected,
            output_root=args.download_root,
            verification=verification,
        )
        if args.dry_run:
            by_batch = Counter(row["republished_batch"] for row in republished_source.rows)
            scope = (f"本轮清单 {len(republished_source.rows)} 个产品"
                     + (f"（候选共 {republished_source.candidate_total} 个）"
                        if republished_source.candidate_total != len(republished_source.rows) else ""))
            checked = ("已按接口核实；" if verification is not None
                       else "未查询官方接口，")
            print(f"{scope}，覆盖 {len(selected)} 个型号；"
                  + "、".join(f"第{batch_no}批 {count} 个" for batch_no, count in sorted(by_batch.items()))
                  + f"\n清单快照：{selection_path}\n预演模式：{checked}未下载 PDF，未写业务库。", flush=True)
            return 0
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
        "mode": "republished" if republish_mode else "catalog",
        "catalog": CATALOG_NAME,
        "catalog_batch": batch,
        "through_earlier_batches": args.through_earlier_batches,
        "exclude_existing": args.exclude_existing,
        "category": "全部" if args.all_categories else "乘用车",
        "limit": args.limit if republish_mode else (args.limit or 100),
        "offset": args.offset,
        "request_interval": list(requested_interval(args) or core.REQUEST_MIN_INTERVAL),
    }
    if republished_source is not None:
        selector["republished"] = {
            "origin": republished_source.origin,
            "generation": republished_source.generation,
            "batches": republished_source.batches,
            "stale_batches": republished_source.stale_batches,
            "total": len(republished_source.rows),
            "total_candidates": republished_source.candidate_total,
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

    query_failures = download_failures = non_pdf = parse_failures = publish_failures = image_failures = 0
    awaiting_effective = announcement_count = pdf_count = 0
    pace_warning = ""
    try:
        # 提速与 --no-images 只覆盖本进程；下载或查询失败会自动恢复默认间隔。
        with request_pace(args) as pace, core.image_downloads(not getattr(args, "no_images", False)):
            for index, item in enumerate(selected, start=1):
                vehicle_id = upsert_vehicle(conn, item)
                begin_model(conn, run_id, index, vehicle_id)
                print(f"[{index}/{len(selected)}] {item['common_name']} / {item['model_code']}", flush=True)
                try:
                    rows = core.query_all_pages(model_code=item["model_code"], page_size=50)
                    rows = [row for row in rows if str(row.get("clxh") or "").upper() == item["model_code"].upper()]
                except Exception as exc:
                    query_failures += 1
                    pace.slow_down("query_failed")
                    message = f"{type(exc).__name__}: {exc}"
                    conn.execute(
                        "UPDATE run_models SET status='query_failed', error=? WHERE run_id=? AND vehicle_id=?",
                        (message, run_id, vehicle_id),
                    )
                    conn.commit()
                    print(f"  查询失败：{message}", file=sys.stderr, flush=True)
                    continue
                if not rows:
                    if republish_mode:
                        awaiting_effective += 1
                        message = (
                            f"记录第{item['republished_batch']}批重新发布，正式接口现在查不到该精确型号"
                        )
                        conn.execute(
                            "UPDATE run_models SET status='awaiting_effective', error=? "
                            "WHERE run_id=? AND vehicle_id=?",
                            (message, run_id, vehicle_id),
                        )
                        conn.commit()
                        print(f"  接口与记录不一致：{message}", flush=True)
                        continue
                    conn.execute(
                        "UPDATE run_models SET status='no_match', error='' WHERE run_id=? AND vehicle_id=?",
                        (run_id, vehicle_id),
                    )
                    conn.commit()
                    print("  公告接口未找到精确型号。", flush=True)
                    continue

                if republish_mode:
                    rows = core.filter_latest_batch(rows)
                    actual_batch = str(rows[0].get("gppc") or rows[0].get("pc") or "")
                    expected_batch = item["republished_batch"]
                    if not batch_is_at_least(actual_batch, expected_batch):
                        # 清单来自正式发布记录，接口批次反而更低说明记录已与源不符，
                        # 不能拿更旧的一版覆盖本地已有参数页。
                        awaiting_effective += 1
                        message = (
                            f"记录第{expected_batch}批，正式接口当前最高为第{actual_batch or '未知'}批"
                        )
                        conn.execute(
                            "UPDATE run_models SET status='awaiting_effective', error=? "
                            "WHERE run_id=? AND vehicle_id=?",
                            (message, run_id, vehicle_id),
                        )
                        conn.commit()
                        print(f"  接口与记录不一致：{message}", flush=True)
                        continue
                    rows = [
                        {
                            **row,
                            "_republished": {
                                "recorded_batch": expected_batch,
                                "local_batch": item["local_batch"],
                                "product_id": item["republished_product_id"],
                                "company": item["republished_company"],
                                "trademark": item["republished_trademark"],
                                "product_name": item["republished_product_name"],
                            },
                        }
                        for row in rows
                    ]

                errors: list[str] = []
                model_pdf_count = 0
                for row in rows:
                    announcement_count += 1
                    outcome = store_announcement(
                        conn,
                        row=row,
                        vehicle_id=vehicle_id,
                        market_name=item["common_name"] or item["model_code"],
                        download_root=args.download_root,
                        pdf_root=UPSTREAM_ROOT,
                        catalog_db=args.catalog_db,
                    )
                    ok_pdf, message = outcome
                    if ok_pdf or message.startswith("解析失败"):
                        pdf_count += 1
                        model_pdf_count += 1
                    if outcome.image_failed:
                        image_failures += 1
                        pace.slow_down("image_failed")
                        if ok_pdf:
                            errors.append(message)
                    if not ok_pdf:
                        if message.startswith("下载失败"):
                            download_failures += 1
                            pace.slow_down("download_failed")
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
        pace_warning = pace.warning
        try:
            interrupted = finish_run(conn, run_id, {
                "query_failures": query_failures, "download_failures": download_failures,
                "non_pdf_documents": non_pdf, "awaiting_effective": awaiting_effective,
                "parse_failures": parse_failures, "publish_failures": publish_failures,
                "image_failures": image_failures,
            })
        finally:
            conn.close()
            refresh_collection_status(args.site_db, catalog_db=args.catalog_db,
                                      pdf_root=UPSTREAM_ROOT, run_id=run_id, reason="catalog_collection")
        if interrupted:
            print(f"本轮有 {interrupted} 个型号因中断未完成，已标记 interrupted。", file=sys.stderr, flush=True)

    print(
        f"完成：选中 {len(selected)} 个目录车型，公告 {announcement_count} 条，PDF {pdf_count} 份，"
        f"接口与记录不一致 {awaiting_effective}，查询失败 {query_failures}，"
        f"下载失败 {download_failures}，解析失败 {parse_failures}，发布失败 {publish_failures}，"
        f"图片异常产品 {image_failures}，非 PDF {non_pdf}。"
        + (f"\n{pace_warning}" if pace_warning else "")
        + f"\n数据库：{args.site_db}\n目录快照：{selection_path}",
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
                     parse_failures, publish_failures, non_pdf, image_failures)) else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--manifest" in argv or any(arg.startswith("--manifest=") for arg in argv):
        from .collection_manifest import main as manifest_main
        return manifest_main(argv)
    return catalog_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
