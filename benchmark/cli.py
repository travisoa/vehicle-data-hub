"""对标报告 CLI。

用法：
    python3 main.py report <本品> --vs <竞品1> <竞品2>           # 综合对标（默认全部板块）
    python3 main.py report <本品> --vs <竞品> --focus sales      # 仅销量对标
    python3 main.py report <本品> --vs <竞品> --focus koubei     # 仅口碑对标
    python3 main.py report <本品>                                   # 单车画像（无竞品）
    python3 main.py report <本品> --offline                        # 用 downloads/benchmark/ 缓存离线生成
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmark import analyzer, collector, report  # noqa: E402
from miit_gonggao.core import safe_part  # noqa: E402
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="main.py report",
        description="抓取汽车之家口碑/懂车帝销量，生成竞品对标分析 HTML 报告",
    )
    parser.add_argument("vehicle", help="本品车型名（如 <本品>）")
    parser.add_argument("--vs", nargs="*", default=[], help="竞品车型名，可多个")
    parser.add_argument(
        "--focus",
        choices=["all", "sales", "koubei"],
        default="all",
        help="报告聚焦板块：all=综合（默认）/ sales=仅销量对标 / koubei=仅口碑对标",
    )
    parser.add_argument("--pages", type=int, default=5, help="每个车系抓取的口碑页数（每页约10条），默认 5")
    parser.add_argument("--offline", action="store_true", help="不联网，使用 downloads/benchmark/ 缓存")
    parser.add_argument("-o", "--output", help="输出 HTML 路径，默认 output/对标报告_<本品>_<时间戳>.html")
    args = parser.parse_args(argv)

    sections = {"sales", "koubei"} if args.focus == "all" else {args.focus}
    need_sales = "sales" in sections

    dcd_sales = autohome_sales = None
    if not args.offline and need_sales:
        try:
            print("抓取懂车帝销量榜（最新月/近半年/近12个月）...")
            dcd_sales = collector.fetch_dcd_sales()
            labels = " / ".join(str(v.get("label", "")) for v in dcd_sales["dims"].values())
            print(f"懂车帝销量榜就绪，月份范围: {labels}")
        except Exception as exc:  # noqa: BLE001 - 销量是增值信息，失败不阻塞报告
            print(f"[警告] 懂车帝销量榜抓取失败，报告中对应列留空: {exc}")
        try:
            print("抓取汽车之家销量榜上下文 ...")
            autohome_sales = collector.fetch_autohome_sales_context()
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] 汽车之家销量榜抓取失败，报告中对应列留空: {exc}")

    names = [args.vehicle, *args.vs]
    profiles = []
    for name in names:
        try:
            data = collector.collect_vehicle(
                name,
                pages=args.pages,
                offline=args.offline,
                dcd_sales=dcd_sales,
                autohome_sales=autohome_sales,
            )
        except collector.CollectError as exc:
            if name == args.vehicle:
                raise SystemExit(f"本品 {name} 采集失败: {exc}") from None
            print(f"[警告] 竞品 {name} 采集失败，跳过: {exc}")
            continue
        profiles.append(analyzer.build_profile(data))

    if not profiles:
        raise SystemExit("没有成功采集到任何车型数据。")
    primary, competitors = profiles[0], profiles[1:]
    suggestions = analyzer.build_suggestions(primary, competitors) if "koubei" in sections else []
    overview = analyzer.build_overview(primary, competitors) if competitors else None

    if args.output:
        output_path = Path(args.output).expanduser()
    else:
        kind = {"sales": "销量对标", "koubei": "口碑对标"}.get(args.focus, "对标报告")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = (
            DEFAULT_OUTPUT_DIR / f"{kind}_{safe_part(args.vehicle)}_{timestamp}.html"
        )
    report.render_report(
        primary,
        competitors,
        output_path,
        suggestions=suggestions,
        sections=sections,
        overview=overview,
    )
    print(f"已生成{ {'sales': '销量对标', 'koubei': '口碑对标'}.get(args.focus, '综合对标') }报告: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
