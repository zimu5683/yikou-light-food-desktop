"""闪时送（sss）平台批量下单自动化。

本模块与管理后台订单处理（:mod:`app.automation`）方向相反：从独立的
《闪时送.xlsx》读取订单（午餐/晚餐两表），再在闪时送平台逐单创建预约单。

登录页存在验证码，程序只自动填写账号密码并等待用户手动完成验证码、点击登录，
随后检测到「创建订单」按钮出现即视为登录成功，再继续自动下单。凭据与路径由
GUI 通过 :class:`AppConfig` 传入，不写入源码；定位器沿用候选链机制，改版时编辑
``sss_locators.json`` 即可适配，无需改代码。
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Callable

try:
    from .automation import (
        BrowserNotFoundError,
        LocatorError,
        _emit,
        _find_by_candidates,
        _launch_browser,
        _locator_failure,
    )
    from .locators import SSS_LOCATORS, load_sss_locators
except ImportError:  # pragma: no cover - allows ``python app/sss.py``
    from automation import (
        BrowserNotFoundError,
        LocatorError,
        _emit,
        _find_by_candidates,
        _launch_browser,
        _locator_failure,
    )
    from locators import SSS_LOCATORS, load_sss_locators

DEFAULT_SHEETS = ("午餐", "晚餐")
LUNCH_TIME = "11:00:00"
DINNER_TIME = "17:00:00"
DEFAULT_SSS_URL = "https://sssplusnew.zhuopaikeji.com/takeout"

# 「地址选项」的候选需要根据 sss_common_address 动态替换文字。
_ADDRESS_OPTION_DEFAULT = {
    "candidates": [
        {"css": ".ant-select-dropdown .ant-select-item-option", "has_text": "{label}"},
        {"text": "{label}"},
    ],
}


def _clean(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def load_sss_orders(excel_path: str | Path, sheets=DEFAULT_SHEETS) -> dict[str, list[dict[str, Any]]]:
    """读取《闪时送.xlsx》的订单（A=姓名 B=门牌号 C=电话 D=送达时间）。

    每张工作表从第 3 行开始，遇到 A/B/C 三列均为空的行即终止。返回
    ``{工作表名: [订单字典, ...]}``，每个订单含 ``row`` 与四列原始值。
    """
    from openpyxl import load_workbook

    wb = load_workbook(excel_path, data_only=True)
    result: dict[str, list[dict[str, Any]]] = {}
    try:
        for sheet_name in sheets:
            if sheet_name not in wb.sheetnames:
                continue
            ws = wb[sheet_name]
            orders: list[dict[str, Any]] = []
            row_num = 3
            while True:
                name = _clean(ws[f"A{row_num}"].value)
                door = _clean(ws[f"B{row_num}"].value)
                phone = _clean(ws[f"C{row_num}"].value)
                delivery_time = ws[f"D{row_num}"].value
                if not any([name, door, phone]):
                    break
                orders.append({
                    "row": row_num,
                    "name": name,
                    "door": door,
                    "phone": phone,
                    "delivery_time": delivery_time,
                })
                row_num += 1
            result[sheet_name] = orders
        return result
    finally:
        wb.close()


def compute_delivery_time(is_dinner: bool, now: _dt.datetime | None = None) -> str:
    """按原脚本规则计算送达时间：午餐 11:00 / 晚餐 17:00，16 点后顺延次日。"""
    now = now or _dt.datetime.now()
    target_date = now
    hour = now.hour
    if 20 <= hour < 24 or 16 <= hour < 20:  # 原脚本的等价写法：16~23 点顺延次日
        target_date = now + _dt.timedelta(days=1)
    time_str = DINNER_TIME if is_dinner else LUNCH_TIME
    return target_date.strftime("%Y-%m-%d ") + time_str


def _resolve(locators: dict[str, Any] | None, name: str) -> dict[str, Any]:
    """Return a step dict, falling back to the built-in 闪时送 default."""
    return (locators or {}).get(name) or SSS_LOCATORS.get(name) or {}


def _substitute_tokens(step: dict[str, Any], **tokens: str) -> dict[str, Any]:
    """Deep-copy a step and replace ``{token}`` placeholders in candidate strings."""
    copied = json.loads(json.dumps(step))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if isinstance(value, str):
                    for token, replacement in tokens.items():
                        value = value.replace("{" + token + "}", replacement)
                    node[key] = value
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(copied)
    return copied


def _address_option_step(locators: dict[str, Any] | None, label: str) -> dict[str, Any]:
    step = _resolve(locators, "地址选项") or _ADDRESS_OPTION_DEFAULT
    return _substitute_tokens(step, label=str(label))


def _click(page: Any, step: dict[str, Any], name: str, timeout: int,
           callback: Callable[[str], Any] | None) -> Any:
    element, index = _find_by_candidates(page, step, timeout)
    if element is None:
        raise _locator_failure(page, name)
    element.click(timeout=timeout)
    _emit(callback, f"{name}：第 {index + 1} 个定位候选命中")
    return element


def _type_into(page: Any, step: dict[str, Any], name: str, value: Any, timeout: int,
               callback: Callable[[str], Any] | None) -> Any:
    element, _ = _find_by_candidates(page, step, timeout)
    if element is None:
        raise _locator_failure(page, name)
    text = "" if value is None else str(value)
    element.click(timeout=timeout)
    try:
        element.fill(text)
    except Exception:
        element.press("Control+a")
        element.press("Delete")
        element.press_sequentially(text, timeout=timeout)
    _emit(callback, f"{name} 已填入：{text}")
    return element


def _fill_delivery_time(page: Any, step: dict[str, Any], is_dinner: bool, timeout: int,
                        callback: Callable[[str], Any] | None) -> None:
    element, _ = _find_by_candidates(page, step, timeout)
    if element is None:
        raise _locator_failure(page, "送达时间输入框")
    value = compute_delivery_time(is_dinner)
    element.click(timeout=timeout)
    try:
        element.fill(value)
    except Exception:
        element.press("Control+a")
        element.press("Delete")
        element.press_sequentially(value, timeout=timeout)
    # 触发 React/Ant Design 受控组件更新（原脚本同样派发了 input/change/blur）。
    element.evaluate(
        "el => ['input','change','blur'].forEach(t => el.dispatchEvent(new Event(t, {bubbles:true, cancelable:true})))"
    )
    _emit(callback, f"送达时间已填入：{value}")


def _wait_for_login(page: Any, locators: dict[str, Any] | None, stop_event: Any,
                    callback: Callable[[str], Any] | None) -> None:
    """等待用户在浏览器中手动完成验证码并点击登录。"""
    _emit(callback, "已填写账号密码，请在浏览器中手动完成验证码并点击登录…")
    step = _resolve(locators, "创建订单")
    while not stop_event.is_set():
        element, _ = _find_by_candidates(page, step, 1000)
        if element is not None:
            _emit(callback, "检测到登录成功，开始下单")
            return
        page.wait_for_timeout(500)


def _wait_modal_close(page: Any, locators: dict[str, Any] | None, timeout: int,
                      callback: Callable[[str], Any] | None) -> None:
    """等待下单弹窗关闭、页面恢复可继续下单的状态。"""
    try:
        page.locator(".ant-modal-mask").first.wait_for(state="hidden", timeout=min(max(timeout, 1), 5000))
    except Exception:
        pass
    step = _resolve(locators, "创建订单")
    element, _ = _find_by_candidates(page, step, timeout)
    if element is None:
        raise _locator_failure(page, "下单后恢复的创建订单按钮")
    _emit(callback, "订单创建完成，页面已就绪")


def _recover_sss_page(page: Any, locators: dict[str, Any] | None, timeout: int,
                      callback: Callable[[str], Any] | None) -> None:
    """下单失败后的兜底恢复：先关掉可能残留的弹窗，再回到可下单状态。

    与订单处理侧的 :func:`_recover_order_table` 对应。全程 best-effort：
    恢复不成功时留给随后的重试流程自行报错，不在此处打断用户决策。
    """
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass
    if _find_by_candidates(page, _resolve(locators, "创建订单"), timeout)[0] is not None:
        return
    try:
        page.reload(timeout=timeout, wait_until="domcontentloaded")
        page.wait_for_timeout(500)
    except Exception:
        pass


def _create_one_order(page: Any, locators: dict[str, Any] | None, order: dict[str, Any],
                      is_dinner: bool, config: Any, timeout: int,
                      callback: Callable[[str], Any] | None) -> None:
    """在闪时送平台按脚本流程为单个订单创建预约单。"""
    name = order.get("name") or ""
    door = order.get("door") or ""
    phone = order.get("phone") or ""
    product_name = str(getattr(config, "sss_product_name", "轻食") or "轻食")
    common_address = str(getattr(config, "sss_common_address", "嗯哼") or "嗯哼")

    _click(page, _resolve(locators, "创建订单"), "创建订单", timeout, callback)

    modal, _ = _find_by_candidates(page, _resolve(locators, "订单弹窗"), timeout)
    if modal is None:
        raise _locator_failure(page, "创建订单弹窗（即时单）")

    _click(page, _resolve(locators, "订单类型下拉"), "订单类型下拉", timeout, callback)
    _click(page, _resolve(locators, "预约单选项"), "选择预约单", timeout, callback)
    _click(page, _resolve(locators, "分单下拉"), "分单下拉", timeout, callback)
    _click(page, _resolve(locators, "一口轻食选项"), "选择一口轻食", timeout, callback)

    _click(page, _resolve(locators, "常用地址"), "常用地址", timeout, callback)
    _click(page, _address_option_step(locators, common_address), "选择常用地址", timeout, callback)
    _click(page, _resolve(locators, "地址确定"), "地址确定", timeout, callback)

    _fill_delivery_time(page, _resolve(locators, "送达时间输入"), is_dinner, timeout, callback)
    _click(page, _resolve(locators, "时间确定"), "时间确定", timeout, callback)

    _type_into(page, _resolve(locators, "顾客姓名"), "顾客姓名", name, timeout, callback)
    _type_into(page, _resolve(locators, "顾客电话"), "顾客电话", phone, timeout, callback)
    _type_into(page, _resolve(locators, "商品名称"), "商品名称", product_name, timeout, callback)
    _type_into(page, _resolve(locators, "门牌号"), "门牌号", door, timeout, callback)

    _click(page, _resolve(locators, "最终确定"), "最终确定", timeout, callback)
    _wait_modal_close(page, locators, timeout, callback)


def run_sss_job(config: Any, stop_event: Any,
                progress_callback: Callable[[str], Any] | None = None,
                password: str | None = None,
                decision_callback: Callable[[str, str], str] | None = None,
                locators: dict[str, Any] | None = None) -> dict[str, int]:
    """读取《闪时送.xlsx》并在闪时送平台逐单创建预约单。

    ``decision_callback(identifier, error)`` 返回 ``retry``/``skip``/``stop``，
    用于单个订单创建失败时的交互决策（与订单处理的 order_decision 一致）。
    """
    configured_excel = getattr(config, "sss_excel_path", None)
    if not configured_excel:
        raise FileNotFoundError("尚未选择闪时送 Excel 文件")
    excel_path = Path(configured_excel)
    if not excel_path.is_file():
        raise FileNotFoundError(f"Excel 文件不存在: {excel_path}")
    if excel_path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError("仅支持 .xlsx 和 .xlsm Excel 文件")
    if password is None:
        password = ""
    if locators is None:
        locators = load_sss_locators()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright，请先安装 requirements.txt") from exc

    orders_by_sheet = load_sss_orders(excel_path)
    total = sum(len(orders) for orders in orders_by_sheet.values())
    if total == 0:
        _emit(progress_callback, "闪时送 Excel 中没有任何订单")
        return {"processed": 0, "created": 0}

    timeout = int(getattr(config, "element_timeout_ms", 8000))
    url = str(getattr(config, "sss_url", "") or "").strip() or DEFAULT_SSS_URL
    account = str(getattr(config, "sss_account", "") or "")
    processed = 0
    created = 0

    with sync_playwright() as playwright:
        browser = _launch_browser(
            playwright,
            getattr(config, "browser_mode", "auto"),
            bool(getattr(config, "headless", False)),
        )
        page = browser.new_page()
        try:
            _emit(progress_callback, "正在打开闪时送登录页…")
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")

            # 若登录页默认是扫码/验证码登录，先切换到账户密码登录；已在该页则跳过。
            try:
                _click(page, _resolve(locators, "登录切换"), "账户密码登录", timeout, progress_callback)
            except Exception:
                pass

            account_input, _ = _find_by_candidates(page, _resolve(locators, "账号输入框"), timeout)
            if account_input is None:
                raise _locator_failure(page, "账号输入框")
            account_input.fill(account)

            password_input, _ = _find_by_candidates(page, _resolve(locators, "密码输入框"), timeout)
            if password_input is None:
                raise _locator_failure(page, "密码输入框")
            password_input.fill(password)

            _wait_for_login(page, locators, stop_event, progress_callback)
            if stop_event.is_set():
                return {"processed": processed, "created": created}

            for sheet_name in DEFAULT_SHEETS:
                orders = orders_by_sheet.get(sheet_name, [])
                if not orders:
                    continue
                is_dinner = sheet_name == "晚餐"
                _emit(progress_callback, f"开始处理【{sheet_name}】表，共 {len(orders)} 单")
                for order in orders:
                    if stop_event.is_set():
                        break
                    processed += 1
                    identifier = f"第 {order['row']} 行 {order.get('name') or '未填写'}"
                    _emit(progress_callback, f"正在创建【{sheet_name}】{identifier}")
                    try:
                        _create_one_order(page, locators, order, is_dinner, config, timeout, progress_callback)
                        created += 1
                        _emit(progress_callback, f"订单创建成功：{identifier}")
                    except Exception as exc:
                        _emit(progress_callback, f"订单创建失败：{identifier}：{exc}")
                        if decision_callback is not None:
                            decision = decision_callback(identifier, str(exc)).lower()
                            if decision == "retry":
                                _recover_sss_page(page, locators, timeout, progress_callback)
                                try:
                                    _create_one_order(page, locators, order, is_dinner, config, timeout, progress_callback)
                                    created += 1
                                    _emit(progress_callback, f"重试成功：{identifier}")
                                except Exception as exc2:
                                    _emit(progress_callback, f"重试仍失败：{identifier}：{exc2}")
                            elif decision == "stop":
                                stop_event.set()
                                break
                if stop_event.is_set():
                    break
        finally:
            browser.close()

    _emit(progress_callback, f"闪时送下单完成：成功 {created}/{processed}")
    return {"processed": processed, "created": created}


__all__ = ["run_sss_job", "load_sss_orders", "compute_delivery_time", "SSS_LOCATORS",
           "BrowserNotFoundError", "LocatorError"]
