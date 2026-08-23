"""清洗与归一化工具。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re


def safe_filename_part(value: str) -> str:
    """把车型名收拾成可直接放进文件名的片段。"""
    text = normalize_whitespace(value)
    text = re.sub(r'[\\/:*?"<>|]+', "", text)
    return text.replace(" ", "")


def build_export_filename(
    model_names: list[str],
    *,
    prefix: str,
    when: datetime | None = None,
    suffix: str = ".xlsx",
) -> str:
    """按「来源_车型配置表_日期」拼导出文件名。

    单车型  -> 汽车之家_<车型>配置表_20260728.xlsx
    多车型  -> 汽车之家_<车型>等3个车型配置表_20260728.xlsx（首车 + 车型总数）
    车型名缺失（抓包回放未指定车型）-> 汽车之家_配置表_20260728.xlsx
    """
    names = [part for part in (safe_filename_part(name) for name in model_names) if part]
    if not names:
        core = "配置表"
    elif len(names) == 1:
        core = f"{names[0]}配置表"
    else:
        core = f"{names[0]}等{len(names)}个车型配置表"
    date_text = (when or datetime.now()).strftime("%Y%m%d")
    return f"{prefix}_{core}_{date_text}{suffix}"


def ensure_unique_path(path: Path) -> Path:
    """同名文件已存在时追加序号，避免同一天重复导出互相覆盖。"""
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"同名文件过多，无法确定导出路径: {path}")


def split_model_names(raw_text: str) -> list[str]:
    """拆分用户输入的车型名称。"""
    parts = re.split(r"[,，\n]+", raw_text)
    return [normalize_whitespace(part) for part in parts if normalize_whitespace(part)]


def normalize_whitespace(value: str) -> str:
    """压缩多余空白。"""
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_text_value(value: str) -> str:
    """统一空值和常见无效占位。"""
    value = normalize_whitespace(value)
    if value in {"-", "--", "暂无", "未公布", "无"}:
        return ""
    return value


def tokenize_model_name(model_name: str) -> list[str]:
    """把车型名拆成可用于匹配的词片段。"""
    normalized = normalize_whitespace(model_name).lower()
    chunks = re.split(r"[\s\-_/]+", normalized)
    return [chunk for chunk in chunks if chunk]


def should_expand_all_trims(model_name: str) -> bool:
    """判断输入更像车系名还是具体配置名。

    返回 True 时，在线抓取会导出该车系全部在售配置；
    返回 False 时，只匹配一个最相关的具体版本。
    """
    text = normalize_whitespace(model_name)
    if not text:
        return False

    specific_patterns = [
        r"20\d{2}款",
        r"(两驱|四驱|前驱|后驱)",
        r"\b\d\.\dT[D]?\b",
        r"\b\d\.\dL\b",
        r"\b\d+km\b",
        r"\b\d座\b",
        r"(AT|MT|CVT|DCT|双离合|手动|自动挡)",
        r"(Max|Pro|Plus|Ultra|Elite|Premium|Luxury)",
        r"(舒适版|精英版|豪华版|旗舰版|尊贵版|尊享版|智享版|智驾版|进阶版|探索版|冠军版|旅行家|行政版|长续航)",
    ]
    return not any(re.search(pattern, text, re.I) for pattern in specific_patterns)


def parse_cookie_string(raw_cookie: str) -> dict[str, str]:
    """解析 header 风格的 Cookie 字符串。"""
    cookies: dict[str, str] = {}
    for chunk in raw_cookie.split(";"):
        piece = chunk.strip()
        if not piece or "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            cookies[key] = value
    return cookies


def parse_cookie_file(path: str) -> dict[str, str]:
    """解析 header 风格或 Netscape 风格 cookie 文件。"""
    file_path = Path(path).expanduser()
    text = file_path.read_text(encoding="utf-8").strip()
    if not text:
        return {}

    if "\t" not in text and "\n" not in text:
        return parse_cookie_string(text)

    if "\t" not in text:
        return parse_cookie_string(text.replace("\n", "; "))

    cookies: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
            continue
        if "=" in stripped:
            key, value = stripped.split("=", 1)
            cookies[key.strip()] = value.strip()
    return cookies


def clean_compare_value(value: str) -> str:
    """面向对比表保留文本，但统一空值显示。"""
    text = normalize_whitespace(value)
    text = re.sub(r"(?:起)?询底价", "", text)
    text = text.replace("获取底价", "").strip()
    if text in {"", "--", "暂无", "未公布"}:
        return "-"
    return text


def extract_numeric_value(value: str) -> str:
    """提取首个数字，适合 summary 做横向比较。"""
    text = normalize_whitespace(value)
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return match.group(0) if match else text


def extract_measurements(value: str) -> tuple[str, str, str]:
    """解析长宽高。"""
    if not value:
        return "", "", ""
    numbers = re.findall(r"\d+(?:\.\d+)?", value.replace(",", ""))
    if len(numbers) >= 3:
        return numbers[0], numbers[1], numbers[2]
    return "", "", ""
