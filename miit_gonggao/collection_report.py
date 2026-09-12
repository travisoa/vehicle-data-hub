#!/usr/bin/env python3
"""生成一轮采集的 Markdown 报告。

采集脚本每轮结束时自动调用；也可独立跑来补出历史某轮的报告：

    python3 -m miit_gonggao.collection_report            # 最新一轮
    python3 -m miit_gonggao.collection_report --run 16     # 指定轮次
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

if __package__:
    from .collection_coverage import best_model_outcomes, coverage_buckets
else:
    from miit_gonggao.collection_coverage import best_model_outcomes, coverage_buckets


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_ROOT = Path(os.environ.get(
    "VEHICLE_DATA_HUB_ROOT", str(Path(__file__).resolve().parent.parent)
)).expanduser().resolve()
DEFAULT_DB = UPSTREAM_ROOT / "data" / "announcement_site.sqlite"
DEFAULT_CATALOG_DB = UPSTREAM_ROOT / "data" / "jianmian_catalog.sqlite"
DEFAULT_OUT_DIR = PROJECT_ROOT / "var" / "reports"

OUTCOME_LABEL = {
    "done": "已完成",
    "partial": "存在下载、解析、发布异常或非 PDF",
    "interrupted": "中断未完成",
    "querying": "未收尾",
    "no_match": "公告系统查无此型号",
    "query_failed": "查询失败",
    "no_document": "尚未取得有效参数页",
    "awaiting_effective": "待正式生效",
}
MAX_DETAIL_ROWS = 50


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_（无）_", ""]
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    out.append("")
    return out


def _duration(started: str, completed: str | None) -> str:
    if not completed:
        return "进行中"
    try:
        delta = datetime.fromisoformat(completed) - datetime.fromisoformat(started)
    except ValueError:
        return "-"
    total = int(delta.total_seconds())
    return f"{total // 3600} 小时 {total % 3600 // 60} 分" if total >= 3600 else f"{total // 60} 分 {total % 60} 秒"



def catalog_coverage(conn: sqlite3.Connection, catalog_db: Path) -> dict[str, int]:
    if not catalog_db.is_file():
        return {}
    buckets = coverage_buckets(conn, catalog_db)
    return {"目录去重型号": sum(map(len, buckets.values())),
            **{label: len(codes) for label, codes in buckets.items()}}


def build_report(conn: sqlite3.Connection, run_id: int, catalog_db: Path) -> str:
    conn.row_factory = sqlite3.Row
    run = conn.execute("SELECT * FROM ingestion_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise SystemExit(f"没有 run_id={run_id} 的采集记录")

    parse_count = dict(run).get("parse_failures")
    publish_count = dict(run).get("publish_failures")

    lines = [f"# 采集报告 run{run_id}", "",
             f"生成于 {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}", ""]

    lines += ["## 本轮概要", ""]
    lines += _table(["项", "值"], [
        ["开始", run["started_at"]],
        ["结束", run["completed_at"] or "（未记录，可能中断）"],
        ["耗时", _duration(run["started_at"], run["completed_at"])],
        ["选中型号", run["selected_models"]],
        ["查询失败", run["query_failures"]],
        ["下载失败" if parse_count is not None else "下载/解析失败（历史合并口径）",
         run["download_failures"]],
        ["解析失败", run["parse_failures"] if parse_count is not None else "未单独记录"],
        ["发布失败", run["publish_failures"] if publish_count is not None else "未单独记录"],
        ["非 PDF", run["non_pdf_documents"]],
    ])

    outcomes = list(conn.execute(
        "SELECT status, COUNT(*) n FROM run_models WHERE run_id = ? GROUP BY status ORDER BY n DESC",
        (run_id,)))
    lines += ["## 本轮结局分布", ""]
    lines += _table(["结局", "说明", "型号数"],
                    [[r["status"], OUTCOME_LABEL.get(r["status"], "-"), r["n"]] for r in outcomes])

    problems = list(conn.execute(
        "SELECT v.announcement_model_code code, v.market_name name, rm.status, rm.error "
        "FROM run_models rm JOIN vehicles v ON v.id = rm.vehicle_id "
        "WHERE rm.run_id = ? AND rm.status <> 'done' ORDER BY rm.status, v.announcement_model_code",
        (run_id,)))
    lines += ["## 本轮需要关注的型号", ""]
    if problems:
        shown = problems[:MAX_DETAIL_ROWS]
        lines += _table(["车辆型号", "市场名", "结局", "说明"],
                        [[p["code"], p["name"] or "-", p["status"],
                          (p["error"] or "-").replace("|", "\\|")[:80]] for p in shown])
        if len(problems) > MAX_DETAIL_ROWS:
            lines += [f"_另有 {len(problems) - MAX_DETAIL_ROWS} 条未列出，"
                      f"用 run_models 表按 run_id={run_id} 查全部。_", ""]
    else:
        lines += ["本轮全部型号均已完成。", ""]

    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='tracking_attempts'").fetchone():
        tracked = list(conn.execute(
            'SELECT e.model_code,e.batch,e.kind,t.status,t.error FROM tracking_attempts t '
            'JOIN tracking_events e USING(event_id) WHERE t.run_id=? ORDER BY e.batch,e.model_code',
            (run_id,)))
        if tracked:
            lines += ["## 本轮公告与扩展事件", "",
                      "逐事件结局独立于型号历史最好结局；旧版本已下载不代表新扩展已完成。", ""]
            lines += _table(["型号", "目标批次", "事件类型", "结局", "原因"], [
                [r[0], r[1], r[2], r[3], (r[4] or '-').replace('|', '\\|')[:120]]
                for r in tracked[:MAX_DETAIL_ROWS]
            ])
            if len(tracked) > MAX_DETAIL_ROWS:
                lines += [f"另有 {len(tracked) - MAX_DETAIL_ROWS} 条事件，见 tracking_attempts。", ""]

    best = best_model_outcomes(conn)
    tally: dict[str, int] = {}
    for status in best.values():
        tally[status] = tally.get(status, 0) + 1
    lines += ["## 全库累计（每个型号取历次最好结局）", ""]
    lines += _table(["结局", "说明", "型号数"],
                    [[k, OUTCOME_LABEL.get(k, "-"), v]
                     for k, v in sorted(tally.items(), key=lambda kv: -kv[1])])

    coverage = catalog_coverage(conn, catalog_db)
    if coverage and coverage["目录去重型号"]:
        total = coverage["目录去重型号"]
        lines += ["## 减免目录覆盖", ""]
        lines += _table(["档位", "型号数", "占比"],
                        [[k, v, f"{v / total:.1%}"] for k, v in coverage.items() if k != "目录去重型号"])
        lines += [f"目录去重型号合计 **{total}**，"
                  f"覆盖率 **{coverage['已下载'] / total:.1%}**。", ""]
        lines += ["> 自动补采只使用逐事件跟踪计划：60 天、最近 2 个正式公告批次；"
                  "未生效公示按 60 天跟踪并等待目标批次发布。超窗旧缺口保留记录，"
                  "不因所在覆盖档位而进入每周回查。", ""]

    return "\n".join(lines) + "\n"


def write_report(db_path: Path, run_id: int | None = None, out_dir: Path = DEFAULT_OUT_DIR,
                 catalog_db: Path = DEFAULT_CATALOG_DB) -> Path:
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        if run_id is None:
            row = conn.execute("SELECT MAX(id) FROM ingestion_runs").fetchone()
            if not row or row[0] is None:
                raise SystemExit("库里还没有任何采集记录")
            run_id = int(row[0])
        text = build_report(conn, run_id, catalog_db)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"ingestion_run{run_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=int, help="采集轮次 id，默认最新一轮")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--catalog-db", type=Path, default=DEFAULT_CATALOG_DB)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error(f"公告业务库不存在：{args.db}")
    path = write_report(args.db, args.run, args.out_dir, args.catalog_db)
    print(f"采集报告: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
