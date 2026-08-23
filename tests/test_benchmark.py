"""对标分析链路测试（离线，合成口碑数据）。"""

from __future__ import annotations

from benchmark.analyzer import build_overview, build_profile, build_suggestions
from benchmark.collector import _month_shift, _range_label, match_sales
from benchmark.report import render_report

_SALES_MATRIX = {
    "dongchedi": {
        "latest": {"count": 8000, "rank": 12, "label": "2026年05月"},
        "half_year": {"count": 45000, "rank": 15, "label": "2025年12月~2026年05月"},
        "year": {"count": 90000, "rank": 18, "label": "2025年06月~2026年05月"},
    },
    "autohome": {
        "latest": {"count": 8100, "rank": 3, "label": "2026年05月"},
        "half_year": {"count": 46000, "rank": 3, "label": "2025年12月~2026年05月"},
        "year": {
            "count": 91000,
            "rank": 1,
            "label": "2025年06月~2026年05月",
            "scope": "brand",
            "brand": "测试品牌",
        },
        "level": "大型SUV",
    },
}


def _vehicle_data(name: str, score: str, smart_score: float) -> dict:
    return {
        "input_name": name,
        "series_id": "7207",
        "series_name": name,
        "collected_at": "2026-07-03 10:00:00",
        "sales": _SALES_MATRIX,
        "koubei": {
            "seriesname": name,
            "average": score,
            "rowcount": 100,
            "aKIndexInt": 73,
            "pricerange": "47.98-65.98",
            "levelname": "大型SUV",
            "levelseriescount": 21,
            "cmpSeriesPPH": [{"newCarPPH": 81, "newCarPPHUserCount": 48}],
            "cmpSeriesScore": [],
            "seriesScoreList": [
                {"typeName": "空间", "typeKey": 3, "score": 4.8},
                {"typeName": "智能化", "typeKey": 40, "score": smart_score},
            ],
            "structuredlist": [
                {
                    "autodrive": {
                        "list": [
                            {
                                "category1Name": "辅助驾驶",
                                "reviewContent": "99.32%的用户认为辅助驾驶值得称赞，高速领航省心。但有部分用户反馈匝道处理不稳。",
                            }
                        ]
                    }
                },
                # 「最满意/最不满意/维度」标签块：Summary 是字符串化的标签列表
                {
                    "id": 1,
                    "ge": 10,
                    "name": "最满意",
                    "Summary": "[{'unableClick': 0, 'Combination': '全部', 'Volume': 0, 'SummaryKey': 0, 'SentimentKey': 0}, "
                    "{'unableClick': 0, 'Combination': '车身霸气', 'Volume': 8, 'SummaryKey': 1, 'SentimentKey': 3}]",
                    "IsHide": 1,
                },
                {
                    "id": 2,
                    "ge": 11,
                    "name": "最不满意",
                    "Summary": "[{'unableClick': 0, 'Combination': '内饰异味大', 'Volume': 4, 'SummaryKey': 2, 'SentimentKey': 2}]",
                    "IsHide": 1,
                },
                {
                    "id": 15,
                    "ge": 8,
                    "name": "性价比",
                    "Summary": "[{'unableClick': 0, 'Combination': '性价比不错', 'Volume': 2, 'SummaryKey': 3, 'SentimentKey': 3}, "
                    "{'unableClick': 0, 'Combination': '优惠幅度小', 'Volume': 2, 'SummaryKey': 4, 'SentimentKey': 2}]",
                    "IsHide": 0,
                },
            ],
            "reviews": [
                {
                    "specname": "2026款 增程 Ultra版 6座",
                    "location": "四川",
                    "posttime": "2026-07-02",
                    "buyprice": "54.68万",
                    "contents": [
                        {"structuredname": "满意", "content": "全家出行非常舒服，第二排零重力座椅是全家人最喜欢的配置。"},
                        {"structuredname": "不满意", "content": "车机系统偶尔卡顿，导航切换目的地的时候会卡几秒，希望 OTA 优化。"},
                    ],
                },
                {
                    "specname": "2026款 增程 Max版",
                    "location": "北京",
                    "posttime": "2026-06-20",
                    "buyprice": "50.98万",
                    "contents": [
                        {"structuredname": "不满意", "content": "提车三个月就降价了，保值率有点担心，售后预约也要等很久。"},
                    ],
                },
            ],
        },
    }


