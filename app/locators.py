"""Locator configuration with ordered candidate chains.

Every UI step is described by an ordered list of locator candidates and the
first match wins.  Candidates are ordered from the most stable anchor (URL
route, DOM structure, ARIA role) down to displayed text, so a wording change
on the admin site usually only degrades to the next candidate instead of
breaking the whole run.

The default table is published to ``<user-config-dir>/locators.json`` on the
first run; editing that file is enough to adapt to site changes without a
code release.
"""
from __future__ import annotations

import json
from typing import Any, Dict

try:
    from .config import user_data_dir
except ImportError:  # pragma: no cover - allows ``python app/automation.py``
    from config import user_data_dir


DEFAULT_LOCATORS: Dict[str, Any] = {
    "login_account_input": {
        "candidates": [
            {"placeholder": "请输入手机号/账号"},
            {"css": "input[type=tel]"},
            {"css": "input:not([type=password])"},
        ],
    },
    "login_password_input": {
        "candidates": [
            {"css": "input[type=password]"},
            {"placeholder": "登录密码"},
        ],
    },
    "login_submit": {
        "candidates": [
            {"role": "button", "name_re": "立即登录|登录|Login|Sign in"},
            {"css": ".el-button--primary", "has_text_re": "登录|Login"},
            {"text": "立即登录"},
        ],
    },
    "门店地址": {
        "goto": "{base}home",
        "wait_url": "**/home",
        "action": "dblclick",
        "candidates": [
            {"css": "div.detail", "has_text_re": "门店地址|门店"},
            {"text": "门店地址"},
        ],
    },
    "订单菜单": {
        "goto": "{base}order",
        "wait_url": "**/order/**",
        "candidates": [
            {"role": "menuitem", "name_re": "订单|Order"},
            {"css": "div.navBarItem", "has_text_re": "订单|Order"},
            {"css": ".el-menu-item", "has_text_re": "订单|Order"},
            {"text": "订单"},
        ],
    },
    "外送订单": {
        "wait_networkidle": True,
        "confirm": "table",
        "candidates": [
            {"role": "tab", "name_re": "外送订单|配送订单|外送"},
            {"css": ".el-tabs__item", "has_text_re": "外送订单|配送订单|外送"},
            {"role": "menuitem", "name_re": "外送订单|配送订单|外送"},
            {"css": "div.navBarItem", "has_text_re": "外送订单|外送"},
            {"text": "外送订单"},
        ],
    },
    "labels": {
        "收货人": {
            "candidates": [
                {"text_re": "收货人|收件人|联系人"},
                {"text_re": "Receiver|Contact"},
            ],
        },
        "配送地址": {
            "candidates": [
                {"text_re": "配送地址|送餐地址|收货地址"},
                {"text_re": "Address"},
            ],
        },
    },
    "meal_table_row": {
        "candidates": [
            {"css": ".table_box tbody tr"},
            {"css": ".el-table__body-wrapper tbody tr"},
        ],
    },
}


def user_locators_path():
    """Return the user-side locator override file, independent of the install."""
    return user_data_dir() / "locators.json"


def _copy(table: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(table))


