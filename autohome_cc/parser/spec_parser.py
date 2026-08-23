"""车型页面与参数 JSON 解析。"""

from __future__ import annotations

import json
import re
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from autohome_cc.utils.cleaners import (
    extract_measurements,
    extract_numeric_value,
    normalize_text_value,
    normalize_whitespace,
)

LABEL_MAPPING = {
    "brand": ["品牌"],
    "series_name": ["车系", "返回"],
    "model_name": ["车型名称", "车型"],
    "energy_type": ["能源类型", "能 源", "能源"],
    "market_date": ["上市时间", "上市日期"],
    "official_price": ["厂商指导价", "官方指导价", "指导价", "价格"],
    "sales_volume": ["销量", "月销量"],
    "engine": ["发动机"],
    "displacement": ["排量", "发动机排量"],
    "intake_type": ["进气形式"],
    "max_power_kw": ["最大功率", "发动机最大功率", "总功率"],
    "max_torque_nm": ["最大扭矩", "发动机最大扭矩", "总扭矩"],
    "horsepower": ["马力", "最大马力"],
    "motor_power_kw": ["电动机总功率", "电动机最大功率", "电机总功率"],
    "motor_torque_nm": ["电动机总扭矩", "电动机最大扭矩", "电机总扭矩"],
    "gearbox": ["变速箱"],
    "drive_type": ["驱动方式", "驱动形式"],
    "battery_type": ["电池类型"],
    "battery_capacity_kwh": ["电池容量", "电池能量"],
    "cltc_range_km": ["CLTC纯电续航里程", "CLTC续航里程"],
    "wltc_range_km": ["WLTC纯电续航里程", "WLTC续航里程"],
    "fast_charge_time": ["快充时间", "快充"],
    "slow_charge_time": ["慢充时间", "慢充"],
    "wheelbase_mm": ["轴距"],
    "seats": ["座位数", "座椅数"],
    "doors": ["车门数"],
    "curb_weight_kg": ["整备质量"],
    "trunk_volume_l": ["行李厢容积", "后备厢容积", "行李箱容积"],
}

FIELD_ID_TO_LABEL = {
    52: "厂商",
    53: "级别",
    8453: "上市时间",
    1149: "能源类型",
    8214: "WLTC纯电续航里程(km)",
    8428: "CLTC纯电续航里程(km)",
    9028: "电池快充时间(小时)",
    9029: "电池慢充时间(小时)",
    9030: "电池快充电量范围(%)",
    9031: "电池慢充电量范围(%)",
    1185: "最大功率(kW)",
    9167: "最大扭矩(N·m)",
    1147: "车身结构",
    1150: "发动机",
    1148: "长*宽*高(mm)",
    1171: "整备质量(kg)",
    1255: "整车质保",
    5886: "长度(mm)",
    5887: "宽度(mm)",
    5888: "高度(mm)",
    1169: "轴距(mm)",
    1170: "前轮距(mm)",
    9004: "后轮距(mm)",
    1172: "车门数(个)",
    1173: "座位数(个)",
    1175: "后备厢容积(L)",
    8444: "车门开启方式",
    8445: "最大满载质量(kg)",
    8446: "电能当量燃料消耗量(L/100km)",
    8447: "官方首任车主权益",
    9148: "官方100-0km/h制动(m)",
    1182: "排量(mL)",
    9124: "排量(L)",
    1183: "进气形式",
    1294: "最大马力(Ps)",
    1198: "电动机(Ps)",
    1265: "变速箱简称",
    1230: "变速箱类型",
}

