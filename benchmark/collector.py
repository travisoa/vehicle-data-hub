"""对标数据采集：汽车之家口碑 + 懂车帝/汽车之家销量榜。

数据均来自公开页面接口，低频串行访问；原始响应缓存到 downloads/benchmark/，
`--offline` 时直接用缓存生成报告。

销量按三个维度采集（两个数据源各自原生支持，非本地累加）：
- 最新月份：懂车帝 month 缺省 / 汽车之家 date=YYYY-MM
- 近半年：懂车帝 month=500 / 汽车之家 date=YYYY-MM_YYYY-MM（6 个月区间）
- 近12个月：懂车帝 month=1000 / 汽车之家 date=YYYY-MM_YYYY-MM（12 个月区间）
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from autohome_cc.crawler.search import (
    SEARCH_API_URL,
    SEARCH_REFERER,
    build_search_params,
    parse_series_hits,
)
from miit_gonggao.core import safe_part

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = PROJECT_ROOT / "downloads" / "benchmark"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
KOUBEI_API = "https://koubeiipv6.app.autohome.com.cn/pc/series/list"
DCD_RANK_API = "https://www.dongchedi.com/motor/pc/car/rank_data"
AH_RANK_PAGE = "https://www.autohome.com.cn/rank/1"
REQUEST_INTERVAL = 1.0
RETRY_TIMES = 3
RETRY_BACKOFF = 1.5

# 销量维度：key -> (中文名, 懂车帝 month 参数, 覆盖月数)
SALES_DIMENSIONS: list[tuple[str, str, int | None, int]] = [
    ("latest", "最新月份", None, 1),
    ("half_year", "近半年", 500, 6),
    ("year", "近12个月", 1000, 12),
]


class CollectError(RuntimeError):
    pass


def _get(
    url: str,
    params: dict[str, Any] | None = None,
    referer: str | None = None,
    timeout: int = 20,
) -> requests.Response:
    """统一 GET 入口：429/5xx 与网络错误指数退避重试，其余 4xx 快速失败。

    与 miit_gonggao.core.http_request 同一套语义：目标站点偶发限流不该中断整轮采集。
    """
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    last_error = ""
    for attempt in range(RETRY_TIMES):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
            if response.status_code < 400:
                return response
            if response.status_code != 429 and response.status_code < 500:
                response.raise_for_status()
            last_error = f"HTTP {response.status_code}"
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = str(exc)
        if attempt < RETRY_TIMES - 1:
            time.sleep(RETRY_BACKOFF * (2**attempt))
    raise CollectError(f"请求失败（重试 {RETRY_TIMES} 次）: {url} {last_error}")


def _get_json(url: str, params: dict[str, Any], referer: str, timeout: int = 20) -> dict[str, Any]:
    response = _get(url, params, referer, timeout)
    try:
        return response.json()
    except ValueError as exc:
        raise CollectError(f"响应不是合法 JSON: {url} {exc}") from exc


def search_series(name: str) -> tuple[str, str]:
    """按车型名搜索汽车之家车系，返回 (series_id, 标准车系名)。

    解析与 autohome_cc 共用（见 autohome_cc.crawler.search.parse_series_hits），
    这里只取第一个车系条目：搜品牌名时接口返回品牌 ID，用它查口碑会得到空数据。
    """
    data = _get_json(SEARCH_API_URL, build_search_params(name), SEARCH_REFERER)
    hits = parse_series_hits(data)
    for hit in hits:
        if hit.is_series:
            return hit.series_id, hit.name or name
    if hits:
        raise CollectError(f"汽车之家搜到的是品牌而非车系: {name}（请改用具体车系名）")
    raise CollectError(f"汽车之家未找到车系: {name}")


def fetch_koubei(series_id: str, *, pages: int = 5, page_size: int = 20) -> dict[str, Any]:
    """抓取车系口碑：汇总信息 + 前 N 页评价列表（服务端实际每页约 10 条）。"""
    merged: dict[str, Any] = {}
    reviews: list[dict[str, Any]] = []
    for page in range(1, max(pages, 1) + 1):
        data = _get_json(
            KOUBEI_API,
            {"pm": 3, "seriesId": series_id, "pageIndex": page, "pageSize": page_size, "order": 0},
            "https://k.autohome.com.cn/",
        )
        if data.get("returncode") != 0:
            raise CollectError(f"口碑接口返回异常: {data.get('message')}")
        result = data.get("result") or {}
        if not merged:
            merged = {key: value for key, value in result.items() if key != "list"}
        reviews.extend(result.get("list") or [])
        if page >= int(result.get("pagecount") or 1):
            break
        time.sleep(REQUEST_INTERVAL)
    merged["reviews"] = reviews
    return merged


# ---------------------------------------------------------------------------
# 销量：懂车帝（全榜）+ 汽车之家（按级别榜），各 最新月/近半年/近12个月 三个维度
# ---------------------------------------------------------------------------


def _month_label(yyyymm: int) -> str:
    return f"{yyyymm // 100}年{yyyymm % 100:02d}月"


def _month_shift(yyyymm: int, delta: int) -> int:
    total = (yyyymm // 100) * 12 + (yyyymm % 100 - 1) + delta
    return (total // 12) * 100 + total % 12 + 1


def _range_label(latest: int, months: int) -> str:
    if months <= 1:
        return _month_label(latest)
    return f"{_month_label(_month_shift(latest, -(months - 1)))}~{_month_label(latest)}"


def _model_token(name: str) -> str:
    """取车系名末尾的型号标识（拉丁字母/数字段，如 009 / M9 / X9 / L90）。

    中文品牌前缀在中英文榜单里会变（中文名↔英文名），但型号标识通常一致，
    可用作跨语言匹配锚点。要求长度 ≥ 2 以避免单字符噪声。
    """
    match = re.search(r"[A-Za-z0-9]+$", str(name).replace(" ", ""))
    token = match.group(0) if match else ""
    return token.lower() if len(token) >= 2 else ""


def match_sales(sales_rows: list[dict[str, Any]], series_name: str) -> dict[str, Any] | None:
    """在销量榜中按车系名匹配（忽略空格大小写），并支持中英文型号标识的跨语言兜底。"""

    def norm(text: str) -> str:
        return "".join(str(text).split()).lower()

    target = norm(series_name)
    for row in sales_rows:
        if norm(row.get("series_name", "")) == target:
            return row
    for row in sales_rows:
        name = norm(row.get("series_name", ""))
        if name and (name in target or target in name):
            return row
    # 跨语言兜底：按型号标识匹配（中文品牌009 ↔ LATIN 009），仅当唯一命中才采用以避免误配
    token = _model_token(series_name)
    if token:
        hits = [row for row in sales_rows if token in norm(row.get("series_name", ""))]
        if len(hits) == 1:
            return hits[0]
    return None


def fetch_dcd_sales(max_offset: int = 400) -> dict[str, Any]:
    """懂车帝销量榜三维度：{dims: {key: {rows, label}}, latest_month: 202605}。"""
    latest_month: int | None = None
    dims: dict[str, Any] = {}
    for key, _dim_name, month_param, months in SALES_DIMENSIONS:
        rows: list[dict[str, Any]] = []
        offset = 0
        while offset < max_offset:
            params: dict[str, Any] = {
                "aid": 1839,
                "app_name": "auto_web_pc",
                "count": 100,
                "offset": offset,
                "rank_data_type": 11,
                "nation": 0,
            }
            if month_param is not None:
                params["month"] = month_param
            data = _get_json(DCD_RANK_API, params, "https://www.dongchedi.com/sales")
            payload = data.get("data") or {}
            if latest_month is None:
                options = payload.get("sells_rank_month") or []
                if options and isinstance(options[0].get("month"), int):
                    latest_month = int(options[0]["month"])
            page = payload.get("list") or []
            if not page:
                break
            rows.extend(page)
            if not payload.get("paging", {}).get("has_more"):
                break
            offset += 100
            time.sleep(REQUEST_INTERVAL)
        dims[key] = {"rows": rows}
    for key, _dim_name, _month_param, months in SALES_DIMENSIONS:
        dims[key]["label"] = _range_label(latest_month, months) if latest_month else ""
    return {"dims": dims, "latest_month": latest_month}


def fetch_autohome_sales_context() -> dict[str, Any]:
    """抓汽车之家销量榜页，取 Next.js buildId、级别映射、三维度 date 值与月份范围标签。"""
    response = _get(AH_RANK_PAGE)
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', response.text, re.S)
    if not match:
        raise CollectError("汽车之家销量榜页未找到 __NEXT_DATA__")
    next_data = json.loads(match.group(1))
    props = next_data["props"]["pageProps"]
    options = props.get("options") or {}

    # 保留全部级别（含「全部SUV/全部MPV」这类多值项，值形如「21,22,23,24」，接口按逗号列表接受）
    levels: dict[str, str] = {}
    for level in options.get("levelList") or []:
        for item in level.get("list") or [level]:
            name, value = str(item.get("name", "")), str(item.get("value", ""))
            if name and value:
                levels[name] = value

    date_options = {str(i.get("name", "")): str(i.get("value", "")) for i in (options.get("otherList") or [{}])[0].get("list", [])}
    latest_name, latest_value = next(
        ((n, v) for n, v in date_options.items() if re.fullmatch(r"\d{4}-\d{2}", v)), ("", "")
    )
    dims: dict[str, dict[str, str]] = {}
    latest_month = int(latest_value.replace("-", "")) if latest_value else None
    for key, _dim_name, _m, months in SALES_DIMENSIONS:
        if key == "latest":
            dims[key] = {"date": latest_value, "label": latest_name or latest_value}
        else:
            source_name = "近半年" if key == "half_year" else "近一年"
            date_value = date_options.get(source_name, "")
            dims[key] = {
                "date": date_value,
                "label": _range_label(latest_month, months) if latest_month else source_name,
            }
    return {"build_id": next_data.get("buildId"), "levels": levels, "dims": dims, "cache": {}}


def fetch_autohome_level_rank(
    context: dict[str, Any],
    level_value: str,
    date_value: str,
    *,
    brand_id: str | int | None = None,
) -> list[dict[str, Any]]:
    """按级别+日期段抓汽车之家级别榜。

    默认 SSR 数据路由只返回首页 20 条；当传入 brand_id 时，等价于页面里先选品牌检索，
    能命中级别 Top20 之外但品牌榜可见的车型。
    """
    brand_value = str(brand_id or "x")
    cache_key = f"{level_value}|{date_value}|{brand_value}"
    if cache_key in context["cache"]:
        return context["cache"][cache_key]
    url = (
        f"https://www.autohome.com.cn/_next/data/{context['build_id']}"
        f"/rank/1-1-{level_value}-0_9000-x-x-{brand_value}/{date_value}.json"
    )
    data = _get_json(url, {}, AH_RANK_PAGE)
    rows = (data.get("pageProps") or {}).get("listRes", {}).get("list") or []
    context["cache"][cache_key] = rows
    time.sleep(REQUEST_INTERVAL)
    return rows


def _resolve_level(levels: dict[str, str], level_name: str) -> tuple[str, str]:
    """把口碑级别名映射到汽车之家榜单级别，返回 (级别名, level 值)。

    口碑对 SUV 给细分级（如「大型SUV」，汽车之家有同名细分榜），对 MPV/轿车只给大类
    （如「MPV」，对应汽车之家的「全部MPV」多值榜）。策略：精确名优先，其次「全部X」。
    """
    if level_name in levels:
        return level_name, levels[level_name]
    broad = f"全部{level_name}"
    if broad in levels:
        return broad, levels[broad]
    for name, value in levels.items():  # 兜底：包含关系
        if level_name and level_name in name:
            return name, value
    return level_name, ""


def build_sales(
    series_name: str,
    level_name: str,
    *,
    brand_id: str | int | None = None,
    brand_name: str = "",
    dcd_sales: dict[str, Any] | None,
    autohome_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """汇总两个来源三维度的销量：{dongchedi: {dim: {count, rank, label}}, autohome: {...}}。"""
    result: dict[str, Any] = {}
    if dcd_sales:
        source: dict[str, Any] = {}
        for key, _dim_name, _m, _months in SALES_DIMENSIONS:
            dim = dcd_sales["dims"].get(key) or {}
            row = match_sales(dim.get("rows") or [], series_name)
            source[key] = {
                "count": row.get("count") if row else None,
                "rank": row.get("rank") if row else None,
                "label": dim.get("label", ""),
            }
        result["dongchedi"] = source
    if autohome_context:
        resolved_name, level_value = _resolve_level(autohome_context["levels"], level_name)
        source = {}
        for key, _dim_name, _m, _months in SALES_DIMENSIONS:
            dim = autohome_context["dims"].get(key) or {}
            entry: dict[str, Any] = {"count": None, "rank": None, "label": dim.get("label", "")}
            if level_value and dim.get("date"):
                try:
                    rows = fetch_autohome_level_rank(autohome_context, level_value, dim["date"])
                except Exception as exc:  # noqa: BLE001 - 单维度失败不影响其余
                    print(f"[警告] 汽车之家销量抓取失败({resolved_name} {dim.get('label')}): {exc}")
                    rows = []
                row = match_sales(
                    [{"series_name": r.get("seriesname", ""), **r} for r in rows], series_name
                )
                if not row and brand_id:
                    try:
                        rows = fetch_autohome_level_rank(
                            autohome_context,
                            level_value,
                            dim["date"],
                            brand_id=brand_id,
                        )
                    except Exception as exc:  # noqa: BLE001 - 品牌兜底失败不影响报告生成
                        print(
                            f"[警告] 汽车之家品牌销量抓取失败({brand_name or brand_id} "
                            f"{resolved_name} {dim.get('label')}): {exc}"
                        )
                        rows = []
                    row = match_sales(
                        [{"series_name": r.get("seriesname", ""), **r} for r in rows], series_name
                    )
                    if row:
                        entry["scope"] = "brand"
                        entry["brand"] = brand_name or str(brand_id)
                if row:
                    entry["count"] = row.get("salecount")
                    entry["rank"] = row.get("rankNum")
                    entry.setdefault("scope", "level")
            source[key] = entry
        source["level"] = resolved_name
        result["autohome"] = source
    return result or None


def collect_vehicle(
    name: str,
    *,
    pages: int = 3,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    offline: bool = False,
    dcd_sales: dict[str, Any] | None = None,
    autohome_sales: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """采集单个车型的口碑与销量，带 JSON 缓存。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    # 车型名来自命令行，可能含 / : 等分隔符，消毒后才能当文件名
    cache_path = cache_dir / f"{safe_part(name)}.json"
    if offline:
        if not cache_path.exists():
            raise CollectError(f"离线模式但没有缓存: {cache_path}（先在线跑一次）")
        return json.loads(cache_path.read_text(encoding="utf-8"))

    series_id, series_name = search_series(name)
    print(f"[{name}] 汽车之家车系: {series_name} (id={series_id})，抓取口碑 ...")
    koubei = fetch_koubei(series_id, pages=pages)
    sales = build_sales(
        series_name,
        str(koubei.get("levelname", "")),
        brand_id=koubei.get("brandId"),
        brand_name=str(koubei.get("brandName", "")),
        dcd_sales=dcd_sales,
        autohome_context=autohome_sales,
    )
    data = {
        "input_name": name,
        "series_id": series_id,
        "series_name": series_name,
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "koubei": koubei,
        "sales": sales,
    }
    cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[{name}] 口碑 {len(koubei.get('reviews') or [])} 条，缓存 -> {cache_path}")
    return data
