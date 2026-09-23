"""正式公告详情页的原图：解析页面实际链接，沿用详情会话，保存可追溯索引。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from PIL import Image

from miit_gonggao import core


class ImageAccessError(ValueError):
    """图片请求返回了访问验证页；本产品余下图片停止请求。"""


def image_folder(row: dict[str, Any], output_dir: Path) -> Path:
    identity = [row.get("clxh", ""), row.get("cpid") or row.get("gid", "")]
    return output_dir / "images" / "_".join(core.safe_part(str(value)) for value in identity)


def parse_image_links(content: bytes, row: dict[str, Any]) -> list[dict[str, str]]:
    """不猜测连续编号，也不把验证页、错误车型或外站链接当成照片清单。"""
    soup = BeautifulSoup(content, "html.parser")
    fields = {}
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        for left, right in zip(cells, cells[1:]):
            fields[left.get_text(strip=True)] = right.get_text(strip=True)
    gid = str(row.get("cpid") or row.get("gid") or "")
    batch = str(row.get("gppc") or row.get("pc") or "")
    if not gid or not batch or fields.get("产品ID") != gid or fields.get("批次") != batch:
        raise ValueError("详情页产品 ID/批次不符或返回访问验证页，未取得图片清单")
    if fields.get("车辆型号") != str(row.get("clxh") or ""):
        raise ValueError("详情页车辆型号不符，未取得图片清单")
    base = urlsplit(core.BASE_URL)
    links = []
    seen = set()
    for img in soup.select("img[src]"):
        url = urlsplit(urljoin(core.DETAIL_URL, img["src"]))
        params = parse_qs(url.query)
        if (url.scheme != base.scheme or url.netloc != base.netloc
                or url.path != base.path + "/getPic"
                or params.get("gid") != [gid] or params.get("pc") != [batch]
                or len(params.get("zpname", [])) != 1):
            raise ValueError("详情页图片链接的来源或产品身份不符")
        name = params["zpname"][0]
        if name not in seen:
            seen.add(name)
            links.append({"name": name, "url": core.BASE_URL + "/getPic?" + urlencode(
                {"gid": gid, "pc": batch, "zpname": name})})
    return links


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".image-", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def download_product_images(row: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """下载页面中所有实际图片，错误落盘但不抛出网络/格式异常影响 PDF。

    图片按内容哈希命名，重采不覆盖旧原图；每次索引也保留历史。
    Cookie 仅在本次详情页和同源图片之间使用，不保存到文件或日志。
    """
    folder = image_folder(row, output_dir)
    result: dict[str, Any] = {
        "schema_version": 1,
        "product_id": str(row.get("cpid") or row.get("gid") or ""),
        "model_code": str(row.get("clxh") or ""),
        "batch": str(row.get("gppc") or row.get("pc") or ""),
        "detail_url": core.DETAIL_URL,
        "directory": folder.relative_to(output_dir).as_posix(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "failed", "error": "", "images": [], "downloaded": 0, "failed": 0,
    }
    try:
        content, response_headers = core.post_form(core.DETAIL_URL, core.product_payload(row))
        links = parse_image_links(content, row)
        cookie = SimpleCookie()
        cookie.load(response_headers.get("set-cookie", ""))
        headers = {"User-Agent": "Mozilla/5.0 gonggao-tool/0.1", "Referer": core.DETAIL_URL}
        if cookie:
            headers["Cookie"] = "; ".join(f"{key}={item.value}" for key, item in cookie.items())
        access_error = ""
        for link in links:
            item: dict[str, Any] = {**link, "status": "failed", "file": "", "error": ""}
            try:
                if access_error:
                    raise ImageAccessError(access_error)
                raw, _headers = core.http_request(link["url"], headers=headers)
                if "访问行为验证".encode() in raw or b"<title>captcha" in raw.lower():
                    raise ImageAccessError("图片接口要求访问验证，停止本产品余下图片请求；请稍后重试")
                with Image.open(io.BytesIO(raw)) as image:
                    image.load()
                    extension = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp"}.get(image.format)
                    if extension is None:
                        raise ValueError(f"不支持的图片格式：{image.format}")
                    width, height = image.size
                digest = hashlib.sha256(raw).hexdigest()
                filename = f"{core.safe_part(link['name'])}_{digest}{extension}"
                atomic_write(folder / filename, raw)
                item.update(status="ok", file=filename, bytes=len(raw), sha256=digest,
                            width=width, height=height)
                result["downloaded"] += 1
            except Exception as exc:
                if isinstance(exc, ImageAccessError):
                    access_error = str(exc)
                    item["status"] = "access_verification"
                result["failed"] += 1
                cause = f"；{type(exc.__cause__).__name__}: {exc.__cause__}" if exc.__cause__ else ""
                item["error"] = f"{type(exc).__name__}: {exc}{cause}"
            result["images"].append(item)
        result["status"] = ("partial" if result["downloaded"] else "failed") if result["failed"] else (
            "ok" if links else "no_images")
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["failed"] += 1
    payload = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    atomic_write(folder / f"manifest_{stamp}.json", payload)
    atomic_write(folder / "manifest.json", payload)
    print(f"公告图片：{result['downloaded']} 张成功，{result['failed']} 项失败；索引：{folder / 'manifest.json'}")
    if result["failed"]:
        print(f"图片下载不完整：{result['model_code']}；{result['error'] or '详见图片索引'}", file=sys.stderr)
    return result


def read_image_result(row: dict[str, Any], output_dir: Path) -> dict[str, Any] | None:
    path = image_folder(row, output_dir) / "manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": "failed", "failed": 1, "error": f"图片索引无法读取：{exc}"}


def publish_images(row: dict[str, Any], staging: Path, output_dir: Path) -> None:
    """采集暂存目录移交：先发布不可变图片/历史索引，再替换当前索引。"""
    source = image_folder(row, staging)
    if not source.exists():
        return
    destination = image_folder(row, output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.name == "manifest.json":
            continue
        target = destination / path.name
        try:
            os.link(path, target)
        except FileExistsError:
            if path.read_bytes() != target.read_bytes():
                raise RuntimeError(f"图片目标已存在且内容不同：{target}")
    atomic_write(destination / "manifest.json", (source / "manifest.json").read_bytes())
