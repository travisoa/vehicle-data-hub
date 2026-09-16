#!/usr/bin/env python3
"""从正式公告发布识别被重新发布的产品，作为强制刷新清单。

公示只用于提前了解，不作为任何采集入口。正式发布侧没有"变更扩展"标记字段，
唯一可用判据是同一产品 ID 在更晚的正式批次里再次出现：本地已有该产品的有效
参数页，而官方已在更高批次重新发布，本地那份就可能不是当前有效版本。

两个来源都只读正式公告数据，重发判据相同，本地侧的入选门槛并不相同：
- ``status``：读已落盘的收录统计记录，复用 ``gonggao status`` 的 refresh 口径，
  不重算；记录过期时拒绝，交由统计入口显式刷新。该口径只收本地 PDF 已解析
  （``state == 'parsed'``）的产品。
- ``batch``：按批次读 ``downloads/announcement_batches/batch<N>.json``，该缓存
  由 doCpQuery 正式接口枚举而来。完整性沿用上游校验，过期缓存保留为历史基线
  并单独标记，不冒充官方当前全集。该口径只要求本地有有效 PDF，不要求解析成功。

因此本地已下载但解析失败的产品被重发时，只有 ``batch`` 口径会选中；``status``
口径把它留在统计的 ``pdf_unparsed`` 候选里，走补采入口。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class RepublishedError(RuntimeError):
    """清单来源不可用；调用方应停止采集而不是退回到公示。"""


@dataclass
class RepublishedSource:
    origin: str
    rows: list[dict[str, Any]]
    batches: list[int] = field(default_factory=list)
    generation: str = ""
    stale_batches: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    total_candidates: int | None = None

    @property
    def candidate_total(self) -> int:
        """未截断前的候选产品总数；截断后仍用它说明本轮之外还剩多少。"""
        return len(self.rows) if self.total_candidates is None else self.total_candidates

    def limited(self, limit: int | None) -> "RepublishedSource":
        """按 --limit 截断本轮清单，让快照、统计与实际处理范围出自同一份 rows。"""
        if limit is None or limit >= len(self.rows):
            return self
        remaining = len(self.rows) - limit
        return replace(
            self, rows=self.rows[:limit], total_candidates=self.candidate_total,
            notes=[*self.notes, f"本轮按 --limit {limit} 只处理前 {limit} 个候选产品，"
                                f"其余 {remaining} 个留待后续轮次。"])

    @property
    def label(self) -> str:
        if self.origin == "status":
            return f"正式发布重发（统计记录 {self.generation}）"
        return "正式发布重发（批次 " + ",".join(str(b) for b in self.batches) + "）"

    def snapshot_scope(self) -> str:
        if self.origin == "status":
            return f"status_{self.generation}"
        return "batch" + "_".join(str(b) for b in self.batches)


def parse_batches(values: list[str]) -> list[int]:
    """解析批次参数：支持 409、405-409、405,407 三种写法的任意组合。"""
    batches: set[int] = set()
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start, _, end = part.partition("-")
                if not start.strip().isdigit() or not end.strip().isdigit():
                    raise RepublishedError(f"批次区间格式无效：{part}")
                low, high = int(start), int(end)
                if low > high:
                    raise RepublishedError(f"批次区间起点大于终点：{part}")
                batches.update(range(low, high + 1))
            elif part.isdigit():
                batches.add(int(part))
            else:
                raise RepublishedError(f"批次格式无效：{part}")
    if not batches:
        raise RepublishedError("未解析出任何批次")
    return sorted(batches)


def _local_products(site_db: Path) -> dict[str, dict[str, Any]]:
    """本地已有有效参数页的产品：只有这些才谈得上"需要刷新到新版本"。"""
    if not site_db.exists():
        raise RepublishedError(f"业务库不存在：{site_db}")
    with closing(sqlite3.connect(f"file:{site_db}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT a.source_product_id, b.batch, a.model_code, a.company, a.trademark, a.product_name "
            "FROM announcements a JOIN batches b ON b.id = a.batch_id "
            "JOIN documents d ON d.announcement_id = a.id "
            "WHERE d.is_pdf = 1 AND d.bytes > 0"
        ).fetchall()
    local: dict[str, dict[str, Any]] = {}
    for product_id, batch, model_code, company, trademark, product_name in rows:
        if not str(batch or "").isdigit():
            continue  # 非数字批次无法与官方批次比较，留给按型号的常规采集。
        local[str(product_id)] = {
            "local_batch": int(batch), "model_code": str(model_code or ""),
            "company": str(company or ""), "trademark": str(trademark or ""),
            "product_name": str(product_name or ""),
        }
    return local


def _load_cache(cache_dir: Path, batch: int) -> tuple[list[dict[str, Any]], bool]:
    """读取一批的正式枚举缓存；返回产品行与是否已过新鲜期。"""
    from scripts import announcement_catalog_gap as gap

    path = cache_dir / f"batch{batch}.json"
    if not path.is_file():
        raise RepublishedError(f"第{batch}批没有本地枚举缓存：{path}；"
                               f"先执行 scripts/announcement_catalog_gap.py --batch {batch} --fetch-only")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RepublishedError(f"第{batch}批缓存无法读取：{exc}") from exc
    # 完整性契约与统计模块一致：只在新鲜期上放宽，其余校验不放宽。
    if not gap.cache_is_complete(payload, fresh=False) or payload.get("failures"):
        raise RepublishedError(f"第{batch}批缓存未通过完整性校验，不能作为正式发布证据：{path}")
    if int(payload.get("batch", -1)) != batch:
        raise RepublishedError(f"第{batch}批缓存内批次与文件名不一致：{path}")
    products = payload.get("products")
    if not isinstance(products, list):
        raise RepublishedError(f"第{batch}批缓存缺少产品清单：{path}")
    try:
        fetched = datetime.fromisoformat(payload["fetched_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RepublishedError(f"第{batch}批缓存的抓取时间无效：{path}") from exc
    stale = datetime.now(timezone.utc) > fetched + timedelta(days=gap.CACHE_MAX_AGE_DAYS)
    return products, stale


def from_batches(site_db: Path, batches: list[int], cache_dir: Path) -> RepublishedSource:
    from miit_gonggao.vehicle_classification import classify_vehicle

    local = _local_products(site_db)
    selected: dict[str, dict[str, Any]] = {}
    stale_batches: list[int] = []
    excluded = 0
    for batch in batches:
        products, stale = _load_cache(cache_dir, batch)
        if stale:
            stale_batches.append(batch)
        for item in products:
            product_id = str(item.get("cpid") or "").strip()
            if not product_id:
                continue
            if str(item.get("gppc") or item.get("pc") or "") != str(batch):
                continue
            known = local.get(product_id)
            if not known or known["local_batch"] >= batch:
                continue
            # 与收录统计同口径：底盘等非整车不属于本站范围，重发也不刷新。
            scope = classify_vehicle(str(item.get("clxh") or known["model_code"]),
                                     str(item.get("clmc") or known["product_name"]), item)
            if scope["inclusion_gate"] == "excluded":
                excluded += 1
                continue
            previous = selected.get(product_id)
            if previous and previous["republished_batch"] >= batch:
                continue
            selected[product_id] = {
                "product_id": product_id,
                "model_code": str(item.get("clxh") or known["model_code"]).strip(),
                "company": str(item.get("qymc") or known["company"]),
                "trademark": str(item.get("cpsb") or known["trademark"]),
                "product_name": str(item.get("clmc") or known["product_name"]),
                "republished_batch": batch,
                "local_batch": known["local_batch"],
            }
    notes = ["正式批次枚举缓存来自 doCpQuery，公示不参与本清单。",
             "只选本地已有有效参数页且本地批次更低的产品；缺参数页的属于收录缺口，走补采入口。"]
    if excluded:
        notes.append(f"按收录范围排除底盘等非整车重发 {excluded} 条。")
    if stale_batches:
        notes.append("以下批次缓存已过新鲜期，按历史基线使用，不代表官方当前全集："
                     + "、".join(f"第{b}批" for b in stale_batches))
    rows = sorted(selected.values(), key=lambda row: (row["republished_batch"], row["model_code"], row["product_id"]))
    return RepublishedSource(origin="batch", rows=rows, batches=list(batches),
                             stale_batches=stale_batches, notes=notes)


def from_status(site_db: Path, *, catalog_db: Path | None = None, pdf_root: Path | None = None,
                cache_dir: Path | None = None, out_dir: Path | None = None,
                allow_stale: bool = False) -> RepublishedSource:
    from miit_gonggao import collection_status

    state = collection_status.read_status(site_db, catalog_db=catalog_db, pdf_root=pdf_root,
                                          cache_dir=cache_dir, out_dir=out_dir)
    if state["status"] != "ready" and not allow_stale:
        reasons = "、".join(state.get("stale_reasons") or []) or state["status"]
        raise RepublishedError(
            f"收录统计记录当前为 {state['status']}（{reasons}），刷新清单可能漏项；"
            "先执行 main.py gonggao status --refresh，或改用 --republished-batch 现算。")
    snapshot = state.get("snapshot")
    if not snapshot:
        raise RepublishedError("尚无收录统计记录；先执行 main.py gonggao status --refresh")
    generation = str(snapshot.get("generation") or "")
    # 与 read_status 同一套推导：自定义业务库时两处不能各拼各的目录。
    output = collection_status.status_output_dir(site_db, out_dir)
    candidates_path = output / "snapshots" / generation / "candidates.json"
    if not candidates_path.is_file():
        raise RepublishedError(f"统计记录 {generation} 缺少候选明细：{candidates_path}")
    try:
        candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RepublishedError(f"候选明细无法读取：{exc}") from exc
    refresh = candidates.get("refresh")
    if not isinstance(refresh, list):
        raise RepublishedError(f"候选明细缺少 refresh 数组：{candidates_path}")
    rows: list[dict[str, Any]] = []
    for item in refresh:
        product_id = str(item.get("product_id") or "").strip()
        batch, local_batch = item.get("batch"), item.get("db_batch")
        if not product_id or not isinstance(batch, int) or not isinstance(local_batch, int):
            raise RepublishedError(f"候选明细的 refresh 行缺少产品 ID 或批次：{item}")
        rows.append({
            "product_id": product_id,
            "model_code": str(item.get("model_code") or "").strip(),
            "company": str(item.get("company") or ""),
            "trademark": str(item.get("trademark") or ""),
            "product_name": str(item.get("product_name") or ""),
            "republished_batch": batch,
            "local_batch": local_batch,
        })
    rows.sort(key=lambda row: (row["republished_batch"], row["model_code"], row["product_id"]))
    batches = sorted({row["republished_batch"] for row in rows})
    notes = [f"清单取自收录统计记录 {generation} 的 refresh 候选，未重算。",
             "统计口径为本地已下载并解析、且官方在更高正式批次重新发布的产品。"]
    if state["status"] != "ready":
        notes.append("统计记录当前为 " + state["status"] + "，本轮按显式允许过期执行。")
    return RepublishedSource(origin="status", rows=rows, batches=batches,
                             generation=generation, notes=notes)


def verify_against_api(rows: list[dict[str, Any]], *, query: Any = None,
                       on_result: Any = None) -> dict[str, list[dict[str, Any]]]:
    """按型号查正式接口，核实记录批次是否真的取得到。

    批次枚举缓存与按型号查询是两个接口，口径并不总是一致：缓存把某产品列进更高
    批次，不等于查询接口会把那一批当作该型号的当前有效版本。不核实就采集，这类
    条目每轮都会重新入选、逐个请求一遍，再被 ``awaiting_effective`` 挡下来。

    只读接口，不下载、不写库。返回四类：
    - ``confirmed``：接口最高批次已达记录批次，是真重发；
    - ``stale_record``：接口最高批次更低，记录与源不符，采集必然跳过；
    - ``missing``：接口查不到该精确型号；
    - ``failed``：本次查询失败，未核实，不能据此下结论。
    """
    from miit_gonggao import core

    query = query or core.query_all_pages
    result: dict[str, list[dict[str, Any]]] = {
        "confirmed": [], "stale_record": [], "missing": [], "failed": []}
    for index, row in enumerate(rows, start=1):
        model = str(row.get("model_code") or "").strip()
        recorded = row.get("republished_batch")
        outcome = dict(row)
        try:
            api_rows = query(model_code=model, page_size=50)
        except Exception as exc:  # noqa: BLE001 - 单条失败不能中断整批核实
            outcome["verify_error"] = f"{type(exc).__name__}: {exc}"
            bucket = "failed"
        else:
            batches = sorted(
                int(value) for item in api_rows
                if str(item.get("clxh") or "").strip().upper() == model.upper()
                and (value := str(item.get("gppc") or item.get("pc") or "")).isdigit()
            )
            outcome["api_batches"] = batches
            outcome["api_latest_batch"] = batches[-1] if batches else None
            if not batches:
                bucket = "missing"
            elif isinstance(recorded, int) and batches[-1] >= recorded:
                bucket = "confirmed"
            else:
                bucket = "stale_record"
        result[bucket].append(outcome)
        if on_result is not None:
            on_result(index, len(rows), model, bucket, outcome)
    return result
