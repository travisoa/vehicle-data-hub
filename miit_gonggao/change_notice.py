#!/usr/bin/env python3
"""查询工信部变更扩展公示，并按公示型号下载最新有效公告参数页。"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import parse

from bs4 import BeautifulSoup

from miit_gonggao import core


MIIT_BASE_URL = "https://www.miit.gov.cn"
# 工信部当前公开、可稳定访问的变更扩展公示入口。后续批次可用 --notice-url 覆盖，
# 避免依赖搜索引擎或猜测 CMS 的随机文章路径。
DEFAULT_CHANGE_NOTICE_URL = (
    "https://www.miit.gov.cn/datainfo/cpgg/art/2026/"
    "art_87540c9a504a405b8a35fb96b503786b.html"
)


@dataclass
class ChangeNoticeSource:
    notice_url: str
    title: str
    published_at: str
    batch: str
    iframe_url: str
    unit_url: str
    unit_params: dict[str, str]


def _parse_jsonish(value: str) -> dict[str, Any]:
    """解析 CMS 输出的单引号字典属性，不执行其中的代码。"""
    parsed = ast.literal_eval(value)
    if not isinstance(parsed, dict):
        raise ValueError("CMS 参数不是对象")
    return parsed


def _fetch_text(url: str, *, referer: str | None = None) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 gonggao-tool/0.1",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    content, _response_headers = core.http_request(url, headers=headers)
    return content.decode("utf-8", errors="replace")


def parse_change_notice_article(article_html: str, article_url: str) -> tuple[str, str, str, str]:
    soup = BeautifulSoup(article_html, "html.parser")
    title_meta = soup.find("meta", attrs={"name": "ArticleTitle"})
    title = str(title_meta.get("content") or "").strip() if title_meta else ""
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    batch_match = re.search(r"第\s*(\d+)\s*批", title)
    if not batch_match:
        raise RuntimeError("未能从公示文章标题识别公告批次")
    published_meta = soup.find("meta", attrs={"name": "PubDate"})
    published_at = str(published_meta.get("content") or "").strip() if published_meta else ""
    iframe = soup.find("iframe", src=True)
    if not iframe:
        raise RuntimeError("公示文章中未找到车型清单 iframe")
    iframe_url = parse.urljoin(article_url, str(iframe["src"]))
    return title, batch_match.group(1), published_at, iframe_url


def parse_change_notice_unit(
    iframe_html: str,
    *,
    notice_url: str,
    title: str,
    published_at: str,
    batch: str,
    iframe_url: str,
) -> ChangeNoticeSource:
    soup = BeautifulSoup(iframe_html, "html.parser")
    script = soup.find("script", attrs={"querydata": True, "url": True})
    if not script:
        raise RuntimeError("公示清单中未找到 CMS 查询单元")
    unit_params = {
        str(key): str(value)
        for key, value in _parse_jsonish(str(script["querydata"])).items()
    }
    return ChangeNoticeSource(
        notice_url=notice_url,
        title=title,
        published_at=published_at,
        batch=batch,
        iframe_url=iframe_url,
        unit_url=parse.urljoin(iframe_url, str(script["url"])),
        unit_params=unit_params,
    )


def load_change_notice_source(notice_url: str = DEFAULT_CHANGE_NOTICE_URL) -> ChangeNoticeSource:
    article_html = _fetch_text(notice_url)
    title, batch, published_at, iframe_url = parse_change_notice_article(
        article_html, notice_url
    )
    iframe_html = _fetch_text(iframe_url, referer=notice_url)
    return parse_change_notice_unit(
        iframe_html,
        notice_url=notice_url,
        title=title,
        published_at=published_at,
        batch=batch,
        iframe_url=iframe_url,
    )


NOTICE_COLUMNS = (
    "notice_title",
    "notice_batch",
    "company",
    "trademark",
    "product_name",
    "model_code",
)


def parse_change_notice_rows(html: str, base_url: str) -> tuple[list[dict[str, str]], int, int]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one(".page-content table")
    if table is None:
        raise RuntimeError("公示查询响应缺少结果表格")

    rows: list[dict[str, str]] = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all("td", recursive=False)
        if len(cells) < len(NOTICE_COLUMNS):
            continue
        values: list[str] = []
        detail_url = ""
        for cell in cells[: len(NOTICE_COLUMNS)]:
            div = cell.find("div")
            value = str(div.get("title") or "").strip() if div else ""
            if not value:
                value = cell.get_text(" ", strip=True)
            values.append(value)
            if not detail_url:
                link = cell.find("a", href=True)
                if link:
                    detail_url = parse.urljoin(base_url, str(link["href"]))
        row = dict(zip(NOTICE_COLUMNS, values, strict=True))
        row["detail_url"] = detail_url
        rows.append(row)

    total = len(rows)
    page_size = max(len(rows), 1)
    pagination = soup.find(id=re.compile(r"_pagination$"))
    if pagination and pagination.get("querydata"):
        page_data = _parse_jsonish(str(pagination["querydata"]))
        total = int(page_data.get("count") or total)
        page_size = int(page_data.get("rows") or page_size)
    return rows, total, page_size


def _fetch_unit_html(
    source: ChangeNoticeSource,
    *,
    search: dict[str, str],
    page_num: int,
    page_size: int,
) -> str:
    params = dict(source.unit_params)
    params["paramJson"] = json.dumps(
        {
            "pageNo": page_num,
            "pageSize": page_size,
            "loadEnabled": True,
            "search": json.dumps(search, ensure_ascii=False),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    url = f"{source.unit_url}?{parse.urlencode(params)}"
    payload = json.loads(_fetch_text(url, referer=source.iframe_url))
    if not payload.get("success"):
        raise RuntimeError(f"公示查询失败: {payload.get('message') or payload}")
    html = (payload.get("data") or {}).get("html")
    if not html:
        raise RuntimeError("公示查询返回空页面")
    return str(html)


def query_change_notice(
    source: ChangeNoticeSource,
    *,
    company: str = "",
    trademark: str = "",
    product_name: str = "",
    model_code: str = "",
    page_size: int = 100,
    limit: int | None = None,
) -> tuple[list[dict[str, str]], int]:
    search = {
        "title": "",
        "PICI": source.batch,
        "CPMC": product_name,
        "QYMC": company,
        "CPXH": model_code,
        "CPSB": trademark,
    }
    first_html = _fetch_unit_html(
        source, search=search, page_num=1, page_size=page_size
    )
    rows, total, actual_page_size = parse_change_notice_rows(first_html, source.iframe_url)
    if limit is not None and len(rows) >= limit:
        return rows[:limit], total

    page_count = math.ceil(total / max(actual_page_size, 1))
    for page_num in range(2, page_count + 1):
        page_html = _fetch_unit_html(
            source, search=search, page_num=page_num, page_size=page_size
        )
        page_rows, _page_total, _page_size = parse_change_notice_rows(
            page_html, source.iframe_url
        )
        rows.extend(page_rows)
        if limit is not None and len(rows) >= limit:
            return rows[:limit], total
    return rows, total


def latest_effective_rows(model_code: str) -> list[dict[str, Any]]:
    rows = core.query_all_pages(model_code=model_code, page_size=50)
    exact = [
        row
        for row in rows
        if str(row.get("clxh") or "").strip().upper() == model_code.strip().upper()
    ]
    return core.filter_latest_batch(exact)


def _write_snapshot(
    source: ChangeNoticeSource,
    *,
    query: dict[str, str],
    total: int,
    rows: list[dict[str, str]],
    output_dir: Path,
) -> Path:
    snapshot_dir = output_dir / core.ANNOUNCEMENT_SNAPSHOT_DIRNAME
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / f"change_notice_{time.strftime('%Y%m%d_%H%M%S')}.json"
    payload = {
        "source": {
            "notice_url": source.notice_url,
            "title": source.title,
            "published_at": source.published_at,
            "batch": source.batch,
            "iframe_url": source.iframe_url,
        },
        "query": query,
        "total": total,
        "returned": len(rows),
        "rows": rows,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _print_rows(rows: list[dict[str, str]]) -> None:
    if not rows:
        print("未查询到变更扩展公示产品。")
        return
    print("序号\t企业名称\t产品商标\t产品名称\t产品型号\t公示批次")
    for index, row in enumerate(rows, start=1):
        print(
            "\t".join(
                [
                    str(index),
                    row["company"],
                    row["trademark"],
                    row["product_name"],
                    row["model_code"],
                    row["notice_batch"],
                ]
            )
        )


def _download_latest_pdfs(
    rows: list[dict[str, str]],
    *,
    output_dir: Path,
    flat_output: bool,
) -> int:
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    non_pdf: list[str] = []
    seen_models: set[str] = set()

    for notice_row in rows:
        model_code = notice_row["model_code"].strip()
        model_key = model_code.upper()
        if not model_code or model_key in seen_models:
            continue
        seen_models.add(model_key)
        try:
            products = latest_effective_rows(model_code)
        except Exception as exc:  # noqa: BLE001 - 单型号失败不能中断整批
            errors.append(model_code)
            print(f"最新公告查询失败，跳过: {model_code} ({exc})", file=sys.stderr)
            continue
        if not products:
            errors.append(model_code)
            print(f"公告接口未找到精确型号，跳过: {model_code}", file=sys.stderr)
            continue

        for product in products:
            batch = str(product.get("gppc") or product.get("pc") or "")
            trademark = str(product.get("cpsb") or notice_row["trademark"])
            download_dir = output_dir if flat_output else core.build_announcement_download_dir(
                output_dir,
                trademark=trademark,
                vehicle_folder=model_code,
                batch=batch,
            )
            label = f"{model_code}（公示第{notice_row['notice_batch']}批 -> 最新公告第{batch}批）"
            try:
                path, is_pdf, byte_count = core.download_param_page(product, download_dir)
            except Exception as exc:  # noqa: BLE001 - 单条失败不能中断整批
                errors.append(label)
                print(f"PDF 下载失败，跳过: {label} ({exc})", file=sys.stderr)
                continue
            entries.append(
                {
                    "notice": notice_row,
                    "announcement": {
                        "qymc": product.get("qymc", ""),
                        "cpsb": product.get("cpsb", ""),
                        "clxh": product.get("clxh", ""),
                        "clmc": product.get("clmc", ""),
                        "gppc": batch,
                        "cpid": product.get("cpid") or product.get("gid") or "",
                        "dataTag": product.get("dataTag", ""),
                    },
                    "folder": os.fspath(download_dir),
                    "file": os.fspath(path),
                    "bytes": byte_count,
                    "ok_pdf": is_pdf,
                }
            )
            print(f"已下载: {label}\n  {path}")
            if not is_pdf:
                non_pdf.append(label)
                print(f"返回内容不是 PDF，已存为 HTML: {path}", file=sys.stderr)

    snapshot_dir = output_dir / core.ANNOUNCEMENT_SNAPSHOT_DIRNAME
    manifest_path = core.write_download_manifest(entries, snapshot_dir)
    print(f"下载索引: {manifest_path}")
    if errors:
        print(f"以下 {len(errors)} 条失败: {'; '.join(errors)}", file=sys.stderr)
    if non_pdf:
        print(f"以下 {len(non_pdf)} 条不是 PDF: {'; '.join(non_pdf)}", file=sys.stderr)
    ok_count = sum(1 for entry in entries if entry["ok_pdf"])
    return core.download_exit_code(
        ok_pdf_count=ok_count,
        problem_count=len(errors) + len(non_pdf),
    )


def command_changes(args: argparse.Namespace) -> int:
    filters = {
        "company": args.company or "",
        "trademark": args.trademark or "",
        "product_name": args.product_name or "",
        "model_code": args.model_code or "",
    }
    if not any(filters.values()) and not args.all:
        raise SystemExit(
            "请提供 --company / --trademark / --product-name / --model-code 之一；"
            "确需遍历整批时显式使用 --all"
        )

    source = load_change_notice_source(args.notice_url)
    rows, total = query_change_notice(
        source,
        **filters,
        page_size=args.page_size,
        limit=args.limit,
    )
    output_dir = Path(args.output_dir).expanduser().resolve()
    snapshot = _write_snapshot(
        source,
        query=filters,
        total=total,
        rows=rows,
        output_dir=output_dir,
    )

    print(f"公示来源: {source.title}")
    if source.published_at:
        print(f"发布日期: {source.published_at}")
    print(f"匹配结果: {total} 条；本次返回: {len(rows)} 条")
    _print_rows(rows)
    print(f"查询快照: {snapshot}")

    if not args.download:
        return 0
    if not rows:
        print("公示查询结果为空，没有可用于拉取最新 PDF 的产品型号。", file=sys.stderr)
        return core.EXIT_DOWNLOAD_FAILED
    return _download_latest_pdfs(
        rows,
        output_dir=output_dir,
        flat_output=args.flat_output,
    )


def register_subcommands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "changes",
        aliases=["change-notice"],
        help="查询变更扩展公示，并按公示型号下载最新有效公告参数页 PDF",
    )
    parser.add_argument("--company", help="公示企业名称")
    parser.add_argument("--trademark", help="公示产品商标")
    parser.add_argument("--product-name", help="公示产品名称")
    parser.add_argument("--model-code", help="公示产品型号")
    parser.add_argument("--all", action="store_true", help="允许遍历整批公示；无查询条件时必须显式指定")
    parser.add_argument("--download", action="store_true", help="按公示型号下载当前最高有效批次参数页 PDF")
    parser.add_argument("--limit", type=core.positive_int, help="限制返回及下载的公示条数")
    parser.add_argument("--page-size", type=core.positive_int, default=100, help="公示查询每页条数，默认 100")
    parser.add_argument(
        "--notice-url",
        default=DEFAULT_CHANGE_NOTICE_URL,
        help="工信部变更扩展公示文章 URL；新批次可显式覆盖",
    )
    parser.add_argument("--flat-output", action="store_true", help="PDF 直接保存到输出目录根目录")
    parser.add_argument(
        "--output-dir",
        default=os.fspath(core.DEFAULT_ANNOUNCEMENT_DIR),
        help=f"PDF 与快照输出根目录，默认 {core.DEFAULT_ANNOUNCEMENT_DIR}",
    )
    parser.set_defaults(func=command_changes)
