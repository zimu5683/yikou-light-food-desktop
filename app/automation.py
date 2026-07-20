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
    from .processing import (
        get_address_base_sheet_name,
        get_donghu_address_segment,
        get_yijin_address_from_product_note,
        parse_receiver_info,
    )
except ImportError:  # pragma: no cover - allows ``python app/automation.py``
    from models import MealInfo, OrderInfo
    from processing import (
        get_address_base_sheet_name,
        get_donghu_address_segment,
        get_yijin_address_from_product_note,
        parse_receiver_info,
    )

REG_MEAL_COUNT = re.compile(r"x\s*(\d+)", re.I)
REG_MEAL_SPLIT = re.compile(r"（午餐）|（晚餐）")
MAX_PAGE_SEARCH = 20
SHEET_MEAL_SUFFIX = {"午餐": "中餐", "晚餐": "晚餐"}
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class BrowserNotFoundError(RuntimeError):
    """Raised when no supported system browser is available."""

    def __init__(self, browsers: dict[str, str | None] | None = None) -> None:
        self.browsers = browsers or detect_browsers()
        super().__init__("未检测到可用浏览器，请安装 Microsoft Edge/Google Chrome，或安装 Playwright Chromium")


def detect_browsers() -> dict[str, str | None]:
    """Find system browsers and an already-installed Playwright Chromium."""
    candidates: dict[str, list[Path]] = {"msedge": [], "chrome": [], "chromium": []}
    for name in candidates:
        found = shutil.which(name) or shutil.which(name + ".exe")
        if found:
            candidates[name].append(Path(found))
    if os.name == "nt":
        roots = [Path(os.environ.get(k, "")) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        candidates["msedge"] += [root / "Microsoft/Edge/Application/msedge.exe" for root in roots if str(root)]
        candidates["chrome"] += [root / "Google/Chrome/Application/chrome.exe" for root in roots if str(root)]
    elif sys.platform == "darwin":
        candidates["msedge"] += _macos_browser_paths("msedge")
        candidates["chrome"] += _macos_browser_paths("chrome")
    chromium = _playwright_chromium_path()
    if chromium:
        candidates["chromium"].append(chromium)
    return {name: next((str(path) for path in paths if path.is_file()), None) for name, paths in candidates.items()}


def _macos_browser_paths(browser: str) -> list[Path]:
    app_roots = (Path("/Applications"), Path.home() / "Applications")
    app_name, executable = {
        "msedge": ("Microsoft Edge.app", "Microsoft Edge"),
        "chrome": ("Google Chrome.app", "Google Chrome"),
    }[browser]
    return [root / app_name / "Contents" / "MacOS" / executable for root in app_roots]


def _playwright_chromium_path() -> Path | None:
    """Return Playwright's Chromium executable when the browser payload exists."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            path = Path(playwright.chromium.executable_path)
        return path if path.is_file() else None
    except Exception:
        return None


def _emit(callback: Callable[[str], Any] | None, message: str) -> None:
    if callback:
        callback(message)


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


def ensure_browser(mode: str = "auto", allow_install: bool = True) -> str:
    """Return a usable browser, installing Playwright Chromium when needed."""
    mode = (mode or "auto").lower()
    found = detect_browsers()
    if mode in {"msedge", "edge"}:
        if found["msedge"]:
            return "msedge"
        raise BrowserNotFoundError(found)
    if mode in {"chrome", "google-chrome"}:
        if found["chrome"]:
            return "chrome"
        raise BrowserNotFoundError(found)
    if found["msedge"]:
        return "msedge"
    if found["chrome"]:
        return "chrome"
    if found.get("chromium"):
        return "chromium"
    if allow_install and not getattr(sys, "frozen", False):
        try:
            _install_chromium()
        except Exception as exc:
            error = BrowserNotFoundError(found)
            error.__cause__ = exc
            raise error from exc
        found = detect_browsers()
        if found.get("chromium"):
            return "chromium"
    raise BrowserNotFoundError(found)


def _install_chromium() -> None:
    """Install Chromium using Playwright's bundled Node driver.

    Calling ``sys.executable -m playwright`` does not work from a PyInstaller
    executable because ``sys.executable`` points back to the application.  The
    driver and CLI are included by the PyInstaller Playwright hook, so invoking
    them directly works in both source and packaged builds.
    """
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    driver, cli = compute_driver_executable()
    subprocess.run(
        [driver, cli, "install", "chromium"],
        check=True,
        env=get_driver_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _launch_browser(playwright: Any, mode: str, headless: bool) -> Any:
    preferred = ensure_browser(mode, allow_install=(str(mode or "auto").lower() == "auto"))
    kwargs = {"headless": headless, "args": ["--window-size=1300,900"]}
    executable = detect_browsers().get(preferred)
    if not executable:
        raise BrowserNotFoundError()
    return playwright.chromium.launch(executable_path=executable, **kwargs)


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
    target_name = f"{base}{SHEET_MEAL_SUFFIX.get(meal_type, meal_type)}"
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


def _load_order_workbook(excel_path: Path, loader: Callable[..., Any] | None = None) -> Any:
    if loader is None:
        from openpyxl import load_workbook
        loader = load_workbook
    return loader(excel_path, keep_vba=excel_path.suffix.lower() == ".xlsm")


def run_job(config: Any, order_count: int, stop_event: Any, progress_callback: Callable[[str], Any] | None = None, password: str | None = None,
            order_decision_callback: Callable[[str, str], str] | None = None,
            save_decision_callback: Callable[[str], str] | None = None) -> dict[str, int]:
    """Process the newest W orders and append their meals to the workbook."""
    configured_excel = getattr(config, "excel_path", None)
    if not configured_excel:
        raise FileNotFoundError("尚未选择 Excel 文件")
    excel_path = Path(configured_excel)
    if not excel_path.is_file():
        raise FileNotFoundError(f"Excel 文件不存在: {excel_path}")
    if excel_path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError("仅支持 .xlsx 和 .xlsm Excel 文件")
    if order_count < 1:
        raise ValueError("order_count 必须大于等于 1")
    if password is None:
        password = getattr(config, "password", "")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright，请先安装 requirements.txt") from exc
    backup_dir = excel_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    shutil.copy2(excel_path, backup_dir / f"{excel_path.stem}_{stamp}{excel_path.suffix}")
    # Preserve embedded VBA when the user explicitly selects an .xlsm file.
    wb = _load_order_workbook(excel_path)
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
                page.locator('div.detail:has-text("门店地址")').dblclick()
                page.wait_for_url("**/home", timeout=timeout)
                page.locator('div.navBarItem:has-text("订单")').click()
                page.wait_for_url("**/order/**", timeout=timeout)
                page.locator("text=外送订单").click()
                page.wait_for_load_state("networkidle")
                _wait_for_order_table(page, timeout)
                _emit(progress_callback, "登录成功，开始搜索订单")
                for number in range(order_count, 0, -1):
                    if stop_event.is_set():
                        break
                    code = f"W{number}"
                    order: OrderInfo | None = None
                    while not stop_event.is_set():
                        _emit(progress_callback, f"搜索 {code}")
                        try:
                            cell = _find_order_cell(page, code, config, progress_callback)
                            cell.locator("xpath=ancestor::tr").locator("text=详情").first.click()
                            page.wait_for_timeout(300)
                            receiver = _label(page, "收货人", timeout)
                            name, phone = parse_receiver_info(receiver)
                            address = _label(page, "配送地址", timeout)
                            base = get_address_base_sheet_name(address)
                            if base == "东湖":
                                address = get_donghu_address_segment(address)
                            elif base == "衣锦":
                                address = get_yijin_address_from_product_note(extract_product_note_text(page))
                            candidate = OrderInfo(code, name, phone, address, base, delivery_address=address)
                            for typ, attr in (("午餐", "lunch"), ("晚餐", "dinner")):
                                setattr(candidate, attr, extract_meal_info(page, typ))

                            # Do not mutate the workbook until the browser has
                            # safely returned to the order list.  A retry can
                            # therefore never duplicate partially written rows.
                            page.go_back()
                            page.wait_for_load_state("networkidle")
                            _wait_for_order_table(page, timeout)
                            order = candidate
                            break
                        except Exception as exc:
                            _recover_order_table(page, timeout)
                            decision = "skip"
                            if order_decision_callback:
                                decision = order_decision_callback(code, str(exc)).lower()
                            if decision == "retry":
                                _emit(progress_callback, f"重试 {code}")
                                continue
                            if decision == "stop":
                                stop_event.set()
                                break
                            _emit(progress_callback, f"{code} 未找到，跳过：{exc}")
                            break

                    if stop_event.is_set():
                        break
                    processed += 1
                    if order is None:
                        continue
                    for typ, meals in (("午餐", order.lunch), ("晚餐", order.dinner)):
                        for meal in meals:
                            for _ in range(max(1, meal.count)):
                                _write_order(wb, order, meal, typ)
                    meal_text = _format_order_meals(order)
                    _emit(progress_callback, (
                        f"订单详情：订单号={order.order_no}，姓名={order.name or '未填写'}，"
                        f"电话={order.phone or '未填写'}，地址={order.address or '未填写'}，餐品={meal_text}"
                    ))
                    found += 1
            finally:
                browser.close()
        _save_workbook_with_retry(wb, excel_path, save_decision_callback)
    finally:
        wb.close()
    _emit(progress_callback, f"处理完成：找到 {found}/{processed} 个订单")
    return {"processed": processed, "found": found}


def _save_workbook_with_retry(workbook: Any, excel_path: Path,
                              decision_callback: Callable[[str], str] | None = None) -> None:
    """Save an Excel workbook, allowing the user to close a locked file and retry."""
    while True:
        try:
            workbook.save(str(excel_path))
            return
        except PermissionError as exc:
            if decision_callback is None:
                raise
            decision = decision_callback(str(exc)).strip().lower()
            if decision not in {"retry", "重试", "再次保存"}:
                raise PermissionError(f"已取消保存 Excel 文件：{excel_path}") from exc


def _format_order_meals(order: OrderInfo) -> str:
    parts: list[str] = []
    for meal_type, meals in (("午餐", order.lunch), ("晚餐", order.dinner)):
        for meal in meals:
            grade = meal.grade or "未标注"
            total = f"{meal.total_meals}餐" if meal.total_meals else "餐品"
            parts.append(f"{meal_type}{grade}{total} x{meal.count}")
    return "、".join(parts) if parts else "未识别"


def _wait_for_order_table(page: Any, timeout: int) -> None:
    """Wait for the SPA to render at least one order row."""
    page.wait_for_load_state("domcontentloaded", timeout=timeout)
    page.locator(".el-table__body-wrapper tbody tr:visible, table tbody tr:visible").first.wait_for(
        state="visible", timeout=timeout
    )


def _recover_order_table(page: Any, timeout: int) -> bool:
    """Best-effort recovery after a detail-page or rendering failure."""
    try:
        _wait_for_order_table(page, min(timeout, 1200))
        return True
    except Exception:
        try:
            page.go_back()
            page.wait_for_load_state("domcontentloaded", timeout=timeout)
            _wait_for_order_table(page, timeout)
            return True
        except Exception:
            return False


def _find_order_cell(page: Any, code: str, config: Any, callback: Callable[[str], Any] | None) -> Any:
    """Find an exact order number, retrying renders and traversing pages."""
    timeout = max(1000, int(getattr(config, "order_search_timeout_ms", 8000)))
    pause = max(200, int(getattr(config, "retry_wait_ms", 1000)))
    attempts = max(1, int(getattr(config, "order_search_attempts", 3)))
    max_pages = max(1, int(getattr(config, "max_page_search", MAX_PAGE_SEARCH)))

    # Searches should always start from page one; after returning from details
    # the SPA may otherwise leave the pagination on the previous order's page.
    first_page = page.locator('.el-pagination li.number').filter(has_text=re.compile(r'^\s*1\s*$')).first
    try:
        if first_page.is_visible() and "active" not in (first_page.get_attribute("class") or ""):
            first_page.click()
            page.wait_for_timeout(pause)
    except Exception:
        pass

    last_error: Exception | None = None
    for page_number in range(1, max_pages + 1):
        cell = page.locator('.el-table__body-wrapper tbody tr:visible td, table tbody tr:visible td').filter(
            has_text=re.compile(rf'^\s*{re.escape(code)}\s*$')
        ).first
        for attempt in range(1, attempts + 1):
            try:
                if cell.count() > 0:
                    cell.wait_for(state="visible", timeout=timeout)
                    return cell
                raise LookupError(f"订单 {code} 不在第 {page_number} 页")
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    _emit(callback, f"{code} 页面仍在刷新，{pause / 1000:g} 秒后重试 ({attempt}/{attempts - 1})")
                    page.wait_for_timeout(pause)

        if page_number >= max_pages:
            break
        next_button = page.locator('.el-pagination button.btn-next, button.btn-next').first
        try:
            disabled = next_button.is_disabled() or next_button.get_attribute("disabled") is not None
            disabled = disabled or "disabled" in (next_button.get_attribute("class") or "").lower()
            if disabled:
                break
            _emit(callback, f"{code} 当前页未找到，继续搜索第 {page_number + 1} 页")
            next_button.click()
            page.wait_for_timeout(pause)
            _wait_for_order_table(page, timeout)
        except Exception as exc:
            last_error = exc
            break
    raise last_error or TimeoutError(f"订单 {code} 未找到")


__all__ = ["run_job", "ensure_browser", "detect_browsers", "BrowserNotFoundError", "parse_receiver_info", "parse_meal_rows", "extract_meal_info", "extract_product_note_text", "get_yijin_address_from_product_note", "get_address_base_sheet_name", "get_donghu_address_segment"]
