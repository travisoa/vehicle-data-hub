"""汽车整车收录范围：车辆序列判定，以及能源 × 批次的收录授权。

不推断 M/N 子类，不依赖 PDF、座位或质量参数。固定清单、近期事件、收录统计和
Website 构库都从这里读取范围规则，不各自保留批次或能源集合。
"""
from __future__ import annotations

import re

COLLECTION_SCOPE = "automotive_complete_v1"

# 能源分组：普通混合动力不属于新能源；「未知」两组都不属于，独立列示。
NEV_ENERGY_TYPES = ("纯电动", "插电式混合动力", "增程式", "燃料电池")
FUEL_ENERGY_TYPES = ("燃油", "混合动力")
UNKNOWN_ENERGY = "未知"
ENERGY_GROUPS = {"新能源": NEV_ENERGY_TYPES, "燃油": FUEL_ENERGY_TYPES}
# 新能源整车收全部历史正式批次；燃油、普通混合动力及动力未知的整车从这一批起按整批收录。
NON_NEV_FIRST_BATCH = 408
NON_NEV_BATCH_LABEL = f"第{NON_NEV_FIRST_BATCH}批及以后"
# 排除词分两类，匹配范围不同：
# - ANY：出现即否定整车身份，对完整名称（含括号附注）匹配。
# - TAIL：只有描述产品自身时才排除，因此仅对剥离括号附注后的主体名称匹配。
#   若对完整名称匹配，结尾锚点会落到括号内容末尾，把「冷藏车（采用载货汽车底盘）」
#   这类整车按括号里的配套说明误排；产品身份只看主体名称。
NON_AUTOMOTIVE_ANY = re.compile(
    r"摩托|三轮|低速汽车|低速货车|自行车|电动两轮车|有轨电车|铁路机车|轨道车|列车|拖拉机|叉车|场地车|非道路"
)
NON_AUTOMOTIVE_TAIL = re.compile(r"挂车[)）]?$")
# 产品名中段的底盘/零部件可描述结构或运输对象，结尾才表明产品本身是零部件。
# “非完整车辆”明确否定整车身份，保留任意位置识别。
INCOMPLETE_ANY = re.compile(r"非完整车辆")
INCOMPLETE_TAIL = re.compile(r"(?:底盘|零部件|上装|车身|车厢|货箱|货厢|总成|装置)[)）]?$")
# 仅列明确的官方类别及已核验别名，不按任意前缀/后缀推断范围。
# 车船税目录第 60 批等使用“插电式混合动力乘用车”，该类别下允许产品名称为空；
# “商用车”跨客车、货车与专用车，仍需读取产品名称，不能映射到单一频道。
AUTOMOTIVE_CATEGORY_ALIASES = {
    "乘用车": "乘用车",
    "插电式混合动力乘用车": "乘用车",
    "客车": "客车",
    "货车": "货车",
    "载货汽车": "货车",
    "专用车": "专用车",
    "专用汽车": "专用车",
}
# GB/T 17350-2024 第 4.4.6.1、4.4.6.2、4.4.3.6 条列为作业类专用汽车，不能只识别『车』字。
SPECIAL_COMPLETE_NAMES = ("汽车起重机", "全地面起重机", "修井机")


def energy_group(energy_type: str | None) -> str | None:
    """归一化能源所属分组：'新能源'、'燃油'；动力未知或无法识别时为 None。"""
    return next((group for group, types in ENERGY_GROUPS.items() if energy_type in types), None)


def non_nev_batch(batch: int | str | None) -> bool:
    """是否为非新能源授权批次（第 408 批及以后的正式批次）；批次缺失或不是数字时不算。"""
    text = str(batch if batch is not None else "").strip()
    return text.isascii() and text.isdecimal() and int(text) >= NON_NEV_FIRST_BATCH


def energy_scope(energy_type: str | None, batch: int | str | None) -> str:
    """能源 × 正式批次的收录授权，energy_type 须为 normalize_energy 的结果。

    - 'nev'：已确认新能源，任何正式批次都在授权内；
    - 'non_nev_batch'：燃油、普通混动或动力未知，位于非新能源授权批次；
    - 'needs_nev_evidence'：动力未知且不在授权批次，须另有新能源证据才能收录；
    - 'outside'：已确认燃油或普通混动，且不在授权批次，不收录。
    批次缺失或不是数字（如没有批次的占位行）时按不在授权批次处理。
    """
    group = energy_group(energy_type)
    if group == "新能源":
        return "nev"
    if non_nev_batch(batch):
        return "non_nev_batch"
    return "outside" if group == "燃油" else "needs_nev_evidence"


def normalize_catalog_category(category: str | None) -> str | None:
    """将明确的官方目录类别归一为四大类，未知/混合类别不作推断。"""
    return AUTOMOTIVE_CATEGORY_ALIASES.get((category or "").strip())


def _normalized_names(product_name: str) -> tuple[str, str]:
    """返回（去空白的完整名称, 再剥离括号附注后的主体名称）。"""
    name = re.sub(r"\s+", "", product_name or "")
    return name, re.sub(r"[（(][^()（）]*[)）]", "", name)


def is_non_automotive(product_name: str) -> bool:
    """独立序列排除；半挂牵引车、挂车运输车不是挂车产品本身。"""
    name, base_name = _normalized_names(product_name)
    return bool(NON_AUTOMOTIVE_ANY.search(name) or NON_AUTOMOTIVE_TAIL.search(base_name))


def classify_vehicle(model_code: str, product_name: str, fields: dict | None = None) -> dict[str, str]:
    """只返回范围状态和原因。fields 的 category 必须来自官方目录，不能传型号推断值。

    名称明确为汽车整车即收录，不要求质量、座位或 PDF；未知名称保留内部待核验。
    dataTag=D 可排除独立底盘，但 dataTag=Z 本身不能证明是汽车（摩托车也可能为 Z）。
    整车参数里的 chassis/chassis_references 是配套底盘信息，不是该产品为底盘的证据；
    名称括号里的配套底盘/挂车说明同理，只看主体名称判定产品身份。
    """
    fields = fields or {}
    name, base_name = _normalized_names(product_name)
    if is_non_automotive(name):
        gate, reason = "excluded", "non_automotive"
    elif (INCOMPLETE_ANY.search(name) or INCOMPLETE_TAIL.search(base_name)
          or str(fields.get("dataTag", "")).upper() == "D"):
        gate, reason = "excluded", "incomplete_vehicle_or_component"
    elif base_name.endswith("车") or base_name.endswith(SPECIAL_COMPLETE_NAMES):
        gate, reason = "accepted", "official_complete_vehicle_name"
    elif normalize_catalog_category(fields.get("category")):
        gate, reason = "accepted", "official_catalog_category"
    else:
        gate, reason = "pending_review", "insufficient_scope_evidence"
    return {"inclusion_gate": gate, "scope_reason": reason}
