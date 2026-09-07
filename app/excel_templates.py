"""Excel 模板生成，供桥接层（app/bridge.py）与未来的 UI 复用。

模板结构对齐用户实际使用的《工作.xlsx》与《闪时送.xlsx》（2026-09 版），
保证「新建模板」生成的文件可直接当作排单/闪时送工作簿使用：

- 排单模板共 13 张表：六张食堂子表（衣锦/医学院/东湖 × 中餐/晚餐）与
  七张周表（周一~周日）。食堂子表第 2 行表头
  「订单 姓名 地址 电话 周一~周日 类型 餐种 餐次」（A~N 共 14 列），
  与 automation._write_order 的写入列一一对应；周表第 1 行按
  中餐/晚餐/总餐三区块合并标题，第 2 行两组 6 列表头
  「订单 姓名 地址 电话 餐种 餐次」，数据均从第 3 行开始。
- 闪时送模板固定《午餐/晚餐》两表，第 1 行合并标题，第 2 行
  「姓名 地址 电话」，从第 3 行起逐行读取（load_sss_orders 约定）。
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side

# ----------------------------------------------------------------------
# 排单（工作簿）模板
# ----------------------------------------------------------------------

# 食堂子表：表名、标题与表头共 14 列（订单 姓名 地址 电话 周一~周日 类型 餐种 餐次）。
_CAMPUS_ORDER = (
    "东湖中餐", "衣锦中餐", "医学院中餐",
    "东湖晚餐", "衣锦晚餐", "医学院晚餐",
)
_SUB_HEADERS = ("订单", "姓名", "地址", "电话", *(
    "周一", "周二", "周三", "周四", "周五", "周六", "周日",
), "类型", "餐种", "餐次")

# 各食堂子表 D（电话）列宽，取自用户工作簿，便于手机号完整显示。
_SUB_COL_WIDTHS = {
    "东湖中餐": 14.75, "衣锦中餐": 15.25, "医学院中餐": 15.16,
    "东湖晚餐": 12.66, "衣锦晚餐": 12.66, "医学院晚餐": 15.25,
}

# 周表：第 1 行三区块标题（合并 A~F / G~L / N~S），第 2 行两组表头。
_WEEKDAY_GROUPS = (
    ("中餐", "A1:F1", "A2:F2"),
    ("晚餐", "G1:L1", "G2:L2"),
    ("总餐", "N1:S1", "N2:S2"),
)
_DAY_HEADERS = ("订单", "姓名", "地址", "电话", "餐种", "餐次")
# 周表列宽（D/J 电话列、周日 Q 列），取自用户工作簿。
_DAY_COL_WIDTHS = {
    "周一": {"D": 12.33, "J": 13.75}, "周二": {"D": 12.50, "J": 13.58},
    "周三": {"D": 13.33, "J": 13.25}, "周四": {"D": 14.41, "J": 12.33},
    "周五": {"D": 13.25, "J": 13.16}, "周六": {"D": 13.66, "J": 13.33},
    "周日": {"D": 13.08, "J": 14.25, "Q": 14.58},
}


def _center_row(ws, row: int, max_col: int) -> None:
    """把第 1/2 行表头设为水平居中（对齐用户工作簿的样式）。"""
    for col in range(1, max_col + 1):
        ws.cell(row, col).alignment = Alignment(horizontal="center")


def write_order_template(dest: Path) -> None:
    """生成排单模板：六张食堂子表 + 七张周表，表头与用户工作簿一致。"""
    wb = Workbook()
    first = True

    def _sheet(title: str):
        nonlocal first
        ws = wb.active if first else wb.create_sheet(title=title)
        if first:
            ws.title = title
            first = False
        return ws

    # 食堂子表：第 1 行标题（跨 14 列合并居中），第 2 行表头。
    for name in _CAMPUS_ORDER:
        ws = _sheet(name)
        ws.merge_cells("A1:N1")
        ws["A1"] = name
        ws.append(_SUB_HEADERS)  # 第 2 行表头，数据从第 3 行开始
        ws.column_dimensions["D"].width = _SUB_COL_WIDTHS[name]
        _center_row(ws, 1, 14)
        _center_row(ws, 2, 14)

    # 周表：第 1 行合并标题，第 2 行 A~F / G~L 两组表头（N~S 总餐区留空表头备用）。
    for day in ("周一", "周二", "周三", "周四", "周五", "周六", "周日"):
        ws = _sheet(day)
        for label, title_range, _header_range in _WEEKDAY_GROUPS:
            ws.merge_cells(title_range)
            ws[title_range.split(":")[0]] = label
        ws.append(list(_DAY_HEADERS) + list(_DAY_HEADERS) + ["", *_DAY_HEADERS])  # 第 2 行表头
        for col_name, width in _DAY_COL_WIDTHS[day].items():
            ws.column_dimensions[col_name].width = width
        _center_row(ws, 1, 19)
        _center_row(ws, 2, 19)

    wb.save(str(dest))
    wb.close()


# ----------------------------------------------------------------------
# 闪时送模板
# ----------------------------------------------------------------------

_SSS_FONT = Font(name="等线", size=11)
_SSS_ALIGN = Alignment(horizontal="center", vertical="center")
_SSS_BORDER = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin"),
)
# 午餐/晚餐表头与列宽，取自用户《闪时送.xlsx》。
_SSS_HEADERS = ("姓名", "地址", "电话")
_SSS_COL_WIDTHS = {
    "午餐": {"A": 9.41, "B": 11.66, "C": 13.75},
    "晚餐": {"A": 13.33, "B": 12.08, "C": 12.91},
}


def write_sss_template(dest: Path) -> None:
    """生成闪时送下单模板：固定《午餐/晚餐》两表（A 姓名 B 地址 C 电话）。"""
    wb = Workbook()
    first = True
    for name in ("午餐", "晚餐"):
        ws = wb.active if first else wb.create_sheet(title=name)
        if first:
            ws.title = name
            first = False
        ws.merge_cells("A1:C1")
        ws["A1"] = name
        # 第 2 行表头，第 3 行起填数据（load_sss_orders 从第 3 行开始读取）。
        ws.append(_SSS_HEADERS)
        for row in range(1, 3):
            for col in range(1, 4):
                cell = ws.cell(row, col)
                cell.font = _SSS_FONT
                cell.alignment = _SSS_ALIGN
                cell.border = _SSS_BORDER
        for col_name, width in _SSS_COL_WIDTHS[name].items():
            ws.column_dimensions[col_name].width = width
    wb.save(str(dest))
    wb.close()
