"""采集轮次：目录/型号名单、固定产品清单、近期事件三个入口共用的轮次生命周期。

入口只负责选出型号和产品；认领轮次、型号结局、单产品结局计数、失败降速和收尾统计都在这里。
记录写入权威业务库的 ingestion_runs 与 run_models，同一时间只允许一个未完成轮次。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

# 型号结局；判断某型号最终是否拿到时按 collection_coverage.OUTCOME_RANK 跨轮取最好结局。
MODEL_OUTCOMES = frozenset({"done", "partial", "no_match", "awaiting_effective", "query_failed", "interrupted"})
# 单产品结局：store_announcement 的结果，加上固定清单对已有文件的判定（已有有效 PDF、旧图片异常）
# 和存储异常。
PRODUCT_OUTCOMES = frozenset({"downloaded", "skipped_existing", "image_failed", "download_failed", "non_pdf",
                              "parse_failed", "publish_failed", "storage_failed"})
# ingestion_runs 的阶段统计列；单产品失败按结局计入对应列，存储异常由轮次中断原因记录。
COUNT_COLUMNS = ("query_failures", "download_failures", "non_pdf_documents", "awaiting_effective",
                 "parse_failures", "publish_failures", "image_failures")
FAILURE_COLUMNS = {"download_failed": "download_failures", "non_pdf": "non_pdf_documents",
                   "parse_failed": "parse_failures", "publish_failed": "publish_failures"}


class RunConflict(RuntimeError):
    """已有未完成轮次，或要恢复的轮次与本次选择不符。"""


class Pace(Protocol):
    def slow_down(self, status: str) -> None: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def unfinished_runs(conn: sqlite3.Connection) -> list[int]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='ingestion_runs'").fetchone():
        return []
    return [row[0] for row in conn.execute("SELECT id FROM ingestion_runs WHERE completed_at IS NULL ORDER BY id")]


def database_path(conn: sqlite3.Connection) -> Path | None:
    """连接的主库文件；内存库没有持久文件，返回 None。"""
    return next((Path(row[2]) for row in conn.execute("PRAGMA database_list") if row[1] == "main" and row[2]), None)


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


class CollectionRun:
    """一轮采集：claim 认领、逐型号 begin_model/finish_model、逐产品 record，最后 finish。

    pace 只在本进程按 --min/--max-interval 提速时传入；live 的下载失败、图片异常及
    未核验的非 PDF 会让它恢复默认间隔。
    """

    def __init__(self, conn: sqlite3.Connection, *, catalog_db: Path, pdf_root: Path, reason: str,
                 db_path: Path | None = None, pace: Pace | None = None) -> None:
        self.conn = conn
        self.catalog_db = catalog_db
        self.pdf_root = pdf_root
        self.reason = reason
        self.db_path = db_path if db_path is not None else database_path(conn)
        self.pace = pace
        self.run_id: int | None = None
        self.counts = dict.fromkeys(COUNT_COLUMNS, 0)

    def ensure_idle(self) -> None:
        """在任何写入或网络请求之前确认没有未完成轮次。"""
        if unfinished_runs(self.conn):
            raise RunConflict("存在未完成采集，不能另起一轮")

    def claim(self, selector: dict[str, Any], selected_models: int, *, resume_run: int | None = None,
              resume_matches: Callable[[dict[str, Any]], bool] | None = None,
              mismatch_message: str = "恢复轮次不存在或与本次选择不匹配") -> int:
        """原子认领轮次。恢复时沿用原轮次（resume_matches 核对其原选择），把上次进程未完成的型号记为中断。"""
        conn = self.conn
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            unfinished = unfinished_runs(conn)
            if any(run_id != resume_run for run_id in unfinished):
                raise RunConflict(f"存在其他未结束采集轮次，拒绝启动：{unfinished}")
            if resume_run is None:
                cursor = conn.execute(
                    "INSERT INTO ingestion_runs(started_at,selector_json,selected_models,parse_failures,"
                    "publish_failures) VALUES (?,?,?,0,0)",
                    (utc_now(), json.dumps(selector, ensure_ascii=False, sort_keys=True), selected_models),
                )
                self.run_id = int(cursor.lastrowid)
            else:
                row = conn.execute("SELECT selector_json FROM ingestion_runs WHERE id=?", (resume_run,)).fetchone()
                if not row or resume_matches is None or not resume_matches(json.loads(row[0])):
                    raise RunConflict(mismatch_message)
                conn.execute("UPDATE ingestion_runs SET completed_at=NULL WHERE id=?", (resume_run,))
                conn.execute("UPDATE run_models SET status='interrupted', error='上次进程未完成该型号' "
                             "WHERE run_id=? AND status='querying'", (resume_run,))
                self.run_id = resume_run
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return self.run_id

    def begin_model(self, order: int, vehicle_id: int) -> None:
        self.conn.execute(
            "INSERT INTO run_models(run_id,sort_order,vehicle_id,status,error) VALUES (?,?,?,'querying','') "
            "ON CONFLICT(run_id,vehicle_id) DO UPDATE SET status='querying',error='',sort_order=excluded.sort_order",
            (self.run_id, order, vehicle_id),
        )
        self.conn.commit()

    def finish_model(self, vehicle_id: int, outcome: str, error: str = "") -> None:
        if outcome not in MODEL_OUTCOMES:
            raise ValueError(f"未知型号结局：{outcome}")
        self.conn.execute("UPDATE run_models SET status=?,error=? WHERE run_id=? AND vehicle_id=?",
                          (outcome, error, self.run_id, vehicle_id))
        self.conn.commit()

    def count(self, column: str, amount: int = 1) -> None:
        if column not in self.counts:
            raise ValueError(f"未知轮次统计列：{column}")
        self.counts[column] += amount

    def query_failed(self, vehicle_id: int, error: str) -> None:
        """型号查询失败：计数、恢复默认间隔并记下结局。"""
        self.count("query_failures")
        self._slow_down("query_failed")
        self.finish_model(vehicle_id, "query_failed", error)

    def record_product(self, outcome: str, *, image_failed: bool = False, live: bool = True,
                       message: str = "") -> bool:
        """计入一个产品的结局，返回它是否为已核验的源站参数页生成异常。

        live 表示本次实际请求过源站；只有 live 的失败会触发降速，沿用已有文件的判定只计数。
        """
        if outcome not in PRODUCT_OUTCOMES:
            raise ValueError(f"未知单产品结局：{outcome}")
        if outcome in FAILURE_COLUMNS:
            self.count(FAILURE_COLUMNS[outcome])
        image_failed = image_failed or outcome == "image_failed"
        if image_failed:
            self.count("image_failures")
        source_error = outcome == "non_pdf" and is_source_generation_error(message, self.pdf_root)
        if live:
            if image_failed:
                self._slow_down("image_failed")
            if outcome == "download_failed" or (outcome == "non_pdf" and not source_error):
                self._slow_down(outcome)
        return source_error

    def record(self, outcome: Any) -> bool:
        """计入 store_announcement 返回的 StoreOutcome。"""
        return self.record_product(outcome.status, image_failed=outcome.image_failed, message=outcome.message)

    def finish(self, reason: str = "") -> int:
        """写入阶段统计与中断结局，再更新一次收录统计；返回因中断而未完成的型号数。

        完成时间不代表所有产品成功；收录统计更新失败不掩盖采集结果或原始异常。
        """
        try:
            interrupted = self.conn.execute(
                "UPDATE run_models SET status='interrupted',error=COALESCE(NULLIF(error,''),?) "
                "WHERE run_id=? AND status='querying'",
                (reason or "本轮采集中断，未完成该型号", self.run_id),
            ).rowcount
            self.conn.execute(
                "UPDATE ingestion_runs SET completed_at=?, " + ",".join(f"{key}=?" for key in COUNT_COLUMNS)
                + " WHERE id=?", [utc_now(), *(self.counts[key] for key in COUNT_COLUMNS), self.run_id])
            self.conn.commit()
        finally:
            self.refresh_status()
        return interrupted

    def refresh_status(self, reason: str | None = None) -> None:
        """批次收尾更新一次收录统计；不放进逐产品事务。"""
        try:
            from . import collection
        except ImportError:  # 直接以脚本路径导入时
            from miit_gonggao import collection
        collection.refresh_collection_status(self.db_path, catalog_db=self.catalog_db, pdf_root=self.pdf_root,
                                             run_id=self.run_id, reason=reason or self.reason)

    def _slow_down(self, status: str) -> None:
        if self.pace is not None:
            self.pace.slow_down(status)
