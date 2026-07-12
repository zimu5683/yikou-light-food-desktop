"""Pure parsing and Excel-processing helpers.

Browser code can call these functions, while tests can exercise them without a
network connection or a running Playwright instance.
"""
from __future__ import annotations

import datetime as _dt
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .models import MealInfo, OrderInfo

RECEIVER_BRACKET = re.compile(r"^\s*(.+?)\s*[（(,，:：]\s*(\d{5,15})\s*[）),，:：]?\s*$")
NUMBERS = re.compile(r"\d+")
MEAL_COUNT = re.compile(r"x\s*(\d+)", re.IGNORECASE)

# Canonical sheet names used by the original workbook.  English aliases make
# the parser usable with test fixtures and newly-created workbooks as well.
ADDRESS_SHEET_MAP = {
    "联建": "衣锦", "衣锦": "衣锦", "医学院": "医学院", "东湖": "东湖",
    "lianjian": "衣锦", "yijin": "衣锦", "medical": "医学院", "donghu": "东湖",
}


def split_text_and_number(text: Any) -> Tuple[str, str]:
    if text is None:
        return "", ""
    value = str(text).strip()
    return NUMBERS.sub("", value).strip(), "".join(NUMBERS.findall(value))


def parse_receiver_info(receiver_text: Any) -> Tuple[str, str]:
    """Parse ``姓名(手机号)``/``姓名，手机号`` and unbracketed forms."""
    if not receiver_text:
        return "", ""
    value = str(receiver_text).strip()
    match = RECEIVER_BRACKET.match(value)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return split_text_and_number(value)


def get_address_base_sheet_name(delivery_address: Any) -> Optional[str]:
    value = str(delivery_address or "")
    lower = value.lower()
    for keyword, sheet in ADDRESS_SHEET_MAP.items():
        if keyword in value or keyword in lower:
            return sheet
    # 农林路 orders are routed to 东湖 unless they explicitly mention 联建.
    if "农林" in value and "联建" not in value:
        return "东湖"
    return None


def get_donghu_address_segment(delivery_address: Any) -> str:
    value = str(delivery_address or "")
    match = re.search(r"大西.*?([A-Za-z]+\d+|\d+[A-Za-z]+)", value, re.I)
    if match:
        return match.group(1)
    match = re.search(r"小西.*?([A-Za-z]+\d+|\d+[A-Za-z]+)", value, re.I)
    if match:
        return match.group(1)
    if "大西" in value:
        return "大西"
    if "小西" in value:
        return "小西"
    return value


def get_yijin_address_from_product_note(product_note: Any) -> str:
    value = str(product_note or "")
    return "外卖柜" if "联建门口外卖柜" in value else "校门口"


def parse_meal_text(text: Any, meal_type: Optional[str] = None) -> list[MealInfo]:
    """Parse product text into lunch/dinner entries.

    Both Chinese ``(午餐)`` and English ``(lunch)`` labels are accepted. A
    product line without a label is associated with ``meal_type`` when given.
    """
    value = str(text or "")
    pattern = re.compile(r"(.+?)\s*[（(]\s*(午餐|晚餐|lunch|dinner)\s*[）)]", re.I)
    matches = list(pattern.finditer(value))
    if not matches and meal_type:
        matches = [re.match(r"(.+)", value)] if value.strip() else []
    result: list[MealInfo] = []
    for match in matches:
        if not match:
            continue
        product = match.group(1).strip()
        label = (match.group(2) if match.lastindex and match.lastindex >= 2 else meal_type or "").lower()
        kind = "午餐" if label in ("午餐", "lunch") else "晚餐" if label in ("晚餐", "dinner") else meal_type
        count_match = MEAL_COUNT.search(value)
        count = int(count_match.group(1)) if count_match else 1
        total = 6 if "六餐" in product or "6餐" in product else 1 if "单点" in product else None
        grade = "经济" if "经济" in product else "豪华" if "豪华" in product else None
        result.append(MealInfo(total_meals=total, grade=grade, count=count, meal_type=kind))
    return result


def _merged_ranges(sheet: Any) -> set[str]:
    merged = getattr(sheet, "merged_cells", None)
    ranges = getattr(merged, "ranges", merged or [])
    return {str(cell) for rng in ranges for cell in rng}


def get_first_empty_row(sheet: Any, merged_ranges: set[str] | None = None, start_col: str = "A", minimum: int = 3) -> int:
    merged_ranges = merged_ranges or set()
    for row in range(max(getattr(sheet, "max_row", minimum), minimum), minimum - 1, -1):
        coord = f"{start_col}{row}"
        if coord in merged_ranges:
            continue
        if sheet[coord].value not in (None, "", " "):
            return row + 1
    return minimum


def _order_value(order: OrderInfo | Mapping[str, Any], attr: str, *aliases: str) -> Any:
    if isinstance(order, OrderInfo):
        return getattr(order, attr, "")
    for key in (attr, *aliases):
        if key in order:
            return order[key]
    return ""


def write_order_row(sheet: Any, order: OrderInfo | Mapping[str, Any], meal: MealInfo | Mapping[str, Any], meal_type: str, merged_ranges: set[str] | None = None) -> int:
    """Append one order row; merged cells are left untouched."""
    columns = {
        "午餐": ("A", "B", "C", "D", "E", "F"),
        "晚餐": ("G", "H", "I", "J", "K", "L"),
    }
    cols = columns[meal_type]
    merged_ranges = merged_ranges or _merged_ranges(sheet)
    row = get_first_empty_row(sheet, merged_ranges, cols[0])
    getm = (lambda k, default="": getattr(meal, k, default)) if isinstance(meal, MealInfo) else (lambda k, default="": meal.get(k, default))
    values = [
        _order_value(order, "order_no", "单号"), _order_value(order, "name", "姓名"),
        _order_value(order, "address", "处理后地址"), _order_value(order, "phone", "电话"),
        getm("grade", getm("经济/豪华", "")), getm("total_meals", getm("总餐次", "")),
    ]
    for col, value in zip(cols, values):
        if f"{col}{row}" not in merged_ranges:
            sheet[f"{col}{row}"] = value
    return row


def backup_excel(path: str | Path, backup_dir: str | Path | None = None) -> Path:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(source)
    folder = Path(backup_dir) if backup_dir else source.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    target = folder / f"{source.stem}_{stamp}{source.suffix}"
    shutil.copy2(source, target)
    return target


def save_excel_with_retry(workbook: Any, excel_path: str | Path, retries: int = 1) -> bool:
    for attempt in range(max(0, retries) + 1):
        try:
            workbook.save(str(excel_path))
            return True
        except PermissionError:
            if attempt >= retries:
                return False
        except Exception:
            return False
    return False


def get_weekday_fill_value(now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    current = (now or _dt.datetime.now()).weekday()
    target = names[(current + 1) % 7]
    return {name: (1 if name == target else "") for name in names}
