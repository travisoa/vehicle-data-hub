"""汽车整车收录范围：不推断 M/N 子类，不依赖 PDF、座位或质量参数。"""
from __future__ import annotations

import re

COLLECTION_SCOPE = "automotive_complete_v1"
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
