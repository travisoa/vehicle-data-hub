#!/usr/bin/env python3
"""统一入口：按车型需求下载汽车之家车型配置 Excel 和工信部公告参数页 PDF。

用法：
    python3 main.py fetch <车型A> <车型B>            # 两个来源都抓
    python3 main.py fetch <车型> --source gonggao    # 只下载工信部公告 PDF
    python3 main.py fetch <车型> --source autohome   # 只抓汽车之家配置 Excel
    python3 main.py autohome --models "<车型>"       # 汽车之家配置抓取完整 CLI
    python3 main.py gonggao query <车型> --download  # 工信部公告查询完整 CLI
    python3 main.py review <车型>                    # 公告 PDF -> 公告参数评审 Excel
    python3 main.py report <本品> --vs <竞品>      # 口碑/销量竞品对标 HTML 报告
    python3 main.py profiles                         # 列出统一车型档案

    # 减免购置税/车船税目录（市场名 -> 公告型号反查，目录数据入库可检索）
    python3 main.py gonggao jianmian sync            # 抓取目录附件解析入 SQLite
    python3 main.py gonggao jianmian search <市场名> --resolve   # 按通用名称反查公告型号/商标
    python3 main.py gonggao jianmian export          # 目录库导出 Excel
"""

from __future__ import annotations

import argparse
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
    parser.add_argument("--gonggao-output-dir", help="工信部公告下载根目录，默认 downloads/")
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
        print(
            f"[提示] {vehicle} 不在车型档案中；可按上方“建议 model_prefixes”"
            "把公告条件沉淀进 data/vehicle_profiles.json"
        )
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


def command_profiles(argv: list[str]) -> int:
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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print(USAGE)
        return 0
    command, rest = argv[0], argv[1:]
    if command == "fetch":
        return command_fetch(rest)
    if command == "gonggao":
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
