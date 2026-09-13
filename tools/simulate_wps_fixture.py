#!/usr/bin/env python3
"""离线仿真：用《测试东湖中餐.xlsx》夹具在本地完整跑一遍云同步逻辑。

夹具四张表：
- 东湖中餐（试验田）   = 云端表同步前状态（96 人，无 9.12 数据）→ 程序的写入对象
- 东湖中餐（本地工作表）= 本地排单表（25 个订单）           → 程序的输入
- 东湖中餐（目标）     = 协作者手工维护的正确结果（121 人）  → 验收标准
- 东湖中餐（基准）     = 基线存档（与试验田相同）

仿真不联网、不改夹具文件：把试验田拷进内存工作簿当"云端"，
跑 build_plan + apply_plan，再与目标表逐格比对（值 + 底色 + 记号）。

已知允许的差异（都有明确原因，不算失败）：
1. 新行的「已出餐/剩余餐」列：真实云端表是公式（程序不写），
   目标表导出时被拍平成了值 → 比对新行时跳过 L/M 列。
2. 豪华行备注列：按用户要求整行金黄（含备注列），目标表导出里备注列没涂。
3. D2 组「灵/刘姵怡」相邻两行的先后顺序：目标表里两行都在组尾、
   数据完全相同，仅协作手工录入顺序与"经济在前豪华在后"规则不同。

用法：.venv/bin/python tools/simulate_wps_fixture.py [夹具路径]
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openpyxl
from openpyxl.styles import PatternFill

from app import wps_cloud as wc
from app.wps_cloud import apply_plan, build_plan

FIXTURE_DEFAULT = Path("/home/zimu/文档/测试东湖中餐.xlsx")
SHEET_TEST = "东湖中餐（试验田）"
SHEET_LOCAL = "东湖中餐（本地工作表）"
SHEET_TARGET = "东湖中餐（目标）"

GOLD = "FFFFC000"
GREEN = "FF92D050"

# 目标表里两个豪华新行（灵/刘姵怡）的顺序偏差：119↔120 互换属已知差异
KNOWN_SWAP_ROWS = (119, 120)


def cell_fill_hex(cell) -> str:
    """返回单元格底色的 ARGB hex（带 #），无底色返回 ''。"""
    fill = cell.fill
    if fill is None or fill.patternType is None:
        return ""
    rgb = getattr(fill.fgColor, "rgb", None)
    if not isinstance(rgb, str) or len(rgb) != 8:
        return ""
    return f"#{rgb}"