def test_build_profile():
    profile = build_profile(_vehicle_data("测试车A", "4.70", 4.2))
    assert profile["series_name"] == "测试车A"
    assert profile["score"] == "4.70"
    assert profile["review_count"] == 100
    assert profile["sales_count"] == 8000
    assert profile["sales_month_text"] == "2026年05月"
    assert profile["pph"] == 81
    # 文字总结形态：「但有部分用户反馈」拆成槽点
    categories = {item["category"] for item in profile["highlights"]}
    assert "辅助驾驶" in categories
    assert any("99.32" == item["percent"] for item in profile["highlights"])
    assert profile["complaints"][0]["category"] == "辅助驾驶"
    assert "匝道" in profile["complaints"][0]["summary"]
    # 标签块形态：解析成好评/差评标签云，不能把原始列表字符串当文字用
    assert {"name": "车身霸气", "count": 8} in profile["tags_good"]
    assert {"name": "性价比不错", "count": 2} in profile["tags_good"]
    assert {"name": "内饰异味大", "count": 4} in profile["tags_bad"]
    assert {"name": "优惠幅度小", "count": 2} in profile["tags_bad"]
    assert all("Combination" not in str(item) for item in profile["highlights"])
    # 不满意关键词归类
    keyword_categories = {item["category"] for item in profile["keyword_complaints"]}
    assert "车机/智能化" in keyword_categories
    assert "价格/服务" in keyword_categories
    assert len(profile["quotes_bad"]) == 2


def test_build_overview():
    primary = build_profile(_vehicle_data("本品车", "4.50", 4.0))
    competitor = build_profile(_vehicle_data("竞品车", "4.70", 4.6))
    overview = build_overview(primary, [competitor])
    text = " ".join(f["text"] for f in overview["findings"])
    # 口碑落后：4.50 vs 4.70 均值 -> 落后 0.20
    assert "落后竞品均值 0.20" in text
    # 智能化 4.0 vs 4.6 -> 短板维度
    assert any(f["topic"] == "短板维度" and "智能化" in f["text"] for f in overview["findings"])
    assert overview["verdict"].startswith("综合研判：本品车")
    assert "智能化" in overview["verdict"]


def test_overview_excludes_low_data_competitor():
    """新车样本不足（评分 0）不能污染竞品均值。"""
    primary = build_profile(_vehicle_data("本品车", "4.44", 4.2))
    strong = build_profile(_vehicle_data("强竞品", "4.56", 4.5))
    newcar = build_profile(_vehicle_data("新车", "0.00", 0.0))  # 1 条口碑、评分 0
    newcar["review_count"] = 1
    overview = build_overview(primary, [strong, newcar])
    koubei = next(f for f in overview["findings"] if f["topic"] == "口碑")
    # 竞品均值只算强竞品 4.56，本品 4.44 落后 0.12（而非被 0 分拉低成领先）
    assert "落后竞品均值 0.12" in koubei["text"]
    assert "竞品均值 4.56" in koubei["text"]
    assert "新车 口碑样本不足已排除" in koubei["text"]
    # 评分渲染：0 分显示为「-（样本 N 条）」而非 0.00
    from benchmark.report import _fmt_score

    assert _fmt_score(newcar) == "-（样本 1 条）"
    assert _fmt_score(primary) == "4.44"


def test_focus_sections(tmp_path):
    primary = build_profile(_vehicle_data("本品车", "4.50", 4.0))
    competitor = build_profile(_vehicle_data("竞品车", "4.70", 4.6))
    # 仅销量：无口碑深度板块
    sales_only = tmp_path / "sales.html"
    render_report(primary, [competitor], sales_only, sections={"sales"}, overview=build_overview(primary, [competitor]))
    h = sales_only.read_text(encoding="utf-8")
    assert "销量对标报告" in h and "销量对比（懂车帝 × 汽车之家）" in h
    assert "用户口碑透视" not in h and "本品改进建议" not in h
    # 仅口碑：无双源销量表
    koubei_only = tmp_path / "koubei.html"
    render_report(primary, [competitor], koubei_only, sections={"koubei"},
                  suggestions=build_suggestions(primary, [competitor]))
    h = koubei_only.read_text(encoding="utf-8")
    assert "口碑对标报告" in h and "用户口碑透视" in h
    assert "销量对比（懂车帝 × 汽车之家）" not in h
    # 单车画像：无综合研判
    single = tmp_path / "single.html"
    render_report(primary, [], single, overview=None)
    assert "车型画像报告" in single.read_text(encoding="utf-8")


