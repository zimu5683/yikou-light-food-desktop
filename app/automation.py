"""Playwright order automation and Excel integration.

This module contains no credentials or machine-specific paths.  The GUI passes
an :class:`AppConfig`, a password and a cancellation event to ``run_job``.
Network selectors intentionally mirror the current admin site, while parsing
and Excel helpers remain usable in unit tests without Playwright installed.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from .models import MealInfo, OrderInfo
except ImportError:  # pragma: no cover - allows ``python app/automation.py``
    from models import MealInfo, OrderInfo

REG_RECEIVER_BRACKET = re.compile(r"^\s*(.+?)\s*[（(](\d+)[）)]\s*$")
REG_NUMBERS = re.compile(r"\d+")
REG_DAXI = re.compile(r"大西.*?([a-zA-Z]+\d+|\d+[a-zA-Z]+)", re.I)
REG_XIAOXI = re.compile(r"小西.*?([a-zA-Z]+\d+|\d+[a-zA-Z]+)", re.I)
REG_MEAL_COUNT = re.compile(r"x\s*(\d+)", re.I)
REG_MEAL_SPLIT = re.compile(r"（午餐）|（晚餐）")
MAX_PAGE_SEARCH = 20
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
ADDRESS_SHEET_MAP = {"联建": "衣锦", "衣锦": "衣锦", "医学院": "医学院", "东湖": "东湖", "农林": "东湖"}


def _emit(callback: Callable[[str], Any] | None, message: str) -> None:
    if callback:
        callback(message)


def parse_receiver_info(text: str | None) -> tuple[str, str]:
    if not text:
        return "", ""
    match = REG_RECEIVER_BRACKET.match(text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return REG_NUMBERS.sub("", text).strip(), "".join(REG_NUMBERS.findall(text))


def get_address_base_sheet_name(address: str) -> str | None:
    for keyword, sheet in ADDRESS_SHEET_MAP.items():
        if keyword in (address or ""):
            return sheet
    return None


def get_donghu_address_segment(address: str) -> str:
    address = address or ""
    match = REG_DAXI.search(address) or REG_XIAOXI.search(address)
    if match:
        return match.group(1)
    return "大西" if "大西" in address else "小西" if "小西" in address else address


def parse_meal_rows(rows: Iterable[dict[str, str]], meal_type: str) -> list[MealInfo]:
    result: list[MealInfo] = []
    for row in rows:
        product = str(row.get("product", ""))
        quantity = str(row.get("qty", ""))
        segments = REG_MEAL_SPLIT.split(product)
        labels = REG_MEAL_SPLIT.findall(product)
        for index, segment in enumerate(segments[:-1]):
            current = "午餐" if labels[index] == "（午餐）" else "晚餐"
            if current != meal_type or not segment.strip():
                continue
            count_match = REG_MEAL_COUNT.search(quantity)
            result.append(MealInfo(
                total_meals=6 if "六餐" in segment else 1 if "单点" in segment else None,
                grade="经济" if "经济" in segment else "豪华" if "豪华" in segment else None,
                count=int(count_match.group(1)) if count_match else 1,
                meal_type=meal_type,
            ))
    return result


def extract_meal_info(page: Any, meal_type: str) -> list[MealInfo]:
    rows = page.eval_on_selector_all(
        ".table_box tbody tr",
        """rows => rows.map(r => ({product:(r.querySelector('td:nth-child(1)')||{}).innerText||'', qty:(r.querySelector('td:nth-child(3)')||{}).innerText||''}))""",
    )
    return parse_meal_rows(rows or [], meal_type)


def extract_product_note_text(page: Any) -> str:
    """Collect free-form notes rendered below product names."""
    try:
        products = page.eval_on_selector_all(
            ".table_box tbody tr",
            """rows => rows.map(r => (r.querySelector('td:nth-child(1)') || {}).innerText || '')""",
        )
    except Exception:
        return ""
    lines: list[str] = []
    for product in products or []:
        parts = [line.strip() for line in str(product).splitlines() if line.strip()]
        lines.extend(parts[1:])
    return " ".join(lines)


def get_yijin_address_from_product_note(note: str) -> str:
    return "外卖柜" if "联建门口外卖柜" in (note or "") else "校门口"


def ensure_browser(mode: str = "auto") -> str:
    """Return a Playwright channel name, installing Chromium as fallback."""
    mode = (mode or "auto").lower()
    if mode in {"msedge", "edge"}:
        return "msedge"
    if mode == "chromium":
        _install_chromium()
        return "chromium"
    # The channel is preferred even if Edge is not on PATH; Playwright gives a
    # deterministic error and run_job then falls back to bundled Chromium.
    if shutil.which("msedge") or shutil.which("msedge.exe") or sys.platform == "win32":
        return "msedge"
    _install_chromium()
    return "chromium"


def _install_chromium() -> None:
    command = [sys.executable, "-m", "playwright", "install", "chromium"]
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _launch_browser(playwright: Any, mode: str, headless: bool) -> Any:
    preferred = ensure_browser(mode)
    kwargs = {"headless": headless, "args": ["--window-size=1300,900"]}
    if preferred == "msedge":
        try:
            return playwright.chromium.launch(channel="msedge", **kwargs)
        except Exception:
            _install_chromium()
    return playwright.chromium.launch(**kwargs)


def _label(page: Any, label: str, timeout: int) -> str:
    try:
        elem = page.locator(f"text={label}").first
        elem.wait_for(timeout=timeout)
        return elem.locator("..").inner_text().strip().split(label)[-1].lstrip("：:").strip()
    except Exception:
        return ""


def _write_order(wb: Any, order: OrderInfo, meal: MealInfo, meal_type: str) -> None:
    base = order.address_base_sheet
    if not base:
        return
    weekday = WEEKDAYS[(_dt.datetime.now().weekday() + 1) % 7]
    weekday_sheet = wb[weekday] if weekday in wb.sheetnames else wb.create_sheet(weekday)
    target_name = f"{base}{meal_type}"
    target = wb[target_name] if target_name in wb.sheetnames else wb.create_sheet(target_name)
    columns = ("A", "B", "C", "D", "E", "F") if meal_type == "午餐" else ("G", "H", "I", "J", "K", "L")
    row = max(3, weekday_sheet.max_row + 1)
    while weekday_sheet[f"{columns[0]}{row}"].value not in (None, ""):
        row += 1
    values = (order.order_no, order.name, order.address, order.phone, meal.grade or "", meal.total_meals or "")
    for col, value in zip(columns, values):
        weekday_sheet[f"{col}{row}"] = value
    row2 = max(3, target.max_row + 1)
    while target[f"A{row2}"].value not in (None, ""):
        row2 += 1
    vals = [order.order_no, order.name, order.address, order.phone] + [1 if d == weekday else "" for d in WEEKDAYS] + [meal_type, meal.grade or "", meal.total_meals or ""]
    for idx, value in enumerate(vals, 1):
        target.cell(row2, idx).value = value


def run_job(config: Any, order_count: int, stop_event: Any, progress_callback: Callable[[str], Any] | None = None, password: str | None = None) -> dict[str, int]:
    """Process the newest W orders and append their meals to the workbook."""
    excel_path = Path(getattr(config, "excel_path", ""))
    if not excel_path.exists():
        raise FileNotFoundError(f"Excel 文件不存在: {excel_path}")
    if order_count < 1:
        raise ValueError("order_count 必须大于等于 1")
    if password is None:
        password = getattr(config, "password", "")
    from openpyxl import load_workbook
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright，请先安装 requirements.txt") from exc
    backup_dir = excel_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(excel_path, backup_dir / f"{excel_path.stem}_{stamp}{excel_path.suffix}")
    wb = load_workbook(excel_path)
    processed = 0
    found = 0
    timeout = int(getattr(config, "element_timeout_ms", 8000))
    try:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright, getattr(config, "browser_mode", "auto"), bool(getattr(config, "headless", False)))
            page = browser.new_page()
            try:
                _emit(progress_callback, "正在登录...")
                page.goto(getattr(config, "target_url", getattr(config, "url", "")), timeout=timeout, wait_until="networkidle")
                page.locator('input[placeholder="请输入手机号/账号"]').fill(getattr(config, "phone_number", getattr(config, "phone", "")))
                page.locator('input[placeholder="登录密码"]').fill(password or "")
                page.locator("text=立即登录").click()
                page.wait_for_url("**/workbench/store", timeout=timeout)
                page.locator('div.detail:has-text("门店地址")').dblclick(); page.wait_for_url("**/home", timeout=timeout)
                page.locator('div.navBarItem:has-text("订单")').click(); page.wait_for_url("**/order/**", timeout=timeout)
                page.locator("text=外送订单").click(); page.wait_for_load_state("networkidle")
                _emit(progress_callback, "登录成功，开始搜索订单")
                for number in range(order_count, 0, -1):
                    if stop_event.is_set(): break
                    code = f"W{number}"; _emit(progress_callback, f"搜索 {code}")
                    try:
                        cell = page.locator(f'//td[normalize-space()="{code}"]').first
                        cell.wait_for(timeout=int(getattr(config, "order_search_timeout_ms", 1000)))
                        cell.locator("xpath=ancestor::tr").locator("text=详情").first.click()
                        page.wait_for_timeout(300)
                        receiver = _label(page, "收货人", timeout)
                        name, phone = parse_receiver_info(receiver)
                        address = _label(page, "配送地址", timeout)
                        base = get_address_base_sheet_name(address)
                        if base == "东湖": address = get_donghu_address_segment(address)
                        elif base == "衣锦": address = get_yijin_address_from_product_note(extract_product_note_text(page))
                        order = OrderInfo(code, name, phone, address, base, delivery_address=address)
                        for typ, attr in (("午餐", "lunch"), ("晚餐", "dinner")):
                            meals = extract_meal_info(page, typ)
                            setattr(order, attr, meals)
                            for meal in meals:
                                for _ in range(max(1, meal.count)): _write_order(wb, order, meal, typ)
                        found += 1; page.go_back(); page.wait_for_load_state("networkidle")
                    except PlaywrightTimeout:
                        _emit(progress_callback, f"{code} 未找到，跳过")
                    processed += 1
            finally:
                browser.close()
        wb.save(excel_path)
    finally:
        wb.close()
    _emit(progress_callback, f"处理完成：找到 {found}/{processed} 个订单")
    return {"processed": processed, "found": found}


__all__ = ["run_job", "ensure_browser", "parse_receiver_info", "parse_meal_rows", "extract_meal_info", "extract_product_note_text", "get_yijin_address_from_product_note", "get_address_base_sheet_name", "get_donghu_address_segment"]
