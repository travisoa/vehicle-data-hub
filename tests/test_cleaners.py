"""cookie / 文件名工具测试。"""

from __future__ import annotations

from autohome_cc.utils.cleaners import parse_cookie_file, parse_cookie_string


def test_parse_cookie_string():
    assert parse_cookie_string("a=1; b=2") == {"a": "1", "b": "2"}


def test_parse_cookie_file_netscape_httponly(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.autohome.com.cn\tTRUE\t/\tTRUE\t0\tsessionid\tsecret\n"
        ".autohome.com.cn\tTRUE\t/\tFALSE\t0\tfoo\tbar\n"
        "# comment only\n",
        encoding="utf-8",
    )
    cookies = parse_cookie_file(str(cookie_file))
    assert cookies["sessionid"] == "secret"
    assert cookies["foo"] == "bar"
    assert len(cookies) == 2
