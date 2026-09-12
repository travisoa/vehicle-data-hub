"""公告能源归一和整车业务频道映射，两项目共用。"""
from __future__ import annotations
import re
from .vehicle_classification import normalize_catalog_category

# ---------------------------------------------------------------- 频道归类

_CODE_RE = re.compile(r"^[A-Za-z]+(\d)")

CHANNEL_LABELS = {
    "passenger": "乘用车",
    # 业务库历史值保持「货车」以兼容 catalog_category；API 配置对外显示「载货汽车」。
    "truck": "货车",
    "bus": "客车",
    "special": "专用车",
}
CHANNEL_BY_LABEL = {label: channel for channel, label in CHANNEL_LABELS.items()}


def derive_channel(model_code: str, product_name: str, *, catalog_category: str = ""
                   ) -> tuple[str, str | None]:
    """为已通过整车范围判断的产品映射频道，返回 (channel, 类别码)。

    范围由调用方携带完整证据统一判断，此处不重复推断。若范围依赖官方目录类别
    才能确认，调用方通过 catalog_category 传入该类别；其他情况按名称与型号映射。
    """
    match = _CODE_RE.match(model_code or "")
    code = match.group(1) if match else None
    catalog_channel = CHANNEL_BY_LABEL.get(normalize_catalog_category(catalog_category))
    if catalog_channel:
        return catalog_channel, code
    if code == "5":
        return "special", code
    if any(term in product_name for term in ("乘用车", "轿车")):
        return "passenger", code
    if "客车" in product_name:
        return "bus", code
    if any(term in product_name for term in ("货车", "载货汽车", "自卸汽车", "牵引车")):
        return "truck", code
    if code in ("7", "2"):
        return "passenger", code
    if code == "6":
        return ("bus" if "客车" in (product_name or "") else "passenger"), code
    if code in ("1", "3", "4"):
        return "truck", code
    return "special", code  # 已确认整车但没有通用车型名称时，按专用汽车业务入口展示。


# ------------------------------------------------------------ 动力类型归一

ENERGY_TYPES = (
    "纯电动",
    "插电式混合动力",
    "增程式",
    "燃料电池",
    "混合动力",
    "燃油",
    "未知",
)

# 关键词按特异性从高到低匹配，先命中先用
_ENERGY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("燃料电池", "燃料电池"),
    ("插电式增程", "增程式"),
    ("增程式", "增程式"),
    ("增程插电", "增程式"),
    ("插电式", "插电式混合动力"),
    ("插电", "插电式混合动力"),
    ("纯电动", "纯电动"),
    ("混合动力", "混合动力"),
)

_FUEL_TYPE_MAP = {
    "纯电动": "纯电动",
    "电": "纯电动",
    "氢气": "燃料电池",
    "燃料电池": "燃料电池",
}


def normalize_energy(fuel_type: str, product_name: str, model_full: str,
                     catalog_energy: str) -> tuple[str, str]:
    """归一化动力类型，返回 (枚举值, 判定来源)。"""
    fuel = (fuel_type or "").strip()
    if fuel in _FUEL_TYPE_MAP:
        return _FUEL_TYPE_MAP[fuel], "fuel_type"

    text = f"{product_name or ''} {model_full or ''}"
    catalog = (catalog_energy or "").strip()
    catalog_hybrid = next((value for keyword, value in _ENERGY_KEYWORDS
                           if keyword in catalog and value in ('增程式', '插电式混合动力')), None)
    if "混合动力" in fuel or "双燃料" in fuel:
        # 「汽油/电混合动力」等：靠产品名区分插电 / 增程 / 普通混动
        for keyword, value in _ENERGY_KEYWORDS:
            if keyword in text and value in ("增程式", "插电式混合动力"):
                return value, "fuel_type+product_name"
        # 燃料栏只写混合动力不能证明不可外接充电；同型号官方目录可进一步确认插电类型。
        if catalog_hybrid:
            return catalog_hybrid, "fuel_type+jianmian"
        return "混合动力", "fuel_type"

    for keyword, value in _ENERGY_KEYWORDS:
        if keyword in text:
            if value == '混合动力' and catalog_hybrid:
                return catalog_hybrid, 'product_name+jianmian'
            return value, "product_name"

    if catalog:
        for keyword, value in _ENERGY_KEYWORDS:
            if keyword in catalog:
                return value, "jianmian"
        if catalog == "新能源":
            return "未知", "jianmian"

    if fuel:
        return "燃油", "fuel_type"
    return "未知", "none"
