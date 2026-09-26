#!/usr/bin/env python3
"""用当前解析器刷新存量公告参数页的解析字段。

解析器改进后，旧版本的解析结果仍留在业务库。例如 2026-09-12 以前入库的改装车参数页：
横向底盘引用表被当成「底盘名称」，底盘 ID 与底盘型号混进了 VIN。本命令重新解析本地 PDF，
只挑出新解析确认属于指定版式、而库里仍是旧结果的记录。

默认只读预览；``--apply`` 才在采集排他锁内写入业务库。写入规则：

- 只改 ``announcement_fields``：PDF 解析键整体换成新结果，目录补充等非 PDF 键原样保留；
  不动 PDF、文档登记和公告记录。
- PDF 须与 ``documents.sha256`` 登记一致，否则跳过并列出；新解析失败不覆盖旧字段。
- 存在未完成采集时拒绝写入；写入提交后刷新一次收录统计。

每次运行的汇总与逐条差异写入 ``var/runs/<时间>-reparse-<版式>/``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from miit_gonggao import collection as seed
from miit_gonggao import review_export

LAYOUTS = ("converted_vehicle",)
DEFAULT_RUNS_DIR = seed.UPSTREAM_ROOT / "var" / "runs"

# 解析器可能产出的全部键。刷新时这些键整体换成新结果（新结果没有的旧键即旧版误解析，一并去掉）；
# 其余键来自目录补充等其他来源，原样保留。
PARSER_KEYS = frozenset(
    {key for _, key in review_export.LINE_LABELS if not key.startswith("@")}
    | {key for _, key in review_export.ENGINE_COLUMNS}
    | {f"{group}_{dim}" for group in ("outline", "cargo") for dim in ("length", "width", "height")}
    | {"batch", "model_code", "product_name", "other", "vin", "chassis", "chassis_references",
       "pdf_layout", "cargo_dims", "source_file"}
)

Parser = Callable[[Path], dict[str, str]]


@dataclass
class Plan:
    layout: str
    scanned: int = 0
    stale: int = 0
    changes: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    unchanged: int = 0

    def summary(self) -> dict[str, Any]:
        keys = Counter(key for item in self.changes for key in item["changed_keys"])
        preserved = Counter(key for item in self.changes for key in item["preserved_keys"])
        return {
            "layout": self.layout,
            "scanned": self.scanned,
            "stale": self.stale,
            "changed": len(self.changes),
            "unchanged": self.unchanged,
            "skipped": dict(Counter(item["reason"] for item in self.skipped)),
            "changed_keys": dict(keys.most_common()),
            "preserved_keys": dict(preserved.most_common()),
        }


def merge_fields(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """PDF 解析键取新结果，其他来源的键保留。"""
    merged = {key: value for key, value in old.items() if key not in PARSER_KEYS}
    merged.update(new)
    return merged


def _parse(path: str) -> tuple[dict[str, str] | None, str]:
    try:
        return review_export.parse_gonggao_pdf(Path(path)), ""
    except Exception as exc:  # 单份版式异常不影响其余记录
        return None, f"{type(exc).__name__}: {exc}"


def _parse_all(paths: list[str], workers: int, parser: Parser | None) -> list[tuple[dict[str, str] | None, str]]:
    if parser is not None:
        results = []
        for path in paths:
            try:
                results.append((parser(Path(path)), ""))
            except Exception as exc:
                results.append((None, f"{type(exc).__name__}: {exc}"))
        return results
    if workers > 1:
        try:
            pool = ProcessPoolExecutor(max_workers=workers)
        except OSError as exc:  # 受限环境（如沙箱禁用信号量）下退回单进程，结果相同只是更慢
            print(f"无法启动并行解析（{type(exc).__name__}: {exc}），改为单进程。", file=sys.stderr)
        else:
            with pool:
                return list(pool.map(_parse, paths, chunksize=64))
    return [_parse(path) for path in paths]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_plan(conn: sqlite3.Connection, pdf_root: Path, layout: str, *, workers: int = 1,
               limit: int | None = None, parser: Parser | None = None) -> Plan:
    """只读计算：哪些记录的库存字段不是该版式的当前解析结果，刷新后各键怎样变化。"""
    rows = conn.execute(
        "SELECT a.id, a.source_product_id, a.model_code, d.relative_path, d.sha256, f.fields_json, f.parse_error "
        "FROM announcement_fields f JOIN announcements a ON a.id = f.announcement_id "
        "JOIN documents d ON d.announcement_id = a.id WHERE d.is_pdf = 1 ORDER BY a.id"
    ).fetchall()
    plan = Plan(layout)
    candidates = []
    for announcement_id, product_id, model_code, relative_path, registered, fields_json, parse_error in rows:
        old = json.loads(fields_json or "{}")
        if old.get("pdf_layout") == layout:
            continue  # 已是当前版式的解析结果
        candidates.append((announcement_id, product_id, model_code, relative_path, registered,
                           fields_json, old, parse_error))
    plan.scanned = len(candidates)
    paths = [str(pdf_root / item[3]) for item in candidates]
    for item, (new, error) in zip(candidates, _parse_all(paths, workers, parser)):
        announcement_id, product_id, model_code, relative_path, registered, fields_json, old, parse_error = item
        base = {"announcement_id": announcement_id, "source_product_id": product_id,
                "model_code": model_code, "relative_path": relative_path}
        if new is None:
            if error:
                plan.skipped.append({**base, "reason": "parse_error", "error": error})
            continue
        if new.get("pdf_layout") != layout:
            continue  # 不是本次要刷新的版式
        if limit is not None and len(plan.changes) >= limit:
            break
        plan.stale += 1
        path = pdf_root / relative_path
        if not path.is_file():
            plan.skipped.append({**base, "reason": "missing_file"})
            continue
        if not registered or _sha256(path) != registered:
            # 本地文件与登记版本不同（例如被人工替换），不能拿它的解析结果改写登记记录。
            plan.skipped.append({**base, "reason": "hash_mismatch"})
            continue
        merged = merge_fields(old, new)
        changed = sorted(key for key in set(old) | set(merged) if old.get(key) != merged.get(key))
        if not changed:
            plan.unchanged += 1
            continue
        plan.changes.append({
            **base, "sha256": registered, "fields_json": fields_json, "parse_error": parse_error,
            "merged": merged, "changed_keys": changed,
            "preserved_keys": sorted(key for key in old if key not in PARSER_KEYS),
            "before": {key: old.get(key) for key in changed},
            "after": {key: merged.get(key) for key in changed},
        })
    return plan


def apply_plan(conn: sqlite3.Connection, plan: Plan) -> int:
    """单事务写入；任一记录在计划后被改动就整体回滚。"""
    with conn:
        for item in plan.changes:
            cursor = conn.execute(
                "UPDATE announcement_fields SET fields_json = ?, parse_error = '' "
                "WHERE announcement_id = ? AND fields_json = ? AND parse_error IS ? "
                "AND EXISTS (SELECT 1 FROM documents d WHERE d.announcement_id = announcement_fields.announcement_id "
                "AND d.relative_path = ? AND d.sha256 = ? AND d.is_pdf = 1)",
                (json.dumps(item["merged"], ensure_ascii=False, sort_keys=True),
                 item["announcement_id"], item["fields_json"], item["parse_error"],
                 item["relative_path"], item["sha256"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"公告 {item['source_product_id']} 的解析字段或文档登记在计划后被改动，已整体回滚")
    return len(plan.changes)


def write_run(run_dir: Path, plan: Plan, meta: dict[str, Any]) -> Path:
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    base, suffix = run_dir, 1
    while True:  # 同一秒内的多次运行各留一份记录，不覆盖
        try:
            run_dir.mkdir()
            break
        except FileExistsError:
            suffix += 1
            run_dir = base.with_name(f"{base.name}-{suffix}")
    with (run_dir / "changes.jsonl").open("w", encoding="utf-8") as handle:
        for item in plan.changes:
            record = {key: item[key] for key in ("announcement_id", "source_product_id", "model_code",
                                                 "relative_path", "changed_keys", "before", "after")}
            # 原始整份字段逐条留档，写入后需要撤回时按公告 ID 原样恢复。
            record["original_fields_json"] = item["fields_json"]
            record["original_parse_error"] = item["parse_error"]
            record["sha256"] = item["sha256"]
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    with (run_dir / "skipped.jsonl").open("w", encoding="utf-8") as handle:
        for item in plan.skipped:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    return write_summary(run_dir, plan, meta)


def write_summary(run_dir: Path, plan: Plan, meta: dict[str, Any]) -> Path:
    """在同一份已留档的计划上记录执行结果，不另建目录或覆盖原始字段。"""
    summary = {**meta, **plan.summary()}
    temporary = run_dir / "summary.json.tmp"
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    temporary.replace(run_dir / "summary.json")
    return run_dir / "summary.json"


def _parser_digest() -> str:
    return hashlib.sha256(Path(review_export.__file__).read_bytes()).hexdigest()


def run(db: Path, *, layout: str, apply: bool, pdf_root: Path, catalog_db: Path, runs_dir: Path,
        workers: int = 1, limit: int | None = None, parser: Parser | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict[str, Any]:
    started = now()
    run_dir = runs_dir / f"{started:%Y%m%dT%H%M%SZ}-reparse-{layout}"
    meta = {"mode": "apply" if apply else "preview", "started_at": started.isoformat(),
            "db": str(db), "pdf_root": str(pdf_root), "parser_sha256": _parser_digest(),
            "limit": limit}
    if not apply:
        with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as conn:
            plan = build_plan(conn, pdf_root, layout, workers=workers, limit=limit, parser=parser)
        return {"summary": write_run(run_dir, plan, meta), "plan": plan, "applied": 0}
    with seed.database_lock(db):
        with closing(sqlite3.connect(db)) as conn:
            if conn.execute("SELECT 1 FROM ingestion_runs WHERE completed_at IS NULL").fetchone():
                raise RuntimeError("存在未完成采集，不能刷新解析字段")
            plan = build_plan(conn, pdf_root, layout, workers=workers, limit=limit, parser=parser)
            # 必须先保留可恢复的原始字段，再提交业务库。留档失败时尚未发生任何写回。
            summary = write_run(run_dir, plan, {**meta, "phase": "prepared"})
            try:
                applied = apply_plan(conn, plan) if plan.changes else 0
            except Exception as exc:
                write_summary(summary.parent, plan, {**meta, "phase": "failed", "applied": 0,
                                                     "error": f"{type(exc).__name__}: {exc}"})
                raise
        write_summary(summary.parent, plan, {**meta, "phase": "completed", "applied": applied})
        if applied:
            # 与采集收尾相同：提交后刷新一次统计，统计失败不回滚已写入的字段。
            seed.refresh_collection_status(db, catalog_db=catalog_db, pdf_root=pdf_root, reason="reparse")
    return {"summary": summary, "plan": plan, "applied": applied}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用当前解析器刷新存量公告参数页的解析字段（默认只预览）")
    parser.add_argument("--layout", choices=LAYOUTS, default=LAYOUTS[0],
                        help="只刷新新解析确认属于该版式、而库存仍是旧结果的记录")
    parser.add_argument("--apply", action="store_true", help="写入业务库；省略时只输出候选统计和差异清单")
    parser.add_argument("--limit", type=int, help="最多刷新前 N 条记录（按公告顺序），用于分段核对")
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1), help="本地解析进程数")
    parser.add_argument("--db", type=Path, default=seed.DEFAULT_SITE_DB)
    parser.add_argument("--catalog-db", type=Path, default=seed.DEFAULT_CATALOG_DB)
    parser.add_argument("--pdf-root", type=Path, default=seed.UPSTREAM_ROOT,
                        help="documents.relative_path 的解析根，默认上游根目录")
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须大于 0")
    db = args.db.expanduser().resolve()
    if not db.is_file():
        parser.error(f"业务库不存在：{db}")
    result = run(db, layout=args.layout, apply=args.apply, pdf_root=args.pdf_root.expanduser().resolve(),
                 catalog_db=args.catalog_db.expanduser().resolve(), runs_dir=args.runs_dir.expanduser().resolve(),
                 workers=max(1, args.workers), limit=args.limit)
    summary = result["plan"].summary()
    print(f"扫描未标记该版式的解析记录 {summary['scanned']} 条；待刷新 {summary['changed']} 条，"
          f"已是新结果 {summary['unchanged']} 条，跳过 {summary['skipped'] or 0}。")
    if summary["changed_keys"]:
        print("字段变化：" + "、".join(f"{key} {count}" for key, count in summary["changed_keys"].items()))
    if summary["preserved_keys"]:
        print("保留的非解析字段：" + "、".join(f"{key} {count}" for key, count in summary["preserved_keys"].items()))
    print(f"汇总与逐条差异：{result['summary'].parent}")
    if args.apply:
        print(f"已写入 {result['applied']} 条。")
    else:
        print("仅预览；加 --apply 写入业务库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
