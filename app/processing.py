"""Pure parsing and Excel-processing helpers.

Browser code can call these functions, while tests can exercise them without a
network connection or a running Playwright instance.
"""
from __future__ import annotations

import datetime as _dt
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

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


# 校区子表（如「东湖中餐」）的地址排序规则。表名以校区开头、以中餐/晚餐结尾。
_CAMPUS_SHEET_PREFIXES = ("东湖", "衣锦", "医学院")
_CAMPUS_SHEET_SUFFIXES = ("中餐", "晚餐")
_ADDRESS_HEADER_ROWS = 2  # 第 1 行标题、第 2 行表头，数据自第 3 行起


def _sort_key_for_address(address: Any, campus: str) -> tuple[int, int, int]:
    """Return a comparable key for one address cell value.

    东湖：大西 → 小西 → A/B/C/D（按数字升序）→ 其他；
    衣锦：校门口 → 外卖柜 → 其他；
    医学院：医N号（按数字升序）→ 其他。
    """
    value = str(address or "").strip()
    if not value:
        return (9, 0, 0)
    if campus == "东湖":
        if value == "大西":
            return (0, 0, 0)
        if value == "小西":
            return (1, 0, 0)
        match = re.fullmatch(r"([A-Da-d])\s*(\d{1,3})", value)
        if match:
            zone = match.group(1).upper()
            zone_order = "ABCD".index(zone)
            return (2, zone_order, int(match.group(2)))
        return (9, 0, 0)
    if campus == "衣锦":
        if "校门口" in value:
            return (0, 0, 0)
        if "外卖柜" in value:
            return (1, 0, 0)
        return (9, 0, 0)
    if campus == "医学院":
        match = re.fullmatch(r"医(\d{1,3})号", value)
        if match:
            return (0, int(match.group(1)), 0)
        return (9, 0, 0)
    return (9, 0, 0)


def clear_campus_sub_sheets(workbook: Any, log: Callable[[str], Any] | None = None) -> list[str]:
    """Clear all data rows of the six campus sub-sheets before writing.

    仅清空六张校区子表（东湖/衣锦/医学院 × 中餐/晚餐）第 3 行起的数据，
    第 1 行标题与第 2 行表头保持不动。删除整行（而非仅置空单元格），
    否则工作表的 max_row 不会收缩、后续写入仍从旧尾部续写。数据区若有
    合并单元格则退化为逐格置空并提示。返回实际被清空的表名列表。
    """
    if log is None:
        log = lambda _message: None  # noqa: E731
    cleared: list[str] = []

    for ws in workbook.worksheets:
        title = ws.title or ""
        if not any(title.startswith(p) for p in _CAMPUS_SHEET_PREFIXES):
            continue
        if not any(title.endswith(s) for s in _CAMPUS_SHEET_SUFFIXES):
            continue
        if ws.max_row <= _ADDRESS_HEADER_ROWS:
            continue
        data_rows = sum(
            1 for row in range(_ADDRESS_HEADER_ROWS + 1, ws.max_row + 1)
            if any(ws.cell(row, col).value not in (None, "") for col in range(1, ws.max_column + 1))
        )
        if not data_rows:
            continue
        if any(rng.min_row >= _ADDRESS_HEADER_ROWS + 1 for rng in ws.merged_cells.ranges):
            # 罕见：用户在数据区手加了合并，删行会破坏合并关系，退化为逐格置空。
            for row in range(_ADDRESS_HEADER_ROWS + 1, ws.max_row + 1):
                for col in range(1, ws.max_column + 1):
                    cell = ws.cell(row, col)
                    if cell.value in (None, ""):
                        continue
                    try:
                        cell.value = None
                    except AttributeError:
                        continue  # 合并区从格只读，跳过以不断开合并关系。
            cleared.append(title)
            log(f"{title}：已清空旧数据（{data_rows} 行，含合并区仅置空）")
            continue
        ws.delete_rows(_ADDRESS_HEADER_ROWS + 1, ws.max_row - _ADDRESS_HEADER_ROWS)
        cleared.append(title)
        log(f"{title}：已清空旧数据（{data_rows} 行）")

    return cleared


def sort_campus_sub_sheets(workbook: Any, log: Callable[[str], Any] | None = None) -> list[str]:
    """Sort each campus sub-sheet's rows by address after the run finishes.

    仅整理六张校区子表（东湖/衣锦/医学院 × 中餐/晚餐），周表与历史日期表不动。
    扫描整张表第 3 行起**所有**非空行一起排序（中间空行会被压缩掉），
    排好后从第 3 行起连续写回。表头与行样式保持不变。
    返回已整理的表名列表（便于上层写日志）。
    """
    if log is None:
        log = lambda _message: None  # noqa: E731
    sorted_sheets: list[str] = []

    for ws in workbook.worksheets:
        title = ws.title or ""
        campus = next((p for p in _CAMPUS_SHEET_PREFIXES if title.startswith(p)), None)
        if campus is None or not any(title.endswith(s) for s in _CAMPUS_SHEET_SUFFIXES):
            continue
        # 数据区若有合并单元格则跳过（模板/程序写入的校区表无合并，防御用户手改）。
        if any(rng.min_row >= _ADDRESS_HEADER_ROWS + 1 for rng in ws.merged_cells.ranges):
            log(f"{title}：数据区含合并单元格，跳过地址排序")
            continue

        # 收集整张表第 3 行起所有非空行（A 列有值即算一行），不再遇空行停止。
        max_col = ws.max_column
        rows: list[tuple[int, tuple[Any, ...]]] = []
        for row_index in range(_ADDRESS_HEADER_ROWS + 1, ws.max_row + 1):
            if ws.cell(row_index, 1).value in (None, ""):
                continue
            values = tuple(ws.cell(row_index, col).value for col in range(1, max_col + 1))
            rows.append((row_index, values))
        if len(rows) <= 1:
            continue

        # 地址在第 3 列（C）；Python 排序稳定，同组保持原先后顺序。
        ordered = sorted(
            rows,
            key=lambda item: (_sort_key_for_address(item[1][2], campus), item[0]),
        )
        for target_index, (source_index, values) in enumerate(ordered):
            row = _ADDRESS_HEADER_ROWS + 1 + target_index
            for col, value in enumerate(values, start=1):
                ws.cell(row, col).value = value
        # 排好后多余的尾部旧行清掉（中间空行被压缩后，尾部会残留重复数据）。
        tail_start = _ADDRESS_HEADER_ROWS + 1 + len(ordered)
        for row in range(tail_start, ws.max_row + 1):
            for col in range(1, max_col + 1):
                cell = ws.cell(row, col)
                if cell.value not in (None, ""):
                    try:
                        cell.value = None
                    except AttributeError:
                        pass
        sorted_sheets.append(title)
        log(f"{title}：已按地址整理（{len(rows)} 行）")

    return sorted_sheets
