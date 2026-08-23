"""对标分析 HTML 报告渲染（自包含单文件，内联样式，无外部依赖）。"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any

_PALETTE = ["#2563eb", "#f59e0b", "#10b981", "#8b5cf6", "#ec4899", "#14b8a6"]

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"PingFang SC","Microsoft YaHei",sans-serif;background:#f1f5f9;color:#0f172a;line-height:1.65}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 60px}
header.hero{background:linear-gradient(135deg,#1e3a8a,#2563eb);color:#fff;border-radius:16px;padding:36px 32px;margin-bottom:28px}
header.hero h1{font-size:26px;margin-bottom:8px}
header.hero p{opacity:.85;font-size:14px}
h2{font-size:20px;margin:36px 0 14px;padding-left:12px;border-left:4px solid #2563eb}
h3{font-size:16px;margin:18px 0 10px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}
.card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 3px rgba(15,23,42,.08)}
.card .vname{font-size:17px;font-weight:700;margin-bottom:2px}
.card .tag{display:inline-block;font-size:12px;color:#2563eb;background:#dbeafe;border-radius:999px;padding:1px 10px;margin-bottom:12px}
.kv{display:grid;grid-template-columns:1fr 1fr;gap:6px 14px;font-size:13px}
.kv b{font-size:16px}
.score-big{font-size:30px;font-weight:800;color:#2563eb}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(15,23,42,.08);font-size:13px}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #e2e8f0}
th{background:#eff6ff;font-weight:600}
tr:last-child td{border-bottom:none}
.bar-row{display:flex;align-items:center;gap:8px;margin:3px 0}
.bar-label{width:110px;font-size:12px;color:#475569;text-align:right;flex-shrink:0}
.bar-track{flex:1;background:#e2e8f0;border-radius:5px;height:14px;overflow:hidden}
.bar-fill{height:100%;border-radius:5px}
.bar-value{width:44px;font-size:12px;font-weight:600}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0 16px;font-size:13px}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.pill-list{display:flex;flex-direction:column;gap:10px}
.pill{border-radius:10px;padding:12px 16px;font-size:13px;background:#fff;box-shadow:0 1px 3px rgba(15,23,42,.06)}
.pill.good{border-left:4px solid #16a34a}
.pill.bad{border-left:4px solid #dc2626}
.pill .cat{font-weight:700;margin-right:8px}
.pill .pct{color:#16a34a;font-weight:700;margin-right:6px}
.quote{background:#fff;border-radius:10px;padding:12px 16px;font-size:13px;margin-bottom:10px;box-shadow:0 1px 3px rgba(15,23,42,.06)}
.quote .meta{color:#64748b;font-size:12px;margin-top:6px}
.quote.good{border-left:4px solid #16a34a}
.quote.bad{border-left:4px solid #dc2626}
.tag-cloud{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.tag-chip{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:3px 12px;font-weight:600;line-height:1.4}
.tag-chip.good{background:#dcfce7;color:#166534}
.tag-chip.bad{background:#fee2e2;color:#991b1b}
.tag-chip .n{font-size:11px;font-weight:400;opacity:.75}
.suggest{background:#fff;border-radius:12px;padding:16px 20px;margin-bottom:12px;box-shadow:0 1px 3px rgba(15,23,42,.08);border-left:4px solid #f59e0b}
.suggest .topic{font-weight:700;font-size:15px}
.suggest .reason{color:#64748b;font-size:12px;margin:4px 0}
footer{margin-top:44px;color:#94a3b8;font-size:12px;text-align:center}
.section-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}
.overview{background:#fff;border-radius:12px;padding:20px 24px;box-shadow:0 1px 3px rgba(15,23,42,.08)}
.ov-item{font-size:14px;padding:8px 0;border-bottom:1px dashed #e2e8f0;line-height:1.6}
.ov-item:last-of-type{border-bottom:none}
.ov-icon{margin-right:8px}
.ov-topic{display:inline-block;min-width:64px;font-weight:700;color:#2563eb;margin-right:10px}
.ov-verdict{margin-top:14px;padding:12px 16px;background:#eff6ff;border-left:4px solid #2563eb;border-radius:8px;font-weight:600;font-size:14px}
"""


