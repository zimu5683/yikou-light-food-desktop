"""Tests for app.wps_cloud —— 云文档同步（全部离线，不联网、不写云端）。

覆盖：目标日期规则、表头解析、人员匹配、总餐次绝对值语义、幂等、
列定位（含协作者写错星期的情况）、写入与回读校验、错误处理。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app import wps_cloud as wc
from app.wps_cloud import (
    Change, CloudOrder, SheetPlan, SyncLedger, WpsCloudError,
    apply_plan, build_plan, find_cli, format_plan, parse_date_header,
    person_key, summarize_plan, target_date_for, weekday_number,
)

# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------


class FakeCli:
    """KdocsCli 的替身：用内存网格模拟一张云端表。"""

    def __init__(self, grid: dict[tuple[int, int], str], *,
                 fail_write: bool = False, corrupt_write: bool = False):
        self.grid = dict(grid)          # (0-based 行, 0-based 列) -> 文本
        self.fail_write = fail_write
        self.corrupt_write = corrupt_write
        self.writes: list[dict] = []
        self.inserts: list[tuple[int, int]] = []
        self.format_ops: list[list[dict]] = []
        self.path = "/fake/kdocs-cli"

    def sheets_info(self, file_id: str):
        return [{"sheetId": 1, "sheetName": "Sheet1", "rowTo": 200, "colTo": 50}]

    def read_grid(self, file_id, worksheet_id, row_from, row_to, col_from, col_to,
                  *, with_format: bool = False):
        hits = {k: v for k, v in self.grid.items()
                if row_from <= k[0] <= row_to and col_from <= k[1] <= col_to}
        if with_format:
            # 与真实接口一致：只返回非空格；fill 模拟为空（模板测试里由 TemplateCli 覆盖）
            return {k: {"text": str(v), "fill": ""} for k, v in hits.items()}
        return hits

    def write_cells(self, file_id, worksheet_id, cells):
        if self.fail_write:
            raise WpsCloudError("模拟写入失败")
        self.writes.append({"file_id": file_id, "cells": list(cells)})
        if self.corrupt_write:
            return                      # 假装成功，但什么都不改（用于验证回读校验）
        for cell in cells:
            self.grid[(int(cell["row"]) - 1, int(cell["col"]) - 1)] = str(cell["value"])

    def read_formulas(self, file_id, worksheet_id, row_from, row_to, col_from, col_to):
        result = {}
        for key, value in self.grid.items():
            if row_from <= key[0] <= row_to and col_from <= key[1] <= col_to and str(value).startswith("="):
                result[key] = str(value)
        return result

    def insert_rows(self, file_id, worksheet_id, *, row, count):
        """在 1-based 行号 row 前插 count 行：0-based ≥ row-1 的内容整体下移。"""
        self.inserts.append((row, count))
        shifted = {}
        for (r, c), v in self.grid.items():
            shifted[(r + count if r >= row - 1 else r, c)] = v
        self.grid = shifted

    def delete_rows(self, file_id, worksheet_id, *, row, count):
        """删除 1-based 行号 row 起 count 行：中间内容移除、下方上移。"""
        lo, hi = row - 1, row - 1 + count - 1
        shifted = {}
        for (r, c), v in self.grid.items():
            if r < lo:
                shifted[(r, c)] = v
            elif r > hi:
                shifted[(r - count, c)] = v
        self.grid = shifted

    def write_format_ops(self, file_id, worksheet_id, ops):
        self.format_ops.append([dict(op) for op in ops])

    def authenticated(self) -> bool:
        return True

    # ---- 格式接口（默认：读不到参考格式，于是跳过格式设置）----
    def read_cell_format(self, file_id, worksheet_id, row, col):
        return None


def make_grid(header: dict[int, str], rows: list[dict[int, str]]) -> dict[tuple[int, int], str]:
    """header/rows 的键都是 0-based 列号；第 1 行标题、第 2 行表头、第 3 行起数据。"""
    grid: dict[tuple[int, int], str] = {}
    for col, text in header.items():
        grid[(1, col)] = text
    for idx, row in enumerate(rows, start=2):
        for col, text in row.items():
            if text not in (None, ""):
                grid[(idx, col)] = str(text)
    return grid


BASE_HEADER = {0: "名字", 1: "地址", 2: "电话", 3: "9.10 周四",
               4: "9.11 周五", 5: "类型", 6: "餐种", 7: "总餐次",
               8: "已出餐", 9: "剩余餐", 10: "备注", 12: "9.11 周五"}


# ----------------------------------------------------------------------
# 目标日期与记号
# ----------------------------------------------------------------------

@pytest.mark.parametrize("hour,expect_day", [
    (18, 11), (19, 11),          # 白天/傍晚：写当天
    (20, 12), (21, 12), (23, 12),  # 晚上：写次日
    (0, 12), (5, 12), (9, 12),   # 凌晨：写次日
    (10, 11), (12, 11),          # 上午 10 点后：写当天
])
def test_target_date_window(hour: int, expect_day: int):
    now = dt.datetime(2026, 9, 11, hour, 0)
    assert target_date_for(now).day == expect_day


def test_target_date_custom_window():
    now = dt.datetime(2026, 9, 11, 19, 0)
    assert target_date_for(now, start_hour=18, end_hour=6).day == 12
    assert target_date_for(now, start_hour=22, end_hour=6).day == 11


@pytest.mark.parametrize("day,expect", [
    (dt.date(2026, 9, 13), 1),   # 周日
    (dt.date(2026, 9, 14), 2),   # 周一
    (dt.date(2026, 9, 18), 6),   # 周五
    (dt.date(2026, 9, 19), 7),   # 周六
])
def test_weekday_number(day: dt.date, expect: int):
    assert weekday_number(day) == expect


# ----------------------------------------------------------------------
# 表头解析
# ----------------------------------------------------------------------

@pytest.mark.parametrize("text,expect", [
    ("9.11 周五", (9, 11)),
    ("9.11周五", (9, 11)),        # 无空格
    ("9.9 周三", (9, 9)),
    ("3.16周一", (3, 16)),        # 协作者漏空格
    ("12.31 周四", (12, 31)),
    ("总餐次", None),
    ("", None),
    (None, None),
    ("13.40", None),              # 非法月日
])
def test_parse_date_header(text, expect):
    assert parse_date_header(text) == expect


def test_find_target_column_ignores_written_weekday():
    """协作者把 9.16 写成「9.16周一」，但 2026-09-16 其实是周三；只认月.日。"""
    header = {0: "名字", 3: "9.16周一", 4: "9.17周二"}
    found = wc.find_target_column(header, dt.date(2026, 9, 16))
    assert found is not None and found[0] == 3


def test_person_key_normalizes_phone():
    assert person_key("张", 19730037965) == ("张", "19730037965")
    assert person_key(" 张 ", "19730037965.0") == ("张", "19730037965")
    assert person_key("张", "") == ("张", "")
    assert person_key(None, None) == ("", "")


# ----------------------------------------------------------------------
# 计划：匹配、总餐次绝对值、幂等
# ----------------------------------------------------------------------

def _plan(cli, orders, target=dt.date(2026, 9, 11), ledger=None, marker=True,
          run_date=None):
    tables = {"东湖中餐": {"file_id": "F1"}}
    return build_plan(cli, local_orders={"东湖中餐": orders}, tables=tables,
                      target=target, ledger=ledger, marker_enabled=marker,
                      run_date=run_date)


def test_new_customer_is_appended():
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "Ichi", 2: "111", 7: "3"},
    ]))
    orders = [CloudOrder("东湖中餐", "新人", "小", "222", "中餐", "经济", 6)]
    plans = _plan(cli, orders)
    changes = plans[0].changes
    assert len(changes) == 1 and changes[0].kind == "new"
    assert changes[0].total_after == 6 and changes[0].row == 4   # 追加到第 4 行


def test_existing_customer_total_is_absolute_not_additive():
    """总餐次写的是本地值本身，不是"云端 + 本地"（否则重复跑会翻倍）。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 4: "1", 7: "6"},
    ]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    change = _plan(cli, orders)[0].changes[0]
    assert change.kind == "existing"
    assert change.total_after == 6 and change.total_before == 6
    assert change.needs_write is False      # 目标格已是 1、总餐次已一致 → 一个字都不写


