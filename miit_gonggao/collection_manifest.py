#!/usr/bin/env python3
"""按 SHA256 冻结的汽车整车产品 ID 清单补采，默认只校验、预览。

复用 seed 的下载、解析、规范目录和逐份发布逻辑；不查询其他型号或公告批次。
新能源 scope 保持历史整车范围；通用 scope 另允许第 408、409 批的非新能源或能源待确认整车。
每份提交后记录 results.jsonl 和原子 progress.json；可用原清单恢复同一 run。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import sys
import time
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__:
    from . import collection as seed
    from .vehicle_categories import CHANNEL_LABELS, derive_channel, normalize_energy
    from .collection_report import write_report
    from .vehicle_classification import classify_vehicle
else:
    from miit_gonggao import collection as seed
    from miit_gonggao.vehicle_categories import CHANNEL_LABELS, derive_channel, normalize_energy
    from miit_gonggao.collection_report import write_report
    from miit_gonggao.vehicle_classification import classify_vehicle

SCOPE = "nev_complete_manifest_v1"
LEGACY_SCOPE = "uncollected_nev_complete_20260912"
AUTOMOTIVE_SCOPE = "automotive_complete_manifest_v1"
NON_NEV_BATCHES = frozenset({408, 409})
NEV_TYPES = {"纯电动", "插电式混合动力", "增程式", "燃料电池"}
SUCCESS = {"downloaded", "skipped_existing"}


# 请求节奏控制由 seed（collection）统一提供，两个入口共用同一实现与降速契约。
requested_interval = seed.requested_interval
RequestPace = seed.RequestPace
request_pace = seed.request_pace


def validate_catalog_energy(rows: list[tuple[str, str, str]], catalog_db: Path | None) -> None:
    """仅以同型号权威目录的具体能源补证；不采用清单自报的能源或泛新能源标签。"""
    if not rows:
        return
    if catalog_db is None:
        raise ValueError(f"清单含产品名未明确新能源的产品，且未提供目录核验：{rows[0][0]}")
    with closing(sqlite3.connect(catalog_db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        catalog_by_model: dict[str, set[str]] = {}
        for product_id, model, name in rows:
            if model not in catalog_by_model:
                catalog_by_model[model] = {
                    str(row[0] or "").strip() for row in conn.execute(
                        "SELECT energy_type FROM catalog_rows WHERE model_code=? COLLATE BINARY", (model,))
                } - {"", "新能源", "新能源汽车"}
            labels = catalog_by_model[model]
            energies = {normalize_energy("", "", "", label)[0] for label in labels}
            if len(energies) != 1 or not energies <= NEV_TYPES:
                raise ValueError(f"目录缺少唯一明确新能源证据或存在能源冲突：{product_id} ({model})")
            # 目录也不能把明确的普通混动产品名直接覆盖成纯电动等不相容能源。
            if any(normalize_energy("", name, "", label)[0] not in energies for label in labels):
                raise ValueError(f"公告产品名与目录能源不相容：{product_id} ({model})")


def load_manifest(path: Path, expected_hash: str, catalog_db: Path | None = None) -> dict[str, Any]:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_hash.lower():
        raise ValueError("manifest SHA256 不符，拒绝下载")
    manifest = json.loads(content)
    scope = manifest.get("scope")
    if manifest.get("schema_version") != 1 or scope not in {SCOPE, LEGACY_SCOPE, AUTOMOTIVE_SCOPE}:
        raise ValueError("manifest schema_version 或 scope 不符")
    products = manifest.get("products")
    if not isinstance(products, list) or not products:
        raise ValueError("manifest products 必须为非空列表")
    ids: set[str] = set()
    models: set[str] = set()
    needs_catalog: list[tuple[str, str, str]] = []
    for row in products:
        if not isinstance(row, dict):
            raise ValueError("manifest 产品行必须是对象")
        product_id = str(row.get("cpid") or "").strip()
        model = str(row.get("clxh") or "").strip()
        batch = str(row.get("gppc") or row.get("pc") or "").strip()
        name = str(row.get("clmc") or "").strip()
        if not product_id or product_id in ids or not model or not batch.isascii() or not batch.isdecimal():
            raise ValueError(f"产品 ID/型号/数字批次不合法或 ID 重复：{product_id!r}")
        if product_id != row["cpid"] or model != row["clxh"]:
            raise ValueError(f"产品 ID 和型号必须为无首尾空白的字符串：{product_id!r}")
        if classify_vehicle(model, name, row)["inclusion_gate"] != "accepted":
            raise ValueError(f"清单含非汽车整车或范围待确认产品：{product_id}")
        needs_nev = scope != AUTOMOTIVE_SCOPE or int(batch) not in NON_NEV_BATCHES
        if needs_nev and normalize_energy("", name, "", "")[0] not in NEV_TYPES:
            needs_catalog.append((product_id, model, name))
        ids.add(product_id)
        models.add(model)
    if manifest.get("expected_products") != len(ids) or manifest.get("expected_models") != len(models):
        raise ValueError("manifest expected_products/expected_models 与实际去重数量不符")
    validate_catalog_energy(needs_catalog, catalog_db)
    return manifest


database_lock = seed.database_lock


@dataclass
class StopFlag:
    reason: str = ""

    def request(self, signum: int, _frame: Any) -> None:
        self.reason = f"收到 {signal.Signals(signum).name}，当前产品提交后停止"


@contextmanager
def signal_handlers(stop: StopFlag):
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    for sig in previous:
        signal.signal(sig, stop.request)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def local_document(conn: sqlite3.Connection, product_id: str, pdf_root: Path) -> tuple[str, str, int] | None:
    """只跳过实际文件有效的 PDF；已有 PDF 的解析问题保留供专项处理。"""
    row = conn.execute(
        "SELECT d.relative_path,d.is_pdf,d.bytes,d.sha256,d.error,f.fields_json,f.parse_error,a.raw_json "
        "FROM announcements a JOIN documents d ON d.announcement_id=a.id "
        "LEFT JOIN announcement_fields f ON f.announcement_id=a.id WHERE a.source_product_id=?",
        (product_id,),
    ).fetchone()
    if not row or row[1] != 1 or row[2] <= 0 or not row[3]:
        return None
    path = (pdf_root / row[0]).resolve()
    if not path.is_relative_to(pdf_root.resolve()):
        raise RuntimeError(f"文档路径越过 PDF 根目录：{product_id}")
    try:
        content = path.read_bytes()
    except OSError:
        return None
    if len(content) != row[2] or not content.startswith(b"%PDF") or hashlib.sha256(content).hexdigest() != row[3]:
        return None
    error = row[6] or row[4] or ""
    try:
        fields = json.loads(row[5]) if row[5] else None
        if not isinstance(fields, dict) or not fields:
            error = error or "已有有效 PDF 缺少解析字段"
    except (TypeError, ValueError):
        error = error or "已有有效 PDF 的解析字段 JSON 异常"
    if error:
        return "parse_failed", f"已有 PDF 解析问题，未重复下载：{error}", len(content)
    from miit_gonggao.images import read_image_result

    image_result = read_image_result(json.loads(row[7]), path.parent)
    if image_result and image_result.get("failed"):
        return "image_failed", "图片下载不完整：已有 PDF 有效，图片异常仍需补采", len(content)
    return "skipped_existing", "", len(content)


def vehicle_id(conn: sqlite3.Connection, row: dict[str, Any]) -> int:
    existing = conn.execute("SELECT id FROM vehicles WHERE announcement_model_code=?", (row["clxh"],)).fetchone()
    if existing:
        return int(existing[0])
    category = CHANNEL_LABELS[derive_channel(row["clxh"], row["clmc"])[0]]
    return seed.upsert_vehicle(conn, {
        "model_code": row["clxh"], "catalog": "公告正式批次清单",
        "batch": str(row.get("gppc") or row.get("pc")), "category": category,
        "common_name": "", "seq": "", "company": str(row.get("qymc") or ""),
    })


def claim_run(conn: sqlite3.Connection, args: argparse.Namespace, manifest: dict[str, Any]) -> int:
    seed.ensure_schema(conn)
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        unfinished = [r[0] for r in conn.execute("SELECT id FROM ingestion_runs WHERE completed_at IS NULL")]
        if any(run_id != args.resume_run for run_id in unfinished):
            raise RuntimeError(f"存在其他未结束采集轮次，拒绝启动：{unfinished}")
        if args.resume_run is not None:
            row = conn.execute("SELECT selector_json FROM ingestion_runs WHERE id=?", (args.resume_run,)).fetchone()
            selector = json.loads(row[0]) if row else {}
            if (selector.get("manifest_sha256") != args.manifest_sha256.lower()
                    or selector.get("scope") != manifest['scope']
                    or selector.get("mode") != "cached_product_manifest"):
                raise RuntimeError("恢复轮次不存在或 manifest SHA256/scope 不匹配")
            run_id = args.resume_run
            conn.execute("UPDATE ingestion_runs SET completed_at=NULL WHERE id=?", (run_id,))
            conn.execute("UPDATE run_models SET status='interrupted', error='上次进程未完成该型号' "
                         "WHERE run_id=? AND status='querying'", (run_id,))
        else:
            selector = {"mode": "cached_product_manifest", "scope": manifest['scope'],
                        "manifest_sha256": args.manifest_sha256.lower(), "manifest": str(args.manifest.resolve()),
                        "expected_products": manifest["expected_products"], "run_dir": str(args.run_dir.resolve()),
                        "source_boundary": manifest.get("source_boundary")}
            cursor = conn.execute(
                "INSERT INTO ingestion_runs(started_at,selector_json,selected_models,parse_failures,publish_failures) "
                "VALUES (?,?,?,0,0)", (seed.utc_now(), json.dumps(selector, ensure_ascii=False),
                                      manifest["expected_models"]),
            )
            run_id = int(cursor.lastrowid)
        conn.commit()
        return run_id
    except BaseException:
        conn.rollback()
        raise


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def failure_status(message: str) -> str:
    for prefix, status in (("下载失败", "download_failed"), ("解析失败", "parse_failed"),
                           ("发布失败", "publish_failed"), ("接口返回非 PDF", "non_pdf")):
        if message.startswith(prefix):
            return status
    return "storage_failed"


def is_source_generation_error(message: str, pdf_root: Path) -> bool:
    """仅识别已核验的单产品参数页缺失/生成异常；未知 HTML 仍执行降速保护。"""
    if not message.startswith("接口返回非 PDF"):
        return False
    _prefix, separator, relative = message.partition("；原文件：")
    if not separator or not relative:
        return False
    path = (pdf_root / relative).resolve()
    if not path.is_relative_to(pdf_root.resolve()):
        return False
    try:
        content = path.read_bytes()
    except OSError:
        return False
    return ("获取参数页发生错误".encode() in content
            and (b"Byte data not found at location" in content
                 or "没有找到参数页".encode() in content))


def collect(args: argparse.Namespace, manifest: dict[str, Any], stop: StopFlag | None = None) -> int:
    stop = stop or StopFlag()
    args.site_db.parent.mkdir(parents=True, exist_ok=True)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    for name in ("results.jsonl", "progress.json"):
        if (args.run_dir / name).exists() and args.resume_run is None:
            raise RuntimeError("run-dir 已有采集产物；请选择新目录或显式 --resume-run")
    progress_path = args.run_dir / "progress.json"
    if args.resume_run is not None and progress_path.exists():
        old = json.loads(progress_path.read_text())
        if old.get("run_id") != args.resume_run or old.get("manifest_sha256") != args.manifest_sha256.lower():
            raise RuntimeError("run-dir 不属于指定恢复轮次/清单")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in manifest["products"]:
        grouped.setdefault(row["clxh"], []).append(row)
    with (request_pace(args) as pace, database_lock(args.site_db),
          closing(sqlite3.connect(args.site_db, timeout=5)) as conn):
        try:
            run_id = claim_run(conn, args, manifest)
            started = time.monotonic()
            counters: Counter[str] = Counter()
            processed = pdf_bytes = downloaded_bytes = attempts = source_errors = 0
            download_seconds = 0.0
            consecutive_failures = 0

            def progress(state: str) -> dict[str, Any]:
                remaining = manifest["expected_products"] - processed
                return {"run_id": run_id, "pid": os.getpid(), "status": state, "updated_at": seed.utc_now(),
                        "manifest_sha256": args.manifest_sha256.lower(), "selected": manifest["expected_products"],
                        "selected_models": len(grouped), "processed": processed, "remaining": remaining,
                        "successful": counters["downloaded"] + counters["skipped_existing"],
                        "results": dict(counters), "accepted_pdf_bytes": pdf_bytes,
                        "downloaded_bytes": downloaded_bytes,
                        "attempted_downloads": attempts, "elapsed_seconds": round(time.monotonic() - started, 3),
                        "estimated_remaining_seconds": (round(download_seconds / attempts * remaining)
                                                        if attempts else None),
                        "stop_reason": stop.reason, "request_interval": list(seed.core.REQUEST_MIN_INTERVAL),
                        "requested_interval": list(pace.requested) if pace.requested is not None else None,
                        "pace_warning": pace.warning, "source_generation_errors": source_errors}

            try:
                atomic_json(progress_path, progress("running"))
                with (args.run_dir / "results.jsonl").open("a", encoding="utf-8") as results:
                    for order, (model, rows) in enumerate(grouped.items(), 1):
                        if stop.reason:
                            break
                        vid = vehicle_id(conn, rows[0])
                        seed.begin_model(conn, run_id, order, vid)
                        errors: list[str] = []
                        model_processed = 0
                        for row in rows:
                            if stop.reason:
                                break
                            item_started = time.monotonic()
                            cached = local_document(conn, row["cpid"], args.pdf_root)
                            attempted = cached is None
                            if cached:
                                status, message, byte_count = cached
                            else:
                                attempts += 1
                                try:
                                    ok, message = seed.store_announcement(
                                        conn, row=row, vehicle_id=vid, market_name=model,
                                        download_root=args.pdf_root / "downloads" / "announcement_site",
                                        pdf_root=args.pdf_root, catalog_db=args.catalog_db,
                                    )
                                    status = "downloaded" if ok else failure_status(message)
                                    current = local_document(conn, row["cpid"], args.pdf_root)
                                    byte_count = current[2] if current else 0
                                    if ok and (not current or current[0] not in {"skipped_existing", "image_failed"}):
                                        status, message = "parse_failed", "下载返回成功但本地 PDF/解析校验未通过"
                                    conn.commit()
                                except Exception as exc:
                                    conn.rollback()
                                    status, byte_count = "storage_failed", 0
                                    message = f"存储异常：{type(exc).__name__}: {exc}"
                                    stop.reason = message
                                download_seconds += time.monotonic() - item_started
                            elapsed = time.monotonic() - item_started
                            processed += 1
                            model_processed += 1
                            counters[status] += 1
                            image_failed = "图片下载不完整：" in message
                            if image_failed:
                                if status != "image_failed":
                                    counters["image_failed"] += 1
                                pace.slow_down("image_failed")
                            pdf_bytes += byte_count if status in SUCCESS else 0
                            downloaded_bytes += byte_count if status == "downloaded" else 0
                            if status not in SUCCESS or image_failed:
                                errors.append(f"{row['cpid']}: {message}")
                                print(f"[{processed}/{manifest['expected_products']}] {model} "
                                      f"{row['cpid']} {status}: {message}",
                                      file=sys.stderr, flush=True)
                            source_error = status == "non_pdf" and is_source_generation_error(message, args.pdf_root)
                            source_errors += int(source_error)
                            if status == "download_failed" or (status == "non_pdf" and not source_error):
                                pace.slow_down(status)
                            consecutive_failures = consecutive_failures + 1 if status == "download_failed" else 0
                            if consecutive_failures >= 3:
                                stop.reason = "连续 3 次下载失败，停止本次采集"
                            record = {"run_id": run_id, "pid": os.getpid(), "at": seed.utc_now(), "cpid": row["cpid"],
                                      "model_code": model, "batch": str(row.get("gppc") or row.get("pc")),
                                      "status": status, "error": message, "bytes": byte_count,
                                      "image_failed": image_failed,
                                      "attempted_download": attempted, "elapsed_seconds": round(elapsed, 3),
                                      "source_generation_error": source_error}
                            results.write(json.dumps(record, ensure_ascii=False) + "\n")
                            results.flush()
                            os.fsync(results.fileno())
                            atomic_json(progress_path, progress("running"))
                            if processed % 50 == 0:
                                print(json.dumps(progress("running"), ensure_ascii=False), flush=True)
                        if model_processed == len(rows):
                            seed.finish_model(conn, run_id, vid, "partial" if errors else "done", "; ".join(errors))
            except (KeyboardInterrupt, InterruptedError) as exc:
                conn.rollback()
                stop.reason = stop.reason or f"进程中断：{type(exc).__name__}"
            except Exception as exc:
                conn.rollback()
                stop.reason = f"运行中断：{type(exc).__name__}: {exc}"
                print(stop.reason, file=sys.stderr, flush=True)
            finally:
                try:
                    seed.finish_run(conn, run_id, {
                        "download_failures": counters["download_failed"], "non_pdf_documents": counters["non_pdf"],
                        "parse_failures": counters["parse_failed"], "publish_failures": counters["publish_failed"],
                        "image_failures": counters["image_failed"],
                    }, stop.reason)
                    all_successful = (sum(counters[key] for key in SUCCESS) == manifest["expected_products"]
                                      and not counters["image_failed"])
                    state = "interrupted" if stop.reason else "complete" if all_successful else "partial"
                    atomic_json(progress_path, progress(state))
                finally:
                    seed.refresh_collection_status(args.site_db, catalog_db=args.catalog_db,
                                                   pdf_root=args.pdf_root, run_id=run_id,
                                                   reason="manifest_collection")
            try:
                write_report(args.site_db, run_id, args.run_dir, args.catalog_db)
            except Exception as exc:
                print(f"采集报告生成失败：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            print(json.dumps(progress(state), ensure_ascii=False), flush=True)
            return 0 if state == "complete" else 130 if state == "interrupted" else 2
        finally:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--site-db", type=Path, default=seed.DEFAULT_SITE_DB)
    parser.add_argument("--pdf-root", type=Path, default=seed.UPSTREAM_ROOT)
    parser.add_argument("--catalog-db", type=Path, default=seed.DEFAULT_CATALOG_DB)
    parser.add_argument("--resume-run", type=int)
    parser.add_argument("--apply", action="store_true", help="实际下载；默认只校验清单")
    parser.add_argument("--min-interval", type=float, help="本进程请求最小间隔（秒），须与最大间隔同时给出")
    parser.add_argument("--max-interval", type=float, help="本进程请求最大间隔（秒），遇下载失败/非 PDF 恢复默认")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest, args.manifest_sha256, catalog_db=args.catalog_db)
        interval = requested_interval(args)
        if not args.apply:
            print(json.dumps({"status": "dry_run", "scope": manifest["scope"], "products": manifest["expected_products"],
                              "models": manifest["expected_models"], "manifest_sha256": args.manifest_sha256.lower(),
                              "site_db": str(args.site_db), "pdf_root": str(args.pdf_root),
                              "request_interval": list(interval or seed.core.REQUEST_MIN_INTERVAL),
                              "requested_interval": list(interval) if interval is not None else None},
                             ensure_ascii=False))
            return 0
        with signal_handlers(stop := StopFlag()):
            return collect(args, manifest, stop)
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"拒绝采集：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