def test_build_suggestions_and_report(tmp_path):
    primary = build_profile(_vehicle_data("本品车", "4.50", 4.0))
    competitor = build_profile(_vehicle_data("竞品车", "4.70", 4.6))
    suggestions = build_suggestions(primary, [competitor])
    topics = [item["topic"] for item in suggestions]
    assert "车机/智能化" in topics
    assert any("智能化（对标短板）" == topic for topic in topics)  # 4.0 vs 4.6 落后 0.6

    output = tmp_path / "report.html"
    render_report(primary, [competitor], output, suggestions=suggestions,
                  overview=build_overview(primary, [competitor]))
    html = output.read_text(encoding="utf-8")
    assert "本品车" in html and "竞品车" in html
    assert "竞品对标分析报告" in html
    assert "本品改进建议" in html
    assert "智能化（对标短板）" in html
    assert "2026年05月销量" in html
    assert "2026年05月：8,000 辆" in html
    # 销量对比：两源三维度，表头标明月份范围，只展示销量数值、不含排名
    assert "销量对比（懂车帝 × 汽车之家）" in html
    assert "懂车帝·近半年" in html and "汽车之家·近12个月" in html
    assert "2025年12月~2026年05月" in html and "2025年06月~2026年05月" in html
    assert "45,000 辆" in html and "91,000 辆" in html
    assert "第3名" not in html and "品牌筛选" not in html  # 已移除排名呈现
    # 标签云渲染为可视化 chip，且不能出现原始列表字符串
    assert "tag-chip" in html
    assert "车身霸气" in html and "内饰异味大" in html
    assert "Combination" not in html and "SentimentKey" not in html


def test_match_sales():
    rows = [
        {"series_name": "示例车型M9", "rank": 3, "count": 15000},
        {"series_name": "示例车型B", "rank": 40, "count": 2000},
    ]
    assert match_sales(rows, "示例车型M9")["count"] == 15000
    assert match_sales(rows, "示例车型 M9")["count"] == 15000  # 忽略空格
    assert match_sales(rows, "不存在车型") is None


def test_match_sales_cross_language():
    """懂车帝对部分品牌用英文名（中文名→拉丁名），按型号标识跨语言兜底匹配。"""
    rows = [
        {"series_name": "LATINBRAND 009", "rank": 244, "count": 1536},
        {"series_name": "LATINBRAND 007", "rank": 120, "count": 5335},
        {"series_name": "星愿", "rank": 1, "count": 38751},
    ]
    assert match_sales(rows, "示例品牌009")["count"] == 1536  # 示例品牌009 ↔ LATINBRAND 009
    assert match_sales(rows, "示例品牌007")["count"] == 5335
    # 数字型号按完整段匹配，009 不得命中 1009
    colliding = [{"series_name": "LATIN 1009", "count": 99}]
    assert match_sales(colliding, "示例品牌009") is None
    assert match_sales(colliding, "示例品牌1009")["count"] == 99
    # 型号标识歧义（多命中）时不误配：全中文查询无 009/007 token 命中
    ambiguous = [{"series_name": "丙品牌M9", "count": 1}, {"series_name": "LATIN M9", "count": 2}]
    assert match_sales(ambiguous, "示例车型M9") is None  # M9 命中 2 条，非唯一 -> 不猜


def test_month_range_labels():
    assert _month_shift(202601, -1) == 202512
    assert _range_label(202605, 1) == "2026年05月"
    assert _range_label(202605, 6) == "2025年12月~2026年05月"
    assert _range_label(202605, 12) == "2025年06月~2026年05月"