class FixtureCloud:
    """用 openpyxl 工作表模拟云端表的 KdocsCli 替身。

    read_grid 的行为对齐真实接口：只返回非空格；with_format 时附带底色；
    insert_rows/delete_rows 用 openpyxl 的行操作（值与底色随行移动）。
    """

    def __init__(self, ws: openpyxl.worksheet.worksheet.Worksheet):
        self.ws = ws
        self.inserts: list[tuple[int, int]] = []
        self.format_ops: list[list[dict]] = []
        self.path = "/fixture/kdocs-cli"

    # ---- KdocsCli 接口 ----

    def sheets_info(self, file_id: str):
        return [{"sheetId": 1, "sheetName": self.ws.title,
                 "rowTo": self.ws.max_row - 1, "colTo": self.ws.max_column - 1}]

    def read_grid(self, file_id, worksheet_id, row_from, row_to, col_from, col_to,
                  *, with_format: bool = False):
        out: dict[tuple[int, int], object] = {}
        for r in range(row_from, row_to + 1):
            for c in range(col_from, col_to + 1):
                cell = self.ws.cell(row=r + 1, column=c + 1)
                if cell.value is None or str(cell.value).strip() == "":
                    continue
                if with_format:
                    out[(r, c)] = {"text": str(cell.value),
                                   "fill": cell_fill_hex(cell)}
                else:
                    out[(r, c)] = str(cell.value)
        return out

    def write_cells(self, file_id, worksheet_id, cells):
        for cell in cells:
            self.ws.cell(row=int(cell["row"]), column=int(cell["col"]),
                value=str(cell["value"]))

    def read_formulas(self, file_id, worksheet_id, row_from, row_to, col_from, col_to):
        result = {}
        for r in range(row_from, row_to + 1):
            for c in range(col_from, col_to + 1):
                value = self.ws.cell(row=r + 1, column=c + 1).value
                if isinstance(value, str) and value.startswith("="):
                    result[(r, c)] = value
        return result

    def insert_rows(self, file_id, worksheet_id, *, row, count):
        self.inserts.append((row, count))
        self.ws.insert_rows(row, count)          # 在 1-based row 前插 count 行

    def delete_rows(self, file_id, worksheet_id, *, row, count):
        self.ws.delete_rows(row, count)

    def write_format_ops(self, file_id, worksheet_id, ops):
        self.format_ops.append([dict(op) for op in ops])
        for op in ops:
            color = op["xf"]["fill"]["back"]["value"]
            argb = f"{color & 0xFFFFFFFF:08X}"
            fill = PatternFill("solid", fgColor=argb)
            for r in range(op["rowFrom"] + 1, op["rowTo"] + 2):
                for c in range(op["colFrom"] + 1, op["colTo"] + 2):
                    self.ws.cell(row=r, column=c).fill = fill

    def read_cell_format(self, file_id, worksheet_id, row, col):
        cell = self.ws.cell(row=row, column=col)
        if cell.value is None:
            return None
        font = cell.font
        color = None
        if font.color is not None and getattr(font.color, "type", "") == "rgb":
            color = font.color.rgb
        return {
            "cellText": str(cell.value),
            "fonts": {"font_east_asia": font.name or "", "size": int(font.size or 10),
                      "color": color},
            "alignment": {"horizontal": "haCenter", "vertical": "vaCenter"},
            "cell_background_color": cell_fill_hex(cell),
        }

    def authenticated(self) -> bool:
        return True


def build_local_workbook(fixture: Path, tmp_xlsx: Path) -> None:
    """把夹具的「本地工作表」拷成标准排单表（子表名「东湖中餐」）。

    逐行原样拷贝（第 1 行标题、第 2 行表头、第 3 行起数据），
    布局必须和真实排单表一致，read_local_orders 才读得对。
    """
    src = openpyxl.load_workbook(fixture, data_only=True)
    ws = src[SHEET_LOCAL]
    dst = openpyxl.Workbook()
    out = dst.active
    out.title = "东湖中餐"
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=14, values_only=True):
        out.append(list(row))
    dst.save(tmp_xlsx)