TEXT_PATTERNS = {
    "brand": [
        re.compile(r"价格单_(?P<value>[^_]+?)_汽车之家"),
    ],
    "series_name": [
        re.compile(r"返回\s*(?P<value>[^\n\r]+)"),
    ],
    "model_name": [
        re.compile(r"当前位置：.*?>\s*(?P<value>\d{4}款[^>\n\r]+?)\s*>参数配置"),
        re.compile(r"(?P<value>\d{4}款[^\n\r]+?)(?:参数配置|价格单)"),
    ],
    "market_date": [
        re.compile(r"上市时间[:：]?\s*(?P<value>[0-9]{4}[.\-/年][0-9]{1,2})"),
    ],
    "official_price": [
        re.compile(r"(?:厂商指导价|官方指导价|指导价)[:：]?\s*(?P<value>[0-9.\-~至万元]+)"),
    ],
    "sales_volume": [
        re.compile(r"(?:月销量|销量)\s*(?P<value>[0-9,]+辆)"),
    ],
    "engine": [
        re.compile(r"发动机[:：]?\s*(?P<value>[^\n\r]{1,80})"),
    ],
    "gearbox": [
        re.compile(r"变速箱[:：]?\s*(?P<value>[^\n\r]{1,80})"),
    ],
    "drive_type": [
        re.compile(r"驱动方式[:：]?\s*(?P<value>[^\n\r]{1,40})"),
    ],
    "seats": [
        re.compile(r"座位数[:：]?\s*(?P<value>\d+座)"),
    ],
    "energy_type": [
        re.compile(r"(?:能源类型|能\s*源)[:：]?\s*(?P<value>[^\n\r]{1,80})"),
    ],
    "battery_capacity_kwh": [
        re.compile(r"(?:电池容量|电池能量)[:：]?\s*(?P<value>[0-9.]+)\s*kWh", re.I),
    ],
    "cltc_range_km": [
        re.compile(r"CLTC(?:纯电)?续航里程[:：]?\s*(?P<value>[0-9.]+)\s*km", re.I),
    ],
    "wltc_range_km": [
        re.compile(r"WLTC(?:纯电)?续航里程[:：]?\s*(?P<value>[0-9.]+)\s*km", re.I),
    ],
    "fast_charge_time": [
        re.compile(r"快充(?:时间)?[:：]?\s*(?P<value>[0-9.]+小时)"),
    ],
    "slow_charge_time": [
        re.compile(r"慢充(?:时间)?[:：]?\s*(?P<value>[0-9.]+小时)"),
    ],
    "wheelbase_mm": [
        re.compile(r"轴距[:：]?\s*(?P<value>[0-9.]+)\s*mm", re.I),
    ],
    "curb_weight_kg": [
        re.compile(r"整备质量[:：]?\s*(?P<value>[0-9.]+)\s*kg", re.I),
    ],
    "trunk_volume_l": [
        re.compile(r"(?:行李厢容积|后备厢容积|行李箱容积)[:：]?\s*(?P<value>[0-9.]+)\s*L", re.I),
    ],
}


def parse_autohome_page(
    model_name: str,
    url: str,
    html: str,
    *,
    search_title: str = "",
    search_snippet: str = "",
) -> tuple[dict, dict]:
    """解析 Autohome 页面，返回 summary / raw_data 两套结果。"""
    soup = BeautifulSoup(html, "html.parser")
    text = normalize_whitespace(soup.get_text("\n", strip=True))
    pair_map = collect_label_value_pairs(soup)

    summary = {
        "input_name": model_name,
        "brand": "",
        "series_name": "",
        "model_name": "",
        "autohome_url": url,
        "energy_type": "",
        "market_date": "",
        "official_price": "",
        "sales_volume": "",
        "engine": "",
        "displacement": "",
        "intake_type": "",
        "max_power_kw": "",
        "max_torque_nm": "",
        "horsepower": "",
        "motor_power_kw": "",
        "motor_torque_nm": "",
        "gearbox": "",
        "drive_type": "",
        "battery_type": "",
        "battery_capacity_kwh": "",
        "cltc_range_km": "",
        "wltc_range_km": "",
        "fast_charge_time": "",
        "slow_charge_time": "",
        "length_mm": "",
        "width_mm": "",
        "height_mm": "",
        "wheelbase_mm": "",
        "seats": "",
        "doors": "",
        "curb_weight_kg": "",
        "trunk_volume_l": "",
    }

    title = normalize_whitespace(soup.title.get_text(" ", strip=True) if soup.title else "")
    description = ""
    desc_tag = soup.find("meta", attrs={"name": "description"})
    if desc_tag:
        description = normalize_whitespace(desc_tag.get("content", ""))

    raw_record = {
        "input_name": model_name,
        "matched_title": search_title,
        "search_query": "",
        "search_rank": "",
        "search_snippet": search_snippet,
        "autohome_url": url,
        "page_title": title,
        "page_description": description,
        "raw_pairs_json": json.dumps(pair_map, ensure_ascii=False),
        "raw_text_excerpt": text[:2000],
    }

    for field, aliases in LABEL_MAPPING.items():
        value = _pick_from_labels(pair_map, aliases)
        if not value:
            value = _pick_from_text(text, field)
        summary[field] = normalize_text_value(value)

    _enrich_from_title_and_breadcrumb(summary, title, text)
    _extract_body_size(summary, pair_map, text)
    _normalize_numeric_fields(summary)

    return summary, raw_record


