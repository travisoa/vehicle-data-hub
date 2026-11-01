"""工信部公告接口的 port：采集入口经它查询正式公告产品、下载参数页及原图。

两个适配器：OfficialSource 走 core 的 HTTP 实现（含节流、重试与原图下载），供生产使用；
ScriptedSource 按给定函数返回结果，供测试与离线重放，入口不再依赖替换模块函数。
"""
from __future__ import annotations

import base64
import json
import os
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs

from miit_gonggao import core

PAGE_SIZE = 50


class AnnouncementSource(Protocol):
    def query(self, model_code: str, batch: int | str | None = None) -> list[dict[str, Any]]:
        """按精确型号查询正式公告产品行；给出 batch 时只查该正式批次。失败抛出异常。"""

    def download(self, row: dict[str, Any], output_dir: Path) -> core.ParameterPageDownload:
        """下载一行产品的参数页到 output_dir；按全局设置同时取原图，原图失败记在结果的 images 里。"""


def query_arguments(model_code: str, batch: int | str | None) -> dict[str, Any]:
    arguments: dict[str, Any] = {"model_code": model_code, "page_size": PAGE_SIZE}
    if batch is not None:
        arguments["pc"] = str(batch)
    return arguments


class OfficialSource:
    """工信部公告接口。每次调用时才取 core 的实现，节流与重试参数随 core 设置生效。"""

    def query(self, model_code: str, batch: int | str | None = None) -> list[dict[str, Any]]:
        return core.query_all_pages(**query_arguments(model_code, batch))

    def download(self, row: dict[str, Any], output_dir: Path) -> core.ParameterPageDownload:
        return core.download_param_page(row, output_dir)


class EvidenceOfficialSource(OfficialSource):
    """复用正式接口，将每页每次尝试及查询结局追加到同一证据日志。

    多线程共用同一个实例，锁保护追加；业务数据库仍由调用方单写者管理。
    正文以 base64 保存原始字节，响应头不保存以免暴露会话 Cookie。
    """

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self._lock = threading.Lock()

    def _append(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(line)
                log.flush()
                os.fsync(log.fileno())

    def query(self, model_code: str, batch: int | str | None = None) -> list[dict[str, Any]]:
        query_id = uuid.uuid4().hex
        now = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731
        self._append({"type": "query_started", "query_id": query_id, "model_code": model_code,
                      "batch": batch, "at": now()})
        attempts = 0
        pages: list[tuple[int, int, int]] = []

        def record(event: dict[str, Any]) -> None:
            nonlocal attempts
            event = dict(event)
            for field in ("request_body", "response_body"):
                value = event.pop(field)
                event[field + "_base64"] = base64.b64encode(value).decode("ascii") if value is not None else None
            self._append({"type": "http_attempt", "query_id": query_id, **event})
            attempts += 1
            if event["response_status"] == 200:
                try:
                    payload = json.loads(base64.b64decode(event["response_body_base64"], validate=True))
                    count = payload["countResult"]
                    page = int(count["pageNum"])
                    total = int(count["total"])
                    total_pages = int(count["totalPage"])
                    requested = int(parse_qs(base64.b64decode(event["request_body_base64"]).decode())
                                    ["pageNum"][0])
                    if (payload["handleResult"]["respCode"] != 200 or not isinstance(payload["cpList"], list)
                            or total < 0 or total_pages < 0 or page != requested):
                        raise ValueError("响应状态、列表或分页无效")
                except (TypeError, ValueError, KeyError, IndexError, UnicodeError) as exc:
                    raise RuntimeError(f"查询响应缺少可核验的分页证据：{exc}") from exc
                pages.append((page, total, total_pages))

        try:
            with core.observe_http_attempts(record):
                rows = super().query(model_code, batch)
            if not attempts:
                raise RuntimeError("查询未产生 HTTP 证据，拒绝记录成功")
            if not pages:
                raise RuntimeError("查询未产生成功分页，拒绝记录成功")
            total, total_pages = pages[0][1:]
            if (any((t, p) != (total, total_pages) for _, t, p in pages)
                    or [page for page, _, _ in pages] != list(range(1, max(1, total_pages) + 1))
                    or len(rows) != total or (total == 0) != (total_pages == 0)):
                raise RuntimeError("查询分页不完整或总数不一致，拒绝记录成功")
        except BaseException as exc:
            self._append({"type": "query_failed", "query_id": query_id, "at": now(),
                          "error": f"{type(exc).__name__}: {exc}"})
            raise
        self._append({"type": "query_succeeded", "query_id": query_id, "at": now(), "rows": len(rows)})
        return rows


class ScriptedSource:
    """按给定函数应答的公告接口；应答可随时替换，模拟同一轮里先成功、后失败。

    on_query 收到与 core.query_all_pages 相同的关键字参数，on_download 收到 (row, output_dir)。
    没有给应答却被调用时直接报错，不会退回真实接口。
    """

    def __init__(self, on_query: Callable[..., list[dict[str, Any]]] | None = None,
                 on_download: Callable[[dict[str, Any], Path], Any] | None = None) -> None:
        self.on_query = on_query
        self.on_download = on_download
        self.queries: list[dict[str, Any]] = []
        self.downloads: list[dict[str, Any]] = []

    def query(self, model_code: str, batch: int | str | None = None) -> list[dict[str, Any]]:
        arguments = query_arguments(model_code, batch)
        self.queries.append(arguments)
        if self.on_query is None:
            raise AssertionError(f"未提供查询应答：{arguments}")
        return self.on_query(**arguments)

    def download(self, row: dict[str, Any], output_dir: Path) -> Any:
        self.downloads.append(row)
        if self.on_download is None:
            raise AssertionError(f"未提供下载应答：{row.get('cpid')}")
        return self.on_download(row, output_dir)


OFFICIAL = OfficialSource()
