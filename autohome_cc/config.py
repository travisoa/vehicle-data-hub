"""项目配置。"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "logs"
BROWSER_PROFILE_DIR = BASE_DIR / ".browser" / "autohome"
DEFAULT_OUTPUT_PREFIX = "汽车之家"

REQUEST_TIMEOUT = 20
BROWSER_TIMEOUT_MS = 30000
REQUEST_RETRY_TOTAL = 3
REQUEST_RETRY_BACKOFF = 1.2
REQUEST_SLEEP_RANGE = (1.0, 2.5)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

ACCESS_BLOCK_KEYWORDS = [
    "请先登录",
    "扫码登录",
    "访问验证",
    "安全验证",
    "验证码",
    "登录后可查看",
    "访问过于频繁",
    "异常访问",
    "robot check",
]

CAPTURE_NOISE_KEYWORDS = [
    "广告",
    "热门推荐",
    "app下载",
    "打开汽车之家",
    "立即打开",
    "询底价",
    "获取底价",
    "降价提醒",
    "经销商优惠",
    "点击了解",
    "查看完整参数",
    "参数纠错",
]

IGNORED_PARAM_KEYWORDS = [
    "图片",
    "视频",
    "报价图片",
    "经销商",
    "询价",
    "底价",
    "对比",
    "关注度",
]

PREFERRED_SECTION_ORDER = [
    "基本参数",
    "车身",
    "发动机",
    "电动机",
    "电池/充电",
    "变速箱",
    "底盘转向",
    "车轮制动",
    "被动安全",
    "主动安全",
    "驾驶操控",
    "驾驶硬件",
    "驾驶功能",
    "外观/防盗",
    "车外灯光",
    "天窗/玻璃",
    "外后视镜",
    "互联/智能化",
    "方向盘/内后视镜",
    "车内充电",
    "座椅配置",
    "音响/车内灯光",
    "空调/冰箱",
    "颜色",
    "选装包",
]

SUMMARY_SECTION_FIELDS = {
    "基础信息": [
        ("brand", "品牌"),
        ("series_name", "车系"),
        ("model_name", "车型"),
        ("autohome_url", "Autohome 链接"),
        ("energy_type", "能源类型"),
        ("market_date", "上市时间"),
        ("official_price", "厂商指导价"),
        ("sales_volume", "销量"),
    ],
    "动力总成": [
        ("engine", "发动机"),
        ("displacement", "排量"),
        ("intake_type", "进气形式"),
        ("max_power_kw", "最大功率(kW)"),
        ("max_torque_nm", "最大扭矩(N·m)"),
        ("horsepower", "最大马力(Ps)"),
        ("motor_power_kw", "电机功率(kW)"),
        ("motor_torque_nm", "电机扭矩(N·m)"),
        ("gearbox", "变速箱"),
        ("drive_type", "驱动方式"),
    ],
    "电池与电动化": [
        ("battery_type", "电池类型"),
        ("battery_capacity_kwh", "电池容量(kWh)"),
        ("cltc_range_km", "CLTC续航(km)"),
        ("wltc_range_km", "WLTC续航(km)"),
        ("fast_charge_time", "快充时间"),
        ("slow_charge_time", "慢充时间"),
    ],
    "车身与空间": [
        ("length_mm", "长度(mm)"),
        ("width_mm", "宽度(mm)"),
        ("height_mm", "高度(mm)"),
        ("wheelbase_mm", "轴距(mm)"),
        ("seats", "座位数"),
        ("doors", "车门数"),
        ("curb_weight_kg", "整备质量(kg)"),
        ("trunk_volume_l", "后备厢容积(L)"),
    ],
}
