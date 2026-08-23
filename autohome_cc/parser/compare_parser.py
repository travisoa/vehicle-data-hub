"""抓包文件/在线配置页的车型对比解析。"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from autohome_cc.config import CAPTURE_NOISE_KEYWORDS, IGNORED_PARAM_KEYWORDS, PREFERRED_SECTION_ORDER, SUMMARY_SECTION_FIELDS
from autohome_cc.parser.spec_parser import FIELD_ID_TO_LABEL, extract_page_data_bundle, score_spec_candidate, strip_html
from autohome_cc.utils.cleaners import clean_compare_value, normalize_whitespace, parse_cookie_file, parse_cookie_string, tokenize_model_name

LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
SPEC_RE = re.compile(r"/spec/(\d+)/|spec-(\d+)-")
MODEL_COUNT_RE = re.compile(r"共\s*\d+\s*款车型")
SECTION_NAMES = set(PREFERRED_SECTION_ORDER)


@dataclass
class CompareModel:
    """单个车型列信息。"""

    spec_id: str
    name: str
    guide_price: str = "-"
    dealer_price: str = "-"
    source: str = ""
    display_name: str = ""


@dataclass
class CompareDataset:
    """一份可导出为对比表的数据集。"""

    source_label: str
    source_type: str
    models: list[CompareModel]
    scalar_rows: list[dict[str, object]] = field(default_factory=list)
    color_rows: list[dict[str, str]] = field(default_factory=list)
    option_rows: list[dict[str, str]] = field(default_factory=list)
    raw_rows: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def parse_capture_file(
    source_path: str | Path,
    requested_models: list[str] | None = None,
) -> CompareDataset:
    """按文件内容自动识别抓包格式。"""
    file_path = Path(source_path).expanduser().resolve()
    text = file_path.read_text(encoding="utf-8")
    return parse_capture_text(
        text,
        source_label=str(file_path),
        requested_models=requested_models,
        source_type="capture_file",
    )


def parse_capture_text(
    text: str,
    *,
    source_label: str,
    requested_models: list[str] | None = None,
    source_type: str = "capture_text",
) -> CompareDataset:
    """自动识别 HTML config 或 markdown/text 抓包。"""
    if re.search(r"\bvar\s+config\s*=", text):
        return _parse_config_capture(text, source_label, requested_models, source_type)

    if MODEL_COUNT_RE.search(text) and "厂商指导价" in text:
        return _parse_markdown_capture(text, source_label, requested_models, source_type)

    raise ValueError("未识别到支持的抓包格式；请提供包含 `var config =` 的 HTML，或包含车型/参数表的文本抓包文件。")


def build_dataset_from_summary(summary: dict, raw: dict, *, source_label: str, source_type: str) -> CompareDataset:
    """把 summary fallback 成一个单车型的对比数据集。"""
    model_name = summary.get("model_name") or summary.get("input_name") or "未命名车型"
    model = CompareModel(
        spec_id="",
        name=model_name,
        guide_price=clean_compare_value(summary.get("official_price", "")),
        dealer_price="-",
        source=source_label,
    )
    scalar_rows: list[dict[str, object]] = []
    for section, fields in SUMMARY_SECTION_FIELDS.items():
        for key, label in fields:
            scalar_rows.append(
                {
                    "section": section,
                    "param": label,
                    "values": [clean_compare_value(str(summary.get(key, "")))],
                }
            )

    dataset = CompareDataset(
        source_label=source_label,
        source_type=source_type,
        models=[model],
        scalar_rows=scalar_rows,
        raw_rows=[{"source_type": source_type, "source_label": source_label, **raw}],
        notes=["页面未能解析为完整配置表，已回退为 summary 字段对比。"],
    )
    _finalize_models(dataset.models)
    return dataset


def parse_param_conf_api_data(
    payload: dict,
    *,
    source_label: str,
    requested_models: list[str] | None = None,
    source_type: str = "param_conf_api",
) -> CompareDataset:
    """解析 `getParamConf` 返回的干净 JSON。"""
    result = payload.get("result") or {}
    bread = result.get("bread") or {}
    series_name = normalize_whitespace(str(bread.get("seriesname", "")))
    title_groups = result.get("titlelist") or []
    data_rows = result.get("datalist") or []
    if not title_groups or not data_rows:
        raise ValueError("getParamConf 未返回有效 titlelist/datalist")

    title_meta: OrderedDict[int, tuple[str, str]] = OrderedDict()
    for group in title_groups:
        section = normalize_whitespace(str(group.get("itemtype", ""))) or "未分类"
        if _should_ignore_group(section):
            continue
        for item in group.get("items", []):
            title_id = int(item.get("titleid", -1))
            if title_id < 0:
                continue
            param = normalize_whitespace(str(item.get("itemname", "")))
            if _should_ignore_param(param):
                continue
            title_meta[title_id] = (section, param)

    all_models: list[CompareModel] = []
    spec_row_maps: dict[str, dict[int, dict]] = {}
    for row in data_rows:
        spec_id = str(row.get("specid", "")).strip()
        spec_name = normalize_whitespace(str(row.get("specname", "")))
        if not spec_id or not spec_name:
            continue
        model_name = normalize_whitespace(f"{series_name} {spec_name}") if series_name else spec_name
        all_models.append(
            CompareModel(
                spec_id=spec_id,
                name=model_name,
                guide_price=clean_compare_value(str(row.get("minprice", ""))),
                dealer_price=clean_compare_value(str(row.get("dealerprice", ""))),
                source=source_label,
            )
        )
        spec_row_maps[spec_id] = {
            int(item.get("titleid", -1)): item
            for item in row.get("paramconflist", [])
            if int(item.get("titleid", -1)) >= 0
        }

    selected_models, notes = _select_models(all_models, requested_models)
    selected_spec_ids = [model.spec_id for model in selected_models]

    scalar_rows: list[dict[str, object]] = []
    color_rows: list[dict[str, str]] = []
    option_rows: list[dict[str, str]] = []
    scalar_skip_titles = {1, 2, 3}

    for title_id, (section, param) in title_meta.items():
        if section == "颜色":
            for model in selected_models:
                item = spec_row_maps.get(model.spec_id, {}).get(title_id, {})
                color_rows.append(
                    {
                        "category": param or "颜色",
                        "spec_id": model.spec_id,
                        "model": model.display_name or model.name,
                        "content": _format_api_color_item(item),
                    }
                )
            continue

        if title_id in scalar_skip_titles:
            continue

        scalar_rows.append(
            {
                "section": section,
                "param": param,
                "values": [
                    _format_api_param_item(spec_row_maps.get(spec_id, {}).get(title_id, {}))
                    for spec_id in selected_spec_ids
                ],
            }
        )

    scalar_rows.sort(key=_row_sort_key)
    dataset = CompareDataset(
        source_label=source_label,
        source_type=source_type,
        models=selected_models,
        scalar_rows=scalar_rows,
        color_rows=color_rows,
        option_rows=option_rows,
        raw_rows=[
            {
                "source_type": source_type,
                "source_label": source_label,
                "requested_models": ", ".join(requested_models or []),
                "selected_models": ", ".join(model.display_name or model.name for model in selected_models),
            }
        ],
        notes=notes,
    )
    return dataset


def merge_compare_datasets(datasets: list[CompareDataset]) -> CompareDataset:
    """合并多份数据集，生成一个总对比表。"""
    if not datasets:
        raise ValueError("没有可合并的数据集")

    merged_models: list[CompareModel] = []
    for dataset in datasets:
        merged_models.extend(dataset.models)
    _finalize_models(merged_models)

    total_columns = len(merged_models)
    row_map: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
    color_rows: list[dict[str, str]] = []
    option_rows: list[dict[str, str]] = []
    raw_rows: list[dict[str, str]] = []
    notes: list[str] = []

    offset = 0
    spec_name_map = {model.spec_id: model.display_name or model.name for model in merged_models if model.spec_id}
    for dataset in datasets:
        for row in dataset.scalar_rows:
            key = (str(row["section"]), str(row["param"]))
            if key not in row_map:
                row_map[key] = ["-"] * total_columns
            for index, value in enumerate(row["values"]):
                row_map[key][offset + index] = clean_compare_value(str(value))

        for row in dataset.color_rows:
            copied = dict(row)
            if copied.get("spec_id") in spec_name_map:
                copied["model"] = spec_name_map[copied["spec_id"]]
            color_rows.append(copied)

        for row in dataset.option_rows:
            copied = dict(row)
            if copied.get("spec_id") in spec_name_map:
                copied["model"] = spec_name_map[copied["spec_id"]]
            option_rows.append(copied)

        raw_rows.extend(dataset.raw_rows)
        notes.extend(dataset.notes)
        offset += len(dataset.models)

    scalar_rows = [
        {"section": section, "param": param, "values": values}
        for (section, param), values in row_map.items()
    ]
    scalar_rows.sort(key=_row_sort_key)

    merged = CompareDataset(
        source_label="merged",
        source_type="merged",
        models=merged_models,
        scalar_rows=scalar_rows,
        color_rows=color_rows,
        option_rows=option_rows,
        raw_rows=raw_rows,
        notes=notes,
    )
    return merged


def load_cookie_mapping(cookie_string: str = "", cookie_file: str = "") -> dict[str, str]:
    """供主流程统一加载 cookies。"""
    cookies: dict[str, str] = {}
    if cookie_string:
        cookies.update(parse_cookie_string(cookie_string))
    if cookie_file:
        cookies.update(parse_cookie_file(cookie_file))
    return cookies


def _parse_config_capture(
    text: str,
    source_label: str,
    requested_models: list[str] | None,
    source_type: str,
) -> CompareDataset:
    bundle = extract_page_data_bundle(text)
    config_data = bundle["config"]
    option_data = bundle.get("option") or {}
    bag_data = bundle.get("bag") or {}
    color_data = bundle.get("color") or {}
    inner_color_data = bundle.get("innerColor") or {}

    result = config_data.get("result") or {}
    paramtypeitems = result.get("paramtypeitems") or []
    all_models = _extract_models_from_config(paramtypeitems, source_label)
    selected_models, notes = _select_models(all_models, requested_models)
    selected_spec_ids = [model.spec_id for model in selected_models]

    scalar_rows: list[dict[str, object]] = []
    color_rows: list[dict[str, str]] = []
    option_rows: list[dict[str, str]] = []

    for group in paramtypeitems:
        group_name = normalize_whitespace(strip_html(group.get("name", ""))) or "未分类"
        if _should_ignore_group(group_name):
            continue

        for item_index, item in enumerate(group.get("paramitems", []), start=1):
            label = normalize_whitespace(strip_html(item.get("name", "")))
            item_id = int(item.get("id", -99999))
            label = FIELD_ID_TO_LABEL.get(item_id, label)
            if group_name == "基本参数" and item_id == -1:
                if item_index == 1:
                    label = "车型"
                elif item_index == 2:
                    label = "厂商指导价"
                elif item_index == 3:
                    label = "厂商"
            if _should_ignore_param(label):
                continue

            values_by_spec = _value_map_from_items(item.get("valueitems", []))

            if label in {"车型", "厂商指导价", "经销商报价"}:
                continue

            scalar_rows.append(
                {
                    "section": group_name,
                    "param": label,
                    "values": [values_by_spec.get(spec_id, "-") for spec_id in selected_spec_ids],
                }
            )

    for group in ((option_data.get("result") or {}).get("configtypeitems") or []):
        group_name = normalize_whitespace(strip_html(group.get("name", ""))) or "配置"
        if _should_ignore_group(group_name):
            continue
        for item in group.get("configitems", []):
            label = normalize_whitespace(strip_html(item.get("name", "")))
            if _should_ignore_param(label):
                continue
            values_by_spec = _value_map_from_items(item.get("valueitems", []))
            scalar_rows.append(
                {
                    "section": group_name,
                    "param": label,
                    "values": [values_by_spec.get(spec_id, "-") for spec_id in selected_spec_ids],
                }
            )

    color_rows.extend(_build_color_rows(selected_models, color_data, "外观颜色"))
    color_rows.extend(_build_color_rows(selected_models, inner_color_data, "内饰颜色"))
    option_rows.extend(_build_bag_rows(selected_models, bag_data))

    scalar_rows.sort(key=_row_sort_key)
    dataset = CompareDataset(
        source_label=source_label,
        source_type=source_type,
        models=selected_models,
        scalar_rows=scalar_rows,
        color_rows=color_rows,
        option_rows=option_rows,
        raw_rows=[
            {
                "source_type": source_type,
                "source_label": source_label,
                "requested_models": ", ".join(requested_models or []),
                "selected_models": ", ".join(model.display_name or model.name for model in selected_models),
            }
        ],
        notes=notes,
    )
    return dataset


def _parse_markdown_capture(
    text: str,
    source_label: str,
    requested_models: list[str] | None,
    source_type: str,
) -> CompareDataset:
    lines = [line.rstrip("\n") for line in text.splitlines()]
    all_models = _extract_models_from_markdown(lines, source_label)
    _fill_markdown_prices(lines, all_models)
    selected_models, notes = _select_models(all_models, requested_models)
    selected_indexes = [_find_model_index(all_models, model.spec_id) for model in selected_models]

    scalar_rows = _parse_markdown_scalar_rows(lines, len(all_models), selected_indexes)
    color_rows = _parse_markdown_color_rows(lines, all_models, selected_indexes)
    option_rows = _parse_markdown_option_rows(lines, all_models, selected_indexes)

    dataset = CompareDataset(
        source_label=source_label,
        source_type=source_type,
        models=selected_models,
        scalar_rows=scalar_rows,
        color_rows=color_rows,
        option_rows=option_rows,
        raw_rows=[
            {
                "source_type": source_type,
                "source_label": source_label,
                "requested_models": ", ".join(requested_models or []),
                "selected_models": ", ".join(model.display_name or model.name for model in selected_models),
            }
        ],
        notes=notes,
    )
    return dataset


def _extract_models_from_config(paramtypeitems: list[dict], source_label: str) -> list[CompareModel]:
    model_item = _find_param_item(paramtypeitems, "车型")
    price_item = _find_param_item(paramtypeitems, "厂商指导价")
    dealer_item = _find_param_item(paramtypeitems, "经销商报价")

    price_map = _value_map(price_item)
    dealer_map = _value_map(dealer_item)
    models: list[CompareModel] = []
    for value_item in (model_item or {}).get("valueitems", []):
        spec_id = str(value_item.get("specid", "")).strip()
        if not spec_id:
            continue
        name = clean_compare_value(strip_html(value_item.get("value", "")))
        models.append(
            CompareModel(
                spec_id=spec_id,
                name=name,
                guide_price=price_map.get(spec_id, "-"),
                dealer_price=dealer_map.get(spec_id, "-"),
                source=source_label,
            )
        )
    _finalize_models(models)
    return models


def _extract_models_from_markdown(lines: list[str], source_label: str) -> list[CompareModel]:
    start = _find_line_regex(lines, MODEL_COUNT_RE)
    end = _find_line_contains(lines, "厂商指导价", start)
    models: list[CompareModel] = []
    for raw in lines[start:end]:
        match = LINK_RE.search(raw)
        if not match:
            continue
        text_value, url = match.groups()
        spec_id = _extract_spec_id(url)
        if not spec_id:
            continue
        models.append(
            CompareModel(
                spec_id=spec_id,
                name=normalize_whitespace(text_value),
                source=source_label,
            )
        )
    _finalize_models(models)
    return models


def _fill_markdown_prices(lines: list[str], models: list[CompareModel]) -> None:
    guide_start = _find_line_contains(lines, "厂商指导价") + 1
    dealer_start = _find_line(lines, "经销商报价", guide_start)
    guide_prices: list[str] = []
    for raw in lines[guide_start:dealer_start]:
        value = clean_compare_value(_clean_text(raw))
        if value != "-":
            guide_prices.append(value)
        if len(guide_prices) == len(models):
            break

    dealer_prices: list[str] = []
    basic_start = _find_line(lines, "基本参数", dealer_start)
    for raw in lines[dealer_start + 1 : basic_start]:
        value = clean_compare_value(_clean_text(raw))
        if value != "-":
            dealer_prices.append(value)
        if len(dealer_prices) == len(models):
            break

    for index, model in enumerate(models):
        if index < len(guide_prices):
            model.guide_price = guide_prices[index]
        if index < len(dealer_prices):
            model.dealer_price = dealer_prices[index]


def _parse_markdown_scalar_rows(
    lines: list[str],
    model_count: int,
    selected_indexes: list[int],
) -> list[dict[str, object]]:
    start = _find_line(lines, "基本参数", _find_line(lines, "经销商报价"))
    end = _find_optional_line(lines, "颜色", start) or _find_optional_line(lines, "选装包", start) or len(lines)
    rows: list[dict[str, object]] = []
    section = "基本参数"
    index = start + 1

    while index < end:
        if not _clean_text(lines[index]):
            index += 1
            continue
        if _is_section_heading(lines, index):
            section = _clean_text(lines[index])
            index += 1
            continue

        param = _clean_text(lines[index])
        values_raw = lines[index + 1 : index + 1 + model_count]
        if len(values_raw) < model_count:
            break
        if not _should_ignore_param(param):
            values = [clean_compare_value(_clean_text(value)) for value in values_raw]
            rows.append(
                {
                    "section": section,
                    "param": param,
                    "values": [values[selected_index] for selected_index in selected_indexes],
                }
            )
        index += model_count + 1

    rows.sort(key=_row_sort_key)
    return rows


def _parse_markdown_color_rows(
    lines: list[str],
    all_models: list[CompareModel],
    selected_indexes: list[int],
) -> list[dict[str, str]]:
    content_start = _find_line(lines, "基本参数", _find_line(lines, "经销商报价"))
    color_start = _find_optional_line(lines, "颜色", content_start)
    if color_start is None:
        return []
    option_start = _find_optional_line(lines, "选装包", color_start) or len(lines)
    block = lines[color_start + 1 : option_start]
    subsection_positions: list[tuple[int, str]] = []

    for idx, raw in enumerate(block):
        label = _clean_text(raw)
        if label in {"外观颜色", "内饰颜色"}:
            subsection_positions.append((idx, label))

    subsection_positions.append((len(block), "__END__"))
    rows: list[dict[str, str]] = []
    for position in range(len(subsection_positions) - 1):
        start_idx, label = subsection_positions[position]
        end_idx, _ = subsection_positions[position + 1]
        tokens = [line for line in block[start_idx + 1 : end_idx] if _clean_text(line)]
        grouped = _parse_multivalue_subsection(tokens, all_models)
        for selected_index in selected_indexes:
            model = all_models[selected_index]
            content = [
                item
                for item in grouped.get(model.spec_id, [])
                if item and item not in {model.name, model.display_name}
            ]
            rows.append(
                {
                    "category": label,
                    "spec_id": model.spec_id,
                    "model": model.display_name or model.name,
                    "content": "\n".join(content) or "-",
                }
            )
    return rows


def _parse_markdown_option_rows(
    lines: list[str],
    all_models: list[CompareModel],
    selected_indexes: list[int],
) -> list[dict[str, str]]:
    content_start = _find_line(lines, "基本参数", _find_line(lines, "经销商报价"))
    option_start = _find_optional_line(lines, "选装包", content_start)
    if option_start is None:
        return []
    footer_start = _find_line_contains(lines, "关于我们", option_start)
    tokens = [line for line in lines[option_start + 2 : footer_start] if _clean_text(line)]
    rows: list[dict[str, str]] = []
    index = 0
    while index < len(tokens):
        package_name = _clean_text(tokens[index])
        index += 1
        if not package_name:
            continue
        entries: list[str] = []
        for model_offset, _ in enumerate(all_models):
            if index >= len(tokens):
                break
            parts = [_clean_text(tokens[index])]
            index += 1
            remaining_models = len(all_models) - model_offset - 1
            while (
                index < len(tokens)
                and _is_package_continuation(tokens[index])
                and len(tokens) - index > remaining_models
            ):
                parts.append(_clean_text(tokens[index]))
                index += 1
            entries.append("\n".join(part for part in parts if part))
        for selected_index in selected_indexes:
            model = all_models[selected_index]
            entry = entries[selected_index] if selected_index < len(entries) else "-"
            rows.append(
                {
                    "package": package_name,
                    "spec_id": model.spec_id,
                    "model": model.display_name or model.name,
                    "content": entry or "-",
                }
            )
    return rows


def _parse_multivalue_subsection(tokens: list[str], models: list[CompareModel]) -> dict[str, list[str]]:
    token_specs = [_extract_spec_id(token) for token in tokens]
    spec_to_index = {model.spec_id: index for index, model in enumerate(models)}
    anchor_positions: list[tuple[int, int]] = []
    last_model_index: int | None = None

    for position, spec_id in enumerate(token_specs):
        if spec_id is None:
            continue
        model_index = spec_to_index.get(spec_id)
        if model_index is None:
            continue
        if model_index != last_model_index:
            anchor_positions.append((position, model_index))
            last_model_index = model_index

    if not anchor_positions:
        total = len(tokens)
        if total % len(models) != 0:
            raise ValueError("颜色分组无法均分到车型列")
        chunk = total // len(models)
        return {
            model.spec_id: [clean_compare_value(_clean_text(token)) for token in tokens[index * chunk : (index + 1) * chunk]]
            for index, model in enumerate(models)
        }

    groups = {model.spec_id: [] for model in models}
    for anchor_index, (start_pos, model_index) in enumerate(anchor_positions):
        next_pos = anchor_positions[anchor_index + 1][0] if anchor_index + 1 < len(anchor_positions) else len(tokens)
        next_model_index = anchor_positions[anchor_index + 1][1] if anchor_index + 1 < len(anchor_positions) else len(models)
        region = tokens[start_pos:next_pos]
        covered_models = next_model_index - model_index
        if covered_models <= 0:
            continue

        anchor_run_length = 0
        for token in region:
            if _extract_spec_id(token) == models[model_index].spec_id:
                anchor_run_length += 1
            else:
                break

        if covered_models == 1:
            groups[models[model_index].spec_id].extend(clean_compare_value(_clean_text(token)) for token in region)
            continue

        best_first_length: int | None = None
        best_score: tuple[float, int] | None = None
        average = len(region) / covered_models
        for first_length in range(anchor_run_length, len(region) - (covered_models - 1) + 1):
            remainder = len(region) - first_length
            if remainder % (covered_models - 1) != 0:
                continue
            chunk = remainder // (covered_models - 1)
            if chunk < 1:
                continue
            score = (abs(first_length - average), first_length)
            if best_score is None or score < best_score:
                best_score = score
                best_first_length = first_length

        if best_first_length is None:
            raise ValueError("颜色分段无法推断车型边界")

        groups[models[model_index].spec_id].extend(
            clean_compare_value(_clean_text(token)) for token in region[:best_first_length]
        )
        remainder = region[best_first_length:]
        chunk = len(remainder) // (covered_models - 1)
        for offset in range(covered_models - 1):
            target_model = models[model_index + 1 + offset]
            start = offset * chunk
            end = start + chunk
            groups[target_model.spec_id].extend(
                clean_compare_value(_clean_text(token)) for token in remainder[start:end]
            )

    first_anchor_pos, first_anchor_model_index = anchor_positions[0]
    if first_anchor_pos > 0:
        leading = [clean_compare_value(_clean_text(token)) for token in tokens[:first_anchor_pos]]
        groups[models[first_anchor_model_index].spec_id] = leading + groups[models[first_anchor_model_index].spec_id]

    return groups


def _select_models(
    all_models: list[CompareModel],
    requested_models: list[str] | None,
) -> tuple[list[CompareModel], list[str]]:
    if not all_models:
        raise ValueError("未解析出任何车型列")

    if not requested_models:
        return all_models, []

    selected: list[CompareModel] = []
    used_indexes: set[int] = set()
    notes: list[str] = []

    for requested in requested_models:
        best_index = None
        best_score = -10_000
        best_match_count = 0
        for index, model in enumerate(all_models):
            if index in used_indexes:
                continue
            score, match_count = _score_requested_model(requested, model)
            if score > best_score:
                best_score = score
                best_index = index
                best_match_count = match_count

        if best_index is None or best_match_count == 0:
            notes.append(f"未在抓包中找到与 `{requested}` 明确匹配的车型，已跳过。")
            continue

        used_indexes.add(best_index)
        selected.append(all_models[best_index])
        if normalize_whitespace(requested) != normalize_whitespace(all_models[best_index].name):
            notes.append(f"`{requested}` 已匹配为 `{all_models[best_index].name}`。")

    if not selected:
        raise ValueError("请求的车型均未能在抓包中匹配到")

    _finalize_models(selected)
    return selected, notes


def _score_requested_model(requested: str, model: CompareModel) -> tuple[int, int]:
    query = normalize_whitespace(requested).lower()
    candidate = normalize_whitespace(model.name).lower()
    if not query or not candidate:
        return -10_000, 0

    if requested.isdigit() and requested == model.spec_id:
        return 50_000, 10

    tokens = [token for token in tokenize_model_name(requested) if token]
    matched_tokens = sum(1 for token in tokens if token and token in candidate)
    score = score_spec_candidate(requested, model.name, 1)
    if query == candidate:
        score += 5_000
    elif query in candidate:
        score += 1_000
    score += matched_tokens * 200
    if matched_tokens == 0 and query not in candidate:
        score -= 10_000
    return score, matched_tokens


def _find_param_item(paramtypeitems: list[dict], label_keyword: str) -> dict | None:
    for group in paramtypeitems:
        for item in group.get("paramitems", []):
            label = normalize_whitespace(strip_html(item.get("name", "")))
            if label_keyword in label:
                return item
    return None


def _value_map(item: dict | None) -> dict[str, str]:
    if not item:
        return {}
    return _value_map_from_items(item.get("valueitems", []))


def _value_map_from_items(valueitems: list[dict]) -> dict[str, str]:
    return {
        str(value_item.get("specid", "")): _format_value_item(value_item)
        for value_item in valueitems
        if str(value_item.get("specid", "")).strip()
    }


def _format_value_item(value_item: dict) -> str:
    base_value = clean_compare_value(strip_html(value_item.get("value", "")))
    sub_entries = []
    for sub_item in value_item.get("sublist", []):
        sub_name = normalize_whitespace(strip_html(sub_item.get("subname", "")))
        sub_value = clean_compare_value(strip_html(str(sub_item.get("subvalue", ""))))
        if sub_name and sub_value not in {"", "-", "1"}:
            sub_entries.append(f"{sub_name}:{sub_value}")
        elif sub_name:
            sub_entries.append(sub_name)
        elif sub_value not in {"", "-", "1"}:
            sub_entries.append(sub_value)

    parts = []
    if base_value != "-":
        parts.append(base_value)
    if sub_entries:
        parts.append(" / ".join(sub_entries))
    return "\n".join(parts) if parts else "-"


def _build_color_rows(models: list[CompareModel], payload: dict, category: str) -> list[dict[str, str]]:
    specitems = ((payload.get("result") or {}).get("specitems")) or []
    color_map: dict[str, str] = {}
    for spec_item in specitems:
        spec_id = str(spec_item.get("specid", "")).strip()
        if not spec_id:
            continue
        entries = []
        for color_item in spec_item.get("coloritems", []):
            name = normalize_whitespace(strip_html(color_item.get("name", "")))
            if not name:
                continue
            price = color_item.get("price", 0)
            if isinstance(price, (int, float)) and price > 0:
                entries.append(f"{name}(+{int(price)})")
            else:
                entries.append(name)
        color_map[spec_id] = "\n".join(entries) if entries else "-"

    return [
        {
            "category": category,
            "spec_id": model.spec_id,
            "model": model.display_name or model.name,
            "content": color_map.get(model.spec_id, "-"),
        }
        for model in models
    ]


def _build_bag_rows(models: list[CompareModel], payload: dict) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    result = payload.get("result") or {}
    for group in result.get("bagtypeitems", []):
        default_package = normalize_whitespace(strip_html(group.get("name", ""))) or "选装包"
        for spec_bag in group.get("bagitems", []):
            spec_id = str(spec_bag.get("specid", "")).strip()
            if not spec_id:
                continue
            model = next((item for item in models if item.spec_id == spec_id), None)
            if model is None:
                continue
            valueitems = spec_bag.get("valueitems", [])
            if not valueitems:
                rows.append(
                    {
                        "package": default_package,
                        "spec_id": model.spec_id,
                        "model": model.display_name or model.name,
                        "content": "-",
                    }
                )
                continue
            for value_item in valueitems:
                package_name = normalize_whitespace(strip_html(value_item.get("name", ""))) or default_package
                rows.append(
                    {
                        "package": package_name,
                        "spec_id": model.spec_id,
                        "model": model.display_name or model.name,
                        "content": _format_value_item(value_item),
                    }
                )
    return rows


def _format_api_param_item(item: dict) -> str:
    if not item:
        return "-"

    item_name = clean_compare_value(str(item.get("itemname", "")))
    sublist = item.get("sublist") or []
    details: list[str] = []
    for sub_item in sublist:
        name = clean_compare_value(str(sub_item.get("name", "")))
        value = clean_compare_value(str(sub_item.get("value", "")))
        priceinfo = clean_compare_value(str(sub_item.get("priceinfo", "")))
        if priceinfo == "None":
            priceinfo = "-"
        part = name if name != "-" else ""
        if value not in {"", "-", "●"}:
            part = f"{part}:{value}" if part else value
        elif value == "●" and part:
            part = part
        if priceinfo not in {"", "-"}:
            part = f"{part}({priceinfo})" if part else priceinfo
        if part:
            details.append(part)

    if item_name not in {"", "-"} and details:
        return f"{item_name}\n" + "\n".join(details)
    if details:
        return "\n".join(details)
    return item_name


def _format_api_color_item(item: dict) -> str:
    colorinfo = item.get("colorinfo") or {}
    entries = []
    for color in colorinfo.get("list", []):
        name = clean_compare_value(str(color.get("name", "")))
        if name in {"", "-"}:
            continue
        price = color.get("price", 0)
        if isinstance(price, (int, float)) and price > 0:
            entries.append(f"{name}(+{int(price)})")
        else:
            entries.append(name)
    return "\n".join(entries) if entries else "-"


def _finalize_models(models: list[CompareModel]) -> None:
    seen: dict[str, int] = {}
    for model in models:
        base_name = model.name or model.spec_id or "未命名车型"
        count = seen.get(base_name, 0) + 1
        seen[base_name] = count
        model.display_name = base_name if count == 1 else f"{base_name} ({count})"


def _row_sort_key(row: dict[str, object]) -> tuple[int, str, str]:
    section = str(row["section"])
    try:
        section_index = PREFERRED_SECTION_ORDER.index(section)
    except ValueError:
        section_index = len(PREFERRED_SECTION_ORDER) + 1
    return section_index, section, str(row["param"])


def _should_ignore_group(group_name: str) -> bool:
    return any(keyword in group_name for keyword in CAPTURE_NOISE_KEYWORDS)


def _should_ignore_param(label: str) -> bool:
    if not label:
        return True
    return any(keyword in label for keyword in IGNORED_PARAM_KEYWORDS)


def _find_model_index(models: list[CompareModel], spec_id: str) -> int:
    for index, model in enumerate(models):
        if model.spec_id == spec_id:
            return index
    raise ValueError(f"未找到 spec_id={spec_id}")


def _extract_spec_id(text: str) -> str | None:
    match = SPEC_RE.search(text)
    if not match:
        return None
    return match.group(1) or match.group(2)


def _normalize_line(line: str) -> str:
    return line.rstrip("\n").replace("\u00a0", " ").strip()


def _clean_text(text: str) -> str:
    cleaned = _normalize_line(text)
    cleaned = LINK_RE.sub(r"\1", cleaned)
    cleaned = cleaned.replace("__", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for keyword in CAPTURE_NOISE_KEYWORDS:
        cleaned = cleaned.replace(keyword, "")
    return normalize_whitespace(cleaned)


def _find_line(lines: list[str], target: str, start: int = 0) -> int:
    for index in range(start, len(lines)):
        if _clean_text(lines[index]) == target:
            return index
    raise ValueError(f"未找到行: {target}")


def _find_optional_line(lines: list[str], target: str, start: int = 0) -> int | None:
    for index in range(start, len(lines)):
        if _clean_text(lines[index]) == target:
            return index
    return None


def _find_line_contains(lines: list[str], target: str, start: int = 0) -> int:
    for index in range(start, len(lines)):
        if target in _clean_text(lines[index]):
            return index
    raise ValueError(f"未找到包含文本: {target}")


def _find_line_regex(lines: list[str], pattern: re.Pattern[str], start: int = 0) -> int:
    for index in range(start, len(lines)):
        if pattern.search(_clean_text(lines[index])):
            return index
    raise ValueError(f"未找到匹配模式: {pattern.pattern}")


def _is_parameter_link(line: str) -> bool:
    normalized = _normalize_line(line)
    return normalized.startswith("[") and "](" in normalized and "/spec/" not in normalized


def _is_section_heading(lines: list[str], index: int) -> bool:
    current = _clean_text(lines[index])
    if current not in SECTION_NAMES:
        return False
    if index + 1 >= len(lines):
        return False
    next_line = lines[index + 1]
    next_clean = _clean_text(next_line)
    if current == "颜色":
        return next_clean in {"外观颜色", "内饰颜色"}
    if current == "选装包":
        return next_clean.startswith("标配") or next_clean.startswith("选配") or "无" in next_clean
    return _is_parameter_link(next_line)


def _is_package_continuation(raw: str) -> bool:
    stripped = _normalize_line(raw)
    cleaned = _clean_text(raw)
    return stripped.startswith("__") or cleaned.startswith("选配") or cleaned.startswith("标配")
