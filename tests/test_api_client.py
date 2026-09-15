"""``app.api_client`` 的登录态判定与网址解析回归锁。

改动前 ``tests/`` 里**没有任何一处**引用过这三个函数，而它们守的是两条要命的边界：

* ``is_auth_expired_payload`` —— 平台用 **HTTP 200 + ``code=10000``** 表示「登录态
  失效」，只看 HTTP 状态码是不够的。判错会导致**无限重登**或**静默失败**。
* ``origin_from_url`` —— 从后台网址里取出「协议 + 域名」，是纯接口模式构造请求头
  的基准；取错会让请求打向错误的主机。
"""
from __future__ import annotations

import pytest

from app.api_client import (AUTH_ERROR_CODES, AUTH_MESSAGE_KEYWORDS,
                            auth_error_message, is_auth_expired_payload,
                            origin_from_url)


# ----------------------------------------------------------------------
# is_auth_expired_payload
# ----------------------------------------------------------------------
@pytest.mark.parametrize("code", sorted(AUTH_ERROR_CODES))
def test_auth_expiry_detected_by_error_code(code):
    assert is_auth_expired_payload({"code": code}) is True


@pytest.mark.parametrize("code", ["401", "10000", " 10000 "])
def test_auth_expiry_detected_by_string_code(code):
    """接口有时把 code 序列化成字符串，必须按整数比较。"""
    assert is_auth_expired_payload({"code": code}) is True


@pytest.mark.parametrize("message", AUTH_MESSAGE_KEYWORDS)
def test_auth_expiry_detected_by_every_keyword(message):
    """每个关键词都要真的能命中 —— 少一个就可能漏判一次登录失效。"""
    assert is_auth_expired_payload({"message": message}) is True


def test_auth_expiry_message_match_is_case_insensitive():
    assert is_auth_expired_payload({"message": "TOKEN INVALID"}) is True
    assert is_auth_expired_payload({"message": "Unauthorized"}) is True
    assert is_auth_expired_payload({"message": "Login Expired"}) is True


def test_auth_expiry_accepts_msg_as_message_alias():
    assert is_auth_expired_payload({"msg": "登录已过期"}) is True


def test_auth_expiry_ignores_non_dict_and_garbage_code():
    for payload in (None, [], "token失效", 0, True):
        assert is_auth_expired_payload(payload) is False
    # code 转不成整数时不能崩，也不能误判成失效
    assert is_auth_expired_payload({"code": "abc"}) is False
    assert is_auth_expired_payload({"code": None}) is False
    assert is_auth_expired_payload({"code": [1, 2]}) is False


def test_normal_success_payload_is_not_treated_as_expired():
    """正常的成功响应**绝不能**被误判成登录失效，否则会无谓地重新登录。"""
    for payload in ({"code": 0, "message": "success"},
                    {"code": 200, "message": "ok", "data": {"x": 1}},
                    {}, {"message": "ok"}):
        assert is_auth_expired_payload(payload) is False


def test_message_path_works_even_when_code_is_absent():
    assert is_auth_expired_payload({"code": None, "message": "unauthorized"}) is True


# ----------------------------------------------------------------------
# auth_error_message
# ----------------------------------------------------------------------
def test_auth_error_message_includes_code_and_message():
    text = auth_error_message({"code": 10000, "message": "token失效，请重新登陆"})
    assert "10000" in text and "token失效" in text
    assert "重新登录" in text


def test_auth_error_message_includes_code_when_message_missing():
    text = auth_error_message({"code": 401})
    assert "401" in text and "重新登录" in text


def test_auth_error_message_uses_fallback_for_unusable_payload():
    assert auth_error_message(None, "兜底文案") == "兜底文案"
    assert auth_error_message({}, "兜底文案") == "兜底文案"
    # 空白 message + code 为 None → 只能用兜底
    assert auth_error_message({"code": None, "message": "   "}, "兜底") == "兜底"


def test_auth_error_message_has_default_when_no_fallback_given():
    assert auth_error_message(None) == "闪时送登录态已失效，请重新登录"
    assert auth_error_message({}, "") == "闪时送登录态已失效，请重新登录"


def test_auth_error_message_falls_back_for_non_dict():
    assert auth_error_message("boom", "兜底") == "兜底"


def test_auth_error_message_prefers_message_over_msg():
    text = auth_error_message({"code": 1, "message": "首选", "msg": "备选"})
    assert "首选" in text and "备选" not in text


# ----------------------------------------------------------------------
# origin_from_url
# ----------------------------------------------------------------------
@pytest.mark.parametrize("url,expected", [
    ("https://m.icall.me/admin/#/login", "https://m.icall.me"),
    ("https://m.icall.me", "https://m.icall.me"),
    ("http://x.cn:8080/a/b?c=d", "http://x.cn:8080"),
    ("https://sssplusnew.zhuopaikeji.com/takeout", "https://sssplusnew.zhuopaikeji.com"),
])
def test_origin_from_url_keeps_scheme_host_and_port(url, expected):
    assert origin_from_url(url) == expected


@pytest.mark.parametrize("url", ["", "   ", "m.icall.me/x", "/only/path", "//host/x", "not a url"])
def test_origin_from_url_rejects_urls_without_scheme_or_host(url):
    """缺协议或缺主机时必须**抛错**，不能返回一个能用的假源去打请求。"""
    with pytest.raises(ValueError):
        origin_from_url(url)


def test_origin_from_url_error_message_quotes_the_input():
    with pytest.raises(ValueError, match="无法从网址提取源"):
        origin_from_url("m.icall.me/x")