def parse_series_config_page(
    model_name: str,
    series_url: str,
    html: str,
    *,
    search_title: str = "",
    search_snippet: str = "",
    brand_name: str = "",
) -> tuple[dict, dict]:
    """解析车系配置页中的嵌入 JSON。"""
    config_data = extract_config_json(html)
    result = config_data.get("result") or {}
    paramtypeitems = result.get("paramtypeitems") or []
    selected_spec = choose_best_spec(model_name, paramtypeitems)
    if not selected_spec:
        raise ValueError("未能从车系配置页定位具体车型")

    spec_id = str(selected_spec.get("specid", "")).strip()
    selected_model_name = selected_spec.get("model_name", "")
    pair_map = build_pair_map_for_spec(paramtypeitems, spec_id)
    page_title = extract_title_from_html(html)

    summary = build_summary_from_pairs(
        model_name=model_name,
        pair_map=pair_map,
        page_title=page_title,
        fallback_text=selected_model_name,
        autohome_url=f"https://car.autohome.com.cn/config/spec/{spec_id}.html" if spec_id else series_url,
        fallback_brand=brand_name,
    )

    raw_record = {
        "input_name": model_name,
        "matched_title": search_title or summary.get("series_name", ""),
        "search_query": "",
        "search_rank": "",
        "search_snippet": search_snippet,
        "autohome_url": summary["autohome_url"],
        "page_title": page_title,
        "page_description": "",
        "raw_pairs_json": json.dumps(pair_map, ensure_ascii=False),
        "raw_text_excerpt": selected_model_name,
    }
    return summary, raw_record


def extract_related_config_urls(base_url: str, html: str) -> list[str]:
    """从车型页中尽量找出参数配置页，作为补抓路径。"""
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for link in soup.select("a[href]"):
        href = link.get("href", "").strip()
        text = normalize_whitespace(link.get_text(" ", strip=True))
        if not href:
            continue
        absolute_url = urljoin(base_url, href)
        if "autohome.com.cn" not in absolute_url:
            continue
        if "/config/" in absolute_url or "参数配置" in text:
            if absolute_url not in urls:
                urls.append(absolute_url)
    return urls


def collect_label_value_pairs(soup: BeautifulSoup) -> dict[str, str]:
    """抽取页面里尽可能多的“标签-值”对。"""
    pairs: dict[str, str] = {}

    for row in soup.select("tr"):
        cells = [normalize_whitespace(cell.get_text(" ", strip=True)) for cell in row.select("th, td")]
        cells = [cell for cell in cells if cell]
        if len(cells) == 2 and cells[0] not in pairs:
            pairs[cells[0]] = cells[1]

    for item in soup.select("li, p, div, span"):
        text = normalize_whitespace(item.get_text(" ", strip=True))
        if not text or len(text) > 120:
            continue
        if "：" in text:
            key, value = text.split("：", 1)
        elif ":" in text:
            key, value = text.split(":", 1)
        else:
            continue
        key = normalize_whitespace(key)
        value = normalize_whitespace(value)
        if key and value and key not in pairs:
            pairs[key] = value

    return pairs


def extract_config_json(html: str) -> dict:
    """从车系配置页提取 `var config = {...}`。"""
    return extract_named_json(html, "config")


