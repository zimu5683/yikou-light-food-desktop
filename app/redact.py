"""输出脱敏：把日志/错误文案里的秘密与客户联系方式掩掉。

边界（很重要，别做过头）：

* **脱敏只用于"输出通道"** —— 日志事件、错误原因、未决/恢复列表、验收证据。
  本机受保护的业务数据文件（配置、账本、未决 journal、留档 Excel）里该有的
  完整信息照旧保留，否则程序就没法工作；
* 不影响"排单表业务界面"按原流程展示名单（那是用户自己的工作数据）；
* 只做**形态识别**，不猜语义：11 位数字掩成前 3 后 4，``键=值`` 形态的凭据
  整段掩掉，Bearer/Basic 头整段掩掉。

这些规则的目标是"任何一条日志复制出去都不会泄露密码或客户手机号"，而不是
"任何地方都看不到业务数据"。
"""
from __future__ import annotations

import re

#: 11 位手机号（前后不能是数字，避免误伤更长的有序号）。
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
#: ``键=值`` / ``键: 值`` 形态的凭据；键名覆盖中英文常见写法。
_SECRET_KV_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|access[_-]?token|refresh[_-]?token|"
    r"cookie|secret|authorization|api[_-]?key|验证码|密码|口令)\b"
    r"(\s*[:=：]\s*)(\S+)")
#: ``Authorization: Bearer xxx`` / ``Basic xxx``。
_AUTH_HEADER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}")

MASK = "***"


def redact(text: object) -> str:
    """返回脱敏后的文本；输入非字符串时先 ``str()``（``None`` 返回空串）。"""
    if text is None:
        return ""
    value = str(text)
    if not value:
        return value
    value = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)} {MASK}", value)
    value = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", value)
    value = _PHONE_RE.sub(lambda m: f"{m.group(1)}****{m.group(2)}", value)
    return value


def mask_phone(value: object) -> str:
    """只掩手机号（未决列表等需要保留其它诊断信息的场景）。"""
    if value is None:
        return ""
    return _PHONE_RE.sub(lambda m: f"{m.group(1)}****{m.group(2)}", str(value))


__all__ = ["MASK", "mask_phone", "redact"]
