"""闪时送配置网址的严格校验与规范化。

背景：闪时送下单的反重复闸门以「实际请求 origin + 规范化账号」作为权威未决
状态身份。已复现过的绕过路径是：同一批次第一次提交进入 uncertain 后，只把
配置网址改成另一种等价写法（例如给主机名加尾点），就会换到另一个 authority
scope、看不到原来的未决记录，于是第二次 POST 被放行。

本模块只做一件事：在**发出任何闪时送请求之前**把配置网址判定为「规范且受
支持」或「明确配置错误」。判定规则刻意保持可预测，并保留历史兼容：

- 只接受 ``http`` / ``https``（scheme 大小写不敏感）；
- 主机名统一小写；``http:80`` / ``https:443`` 作为默认端口被去掉；
- 路径 / 查询 / 片段继续被忽略（origin 本来就不使用它们）；
- IPv4、方括号 IPv6、punycode（A-label）、单标签主机仍按原样受理；
- 主机名必须 ASCII：中文/全角等非 ASCII 域名在 ``Origin``/``Referer`` 头上会
  直接抛 ``UnicodeEncodeError``，这里提前给出明确配置错误；
- 主机名不得以 ``.`` 结尾（尾点写法在 DNS 查询、Cookie 作用域、``Origin``/
  ``Referer`` 上都与不带尾点不同，属未验证语义，不允许进入生产链路）；
- 不接受网址里的用户名/密码（客户端本来就会丢弃）；
- 端口必须是 1-65535 的整数，端口 0 / 越界 / 非数字一律拒绝。

判定失败一律抛 :class:`SssUrlConfigError`（``ValueError`` 子类），错误文案包含
原因与操作建议；配置保存入口、任务启动入口与 ``SssApiClient`` 构造函数共用同一
份规则，任何入口都不能把非规范网址带进请求阶段。

本模块**不**用于管理后台网址，以免误伤共用客户端的其他业务。
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

#: 配置网址长度上限，避免病态输入。
_MAX_URL_LENGTH = 2048
#: DNS 主机名总长度上限（RFC 1035）。
_MAX_HOST_LENGTH = 253
#: 单个标签长度上限。
_MAX_LABEL_LENGTH = 63
_SUPPORTED_SCHEMES = ("http", "https")
_DEFAULT_PORTS = {"http": 80, "https": 443}
#: DNS 标签：允许 ASCII 字母/数字/连字符/下划线；保守拒绝其它字符。
_LABEL_RE = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?$")
_ONLY_DIGITS_AND_DOTS_RE = re.compile(r"^[0-9.]+$")


class SssUrlConfigError(ValueError):
    """闪时送配置网址不受支持；调用方必须在产生外部请求之前终止。"""


def _error(reason: str, advice: str) -> SssUrlConfigError:
    return SssUrlConfigError(f"闪时送网址配置错误：{reason}；{advice}")


def _has_control_chars(text: str) -> bool:
    return any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def _canonical_host(host: str, *, bracketed: bool) -> str:
    """校验并返回规范主机名（IPv6 保留方括号）。"""
    if bracketed or ":" in host:
        if "%" in host:
            raise _error("IPv6 地址带 zone id 不受支持",
                         "请去掉 % 之后的接口名，或改用标准主机名")
        try:
            address = ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise _error(f"IPv6 地址写法不合法（{host}）",
                         "请使用方括号形式，例如 https://[::1]:8443/") from exc
        return f"[{address.compressed}]"

    if _ONLY_DIGITS_AND_DOTS_RE.match(host):
        try:
            return str(ipaddress.IPv4Address(host))
        except ValueError as exc:
            raise _error(f"IPv4 地址写法不合法（{host}）",
                         "请检查是否为四段 0-255 的数字，例如 127.0.0.1") from exc

    if len(host) > _MAX_HOST_LENGTH:
        raise _error("主机名过长", "请检查是否误粘贴了额外内容")
    for label in host.split("."):
        if not label:
            raise _error(f"主机名包含空的标签（{host}）",
                         "请检查是否多写了连续的点号")
        if len(label) > _MAX_LABEL_LENGTH:
            raise _error(f"主机名标签过长（{host}）", "请检查是否误粘贴了额外内容")
        if not _LABEL_RE.match(label):
            raise _error(
                f"主机名包含不支持的字符（{label}）",
                "主机名只允许 ASCII 字母/数字/连字符/下划线；"
                "中文域名请改写为 punycode（A-label，形如 xn--…）")
    return host


def canonical_sss_origin(url: object) -> str:
    """校验闪时送配置网址并返回规范 origin（协议+主机+非默认端口）。

    Raises:
        SssUrlConfigError: 网址为空、非 http/https、主机名非 ASCII 或带尾点、
            含用户名/密码、端口非法、IPv4/IPv6 写法非法、含空白/控制字符等。
    """
    text = str(url or "").strip()
    if not text:
        raise _error("网址为空",
                     "请填写形如 https://sssplusnew.zhuopaikeji.com/takeout 的完整网址")
    if len(text) > _MAX_URL_LENGTH:
        raise _error("网址过长", "请检查是否误粘贴了额外内容")
    if _has_control_chars(text):
        raise _error("网址中含空格或控制字符", "请删除网址中的空格与不可见字符")

    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise _error(f"网址无法解析（{exc}）",
                     "请检查方括号/百分号转义等写法是否完整") from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in _SUPPORTED_SCHEMES:
        raise _error(
            f"仅支持 http:// 或 https:// 开头的网址（当前 {parts.scheme or '缺少协议'}）",
            "请以 http:// 或 https:// 开头重新填写")
    if not parts.netloc or not parts.hostname:
        raise _error("网址缺少主机名",
                     "请填写完整网址，例如 https://sss.example.com/takeout")
    if "@" in parts.netloc:
        raise _error("网址中不应包含用户名或密码",
                     "请去掉 @ 之前的部分，只保留协议、主机名与端口")

    try:
        port = parts.port
    except ValueError as exc:
        raise _error("端口不是 0-65535 的整数",
                     "请检查主机名后面的 :端口 写法") from exc
    if port is not None and not 1 <= port <= 65535:
        raise _error(f"端口 {port} 不在有效范围 1-65535",
                     "请填写有效端口，或删除端口使用默认端口")

    host = parts.hostname
    if host.endswith("."):
        raise _error(
            f"主机名不能以点号结尾（{host}）",
            "尾点写法（例如 sss.example.com.）会被平台/Cookie/Origin 当成不同来源，"
            "请删除主机名结尾的点号后重试")
    if not host.isascii():
        raise _error(
            f"不支持非 ASCII（中文）域名（{host}）",
            "请改写为 punycode 写法（A-label，形如 xn--fsqu00a.xn--0zwm56d）"
            "或标准英文域名")

    canonical_host = _canonical_host(host, bracketed=parts.netloc.startswith("["))
    suffix = ""
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        suffix = f":{port}"
    return f"{scheme}://{canonical_host}{suffix}"


def is_supported_sss_url(url: object) -> bool:
    """只读判断：网址是否规范且受支持（不抛异常，供诊断/测试使用）。"""
    try:
        canonical_sss_origin(url)
    except SssUrlConfigError:
        return False
    return True


__all__ = [
    "SssUrlConfigError",
    "canonical_sss_origin",
    "is_supported_sss_url",
]