def test_total_written_when_mismatch():
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 7: "3"},        # 云端 3，本地 6
    ]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    change = _plan(cli, orders)[0].changes[0]
    assert change.total_after == 6 and change.total_before == 3
    assert change.needs_write is True


def test_match_requires_both_name_and_phone():
    """同名不同电话必须视为两个人（不会写错行）。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "李", 2: "111", 7: "5"},
    ]))
    orders = [CloudOrder("东湖中餐", "李", "小", "999", "中餐", "经济", 2)]
    change = _plan(cli, orders)[0].changes[0]
    assert change.kind == "new"


def test_missing_target_column_blocks_write():
    header = dict(BASE_HEADER)
    header.pop(4)                       # 去掉 9.11 列
    header.pop(12)                      # 通讯记号区的同名表头也去掉
    cli = FakeCli(make_grid(header, [{0: "张", 2: "111", 7: "6"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    plan = _plan(cli, orders)[0]
    assert plan.target_col == 0
    assert any("没有 9.11" in w for w in plan.warnings)
    assert plan.changes == []


def test_broken_header_blocks_write():
    """表头被改乱（例如「类型」列表头被覆盖成日期）时必须拒绝写入。

    真实事故：测试副本的第 7 列（类型）表头被写成了「9.12 周六」，
    程序当时照样往那一列写数字 —— 虽然位置凑巧正确，但往看不懂的表里
    写数据本身就不该发生。
    """
    header = dict(BASE_HEADER)
    header[5] = "9.12 周六"          # 覆盖「类型」表头，模拟被改乱
    cli = FakeCli(make_grid(header, [{0: "张", 2: "111", 7: "6"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    plan = _plan(cli, orders)[0]
    assert plan.changes == []
    assert any("表头异常" in w and "类型" in w for w in plan.warnings)


def test_column_lookup_handles_shifted_layout():
    """协作者插列后，工作列整体右移，仍要按表头名找到。"""
    header = {0: "名字", 1: "地址", 2: "电话", 3: "9.10 周四", 4: "9.11 周五",
              5: "9.12 周六", 6: "类型", 7: "餐种", 8: "总餐数", 9: "出餐",
              10: "剩余", 11: "备注"}
    cli = FakeCli(make_grid(header, [{0: "张", 2: "111", 8: "2"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    plan = _plan(cli, orders)[0]
    assert plan.columns["total"] == 9          # 1-based，"总餐数"在第 9 列
    assert plan.target_col == 5                # "9.11 周五"（0-based 4 → 1-based 5）
    assert plan.changes[0].total_after == 6


# ----------------------------------------------------------------------
# 写入与校验
# ----------------------------------------------------------------------

def test_apply_plan_writes_and_verifies():
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 7: "3"},
    ]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    plans = _plan(cli, orders)
    result = apply_plan(cli, plans, ledger=None, marker_enabled=False)
    assert result["written"] == 1 and result["failed"] == 0
    # 目标日期列（第 5 列 = index 4）与总餐次（第 8 列 = index 7）都写对了
    assert cli.grid[(2, 4)] == "1"
    assert cli.grid[(2, 7)] == "6"


def test_apply_plan_detects_silent_write_failure():
    """接口返回成功但内容没变时，回读校验必须发现（曾出现过假阳性）。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 7: "3"},
    ]), corrupt_write=True)
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    plans = _plan(cli, orders)
    result = apply_plan(cli, plans, ledger=None, marker_enabled=False)
    assert result["failed"] == 1
    assert result["sheets"][0]["status"] == "verify_failed"


