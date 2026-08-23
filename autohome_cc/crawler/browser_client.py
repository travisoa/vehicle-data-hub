"""浏览器辅助抓取：扫码登录、渲染搜索页、抓取配置页。"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
from pathlib import Path
import re
from urllib.parse import quote

from autohome_cc.config import ACCESS_BLOCK_KEYWORDS
from autohome_cc.crawler.search import SearchCandidate
from autohome_cc.utils.cleaners import normalize_whitespace


class BrowserUnavailableError(RuntimeError):
    """未安装 Playwright 或未准备浏览器运行环境。"""


@dataclass
class BrowserFetchResult:
    """浏览器抓取结果。"""

    url: str
    status_code: int
    text: str
    final_url: str
    cookies: dict[str, str] = field(default_factory=dict)


class BrowserAutohomeClient:
    """仅在显式启用时使用的浏览器辅助抓取客户端。"""

    LOGIN_URL = "https://account.autohome.com.cn/login"
    SEARCH_URL_TEMPLATE = "https://sou.autohome.com.cn/search.aspx?q={query}"

    def __init__(
        self,
        logger,
        *,
        user_data_dir: Path,
        channel: str = "",
        timeout_ms: int = 30_000,
        headless: bool = True,
    ) -> None:
        self.logger = logger
        self.user_data_dir = Path(user_data_dir).expanduser()
        self.channel = channel.strip()
        self.timeout_ms = timeout_ms
        self.headless = headless

        if importlib.util.find_spec("playwright.sync_api") is None:
            raise BrowserUnavailableError(
                "未安装 playwright。请先执行 `pip install playwright`，再执行 "
                "`python3 -m playwright install chromium`。"
            )

    def interactive_login(self) -> dict[str, str]:
        """打开浏览器让用户扫码登录，并返回 cookies。"""
        with self._playwright() as playwright:
            context = self._launch_context(playwright, headless=False)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self._goto(page, self.LOGIN_URL)
                print("浏览器已打开 Autohome 登录页，请扫码登录后回到终端按回车继续。")
                input()
                self._goto(page, "https://car.autohome.com.cn/")
                cookies = self._cookies_to_mapping(context.cookies())
                self.logger.info("浏览器登录完成，已同步 cookies", stage="browser_login")
                return cookies
            finally:
                context.close()

    def search(self, model_name: str) -> list[SearchCandidate]:
        """使用浏览器渲染搜索结果页，提取候选配置链接。"""
        search_url = self.SEARCH_URL_TEMPLATE.format(query=quote(model_name))
        with self._playwright() as playwright:
            context = self._launch_context(playwright, headless=self.headless)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self._goto(page, search_url)
                page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
                page.wait_for_timeout(1200)
                raw_links = page.eval_on_selector_all(
                    "a[href]",
                    """
                    (elements) => elements.map((element, index) => ({
                        href: element.href || '',
                        text: (element.innerText || element.textContent || '').trim(),
                        index
                    }))
                    """,
                )
                return self._build_candidates(model_name, raw_links)
            finally:
                context.close()

    def fetch_text(self, url: str, *, allow_manual_login: bool = False) -> BrowserFetchResult:
        """使用浏览器打开页面并返回渲染后的 HTML。"""
        with self._playwright() as playwright:
            context = self._launch_context(
                playwright,
                headless=False if allow_manual_login else self.headless,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self._goto(page, url)
                html = page.content()
                if allow_manual_login and self._looks_like_access_block(page.url, html):
                    print("浏览器已打开 Autohome 登录/验证页，请扫码后回到终端按回车继续。")
                    input()
                    self._goto(page, url)
                html = self._extract_rendered_html(page)
                return BrowserFetchResult(
                    url=url,
                    status_code=200,
                    text=html,
                    final_url=page.url,
                    cookies=self._cookies_to_mapping(context.cookies()),
                )
            finally:
                context.close()

    def _build_candidates(self, model_name: str, raw_links: list[dict]) -> list[SearchCandidate]:
        candidates: list[SearchCandidate] = []
        seen: set[str] = set()

        for item in raw_links:
            href = normalize_whitespace(str(item.get("href", "")))
            title = normalize_whitespace(str(item.get("text", ""))) or model_name
            if not href or href in seen or "autohome.com.cn" not in href:
                continue
            if "/config/series/" not in href and "/config/spec/" not in href:
                continue
            seen.add(href)

            rank = len(candidates) + 1
            score = 100 - rank
            lowered_query = normalize_whitespace(model_name).lower()
            lowered_title = title.lower()
            if lowered_query == lowered_title:
                score += 20
            elif lowered_query in lowered_title:
                score += 10

            series_match = re.search(r"/series/(\d+)", href)
            candidates.append(
                SearchCandidate(
                    model_name=model_name,
                    query=model_name,
                    title=title,
                    snippet="browser_search",
                    url=href,
                    rank=rank,
                    score=score,
                    series_id=series_match.group(1) if series_match else "",
                    brand="",
                )
            )

        candidates.sort(key=lambda item: (-item.score, item.rank))
        return candidates

    def _launch_context(self, playwright, *, headless: bool):
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        launch_kwargs = {
            "user_data_dir": str(self.user_data_dir),
            "headless": headless,
        }
        if self.channel:
            launch_kwargs["channel"] = self.channel
        try:
            return playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as exc:  # noqa: BLE001
            raise BrowserUnavailableError(
                "无法启动 Playwright 浏览器。请确认已执行 "
                "`python3 -m playwright install chromium`，或传入可用的浏览器 channel。"
            ) from exc

    def _goto(self, page, url: str) -> None:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
            page.wait_for_timeout(500)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"浏览器访问失败: {url}") from exc

    def _looks_like_access_block(self, final_url: str, html: str) -> bool:
        lowered = html[:4000].lower()
        return (
            "login" in final_url.lower()
            or any(keyword.lower() in lowered for keyword in ACCESS_BLOCK_KEYWORDS)
        )

    @staticmethod
    def _cookies_to_mapping(cookies: list[dict]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for item in cookies:
            name = str(item.get("name", "")).strip()
            value = str(item.get("value", "")).strip()
            if name:
                mapping[name] = value
        return mapping

    def _extract_rendered_html(self, page) -> str:
        """把页面里依赖伪元素显示的占位字还原进 DOM 后，再导出 HTML。"""
        page.evaluate(
            """
            () => {
              const cleanContent = (value) => {
                if (!value || value === 'none' || value === 'normal') return '';
                return value.replace(/^['"]|['"]$/g, '');
              };

              const targets = Array.from(document.querySelectorAll('[class*="hs_kw"]'));
              for (const element of targets) {
                const before = cleanContent(window.getComputedStyle(element, '::before').content);
                const after = cleanContent(window.getComputedStyle(element, '::after').content);
                const text = `${before}${after}`;
                if (!text) continue;
                element.textContent = text;
                element.removeAttribute('class');
              }
            }
            """
        )
        return page.content()

    @staticmethod
    def _playwright():
        from playwright.sync_api import sync_playwright

        return sync_playwright()
