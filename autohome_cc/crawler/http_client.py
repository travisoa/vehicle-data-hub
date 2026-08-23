"""HTTP 请求封装。"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import requests
from requests import Response, Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from autohome_cc.config import ACCESS_BLOCK_KEYWORDS, DEFAULT_HEADERS, REQUEST_RETRY_BACKOFF, REQUEST_RETRY_TOTAL, REQUEST_SLEEP_RANGE, REQUEST_TIMEOUT


@dataclass
class FetchResult:
    """一次抓取结果。"""

    url: str
    status_code: int
    text: str
    final_url: str


class LoginRequiredError(RuntimeError):
    """Autohome 返回登录/验证页。"""


class HttpClient:
    """带重试和节流的简单 HTTP 客户端。"""

    def __init__(self, logger, *, cookies: dict[str, str] | None = None) -> None:
        self.logger = logger
        self.session = self._build_session()
        if cookies:
            self.session.cookies.update(cookies)

    def set_cookies(self, cookies: dict[str, str]) -> None:
        """合并外部获得的 cookies，例如浏览器登录态。"""
        if cookies:
            self.session.cookies.update(cookies)

    def _build_session(self) -> Session:
        session = requests.Session()
        session.headers.update(DEFAULT_HEADERS)

        retry = Retry(
            total=REQUEST_RETRY_TOTAL,
            connect=REQUEST_RETRY_TOTAL,
            read=REQUEST_RETRY_TOTAL,
            status=REQUEST_RETRY_TOTAL,
            backoff_factor=REQUEST_RETRY_BACKOFF,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def get_text(
        self,
        url: str,
        *,
        params: dict | None = None,
        referer: str | None = None,
    ) -> FetchResult:
        """获取文本内容。"""
        headers = {}
        if referer:
            headers["Referer"] = referer

        response = self.session.get(
            url,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        self._respect_sleep()
        self._raise_for_access_block(response)
        response.raise_for_status()
        return FetchResult(
            url=url,
            status_code=response.status_code,
            text=response.text,
            final_url=str(response.url),
        )

    def get_response(
        self,
        url: str,
        *,
        params: dict | None = None,
        referer: str | None = None,
    ) -> Response:
        """返回原始响应对象，便于调试或后续扩展。"""
        headers = {}
        if referer:
            headers["Referer"] = referer

        response = self.session.get(
            url,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        self._respect_sleep()
        self._raise_for_access_block(response)
        response.raise_for_status()
        return response

    def _respect_sleep(self) -> None:
        """控制抓取频率，避免过于频繁。"""
        sleep_seconds = random.uniform(*REQUEST_SLEEP_RANGE)
        time.sleep(sleep_seconds)

    def _raise_for_access_block(self, response: Response) -> None:
        """识别登录墙、访问验证和反爬提示。"""
        body = response.text[:4000]
        final_url = str(response.url)
        if response.status_code in {401, 403}:
            raise LoginRequiredError(self._build_login_hint(final_url, response.status_code))

        if any(keyword.lower() in body.lower() for keyword in ACCESS_BLOCK_KEYWORDS):
            raise LoginRequiredError(self._build_login_hint(final_url, response.status_code))

    @staticmethod
    def _build_login_hint(final_url: str, status_code: int) -> str:
        return (
            f"Autohome 返回了权限/验证页面 (status={status_code}, url={final_url})。"
            "请让用户先扫码登录 Autohome，或导出当前浏览器 cookies 后通过 "
            "`--cookie-file` / `--cookies` 重新执行。"
        )