def test_scan_bounds_respects_read_budget():
    """读取范围必须按"行×列"控制，避免撞 5 万格上限。

    真实事故：`衣锦中餐` 有 121 个日期列，若按 285 行 × 201 列去读
    （= 57285 格）会被接口拒绝，整个预览/上传直接失败。
    """
    # 宽表：列多 -> 行必须被压缩
    row_to, col_to = wc.scan_bounds({"rowTo": 500, "colTo": 199})
    assert (row_to + 1) * (col_to + 1) <= wc.MAX_READ_CELLS
    # 窄表：不受影响
    assert wc.scan_bounds({"rowTo": 103, "colTo": 13}) == (103, 13)
    # 缺字段 / None 不应崩
    assert wc.scan_bounds(None) == (0, 0)


# 模板行的颜色分布：A~J 白、K~M 黄、N 白（东湖中餐第 99 行的真实分布）
TEMPLATE_COLORS = {c: "#FFFFFFFF" for c in range(1, 15)}
for _c in (11, 12, 13):
    TEMPLATE_COLORS[_c] = "#FFFFC000"


class TemplateCli(FakeCli):
    """带"模板行格式"的替身：能学到字体与逐列底色。"""

    def __init__(self, *a, row_colors=None, **kw):
        super().__init__(*a, **kw)
        self.row_colors = dict(row_colors or TEMPLATE_COLORS)

    def read_grid(self, file_id, worksheet_id, row_from, row_to, col_from, col_to,
                  *, with_format: bool = False):
        hits = super().read_grid(file_id, worksheet_id, row_from, row_to,
                                 col_from, col_to)
        if with_format:
            return {k: {"text": str(v),
                        "fill": self.row_colors.get(k[1] + 1, "#FFFFFFFF")}
                    for k, v in hits.items()}
        return hits

    def read_cell_format(self, file_id, worksheet_id, row, col):
        """任何有内容的行都提供格式（与真实接口行为一致）。"""
        if (row - 1, 0) not in self.grid:
            return None
        return {"fonts": {"font_east_asia": "Microsoft YaHei", "size": 10,
                          "color": "#FF000000"},
                "alignment": {"horizontal": "haCenter", "vertical": "vaCenter"},
                "cell_background_color": self.row_colors.get(col, "#FFFFFFFF"),
                "cellText": "参考"}


