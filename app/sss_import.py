"""闪时送下单前的「云端当天名单」导入（东湖午餐 / 东湖晚餐）。

用户口径（2026-09-15 确认）：

1. **目标日**：用户口径是「9.15 晚上 8 点之后到 9.16 早上 10 点之前，识别的
   日期都是 9.16」——即 ``hour >= wps_target_hour_start``（默认 20）识别次日，
   其余时刻识别运行日（见 :func:`ordering_target_date`）；
2. **名单来源**：云端的「东湖中餐」「东湖晚餐」两张表，取目标日期那一列里标
   ``1`` 的行，姓名 / 地址 / 电话直接进内存用于下单，**不再从《闪时送.xlsx》读单**；
3. **地址过滤**：地址是「大西」或「小」的人**不送闪时送、不下单**（含「小西」、
   带空格等写法变体），其余地址全部下单；
4. **留档**：把实际要下单的名单写一份到《闪时送.xlsx》（先清空 A:C 第 3 行起），
   E1 写云端该日期列的表头原文（形如 ``9.16 周三``）；留档失败只告警，不影响下单；
5. **缺当天列**：某张表没有目标日期列（例如周末不做晚餐）→ 那一餐不下单，
   另一餐照常；
6. **日期闸门**：程序识别的目标日必须等于本次下单真正使用的送达日期
   （午餐 11:00 / 晚餐 17:00，16 点后顺延次日），不一致一律拒绝下单；
7. **拒绝语义**：云端读不到（未授权 / 缺 kdocs-cli / 额度用尽 / 表头异常）或要下单
   的人数据不完整 → 抛 :class:`ImportRefused`，由调用方在登录与提交之前中断。

本模块只依赖 ``openpyxl`` 与 :mod:`app.wps_cloud`，**不导入** :mod:`app.sss`
（避免循环导入）：送达日期等规则由调用方以参数传入。
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    from .wps_cloud import (
        ADDRESS_ALIASES,
        HEADER_ADDRESS,
        HEADER_NAME,
        HEADER_PHONE,
        HEADER_ROW,
        KdocsCli,
        WpsCloudError,
        _address_key,
        _find_column,
        column_name,
        date_region,
        effective_tables,
        find_target_column,
        scan_bounds,
    )
except ImportError:  # pragma: no cover - allows ``python app/sss_import.py``
    from wps_cloud import (  # type: ignore[no-redef]
        ADDRESS_ALIASES,
        HEADER_ADDRESS,
        HEADER_NAME,
        HEADER_PHONE,
        HEADER_ROW,
        KdocsCli,
        WpsCloudError,
        _address_key,
        _find_column,
        column_name,
        date_region,
        effective_tables,
        find_target_column,
        scan_bounds,
    )

# 闪时送工作表 -> 云端排单表（用户口径：闪时送的午餐/晚餐就是东湖的两顿）。
SSS_SHEET_SOURCES: dict[str, str] = {"午餐": "东湖中餐", "晚餐": "东湖晚餐"}
# 这些地址由用户自己送，不进闪时送下单名单（写法变体经归一化后一并命中）。
SKIP_ADDRESSES: tuple[str, ...] = ("大西", "小")
# 《闪时送.xlsx》的写入位置：A/B/C 三列，数据从第 3 行开始；日期写 E1。
COL_NAME, COL_ADDRESS, COL_PHONE = 1, 2, 3
FIRST_DATA_ROW = 3
DATE_ROW, DATE_COL = 1, 5          # E1
CELL_MARK = "1"
_PHONE_RE = re.compile(r"^[0-9]{11}$")
_WHITESPACE_RE = re.compile(r"\s+")
# 拒绝消息里最多列出多少条问题行，避免刷屏。
_MAX_PROBLEM_ROWS = 10


class ImportRefused(RuntimeError):
    """云端名单不可用或日期不匹配：调用方必须在下单之前中断。"""


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------

def _log(log: Callable[[str], Any] | None, message: str, level: str = "INFO") -> None:
    """写日志；兼容只接收一个参数的简单回调。"""
    if log is None:
        return
    try:
        log(message)
    except TypeError:
        try:
            log(message, level)  # type: ignore[call-arg]
        except Exception:
            pass
    except Exception:
        pass


def _clean_text(value: Any) -> str:
    """单元格文本规范化：NFKC（全角转半角）+ 去首尾空白。"""
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def normalise_phone(value: Any) -> str:
    """规范成 11 位 ASCII 数字字符串，拒绝含糊格式。

    与 :func:`app.sss._normalise_phone` 行为一致（这里独立实现是为了避免
    与 ``app.sss`` 形成循环导入）。
    """
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        # openpyxl/接口把整数字段读成 float 时只接受精确整数，防止 .5 被截断。
        if not value.is_integer():
            return ""
        text = str(int(value))
    else:
        text = unicodedata.normalize("NFKC", str(value)).strip()
    return _WHITESPACE_RE.sub("", text)


def address_group(value: Any) -> str:
    """地址归一化键：小西→小、忽略空格与大小写（与云同步地址规则一致）。"""
    text = _clean_text(value)
    text = ADDRESS_ALIASES.get(text, text)
    return _address_key(text)


_SKIP_ADDRESS_KEYS = frozenset(
    _address_key(ADDRESS_ALIASES.get(item, item)) for item in SKIP_ADDRESSES)


def should_skip_address(value: Any) -> bool:
    """地址是否属于「不需要闪时送」的组（大西 / 小）。"""
    key = address_group(value)
    return bool(key) and key in _SKIP_ADDRESS_KEYS


def ordering_target_date(now: _dt.datetime | None = None, *,
                         start_hour: int = 20) -> _dt.date:
    """闪时送下单要识别的「当天」日期。

    用户口径（2026-09-15 原话）：「9.15 的晚上 8 点之后到 9.16 早上 10 点之前，
    程序需要识别的日期是 9.16，然后下 9.16 的中午晚上的单」。因此：

    - ``hour >= start_hour``（晚上 20:00 之后）→ 识别**次日**；
    - 其余时刻（含次日 00:00~10:00 的清晨）→ 识别**运行日**。

    与云同步的 :func:`app.wps_cloud.target_date_for` **刻意不同**：后者在
    ``[0, end_hour)`` 也 +1 天（那是"这一晚该往哪一列写"的问题）；下单必须与
    实际送达日期一致 —— 清晨 9 点下单送的是**当天**的午餐/晚餐，不能顺延。
    """
    now = now or _dt.datetime.now()
    if now.hour >= max(0, min(23, int(start_hour))):
        return (now + _dt.timedelta(days=1)).date()
    return now.date()


def _cloud_error(table: str, exc: BaseException) -> ImportRefused:
    return ImportRefused(
        f"读取云端表「{table}」失败：{exc}。已拒绝下单，避免用隔天名单下单。"
        "可先在「云文档同步」页签完成授权或等额度恢复；"
        "也可以把「名单来源」切到「本地 Excel」，自己把当天名单填进《闪时送.xlsx》后再下单。")


def check_target_day(target: _dt.date, delivery_date: _dt.date) -> None:
    """日期闸门：识别到的目标日必须等于本次下单真正使用的送达日期。"""
    if target == delivery_date:
        return
    raise ImportRefused(
        f"日期不匹配：云端当天名单用的是 {target.month}.{target.day}，"
        f"而本次下单的送达日期是 {delivery_date.month}.{delivery_date.day}。"
        "已拒绝下单（只有 20:00 ~ 次日 10:00 之间运行时两者才会一致，"
        "请在 20:00 之后再跑，或改用「本地 Excel」名单来源）。")


# ----------------------------------------------------------------------
# 云端读取
# ----------------------------------------------------------------------

@dataclass
class MealImport:
    """一餐（午餐/晚餐）的云端当天名单。"""

    meal: str                                   # 午餐 / 晚餐
    table: str = ""                             # 东湖中餐 / 东湖晚餐
    file_id: str = ""
    target: _dt.date | None = None
    date_text: str = ""                         # 云端日期列表头原文；"" = 没有当天列
    marked_total: int = 0                       # 该列标 1 的总人数
    skipped_address: int = 0                    # 其中因地址是大西/小被跳过的人数
    orders: list[dict[str, Any]] = field(default_factory=list)
    skipped: bool = False                       # 没有当天列 / 该表未配置 → 这一餐不下单
    skip_reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def order_count(self) -> int:
        """该餐实际要下单的人数（已剔除被地址过滤掉的人）。"""
        return len(self.orders)


def read_cloud_meal(cli: Any, *, file_id: str, table: str, meal: str,
                    target: _dt.date,
                    log: Callable[[str], Any] | None = None) -> MealImport:
    """读一张云端表的当天列，返回该餐要下单的人（只读，2 次接口调用）。

    - 列位置全部按表头文字定位（协作者常插列，不能写死列号）；
    - 日期列只在「电话列右一列 ~ 第一个结构列左一列」之间找，备注右侧那句
      「9.16 周三」是协作者的标记，绝不当成日期列；
    - 地址归一化后属于「大西/小」的行计入 ``skipped_address``，不进下单名单。
    """
    result = MealImport(meal=meal, table=table, file_id=file_id, target=target)
    try:
        infos = cli.sheets_info(file_id)
    except WpsCloudError as exc:
        raise _cloud_error(table, exc) from exc
    if not infos:
        raise ImportRefused(
            f"云端表「{table}」不可读或不是在线表格（file_id={file_id}）。"
            "已拒绝下单；可改用「本地 Excel」名单来源人工下单。")
    info = infos[0]
    worksheet_id = int(info.get("sheetId") or 1)
    row_to, col_to = scan_bounds(info)
    try:
        grid = cli.read_grid(file_id, worksheet_id, 0, row_to, 0, col_to)
    except WpsCloudError as exc:
        raise _cloud_error(table, exc) from exc

    header = {col: text for (row, col), text in grid.items() if row == HEADER_ROW - 1}
    name_col = _find_column(header, HEADER_NAME)
    phone_col = _find_column(header, HEADER_PHONE)
    if name_col is None or phone_col is None:
        missing = []
        if name_col is None:
            missing.append("名字/姓名")
        if phone_col is None:
            missing.append("电话")
        raise ImportRefused(
            f"云端表「{table}」表头异常：找不到「{'、'.join(missing)}」列，"
            "已拒绝下单（请人工核对云端表结构，或改用「本地 Excel」名单来源）。")
    addr_col = _find_column(header, HEADER_ADDRESS) or COL_ADDRESS
    columns = {"name": name_col, "address": addr_col, "phone": phone_col}

    date_lo, date_hi = date_region(columns)
    found = find_target_column(header, target, col_from=date_lo, col_to=date_hi)
    if not found:
        result.skipped = True
        result.skip_reason = (
            f"云端表「{table}」没有 {target.month}.{target.day} 这一列，"
            "当天不送这一餐（周末不做晚餐时属正常）")
        _log(log, f"云名单：{result.skip_reason}")
        return result
    target_col, date_text = found[0] + 1, str(found[1]).strip()
    result.date_text = date_text

    seen: set[tuple[str, str]] = set()
    for row0 in range(FIRST_DATA_ROW - 1, row_to + 1):
        if _clean_text(grid.get((row0, target_col - 1), "")) != CELL_MARK:
            continue
        result.marked_total += 1
        name = _clean_text(grid.get((row0, name_col - 1), ""))
        door = _clean_text(grid.get((row0, addr_col - 1), ""))
        phone = normalise_phone(grid.get((row0, phone_col - 1), ""))
        if should_skip_address(door):
            result.skipped_address += 1
            continue
        key = (name, phone)
        if key in seen:
            result.warnings.append(
                f"{table} 第 {row0 + 1} 行：{name or '（无姓名）'} 重复标 1，已去重")
            continue
        seen.add(key)
        result.orders.append({
            "row": row0 + 1,          # 云端行号，便于日志与报错定位
            "name": name,
            "door": door,
            "phone": phone,
            "source": table,
            # 报错时指出"哪一格、现在是什么"，用户可直接照着去云端改。
            "name_cell": f"{column_name(name_col)}{row0 + 1}",
            "address_cell": f"{column_name(addr_col)}{row0 + 1}",
            "phone_cell": f"{column_name(phone_col)}{row0 + 1}",
            "raw_phone": _clean_text(grid.get((row0, phone_col - 1), "")),
        })
    return result


def validate_orders(meals: list[MealImport]) -> list[str]:
    """校验**要下单的人**（被地址过滤掉的人不参与），返回问题行描述。

    描述里带上**云端单元格地址与当前值**（如 ``C106「0」``），用户可以直接照着
    去云端改，不必再来回排查"到底哪一格不对"。
    """
    problems: list[str] = []
    for meal in meals:
        for order in meal.orders:
            row = order.get("row", "?")
            name = order.get("name") or "无姓名"
            issues = []
            if not order.get("name"):
                issues.append(f"{order.get('name_cell') or '姓名格'} 是空的")
            if not order.get("door"):
                issues.append(f"{order.get('address_cell') or '地址格'} 是空的")
            if not _PHONE_RE.fullmatch(str(order.get("phone") or "")):
                raw = str(order.get("raw_phone") or "").strip()
                shown = f"现在是「{raw}」" if raw else "是空的"
                issues.append(
                    f"{order.get('phone_cell') or '电话格'} {shown}，不是 11 位手机号")
            if issues:
                problems.append(f"{meal.table} 第 {row} 行 {name}：" + "；".join(issues))
    return problems


def collect_day_orders(config: Any, *, target: _dt.date,
                       cli: Any | None = None,
                       log: Callable[[str], Any] | None = None) -> list[MealImport]:
    """读两张云端表（东湖中餐/东湖晚餐）并返回当天要下单的名单。

    读取目标沿用云同步页签的**生效目标**（测试模式下读测试副本）；
    任何读取/校验问题都抛 :class:`ImportRefused`，绝不返回半份名单。
    """
    try:
        tables = effective_tables(config)
    except WpsCloudError as exc:
        raise ImportRefused(
            f"云端表配置不可用：{exc}。已拒绝下单；"
            "请在「云文档同步」页签确认写入目标后再试。") from exc
    if cli is None:
        try:
            cli = KdocsCli(getattr(config, "wps_cli_path", "") or None)
        except WpsCloudError as exc:
            raise ImportRefused(
                f"找不到 kdocs-cli 组件：{exc} 已拒绝下单；"
                "可改用「本地 Excel」名单来源人工下单。") from exc

    meals: list[MealImport] = []
    for meal, table in SSS_SHEET_SOURCES.items():
        conf = tables.get(table) or {}
        file_id = str(conf.get("file_id") or "").strip()
        if not file_id:
            skipped = MealImport(meal=meal, table=table, target=target, skipped=True,
                                 skip_reason=f"云端表「{table}」未配置 file_id，这一餐不下单")
            _log(log, f"云名单：{skipped.skip_reason}")
            meals.append(skipped)
            continue
        meals.append(read_cloud_meal(cli, file_id=file_id, table=table, meal=meal,
                                     target=target, log=log))

    problems = validate_orders(meals)
    if problems:
        shown = "；".join(problems[:_MAX_PROBLEM_ROWS])
        more = f"；另有 {len(problems) - _MAX_PROBLEM_ROWS} 行" if len(problems) > _MAX_PROBLEM_ROWS else ""
        raise ImportRefused(
            f"云端当天名单存在无效数据（共 {len(problems)} 行）：{shown}{more}。"
            "已拒绝下单，请先在云端补全姓名/地址/电话（必须是 11 位手机号）后再试。")
    return meals


# ----------------------------------------------------------------------
# 留档写回《闪时送.xlsx》
# ----------------------------------------------------------------------

def archive_to_excel(excel_path: str | os.PathLike[str], meals: list[MealImport],
                     log: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """把当天名单写进《闪时送.xlsx》留档：清空 A:C 第 3 行起，E1 写日期原文。

    只写要下单的人（不含大西/小）；被跳过的那一餐清空名单并把 E1 清空。
    写入走同目录临时文件 + ``os.replace``，避免中途失败留下半份文件。
    """
    from openpyxl import load_workbook

    path = Path(excel_path)
    if not path.is_file():
        raise FileNotFoundError(f"留档 Excel 不存在：{path}")
    workbook = load_workbook(path)
    try:
        summary: dict[str, Any] = {}
        for meal in meals:
            if meal.meal in workbook.sheetnames:
                sheet = workbook[meal.meal]
            else:
                sheet = workbook.create_sheet(title=meal.meal)
                _log(log, f"留档：工作簿里没有「{meal.meal}」表，已新建", "WARN")
            last_row = max(int(sheet.max_row or 0), FIRST_DATA_ROW)
            for row in range(FIRST_DATA_ROW, last_row + 1):
                for col in (COL_NAME, COL_ADDRESS, COL_PHONE):
                    sheet.cell(row=row, column=col).value = None
            cleared = max(0, last_row - FIRST_DATA_ROW + 1)
            if meal.skipped:
                sheet.cell(row=DATE_ROW, column=DATE_COL).value = None
                summary[meal.meal] = {"written": 0, "cleared": cleared, "date_text": ""}
                continue
            for offset, order in enumerate(meal.orders):
                row = FIRST_DATA_ROW + offset
                sheet.cell(row=row, column=COL_NAME).value = order["name"]
                sheet.cell(row=row, column=COL_ADDRESS).value = order["door"]
                phone_cell = sheet.cell(row=row, column=COL_PHONE)
                phone_cell.value = str(order["phone"])
                # 电话按文本写，避免 Excel 把 11 位号码显示成科学计数。
                phone_cell.number_format = "@"
            sheet.cell(row=DATE_ROW, column=DATE_COL).value = meal.date_text
            summary[meal.meal] = {"written": meal.order_count, "cleared": cleared,
                                  "date_text": meal.date_text}

        handle, temp_name = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".xlsx", dir=str(path.parent))
        os.close(handle)
        temp_path = Path(temp_name)
        try:
            workbook.save(temp_path)
            os.replace(temp_path, path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return summary
    except PermissionError as exc:
        raise PermissionError(
            f"《{path.name}》无法写入（可能正被 Excel/WPS 打开）：{exc}") from exc
    finally:
        workbook.close()


# ----------------------------------------------------------------------
# 编排：读云端 → 校验 → 留档
# ----------------------------------------------------------------------

@dataclass
class DayOrders:
    """一次「当天名单」导入的完整结果。"""

    target_date: _dt.date
    meals: list[MealImport] = field(default_factory=list)
    orders_by_sheet: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    archive: dict[str, Any] = field(default_factory=dict)
    archive_error: str = ""

    @property
    def total(self) -> int:
        """两张表合计要下单的人数。"""
        return sum(len(orders) for orders in self.orders_by_sheet.values())

    @property
    def date_text(self) -> str:
        """留档 E1 用的日期原文（取第一张有当天列的表头原文）。"""
        for meal in self.meals:
            if meal.date_text:
                return meal.date_text
        return ""

    def as_summary(self) -> dict[str, Any]:
        """给界面/日志用的精简结构。"""
        return {
            "target_date": self.target_date.isoformat(),
            "date_text": self.date_text,
            "total": self.total,
            "date_gate": "ok",
            "meals": {
                meal.meal: {
                    "table": meal.table,
                    "marked": meal.marked_total,
                    "skipped_address": meal.skipped_address,
                    "orders": meal.order_count,
                    "date_text": meal.date_text,
                    "skipped": meal.skipped,
                    "reason": meal.skip_reason,
                    "warnings": list(meal.warnings),
                }
                for meal in self.meals
            },
            "archive": self.archive,
            "archive_error": self.archive_error,
        }


def prepare_day_orders(config: Any, *, now: _dt.datetime | None = None,
                       delivery_date: _dt.date | None = None,
                       cli: Any | None = None,
                       log: Callable[[str], Any] | None = None) -> DayOrders:
    """下单前的主入口：算目标日 → 日期闸门 → 读云端 → 校验 → 留档。

    ``delivery_date`` 传入时先过日期闸门（不一致直接拒绝，**不写任何文件**）；
    ``cli`` 可注入（测试用）。留档失败只记 ``archive_error`` 与告警，不影响下单。
    """
    now = now or _dt.datetime.now()
    target = ordering_target_date(
        now,
        start_hour=int(getattr(config, "wps_target_hour_start", 20) or 0),
    )
    if delivery_date is not None:
        check_target_day(target, delivery_date)
    _log(log, f"云端当天名单：识别日期 {target.year}-{target.month:02d}-{target.day:02d}"
              f"（{target.month}.{target.day}）")

    meals = collect_day_orders(config, target=target, cli=cli, log=log)
    day = DayOrders(target_date=target, meals=meals,
                    orders_by_sheet={meal.meal: list(meal.orders) for meal in meals})
    for meal in meals:
        if meal.skipped:
            continue
        _log(log, f"{meal.table} {target.month}.{target.day}：标 1 共 {meal.marked_total} 人，"
                  f"其中 {meal.skipped_address} 人地址是「大西/小」不走闪时送，"
                  f"实际下单 {meal.order_count} 人")

    excel_path = getattr(config, "sss_excel_path", None)
    if excel_path:
        try:
            day.archive = archive_to_excel(excel_path, meals, log=log)
            parts = "、".join(
                f"{name} {info['written']} 人"
                + (f"（E1={info['date_text']}）" if info["date_text"] else "（E1 已清空）")
                for name, info in day.archive.items())
            _log(log, f"已留档到《{Path(excel_path).name}》：{parts}")
        except Exception as exc:  # 留档只是留痕，绝不因此阻断下单
            day.archive_error = str(exc)
            _log(log, f"留档写入失败：{exc}（不影响下单，下单用的是云端内存名单）", "WARN")
    else:
        _log(log, "未选择订单 Excel 文件：跳过留档（下单用的是云端内存名单）")
    return day


__all__ = [
    "ImportRefused", "MealImport", "DayOrders", "SSS_SHEET_SOURCES",
    "SKIP_ADDRESSES", "address_group", "should_skip_address", "normalise_phone",
    "read_cloud_meal", "validate_orders", "collect_day_orders",
    "archive_to_excel", "prepare_day_orders", "check_target_day",
    "ordering_target_date",
]
