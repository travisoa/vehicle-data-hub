"""构建报告与采集报告共用的目录覆盖判定；不把失败或中断当作查无。"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

COVERAGE_DOWNLOADED = "已下载"
COVERAGE_NO_PDF = "已查到公告无参数页"
COVERAGE_NO_MATCH = "公告系统查无此型号"
COVERAGE_UNSEEN = "未进候选池"
COVERAGE_WAITING = "待正式生效"
COVERAGE_RETRY = "采集失败或未完成，待重试"


# 成功/明确的查询结局不因后来一次网络失败或中断降级；同级采用较新记录。
OUTCOME_RANK = {"done": 5, "partial": 4, "no_match": 3, "awaiting_effective": 3,
                "query_failed": 2, "interrupted": 2, "querying": 1}


def best_model_outcomes(conn: sqlite3.Connection) -> dict[int, str]:
    best: dict[int, str] = {}
    for vid, status in conn.execute("SELECT vehicle_id, status FROM run_models ORDER BY run_id"):
        if OUTCOME_RANK.get(status, 0) >= OUTCOME_RANK.get(best.get(vid, ""), -1):
            best[vid] = status
    return best


def coverage_buckets(conn: sqlite3.Connection, catalog_db: Path) -> dict[str, set[str]]:
    def norm(value: str | None) -> str:
        return (value or "").strip().upper()

    with closing(sqlite3.connect(f"file:{catalog_db}?mode=ro", uri=True)) as cat:
        codes = {norm(c) for (c,) in cat.execute("SELECT DISTINCT model_code FROM catalog_rows") if norm(c)}
    pool = {norm(c) for (c,) in conn.execute("SELECT announcement_model_code FROM vehicles") if norm(c)}
    documents = list(conn.execute(
        "SELECT a.model_code,d.is_pdf,d.bytes,d.error FROM announcements a "
        "LEFT JOIN documents d ON d.announcement_id=a.id"))
    pdf = {norm(c) for c, is_pdf, _, _ in documents if is_pdf == 1}
    html = {norm(c) for c, is_pdf, size, error in documents
            if is_pdf == 0 and (size or 0) > 0 and not (error or "").startswith("下载失败")}
    seen = {norm(c) for c, _, _, _ in documents}
    outcomes = best_model_outcomes(conn)
    by_code = {norm(code): outcomes.get(vid, "") for vid, code in conn.execute(
        "SELECT id, announcement_model_code FROM vehicles")}
    no_match = {code for code, status in by_code.items() if status == "no_match"} - seen
    awaiting = {code for code, status in by_code.items() if status == "awaiting_effective"}
    # 有成功文档时优先采用成功结局；没有文档的失败/中断需进入补采清单。
    downloaded = codes & pdf
    no_pdf = (codes & html) - downloaded
    absent = (codes & no_match) - downloaded - no_pdf
    unseen = codes - pool - seen
    waiting = (codes & awaiting) - downloaded - no_pdf - absent - unseen
    retry = codes - downloaded - no_pdf - absent - unseen - waiting
    return {COVERAGE_DOWNLOADED: downloaded, COVERAGE_NO_PDF: no_pdf,
            COVERAGE_NO_MATCH: absent, COVERAGE_UNSEEN: unseen,
            COVERAGE_WAITING: waiting, COVERAGE_RETRY: retry}