def test_learn_row_format_copies_font_and_per_column_fills():
    """学模板行：字体/对齐 + **逐列底色**（黄带位置要照抄）。"""
    cli = TemplateCli(make_grid(BASE_HEADER, [{0: "老人", 2: "111", 7: "3"}]))
    spec = wc.learn_row_format(cli, "F1", 1, col_from=1, col_to=14, rows=[3])
    assert spec["font_name"] == "Microsoft YaHei"
    assert spec["font_size"] == 10
    assert spec["alcH"] == 2 and spec["alcV"] == 1
    assert spec["fills"][1] == "#FFFFFFFF"
    assert spec["fills"][11] == "#FFFFC000", "K~M 列必须照抄成黄色"
    assert spec["fills"][14] == "#FFFFFFFF"


def test_learn_row_format_returns_empty_when_no_reference():
    class EmptyCli:
        def read_grid(self, *_a, **_k):
            return {}

        def read_cell_format(self, *_a, **_k):
            return None

    assert wc.learn_row_format(EmptyCli(), "F1", 1, col_from=1, col_to=14, rows=[3]) == {}


def test_new_rows_copy_template_fills():
    """经济餐新行照抄模板底色（黄带位置一致）；豪华餐整行金黄。"""
    # 模板行整行有值（真实接口只返回非空格，底色缓存才覆盖每一列）
    full_row = {0: "老人", 1: "D1", 2: "111", 3: "0", 4: "1", 5: "0", 6: "中餐",
                7: "经济", 8: "3", 9: "1", 10: "2", 11: ""}
    cli = TemplateCli(make_grid(BASE_HEADER, [full_row]))
    orders = [
        CloudOrder("东湖中餐", "经济人", "D2", "13900000001", "中餐", "经济", 1),
        CloudOrder("东湖中餐", "豪华人", "D2", "13900000002", "中餐", "豪华", 1),
    ]
    apply_plan(cli, _plan(cli, orders), ledger=None, marker_enabled=False)
    assert len(cli.format_ops) == 1, "所有格式操作合并后一次调用"
    ops = cli.format_ops[0]
    econ = [op for op in ops if op["rowFrom"] == 3]        # 1-based 第 4 行
    lux = [op for op in ops if op["rowFrom"] == 4]         # 1-based 第 5 行
    assert lux and len(lux) == 1, "豪华餐一整个行区间一个操作"
    assert (lux[0]["colFrom"], lux[0]["colTo"]) == (0, 10), "豪华餐整行（名字到备注列）金黄"
    assert lux[0]["xf"]["fill"]["back"]["value"] == wc.FILL_LUXURY
    white = next(op for op in econ if op["colFrom"] == 0)
    assert white["colTo"] == 9, "白底列合并成一个区间"
    assert white["xf"]["fill"]["back"]["value"] == wc._argb_to_int("#FFFFFFFF")
    gold = next(op for op in econ
                if op["xf"]["fill"]["back"]["value"] == wc.FILL_LUXURY)
    assert (gold["colFrom"], gold["colTo"]) == (10, 10), "总餐次列照抄成金黄"


