#!/usr/bin/env python3
"""按车型类别和能源形式为公告网站 PDF 建立品牌分类目录。

分类目录使用硬链接，保留 downloads/announcement_site 原始目录不动，且不会
重复占用 PDF 文件空间。商业车会同时链接至其车种目录及适用的能源目录。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
# PDF 只存放在上游 vehicle-data-hub，relative_path 一律相对上游根解析；分类目录也建在
# 上游，硬链接才能和原始 PDF 落在同一文件系统。
UPSTREAM_ROOT = Path(os.environ.get(
    "VEHICLE_DATA_HUB_ROOT", str(Path(__file__).resolve().parent.parent)
)).expanduser().resolve()
if str(UPSTREAM_ROOT) not in sys.path:
    sys.path.insert(0, str(UPSTREAM_ROOT))
from miit_gonggao import core  # noqa: E402  上游是 PDF 与目录结构的权威

DEFAULT_DB = UPSTREAM_ROOT / "data" / "announcement_site.sqlite"
DEFAULT_OUTPUT_ROOT = UPSTREAM_ROOT / "downloads" / "announcement_site" / "分类公告"
COMMERCIAL_CATEGORIES = {"客车", "货车", "专用车"}


def safe_part(value: object) -> str:
    """与主目录同一套规范化：上游 core.safe_part 会把空白折成下划线，本地实现过去不会，
    导致「路虎(LAND ROVER)牌」在分类目录和主目录下产生两个名字。一律走上游的。"""
    return core.safe_part(str(value or "未标注"))


def energy_categories(fields: dict[str, object]) -> set[str]:
    """返回截图所列的商业车能源分类；字段缺失时不做猜测。"""
    fuel_type = str(fields.get("fuel_type") or "")
    text = " ".join(
        str(fields.get(key) or "") for key in ("fuel_type", "product_name", "model_full")
    )
    categories: set[str] = set()
    if fuel_type in {"纯电动", "电"} or "纯电动" in text:
        categories.add("纯电动商用车")
    if any(token in text for token in ("插电", "增程", "电混合", "燃料电池", "氢气")):
        categories.add("插混及燃料电池商用车")
    return categories


def load_records(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT d.relative_path, v.catalog_category, a.trademark, b.batch, f.fields_json
        FROM documents d
        JOIN announcements a ON a.id = d.announcement_id
        JOIN vehicles v ON v.id = a.vehicle_id
        LEFT JOIN batches b ON b.id = a.batch_id
        LEFT JOIN announcement_fields f ON f.announcement_id = a.id
        WHERE d.is_pdf = 1
        ORDER BY v.catalog_category, a.trademark, b.batch, d.relative_path
        """
    ).fetchall()


def categories_for(record: sqlite3.Row) -> set[str]:
    vehicle_category = str(record["catalog_category"] or "")
    if vehicle_category == "乘用车":
        return {"乘用车"}
    if vehicle_category not in COMMERCIAL_CATEGORIES:
        return set()
    try:
        fields = json.loads(record["fields_json"] or "{}")
    except json.JSONDecodeError:
        fields = {}
    return {vehicle_category, *energy_categories(fields)}


def build_links(db_path: Path, output_root: Path, dry_run: bool,
                source_root: Path = UPSTREAM_ROOT) -> tuple[Counter[str], int]:
    with sqlite3.connect(db_path) as conn:
        records = load_records(conn)

    counts: Counter[str] = Counter()
    skipped = 0
    for record in records:
        source = source_root / str(record["relative_path"])
        if not source.is_file():
            raise FileNotFoundError(f"数据库记录的 PDF 不存在：{source}")
        for category in categories_for(record):
            destination = (
                output_root
                / category
                / safe_part(record["trademark"])
                / f"第{safe_part(record['batch'])}批"
                / source.name
            )
            counts[category] += 1
            if dry_run:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if os.path.samefile(source, destination):
                    skipped += 1
                    continue
                raise FileExistsError(f"分类目标存在同名不同文件：{destination}")
            os.link(source, destination)

    if not dry_run:
        (output_root / "分类统计.json").write_text(
            json.dumps(
                {
                    "link_type": "hardlink",
                    "categories": dict(sorted(counts.items())),
                    "records_without_energy_subcategory": sum(
                        1
                        for record in records
                        if str(record["catalog_category"] or "") in COMMERCIAL_CATEGORIES
                        and len(categories_for(record)) == 1
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
    return counts, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--source-root", type=Path, default=UPSTREAM_ROOT,
                        help="数据库 relative_path 所基于的根目录")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error(f"数据库不存在：{args.db}")
    counts, skipped = build_links(args.db, args.output_root, args.dry_run, args.source_root)
    action = "计划建立" if args.dry_run else "已建立"
    print(f"{action}分类硬链接：" + "，".join(f"{key} {value} 份" for key, value in sorted(counts.items())))
    if not args.dry_run:
        print(f"已存在且复用：{skipped} 份；目录：{args.output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