def extract_named_json(html: str, variable_name: str) -> dict:
    """从页面脚本中提取 `var <name> = {...}`。"""
    pattern = re.compile(rf"\bvar\s+{re.escape(variable_name)}\s*=\s*", re.I)
    match = pattern.search(html)
    if not match:
        raise ValueError(f"页面中未找到 {variable_name} JSON")

    brace_start = html.find("{", match.end())
    if brace_start == -1:
        raise ValueError(f"{variable_name} JSON 起始位置无效")

    brace_end = _find_matching_brace(html, brace_start)
    json_text = html[brace_start : brace_end + 1]
    return json.loads(json_text)


def extract_page_data_bundle(html: str) -> dict[str, dict]:
    """提取配置页常见的多个内嵌 JSON 变量。"""
    bundle: dict[str, dict] = {}
    for variable_name in ("config", "option", "bag", "color", "innerColor"):
        try:
            bundle[variable_name] = extract_named_json(html, variable_name)
        except ValueError:
            continue

    if "config" not in bundle:
        raise ValueError("页面中未找到 config JSON")
    return bundle


def choose_best_spec(model_name: str, paramtypeitems: list[dict]) -> dict:
    """根据输入车型名，从系列配置页里选一个最相关的具体车型。"""
    model_item = None
    for group in paramtypeitems:
        for item in group.get("paramitems", []):
            clean_name = strip_html(item.get("name", ""))
            if "车型" in clean_name:
                model_item = item
                break
        if model_item:
            break

    if not model_item:
        return {}

    candidates = []
    for index, value_item in enumerate(model_item.get("valueitems", []), start=1):
        clean_value = normalize_whitespace(strip_html(value_item.get("value", "")))
        score = score_spec_candidate(model_name, clean_value, index)
        candidates.append(
            {
                "specid": value_item.get("specid", ""),
                "model_name": clean_value,
                "score": score,
                "index": index,
            }
        )

    if not candidates:
        return {}

    candidates.sort(key=lambda item: (-item["score"], item["index"]))
    return candidates[0]


def score_spec_candidate(input_name: str, candidate_name: str, index: int) -> int:
    """对候选车型打分。"""
    query = normalize_whitespace(input_name).lower()
    candidate = normalize_whitespace(candidate_name).lower()
    score = 100 - index

    for token in re.split(r"[\s,，/_-]+", query):
        if token and token in candidate:
            score += 12

    year_match = re.search(r"(20\d{2})款", candidate_name)
    if year_match:
        score += int(year_match.group(1))

    return score


def build_pair_map_for_spec(paramtypeitems: list[dict], spec_id: str) -> dict[str, str]:
    """把指定 spec 列抽成扁平键值对。"""
    pair_map: dict[str, str] = {}
    for group in paramtypeitems:
        group_name = normalize_whitespace(strip_html(group.get("name", "")))
        for item_index, item in enumerate(group.get("paramitems", []), start=1):
            label = normalize_whitespace(strip_html(item.get("name", "")))
            item_id = int(item.get("id", -99999))
            label = FIELD_ID_TO_LABEL.get(item_id, label)
            if not label:
                continue
            selected_value = ""
            for value_item in item.get("valueitems", []):
                if str(value_item.get("specid", "")) == str(spec_id):
                    selected_value = normalize_whitespace(
                        strip_html(value_item.get("value", ""))
                    )
                    break
            if selected_value:
                # 价格字段在部分页面里被高亮切碎，只剩“厂 ( )”，按位置兜底。
                if group_name == "基本参数" and item_index == 2 and looks_like_price(selected_value):
                    label = "厂商指导价"
                elif group_name == "基本参数" and item_index == 3 and not looks_like_price(selected_value):
                    label = "厂商"
                pair_map[label] = selected_value
    return pair_map