def norm(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    try:
        return str(int(float(text)))
    except (TypeError, ValueError):
        return text


def is_white(hexfill: str) -> bool:
    return hexfill in ("", "#FFFFFFFF", "#00FFFFFF")


def main() -> int:
    fixture = Path(sys.argv[1]) if len(sys.argv) > 1 else FIXTURE_DEFAULT
    print(f"夹具：{fixture}")
    tmp_xlsx = Path("/tmp/fixture_本地工作表.xlsx")
    build_local_workbook(fixture, tmp_xlsx)

    src = openpyxl.load_workbook(fixture)
    cloud = FixtureCloud(src[SHEET_TEST])
    target = src[SHEET_TARGET]

    orders = wc.read_local_orders(tmp_xlsx, sheets=["东湖中餐"])[  "东湖中餐"]
    print(f"本地订单：{len(orders)} 个")

    plans = build_plan(
        cloud, local_orders={"东湖中餐": orders},
        tables={"东湖中餐": {"file_id": "FIXTURE", "drive_id": ""}},
        target=dt.date(2026, 9, 12), ledger=None, marker_enabled=True,
        run_date=dt.date(2026, 9, 11),
        log=lambda m: print(f"  [log] {m}"))
    plan = plans[0]
    print(f"\n计划：目标列 {plan.target_col}（{plan.target_header}），"
          f"记号列 {plan.marker_col}，记号值 {plan.weekday_number}，"
          f"插入块 {len(plan.insert_blocks)} 个")
    for block in plan.insert_blocks:
        print(f"  - 第 {block.position} 行前插 {block.count} 行"
              f"（「{block.address}」组，first_row={block.first_row}"
              f"{'，表尾' if block.append_only else ''}）")
    for warning in plan.warnings:
        print(f"  ⚠ {warning}")

    result = apply_plan(cloud, plans, ledger=None, marker_enabled=True,
                        log=lambda m: print(f"  [log] {m}"))
    print(f"apply 结果：{result['sheets']}")
    if result["failed"]:
        print("❌ 有表写入失败，中止比对")
        return 1

    # ---- 与目标表逐格比对 ----
    problems: list[str] = []
    known_swaps: list[str] = []
    max_row = max(target.max_row, cloud.ws.max_row)
    for r in range(3, max_row + 1):
        t_vals = [norm(target.cell(r, c).value) for c in range(1, 15)]
        g_vals = [norm(cloud.ws.cell(r, c).value) for c in range(1, 15)]
        t_name = t_vals[0]
        if not t_name and not g_vals[0]:
            continue
        is_new = t_name in {c.name for c in plan.changes if c.kind == "new"}
        is_lux = target.cell(r, 10).value == "豪华"
        for idx in range(14):
            if t_vals[idx] == g_vals[idx]:
                continue
            # 允许差异 1：新行的 L/M 列（真实表是公式，程序不写）
            if is_new and idx in (11, 12):
                continue
            known_swaps.append(
                f"r{r} 第{idx + 1}列：目标 {t_vals[idx]!r} vs 实际 {g_vals[idx]!r}")
        # 底色比对（A~N 列）
        for c in range(1, 15):
            t_fill = cell_fill_hex(target.cell(r, c))
            g_fill = cell_fill_hex(cloud.ws.cell(r, c))
            if is_white(t_fill) and is_white(g_fill):
                continue
            if t_fill == g_fill:
                continue
            # 允许差异 2：豪华行备注列按用户要求整行金黄
            if is_lux and c == 14 and g_fill == f"#{GOLD}" and not t_fill:
                continue
            known_swaps.append(
                f"r{r} 第{c}列底色：目标 {t_fill or '无'} vs 实际 {g_fill or '无'}")

    # 记号
    q2 = norm(cloud.ws.cell(2, 17).value)
    if q2 != "6":
        problems.append(f"Q2 记号应为 6，实际 {q2!r}")
    p2 = cloud.ws.cell(2, 16).value
    if norm(p2) != norm(target.cell(2, 16).value):
        problems.append(f"P2 被意外改动：{p2!r}")

    # 行数必须一致
    if cloud.ws.max_row != target.max_row:
        problems.append(f"行数不一致：实际 {cloud.ws.max_row} vs 目标 {target.max_row}")

    hard = [s for s in known_swaps
            if not _is_known_deviation(s)]
    print("\n---- 差异明细 ----")
    for item in known_swaps[:60]:
        tag = "✔已知" if _is_known_deviation(item) else "❌"
        print(f" {tag} {item}")
    if len(known_swaps) > 60:
        print(f" …共 {len(known_swaps)} 条")
    if problems:
        for item in problems:
            print(f" ❌ {item}")
    if hard or problems:
        print(f"\n❌ 仿真未通过：{len(hard)} 处硬差异 + {len(problems)} 处结构问题")
        return 1
    print("\n✅ 仿真通过：试验田 + 本地订单 → 与目标表一致"
          "（除已知的公式列/备注列底色/豪华行顺序三项允许差异）")
    return 0


def _is_known_deviation(item: str) -> bool:
    """只有"灵/刘姵怡顺序"这一类值差异算已知；底色差异不允许。"""
    m = re.match(r"r(\d+) ", item)
    if not m:
        return False
    row = int(m.group(1))
    return row in KNOWN_SWAP_ROWS and "底色" not in item


if __name__ == "__main__":
    raise SystemExit(main())
