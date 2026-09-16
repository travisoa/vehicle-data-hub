#!/usr/bin/env python3
"""按公告批次整理「减免购置税新能源目录未覆盖」的车型目录。

公告参数查询接口拒绝只带批次的空条件查询（respCode=500，"请输入查询条件"），
因此按企业名称关键词并集枚举整批，再用型号两字母前缀抽样交叉校验完备性。
输出列与 `jianmian export --by-category` 完全对齐（前 19 列），便于两份表直接拼接；
公告接口没有的目录字段（能源类型、续驶里程等在参数页 PDF 里）留空而不是编造。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from miit_gonggao import core  # noqa: E402
from miit_gonggao.jianmian import EXPORT_HEADERS  # noqa: E402

# 购置税新能源目录的现名与 2021 年前的旧名，属于同一政策序列，默认一起作为「已覆盖」基准；
# 只想按网站现行采集口径（seed_announcement_site.py 的 CATALOG_NAME）对比时传单个 --catalog-name。
CATALOG_NAMES = (
    "减免车辆购置税的新能源汽车车型目录",
    "免征车辆购置税的新能源汽车车型目录",
)
CACHE_DIR = PROJECT_ROOT / "downloads" / "announcement_batches"
DEFAULT_OUT_DIR = PROJECT_ROOT / "output" / "gonggao_gap_by_category"
_CACHE_WRITES = 0

# 企业名称至少两个字才被接口接受（单字返回「请输入查询条件(至少有一项不少于两个字)」）。
# 依据实测的 3,138 家公告企业：绝大多数名字含「公司」，其余是「…厂」「…研究所」，
# 用下面的两字后缀覆盖。不要加「有限」「股份」「集团」这类大词——含它们的企业几乎必然
# 也含「公司」，只会让每批多翻上百页却零新增。
# 这份词表按已知样本推导，不保证对未来批次永久完备（实测就出现过新后缀「备厂」），
# 因此 fetch_batch 末尾有一道自愈补齐：见 absorb_unknown_companies。
COMPANY_KEYWORDS = (
    "公司",
    "车厂", "械厂", "造厂", "总厂", "工厂", "装厂", "辆厂", "箱厂", "备厂", "材厂",
    "究所", "摩托",
)
# 「材厂」是 2026-09-09 用公告正式附件的官方企业名单核对时补上的：湖北省消防器材厂
# 跨 9 个批次共 27 条产品，只有第390批被前缀校验碰巧撞到。这佐证了词表必须配合
# 附件核对与自愈补齐使用，不能指望一次推准。
# 独立于企业名的完备性证据：型号前缀反查，命中结果必须已在企业枚举并集内。
# 抽中的漏网条目直接补进结果，并触发自愈补齐，同时把数量写进缓存供事后审计。
VERIFY_PREFIXES = ("BJ", "ZZ", "CA", "SX", "EQ", "HF")

# 枚举口径的版本号。改动关键词表或枚举逻辑时必须递增：
# load_batch 会把版本不符的旧缓存视为失效并重新枚举，避免用不同口径的数据拼一份结果。
# v1: 初版词表 + 首页跳页优化——该优化会在某关键词首页恰好全部已收录时跳过其余页，
#     漏掉只有该关键词才能捞到的企业（第253批实测漏 120 条），已废弃。
# v2: 去掉跳页，词表按企业名后缀重推；但缺「备厂」，且没有自愈补齐。
CACHE_VERSION = 4
# v4: 仅复用完整且经过要求的校验的缓存；缺失批次每次重探。
CACHE_MAX_AGE_DAYS = 7

# 接口对数据表不存在的批次返回 respCode=500 加这个 digest。它有两种含义：
# 新批次尚未发布（如探测 pc=410），或历史批次在上游库里就没有表（实测第210批，
# 而209、211 都正常）。两种都不是本地抓取失败，必须与真正的空结果区分。
ABSENT_BATCH_DIGEST = "doesn't exist"

# AGENTS.md 要求工信部请求「低频、串行、温和」，core.http_request 默认节流 0.8~1.8 秒。
# 全量批次枚举是一次性的大规模只读作业（约 2.9 万次请求），项目所有者明确要求提速，
# 因此本脚本允许用 --min-interval/--max-interval 显式放宽——只覆盖本进程的 core 节流，
# 不改 core.py 的默认值，PDF 下载等其他功能不受影响。默认仍是保守值，提速必须显式传参。
# 仍然保持串行：同样的平均 QPS 下，串行比并发对服务端更平滑。
# 连续失败会自动退回保守节流，见 AdaptiveThrottle。
FAST_MIN_INTERVAL = 0.2
FAST_MAX_INTERVAL = 0.4
FAILURE_BACKOFF_THRESHOLD = 3   # 连续这么多次请求异常就降速

EXTRA_HEADERS = [("cpid", "公告产品ID"), ("is_chassis", "是否底盘"),
                 ("batch_count", "出现批次数"), ("first_batch", "最早批次")]

# 接口对更早的批次直接返回 respCode=500「不支持对小于173批以下的数据查询.」
MIN_QUERYABLE_BATCH = 173
# openpyxl 单表上限 1,048,576 行；跨批次全量按产品 ID 会超限，Excel 按型号去重，
# 逐产品明细另出 CSV。
XLSX_MAX_ROWS = 1_000_000


# 摩托车、挂车、三轮汽车、低速汽车是独立产品序列，型号不按 GB 9417 编制，
# 但形如 AMT1200DZK-35 同样能匹配「字母+类别码」，只看类别码会把电动正三轮摩托车
# 判成货车。必须先按产品名称排除；网站整车范围由 vehicle_classification 独立判定。
NON_AUTOMOTIVE = re.compile(r"摩托车|挂车|三轮汽车|低速汽车|低速货车")


def derive_category(model_code: str, product_name: str) -> str:
    """按产品名称与型号类别码生成批次导出的历史兼容分组。

    独立产品序列单独成组。本分组用于目录导出，不是网站汽车整车收录判断；
    收录范围由 miit_gonggao.vehicle_classification 统一判定。
    """
    name = product_name or ""
    if NON_AUTOMOTIVE.search(name):
        return "挂车" if "挂车" in name else "三轮汽车" if "三轮汽车" in name else (
            "低速汽车" if "低速" in name else "摩托车")
    match = re.match(r"^[A-Za-z]+(\d)", model_code or "")
    if not match:
        return "三轮汽车" if "三轮汽车" in name else "未分类"
    code = match.group(1)
    if code in {"2", "7"}:
        return "乘用车"
    if code == "6":
        return "客车" if "客车" in name else "乘用车"
    if code in {"1", "3", "4"}:
        return "货车"
    if code == "5":
        return "专用车"
    if code == "8":
        return "摩托车"
    if code == "9":
        return "挂车"
    return "未分类"


class AdaptiveThrottle:
    """按失败情况自适应调速：连续异常就退回保守节流，恢复后再提速。

    提速是所有者授权的一次性作业策略，不是可以无视对方服务状态的许可——
    接口一旦开始报错，继续用高频重试只会让情况更糟。
    """

    def __init__(self, fast: tuple[float, float] | None) -> None:
        self.fast = fast
        self.safe = core.REQUEST_MIN_INTERVAL
        self.failures = 0
        self.slowed = False
        self.apply()

    def apply(self) -> None:
        core.REQUEST_MIN_INTERVAL = self.safe if (self.slowed or not self.fast) else self.fast

    def record_success(self) -> None:
        if self.failures or self.slowed:
            self.failures = 0
            if self.slowed:
                self.slowed = False
                self.apply()
                print("  接口恢复正常，回到提速节流", flush=True)

    def record_failure(self) -> None:
        self.failures += 1
        if self.fast and not self.slowed and self.failures >= FAILURE_BACKOFF_THRESHOLD:
            self.slowed = True
            self.apply()
            print(f"  连续 {self.failures} 次请求异常，降回 {self.safe} 秒保守节流",
                  file=sys.stderr, flush=True)


THROTTLE = AdaptiveThrottle(None)


class BatchAbsent(RuntimeError):
    """该批次在工信部库里没有数据表，不是抓取失败。"""


def probe_batch(batch: str) -> None:
    """先探一页，把「批次表不存在」与真正的空结果区分开。"""
    head = core.query_products(company=COMPANY_KEYWORDS[0], pc=batch,
                               page_num=1, page_size=1)
    result = head.get("handleResult") or {}
    if result.get("respCode") == 200:
        return
    digest = str(result.get("digest") or "")
    if ABSENT_BATCH_DIGEST in digest:
        raise BatchAbsent(digest)
    raise RuntimeError(f"查询失败: {digest or result}")


def fetch_batch(batch: str, *, keywords=COMPANY_KEYWORDS, failures: list[str] | None = None) -> dict[str, dict]:
    """枚举一个公告批次的全部产品，按产品 ID 去重。"""
    failures = failures if failures is not None else []
    products: dict[str, dict] = {}

    def absorb(rows: list[dict]) -> int:
        added = 0
        for row in rows:
            cpid = str(row.get("cpid") or "").strip()
            if not cpid or cpid in products:
                continue
            products[cpid] = row
            added += 1
        return added

    for keyword in keywords:
        try:
            rows = core.query_all_pages(company=keyword, pc=batch, page_size=100)
        except Exception as exc:
            # 企业关键词是主枚举通道，失败意味着这一批缺数据。失败会记进 failures，
            # 由 load_batch 落成 .partial.json 并抛错，绝不能当成完整批次写正式缓存。
            THROTTLE.record_failure()
            failures.append(f"company:{keyword}: {exc}")
            print(f"  企业关键词「{keyword}」查询失败：{type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            continue
        THROTTLE.record_success()
        added = absorb(rows)
        print(f"  企业关键词「{keyword}」：返回 {len(rows)}，新增 {added}，累计 {len(products)}",
              flush=True)
    return products, absorb


def absorb_unknown_companies(batch: str, products: dict[str, dict],
                             absorb, *, keywords=COMPANY_KEYWORDS,
                             failures: list[str] | None = None) -> list[str]:
    """把关键词表没覆盖到的企业按精确企业名再查一遍。

    型号前缀校验只要捞到该企业的任意一条产品，这里就能把它在该批的产品补全。
    这样词表漏掉一个新后缀（如实测出现过的「备厂」）时不会整家企业丢失，
    并把新发现的企业名打印出来，便于后续把后缀补进 COMPANY_KEYWORDS。
    """
    failures = failures if failures is not None else []
    unknown = sorted({
        str(item.get("qymc") or "").strip()
        for item in products.values()
        if str(item.get("qymc") or "").strip()
        and not any(k in str(item.get("qymc") or "") for k in keywords)
    })
    for company in unknown:
        try:
            rows = core.query_all_pages(company=company, pc=batch, page_size=100)
        except Exception as exc:
            THROTTLE.record_failure()
            failures.append(f"company:{company}: {exc}")
            print(f"  补齐企业「{company}」失败：{type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            continue
        THROTTLE.record_success()
        added = absorb(rows)
        print(f"  词表未覆盖的企业「{company}」：返回 {len(rows)}，补齐 {added}", flush=True)
    return unknown


def verify_coverage(batch: str, products: dict[str, dict],
                    prefixes=VERIFY_PREFIXES, failures: list[str] | None = None) -> list[dict]:
    """用型号前缀反查，检验企业关键词并集是否漏采。"""
    failures = failures if failures is not None else []
    missed: list[dict] = []
    for prefix in prefixes:
        try:
            rows = core.query_all_pages(model_code=prefix, pc=batch, page_size=100)
        except Exception as exc:
            THROTTLE.record_failure()
            failures.append(f"prefix:{prefix}: {exc}")
            print(f"  校验前缀「{prefix}」查询失败：{type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            continue
        THROTTLE.record_success()
        gap = [row for row in rows if str(row.get("cpid") or "").strip() not in products]
        print(f"  校验前缀「{prefix}」：{len(rows)} 条，未被企业枚举覆盖 {len(gap)} 条", flush=True)
        missed.extend(gap)
    return missed


def cache_is_complete(payload: dict, *, verify: bool = True, fresh: bool = True) -> bool:
    """无下载源/部分失败/过期缓存不能证明本轮的批次内容。

    ``fresh=False`` 只放宽新鲜期，供把过期缓存当历史基线并单独标记的调用方使用；
    版本、完整性、交叉校验和"抓取时间不得在未来"一律不放宽。
    """
    if (payload.get("cache_version") != CACHE_VERSION or not payload.get("complete")
            or payload.get("absent_upstream")):
        return False
    if verify and set(payload.get("verify_prefixes", [])) != set(VERIFY_PREFIXES):
        return False
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(payload["fetched_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return False
    if age < 0:
        return False
    return not fresh or age <= CACHE_MAX_AGE_DAYS * 86400


def write_cache(path: Path, payload: dict) -> None:
    global _CACHE_WRITES
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    _CACHE_WRITES += 1


def load_batch(batch: str, *, refresh: bool = False, verify: bool = True) -> dict[str, dict]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"batch{batch}.json"
    if cache_path.exists() and not refresh:
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            payload = {}
        if cache_is_complete(payload, verify=verify):
            print(f"第{batch}批：命中完整缓存（{len(payload['products'])} 条）", flush=True)
            return {str(p["cpid"]): p for p in payload["products"]}

    print(f"第{batch}批：按企业名称关键词枚举…", flush=True)
    stamp = datetime.now(timezone.utc).isoformat()
    try:
        probe_batch(batch)
    except BatchAbsent as exc:
        # 保留既有成功缓存，缺失证据另存；下次必须重新探测。
        write_cache(cache_path.with_suffix(".absent.json"), {
            "batch": batch, "cache_version": CACHE_VERSION, "fetched_at": stamp,
            "source": core.QUERY_URL, "absent_upstream": True, "complete": False,
            "absent_digest": str(exc), "products": [],
        })
        return {}
    failures: list[str] = []
    products, absorb = fetch_batch(batch, failures=failures)
    missed = verify_coverage(batch, products, failures=failures) if verify else []
    absorb(missed)
    unknown = absorb_unknown_companies(batch, products, absorb, failures=failures)
    payload = {
        "batch": batch, "cache_version": CACHE_VERSION, "source": core.QUERY_URL,
        "fetched_at": stamp, "complete": not failures, "failures": failures,
        "company_keywords": list(COMPANY_KEYWORDS),
        "verify_prefixes": list(VERIFY_PREFIXES) if verify else [],
        "verify_missed": len(missed) if verify else None,
        "companies_outside_keywords": unknown,
        "products": [{**v, "cpid": k} for k, v in products.items()],
    }
    if failures:
        write_cache(cache_path.with_suffix(".partial.json"), payload)
        raise RuntimeError(f"第{batch}批枚举不完整，{len(failures)} 项失败；部分结果已保存，须重试")
    write_cache(cache_path, payload)
    print(f"第{batch}批：合计 {len(products)} 条，已缓存到 {cache_path}", flush=True)
    return products


def catalog_model_codes(catalog_db: Path, catalog_names: tuple[str, ...]) -> set[str]:
    placeholders = ",".join("?" * len(catalog_names))
    with closing(sqlite3.connect(f"file:{catalog_db}?mode=ro", uri=True)) as conn:
        return {
            str(code).strip().upper()
            for (code,) in conn.execute(
                f"SELECT DISTINCT model_code FROM catalog_rows WHERE catalog IN ({placeholders})",
                catalog_names,
            )
            if str(code or "").strip()
        }


def build_rows(products: dict[str, dict], covered: set[str]) -> list[dict[str, str]]:
    """逐产品 ID 的未覆盖明细；同型号跨批次会有多条。"""
    rows: list[dict[str, str]] = []
    for cpid, item in products.items():
        model_code = str(item.get("clxh") or "").strip()
        if not model_code or model_code.upper() in covered:
            continue
        product_name = str(item.get("clmc") or "").strip()
        rows.append({
            "catalog": "公告发布（未进购置税新能源目录）",
            "batch": str(item.get("gppc") or item.get("pc") or "").strip(),
            "part": "",
            "energy_type": "",
            "category": derive_category(model_code, product_name),
            "seq": "",
            "company": str(item.get("qymc") or "").strip(),
            "trademark": str(item.get("cpsb") or "").strip(),
            "model_code": model_code,
            "common_name": "",
            "product_name": product_name,
            "range_km": "", "fuel_consumption": "", "displacement_ml": "",
            "curb_mass": "", "battery_mass": "", "battery_energy": "",
            "remark": "", "art_id": "",
            "cpid": cpid,
            "is_chassis": "是" if str(item.get("dataTag") or "") == "D" else "",
            "batch_count": "", "first_batch": "",
        })
    return rows


def dedupe_by_model(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """一个车辆型号一行，取最新批次的那条，并记下它出现过的批次跨度。"""
    def batch_num(row: dict[str, str]) -> int:
        try:
            return int(row["batch"])
        except (TypeError, ValueError):
            return 0

    best: dict[str, dict[str, str]] = {}
    seen_batches: dict[str, set[int]] = {}
    for row in rows:
        key = row["model_code"].upper()
        seen_batches.setdefault(key, set()).add(batch_num(row))
        if key not in best or batch_num(row) > batch_num(best[key]):
            best[key] = dict(row)
    merged = []
    for key, row in best.items():
        batches = sorted(b for b in seen_batches[key] if b)
        row["batch_count"] = str(len(batches))
        row["first_batch"] = str(batches[0]) if batches else ""
        merged.append(row)
    merged.sort(key=lambda r: (r["category"], r["company"], r["model_code"]))
    for index, row in enumerate(merged, start=1):
        row["seq"] = str(index)
    return merged


def write_csv(rows: list[dict[str, str]], out_path: Path) -> None:
    import csv

    headers = [*EXPORT_HEADERS, *EXTRA_HEADERS]
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([label for _, label in headers])
        for row in rows:
            writer.writerow([row[key] for key, _ in headers])
    tmp_path.replace(out_path)


def write_xlsx(rows: list[dict[str, str]], out_path: Path, sheet_title: str) -> None:
    from openpyxl import Workbook

    headers = [*EXPORT_HEADERS, *EXTRA_HEADERS]
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_title[:31]
    sheet.append([label for _, label in headers])
    for row in rows:
        sheet.append([row[key] for key, _ in headers])
    sheet.freeze_panes = "A2"
    core.save_workbook_atomic(workbook, out_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True,
                        help="公告批次，逗号分隔或 400-409 区间")
    parser.add_argument("--catalog-db", type=Path,
                        default=PROJECT_ROOT / "data" / "jianmian_catalog.sqlite")
    parser.add_argument("--catalog-name", action="append", dest="catalog_names",
                        help="作为「已覆盖」基准的目录名，可重复；默认两个购置税新能源目录")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--refresh", action="store_true", help="忽略批次缓存重新枚举")
    parser.add_argument("--fetch-only", action="store_true",
                        help="只枚举并缓存批次，不生成 Excel/CSV")
    parser.add_argument("--fast", action="store_true",
                        help=f"提速枚举：节流放宽到 {FAST_MIN_INTERVAL}~{FAST_MAX_INTERVAL} 秒"
                             "（默认沿用 core 的 0.8~1.8 秒；连续失败会自动降回）")
    parser.add_argument("--min-interval", type=float, help="自定义最小请求间隔（秒）")
    parser.add_argument("--max-interval", type=float, help="自定义最大请求间隔（秒）")
    parser.add_argument("--verify-every", type=int, default=1,
                        help="每隔几个批次做一次型号前缀交叉校验；1 表示每批都做")
    args = parser.parse_args(argv)

    batches: list[int] = []
    for part in str(args.batch).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            batches.extend(range(int(start), int(end) + 1))
        else:
            batches.append(int(part))
    batches = sorted(set(batches))
    skipped = [b for b in batches if b < MIN_QUERYABLE_BATCH]
    batches = [b for b in batches if b >= MIN_QUERYABLE_BATCH]
    if skipped:
        print(f"跳过 {len(skipped)} 个批次（第{skipped[0]}-{skipped[-1]}批）："
              f"接口不支持查询第{MIN_QUERYABLE_BATCH}批以下的数据", flush=True)
    if not batches:
        parser.error("--batch 没有解析出可查询的批次")
    if not args.catalog_db.exists():
        parser.error(f"目录库不存在：{args.catalog_db}")

    global THROTTLE
    if args.min_interval or args.max_interval:
        low = args.min_interval or FAST_MIN_INTERVAL
        high = args.max_interval or max(FAST_MAX_INTERVAL, low)
        if low <= 0 or high < low:
            parser.error("--min-interval 必须大于 0 且不大于 --max-interval")
        THROTTLE = AdaptiveThrottle((low, high))
    elif args.fast:
        THROTTLE = AdaptiveThrottle((FAST_MIN_INTERVAL, FAST_MAX_INTERVAL))
    if THROTTLE.fast:
        print(f"提速模式：请求间隔 {THROTTLE.fast[0]}~{THROTTLE.fast[1]} 秒"
              f"（默认 {THROTTLE.safe[0]}~{THROTTLE.safe[1]}）", flush=True)

    catalog_names = tuple(args.catalog_names or CATALOG_NAMES)
    covered = catalog_model_codes(args.catalog_db, catalog_names)
    print(f"已覆盖基准目录：{'、'.join(catalog_names)}", flush=True)
    print(f"基准目录内唯一型号：{len(covered)} 个", flush=True)

    products: dict[str, dict] = {}
    failed: list[int] = []
    cache_writes_before = _CACHE_WRITES
    try:
        for index, batch in enumerate(batches, start=1):
            print(f"[{index}/{len(batches)}] 第{batch}批", flush=True)
            try:
                verify = args.verify_every <= 1 or (index - 1) % args.verify_every == 0
                products.update(load_batch(str(batch), refresh=args.refresh, verify=verify))
            except Exception as exc:  # 单批失败不该丢掉已抓到的其他批次
                failed.append(batch)
                print(f"  第{batch}批枚举失败：{type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
    finally:
        if _CACHE_WRITES != cache_writes_before:
            from miit_gonggao import collection

            catalog_db = args.catalog_db.expanduser().resolve()
            site_db = catalog_db.parent / "announcement_site.sqlite"
            collection.refresh_collection_status(site_db, catalog_db=catalog_db,
                                                 pdf_root=site_db.parent.parent, reason="batch_cache")
    print(f"公告产品合计：{len(products)} 条（{len(batches) - len(failed)}/{len(batches)} 个批次）",
          flush=True)
    if failed:
        print(f"失败批次 {len(failed)} 个：{failed}", file=sys.stderr, flush=True)
    if args.fetch_only:
        return 1 if failed else 0

    detail = build_rows(products, covered)
    rows = dedupe_by_model(detail)
    print(f"目录未覆盖：{len(detail)} 条产品记录，{len(rows)} 个唯一型号", flush=True)
    current_only = catalog_model_codes(args.catalog_db, (CATALOG_NAMES[0],))
    narrow = {str(p.get("clxh") or "").strip().upper() for p in products.values()} - current_only
    print(f"仅按「{CATALOG_NAMES[0]}」对比时未覆盖型号：{len(narrow)} 个", flush=True)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    label = str(batches[0]) if len(batches) == 1 else f"{batches[0]}-{batches[-1]}"
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault(row["category"], []).append(row)

    full_path = args.out_dir / f"全量_第{label}批.xlsx"
    write_xlsx(rows, full_path, "全量")
    print(f"已导出 {len(rows)} 行: {full_path}")
    for name, group_rows in sorted(groups.items(), key=lambda item: -len(item[1])):
        out_path = args.out_dir / f"{name}_第{label}批.xlsx"
        write_xlsx(group_rows, out_path, name)
        print(f"已导出 {len(group_rows)} 行: {out_path}")

    # 逐产品 ID 明细（同型号跨批次多条）不受 Excel 行数限制，另出 CSV
    detail.sort(key=lambda r: (int(r["batch"] or 0), r["category"], r["model_code"]))
    for index, row in enumerate(detail, start=1):
        row["seq"] = str(index)
    detail_path = args.out_dir / f"逐产品明细_第{label}批.csv"
    write_csv(detail, detail_path)
    print(f"已导出 {len(detail)} 行: {detail_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
