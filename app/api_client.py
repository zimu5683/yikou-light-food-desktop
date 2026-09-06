"""纯接口模式的 HTTP API 客户端（不启动浏览器）。

两个平台的登录、列表、下单等接口都通过普通 HTTP 请求完成，避免弹出浏览器。
管理后台有 WAF，需要带浏览器特征的请求头；闪时送平台登录需要图形验证码，
验证码图片由调用方展示给用户后把用户输入的 code 传回。
"""
from __future__ import annotations

import random
from typing import Any
from urllib.parse import urlsplit

import requests


class ApiError(RuntimeError):
    """纯接口模式下请求失败或响应异常时抛出。"""


def origin_from_url(url: str) -> str:
    """从完整 URL 中提取协议+域名，例如 https://m.icall.me/admin/#/login → https://m.icall.me。"""
    parts = urlsplit(url or "")
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"无法从网址提取源：{url}")
    return f"{parts.scheme}://{parts.netloc}"


def _browser_headers(origin: str, *, admin: bool = True) -> dict[str, str]:
    """构造能通过管理后台 WAF 的浏览器特征请求头。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": origin,
        "X-Requested-With": "XMLHttpRequest",
    }
    headers["Referer"] = (origin + "/admin/") if admin else (origin + "/takeout")
    return headers


def _find_key(obj: Any, key: str) -> Any:
    """在嵌套 JSON 中递归查找第一个非空键值。"""
    if isinstance(obj, dict):
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


class AdminApiClient:
    """管理后台纯接口客户端。"""

    def __init__(self, url: str, username: str, password: str, timeout: float = 15) -> None:
        self.origin = origin_from_url(url)
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(_browser_headers(self.origin, admin=True))
        self.token = ""
        self.uniacid = ""

    def login(self) -> None:
        """账号密码登录，保存 token 与 uniacid。"""
        try:
            resp = self.session.post(
                self.origin + "/channel/login",
                json={"username": self.username, "password": self.password, "remember": False},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(f"管理后台登录请求失败：{exc}") from exc

        if resp.status_code == 403:
            raise ApiError("管理后台拒绝了纯接口登录（HTTP 403），请改用浏览器备用模式")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiError(f"管理后台登录返回非 JSON（HTTP {resp.status_code}）") from exc

        if payload.get("code") not in (200, None):
            raise ApiError(payload.get("msg") or f"管理后台登录失败（{payload.get('code')}）")

        data = payload.get("data") or {}
        user_info = data.get("user_info") or {}
        self.token = str(data.get("token") or "")
        self.uniacid = str(data.get("uniacid") or user_info.get("uniacid") or "")
        if not self.token:
            raise ApiError("管理后台登录响应中没有 token")
        if not self.uniacid:
            raise ApiError("管理后台登录响应中没有 uniacid")

    def get_json(self, path: str) -> dict[str, Any]:
        """带鉴权 GET 并解析 JSON，401 按登录失效处理。"""
        if not self.token:
            raise ApiError("尚未登录管理后台")
        headers = {"Authorization": f"Bearer {self.token}", "uniacid": self.uniacid}
        try:
            resp = self.session.get(self.origin + path, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ApiError(f"接口 {path} 请求失败：{exc}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiError(f"接口 {path} 返回非 JSON（HTTP {resp.status_code}）") from exc

        if resp.status_code == 401 or payload.get("code") == 401:
            raise ApiError("登录会话已失效（接口 401），请重新运行程序")
        code = payload.get("code")
        if code not in (200, None):
            raise ApiError(f"接口 {path} 返回异常：{payload.get('msg') or code}")
        return payload


class SssApiClient:
    """闪时送纯接口客户端。"""

    def __init__(self, url: str, account: str, password: str, timeout: float = 15) -> None:
        self.origin = origin_from_url(url)
        self.account = account
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(_browser_headers(self.origin, admin=False))
        self.token = ""

    def fetch_captcha(self) -> bytes:
        """获取登录图形验证码 PNG 图片。"""
        try:
            resp = self.session.get(
                self.origin + "/consumer/customer/verify-code",
                params={"data": str(random.random()), "phone": self.account},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(f"获取闪时送验证码失败：{exc}") from exc
        if not resp.content.startswith(b"\x89PNG"):
            raise ApiError("闪时送验证码接口未返回 PNG 图片")
        return resp.content

    def login(self, code: str) -> None:
        """使用账号密码和用户输入的图形验证码登录。"""
        code = str(code or "").strip()
        if len(code) < 4:
            raise ApiError("请输入完整图形验证码")
        try:
            resp = self.session.post(
                self.origin + "/consumer/customer/password/login",
                json={"mobile": self.account, "password": self.password, "code": code},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(f"闪时送登录请求失败：{exc}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiError(f"闪时送登录返回非 JSON（HTTP {resp.status_code}）") from exc

        if not payload.get("success"):
            raise ApiError(payload.get("message") or payload.get("msg") or "闪时送登录失败（验证码错误？）")

        token = _find_key(payload, "token")
        if not token:
            raise ApiError("闪时送登录响应中没有 token")
        self.token = str(token)

    def _request(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["token"] = self.token
        try:
            resp = self.session.request(
                method,
                self.origin + path,
                headers=headers,
                json=body if body is not None else None,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(f"接口 {path} 请求失败：{exc}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiError(f"接口 {path} 返回非 JSON（HTTP {resp.status_code}）") from exc

        if resp.status_code == 401 or payload.get("code") == 401:
            raise ApiError("闪时送登录态已失效（接口 401），请重新登录")
        return payload

    def get_json(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def post_json(self, path: str, body: Any = None) -> dict[str, Any]:
        return self._request("POST", path, body)
