#!/usr/bin/env python3
"""统一入口：按车型需求下载汽车之家车型配置 Excel 和工信部公告参数页 PDF。

用法：
    python3 main.py fetch <车型A> <车型B>            # 两个来源都抓
    python3 main.py fetch <车型> --source gonggao    # 只下载工信部公告 PDF
    python3 main.py fetch <车型> --source autohome   # 只抓汽车之家配置 Excel
    python3 main.py autohome --models "<车型>"       # 汽车之家配置抓取完整 CLI
    python3 main.py gonggao collect -f <型号名单>    # 批量下载并登记统一业务库
    python3 main.py gonggao collect --manifest <清单> --help  # 固定产品清单模式
    python3 main.py gonggao status                  # 读取持久收录统计；--refresh 显式更新
    python3 main.py gonggao query <车型> --download  # 工信部公告查询完整 CLI
    python3 main.py gonggao collect --republished-from-status  # 正式发布重发 -> 刷新已有参数页
    python3 main.py gonggao changes --model-code <型号>  # 变更扩展公示只读查询（提前了解，不下载）
    python3 main.py review <车型>                    # 公告 PDF -> 公告参数评审 Excel
    python3 main.py report <本品> --vs <竞品>      # 口碑/销量竞品对标 HTML 报告
    python3 main.py profiles                         # 列出统一车型档案
    python3 main.py profiles add <市场名>            # 反查公告条件并写入档案（-f 名单文件可批量）

    # 减免购置税/车船税目录（市场名 -> 公告型号反查，目录数据入库可检索）
    python3 main.py gonggao jianmian sync            # 抓取目录附件解析入 SQLite
    python3 main.py gonggao jianmian search <市场名> --resolve   # 按通用名称反查公告型号/商标
    python3 main.py gonggao jianmian export          # 目录库导出 Excel
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from miit_gonggao import core as gonggao_core  # noqa: E402

USAGE = __doc__


def split_vehicle_args(values: list[str]) -> list[str]:
    vehicles: list[str] = []
    for value in values:
        for part in value.replace("，", ",").split(","):
            part = part.strip()
            if part and part not in vehicles:
                vehicles.append(part)
    return vehicles


def build_fetch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py fetch",
        description="按车型需求同时下载汽车之家车型配置 Excel 和工信部公告参数页 PDF",
    )
    parser.add_argument("vehicles", nargs="+", help="一个或多个车型名，可空格或逗号分隔")
    parser.add_argument(
        "--source",
        choices=["both", "autohome", "gonggao"],
        default="both",
        help="数据来源，默认 both",
    )
    parser.add_argument("--mapping-file", help="统一车型档案 JSON，默认 data/vehicle_profiles.json")
    # 工信部公告选项
    parser.add_argument("--all-batches", action="store_true", help="公告查询保留全部批次（默认只取最新批次）")
    parser.add_argument("--detail-html", action="store_true", help="公告下载时同时保存主要技术参数 HTML")
    parser.add_argument("--limit", type=gonggao_core.positive_int, help="公告下载条数限制")
    # 汽车之家选项
    parser.add_argument("--cookies", help="Autohome Cookie header 字符串")
    parser.add_argument("--cookie-file", help="Autohome Cookie 文件路径")
    parser.add_argument("--browser-login", action="store_true", help="抓取前先打开浏览器扫码登录 Autohome")
    parser.add_argument("--browser-fallback", action="store_true", help="requests 失败时启用浏览器兜底")
    parser.add_argument("--browser-profile-dir", help="浏览器登录态目录，默认 .browser/autohome")
    parser.add_argument("--browser-channel", help="Playwright channel，例如 chrome 或 msedge")
    parser.add_argument("--autohome-output-dir", help="汽车之家 Excel 输出目录，默认 output/")
    parser.add_argument("--gonggao-output-dir", help="工信部公告下载根目录，默认 downloads/announcement_site/")
    return parser


def run_gonggao_for_vehicle(vehicle: str, args: argparse.Namespace) -> int:
    """返回 gonggao 退出码语义：0 全部成功 / 1 失败 / 2 部分成功。"""
    argv = ["query", vehicle, "--download"]
    if not args.all_batches:
        argv.append("--latest-batch")
    if args.detail_html:
        argv.append("--detail-html")
    if args.limit is not None:
        argv.extend(["--limit", str(args.limit)])
    if args.mapping_file:
        argv.extend(["--mapping-file", args.mapping_file])
    if args.gonggao_output_dir:
        argv.extend(["--output-dir", args.gonggao_output_dir])
    print(f"\n=== [工信部公告] {vehicle} ===")
    try:
        return gonggao_core.main(argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            return gonggao_core.EXIT_OK
        print(f"[工信部公告] {vehicle} 失败: {exc}", file=sys.stderr)
        return gonggao_core.EXIT_DOWNLOAD_FAILED
    except Exception as exc:  # noqa: BLE001
        print(f"[工信部公告] {vehicle} 失败: {exc}", file=sys.stderr)
        return gonggao_core.EXIT_DOWNLOAD_FAILED


def run_jianmian_fallback(vehicle: str, args: argparse.Namespace) -> int:
    """档案没有公告查询条件时，按市场名从减免税目录反查公告型号并下载参数页。

    返回值与 run_gonggao_for_vehicle 一致：0 全部成功 / 1 失败 / 2 部分成功。
    """
    print(f"\n=== [工信部公告] {vehicle}（档案未命中，走减免税目录反查）===")
    argv = ["jianmian", "search", vehicle, "--download"]
    if not args.all_batches:
        argv.append("--latest-batch")
    if args.gonggao_output_dir:
        argv.extend(["--output-dir", args.gonggao_output_dir])
    try:
        code = gonggao_core.main(argv)
    except SystemExit as exc:
        print(f"[工信部公告] {vehicle} 反查失败: {exc}", file=sys.stderr)
        return gonggao_core.EXIT_DOWNLOAD_FAILED
    except Exception as exc:  # noqa: BLE001
        print(f"[工信部公告] {vehicle} 反查失败: {exc}", file=sys.stderr)
        return gonggao_core.EXIT_DOWNLOAD_FAILED
    if code in (gonggao_core.EXIT_OK, gonggao_core.EXIT_DOWNLOAD_PARTIAL):
        print(f"[提示] {vehicle} 不在车型档案中；沉淀条件直接跑：main.py profiles add {vehicle}")
    return code


def run_autohome(model_names: list[str], args: argparse.Namespace) -> bool:
    from autohome_cc import app as autohome_app

    argv = ["--models", ",".join(model_names)]
    if args.cookies:
        argv.extend(["--cookies", args.cookies])
    if args.cookie_file:
        argv.extend(["--cookie-file", args.cookie_file])
    if args.browser_login:
        argv.append("--browser-login")
    if args.browser_fallback:
        argv.append("--browser-fallback")
    if args.browser_profile_dir:
        argv.extend(["--browser-profile-dir", args.browser_profile_dir])
    if args.browser_channel:
        argv.extend(["--browser-channel", args.browser_channel])
    if args.autohome_output_dir:
        argv.extend(["--output-dir", args.autohome_output_dir])
    print(f"\n=== [汽车之家] {', '.join(model_names)} ===")
    try:
        return autohome_app.main(argv) == 0
    except SystemExit as exc:
        if exc.code in (0, None):
            return True
        print(f"[汽车之家] 失败: {exc}", file=sys.stderr)
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[汽车之家] 失败: {exc}", file=sys.stderr)
        return False


def command_fetch(argv: list[str]) -> int:
    args = build_fetch_parser().parse_args(argv)
    vehicles = split_vehicle_args(args.vehicles)
    if not vehicles:
        raise SystemExit("未提供有效车型名。")

    mapping_path = (
        Path(args.mapping_file).expanduser().resolve()
        if args.mapping_file
        else gonggao_core.DEFAULT_MAPPING_PATH
    )
    profiles = gonggao_core.load_mappings(mapping_path)

    failures: list[str] = []
    partials: list[str] = []

    if args.source in ("both", "autohome"):
        autohome_models: list[str] = []
        for vehicle in vehicles:
            profile = gonggao_core.find_mapping(vehicle, profiles)
            names = profile.autohome_models if profile and profile.autohome_models else [vehicle]
            for name in names:
                if name not in autohome_models:
                    autohome_models.append(name)
        if not run_autohome(autohome_models, args):
            failures.append("autohome")

    if args.source in ("both", "gonggao"):
        for vehicle in vehicles:
            profile = gonggao_core.find_mapping(vehicle, profiles)
            has_conditions = bool(
                profile
                and (
                    profile.trademark
                    or profile.filters.get("clxh")
                    or profile.filters.get("qymc")
                    or profile.model_prefixes
                )
            )
            code = (
                run_gonggao_for_vehicle(vehicle, args)
                if has_conditions
                else run_jianmian_fallback(vehicle, args)
            )
            if code == gonggao_core.EXIT_DOWNLOAD_PARTIAL:
                partials.append(f"gonggao:{vehicle}")
            elif code != gonggao_core.EXIT_OK:
                failures.append(f"gonggao:{vehicle}")

    print()
    if partials:
        print(f"以下任务部分成功（已拿到 PDF，另有条目需人工检查）: {', '.join(partials)}")
    if failures:
        print(f"完成，但以下任务失败: {', '.join(failures)}")
        return 1
    if partials:
        return 0
    print("全部任务完成。")
    return 0


def _warn(message: str) -> None:
    """输出到 stderr 前先冲掉 stdout：管道场景下 stdout 是块缓冲、stderr 无缓冲，
    不冲的话报错行会跑到对应的「=== 车型 ===」标题前面，看不出是哪个车型失败的。"""
    sys.stdout.flush()
    print(message, file=sys.stderr, flush=True)


def _collect_profile_names(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[str]:
    names = split_vehicle_args(args.names)
    if args.from_file:
        text = Path(args.from_file).expanduser().read_text(encoding="utf-8")
        names.extend(
            line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
        )
    if not names:
        parser.error("需要至少一个车型名，或用 -f 指定名单文件")
    if args.alias and len(names) > 1:
        parser.error("--alias 只能在单个车型名时使用")
    return names


def command_profiles_add(argv: list[str]) -> int:
    """按市场名从减免税目录反查公告条件，直接写入统一车型档案。

    此前 jianmian search 已经算出「建议 model_prefixes」和公告商标，但只打印到终端，
    需要人再手抄进 data/vehicle_profiles.json；这条命令把该 handoff 补上。
    """
    parser = argparse.ArgumentParser(
        prog="main.py profiles add",
        description="按市场名反查公告条件并写入统一车型档案",
    )
    parser.add_argument("names", nargs="*", help="车型市场名，可传多个（也支持逗号分隔）")
    parser.add_argument("-f", "--from-file", help="从文件按行读取车型名，# 开头的行视为注释")
    parser.add_argument("--alias", action="append", default=[], help="附加别名，仅单车型时可用，可重复")
    parser.add_argument("--mapping-file", help="统一车型档案 JSON，默认 data/vehicle_profiles.json")
    parser.add_argument("--db", help="减免税目录库路径，默认 data/jianmian_catalog.sqlite")
    parser.add_argument("--limit", type=int, default=200, help="目录检索返回条数上限，默认 200")
    parser.add_argument("--overwrite", action="store_true", help="已存在同名或同别名档案时覆盖")
    parser.add_argument("--dry-run", action="store_true", help="只打印将写入的条目，不落盘")
    args = parser.parse_args(argv)

    names = _collect_profile_names(args, parser)

    from miit_gonggao import jianmian

    mapping_path = (
        Path(args.mapping_file).expanduser().resolve()
        if args.mapping_file
        else gonggao_core.DEFAULT_MAPPING_PATH
    )
    db_path = Path(args.db).expanduser().resolve() if args.db else jianmian.DB_PATH
    if not db_path.exists():
        _warn(f"目录库不存在: {db_path}")
        _warn("先运行: main.py gonggao jianmian sync")
        return gonggao_core.EXIT_DOWNLOAD_FAILED

    entries = gonggao_core.load_raw_mappings(mapping_path)
    conn = jianmian.open_db(db_path)
    staged: list[dict[str, object]] = []
    counters = {"added": 0, "updated": 0, "skipped": 0}
    failed: list[str] = []

    for name in names:
        print(f"\n=== {name} ===")
        if not args.overwrite and gonggao_core.mapping_exists(entries, name, args.alias):
            counters["skipped"] += 1
            print("档案中已有同名或同别名条目，跳过（要覆盖加 --overwrite）")
            continue
        rows = jianmian.query_rows(conn, name, limit=args.limit)
        if not rows:
            _warn(f"目录库中未找到「{name}」，跳过；可先 jianmian sync，或换用目录里的通用名称")
            failed.append(name)
            continue
        model_codes = sorted({row["model_code"] for row in rows if row["model_code"]})
        prefixes = jianmian.suggest_prefixes(model_codes)
        print(f"目录命中 {len(rows)} 条 / 车辆型号 {len(model_codes)} 个")
        if args.limit and len(rows) == args.limit:
            _warn(f"命中条数已达上限 {args.limit}，model_prefixes 可能不全；"
                  f"用 --limit 调大后重跑（--overwrite 覆盖）")
        print(f"建议 model_prefixes: {', '.join(prefixes) or '(无)'}")

        # 反查要为每个车辆型号打一次公告接口，网络异常只跳过当前车型：
        # 与 fetch 一致，不能让一次失败作废整批已经查好的结果
        try:
            resolved = jianmian.resolve_models(model_codes)
        except Exception as exc:  # noqa: BLE001
            _warn(f"「{name}」公告反查失败，跳过: {type(exc).__name__}: {exc}")
            failed.append(name)
            continue
        if not resolved:
            _warn("公告接口未返回匹配产品，商标与车辆名称无法确定")

        entry, notes = gonggao_core.build_profile_entry(
            name,
            aliases=args.alias,
            model_prefixes=prefixes,
            resolved_rows=resolved,
        )
        entries, action = gonggao_core.upsert_mapping(entries, entry, overwrite=args.overwrite)
        if action == "skipped":
            counters["skipped"] += 1
            print("档案中已有同名或同别名条目，跳过（要覆盖加 --overwrite）")
            continue
        counters[action] += 1
        staged.append(entry)
        print(f"{'覆盖' if action == 'updated' else '新增'}档案条目: {entry['gonggao']['trademark'] or '(商标待补)'}")
        for note in notes:
            print(f"  [待确认] {note}")

    if not staged:
        print("\n没有条目需要写入。")
    elif args.dry_run:
        print("\n--dry-run，未写盘。将写入的条目：")
        print(json.dumps(staged, ensure_ascii=False, indent=2))
    else:
        gonggao_core.save_mappings(mapping_path, entries)
        print(f"\n已写入 {mapping_path}")

    print(
        f"新增 {counters['added']} / 覆盖 {counters['updated']} / "
        f"跳过 {counters['skipped']} / 失败 {len(failed)}"
    )
    if failed:
        _warn(f"失败车型: {', '.join(failed)}")
    if not failed:
        return gonggao_core.EXIT_OK
    # 与 fetch 一致：全失败为 1，部分成功为 2，便于脚本区分「名单全错」和「个别没查到」
    if not staged and counters["skipped"] == 0:
        return gonggao_core.EXIT_DOWNLOAD_FAILED
    return gonggao_core.EXIT_DOWNLOAD_PARTIAL


def command_profiles(argv: list[str]) -> int:
    if argv and argv[0] == "add":
        return command_profiles_add(argv[1:])
    parser = argparse.ArgumentParser(prog="main.py profiles", description="列出统一车型档案")
    parser.add_argument("--mapping-file", help="统一车型档案 JSON，默认 data/vehicle_profiles.json")
    args = parser.parse_args(argv)
    mapping_path = (
        Path(args.mapping_file).expanduser().resolve()
        if args.mapping_file
        else gonggao_core.DEFAULT_MAPPING_PATH
    )
    profiles = gonggao_core.load_mappings(mapping_path)
    if not profiles:
        print("暂无车型档案。")
        return 0
    for item in profiles:
        autohome = ", ".join(item.autohome_models) or "(直接用档案名搜索)"
        prefixes = ", ".join(item.model_prefixes)
        exclude = ", ".join(item.exclude_model_prefixes)
        filters = ", ".join(f"{k}={v}" for k, v in item.filters.items())
        print(
            f"{item.name}\tautohome: {autohome}\tgonggao: {item.trademark}"
            f"\tfilters: {filters}\tmodel_prefixes: {prefixes}\texclude: {exclude}"
        )
    return 0


def _sqlite_revision(path: Path) -> tuple:
    """只探测主文件/WAL 的变化，不读取或重算目录内容。"""
    parts = []
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            stat = candidate.stat()
        except OSError:
            parts.append(None)
        else:
            parts.append((stat.st_ino, stat.st_size, stat.st_mtime_ns))
    return tuple(parts)


def _sync_catalog(argv: list[str]) -> int:
    from miit_gonggao import collection, jianmian

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--db", type=Path, default=jianmian.DB_PATH)
    args, _ = parser.parse_known_args(argv[2:])
    catalog_db = args.db.expanduser().resolve()
    before = _sqlite_revision(catalog_db)
    try:
        return gonggao_core.main(argv)
    finally:
        if _sqlite_revision(catalog_db) != before:
            # 自定义目录库的统计跟随该库，不读取或写入默认活动库。
            site_db = catalog_db.parent / "announcement_site.sqlite"
            collection.refresh_collection_status(site_db, catalog_db=catalog_db,
                                                 pdf_root=site_db.parent.parent, reason="catalog_sync")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print(USAGE)
        return 0
    command, rest = argv[0], argv[1:]
    if command == "fetch":
        return command_fetch(rest)
    if command == "gonggao":
        if rest and rest[0] == "status":
            from miit_gonggao import collection_status

            return collection_status.main(rest[1:])
        if rest[:2] == ["jianmian", "sync"]:
            return _sync_catalog(rest)
        return gonggao_core.main(rest)
    if command == "review":
        from miit_gonggao import review_export

        return review_export.main(rest)
    if command == "report":
        from benchmark import cli as benchmark_cli

        return benchmark_cli.main(rest)
    if command == "autohome":
        from autohome_cc import app as autohome_app

        return autohome_app.main(rest)
    if command == "profiles":
        return command_profiles(rest)
    print(f"未知命令: {command}\n", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