# ----------------------------------------------------------------------
# 新客户按地址组插入（排序）
# ----------------------------------------------------------------------

def _grouped_grid():
    return FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 7: "3"},
        {0: "李", 1: "大西", 2: "222", 7: "4"},
        {0: "王", 1: "小", 2: "333", 7: "5"},
    ]))


def test_new_customers_insert_after_address_group():
    """新客户插到自己地址组的末尾，而不是表尾；下方已有内容整体下移。"""
    cli = _grouped_grid()
    orders = [
        CloudOrder("东湖中餐", "新人小", "小", "999", "中餐", "经济", 2),
        CloudOrder("东湖中餐", "新人西", "大西", "888", "中餐", "经济", 1),
    ]
    plan = _plan(cli, orders)[0]
    blocks = {b.position: b for b in plan.insert_blocks}
    assert set(blocks) == {5, 6}, "大西组末行=4 → 插在第 5 行前；小组末行=5 → 插在第 6 行前"
    assert blocks[5].first_row == 5 and blocks[6].first_row == 7
    rows = {c.name: c.row for c in plan.changes}
    assert rows == {"新人西": 5, "新人小": 7}
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert cli.grid[(3, 0)] == "李"            # 大西组在插入点之前，不动
    assert cli.grid[(5, 0)] == "王"            # 小组的王被下移到第 6 行
    assert cli.grid[(4, 0)] == "新人西" and cli.grid[(6, 0)] == "新人小"


def test_new_customer_row_is_complete():
    """新客户行必须填齐：姓名/地址/电话/类型/餐种/总餐次/日期格。

    真实事故：初版只写了姓名/地址/电话与日期格，漏了「类型」和「餐种」，
    追加出来的人在云端缺了餐别信息。
    """
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "老人", 2: "111", 7: "3"}]))
    orders = [CloudOrder("东湖中餐", "新人", "D2", "222", "中餐", "豪华", 6)]
    plans = _plan(cli, orders)
    row = plans[0].changes[0].row
    apply_plan(cli, plans, ledger=None, marker_enabled=False)

    header = {c: v for (r, c), v in cli.grid.items() if r == 1}
    got = {header[c]: cli.grid[(row - 1, c)]
           for c in sorted(header) if (row - 1, c) in cli.grid}
    assert got["名字"] == "新人"
    assert got["地址"] == "D2"
    assert got["电话"] == "222"
    assert got["类型"] == "中餐"
    assert got["餐种"] == "豪华"
    assert got["总餐次"] == "6"
    assert got["9.11 周五"] == "1"


def test_existing_customer_below_insertion_is_written_at_shifted_row():
    """插入会让下方的老客户整体下移：他们的写入行号必须跟着位移，否则会写错行。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 7: "3"},
        {0: "王", 1: "小", 2: "333", 7: "5"},
        {0: "李", 1: "大西", 2: "222", 7: "4"},
    ]))
    orders = [
        CloudOrder("东湖中餐", "新人小", "小", "999", "中餐", "经济", 2),
        CloudOrder("东湖中餐", "李", "大西", "222", "中餐", "经济", 6),   # 云端 4 餐 → 需更新
    ]
    plan = _plan(cli, orders)[0]
    by_name = {c.name: c for c in plan.changes}
    assert by_name["李"].kind == "existing" and by_name["李"].row == 6
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert cli.grid[(5, 7)] == "6", "李下移到第 6 行，总餐次应写在新行号上"
    assert cli.grid[(5, 4)] == "1", "李下移到第 6 行，日期格应写在新行号上"
    assert cli.grid[(4, 0)] == "新人小", "新客户插在小组末尾（原李的位置）"


def test_address_alias_and_case_insensitive_group_match():
    """本地「小西」按别名进云端「小」组（地址也按云端写法落表）；
    「B2」忽略大小写匹配云端「b2」组，但地址按本地原样写。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 7: "3"},
        {0: "李", 1: "b2", 2: "222", 7: "4"},
    ]))
    orders = [
        CloudOrder("东湖中餐", "黄", "小西", "18377144245", "中餐", "经济", 1),
        CloudOrder("东湖中餐", "陈章依", "B2", "15395721282", "中餐", "豪华", 1),
    ]
    plan = _plan(cli, orders)[0]
    rows = {c.name: c.row for c in plan.changes}
    addrs = {c.name: c.address for c in plan.changes}
    # 黄插到小组末（原第 3 行后）= 第 4 行；李(b2) 被挤到第 5 行；
    # 陈章依插到 b2 组末（李原在第 4 行后）= 第 6 行（要算上黄那一行的位移）。
    assert rows == {"黄": 4, "陈章依": 6}
    assert addrs == {"黄": "小", "陈章依": "B2"}
    assert not [w for w in plan.warnings if "地址组" in w]
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert cli.grid[(3, 1)] == "小" and cli.grid[(5, 1)] == "B2"
    assert cli.grid[(4, 0)] == "李", "李(b2) 被黄的插入挤到第 5 行"


