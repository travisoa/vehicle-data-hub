#!/usr/bin/env python3
"""归档公告参数页的历史快照，不把旧版本混入当前公告库。

公告网站的 PDF 是按请求动态生成的。同一产品 ID 后续可能返回字段不同的
内容，因此主目录只保留当前下载版本；已发现的旧版本移入上游
``downloads/announcement_site/_revisions/``，并写入修订清单。

默认只报告候选项。传入 ``--apply`` 才会移动文件、删除分类索引中的对应
硬链接并刷新分类统计。
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
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# PDF 与分类目录都在上游 vehicle-data-hub，documents.relative_path 一律相对上游根解析。
UPSTREAM_ROOT = Path(os.environ.get(
    "VEHICLE_DATA_HUB_ROOT", str(Path(__file__).resolve().parent.parent)
)).expanduser().resolve()
for _path in (UPSTREAM_ROOT, Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

DEFAULT_ROOT = UPSTREAM_ROOT / "downloads" / "announcement_site"
DB_PATH = UPSTREAM_ROOT / "data" / "announcement_site.sqlite"
REVISION_DIRNAME = "_revisions"
CATEGORY_DIRNAME = "分类公告"
SKIP_TOP_LEVEL = {REVISION_DIRNAME, "_snapshots", CATEGORY_DIRNAME}
IDENTITY_FIELDS = ("model_code", "batch", "product_no")
SIGNIFICANT_FIELDS = {
    "batch",
    "product_no",
    "publish_date",
    "effective_date",
    "gross_mass",
    "axle_load",
}


def count_without_energy_subcategory(db_path: Path) -> int:
    """商用车中只归到车种目录、未拿到能源子类的记录数；与 classify 脚本共用同一份判定。"""
    from miit_gonggao.collection_classify import (
        COMMERCIAL_CATEGORIES,
        categories_for,
        load_records,
    )

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return sum(
            1
            for record in load_records(connection)
            if str(record["catalog_category"] or "") in COMMERCIAL_CATEGORIES
            and len(categories_for(record)) == 1
        )
    finally:
        connection.close()


def parse_pdf_fields(path: Path) -> dict[str, str]:
    """延迟导入，确保直接执行 scripts/ 下的脚本时仍能定位项目包。"""
    from miit_gonggao.review_export import parse_gonggao_pdf

    return parse_gonggao_pdf(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def product_id_from_filename(path: Path) -> str:
    """读取统一命名末段的产品 ID，兼容已标记的历史文件名。"""
    stem = re.sub(r"_历史(?:下载|快照)(?:_\d+)?$", "", path.stem)
    return stem.rsplit("_", 1)[-1]


def pdf_metadata(path: Path) -> dict[str, str]:
    """读 PDF 自带元数据。

    过去调 pdfinfo(poppler)：既是未声明的外部二进制，输出的又是本地化的人类可读时间
    （``Thu Aug 27 04:55:28 2026 CST``）。解析它的 ``%Z`` 只认当前 TZ 环境的时区缩写，
    在 TZ=UTC 的服务器或非英文 locale 下一律失败，时间戳会集体退化成「未知生成时间」。
    pdfplumber 已是运行依赖，且给出 PDF 原生的 ``D:YYYYMMDDHHmmSS`` 格式，与环境无关。
    """
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        meta = pdf.metadata or {}
        pages = len(pdf.pages)
    return {
        "CreationDate": str(meta.get("CreationDate") or ""),
        "ModDate": str(meta.get("ModDate") or ""),
        "Producer": str(meta.get("Producer") or ""),
        "Creator": str(meta.get("Creator") or ""),
        "Pages": str(pages),
    }


def revision_timestamp(metadata: dict[str, str]) -> str:
    match = re.match(r"D:(\d{14})", metadata.get("CreationDate", ""))
    return match.group(1) if match else "未知生成时间"


def normalized_revision_name(source: Path, metadata: dict[str, str], fallback: str = "") -> str:
    """归档文件名。取不到生成时间时补一段内容哈希，否则同一路径的多个历史版本会重名。"""
    stem = re.sub(r"_历史(?:下载|快照)(?:_\d+)?$", "", source.stem)
    timestamp = revision_timestamp(metadata)
    if timestamp == "未知生成时间" and fallback:
        timestamp = f"{timestamp}_{fallback}"
    return f"{stem}_历史快照_{timestamp}{source.suffix.lower()}"


def load_current_documents(db_path: Path) -> dict[str, Path]:
    # 只读打开：sqlite3.connect 对不存在的路径会静默建一个空库，等于凭空造出一份
    # 会误导其他工具的「公告库」。只读既有权威业务库，这里绝不创建它。
    if not db_path.is_file():
        raise SystemExit(
            f"公告业务库不存在：{db_path}\n请先运行 main.py gonggao collect 采集。"
        )
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {
            product_id: (UPSTREAM_ROOT / relative_path).resolve()
            for product_id, relative_path in connection.execute(
                """
                SELECT a.source_product_id, d.relative_path
                FROM announcements AS a
                JOIN documents AS d ON d.announcement_id = a.id
                WHERE d.is_pdf = 1
                """
            )
        }
    finally:
        connection.close()


def compare_fields(legacy: dict[str, str], current: dict[str, str]) -> list[str]:
    return [
        key
        for key in sorted(set(legacy) | set(current))
        if key != "source_file" and (legacy.get(key) or "").strip() != (current.get(key) or "").strip()
    ]


def is_current_path(path: Path, current_paths: set[Path]) -> bool:
    return path in current_paths


def collect_candidates(root: Path, current_by_product_id: dict[str, Path]) -> tuple[list[dict[str, Any]], int, int]:
    current_paths = set(current_by_product_id.values())
    candidates: list[dict[str, Any]] = []
    unmatched = 0
    aliases = 0
    for source in root.rglob("*.pdf"):
        relative = source.relative_to(root)
        if relative.parts[0] in SKIP_TOP_LEVEL or is_current_path(source.resolve(), current_paths):
            continue
        product_id = product_id_from_filename(source)
        current = current_by_product_id.get(product_id)
        if current is None:
            unmatched += 1
            continue
        if os.path.samefile(source, current):
            aliases += 1
            continue
        if not current.is_file():
            raise RuntimeError(f"数据库登记的当前 PDF 不存在：{current}")
        source_hash = sha256(source)
        current_hash = sha256(current)
        if source_hash == current_hash:
            continue
        legacy_fields = parse_pdf_fields(source)
        current_fields = parse_pdf_fields(current)
        changed_fields = compare_fields(legacy_fields, current_fields)
        same_identity = all(legacy_fields.get(key) == current_fields.get(key) for key in IDENTITY_FIELDS)
        classification = (
            "same_identity_other_changed"
            if same_identity
            else "identity_changed"
            if set(changed_fields) & SIGNIFICANT_FIELDS
            else "identity_changed_other_changed"
        )
        metadata = pdf_metadata(source)
        candidates.append(
            {
                "product_id": product_id,
                "source": source,
                "current": current,
                "source_sha256": source_hash,
                "current_sha256": current_hash,
                "legacy_fields": legacy_fields,
                "current_fields": current_fields,
                "changed_fields": changed_fields,
                "classification": classification,
                "metadata": metadata,
            }
        )
    return candidates, unmatched, aliases


def remove_category_links(category_root: Path, archived_paths: list[Path]) -> int:
    inode_keys = {(path.stat().st_dev, path.stat().st_ino) for path in archived_paths}
    removed = 0
    for link in category_root.rglob("*.pdf"):
        stat = link.stat()
        if (stat.st_dev, stat.st_ino) in inode_keys:
            link.unlink()
            removed += 1
    return removed


def prune_empty_parents(paths: list[Path], root: Path) -> int:
    removed = 0
    for source in paths:
        parent = source.parent
        while parent != root:
            try:
                parent.rmdir()
            except OSError:
                break
            removed += 1
            parent = parent.parent
    return removed


def refresh_category_stats(category_root: Path, db_path: Path) -> dict[str, int]:
    """重算分类统计。能源子类缺失数由 classify 脚本的同一函数算出，不再硬编码 0。"""
    if not category_root.is_dir():
        return {}
    categories = {
        child.name: sum(1 for _ in child.rglob("*.pdf"))
        for child in category_root.iterdir()
        if child.is_dir()
    }
    stats_path = category_root / "分类统计.json"
    stats = {
        "link_type": "hardlink",
        "categories": dict(sorted(categories.items())),
        "records_without_energy_subcategory": count_without_energy_subcategory(db_path),
    }
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return categories


def build_manifest(root: Path, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    records = []
    for item in candidates:
        source = item["source"]
        metadata = item["metadata"]
        destination = (
            root / REVISION_DIRNAME / source.relative_to(root).parent
            / normalized_revision_name(source, metadata, fallback=item["source_sha256"][:8])
        )
        records.append(
            {
                "product_id": item["product_id"],
                "classification": item["classification"],
                "changed_fields": item["changed_fields"],
                "legacy": {
                    "path": str(destination.relative_to(root)),
                    "sha256": item["source_sha256"],
                    "pdf_metadata": metadata,
                    "identity": {key: item["legacy_fields"].get(key, "") for key in IDENTITY_FIELDS},
                },
                "current": {
                    "path": str(item["current"].relative_to(UPSTREAM_ROOT)),
                    "sha256": item["current_sha256"],
                    "identity": {key: item["current_fields"].get(key, "") for key in IDENTITY_FIELDS},
                },
            }
        )
    return {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "description": "公告网站动态生成 PDF 的历史快照；主目录只保留当前版本。",
        "records": records,
    }


def validate_destinations(candidates: list[dict[str, Any]], destinations: list[Path]) -> None:
    """整批校验，必须跑在任何一次移动之前。

    这个检查过去写在移动循环内部：第 N 个撞名时前 N-1 个已经移进 _revisions，而 manifest
    要等循环结束才写，于是那些文件既不在主目录、也不在清单里；_revisions 又在 SKIP_TOP_LEVEL
    中不参与后续扫描，重跑也发现不了。
    """
    claimed: dict[Path, Path] = {}
    for item, destination in zip(candidates, destinations, strict=True):
        if destination in claimed:
            raise RuntimeError(
                f"两个候选归档到同一目标，拒绝执行：{destination}\n"
                f"  {claimed[destination]}\n  {item['source']}"
            )
        claimed[destination] = item["source"]
        if destination.exists():
            raise RuntimeError(f"历史快照目标已存在，拒绝覆盖：{destination}")


def apply_archive(root: Path, candidates: list[dict[str, Any]], db_path: Path) -> tuple[int, int, int, dict[str, int]]:
    revision_root = root / REVISION_DIRNAME
    manifest = build_manifest(root, candidates)
    destinations = [root / item["legacy"]["path"] for item in manifest["records"]]
    validate_destinations(candidates, destinations)

    moved: list[tuple[Path, Path]] = []
    try:
        for source_item, destination in zip(candidates, destinations, strict=True):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_item["source"]), str(destination))
            moved.append((destination, source_item["source"]))
    except Exception:
        # 移动到一半失败（磁盘满、权限等）：原样退回，不留中间态
        for destination, origin in reversed(moved):
            origin.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(origin))
        raise
    removed_links = remove_category_links(root / CATEGORY_DIRNAME, destinations)
    removed_dirs = prune_empty_parents([item["source"] for item in candidates], root)
    revision_root.mkdir(exist_ok=True)
    manifest_path = revision_root / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["records"] = existing.get("records", []) + manifest["records"]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    categories = refresh_category_stats(root / CATEGORY_DIRNAME, db_path)
    return len(destinations), removed_links, removed_dirs, categories


def main() -> int:
    parser = argparse.ArgumentParser(description="归档公告参数页的历史 PDF 快照")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help=f"公告 PDF 根目录，默认 {DEFAULT_ROOT}")
    parser.add_argument("--db", type=Path, default=DB_PATH, help=f"当前公告数据库，默认 {DB_PATH}")
    parser.add_argument("--apply", action="store_true", help="执行移动；省略时仅输出候选统计")
    args = parser.parse_args()
    root = args.root.resolve()
    candidates, unmatched, aliases = collect_candidates(root, load_current_documents(args.db.resolve()))
    kinds = Counter(item["classification"] for item in candidates)
    print(f"历史快照候选: {len(candidates)} ({dict(sorted(kinds.items()))})")
    print(f"无当前库对应的旧 PDF（不处理）: {unmatched}")
    print(f"仅路径大小写别名（不处理）: {aliases}")
    if not args.apply:
        print("仅预览；加 --apply 执行归档。")
        return 0
    if not candidates:
        print("没有需要归档的历史快照。")
        return 0
    moved, removed_links, removed_dirs, categories = apply_archive(root, candidates, args.db.resolve())
    print(f"已归档: {moved}")
    print(f"已移除分类硬链接: {removed_links}")
    print(f"已清理空目录: {removed_dirs}")
    print(f"分类统计: {dict(sorted(categories.items()))}")
    print(f"修订清单: {root / REVISION_DIRNAME / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