def build_summary_from_pairs(
    *,
    model_name: str,
    pair_map: dict[str, str],
    page_title: str,
    fallback_text: str,
    autohome_url: str,
    fallback_brand: str = "",
) -> dict:
    """基于键值对生成 summary 行。"""
    summary = {
        "input_name": model_name,
        "brand": "",
        "series_name": "",
        "model_name": "",
        "autohome_url": autohome_url,
        "energy_type": "",
        "market_date": "",
        "official_price": "",
        "sales_volume": "",
        "engine": "",
        "displacement": "",
        "intake_type": "",
        "max_power_kw": "",
        "max_torque_nm": "",
        "horsepower": "",
        "motor_power_kw": "",
        "motor_torque_nm": "",
        "gearbox": "",
        "drive_type": "",
        "battery_type": "",
        "battery_capacity_kwh": "",
        "cltc_range_km": "",
        "wltc_range_km": "",
        "fast_charge_time": "",
        "slow_charge_time": "",
        "length_mm": "",
        "width_mm": "",
        "height_mm": "",
        "wheelbase_mm": "",
        "seats": "",
        "doors": "",
        "curb_weight_kg": "",
        "trunk_volume_l": "",
    }

    for field, aliases in LABEL_MAPPING.items():
        value = _pick_from_labels(pair_map, aliases)
        summary[field] = normalize_text_value(value)

    if page_title:
        title_match = re.search(r"汽车之家\|([^|]+)\|报价大全", page_title)
        if title_match:
            summary["series_name"] = summary["series_name"] or normalize_whitespace(
                title_match.group(1)
            )

    if not summary["model_name"]:
        summary["model_name"] = fallback_text

    if not summary["brand"]:
        summary["brand"] = normalize_text_value(fallback_brand)

    if not summary["brand"]:
        manufacturer = normalize_text_value(_pick_from_labels(pair_map, ["品牌", "厂商"]))
        summary["brand"] = manufacturer

    if pair_map.get("排量(L)") and not summary["displacement"]:
        summary["displacement"] = pair_map.get("排量(L)", "")

    if pair_map.get("排量(mL)") and not summary["displacement"]:
        summary["displacement"] = pair_map.get("排量(mL)", "")

    if pair_map.get("长度(mm)") and not summary["length_mm"]:
        summary["length_mm"] = pair_map.get("长度(mm)", "")
    if pair_map.get("宽度(mm)") and not summary["width_mm"]:
        summary["width_mm"] = pair_map.get("宽度(mm)", "")
    if pair_map.get("高度(mm)") and not summary["height_mm"]:
        summary["height_mm"] = pair_map.get("高度(mm)", "")
    if pair_map.get("轴距(mm)") and not summary["wheelbase_mm"]:
        summary["wheelbase_mm"] = pair_map.get("轴距(mm)", "")
    if pair_map.get("座位数(个)") and not summary["seats"]:
        summary["seats"] = pair_map.get("座位数(个)", "")
    if pair_map.get("车门数(个)") and not summary["doors"]:
        summary["doors"] = pair_map.get("车门数(个)", "")
    if pair_map.get("整备质量(kg)") and not summary["curb_weight_kg"]:
        summary["curb_weight_kg"] = pair_map.get("整备质量(kg)", "")
    if pair_map.get("后备厢容积(L)") and not summary["trunk_volume_l"]:
        summary["trunk_volume_l"] = pair_map.get("后备厢容积(L)", "")
    if pair_map.get("厂商指导价") and not summary["official_price"]:
        summary["official_price"] = pair_map.get("厂商指导价", "")

    _extract_body_size(summary, pair_map, " ".join(pair_map.values()))
    _normalize_numeric_fields(summary)
    return summary


def extract_title_from_html(html: str) -> str:
    """读取 title。"""
    soup = BeautifulSoup(html, "html.parser")
    return normalize_whitespace(soup.title.get_text(" ", strip=True) if soup.title else "")


def strip_html(value: str) -> str:
    """移除 Autohome 参数 JSON 中的高亮 span。"""
    if value is None:
        return ""
    text = str(value)
    if "<" not in text and ">" not in text:
        return normalize_whitespace(text)
    return normalize_whitespace(BeautifulSoup(text, "html.parser").get_text(" ", strip=True))


def looks_like_price(value: str) -> bool:
    """判断一个值是否像万元价格。"""
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", normalize_whitespace(value)))


