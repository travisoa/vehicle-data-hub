"""汽车之家访问拦截识别测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from autohome_cc.config import (
    ACCESS_BLOCK_KEYWORDS,
    ACCESS_BLOCK_STRONG_KEYWORDS,
    ACCESS_BLOCK_WEAK_KEYWORDS,
)
from autohome_cc.crawler.http_client import HttpClient, LoginRequiredError


def _response(text: str, *, status_code: int = 200, url: str = "https://car.autohome.com.cn/config/series/1.html"):
    return SimpleNamespace(text=text, status_code=status_code, url=url)


def test_access_block_strong_keyword_on_200():
    client = HttpClient(logger=None)
    with pytest.raises(LoginRequiredError):
        client._raise_for_access_block(_response("请完成访问验证后继续"))


def test_access_block_rate_limit_on_200():
    client = HttpClient(logger=None)
    with pytest.raises(LoginRequiredError):
        client._raise_for_access_block(_response("访问过于频繁，请稍后再试"))


def test_access_block_ignores_lone_captcha_on_normal_page():
    client = HttpClient(logger=None)
    client._raise_for_access_block(_response("本页支持图形验证码，填写配置参数"))


def test_access_block_strong_keywords_are_derived():
    assert set(ACCESS_BLOCK_STRONG_KEYWORDS) == set(ACCESS_BLOCK_KEYWORDS) - set(
        ACCESS_BLOCK_WEAK_KEYWORDS
    )
    assert set(ACCESS_BLOCK_WEAK_KEYWORDS).issubset(ACCESS_BLOCK_KEYWORDS)
    assert "验证码" in ACCESS_BLOCK_WEAK_KEYWORDS
    assert "验证码" not in ACCESS_BLOCK_STRONG_KEYWORDS
