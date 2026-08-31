#!/usr/bin/env python3
"""Query and download MIIT-EIDC vehicle announcement parameter pages."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import re
import string
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request


BASE_URL = "https://service.miit-eidc.org.cn/miitxxgk/gonggao/xxgk"
QUERY_URL = f"{BASE_URL}/doCpQuery"
PDF_URL = f"{BASE_URL}/queryCpParamPage"
DETAIL_URL = f"{BASE_URL}/queryCpData"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAPPING_PATH = PROJECT_ROOT / "data" / "vehicle_profiles.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "downloads"
DEFAULT_ANNOUNCEMENT_DIR = DEFAULT_OUTPUT_DIR / "announcement_site"
ANNOUNCEMENT_SNAPSHOT_DIRNAME = "_snapshots"


@dataclass
class QueryProfile:
    name: str
    aliases: list[str]
    trademark: str
    filters: dict[str, str]
    model_prefixes: list[str]
    exclude_model_prefixes: list[str]
    autohome_models: list[str]


def normalize_key(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value).casefold()


def load_mappings(path: Path = DEFAULT_MAPPING_PATH) -> list[QueryProfile]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    profiles: list[QueryProfile] = []
    for item in raw:
        # 统一车型档案：公告查询条件放在 gonggao 子对象里；也兼容旧版平铺格式
        gonggao = item.get("gonggao") or item
        autohome = item.get("autohome") or {}
        profiles.append(
            QueryProfile(
                name=item["name"],
                aliases=item.get("aliases", []),
                trademark=gonggao.get("trademark", ""),
                filters=gonggao.get("filters", {}),
                model_prefixes=gonggao.get("model_prefixes", []),
                exclude_model_prefixes=gonggao.get("exclude_model_prefixes", []),
                autohome_models=autohome.get("models", []),
            )
        )
    return profiles


def find_mapping(vehicle: str | None, mappings: list[QueryProfile]) -> QueryProfile | None:
    if not vehicle:
        return None
    needle = normalize_key(vehicle)
    for item in mappings:
        keys = [item.name, *item.aliases]
        if any(normalize_key(key) == needle for key in keys):
            return item
    # 子串模糊匹配按键长度取最长，避免短名（D9/M9）绑到先出现的无关档案
    fuzzy: list[tuple[int, QueryProfile]] = []
    for item in mappings:
        keys = [item.name, *item.aliases]
        overlap = [
            len(normalized)
            for key in keys
            if (normalized := normalize_key(key)) and (needle in normalized or normalized in needle)
        ]
        if overlap:
            fuzzy.append((max(overlap), item))
    if not fuzzy:
        return None
    fuzzy.sort(key=lambda item: -item[0])
    return fuzzy[0][1]


def parse_key_value_pairs(values: list[str] | None, option_name: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise SystemExit(f"{option_name} 需要 FIELD=VALUE 格式，例如 {option_name} clmc=多用途乘用车")
        key, item_value = value.split("=", 1)
        key = key.strip()
        item_value = item_value.strip()
        if not key or not item_value:
            raise SystemExit(f"{option_name} 需要 FIELD=VALUE 格式，例如 {option_name} clmc=多用途乘用车")
        parsed[key] = item_value
    return parsed


def old_frontend_value(value: str | None) -> str:
    """Match the legacy page behavior: encodeURI(value), then form encode."""
    if not value:
        return ""
    return parse.quote(value, safe="~()*!.'")


# 串行节流 + 重试退避：分页、PDF 下载等全部 EIDC 请求共用一个入口
REQUEST_RETRIES = 3
REQUEST_BACKOFF = 2.0
REQUEST_MIN_INTERVAL = (0.8, 1.8)

_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    wait = _last_request_at + random.uniform(*REQUEST_MIN_INTERVAL) - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def http_request(
    url: str,
    *,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> tuple[bytes, dict[str, str]]:
    """节流 + 重试的 HTTP 请求；网络错误/超时/5xx/响应截断退避重试，4xx 直接抛出。"""
    body = parse.urlencode(data).encode("utf-8") if data is not None else None
    last_error: Exception | None = None
    for attempt in range(REQUEST_RETRIES):
        if attempt:
            time.sleep(REQUEST_BACKOFF**attempt)
        _throttle()
        req = request.Request(
            url,
            data=body,
            headers=headers or {},
            method="POST" if body is not None else "GET",
        )
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.read(), resp_headers
        except error.HTTPError as exc:
            if exc.code < 500:
                raise
            last_error = exc
        except (error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        except http.client.HTTPException as exc:
            # EIDC 偶发 chunked 响应提前结束（IncompleteRead），不属于 OSError，需单独重试
            last_error = exc
    raise RuntimeError(f"请求失败（已重试 {REQUEST_RETRIES} 次）: {url}") from last_error


def post_form(url: str, data: dict[str, str], timeout: int = 60) -> tuple[bytes, dict[str, str]]:
    return http_request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "Mozilla/5.0 gonggao-tool/0.1",
            "Referer": "https://service.miit-eidc.org.cn/miitxxgk/gonggao_xxgk/index_ggcp.html",
        },
        timeout=timeout,
    )


def query_products(
    *,
    trademark: str = "",
    company: str = "",
    model_code: str = "",
    vehicle_name: str = "",
    pc: str = "",
    page_num: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    payload = {
        "qymc": old_frontend_value(company),
        "pc": pc,
        "cpsb": old_frontend_value(trademark),
        "clxh": model_code or "",
        "clmc": old_frontend_value(vehicle_name),
        "scdz": "",
        "cplb": "0",
        "cxtype": "",
        "pageSize": str(page_size),
        "pageNum": str(page_num),
    }
    content, _headers = post_form(QUERY_URL, payload)
    return json.loads(content.decode("utf-8"))


def query_all_pages(**kwargs: Any) -> list[dict[str, Any]]:
    first = query_products(page_num=1, **kwargs)
    check_response(first)
    count = first.get("countResult") or {}
    total_pages = int(count.get("totalPage") or 0)
    rows = list(first.get("cpList") or [])
    for page_num in range(2, total_pages + 1):
        page = query_products(page_num=page_num, **kwargs)
        check_response(page)
        rows.extend(page.get("cpList") or [])
    return rows


def check_response(data: dict[str, Any]) -> None:
    result = data.get("handleResult") or {}
    if result.get("respCode") != 200:
        raise RuntimeError(f"查询失败: {result.get('digest') or result}")


def random_validate_token(length: int = 65) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def safe_part(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\s]+", "_", str(value).strip())
    value = value.strip("._")
    if not value or value in {".", ".."}:
        return "unknown"
    return value


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是 >= 1 的整数")
    return parsed


def is_pdf_bytes(content: bytes) -> bool:
    """只认 %PDF 魔数；Content-Type 不能当依据（验证页也可能标 application/pdf）。"""
    return content.startswith(b"%PDF")


# 下载类命令的退出码：区分「一份 PDF 都没拿到」和「拿到了但有条目需人工检查」，
# 让 main.py fetch 不把常见的部分非 PDF 当成整车型失败。
EXIT_OK = 0
EXIT_DOWNLOAD_FAILED = 1
EXIT_DOWNLOAD_PARTIAL = 2


def download_exit_code(*, ok_pdf_count: int, problem_count: int) -> int:
    """0=全部成功；1=全失败（没有任何 PDF）；2=部分成功（有 PDF，但有失败或非 PDF）。"""
    if problem_count <= 0:
        return EXIT_OK
    if ok_pdf_count <= 0:
        return EXIT_DOWNLOAD_FAILED
    return EXIT_DOWNLOAD_PARTIAL


def save_workbook_atomic(workbook: Any, out_path: Path) -> None:
    """先写临时文件，再原子替换到目标路径（进程中途被杀不会留下半截 Excel）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=out_path.parent,
        prefix=f".{out_path.stem}.",
        suffix=out_path.suffix,
        delete=False,
    ) as tmp_file:
        tmp_path = Path(tmp_file.name)

    try:
        workbook.save(tmp_path)
        tmp_path.replace(out_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def build_download_folder_name(
    *,
    args: argparse.Namespace,
    mapping: QueryProfile | None,
    trademark: str,
    company: str,
    model_code: str,
    vehicle_name: str,
) -> str:
    if args.vehicle_folder:
        return safe_part(args.vehicle_folder)
    if mapping:
        return safe_part(mapping.name)
    if args.vehicle:
        return safe_part(args.vehicle)

    parts = [trademark]
    prefixes = args.model_prefix or []
    if model_code:
        parts.append(model_code)
    elif prefixes:
        parts.append("+".join(prefixes))
    if vehicle_name:
        parts.append(vehicle_name)
    elif company:
        parts.append(company)
    return safe_part("_".join(part for part in parts if part))


def build_announcement_download_dir(
    output_root: Path,
    *,
    trademark: str,
    vehicle_folder: str,
    batch: str,
) -> Path:
    """公告 PDF 的统一目录：品牌 / 车型（查询名）/ 批次。"""
    return (
        output_root
        / safe_part(trademark or "未标注商标")
        / safe_part(vehicle_folder or "未标注车型")
        / f"第{safe_part(batch or '未标注')}批"
    )


def model_matches_prefixes(
    model_code: str,
    *,
    include_prefixes: list[str] | None = None,
    exclude_prefixes: list[str] | None = None,
) -> bool:
    model_code = (model_code or "").upper()
    include_prefixes = [item.upper() for item in include_prefixes or [] if item]
    exclude_prefixes = [item.upper() for item in exclude_prefixes or [] if item]
    if include_prefixes and not any(model_code.startswith(prefix) for prefix in include_prefixes):
        return False
    if exclude_prefixes and any(model_code.startswith(prefix) for prefix in exclude_prefixes):
        return False
    return True


def apply_row_filters(
    rows: list[dict[str, Any]],
    *,
    mapping: QueryProfile | None = None,
    include_prefixes: list[str] | None = None,
    exclude_prefixes: list[str] | None = None,
    row_filters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    combined_include = [
        *(mapping.model_prefixes if mapping else []),
        *(include_prefixes or []),
    ]
    combined_exclude = [
        *(mapping.exclude_model_prefixes if mapping else []),
        *(exclude_prefixes or []),
    ]
    row_filters = row_filters or {}
    if not combined_include and not combined_exclude and not row_filters:
        return rows
    return [
        row
        for row in rows
        if model_matches_prefixes(
            row.get("clxh", ""),
            include_prefixes=combined_include,
            exclude_prefixes=combined_exclude,
        )
        and all(str(expected) in str(row.get(field, "")) for field, expected in row_filters.items())
    ]


def filter_latest_batch(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def batch_of(row: dict[str, Any]) -> int | None:
        raw = str(row.get("gppc") or row.get("pc") or "")
        # 统一按整数比较：接口若返回零填充批次("0404")，字符串比较会把它误判成非最新
        return int(raw) if raw.isdigit() else None

    batches = [value for value in (batch_of(row) for row in rows) if value is not None]
    if not batches:
        return rows
    latest = max(batches)
    return [row for row in rows if batch_of(row) == latest]


def download_param_page(row: dict[str, Any], output_dir: Path) -> tuple[Path, bool, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "NECaptchaValidate": random_validate_token(),
        "dataTag": row.get("dataTag", ""),
        "gid": row.get("cpid") or row.get("gid", ""),
        "pc": str(row.get("gppc") or row.get("pc") or ""),
    }
    content, _headers = post_form(PDF_URL, payload)
    stem = "_".join(
        safe_part(str(part))
        for part in [
            row.get("cpsb", ""),
            row.get("clxh", ""),
            row.get("gppc") or row.get("pc", ""),
            row.get("cpid") or row.get("gid", ""),
        ]
        if part
    )
    is_pdf = is_pdf_bytes(content)
    suffix = ".pdf" if is_pdf else ".html"
    path = output_dir / f"{stem}{suffix}"
    path.write_bytes(content)
    return path, is_pdf, len(content)


def download_detail_html(row: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "NECaptchaValidate": random_validate_token(),
        "dataTag": row.get("dataTag", ""),
        "gid": row.get("cpid") or row.get("gid", ""),
        "pc": str(row.get("gppc") or row.get("pc") or ""),
    }
    content, _headers = post_form(DETAIL_URL, payload)
    stem = "_".join(
        safe_part(str(part))
        for part in [row.get("cpsb", ""), row.get("clxh", ""), row.get("gppc") or row.get("pc", "")]
        if part
    )
    path = output_dir / f"{stem}_detail.html"
    path.write_bytes(content)
    return path


def print_rows(rows: list[dict[str, Any]], limit: int | None = None) -> None:
    shown = rows[:limit] if limit is not None else rows
    if not shown:
        print("未查询到公告产品。")
        return
    print("序号\t企业名称\t产品商标\t车辆型号\t车辆名称\t批次\t产品ID")
    for index, row in enumerate(shown, start=1):
        print(
            "\t".join(
                [
                    str(index),
                    row.get("qymc", ""),
                    row.get("cpsb", ""),
                    row.get("clxh", ""),
                    row.get("clmc", ""),
                    str(row.get("gppc") or row.get("pc") or ""),
                    row.get("cpid") or row.get("gid") or "",
                ]
            )
        )
    if limit and len(rows) > limit:
        print(f"... 还有 {len(rows) - limit} 条未展示")


def write_query_snapshot(
    rows: list[dict[str, Any]],
    *,
    output_dir: Path,
    query: dict[str, str],
    mapping: QueryProfile | None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"query_{time.strftime('%Y%m%d_%H%M%S')}.json"
    payload = {
        "source": QUERY_URL,
        "query": query,
        "mapping": None
        if mapping is None
        else {
            "name": mapping.name,
            "trademark": mapping.trademark,
            "filters": mapping.filters,
            "model_prefixes": mapping.model_prefixes,
            "exclude_model_prefixes": mapping.exclude_model_prefixes,
        },
        "total": len(rows),
        "rows": rows,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_download_manifest(entries: list[dict[str, Any]], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"manifest_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def command_query(args: argparse.Namespace) -> int:
    mappings = load_mappings(Path(args.mapping_file).expanduser().resolve())
    mapping = find_mapping(args.vehicle, mappings)
    trademark = args.trademark or (mapping.trademark if mapping else "")

    filters = dict(mapping.filters if mapping else {})
    company = args.company or filters.get("qymc", "")
    model_code = args.model_code or filters.get("clxh", "")
    profile_prefixes = [
        *(mapping.model_prefixes if mapping else []),
        *(args.model_prefix or []),
    ]
    if not trademark and not model_code and not company and not profile_prefixes:
        raise SystemExit(
            "未能确定查询条件；请提供 --trademark / --model-code / --company / --model-prefix 之一，"
            "或先用 jianmian search 从减免税目录反查车辆型号"
        )
    vehicle_name = args.vehicle_name or filters.get("clmc", "")
    page_size = args.page_size

    query = {
        "vehicle": args.vehicle or "",
        "trademark": trademark,
        "company": company,
        "model_code": model_code,
        "vehicle_name": vehicle_name,
        "pc": args.pc or "",
    }
    row_filters = parse_key_value_pairs(args.row_filter, "--row-filter")
    if args.all_pages or args.download:
        rows = query_all_pages(
            trademark=trademark,
            company=company,
            model_code=model_code,
            vehicle_name=vehicle_name,
            pc=args.pc or "",
            page_size=page_size,
        )
    elif not model_code and profile_prefixes:
        # 大商标（数千条记录）单页结果经前缀过滤后常为空：
        # 改为逐前缀走服务端 clxh 模糊匹配，请求量小且结果完整
        rows = []
        seen: set[str] = set()
        for prefix in profile_prefixes:
            for row in query_all_pages(
                trademark=trademark,
                company=company,
                model_code=prefix,
                vehicle_name=vehicle_name,
                pc=args.pc or "",
                page_size=page_size,
            ):
                key = str(row.get("cpid") or row.get("gid") or id(row))
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
    else:
        data = query_products(
            trademark=trademark,
            company=company,
            model_code=model_code,
            vehicle_name=vehicle_name,
            pc=args.pc or "",
            page_size=page_size,
        )
        check_response(data)
        rows = list(data.get("cpList") or [])

    before_filter_count = len(rows)
    rows = apply_row_filters(
        rows,
        mapping=mapping,
        include_prefixes=args.model_prefix or [],
        exclude_prefixes=args.exclude_model_prefix or [],
        row_filters=row_filters,
    )
    if args.latest_batch:
        rows = filter_latest_batch(rows)

    output_dir = Path(args.output_dir).expanduser().resolve()
    snapshot_dir = output_dir / ANNOUNCEMENT_SNAPSHOT_DIRNAME
    snapshot = write_query_snapshot(rows, output_dir=snapshot_dir, query=query, mapping=mapping)

    print(f"产品商标: {trademark}")
    if mapping:
        print(f"查询配置: {mapping.name}")
    if before_filter_count != len(rows):
        print(f"结果筛选: {before_filter_count} -> {len(rows)}")
    if args.latest_batch and rows:
        print(f"最新批次: {rows[0].get('gppc') or rows[0].get('pc')}")
    print_rows(rows, limit=args.limit)
    print(f"查询结果: {snapshot}")

    if args.download:
        selected = rows[: args.limit] if args.limit is not None else rows
        if not selected:
            print("查询结果为空，没有可下载的公告。", file=sys.stderr)
            return 1
        manifest_entries: list[dict[str, Any]] = []
        vehicle_folder = build_download_folder_name(
            args=args,
            mapping=mapping,
            trademark=trademark,
            company=company,
            model_code=model_code,
            vehicle_name=vehicle_name,
        )
        print(f"下载根目录: {output_dir}")
        errors: list[str] = []
        non_pdf: list[str] = []
        for row in selected:
            label = f"{row.get('cpsb', '')} {row.get('clxh', '')}".strip()
            download_dir = output_dir if args.flat_output else build_announcement_download_dir(
                output_dir,
                trademark=str(row.get("cpsb") or trademark),
                vehicle_folder=vehicle_folder,
                batch=str(row.get("gppc") or row.get("pc") or ""),
            )
            try:
                path, is_pdf, byte_count = download_param_page(row, download_dir)
            except Exception as exc:  # 单条下载失败不中断整批
                errors.append(label)
                print(f"下载失败，跳过: {label} ({exc})", file=sys.stderr)
                continue
            manifest_entries.append(
                {
                    "qymc": row.get("qymc", ""),
                    "cpsb": row.get("cpsb", ""),
                    "clxh": row.get("clxh", ""),
                    "clmc": row.get("clmc", ""),
                    "gppc": row.get("gppc") or row.get("pc") or "",
                    "cpid": row.get("cpid") or row.get("gid") or "",
                    "dataTag": row.get("dataTag", ""),
                    "folder": os.fspath(download_dir),
                    "file": os.fspath(path),
                    "bytes": byte_count,
                    "ok_pdf": is_pdf,
                }
            )
            print(f"已下载: {path}")
            if not is_pdf:
                non_pdf.append(label)
                print(f"下载内容不是 PDF，已存为 HTML: {path}", file=sys.stderr)
            if args.detail_html:
                try:
                    detail_path = download_detail_html(row, download_dir)
                    print(f"已保存详情: {detail_path}")
                except Exception as exc:  # 详情失败不影响已下载的参数页
                    print(f"详情页保存失败，跳过: {label} ({exc})", file=sys.stderr)
        manifest_path = write_download_manifest(manifest_entries, snapshot_dir)
        print(f"下载索引: {manifest_path}")
        if errors:
            print(f"以下 {len(errors)} 条下载失败: {'; '.join(errors)}", file=sys.stderr)
        if non_pdf:
            print(
                f"以下 {len(non_pdf)} 条返回的不是 PDF，已存为 HTML 供人工检查: {'; '.join(non_pdf)}",
                file=sys.stderr,
            )
        ok_pdf_count = sum(1 for entry in manifest_entries if entry.get("ok_pdf"))
        code = download_exit_code(
            ok_pdf_count=ok_pdf_count, problem_count=len(errors) + len(non_pdf)
        )
        if code == EXIT_DOWNLOAD_FAILED:
            print("没有成功下载任何 PDF。", file=sys.stderr)
        elif code == EXIT_DOWNLOAD_PARTIAL:
            print(
                f"部分成功：已下载 {ok_pdf_count} 份 PDF，"
                f"另有 {len(errors) + len(non_pdf)} 条需人工检查。",
                file=sys.stderr,
            )
        return code
    return 0


def command_profiles(args: argparse.Namespace) -> int:
    mappings = load_mappings(Path(args.mapping_file).expanduser().resolve())
    if not mappings:
        print("暂无查询配置。")
        return 0
    for item in mappings:
        aliases = ", ".join(item.aliases)
        filters = ", ".join(f"{k}={v}" for k, v in item.filters.items())
        model_prefixes = ", ".join(item.model_prefixes)
        exclude_prefixes = ", ".join(item.exclude_model_prefixes)
        print(
            f"{item.name}\t{item.trademark}\taliases: {aliases}\tfilters: {filters}"
            f"\tmodel_prefixes: {model_prefixes}\texclude: {exclude_prefixes}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="查询并下载工信部装备工业发展中心公告参数页")
    subparsers = parser.add_subparsers(dest="command", required=True)

    query_parser = subparsers.add_parser("query", help="按本地配置或公告字段查询产品")
    query_parser.add_argument("vehicle", nargs="?", help="本地查询配置名，可为销售车型、项目代号或内部简称")
    query_parser.add_argument("--mapping-file", default=os.fspath(DEFAULT_MAPPING_PATH), help="本地查询配置 JSON 文件")
    query_parser.add_argument("--trademark", help="产品商标，例如 XX牌")
    query_parser.add_argument("--company", help="企业名称筛选")
    query_parser.add_argument("--model-code", help="车辆型号模糊筛选，例如 ABC6490")
    query_parser.add_argument("--vehicle-name", help="车辆名称筛选，例如 纯电动多用途乘用车")
    query_parser.add_argument("--pc", help="公告批次筛选")
    query_parser.add_argument("--model-prefix", action="append", help="车辆型号前缀筛选，可重复")
    query_parser.add_argument("--exclude-model-prefix", action="append", help="排除车辆型号前缀，可重复")
    query_parser.add_argument("--row-filter", action="append", help="查询后按任意字段包含筛选，格式 FIELD=VALUE，可重复")
    query_parser.add_argument("--latest-batch", action="store_true", help="只保留筛选结果里的最高公告批次")
    query_parser.add_argument("--page-size", type=int, default=50, help="每页条数，默认 50")
    query_parser.add_argument("--all-pages", action="store_true", help="拉取全部分页")
    query_parser.add_argument("--download", action="store_true", help="下载公告参数页 PDF")
    query_parser.add_argument("--detail-html", action="store_true", help="同时保存主要技术参数 HTML")
    query_parser.add_argument("--vehicle-folder", help="下载时使用的车型目录名；默认按查询配置或查询条件自动生成")
    query_parser.add_argument("--flat-output", action="store_true", help="下载文件直接保存到输出目录根目录，兼容旧版平铺结构")
    query_parser.add_argument("--limit", type=positive_int, help="限制展示或下载条数")
    query_parser.add_argument(
        "--output-dir",
        default=os.fspath(DEFAULT_ANNOUNCEMENT_DIR),
        help=f"公告下载根目录，默认 {DEFAULT_ANNOUNCEMENT_DIR}",
    )
    query_parser.set_defaults(func=command_query)

    profiles_parser = subparsers.add_parser("profiles", help="列出本地查询配置")
    profiles_parser.add_argument("--mapping-file", default=os.fspath(DEFAULT_MAPPING_PATH), help="本地查询配置 JSON 文件")
    profiles_parser.set_defaults(func=command_profiles)

    trademarks_parser = subparsers.add_parser("trademarks", help="兼容旧命令：列出本地查询配置")
    trademarks_parser.add_argument("--mapping-file", default=os.fspath(DEFAULT_MAPPING_PATH), help="本地查询配置 JSON 文件")
    trademarks_parser.set_defaults(func=command_profiles)

    from miit_gonggao import change_notice

    change_notice.register_subcommands(subparsers)

    from miit_gonggao import jianmian

    jianmian.register_subcommands(subparsers)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