def load_locators() -> Dict[str, Any]:
    """Return the locator table, preferring the user-side override.

    A malformed user override is left untouched (so it can be repaired) and
    the built-in defaults are used instead.  On the first run the default
    table is published to the user directory as an editable template.
    """
    target = user_locators_path()
    if target.exists():
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (OSError, ValueError):
            pass
        return _copy(DEFAULT_LOCATORS)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(DEFAULT_LOCATORS, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return _copy(DEFAULT_LOCATORS)


# 闪时送（sss）平台单独使用一套定位器，避免与管理后台（默认表）的键互相覆盖。
SSS_LOCATORS: Dict[str, Any] = {
    "登录切换": {
        "candidates": [
            {"text": "账户密码登录"},
            {"text_re": "账户密码登录|密码登录"},
            {"role": "button", "name_re": "账户密码登录|密码登录"},
        ],
    },
    "账号输入框": {
        "candidates": [
            {"placeholder": "请输入账号"},
            {"css": "input[placeholder*='账号']"},
            {"css": "input:not([type=password])"},
        ],
    },
    "密码输入框": {
        "candidates": [
            {"placeholder": "请输入密码"},
            {"css": "input[type=password]"},
        ],
    },
    "订单弹窗": {
        "candidates": [
            {"text": "即时单"},
            {"css": ".ant-modal", "has_text": "即时单"},
        ],
    },
    "创建订单": {
        "candidates": [
            {"text": "创建订单"},
            {"role": "button", "name_re": "创建订单"},
        ],
    },
    "订单类型下拉": {
        "candidates": [
            {"css": ".ant-select-selector", "has_text": "即时单"},
            {"css": "span.ant-select-selection-item", "has_text": "即时单"},
            {"text": "即时单"},
        ],
    },
    "预约单选项": {
        "candidates": [
            {"css": ".ant-select-dropdown .ant-select-item-option", "has_text": "预约单"},
            {"text": "预约单"},
        ],
    },
    "分单下拉": {
        "candidates": [
            {"css": ".ant-select-selector", "has_text": "自动分单"},
            {"css": "span.ant-select-selection-item", "has_text": "自动分单"},
            {"text": "自动分单"},
        ],
    },
    "一口轻食选项": {
        "candidates": [
            {"css": ".ant-select-dropdown .ant-select-item-option", "has_text": "一口轻食"},
            {"text": "一口轻食"},
        ],
    },
    "常用地址": {
        "candidates": [
            {"text": "常用地址"},
            {"role": "button", "name_re": "常用地址"},
        ],
    },
    "地址选项": {
        # 选项文字由 sss_common_address（默认“嗯哼”）通过 {label} 动态替换。
        "candidates": [
            {"css": ".ant-select-dropdown .ant-select-item-option", "has_text": "{label}"},
            {"text": "{label}"},
        ],
    },
    "地址确定": {
        "candidates": [
            {"css": ".ant-modal-footer .ant-btn-primary", "has_text": "确定"},
            {"css": ".ant-btn-primary", "has_text": "确定"},
            {"role": "button", "name_re": "确定|确认"},
        ],
    },
    "送达时间输入": {
        "candidates": [
            {"placeholder": "请选择日期时间"},
            {"xpath": "//*[contains(text(),'送达时间')]/following::input[1]"},
        ],
    },
    "时间确定": {
        "candidates": [
            {"css": ".ant-picker-ok button"},
            {"css": ".ant-picker-footer button", "has_text": "确定"},
            {"role": "button", "name": "确定"},
        ],
    },
    "顾客姓名": {
        "candidates": [
            {"css": "#receiveName"},
            {"placeholder": "请输入收货人姓名"},
        ],
    },
    "顾客电话": {
        "candidates": [
            {"css": "#receiveMobile"},
            {"placeholder": "请输入收货人电话"},
        ],
    },
    "商品名称": {
        "candidates": [
            {"xpath": "//*[contains(text(),'名称')]/following::input[1]"},
        ],
    },
    "门牌号": {
        "candidates": [
            {"css": "#doorNum"},
            {"placeholder": "请输入门牌号"},
        ],
    },
    "最终确定": {
        "candidates": [
            {"css": ".ant-modal-footer .ant-btn-primary", "has_text": "确定"},
            {"css": ".ant-modal .ant-btn-primary"},
            {"role": "button", "name_re": "确定|确认"},
        ],
    },
}


def sss_user_locators_path():
    """Return the user-side 闪时送 locator override file, independent of install."""
    return user_data_dir() / "sss_locators.json"


def load_sss_locators() -> Dict[str, Any]:
    """Return the 闪时送 locator table, preferring the user-side override.

    Mirrors :func:`load_locators`: a malformed override is left untouched and
    the built-in defaults are used instead; on first run the default table is
    published as an editable template to ``sss_locators.json``.
    """
    target = sss_user_locators_path()
    if target.exists():
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (OSError, ValueError):
            pass
        return _copy(SSS_LOCATORS)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(SSS_LOCATORS, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return _copy(SSS_LOCATORS)