def _find_matching_brace(text: str, start_index: int) -> int:
    """找到与起始大括号匹配的结束位置。"""
    depth = 0
    in_string = False
    escape = False
    quote_char = ""

    for index in range(start_index, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote_char:
                in_string = False
        else:
            if char in {'"', "'"}:
                in_string = True
                quote_char = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index

    raise ValueError("未找到 config JSON 结束位置")


def _pick_from_labels(pair_map: dict[str, str], aliases: Iterable[str]) -> str:
    for alias in aliases:
        for key, value in pair_map.items():
            if alias in key:
                return value
    return ""


def _pick_from_text(text: str, field: str) -> str:
    for pattern in TEXT_PATTERNS.get(field, []):
        match = pattern.search(text)
        if match:
            return normalize_whitespace(match.group("value"))
    return ""


def _enrich_from_title_and_breadcrumb(summary: dict, title: str, text: str) -> None:
    if not summary["brand"]:
        brand_match = re.search(r"_([^_]+?)_汽车之家", title)
        if brand_match:
            summary["brand"] = normalize_whitespace(brand_match.group(1))

    if not summary["series_name"]:
        series_match = re.search(r"返回\s*([^\n\r]+)", text)
        if series_match:
            summary["series_name"] = normalize_whitespace(series_match.group(1))

    if not summary["model_name"]:
        model_match = re.search(r"(\d{4}款[^\n\r]+?)(?:车型首页|参数配置|价格单)", text)
        if model_match:
            summary["model_name"] = normalize_whitespace(model_match.group(1))

    if not summary["series_name"]:
        series_from_title = re.search(r"〖([^0-9]{1,30}?)(?:\d{4}款)", title)
        if series_from_title:
            summary["series_name"] = normalize_whitespace(series_from_title.group(1))

    if not summary["model_name"] and title:
        cleaned_title = title.replace("参数配置表", "").replace("价格单", "")
        cleaned_title = cleaned_title.replace("〖", "").replace("〗", "")
        summary["model_name"] = normalize_whitespace(cleaned_title.split("_")[0])


def _extract_body_size(summary: dict, pair_map: dict[str, str], text: str) -> None:
    size_value = ""
    for key, value in pair_map.items():
        if "长" in key and "宽" in key and "高" in key:
            size_value = value
            break

    if not size_value:
        size_match = re.search(r"长[\/×xX*]\s*宽[\/×xX*]\s*高[:：]?\s*([0-9.\s/×xX*]+)", text)
        if size_match:
            size_value = size_match.group(1)

    length_mm, width_mm, height_mm = extract_measurements(size_value)
    if length_mm:
        summary["length_mm"] = length_mm
    if width_mm:
        summary["width_mm"] = width_mm
    if height_mm:
        summary["height_mm"] = height_mm


def _normalize_numeric_fields(summary: dict) -> None:
    numeric_fields = [
        "max_power_kw",
        "max_torque_nm",
        "horsepower",
        "motor_power_kw",
        "motor_torque_nm",
        "battery_capacity_kwh",
        "cltc_range_km",
        "wltc_range_km",
        "length_mm",
        "width_mm",
        "height_mm",
        "wheelbase_mm",
        "doors",
        "curb_weight_kg",
        "trunk_volume_l",
    ]

    for field in numeric_fields:
        value = summary.get(field, "")
        if not value:
            continue
        summary[field] = extract_numeric_value(value)

    if summary.get("seats"):
        seat_value = extract_numeric_value(summary["seats"])
        summary["seats"] = seat_value if seat_value != "" else summary["seats"]

    if summary.get("displacement") == "" and summary.get("engine"):
        displacement_match = re.search(r"(\d+(?:\.\d+)?)L", summary["engine"], re.I)
        if displacement_match:
            summary["displacement"] = displacement_match.group(1)

    if summary.get("intake_type") == "" and summary.get("engine"):
        if "涡轮" in summary["engine"] or "T" in summary["engine"]:
            summary["intake_type"] = "涡轮增压"
        elif "自然吸气" in summary["engine"]:
            summary["intake_type"] = "自然吸气"