def test_unknown_address_appends_to_table_end_with_warning():
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 1: "小", 2: "111", 7: "3"}]))
    orders = [CloudOrder("东湖中餐", "路人", "食堂", "555", "中餐", "经济", 1)]
    plan = _plan(cli, orders)[0]
    assert any("食堂" in w for w in plan.warnings)
    change = plan.changes[0]
    assert change.row == 4 and "追加到表尾" in change.detail
    block = plan.insert_blocks[0]
    assert block.append_only, "表尾追加不需要调用插入行接口"
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert cli.grid[(3, 0)] == "路人"


def test_marker_column_detected_from_existing_weekday_number():
    """协作者的记号位会移动（实测东湖中餐在备注+3 的 Q 列）：
    表头行右侧已有 1~7 数字时，记号写回原位。"""
    header = dict(BASE_HEADER)
    header[16] = "5"                       # 0-based 16 = 第 17 列（Q 列）
    cli = FakeCli(make_grid(header, [{0: "张", 2: "111", 7: "3"}]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)])[0]
    assert plan.marker_col == 17
    apply_plan(cli, [plan], ledger=None, marker_enabled=True)
    assert cli.grid[(1, 16)] == "6", "周五运行记 6，且写回协作者原有的记号位"
    assert cli.grid[(1, 12)] == "9.11 周五", "备注+2 的兜底位不该被误写"


