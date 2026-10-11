"""工信部公告接口的 port：采集入口经它查询正式公告产品、下载参数页及原图。

两个适配器：OfficialSource 走 core 的 HTTP 实现（含节流、重试与原图下载），供生产使用；
ScriptedSource 按给定函数返回结果，供测试与离线重放，入口不再依赖替换模块函数。
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

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
