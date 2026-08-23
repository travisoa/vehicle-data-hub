"""口碑数据分析：提炼亮点/槽点/用户原声，生成改进建议。

输入 collector.collect_vehicle 的原始数据（纯 dict），输出归一化画像，便于离线测试。
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from typing import Any

# 不满意文本的槽点归类词典
COMPLAINT_LEXICON: dict[str, list[str]] = {
    "车机/智能化": ["车机", "卡顿", "死机", "语音", "导航", "蓝牙", "OTA", "软件", "app", "APP", "地图", "智驾", "辅助驾驶", "泊车", "屏幕"],
    "续航/能耗": ["续航", "掉电", "油耗", "电耗", "充电", "亏电", "打折", "耗电"],
    "舒适性/NVH": ["异响", "噪音", "胎噪", "风噪", "颠", "悬挂", "悬架", "滤震", "座椅硬", "晕车", "隔音"],
    "空间/储物": ["空间", "后备箱", "腿部", "头部", "第三排", "储物", "拥挤"],
    "动力/操控": ["动力", "加速", "刹车", "转向", "操控", "顿挫", "肉", "推背"],
    "内饰做工": ["内饰", "做工", "塑料", "异味", "装配", "缝隙", "品控", "质感"],
    "外观细节": ["外观", "颜值", "造型", "车漆", "轮毂"],
    "价格/服务": ["价格", "降价", "保值", "优惠", "服务", "售后", "交付", "保险", "维修", "贵"],
}

# 槽点类别 -> 改进建议模板
SUGGESTION_TEMPLATES: dict[str, str] = {
    "车机/智能化": "优先优化车机系统流畅度与语音/导航体验，建立 OTA 快速迭代通道，把智能化从卖点做成口碑护城河。",
    "续航/能耗": "标定更贴近真实工况的续航显示策略，公开低温/高速衰减数据，配合充电网络合作降低里程焦虑。",
    "舒适性/NVH": "针对异响与路噪做专项 NVH 整改（声学包、衬套、密封件），并纳入下线检测项。",
    "空间/储物": "在改款中优化座椅布局与储物方案（第三排进出、后备箱纵深），用场景化空间演示回应用户关切。",
    "动力/操控": "优化动力标定的平顺性（起步/低速顿挫），提供多种驾驶模式标定以覆盖不同驾驶偏好。",
    "内饰做工": "提升装配一致性与内饰用料，重点消除异味投诉（低 VOC 材料），加强供应商来料品控。",
    "外观细节": "保持家族化设计语言的同时，通过外观选装件与个性化配色回应差异化审美需求。",
    "价格/服务": "稳定价格体系保护老车主权益，缩短交付周期，强化售后响应 SLA 与透明化维保价格。",
}

_PERCENT_RE = re.compile(r"^(\d+(?:\.\d+)?)%")


def _split_review_content(text: str) -> tuple[str, str]:
    """结构化总结通常是「xx%的用户认为…。但有部分用户反馈…」，切成好评/差评两段。"""
    for splitter in ("但有部分用户反馈", "但有用户反馈", "但部分用户", "但也有用户"):
        if splitter in text:
            positive, _, negative = text.partition(splitter)
            return positive.strip(), (splitter + negative).strip()
    return text.strip(), ""


def _extract_structured(koubei: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """从 structuredlist 提取官方总结的亮点/槽点（文字总结形态）。"""
    highlights: list[dict[str, str]] = []
    complaints: list[dict[str, str]] = []
    seen: set[str] = set()
    for block in koubei.get("structuredlist") or []:
        if not isinstance(block, dict):
            continue
        # {"autodrive": {"list": [{category1Name, reviewContent}...]}}
        for value in block.values():
            if isinstance(value, dict) and isinstance(value.get("list"), list):
                for item in value["list"]:
                    category = str(item.get("category1Name", "")).strip()
                    content = str(item.get("reviewContent", "")).strip()
                    if not content or category in seen:
                        continue
                    seen.add(category)
                    positive, negative = _split_review_content(content)
                    match = _PERCENT_RE.match(positive)
                    if positive:
                        highlights.append(
                            {"category": category, "percent": match.group(1) if match else "", "summary": positive}
                        )
                    if negative:
                        complaints.append({"category": category, "summary": negative})
    return highlights, complaints


# SentimentKey：3=好评标签，2=差评标签
_SENTIMENT_GOOD = 3
_SENTIMENT_BAD = 2


def _parse_tag_summary(summary: Any) -> list[dict[str, Any]]:
    """Summary 字段是字符串化的标签列表，解析成 [{name, count, sentiment}]。"""
    if isinstance(summary, str):
        text = summary.strip()
        if not text.startswith("["):
            return []
        try:
            summary = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return []
    if not isinstance(summary, list):
        return []
    tags: list[dict[str, Any]] = []
    for item in summary:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Combination") or item.get("combination") or "").strip()
        count = int(item.get("Volume") or item.get("volume") or 0)
        sentiment = int(item.get("SentimentKey") or item.get("sentimentKey") or 0)
        if name and name != "全部" and count > 0:
            tags.append({"name": name, "count": count, "sentiment": sentiment})
    return tags


def _extract_tags(koubei: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """从「最满意/最不满意/各维度」标签块提取好评/差评标签云（按标签名去重取最大次数）。"""
    good: dict[str, int] = {}
    bad: dict[str, int] = {}
    for block in koubei.get("structuredlist") or []:
        if not isinstance(block, dict) or block.get("Summary") is None:
            continue
        for tag in _parse_tag_summary(block.get("Summary")):
            bucket = good if tag["sentiment"] == _SENTIMENT_GOOD else bad if tag["sentiment"] == _SENTIMENT_BAD else None
            if bucket is not None and tag["count"] > bucket.get(tag["name"], 0):
                bucket[tag["name"]] = tag["count"]
    def to_sorted(bucket: dict[str, int]) -> list[dict[str, Any]]:
        return [{"name": name, "count": count} for name, count in sorted(bucket.items(), key=lambda kv: -kv[1])]

    return to_sorted(good), to_sorted(bad)


def _review_texts(reviews: list[dict[str, Any]], structured_name: str) -> list[tuple[str, dict[str, Any]]]:
    texts: list[tuple[str, dict[str, Any]]] = []
    for review in reviews:
        for content in review.get("contents") or []:
            if content.get("structuredname") == structured_name:
                text = str(content.get("content", "")).strip()
                if text:
                    texts.append((text, review))
    return texts


def _analyze_complaint_keywords(
    negative_texts: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """按词典统计不满意文本的槽点类别与代表原声。"""
    counter: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}
    for text, _review in negative_texts:
        matched: set[str] = set()
        for category, keywords in COMPLAINT_LEXICON.items():
            if any(keyword in text for keyword in keywords):
                matched.add(category)
        for category in matched:
            counter[category] += 1
            samples.setdefault(category, [])
            if len(samples[category]) < 2:
                samples[category].append(text)
    return [
        {"category": category, "count": count, "samples": samples.get(category, [])}
        for category, count in counter.most_common()
    ]


def _pick_quotes(texts: list[tuple[str, dict[str, Any]]], limit: int) -> list[dict[str, str]]:
    quotes: list[dict[str, str]] = []
    for text, review in texts:
        if len(text) < 20:
            continue
        quotes.append(
            {
                "text": text,
                "spec": str(review.get("specname", "")),
                "location": str(review.get("location", "")),
                "posttime": str(review.get("posttime", "")),
            }
        )
        if len(quotes) >= limit:
            break
    return quotes


def build_profile(data: dict[str, Any]) -> dict[str, Any]:
    """把采集数据归一化成报告画像。"""
    koubei = data.get("koubei") or {}
    reviews = koubei.get("reviews") or []
    highlights, complaints = _extract_structured(koubei)
    tags_good, tags_bad = _extract_tags(koubei)
    negative_texts = _review_texts(reviews, "不满意")
    positive_texts = _review_texts(reviews, "满意")
    keyword_complaints = _analyze_complaint_keywords(negative_texts)

    buy_prices = [str(r.get("buyprice", "")) for r in reviews if r.get("buyprice")]
    raw_sales = data.get("sales") or {}
    # 新格式：{dongchedi: {latest/half_year/year: {count, rank, label}}, autohome: {...}}
    sales_matrix = raw_sales if isinstance(raw_sales.get("dongchedi") or raw_sales.get("autohome"), dict) else None
    if sales_matrix:
        latest = (sales_matrix.get("dongchedi") or {}).get("latest") or {}
        sales = {
            "count": latest.get("count"),
            "rank": latest.get("rank"),
            "sales_month_text": latest.get("label"),
        }
    else:
        sales = raw_sales  # 旧缓存平铺格式兼容
    return {
        "name": data.get("input_name", ""),
        "series_name": koubei.get("seriesname") or data.get("series_name", ""),
        "series_id": str(data.get("series_id", "")),
        "collected_at": data.get("collected_at", ""),
        "score": str(koubei.get("average", "")),
        "review_count": int(koubei.get("rowcount") or 0),
        "ak_index": koubei.get("aKIndexInt"),
        "price_range": str(koubei.get("pricerange", "")),
        "level_name": str(koubei.get("levelname", "")),
        "level_series_count": koubei.get("levelseriescount"),
        "pph": (koubei.get("cmpSeriesPPH") or [{}])[0].get("newCarPPH"),
        "pph_user_count": (koubei.get("cmpSeriesPPH") or [{}])[0].get("newCarPPHUserCount"),
        "dim_scores": [
            {"name": str(item.get("typeName", "")), "score": float(item.get("score") or 0)}
            for item in koubei.get("seriesScoreList") or []
        ],
        "highlights": highlights,
        "complaints": complaints,
        "tags_good": tags_good,
        "tags_bad": tags_bad,
        "keyword_complaints": keyword_complaints,
        "quotes_good": _pick_quotes(positive_texts, 3),
        "quotes_bad": _pick_quotes(negative_texts, 3),
        "sample_buy_prices": buy_prices[:5],
        "sales_rank": sales.get("rank"),
        "sales_count": sales.get("count"),
        "sales_month": sales.get("sales_month"),
        "sales_month_text": sales.get("sales_month_text"),
        "sales_matrix": sales_matrix,
        "cmp_scores": koubei.get("cmpSeriesScore") or [],
    }


def valid_score(profile: dict[str, Any]) -> float | None:
    """综合口碑评分，仅当 >0 时有效（新车样本不足时接口返回 0/空，不能当真实评分参与对比）。"""
    try:
        value = float(profile.get("score") or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def pick_headline_sales(profile: dict[str, Any]) -> dict[str, Any] | None:
    """挑一个代表性销量口径：优先懂车帝近12个月，依次回退到近半年/最新月、再回退汽车之家。"""
    matrix = profile.get("sales_matrix") or {}
    for source, src_name in (("dongchedi", "懂车帝全榜"), ("autohome", "汽车之家级别榜")):
        for key, dim_name in (("year", "近12个月"), ("half_year", "近半年"), ("latest", "最新月")):
            entry = (matrix.get(source) or {}).get(key) or {}
            if entry.get("count"):
                return {"count": int(entry["count"]), "dim": dim_name, "source": src_name, "label": entry.get("label", "")}
    if profile.get("sales_count"):  # 旧平铺缓存兼容
        return {"count": int(profile["sales_count"]), "dim": "最新月", "source": "懂车帝", "label": profile.get("sales_month_text", "")}
    return None


def build_overview(primary: dict[str, Any], competitors: list[dict[str, Any]]) -> dict[str, Any]:
    """综合研判：把销量位次、口碑分差、维度强弱、价格定位合成为要点 + 一句结论。"""
    profiles = [primary, *competitors]
    findings: list[dict[str, str]] = []

    # 销量位次（同口径优先取近12个月）
    sales = [(p, pick_headline_sales(p)) for p in profiles]
    ranked = sorted([(p, s) for p, s in sales if s], key=lambda x: -x[1]["count"])
    primary_sales = next((s for p, s in sales if p is primary), None)
    if primary_sales and len(ranked) > 1:
        order = [p for p, _ in ranked]
        pos = order.index(primary) + 1
        tail = "领先本次对比全部竞品" if pos == 1 else f"落后榜首 {ranked[0][0]['series_name']}（{ranked[0][1]['count']:,} 辆）"
        findings.append({
            "icon": "📈",
            "topic": "销量",
            "text": f"{primary_sales['label']}{primary_sales['dim']}销量 {primary_sales['count']:,} 辆，"
                    f"在 {len(ranked)} 款可比车型中列第 {pos}，{tail}。",
        })

    # 口碑评分 vs 竞品均值（评分 ≤0 视为样本不足，排除出均值，避免污染研判）
    p_score = valid_score(primary)
    comp_scores = [s for s in (valid_score(c) for c in competitors) if s is not None]
    excluded = [c["series_name"] for c in competitors if valid_score(c) is None]
    if p_score and comp_scores:
        avg = sum(comp_scores) / len(comp_scores)
        diff = p_score - avg
        note = f"（竞品均值 {avg:.2f}"
        note += f"；{'、'.join(excluded)} 口碑样本不足已排除）" if excluded else "）"
        findings.append({
            "icon": "⭐",
            "topic": "口碑",
            "text": f"综合口碑 {p_score:.2f} 分，{'领先' if diff >= 0 else '落后'}竞品均值 {abs(diff):.2f} 分{note}。",
        })

    # 维度强弱：本品相对竞品均值差最大/最小的维度（同样排除 ≤0 的无效维度分）
    best = worst = None
    for dim in primary.get("dim_scores", []):
        if dim["score"] <= 0:
            continue
        others = [
            o["score"]
            for c in competitors
            for o in c.get("dim_scores", [])
            if o["name"] == dim["name"] and o["score"] > 0
        ]
        if not others:
            continue
        gap = dim["score"] - sum(others) / len(others)
        if best is None or gap > best[1]:
            best = (dim["name"], gap)
        if worst is None or gap < worst[1]:
            worst = (dim["name"], gap)
    if best and best[1] > 0.05:
        findings.append({"icon": "🟢", "topic": "优势维度", "text": f"「{best[0]}」口碑领先竞品均值 {best[1]:.2f} 分，是主要相对优势。"})
    if worst and worst[1] < -0.05:
        findings.append({"icon": "🔴", "topic": "短板维度", "text": f"「{worst[0]}」口碑落后竞品均值 {abs(worst[1]):.2f} 分，是首要补强方向。"})

    # 价格定位
    def _low_price(profile: dict[str, Any]) -> float | None:
        match = re.search(r"\d+(?:\.\d+)?", str(profile.get("price_range") or ""))
        return float(match.group()) if match else None

    p_price = _low_price(primary)
    comp_prices = [v for v in (_low_price(c) for c in competitors) if v is not None]
    if p_price is not None and comp_prices:
        avg_price = sum(comp_prices) / len(comp_prices)
        if p_price <= min(comp_prices):
            note = "起售价为本次对比最低，价格优势明显"
        elif p_price >= max(comp_prices):
            note = "起售价为本次对比最高，走高端定位"
        else:
            note = f"起售价居中（竞品均值约 {avg_price:.1f} 万）"
        findings.append({"icon": "💰", "topic": "价格", "text": f"厂商指导价起 {p_price:.1f} 万，{note}。"})

    # 一句结论：销量/口碑/价格三项头部指标 + 最佳维度 + 首要短板
    verdict = ""
    if competitors:
        wins = [
            topic
            for topic, kw in (("销量", "领先"), ("口碑", "领先"), ("价格", "最低"))
            if any(f["topic"] == topic and kw in f["text"] for f in findings)
        ]
        lead_dim = best[0] if best and best[1] > 0.05 else ""
        gap_dim = worst[0] if worst and worst[1] < -0.05 else ""
        clauses = []
        if wins:
            clauses.append("、".join(wins) + "占优")
        if lead_dim:
            clauses.append(f"「{lead_dim}」口碑最佳")
        head = "，".join(clauses) if clauses else "各项表现与竞品接近"
        tail = f"；主要短板为「{gap_dim}」口碑" if gap_dim else ""
        verdict = f"综合研判：{primary['series_name']} 相对本次竞品，{head}{tail}。"

    return {"findings": findings, "verdict": verdict}


def build_suggestions(primary: dict[str, Any], competitors: list[dict[str, Any]]) -> list[dict[str, str]]:
    """针对本品生成改进建议：槽点频次 + 与竞品的维度分差。"""
    suggestions: list[dict[str, str]] = []
    seen: set[str] = set()

    for item in primary.get("keyword_complaints", [])[:4]:
        category = item["category"]
        if category in seen or category not in SUGGESTION_TEMPLATES:
            continue
        seen.add(category)
        sample = item["samples"][0] if item.get("samples") else ""
        suggestions.append(
            {
                "topic": category,
                "reason": f"{item['count']} 条「不满意」评价提及" + (f"，如：“{sample[:60]}…”" if sample else ""),
                "advice": SUGGESTION_TEMPLATES[category],
            }
        )

    # 与竞品的维度分差（落后 >= 0.15 分）
    competitor_best: dict[str, tuple[float, str]] = {}
    for competitor in competitors:
        for dim in competitor.get("dim_scores", []):
            best = competitor_best.get(dim["name"])
            if best is None or dim["score"] > best[0]:
                competitor_best[dim["name"]] = (dim["score"], competitor.get("series_name", ""))
    for dim in primary.get("dim_scores", []):
        best = competitor_best.get(dim["name"])
        if best and best[0] - dim["score"] >= 0.15:
            suggestions.append(
                {
                    "topic": f"{dim['name']}（对标短板）",
                    "reason": f"本品 {dim['score']:.2f} 分，落后 {best[1]} 的 {best[0]:.2f} 分",
                    "advice": f"拆解 {best[1]} 在「{dim['name']}」维度的用户好评来源，明确差距项并纳入下一改款目标。",
                }
            )
    return suggestions