def test_marker_value_is_run_day_not_target_day():
    """记号 = 运行日的周几：周五晚跑、目标周六，应记 6（周五）而不是 7。"""
    header = {0: "名字", 1: "地址", 2: "电话", 3: "9.10 周四", 4: "9.11 周五",
              5: "9.12 周六", 6: "类型", 7: "餐种", 8: "总餐次",
              9: "已出餐", 10: "剩余餐", 11: "备注", 16: "5"}
    cli = FakeCli(make_grid(header, [{0: "张", 2: "111", 8: "3"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    apply_plan(cli, _plan(cli, orders, target=dt.date(2026, 9, 12),
                          run_date=dt.date(2026, 9, 11)),
               ledger=None, marker_enabled=True)
    assert cli.grid[(1, 16)] == "6", "写回协作者原记号位（Q 列），值更新为运行日的周几"


def test_apply_plan_skips_marker_in_test_mode_call():
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    apply_plan(cli, _plan(cli, orders), ledger=None, marker_enabled=False)
    # 第 13 列（index 12）是通讯记号位；BASE_HEADER 里放的是 9.11 表头，
    # 关闭记号时不应被改写成周几数字。
    assert cli.grid[(1, 12)] == "9.11 周五"


def test_insert_failure_rolls_back_inserted_rows():
    """插入成功但写入失败时，必须把插出来的行删掉，云端不能留烂尾空行。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 7: "3"},
        {0: "王", 1: "小", 2: "333", 7: "5"},
        {0: "赵末", 1: "学3", 2: "444", 7: "6"},   # 表尾行，保证「小」组不在表尾
    ]))
    cli.fail_write = True
    orders = [CloudOrder("东湖中餐", "新人小", "小", "999", "中餐", "经济", 2)]
    plan = _plan(cli, orders)[0]
    result = apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert result["failed"] == 1
    assert cli.inserts, "确实调用过插入行接口"
    assert cli.grid[(3, 0)] == "王" and cli.grid[(4, 0)] == "赵末", "回滚后云端恢复原状"
    assert (5, 0) not in cli.grid, "插入的空行已被删除"


def test_write_cells_batches_under_api_limit():
    """单次 update-range-data 有 100 项上限，写入必须自动分批。

    真实事故：东湖中餐一次追加 25 人 ≈ 175 格，接口返回
    ``400001 rangeData length 175 exceeds limit 100``，整张表写入失败。
    """
    calls: list[list[dict]] = []

    class BatchCli(FakeCli):
        """只覆盖底层接口，保留真实的分批逻辑（在 KdocsCli.write_cells 里）。"""

        def _run(self, *args, params=None):
            if args[:2] == ("sheet", "get-sheets-info"):
                return {"detail": {"sheetsInfo": [{"sheetId": 1}]}}
            batch = list(params["rangeData"])
            assert len(batch) <= 100, f"单批 {len(batch)} 项超过接口上限 100"
            calls.append(batch)
            # 把写入反映到网格里，供回读校验使用
            for item in batch:
                self.grid[(item["rowFrom"], item["colFrom"])] = str(item["formula"])
            return {"detail": {}}

        def write_cells(self, file_id, worksheet_id, cells):
            # 走真实实现（父类），从而验证分批
            wc.KdocsCli.write_cells(self, file_id, worksheet_id, cells)

        def read_cell_format(self, file_id, worksheet_id, row, col):
            return {"fonts": {"font_east_asia": "Microsoft YaHei", "size": 10,
                              "color": "#FF000000"},
                    "alignment": {"horizontal": "haCenter", "vertical": "vaCenter"},
                    "cellText": "参考"}
        # write_format_ops 沿用 FakeCli 的记录实现

    cli = BatchCli(make_grid(BASE_HEADER, [{0: "老人", 2: "111", 7: "3"}]))
    # 25 个新客户 × 9 格（基础字段、日期格、两条公式）= 225 格
    orders = [CloudOrder("东湖中餐", f"新{i}", "D2", f"1390000{i:04d}", "中餐", "经济", 1)
              for i in range(25)]
    result = apply_plan(cli, _plan(cli, orders), ledger=None, marker_enabled=False)
    assert len(calls) >= 2, "175 格必须拆成多批"
    assert sum(len(b) for b in calls) == 25 * 9
    assert result["written"] == 1 and result["failed"] == 0


def test_apply_plan_write_error_is_reported_not_raised():
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]), fail_write=True)
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    result = apply_plan(cli, _plan(cli, orders), ledger=None, marker_enabled=False)
    assert result["failed"] == 1 and result["written"] == 0


def test_apply_plan_writes_marker_when_enabled():
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    apply_plan(cli, _plan(cli, orders), ledger=None, marker_enabled=True)
    # 备注列（第 11 列 = index 10）+ 2 = 第 13 列（index 12），第 2 行
    assert cli.grid[(1, 12)] == str(weekday_number(dt.date(2026, 9, 11)))


def test_second_run_writes_nothing():
    """幂等：内容一致时第二次运行零写入。"""
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    apply_plan(cli, _plan(cli, orders), ledger=None, marker_enabled=False)
    before = dict(cli.grid)
    result = apply_plan(cli, _plan(cli, orders), ledger=None, marker_enabled=False)
    assert cli.grid == before
    assert result["sheets"][0]["status"] in {"noop", "ok"}


# ----------------------------------------------------------------------
# 汇总与渲染
# ----------------------------------------------------------------------

def test_summary_counts():
    plans = [SheetPlan(sheet="s", file_id="f", changes=[
        Change("existing", "a", "1", 3, 0, 5, 6, 6, True),    # 已完成
        Change("existing", "b", "2", 4, 3, 5, 1, 4, False),   # 需更新
        Change("new", "c", "3", 5, 2, 5, 0, 2, False),        # 新增
    ])]
    assert summarize_plan(plans) == {"to_update": 1, "to_append": 1,
                                     "unchanged": 1, "warned": 0}


def test_format_plan_mentions_missing_column():
    plans = [SheetPlan(sheet="东湖中餐", file_id="f",
                       target_date=dt.date(2026, 9, 12),
                       warnings=["云端表里没有 9.12 这一列"])]
    text = format_plan(plans)
    assert "东湖中餐" in text and "没有 9.12" in text


# ----------------------------------------------------------------------
# 账本
# ----------------------------------------------------------------------

def test_ledger_roundtrip(tmp_path: Path):
    path = tmp_path / "state.json"
    led = SyncLedger(path)
    assert led.synced_meals("2026-09-11", "F1", "张", "111") is None
    led.record("2026-09-11", "F1", {"张\u0000111": 6})
    led.save()
    again = SyncLedger(path)
    assert again.synced_meals("2026-09-11", "F1", "张", "111") == 6
    summary = again.batch_summary("2026-09-11", "F1")
    assert summary and summary["people"] == 1


def test_ledger_ignores_corrupt_file(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("{ not json", encoding="utf-8")
    led = SyncLedger(path)
    assert led.synced_meals("2026-09-11", "F1", "张", "111") is None


# ----------------------------------------------------------------------
# 本地表读取
# ----------------------------------------------------------------------

def test_read_local_orders(tmp_path: Path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "东湖中餐"
    ws.cell(2, 1, "订单")
    ws.cell(2, 2, "姓名")
    ws.cell(3, 1, "W1")
    ws.cell(3, 2, "张")
    ws.cell(3, 3, "大西")
    ws.cell(3, 4, "19730037965")
    ws.cell(3, 12, "中餐")
    ws.cell(3, 13, "经济")
    ws.cell(3, 14, 6)
    path = tmp_path / "排单.xlsx"
    wb.save(path)

    orders = wc.read_local_orders(path, sheets=["东湖中餐"])
    assert len(orders["东湖中餐"]) == 1
    order = orders["东湖中餐"][0]
    assert (order.name, order.phone, order.meals) == ("张", "19730037965", 6)
    assert order.meal_type == "中餐" and order.meal_kind == "经济"


def test_read_local_orders_missing_file(tmp_path: Path):
    with pytest.raises(WpsCloudError):
        wc.read_local_orders(tmp_path / "nope.xlsx")


# ----------------------------------------------------------------------
# CLI 查找
# ----------------------------------------------------------------------

def test_find_cli_prefers_explicit(tmp_path: Path):
    fake = tmp_path / "kdocs-cli"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    assert find_cli(fake) == str(fake)


def test_find_cli_reports_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(wc.shutil, "which", lambda _name: None)
    monkeypatch.setattr(wc, "__file__", str(tmp_path / "app" / "wps_cloud.py"))
    monkeypatch.setattr(wc.sys, "executable", str(tmp_path / "python"))
    monkeypatch.delattr(wc.sys, "_MEIPASS", raising=False)
    with pytest.raises(WpsCloudError):
        find_cli()


def test_delete_rows_uses_range_data_for_real_cli_shape():
    cli = wc.KdocsCli.__new__(wc.KdocsCli)
    calls = []
    cli._run = lambda *args, **kwargs: calls.append((args, kwargs))
    cli.delete_rows("F1", 1, row=4, count=2)
    params = calls[0][1]["params"]
    assert params["range_data"] == [{"col_from": 0, "col_to": 16383,
                                      "row_from": 3, "row_to": 4}]
    assert params["shift_type"] == "shift_up"


def test_read_grid_with_format_accepts_real_background_field():
    cli = wc.KdocsCli.__new__(wc.KdocsCli)
    cli._run = lambda *args, **kwargs: {"detail": {"rangeData": [{
        "originRow": 2, "originCol": 10, "cellText": "6",
        "cell_background_color": "#FFFFC000",
    }]}}
    assert cli.read_grid("F1", 1, 0, 3, 0, 12, with_format=True) == {
        (2, 10): {"text": "6", "fill": "#FFFFC000"}}


def test_formula_cells_for_new_rows_use_dynamic_date_columns():
    plan = SheetPlan(sheet="东湖中餐", file_id="F1", target_col=8,
                     columns={"total": 12, "served": 13, "left": 14},
                     date_cols=[4, 5, 6, 7, 8, 9])
    cells = wc.formula_cells_for_new_rows(plan, [33, 34])
    assert cells == [
        {"row": 33, "col": 13, "value": "=SUM(D33:I33)"},
        {"row": 33, "col": 14, "value": "=L33-M33"},
        {"row": 34, "col": 13, "value": "=SUM(D34:I34)"},
        {"row": 34, "col": 14, "value": "=L34-M34"},
    ]
