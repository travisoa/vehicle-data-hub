"""频道映射：国标型号按类别代号，进口车厂家型号不取数字，产品名写明乘用车时优先。"""
import pytest

from miit_gonggao.vehicle_categories import derive_channel


@pytest.mark.parametrize("model,name,hint,expected", [
    # 进口车厂家型号没有国标类别代号，「i5」「i3」里的数字不能当类别代号。
    ("i5 M60 xDrive 41FK", "纯电动轿车（新能源汽车）", "", ("passenger", None)),
    ("Taycan 4 D131", "纯电动乘用车（新能源汽车）", "", ("passenger", None)),
    # 形状像国标的进口型号：产品名写明乘用车，不被类别代号 5 归入专用车。
    ("MD54BEV1", "纯电动乘用车", "", ("passenger", "5")),
    # 型号和产品名都判不出时，进口车才用同型号的官方目录类别兜底。
    ("i3 8P21", "纯电动汽车（新能源汽车）", "乘用车", ("passenger", None)),
    ("i3 8P21", "纯电动汽车（新能源汽车）", "", ("special", None)),
    # 国标专用汽车：类别代号 5 仍先于产品名里的「货车」。
    ("CA5090XXYP40L4BEVA84", "纯电动厢式货车", "", ("special", "5")),
    # 国标型号有类别代号时不采用目录兜底。
    ("ZZ1040BEV", "纯电动汽车", "乘用车", ("truck", "1")),
    ("ZZ6120BEV", "纯电动城市客车", "", ("bus", "6")),
])
def test_channel_follows_gb_code_then_explicit_name_then_catalog_for_imports(model, name, hint, expected):
    assert derive_channel(model, name, catalog_hint=hint) == expected


def test_official_category_still_decides_when_scope_relies_on_it():
    assert derive_channel("IMPORT", "", catalog_category="乘用车") == ("passenger", None)