def _e(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def _fmt_score(profile: dict[str, Any]) -> str:
    """综合口碑评分；≤0（新车样本不足）显示为「-」而非误导性的 0.00。"""
    try:
        value = float(profile.get("score") or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        count = profile.get("review_count") or 0
        return f"-（样本 {count} 条）" if count else "-"
    return f"{value:.2f}"


def _fmt_sales(profile: dict[str, Any], *, include_month: bool = True) -> str:
    if profile.get("sales_count"):
        month = profile.get("sales_month_text")
        prefix = f"{month}：" if include_month and month else ""
        if include_month and not month:
            prefix = "月份未获取："
        return f"{prefix}{profile['sales_count']:,} 辆"
    return "未上榜/未获取"


def _sales_metric_label(profile: dict[str, Any]) -> str:
    month = profile.get("sales_month_text")
    return f"{month}销量" if month else "月销量（月份未获取）"


def _sales_table_header(profiles: list[dict[str, Any]]) -> str:
    months = {str(profile.get("sales_month_text") or "") for profile in profiles}
    months.discard("")
    if len(months) == 1:
        return f"{next(iter(months))}销量"
    if months:
        return "月销量（含月份）"
    return "月销量"


def _vehicle_card(profile: dict[str, Any], color: str, is_primary: bool) -> str:
    tag = "本品" if is_primary else "竞品"
    pph = f"{profile['pph']}" if profile.get("pph") else "-"
    return f"""
<div class="card" style="border-top:4px solid {color}">
  <div class="vname">{_e(profile["series_name"])}</div>
  <span class="tag">{tag} · {_e(profile.get("level_name") or "-")}</span>
  <div class="score-big">{_e(_fmt_score(profile))} <span style="font-size:13px;color:#64748b;font-weight:400">综合口碑评分</span></div>
  <div class="kv" style="margin-top:12px">
    <span>口碑评价数 <b>{profile.get("review_count", 0)}</b></span>
    <span>厂商指导价 <b>{_e(profile.get("price_range") or "-")}万</b></span>
    <span>{_e(_sales_metric_label(profile))} <b>{_e(_fmt_sales(profile, include_month=False))}</b></span>
    <span>新车百车故障数 PPH <b>{_e(pph)}</b></span>
  </div>
</div>"""


def _dim_bars(profiles: list[dict[str, Any]]) -> str:
    dims: list[str] = []
    for profile in profiles:
        for dim in profile.get("dim_scores", []):
            if dim["name"] not in dims:
                dims.append(dim["name"])
    if not dims:
        return "<p>暂无维度评分数据。</p>"
    legend = "".join(
        f'<span><i style="background:{_PALETTE[i % len(_PALETTE)]}"></i>{_e(p["series_name"])}</span>'
        for i, p in enumerate(profiles)
    )
    rows: list[str] = [f'<div class="legend">{legend}</div>']
    for dim_name in dims:
        rows.append(f'<h3 style="margin:14px 0 6px">{_e(dim_name)}</h3>')
        for index, profile in enumerate(profiles):
            score = next((d["score"] for d in profile.get("dim_scores", []) if d["name"] == dim_name), None)
            color = _PALETTE[index % len(_PALETTE)]
            if not score or score <= 0:  # 无该维度数据或样本不足（评分 0）
                rows.append(
                    f'<div class="bar-row"><span class="bar-label">{_e(profile["series_name"])}</span>'
                    f'<div class="bar-track"></div><span class="bar-value">-</span></div>'
                )
                continue
            width = max(min(score / 5 * 100, 100), 2)
            rows.append(
                f'<div class="bar-row"><span class="bar-label">{_e(profile["series_name"])}</span>'
                f'<div class="bar-track"><div class="bar-fill" style="width:{width:.0f}%;background:{color}"></div></div>'
                f'<span class="bar-value">{score:.2f}</span></div>'
            )
    return "".join(rows)


def _highlight_block(profile: dict[str, Any]) -> str:
    items = profile.get("highlights") or []
    if not items:
        return "<p>暂无结构化亮点数据。</p>"
    pills = "".join(
        f'<div class="pill good"><span class="cat">{_e(i["category"])}</span>'
        + (f'<span class="pct">{_e(i["percent"])}% 好评</span>' if i.get("percent") else "")
        + f'{_e(i["summary"])}</div>'
        for i in items[:8]
    )
    return f'<div class="pill-list">{pills}</div>'


def _complaint_block(profile: dict[str, Any]) -> str:
    parts: list[str] = []
    structured = profile.get("complaints") or []
    if structured:
        parts.append(
            '<div class="pill-list">'
            + "".join(
                f'<div class="pill bad"><span class="cat">{_e(i["category"])}</span>{_e(i["summary"])}</div>'
                for i in structured[:8]
            )
            + "</div>"
        )
    keyword_rows = profile.get("keyword_complaints") or []
    if keyword_rows:
        body = "".join(
            f"<tr><td>{_e(row['category'])}</td><td>{row['count']}</td>"
            f"<td>{_e(row['samples'][0][:90] if row.get('samples') else '')}</td></tr>"
            for row in keyword_rows[:8]
        )
        parts.append(
            '<h3>「不满意」评价关键词统计</h3><table><tr><th>槽点类别</th><th>提及条数</th><th>代表原声</th></tr>'
            + body
            + "</table>"
        )
    return "".join(parts) or "<p>暂无槽点数据（口碑样本较少）。</p>"


_SALES_DIMS = [("latest", "最新月份"), ("half_year", "近半年"), ("year", "近12个月")]


def _fmt_sales_cell(entry: dict[str, Any] | None) -> str:
    """只展示销量数值；排名对小众/高端车参考意义不大，不再呈现。"""
    if not entry or entry.get("count") in (None, "", 0):
        return "-"
    return f"{int(entry['count']):,} 辆"


def _sales_matrix_section(profiles: list[dict[str, Any]]) -> str:
    """销量对比：懂车帝（全榜）× 汽车之家（级别榜），各三个维度，表头标明具体月份范围。"""
    with_matrix = [p for p in profiles if p.get("sales_matrix")]
    if not with_matrix:
        return ""
    # 各维度月份范围标签取首个有数据的车型
    labels: dict[str, dict[str, str]] = {"dongchedi": {}, "autohome": {}}
    for profile in with_matrix:
        for source in ("dongchedi", "autohome"):
            for key, _n in _SALES_DIMS:
                entry = (profile["sales_matrix"].get(source) or {}).get(key) or {}
                if entry.get("label") and key not in labels[source]:
                    labels[source][key] = str(entry["label"])
    header_cells = "".join(
        f"<th>懂车帝·{name}<br><span style='font-weight:400;font-size:11px'>{_e(labels['dongchedi'].get(key, ''))}</span></th>"
        for key, name in _SALES_DIMS
    ) + "".join(
        f"<th>汽车之家·{name}<br><span style='font-weight:400;font-size:11px'>{_e(labels['autohome'].get(key, ''))}</span></th>"
        for key, name in _SALES_DIMS
    )
    body_rows = []
    for index, profile in enumerate(profiles):
        matrix = profile.get("sales_matrix") or {}
        cells = "".join(
            f"<td>{_fmt_sales_cell((matrix.get('dongchedi') or {}).get(key))}</td>" for key, _n in _SALES_DIMS
        ) + "".join(
            f"<td>{_fmt_sales_cell((matrix.get('autohome') or {}).get(key))}</td>" for key, _n in _SALES_DIMS
        )
        body_rows.append(
            f"<tr><td>{_e(profile['series_name'])}{'（本品）' if index == 0 else ''}</td>{cells}</tr>"
        )
    note = (
        "<p style='font-size:12px;color:#64748b;margin-top:6px'>"
        "销量数值取自懂车帝全市场榜与汽车之家细分级别榜（未进级别 Top20 时按品牌检索补齐），"
        "含最新月/近半年/近12个月三口径；两家统计口径可能略有差异。</p>"
    )
    return (
        "<h2>销量对比（懂车帝 × 汽车之家）</h2>"
        f"<table><tr><th>车系</th>{header_cells}</tr>{''.join(body_rows)}</table>{note}"
    )


def _tag_cloud(tags: list[dict[str, Any]], css: str, limit: int = 14) -> str:
    """标签云：字号随提及次数缩放。"""
    tags = tags[:limit]
    if not tags:
        return "<p>暂无标签数据。</p>"
    max_count = max(tag["count"] for tag in tags)
    chips: list[str] = []
    for tag in tags:
        size = 12 + round(8 * (tag["count"] / max_count))
        chips.append(
            f'<span class="tag-chip {css}" style="font-size:{size}px">{_e(tag["name"])}'
            f'<span class="n">×{tag["count"]}</span></span>'
        )
    return f'<div class="tag-cloud">{"".join(chips)}</div>'


def _quotes_block(profile: dict[str, Any]) -> str:
    parts: list[str] = []
    for css, label, key in (("good", "满意", "quotes_good"), ("bad", "不满意", "quotes_bad")):
        for quote in profile.get(key) or []:
            meta = " · ".join(x for x in (quote.get("spec"), quote.get("location"), quote.get("posttime")) if x)
            parts.append(
                f'<div class="quote {css}"><b>[{label}]</b> {_e(quote["text"])}'
                f'<div class="meta">{_e(meta)}</div></div>'
            )
    return "".join(parts) or "<p>暂无用户原声。</p>"


def _overview_section(overview: dict[str, Any] | None) -> str:
    if not overview or not overview.get("findings"):
        return ""
    items = "".join(
        f'<div class="ov-item"><span class="ov-icon">{_e(f.get("icon", ""))}</span>'
        f'<span class="ov-topic">{_e(f["topic"])}</span>{_e(f["text"])}</div>'
        for f in overview["findings"]
    )
    verdict = f'<div class="ov-verdict">{_e(overview["verdict"])}</div>' if overview.get("verdict") else ""
    return f'<h2>综合研判</h2><div class="overview">{items}{verdict}</div>'


_TITLE_BY_FOCUS = {
    frozenset({"sales"}): "销量对标报告",
    frozenset({"koubei"}): "口碑对标报告",
}


def render_report(
    primary: dict[str, Any],
    competitors: list[dict[str, Any]],
    output_path: Path,
    *,
    suggestions: list[dict[str, str]] | None = None,
    sections: set[str] | None = None,
    overview: dict[str, Any] | None = None,
) -> Path:
    sections = sections or {"sales", "koubei"}
    show_sales = "sales" in sections
    show_koubei = "koubei" in sections
    profiles = [primary, *competitors]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    vs_text = " vs ".join(_e(p["series_name"]) for p in profiles) if competitors else _e(primary["series_name"])
    if not competitors:
        report_kind = "车型画像报告"
    else:
        report_kind = _TITLE_BY_FOCUS.get(frozenset(sections), "竞品对标分析报告")

    cards = "".join(
        _vehicle_card(profile, _PALETTE[index % len(_PALETTE)], index == 0)
        for index, profile in enumerate(profiles)
    )

    compare_rows = "".join(
        f"<tr><td>{_e(p['series_name'])}{'（本品）' if i == 0 else ''}</td>"
        f"<td>{_e(_fmt_score(p))}</td><td>{p.get('review_count', 0)}</td>"
        f"<td>{_e(p.get('price_range') or '-')}万</td><td>{_e(_fmt_sales(p))}</td>"
        f"<td>{_e(p.get('pph') or '-')}</td><td>{_e(p.get('level_name') or '-')}</td></tr>"
        for i, p in enumerate(profiles)
    )
    sales_header = _sales_table_header(profiles)

    koubei_html = ""
    if show_koubei:
        vehicle_sections = "".join(
            f"""
<h2>{_e(profile["series_name"])} · 用户口碑透视</h2>
<div class="section-grid">
  <div class="card"><h3 style="margin-top:0">🏷 好评高频标签</h3>{_tag_cloud(profile.get("tags_good") or [], "good")}</div>
  <div class="card"><h3 style="margin-top:0">🏷 差评高频标签</h3>{_tag_cloud(profile.get("tags_bad") or [], "bad")}</div>
</div>
<div class="section-grid" style="margin-top:18px">
  <div><h3>👍 亮点（用户好评聚焦）</h3>{_highlight_block(profile)}</div>
  <div><h3>👎 槽点（用户不满聚焦）</h3>{_complaint_block(profile)}</div>
</div>
<h3>💬 用户原声</h3>
{_quotes_block(profile)}"""
            for profile in profiles
        )
        suggestion_html = ""
        if suggestions:
            suggestion_html = "<h2>本品改进建议</h2>" + "".join(
                f'<div class="suggest"><div class="topic">{index}. {_e(item["topic"])}</div>'
                f'<div class="reason">依据：{_e(item["reason"])}</div><div>{_e(item["advice"])}</div></div>'
                for index, item in enumerate(suggestions, start=1)
            )
        koubei_html = (
            f'<h2>口碑分维度评分对比</h2><div class="card">{_dim_bars(profiles)}</div>'
            f"{vehicle_sections}{suggestion_html}"
        )

    sales_html = _sales_matrix_section(profiles) if show_sales else ""

    # 数据来源按实际呈现的板块动态生成（核心指标卡片始终含口碑评分/PPH，故口碑源常在）
    sources = ["汽车之家车主口碑"]
    if show_sales:
        sources.append("懂车帝 + 汽车之家销量榜")
    source_line = " · ".join(sources)

    document = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{vs_text} {report_kind}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
<header class="hero">
  <h1>{vs_text} {report_kind}</h1>
  <p>数据来源：{source_line} &nbsp;|&nbsp; 生成时间：{generated} &nbsp;|&nbsp; Vehicle Data Hub 自动生成</p>
</header>

{_overview_section(overview)}

<h2>核心指标概览</h2>
<div class="cards">{cards}</div>

<h2>关键指标对比</h2>
<table>
<tr><th>车系</th><th>综合评分</th><th>口碑数</th><th>指导价</th><th>{_e(sales_header)}</th><th>新车PPH</th><th>细分市场</th></tr>
{compare_rows}
</table>

{sales_html}
{koubei_html}

<footer>
本报告由 Vehicle Data Hub 自动生成 · 口碑观点来自公开车主评价的自动聚合，供内部产品对标参考，不代表官方结论。<br>
销量取自懂车帝与汽车之家公开榜单，含最新月/近半年/近12个月三口径；样本量与时间窗有限，关键结论建议结合完整口碑与实测数据复核。
</footer>
</div>
</body>
</html>"""
    output_path.write_text(document, encoding="utf-8")
    return output_path
