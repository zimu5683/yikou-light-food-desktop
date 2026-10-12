"""Tests for app.wps_cloud —— 云文档同步（全部离线，不联网、不写云端）。

覆盖：目标日期规则、表头解析、人员匹配（含一人多行多槽位）、总餐次增量累加、
协作者 0 保护、批次日期闸门、幂等、列定位（含协作者写错星期的情况）、
写入与回读校验、账本损坏时失败关闭、错误处理。
"""
from __future__ import annotations

import datetime as dt
import re
import tempfile
import uuid
from pathlib import Path

import pytest

from app import wps_cloud as wc
from app.wps_journal import SyncJournal, journal_path_for
from app.wps_cloud import (
    Change, CloudOrder, LedgerCorruptError, SheetPlan, SyncLedger, WpsCloudError,
    apply_plan, build_plan, find_cli, format_plan, parse_date_header,
    normalize_phone, person_key, summarize_plan, target_date_for,
    text_cell_formula, weekday_number,
)


def _ledger_with(directory, date_key: str, file_id: str, name: str, phone: str,
                 slots: list[int], orders: list[str] | None = None):
    """造一个已记录 ``slots`` 的临时账本（模拟"本批已经同步过一次"）。

    ``orders`` 是同一批槽位对应的本地订单号（账本里的槽位身份）；不传就是
    旧账本形态（没有身份信息，只能按行号位置配对）。
    """
    path = Path(directory) / f"state-{uuid.uuid4().hex[:8]}.json"
    ledger = SyncLedger(path)
    payload = {"slots": list(slots), "total": sum(slots)}
    if orders is not None:
        payload["orders"] = list(orders)
    ledger.record(date_key, file_id, {f"{name}\u0000{phone}": payload})
    ledger.save()
    return ledger


def tmp_path_dir() -> Path:
    """给不需要 pytest fixture 的用例造一个临时目录。"""
    return Path(tempfile.mkdtemp(prefix="yikou-wps-ledger-"))

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
        self.sorts: list[dict] = []
        self.deleted_columns: list[tuple[int, int]] = []
        # 只读往返的流水：日期闸门要求"被拒绝的子表一个请求都不发"，靠它断言。
        self.reads: list[tuple] = []
        self.path = "/fake/kdocs-cli"

    def sheets_info(self, file_id: str):
        self.reads.append(("sheets_info", file_id))
        return [{"sheetId": 1, "sheetName": "Sheet1", "rowTo": 200, "colTo": 50}]

    def read_grid(self, file_id, worksheet_id, row_from, row_to, col_from, col_to,
                  *, with_format: bool = False):
        self.reads.append(("read_grid", row_from, row_to, col_from, col_to,
                           bool(with_format)))
        hits = {k: v for k, v in self.grid.items()
                if row_from <= k[0] <= row_to and col_from <= k[1] <= col_to}
        if with_format:
            # 与真实接口一致：只返回非空格；fill 模拟为空（模板测试里由 TemplateCli 覆盖）
            return {k: {"text": _grid_text(v), "fill": ""} for k, v in hits.items()}
        return {k: _grid_text(v) for k, v in hits.items()}

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

    @staticmethod
    def _col_index(letter: str) -> int:
        value = 0
        for char in str(letter).upper():
            value = value * 26 + (ord(char) - 64)
        return value - 1

    def sort_range(self, file_id, worksheet_id, *, range_ref, key, order="asc",
                   header=False, key2=None, order2=None):
        """模拟云端原地排序：按辅助列**稳定**排序区域内所有列。

        真实接口承诺"保留格式与公式、等键保持源顺序"，这里至少要把"行整体搬动、
        所有列一起走"这条语义模拟对 —— 单元格错位是最危险的失败模式。
        """
        import re as _re
        self.sorts.append({"range": range_ref, "key": key, "order": order,
                           "header": header})
        match = _re.match(r"^([A-Z]+)(\d+):([A-Z]+)(\d+)$", str(range_ref))
        assert match, f"排序区域格式非法：{range_ref}"
        c0 = self._col_index(match.group(1))
        r0 = int(match.group(2))
        c1 = self._col_index(match.group(3))
        r1 = int(match.group(4))
        first = r0 - 1 + (1 if header else 0)          # 0-based 首行
        last = r1 - 1                                   # 0-based 末行
        key_col = self._col_index(key)

        def sort_key(row: int):
            raw = str(self.grid.get((row, key_col), "") or "").strip()
            # 空键排最后；键是零填充字符串，字典序即数值序
            return (1, "") if not raw else (0, raw)

        reverse = str(order).lower() == "desc"
        rows = sorted(range(first, last + 1), key=sort_key, reverse=reverse)
        block = {(r, c): v for (r, c), v in self.grid.items()
                 if first <= r <= last and c0 <= c <= c1}
        for r in range(first, last + 1):
            for c in range(c0, c1 + 1):
                self.grid.pop((r, c), None)
        for offset, src in enumerate(rows):
            for c in range(c0, c1 + 1):
                if (src, c) in block:
                    self.grid[(first + offset, c)] = block[(src, c)]

    def delete_columns(self, file_id, worksheet_id, *, column, rows):
        """删除一整列（左移）；排序辅助列的收尾清理。"""
        self.deleted_columns.append((column, rows))
        col = column - 1
        shifted = {}
        for (r, c), v in self.grid.items():
            if c == col:
                continue
            shifted[(r, c - 1 if c > col else c)] = v
        self.grid = shifted

    def authenticated(self) -> bool:
        return True

    # ---- 格式接口（默认：读不到参考格式，于是跳过格式设置）----
    def read_cell_format(self, file_id, worksheet_id, row, col):
        return None


def _grid_text(value):
    """把格子载荷转成文本：允许用例传 ``{"text": ...}`` 模拟带格式读取。"""
    if isinstance(value, dict):
        return str(value.get("text", ""))
    return str(value)


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
          run_date=None, address_order=None, sort=True):
    tables = {"东湖中餐": {"file_id": "F1"}}
    return build_plan(cli, local_orders={"东湖中餐": orders}, tables=tables,
                      target=target, ledger=ledger, marker_enabled=marker,
                      run_date=run_date,
                      address_order={} if address_order is None else address_order,
                      sort_enabled=sort)


def test_new_customer_is_inserted_below_header():
    """新客户插到第 3 行与第 4 行之间（关闭排序时就是第 4 行）。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "Ichi", 2: "111", 7: "3"},
    ]))
    orders = [CloudOrder("东湖中餐", "新人", "小", "222", "中餐", "经济", 6)]
    plans = _plan(cli, orders, sort=False)
    changes = plans[0].changes
    assert len(changes) == 1 and changes[0].kind == "new"
    assert changes[0].total_after == 6 and changes[0].row == 4
    assert [b.position for b in plans[0].insert_blocks] == [4]


def test_existing_customer_total_is_additive():
    """总餐次 = 云端现值 + 本次增量（不是绝对值覆盖）。

    云端 6 + 本批本地 6（账本里本批还没有记录）= 12。写成绝对值会让
    "云端已经累加过的历史餐次"被本地本批值抹掉。
    """
    cli = FakeCli(make_grid(BASE_HEADER, [
        # 真实老行：类型/餐种/已出餐/剩余餐都有值，所以不需要"补全"
        {0: "张", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "6", 8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    change = _plan(cli, orders)[0].changes[0]
    assert change.kind == "existing"
    assert change.total_before == 6 and change.total_after == 12
    assert change.local_meals == 6 and change.ledger_prev is None
    assert change.needs_write is True


def test_total_written_when_mismatch():
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 7: "3"},        # 云端 3，本批本地 6 → 3 + 6 = 9
    ]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    change = _plan(cli, orders)[0].changes[0]
    assert change.total_after == 9 and change.total_before == 3
    assert change.needs_write is True


def test_ledger_delta_is_only_the_difference():
    """账本里本批已同步 6 餐、本地本批变 8 餐 → 只补 +2（不是再加一遍 8）。"""
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张", "111", [6])
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "10", 8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 8)]
    change = _plan(cli, orders, ledger=ledger)[0].changes[0]
    assert change.ledger_prev == 6 and change.total_after == 12


def test_reupload_with_same_ledger_writes_nothing():
    """重复上传（账本已记本批）时增量为 0：日期格已经是 1，一个格子都不写。"""
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张", "111", [6])
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "12", 8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    change = _plan(cli, orders, ledger=ledger)[0].changes[0]
    assert change.total_after == change.total_before == 12
    assert change.needs_write is False


def test_collaborator_zero_in_target_cell_is_never_overwritten():
    """协作者在日期格里写了 0（当天不送）：只读不写，但总餐次照常累加。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 4: "0", 5: "中餐", 6: "经济", 7: "3", 8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    plan = _plan(cli, orders)[0]
    change = plan.changes[0]
    assert change.target_occupied == "0" and change.target_blocked is True
    assert change.total_after == 9, "日期格不改，不表示总餐次也不加"
    assert any("协作者已经写了" in w for w in plan.warnings)

    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert cli.grid[(2, 4)] == "0", "协作者写的 0 必须原样保留"
    assert cli.grid[(2, 7)] == "9"


def test_other_non_empty_marker_values_are_kept():
    """日期格里的其它非空值（例如「不送」）同样只读不写。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 4: "不送", 5: "中餐", 6: "经济", 7: "3", 8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    change = _plan(cli, [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 1)])[0].changes[0]
    assert change.target_blocked is True and change.target_occupied == "不送"


def test_existing_mark_one_keeps_one_and_adds_nothing():
    """已经是 1：不重复写 1，也不额外加餐（增量由账本决定）。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "1", 8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    change = _plan(cli, [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 1)])[0].changes[0]
    assert change.target_ok is True and change.target_blocked is False


def test_multi_row_customer_writes_two_cloud_rows():
    """同一个人本地两行 → 云端两行、各标当天 1，两行各自的餐次独立累加。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "雷", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "2", 8: "=SUM(D3)", 9: "=H3-I3"},
        {0: "雷", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "1", 8: "=SUM(D4)", 9: "=H4-I4"},
    ]))
    orders = [
        CloudOrder("东湖中餐", "雷", "小", "111", "中餐", "经济", 1, row=3, rows=(3,)),
        CloudOrder("东湖中餐", "雷", "小", "111", "中餐", "经济", 1, row=4, rows=(4,)),
    ]
    plan = _plan(cli, orders, sort=False)[0]
    assert [c.slot for c in plan.changes] == [1, 2]
    assert [c.row for c in plan.changes] == [3, 4]
    assert [c.total_after for c in plan.changes] == [3, 2]
    assert any("在本地表里有 2 行" in w for w in plan.warnings)

    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert cli.grid[(2, 4)] == "1" and cli.grid[(3, 4)] == "1"
    assert cli.grid[(2, 7)] == "3" and cli.grid[(3, 7)] == "2"


def test_multi_row_second_order_creates_a_second_cloud_row():
    """本地一天两单而云端只有一行：第二单**新建一行**，不是往第一行叠加。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "雷", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "1", 8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    orders = [
        CloudOrder("东湖中餐", "雷", "小", "111", "中餐", "经济", 1, row=3, rows=(3,)),
        CloudOrder("东湖中餐", "雷", "W5", "111", "中餐", "经济", 1, row=9, rows=(9,)),
    ]
    plan = _plan(cli, orders, sort=False)[0]
    kinds = [(c.kind, c.slot, c.total_after) for c in plan.changes]
    assert kinds == [("existing", 1, 2), ("new", 2, 1)], kinds
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    # 第 2 槽的新行插在第 4 行（0-based 3），老行仍停在第 3 行
    assert cli.grid[(2, 7)] == "2", "第 1 槽：云端 1 + 本批 1 = 2"
    assert cli.grid[(3, 0)] == "雷" and cli.grid[(3, 4)] == "1" and cli.grid[(3, 7)] == "1"


def test_zero_meals_row_is_skipped():
    """本地「餐次」为空/0：不写日期格、不动总餐次（否则白送一餐）。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 4: "0", 5: "中餐", 6: "经济", 7: "3", 8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 0)])[0]
    assert plan.changes == []
    assert any("「餐次」是空的" in w for w in plan.warnings)
    assert cli.grid[(2, 4)] == "0" or True


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
    assert plan.changes[0].total_after == 8    # 云端 2 + 本批 6


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
    assert cli.grid[(2, 7)] == "9", "云端 3 + 本批 6 = 9"


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


# ----------------------------------------------------------------------
# D08：读取预算截断 / 读不完整
# ----------------------------------------------------------------------

class LongSheetCli(FakeCli):
    """used range 比单次读取预算（45,000 格）更长的云端表替身。

    ``rowTo=300, colTo=199`` 时单次读取只覆盖第 1~225 行 —— 正是 D08 的触发条件。
    """

    def __init__(self, grid, *, row_to=300, col_to=199, **kwargs):
        super().__init__(grid, **kwargs)
        self.row_to = row_to
        self.col_to = col_to

    def sheets_info(self, file_id: str):
        info = super().sheets_info(file_id)
        info[0]["rowTo"] = self.row_to
        info[0]["colTo"] = self.col_to
        return info


def long_grid(*, person_row: int, rows: int, name: str = "张三",
              phone: str = "13800000001") -> dict[tuple[int, int], str]:
    """``rows`` 行的云端排单表；第 ``person_row`` 行（1-based）是已有客户。"""
    people = [{0: f"客户{index + 1}", 2: f"1380000{index:04d}", 7: "1"}
              for index in range(rows)]
    people[person_row - 3] = {0: name, 1: "b2", 2: phone, 4: "1",
                              5: "经济餐", 6: "中餐", 7: "10"}
    return make_grid(BASE_HEADER, people)


def test_读取预算截断时已有客户不被当成新客户():
    """D08：第 230 行的老客户不能被漏读成"新客户"（否则会插重复行、餐次分开加）。"""
    cli = LongSheetCli(long_grid(person_row=230, rows=231))
    orders = [CloudOrder("东湖中餐", "张三", "b2", "13800000001", "中餐", "经济", 2)]
    plans = _plan(cli, orders, sort=False)
    change = plans[0].changes[0]
    assert change.kind == "existing"
    assert change.row == 230
    assert (change.total_before, change.total_after) == (10, 12)


def test_读取预算截断时老客户不被插成重复行():
    """D08 的写侧后果：apply_plan 必须写回老客户那一行，而不是插一行新客户。"""
    cli = LongSheetCli(long_grid(person_row=230, rows=231))
    orders = [CloudOrder("东湖中餐", "张三", "b2", "13800000001", "中餐", "经济", 2)]
    plans = _plan(cli, orders, sort=False)
    result = apply_plan(cli, plans, ledger=None, marker_enabled=False)
    assert result["sheets"][0]["status"] == "ok"
    assert cli.inserts == []
    assert cli.grid[(229, 7)] == "12", "第 230 行（0-based 229）总餐次 10 → 12"


def test_表比可完整读取上限更长时写侧阻断并明示():
    """D08：读不完整（used range 超过 400 行）时整表拒绝写入，并说清原因。"""
    cli = LongSheetCli(long_grid(person_row=450, rows=451), row_to=500, col_to=50)
    orders = [CloudOrder("东湖中餐", "张三", "b2", "13800000001", "中餐", "经济", 2)]
    plans = _plan(cli, orders, sort=False)
    plan = plans[0]
    assert plan.blocked_reason and plan.changes == []
    assert "501" in plan.blocked_reason and "401" in plan.blocked_reason
    assert "拒绝" in plan.blocked_reason
    assert cli.reads == [("sheets_info", "F1")], "只允许探测 used range，不读内容"
    assert summarize_plan(plans)["blocked"] == 1
    assert "拒绝写入" in format_plan(plans)

    result = apply_plan(cli, plans, ledger=None, marker_enabled=False)
    assert result["sheets"][0]["status"] == "blocked"
    assert result["sheets"][0]["cloud_writes"] == 0
    assert not cli.writes and not cli.inserts


# 模板行的颜色分布（对齐 BASE_HEADER 与真实表）：名字~餐种白、总餐次/已出餐/剩余餐金黄、备注白
TEMPLATE_COLORS = {c: "#FFFFFFFF" for c in range(1, 15)}
for _c in (8, 9, 10):
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
    assert spec["fills"][8] == "#FFFFC000", "总餐次列必须照抄成黄色"
    assert spec["fills"][9] == "#FFFFC000" and spec["fills"][10] == "#FFFFC000"
    assert spec["fills"][11] == "#FFFFFFFF", "备注列照抄成白色"


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
    assert white["colTo"] == 6, "第 1~7 列（名字~餐种）合并成一个白底区间"
    assert white["xf"]["fill"]["back"]["value"] == wc._argb_to_int("#FFFFFFFF")
    gold = next(op for op in econ
                if op["xf"]["fill"]["back"]["value"] == wc.FILL_LUXURY)
    assert (gold["colFrom"], gold["colTo"]) == (7, 9), "总餐次~剩余餐 三列金黄"




# 真实表里实测到的写法：日期列与「类型」之间夹着一列空列（正式表 L 列就是它）
GAPPED_HEADER = {0: "名字", 1: "地址", 2: "电话", 3: "9.10 周四", 4: "9.11 周五",
                 # 第 5 列（0-based）= F 列：表头与数据都没有内容
                 6: "类型", 7: "餐种", 8: "总餐次", 9: "已出餐", 10: "剩余餐",
                 11: "备注"}


def test_paint_covers_gap_column_between_dates_and_type():
    """日期与「类型」之间夹着空列时，那一格也必须被涂到。

    用户 2026-09-15 反馈：接口不返回空单元格、读不到它的底色，逐列照抄会漏掉它，
    于是那一格保留了插入时从上一行继承的颜色。现在改成 A~餐种 整段刷。
    """
    cli = TemplateCli(make_grid(GAPPED_HEADER, [
        {0: "老人", 1: "D1", 2: "111", 3: "1", 4: "1",
         6: "中餐", 7: "经济", 8: "3", 9: "1", 10: "2", 11: "备注"},
    ]))
    plans = _plan(cli, [CloudOrder("东湖中餐", "经济人", "D2", "13900000001",
                                   "中餐", "经济", 1)], sort=False)
    plan = plans[0]
    apply_plan(cli, plans, ledger=None, marker_enabled=False)

    ops = [op for op in cli.format_ops[0] if op["rowFrom"] == 3]
    covered = set()
    for op in ops:
        covered.update(range(op["colFrom"] + 1, op["colTo"] + 2))
    want = set(range(1, plan.columns["remark"] + 1))
    assert covered >= want, f"这些列没涂到：{sorted(want - covered)}"

    white = next(op for op in ops if op["colFrom"] == 0)
    assert white["colTo"] + 1 == plan.columns["kind"], "A~餐种 一整段（含空列）刷底色"
    assert white["xf"]["fill"]["back"]["value"] == wc._argb_to_int("#FFFFFFFF")
    gap = next(op for op in ops if op["colFrom"] <= 5 <= op["colTo"])
    assert gap["xf"]["fill"]["back"]["value"] == wc._argb_to_int("#FFFFFFFF"), \
        "空列（第 6 列）不能漏掉"
    gold = next(op for op in ops if op["xf"]["fill"]["back"]["value"] == wc.FILL_LUXURY)
    assert gold["colFrom"] + 1 == plan.columns["total"], "金黄带从总餐次列开始"


# ----------------------------------------------------------------------
# 新客户按地址组插入（排序）
# ----------------------------------------------------------------------

def _grouped_grid():
    return FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 7: "3"},
        {0: "李", 1: "大西", 2: "222", 7: "4"},
        {0: "王", 1: "小", 2: "333", 7: "5"},
    ]))


def test_new_customers_insert_as_single_block_at_row4():
    """所有新客户合成一块，统一插到第 3 行与第 4 行之间。"""
    cli = _grouped_grid()
    orders = [
        CloudOrder("东湖中餐", "新人小", "小", "999", "中餐", "经济", 2),
        CloudOrder("东湖中餐", "新人西", "大西", "888", "中餐", "经济", 1),
    ]
    plan = _plan(cli, orders, sort=False)[0]
    assert [(b.position, b.count, b.first_row) for b in plan.insert_blocks] == [(4, 2, 4)]
    rows = {c.name: c.row for c in plan.changes}
    # plan.changes 只包含本地排单表里的人；张/李/王 不在本地表里，看最终表格
    assert rows == {"新人小": 4, "新人西": 5}
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    names = [cli.grid.get((r, 0)) for r in range(2, 8)]
    assert names == ["张", "新人小", "新人西", "李", "王", None], f"实际 {names}"


def test_sort_reorders_whole_table_by_address_order():
    """按列B 的规定顺序重排整张表；清单外的地址排到最后面；组内老行在新行之前。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "大西", 2: "111", 7: "3"},
        {0: "李", 1: "小", 2: "222", 7: "3"},
        {0: "王", 1: "学三", 2: "333", 7: "3"},
    ]))
    order = {"东湖中餐": ["小", "大西", "A1"]}
    plan = _plan(cli, [CloudOrder("东湖中餐", "新人", "大西", "444", "中餐", "经济", 1)],
                 address_order=order)[0]
    rows = {c.name: c.row for c in plan.changes}
    assert rows == {"新人": 5}
    # 清单外地址按设计排到表尾，只作为结构化数据保留，不再逐条报成风险
    assert "学三" in plan.unknown_addresses
    assert plan.sort_range and plan.sort_key_col

    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    names = [cli.grid.get((r, 0)) for r in range(2, 6)]
    assert names == ["李", "张", "新人", "王"], f"实际顺序 {names}"
    assert cli.grid[(4, 1)] == "大西", "新人的地址跟着行一起搬到新位置"


def test_empty_order_list_sorts_addresses_naturally():
    """医学院那种「按列B升序」：自然数序 —— 医2号排在医10号之前。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "甲", 1: "医10号", 2: "111", 7: "3"},
        {0: "乙", 1: "医2号", 2: "222", 7: "3"},
        {0: "丙", 1: "医1号", 2: "333", 7: "3"},
    ]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "新", "医2号", "444", "中餐", "经济", 1)],
                 address_order={"东湖中餐": []})[0]
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    names = [cli.grid.get((r, 0)) for r in range(2, 6)]
    assert names == ["丙", "乙", "新", "甲"], f"实际顺序 {names}"


def test_address_alias_and_case_insensitive_match():
    """本地「小西」按别名进云端「小」组；「B2」忽略大小写匹配，并按清单标准写法落表。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 7: "3"},
        {0: "李", 1: "b2", 2: "222", 7: "4"},
    ]))
    orders = [
        CloudOrder("东湖中餐", "黄", "小西", "18377144245", "中餐", "经济", 1),
        CloudOrder("东湖中餐", "陈章依", "B2", "15395721282", "中餐", "豪华", 1),
    ]
    plan = _plan(cli, orders, address_order={"东湖中餐": ["小", "b2"]})[0]
    addrs = {c.name: c.address for c in plan.changes}
    assert addrs == {"黄": "小", "陈章依": "b2"}
    assert not [w for w in plan.warnings if "不在排序清单" in w]
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    names = [cli.grid.get((r, 0)) for r in range(2, 6)]
    assert names == ["张", "黄", "李", "陈章依"], f"实际顺序 {names}"
    assert cli.grid[(5, 1)] == "b2", "本地写 B2，云端按清单落成 b2"


def test_unknown_address_goes_to_table_end():
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 1: "小", 2: "111", 7: "3"}]))
    orders = [CloudOrder("东湖中餐", "路人", "食堂", "555", "中餐", "经济", 1)]
    plan = _plan(cli, orders, address_order={"东湖中餐": ["小"]})[0]
    assert "食堂" in plan.unknown_addresses
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert cli.grid[(2, 0)] == "张" and cli.grid[(3, 0)] == "路人", "清单外的人排在表尾"


def test_helper_column_is_written_then_deleted():
    """排序用辅助列：写在所有内容列右侧，排序后整列删掉（右侧不留垃圾）。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "b2", 2: "111", 7: "3"},
        {0: "李", 1: "小", 2: "222", 7: "3"},
    ]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "新", "小", "999", "中餐", "经济", 1)],
                 address_order={"东湖中餐": ["小", "b2"]})[0]
    assert cli.sheets_info("F1")[0]["colTo"] + 1 < plan.sort_key_col, "辅助列在内容列右侧"
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert cli.sorts and cli.sorts[0]["key"] == wc.column_name(plan.sort_key_col)
    assert cli.sorts[0]["header"] is False, "第 3 行起排序，首行不是表头"
    assert cli.deleted_columns == [(plan.sort_key_col, plan.last_data_row)]
    left = [v for v in cli.grid.values() if re.fullmatch(r"\d{4}", str(v))]
    assert not left, f"辅助列没清干净：{left}"


# ----------------------------------------------------------------------
# used range 被"整列污染"的表（东湖中餐实测 colTo=16383）必须照样能排序
# ----------------------------------------------------------------------

class BogusWidthCli(FakeCli):
    """``sheetsInfo.colTo`` 报成网格最右列（16383），真实内容只有十来列。"""

    def sheets_info(self, file_id: str):
        info = dict(super().sheets_info(file_id)[0])
        info["colTo"] = 16383
        return [info]


def test_bogus_used_range_does_not_disable_sorting():
    """接口把 used range 报成 16383 时，排序不能被静默跳过。

    这是"东湖中餐排不了序"的回归测试：旧实现直接拿 colTo 当最后使用列，
    算出辅助列 16385 > MAX_SORT_COL，于是 sort_on 被关掉，新客户留在表格最上面。
    """
    cli = BogusWidthCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "大西", 2: "111", 7: "3"},
        {0: "李", 1: "小", 2: "222", 7: "3"},
    ]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "新", "小", "999", "中餐", "经济", 1)],
                 address_order={"东湖中餐": ["小", "大西"]})[0]

    assert plan.sort_enabled, f"应当排序；warnings={plan.warnings}"
    assert not [w for w in plan.warnings if "宽度异常" in w]
    # 辅助列落在"读取范围"右侧（东湖中餐真实内容只到第 22 列，读取上限 200），
    # 关键是它必须是一个**能一起排序**的合理列号，而不是 16385。
    assert plan.sort_key_col <= wc.MAX_SCAN_COL + 2, "辅助列必须落在可排序范围内"
    assert plan.sort_key_col > plan.columns["remark"], "辅助列必须在备注右侧"
    assert wc.column_name(plan.sort_key_col) in plan.sort_range

    result = apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert result["failed"] == 0, result["sheets"]
    names = [cli.grid.get((r, 0)) for r in range(2, 5)]
    # 小 组：老客户李在前、本次新增的「新」在后；大西 组的张排在其后
    assert names == ["李", "新", "张"], f"实际顺序 {names}"


def test_sort_refuses_when_content_sits_past_the_scan_range():
    """辅助列右边还有内容（在读取范围之外）时拒绝排序，并回滚插入 —— 绝不排错位。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "大西", 2: "111", 7: "3"},
        {0: "李", 1: "小", 2: "222", 7: "3"},
    ]))
    # 在第 60 列（辅助列右侧）塞一个"看不见的"内容，模拟读取范围外还有东西
    cli.grid[(2, 59)] = "范围外"
    plan = _plan(cli, [CloudOrder("东湖中餐", "新", "小", "999", "中餐", "经济", 1)],
                 address_order={"东湖中餐": ["小", "大西"]})[0]
    assert plan.sort_enabled

    result = apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert result["failed"] == 1
    assert not cli.sorts, "探测不通过时不该真的发排序请求"
    assert "右侧" in result["sheets"][0]["reason"]
    names = [cli.grid.get((r, 0)) for r in range(2, 5)]
    assert names == ["张", "李", None], f"插入的新行必须被回滚，实际 {names}"


def test_probe_skips_the_helper_column_itself():
    """探测从辅助列右边一格开始：辅助列里的排序键不算"右侧还有内容"。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "大西", 2: "111", 7: "3"},
    ]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "新", "小", "999", "中餐", "经济", 1)],
                 address_order={"东湖中餐": ["小", "大西"]})[0]
    cli.grid[(2, plan.sort_key_col - 1)] = "0000"      # 假装辅助列已被写过键
    ok, detail = wc.probe_sort_area(cli, plan, 1, col_from=plan.sort_key_col + 1,
                                    row_to=plan.last_data_row)
    assert (ok, detail) == (True, "")


def test_sort_failure_rolls_back_inserted_rows():
    """排序失败时行还没被搬动，可以把插进去的新行整块删掉。"""
    class NoSortCli(FakeCli):
        def sort_range(self, *_a, **_k):
            raise WpsCloudError("模拟排序失败")

    cli = NoSortCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "大西", 2: "111", 7: "3"},
        {0: "王", 1: "小", 2: "222", 7: "3"},
    ]))
    result = apply_plan(cli, _plan(cli, [CloudOrder("东湖中餐", "新人", "小", "999",
                                                    "中餐", "经济", 1)],
                                   address_order={"东湖中餐": ["小", "大西"]}),
                        ledger=None, marker_enabled=False)
    assert result["failed"] == 1
    assert cli.inserts, "确实调用过插入行接口"
    assert [cli.grid.get((r, 0)) for r in range(2, 4)] == ["张", "王"], "回滚后恢复原状"


def test_write_failure_after_sort_does_not_delete_rows():
    """排序成功后写入失败：**不回滚删行**（新行已散落到各地址组，删行会删错人）。"""
    class FailAfterSortCli(FakeCli):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.sorted = False

        def sort_range(self, *args, **kwargs):
            super().sort_range(*args, **kwargs)
            self.sorted = True

        def write_cells(self, file_id, worksheet_id, cells):
            if self.sorted:
                raise WpsCloudError("模拟排序后写入失败")
            return super().write_cells(file_id, worksheet_id, cells)

    cli = FailAfterSortCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "大西", 2: "111", 7: "3"},
    ]))
    result = apply_plan(cli, _plan(cli, [CloudOrder("东湖中餐", "新人", "小", "999",
                                                    "中餐", "经济", 1)],
                                   address_order={"东湖中餐": ["小", "大西"]}),
                        ledger=None, marker_enabled=False)
    assert result["failed"] == 1
    assert cli.sorts, "排序已经发生"
    names = [cli.grid.get((r, 0)) for r in range(2, 5)]
    assert "新人" in names, f"新行不能被删掉（实际 {names}）"


def test_actual_sort_result_wins_over_prediction():
    """云端排法与预测不一致时：按**地址定位到的实际行号**写入，并标记 sort_mismatch。

    D01 之后这条的语义更精确了：``plan.final_rows`` 只是**预测/诊断**，写入位置由
    「人 + 地址组 + 组内序号」定位决定（写入前还会逐行核对地址）。所以"实际行号
    与预测不同"不再是风险 —— 只要定位到的还是变更原本那一行，就必须照写不误，
    同时把差异留成 ``sort_mismatch`` 供人工排查。
    """
    class ShuffleCli(FakeCli):
        """故意按降序排，模拟"云端排法与本地预测不一致"。"""

        def sort_range(self, file_id, worksheet_id, **kwargs):
            # 故意按**降序**排（与本地的升序预测不同），模拟"云端排法与预测不一致"
            kwargs["order"] = "desc"
            super().sort_range(file_id, worksheet_id, **kwargs)

    cli = ShuffleCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "大西", 2: "111", 7: "3"},
        {0: "王", 1: "小", 2: "222", 7: "3"},
    ]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "新人", "小", "999", "中餐", "经济", 1),
                       CloudOrder("东湖中餐", "王", "小", "222", "中餐", "经济", 3)],
                 address_order={"东湖中餐": ["小", "大西"]})[0]
    result = apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert result["written"] == 1
    assert result["sheets"][0]["sort_mismatch"] is True
    for change in plan.changes:
        row = change.row
        # 写入前按地址定位并核对：行号可以是实际的，行身份不能错
        assert cli.grid.get((row - 1, 0)) == change.name, \
            f"{change.name} 的写入行号与姓名不符（第 {row} 行）"
        assert cli.grid.get((row - 1, 1)) == change.address, \
            f"{change.name} 写到了地址不符的行（第 {row} 行：" \
            f"{cli.grid.get((row - 1, 1))!r}）"


def test_existing_customer_written_at_post_sort_row():
    """整表重排后，老客户的总餐次/日期格必须写在他们的**新**行号上。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "b2", 2: "111", 7: "3"},
        {0: "李", 1: "小", 2: "222", 7: "3"},
    ]))
    orders = [
        CloudOrder("东湖中餐", "李", "小", "222", "中餐", "经济", 6),      # 老客户，3 → 6
        CloudOrder("东湖中餐", "新人", "小", "999", "中餐", "经济", 1),
    ]
    plan = _plan(cli, orders, address_order={"东湖中餐": ["小", "b2"]})[0]
    rows = {c.name: c.row for c in plan.changes}
    assert rows == {"新人": 4, "李": 3}
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    # 李：云端 3 + 本批 6 = 9；张没有日期格，别写错行
    assert cli.grid[(2, 0)] == "李" and cli.grid[(2, 7)] == "9" and cli.grid[(2, 4)] == "1"
    assert cli.grid[(4, 0)] == "张" and (4, 4) not in cli.grid, "张没有日期格，别写错行"


# ----------------------------------------------------------------------
# 排序后的行定位：排序只按地址重排，变更的身份不能跟着漂（D01）
# ----------------------------------------------------------------------

def _person_rows(cli: FakeCli, name: str, phone: str) -> list[dict[str, str]]:
    """按**内容**（不是行号）找出某人在表里的每一行 —— 断言不依赖绝对行号。

    ``served`` 是这一行的「已出餐」格：老行的公式不会被改写、跟着行走，
    因此可以用它把"原来哪一行"钉住（行号会变，公式里的行号不会）。
    """
    rows: list[dict[str, str]] = []
    for (row, col), value in list(cli.grid.items()):
        if col != 0 or str(value).strip() != name:
            continue
        if wc.normalize_phone(str(cli.grid.get((row, 2), "") or "")) != phone:
            continue
        rows.append({
            "row": str(row + 1),
            "address": str(cli.grid.get((row, 1), "") or "").strip(),
            "mark": str(cli.grid.get((row, 4), "") or "").strip(),
            "total": str(cli.grid.get((row, 7), "") or "").strip(),
            "served": str(cli.grid.get((row, 8), "") or "").strip(),
        })
    return sorted(rows, key=lambda item: int(item["row"]))


def _d01_cloud(*, first: int, second: int,
               first_mark: str = "1", second_mark: str = "1") -> FakeCli:
    """D01 场景的云端表：同一客户两条 B1 旧行 + 另一个人（证明不是"行数变了"）。"""
    return FakeCli(make_grid(BASE_HEADER, [
        {0: "张三", 1: "B1", 2: "111", 4: first_mark, 5: "中餐", 6: "经济",
         7: str(first), 8: "=SUM(D3:E3)", 9: "=H3-I3"},
        {0: "张三", 1: "B1", 2: "111", 4: second_mark, 5: "中餐", 6: "经济",
         7: str(second), 8: "=SUM(D4:E4)", 9: "=H4-I4"},
        {0: "李四", 1: "B1", 2: "222", 4: "1", 5: "中餐", 6: "经济",
         7: "3", 8: "=SUM(D5:E5)", 9: "=H5-I5"},
    ]))


def _d01_orders(first: int, second: int, new: int = 2) -> list[CloudOrder]:
    """张三本地三行：B1 两条旧单（槽位 1、2）+ 新下的 A1 单（槽位 3）。"""
    return [
        CloudOrder("东湖中餐", "张三", "B1", "111", "中餐", "经济", first,
                   row=2, rows=(2,)),
        CloudOrder("东湖中餐", "张三", "B1", "111", "中餐", "经济", second,
                   row=3, rows=(3,)),
        CloudOrder("东湖中餐", "张三", "A1", "111", "中餐", "经济", new,
                   row=9, rows=(9,)),
    ]


def test_多行客户排序后槽位身份不错配():
    """D01 场景 A（``test_multi_row_customer_slots_keep_identity_after_sort``）。

    云端同一客户两条 B1 旧行（10 / 20，当天标记已经是 1）、账本本批已同步
    [6, 1]，本地保留这两行并**新增**一条 A1 的 2 餐订单。开启地址排序后 A1
    排到两条 B1 之前 —— 本地 A1 行与两条 B1 旧行之间没有出现任何数值差异，
    所以定位错行时**只能**表现为：20 餐被新行的 2 覆盖、A1 行漏写，且仍报成功。

    期望：旧行各自保留 10 / 20，A1 新行写入 2 餐与当天标记，合计 32。
    """
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张三", "111", [6, 1])
    cli = _d01_cloud(first=10, second=20)
    plan = _plan(cli, _d01_orders(6, 1), ledger=ledger,
                 address_order={"东湖中餐": []})[0]

    result = apply_plan(cli, [plan], ledger=ledger, marker_enabled=False)

    zhang = _person_rows(cli, "张三", "111")
    a1 = [item for item in zhang if item["address"] == "A1"]
    b1 = [item for item in zhang if item["address"] == "B1"]
    assert len(a1) == 1, f"A1 新行必须存在且只有一条：{zhang}"
    assert a1[0]["total"] == "2" and a1[0]["mark"] == "1", \
        f"A1 新行应写入 2 餐 + 当天标记，实际 {a1[0]}"
    assert sorted(item["total"] for item in b1) == ["10", "20"], \
        f"两条 B1 旧行的餐次必须原样保留，实际 {b1}"
    # 身份而不是行号：老行「已出餐」公式留在自己那一行上，行号变了也不该互换
    by_formula = {item["served"]: item["total"] for item in b1}
    assert by_formula == {"=SUM(D3:E3)": "10", "=SUM(D4:E4)": "20"}, by_formula
    assert sum(int(item["total"]) for item in zhang) == 32, zhang
    assert result["sheets"][0]["status"] == "ok", result["sheets"]
    assert result["sheets"][0]["sort_mismatch"] is False, result["sheets"]
    assert result["failed"] == 0, result["sheets"]


def test_多行客户排序后增量写回原行():
    """D01 场景 B：两条旧行**还没写当天标记**、本地第 1 槽从 6 加到 8 餐。

    这一批每个槽位都有真实写入，而且两条旧行的云端现值相同（10 / 10）：
    按「第几个物理行」定位时，「+2 餐」会落到 A1 新行那一行上、新行的 2 餐
    又会落到第二条旧行上 —— 合计凑巧还是 24 餐，回读校验也照样通过。

    期望：+2 落在**原来那一行**（公式 =SUM(D3:E3) 的老行）上变成 12，
    第二条旧行仍是 10，A1 新行 2 餐 + 当天标记。
    """
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张三", "111", [6, 1])
    cli = _d01_cloud(first=10, second=10, first_mark="", second_mark="")
    plan = _plan(cli, _d01_orders(8, 1), ledger=ledger,
                 address_order={"东湖中餐": []})[0]

    result = apply_plan(cli, [plan], ledger=ledger, marker_enabled=False)

    zhang = _person_rows(cli, "张三", "111")
    a1 = [item for item in zhang if item["address"] == "A1"]
    b1 = [item for item in zhang if item["address"] == "B1"]
    assert len(a1) == 1 and a1[0]["total"] == "2" and a1[0]["mark"] == "1", \
        f"A1 新行应写入 2 餐 + 当天标记，实际 {a1}"
    by_formula = {item["served"]: (item["total"], item["mark"]) for item in b1}
    assert by_formula["=SUM(D3:E3)"] == ("12", "1"), \
        f"第 1 槽的 +2 餐必须落在原来那一行上：{by_formula}"
    assert by_formula["=SUM(D4:E4)"] == ("10", "1"), \
        f"第 2 槽不应被加餐：{by_formula}"
    assert sum(int(item["total"]) for item in zhang) == 24, zhang
    assert result["sheets"][0]["status"] == "ok", result["sheets"]
    assert result["sheets"][0]["sort_mismatch"] is False, result["sheets"]


def test_排序后地址回读不符必须失败():
    """D01：排序后定位不到变更原本那一行时，必须报失败 —— 不许猜行号继续写。

    内存云表在排序之后把 A1 那行的地址列改掉（模拟地址列没被排序区带着走、
    或表在排序后被协作者改过）：此时「A1 组里的第 1 行」证明不了是哪一行。
    期望 fail-closed：一个餐次/标记格子都不写，且失败原因里说清是**地址**
    对不上（旧实现会继续按错位行号写下去，只有数值凑巧对不上时才会被发现）。
    """
    class AddressMovedCli(FakeCli):
        """排序完成后改掉 A1 行的地址列。"""

        def sort_range(self, *args, **kwargs):
            super().sort_range(*args, **kwargs)
            for (row, col), value in list(self.grid.items()):
                if col == 1 and str(value).strip() == "A1":
                    self.grid[(row, col)] = "C9"

    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张三", "111", [6, 1])
    cli = AddressMovedCli(make_grid(BASE_HEADER, [
        {0: "张三", 1: "B1", 2: "111", 4: "", 5: "中餐", 6: "经济",
         7: "10", 8: "=SUM(D3:E3)", 9: "=H3-I3"},
        {0: "张三", 1: "B1", 2: "111", 4: "", 5: "中餐", 6: "经济",
         7: "10", 8: "=SUM(D4:E4)", 9: "=H4-I4"},
    ]))
    plan = _plan(cli, _d01_orders(8, 1), ledger=ledger,
                 address_order={"东湖中餐": []})[0]

    result = apply_plan(cli, [plan], ledger=ledger, marker_enabled=False)

    sheet = result["sheets"][0]
    assert sheet["status"] in {"failed", "verify_failed"}, sheet
    assert sheet["uncertain"] is True, sheet
    detail = " ".join([*(sheet.get("problems") or []), str(sheet.get("reason") or "")])
    assert "地址" in detail, f"失败原因要带上地址口径：{sheet}"
    # fail-closed：这个人的每一个格子都没动（+2 餐没写、新行没标当天 1）
    zhang = _person_rows(cli, "张三", "111")
    assert sorted(int(item["total"]) for item in zhang if item["total"]) == [10, 10], zhang
    assert not [item for item in zhang if item["mark"]], zhang
    assert result["failed"] == 1, result["sheets"]


def test_同地址组内新增行排在既有行之后():
    """D01 补充：同一客户在同一**地址组**里既有两行、本批又新增一行。

    排序键是「地址名次 + 组内位置」（既有 0 < 新增 1），所以组内顺序是
    「两条既有行（保持原顺序）+ 新行」。组内序号也按这个口径记，第 3 槽位就是
    组内第 3 行 —— 用「已出餐」公式钉住身份，谁都不许挪位。
    """
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张三", "111",
                          [6, 1, 0])
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张三", 1: "B1", 2: "111", 4: "1", 5: "中餐", 6: "经济",
         7: "10", 8: "=SUM(D3:E3)", 9: "=H3-I3"},
        {0: "张三", 1: "B1", 2: "111", 4: "1", 5: "中餐", 6: "经济",
         7: "20", 8: "=SUM(D4:E4)", 9: "=H4-I4"},
        {0: "李四", 1: "A1", 2: "222", 4: "1", 5: "中餐", 6: "经济",
         7: "3", 8: "=SUM(D5:E5)", 9: "=H5-I5"},
    ]))
    orders = [*_d01_orders(6, 1)[:2],
              CloudOrder("东湖中餐", "张三", "B1", "111", "中餐", "经济", 4,
                         row=9, rows=(9,))]
    plan = _plan(cli, orders, ledger=ledger, address_order={"东湖中餐": []})[0]
    assert [(c.address_in_sheet, c.group_index, c.group_total)
            for c in plan.changes] == [("B1", 1, 3), ("B1", 2, 3), ("B1", 3, 3)]

    result = apply_plan(cli, [plan], ledger=ledger, marker_enabled=False)

    assert result["sheets"][0]["status"] == "ok", result["sheets"]
    zhang = _person_rows(cli, "张三", "111")
    assert sorted(item["total"] for item in zhang) == ["10", "20", "4"], zhang
    assert [item["served"] for item in zhang] == ["=SUM(D3:E3)", "=SUM(D4:E4)",
                                                 "=SUM(D6:E6)"], zhang
    assert [item["mark"] for item in zhang] == ["1", "1", "1"], zhang


def test_回读地址与变更不符时校验失败():
    """D01：写入后回读的地址列与变更不符 → ``verify_failed``，且不更新账本。

    定位阶段读到的地址是对的，此后地址列被改（并发改动 / 写入没落到地址列）：
    姓名与餐次都可能照样相符，**只有地址**是"这一行还是原来那一行"的凭据。
    旧实现的回读校验不含地址列，这种错配会被判成成功。
    """
    class AddressClobberCli(FakeCli):
        """排序之后的那一次写入（带总餐次的那批）顺手把地址列改掉。"""

        def write_cells(self, file_id, worksheet_id, cells):
            super().write_cells(file_id, worksheet_id, cells)
            if 8 in {int(cell["col"]) for cell in cells}:     # 第 8 列 = 总餐次
                for (row, col), value in list(self.grid.items()):
                    if col == 1 and str(value).strip() == "A1":
                        self.grid[(row, col)] = "C9"

    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张三", "111", [6, 1])
    cli = AddressClobberCli(make_grid(BASE_HEADER, [
        {0: "张三", 1: "B1", 2: "111", 4: "", 5: "中餐", 6: "经济",
         7: "10", 8: "=SUM(D3:E3)", 9: "=H3-I3"},
        {0: "张三", 1: "B1", 2: "111", 4: "", 5: "中餐", 6: "经济",
         7: "10", 8: "=SUM(D4:E4)", 9: "=H4-I4"},
    ]))
    plan = _plan(cli, _d01_orders(8, 1), ledger=ledger,
                 address_order={"东湖中餐": []})[0]

    result = apply_plan(cli, [plan], ledger=ledger, marker_enabled=False)

    sheet = result["sheets"][0]
    assert sheet["status"] in {"verify_failed", "failed"}, sheet
    assert sheet["uncertain"] is True, sheet
    problems = sheet.get("problems") or []
    assert any("地址" in str(item) for item in problems), sheet
    # 校验没过就不许记进账本（否则下次会把没写对的那批当成已同步）
    assert SyncLedger(ledger.path).synced_slots("2026-09-11", "F1", "张三", "111") == [6, 1]


def test_missing_meal_columns_are_completed():
    """半成品行自愈：老客户缺类型/餐种/公式时补齐（上次中断后的补救）。"""
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 1: "小", 2: "111", 7: "6"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "豪华", 6)]
    plan = _plan(cli, orders, sort=False)[0]
    change = plan.changes[0]
    assert change.fill_type and change.fill_kind and change.fill_formula
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert cli.grid[(2, 5)] == "中餐" and cli.grid[(2, 6)] == "豪华"
    assert cli.grid[(2, 8)] == "=SUM(D3:E3)" and cli.grid[(2, 9)] == "=H3-I3"


def test_sort_disabled_keeps_new_rows_on_top():
    """排序开关关闭：新行留在第 4 行起，不做任何重排（应急模式）。"""
    cli = _grouped_grid()
    orders = [CloudOrder("东湖中餐", "新人", "小", "999", "中餐", "经济", 1)]
    plan = _plan(cli, orders, sort=False)[0]
    assert plan.sort_key_col == 0 and plan.sort_range == "" and not plan.sort_enabled
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    names = [cli.grid.get((r, 0)) for r in range(2, 6)]
    assert names == ["张", "新人", "李", "王"], f"实际顺序 {names}"


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


def test_date_region_stops_before_first_structural_column():
    """日期列区间 = 电话右侧 ~ 第一个结构列左侧（衣锦中餐有 114 个日期列，同理）。"""
    assert wc.date_region({"phone": 4, "type": 11, "kind": 12, "total": 13,
                           "served": 14, "left": 15, "remark": 16}) == (5, 10)
    wide = {"phone": 4, "type": 118, "kind": 119, "total": 120,
            "served": 121, "left": 122, "remark": 123}
    assert wc.date_region(wide) == (5, 117)
    assert wc.date_region({"phone": 4}) == (5, wc.MAX_SCAN_COL)


def test_collaborator_marker_column_is_not_a_date_column():
    """协作者写在备注右侧的「9.11 周五」是标记，不是日期列。

    真实缺陷：date_cols 原来扫描整行表头，把标记列也算进去，
    新行的「已出餐」公式因此变成 =SUM(D:E:M)（多统计了标记列）。
    """
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "老人", 1: "小", 2: "111", 7: "3"}]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "新人", "小", "222", "中餐", "经济", 1)],
                 sort=False)[0]
    assert plan.date_cols == [4, 5], f"实际 {plan.date_cols}（13 是协作者的标记列）"
    assert any("已忽略日期区间外的日期样式格" in w for w in plan.warnings)
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    row = plan.changes[0].row
    assert cli.grid[(row - 1, 8)] == f"=SUM(D{row}:E{row})", "公式不能含标记列"
    assert cli.grid[(1, 12)] == "9.11 周五", "协作者的标记格一个字节都不能动"


def test_target_date_is_not_written_into_marker_column():
    """当天没有真实日期列时，宁可不写，也不能把 1 写进协作者的标记格。"""
    header = dict(BASE_HEADER)
    header.pop(4)                       # 去掉真正的 9.11 列，只留备注右侧的标记
    cli = FakeCli(make_grid(header, [{0: "张", 2: "111", 7: "6"}]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)])[0]
    assert plan.target_col == 0
    assert any("没有 9.11" in w for w in plan.warnings)
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert cli.grid[(1, 12)] == "9.11 周五", "协作者的标记格必须原样不动"


def test_marker_column_guard_avoids_date_like_cell():
    """记号兜底位是备注+3：备注+2 被协作者的日期标记占着。"""
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)])[0]
    assert plan.marker_col == 14        # 备注列 11 + 3


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
    # 关闭排序：本用例只验证分批，25 人 × 9 格 = 225 格
    result = apply_plan(cli, _plan(cli, orders, sort=False), ledger=None,
                        marker_enabled=False)
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
    # 备注列（第 11 列 = index 10）+ 3 = 第 14 列（index 13），第 2 行；
    # 备注+2（index 12）是协作者的日期标记，绝不能被写
    assert cli.grid[(1, 13)] == str(weekday_number(dt.date(2026, 9, 11)))
    assert cli.grid[(1, 12)] == "9.11 周五"


def test_second_run_writes_nothing():
    """幂等：账本已记本批时第二次运行零写入（增量 0、日期格已是 1）。"""
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张", "111", [6])
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "9", 8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    first = apply_plan(cli, _plan(cli, orders, ledger=ledger), ledger=ledger,
                       marker_enabled=False)
    assert first["sheets"][0]["status"] == "noop", first["sheets"]
    before = dict(cli.grid)
    second = apply_plan(cli, _plan(cli, orders, ledger=ledger), ledger=ledger,
                        marker_enabled=False)
    assert cli.grid == before
    assert second["sheets"][0]["status"] in {"noop", "ok"}
    assert not cli.writes, "第二次运行不应发出任何写请求"


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
                                     "unchanged": 1, "warned": 0, "skipped": 0,
                                     "blocked": 0}


def test_summary_counts_blocked_sheet_and_skipped_cells():
    """汇总要把"整表被拒绝"和"日期格被协作者占用"分别计数出来。"""
    blocked = SheetPlan(sheet="s1", file_id="f", blocked_reason="星期不符")
    skipped = SheetPlan(sheet="s2", file_id="f2", changes=[
        Change("existing", "a", "1", 3, 3, 5, 1, 4, False, target_occupied="0"),
    ])
    summary = summarize_plan([blocked, skipped])
    assert summary["blocked"] == 1 and summary["skipped"] == 1


def test_format_plan_renders_blocked_sheet():
    plans = [SheetPlan(sheet="东湖中餐", file_id="f", blocked_reason="星期标记是「周六」")]
    text = format_plan(plans)
    assert "一个格子都没动" in text and "星期标记是「周六」" in text


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


def test_ledger_rejects_corrupt_file(tmp_path: Path):
    """账本损坏必须失败关闭：不能当成空账本把同一批餐再加一遍。"""
    path = tmp_path / "state.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(LedgerCorruptError):
        SyncLedger(path)


def test_ledger_rejects_structurally_broken_payload(tmp_path: Path):
    """截断/半截写入（缺 people、条目缺锚点）同样必须拒绝读取。"""
    path = tmp_path / "state.json"
    path.write_text('{"version": 1, "batches": {"2026-09-11": {"F1": {}}}}',
                    encoding="utf-8")
    with pytest.raises(LedgerCorruptError):
        SyncLedger(path)
    path.write_text('{"version": 1, "batches": {"2026-09-11": {"F1": '
                    '{"people": {"张\\u0000111": {"at": "x"}}}}}}', encoding="utf-8")
    with pytest.raises(LedgerCorruptError):
        SyncLedger(path)


def test_ledger_reads_legacy_meals_field(tmp_path: Path):
    """旧账本只有 meals：当成第 1 个槽位，升级后同一批不会被重复加一次。"""
    path = tmp_path / "state.json"
    path.write_text(
        '{"version": 1, "batches": {"2026-09-11": {"F1": {"synced_at": "x", '
        '"people": {"张\\u0000111": {"meals": 6, "at": "2026-09-11T20:00:00"}}}}}}',
        encoding="utf-8")
    ledger = SyncLedger(path)
    assert ledger.synced_local("2026-09-11", "F1", "张", "111") == 6
    assert ledger.synced_slots("2026-09-11", "F1", "张", "111") == [6]


def test_ledger_records_slots_and_survives_concurrent_save(tmp_path: Path):
    """record 写入 slots/local/total；两个账本对象先后 save 不互相覆盖。"""
    path = tmp_path / "state.json"
    first = SyncLedger(path)
    first.record("2026-09-11", "F1", {"甲\u00001": {"slots": [1, 2], "total": 5}})
    first.save()
    second = SyncLedger(path)
    second.record("2026-09-11", "F1", {"乙\u00002": {"slots": [3], "total": 3}})
    second.save()
    merged = SyncLedger(path)
    assert merged.synced_slots("2026-09-11", "F1", "甲", "1") == [1, 2]
    assert merged.synced_slots("2026-09-11", "F1", "乙", "2") == [3]
    assert merged.synced_total("2026-09-11", "F1", "乙", "2") == 3


# ----------------------------------------------------------------------
# 批次日期闸门（零云端写请求）
# ----------------------------------------------------------------------

def _weekday_grid(rows):
    """按 2026-09-12（周六）的本地行造一张云端表 + 一批带星期标记的订单。"""
    cli = FakeCli(make_grid(BASE_HEADER, rows))
    return cli


def test_batch_date_mismatch_blocks_whole_sheet_with_zero_requests():
    """本地表的星期标记全是别的日期：整表拒绝，一个云端请求都不发。"""
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6, row=3,
                         weekday_marks=("周五",))]
    plans = build_plan(cli, local_orders={"东湖中餐": orders},
                       tables={"东湖中餐": {"file_id": "F1"}},
                       target=dt.date(2026, 9, 12), ledger=None,
                       run_date=dt.date(2026, 9, 11))
    plan = plans[0]
    assert plan.blocked_reason and plans[0].changes == []
    assert cli.reads == [], "被拒绝的子表连只读请求都不该发"

    result = apply_plan(cli, plans, ledger=None, marker_enabled=False)
    assert result["sheets"][0]["status"] == "blocked"
    assert result["sheets"][0]["cloud_writes"] == 0
    assert not cli.writes and not cli.inserts


def test_batch_date_partial_mismatch_only_warns():
    """只有部分行标记不符：只警告，照常写入（可能是个别行来自别的批次）。"""
    cli = FakeCli(make_grid(BASE_HEADER, []))
    orders = [
        CloudOrder("东湖中餐", "甲", "小", "111", "中餐", "经济", 1, row=3,
                   weekday_marks=("周六",)),
        CloudOrder("东湖中餐", "乙", "小", "222", "中餐", "经济", 1, row=4,
                   weekday_marks=("周五",)),
    ]
    plan = build_plan(cli, local_orders={"东湖中餐": orders},
                      tables={"东湖中餐": {"file_id": "F1"}},
                      target=dt.date(2026, 9, 12), ledger=None)
    assert plan[0].blocked_reason == ""
    assert any("不是「周六」" in w for w in plan[0].warnings)


def test_batch_date_without_marks_only_warns():
    """人工做的表没有任何星期标记：只警告，照常写入。"""
    cli = FakeCli(make_grid(BASE_HEADER, []))
    orders = [CloudOrder("东湖中餐", "甲", "小", "111", "中餐", "经济", 1, row=3)]
    plan = build_plan(cli, local_orders={"东湖中餐": orders},
                      tables={"东湖中餐": {"file_id": "F1"}},
                      target=dt.date(2026, 9, 12), ledger=None)
    assert plan[0].blocked_reason == ""
    assert any("没有星期标记" in w for w in plan[0].warnings)


def test_read_local_orders_captures_weekday_marks_and_order_no(tmp_path: Path):
    """本地读取层要把「订单」列与周一~周日标记带出来（日期闸门靠它）。"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "东湖中餐"
    ws.cell(2, 2, "姓名")
    ws.cell(3, 1, "W25")
    ws.cell(3, 2, "雷丝淇")
    ws.cell(3, 3, "小")
    ws.cell(3, 4, "13800000000")
    ws.cell(3, 11, "1")            # 第 11 列 = 周日
    ws.cell(3, 14, 2)              # 餐次
    path = tmp_path / "排单.xlsx"
    wb.save(path)
    orders = wc.read_local_orders(path, sheets=("东湖中餐",))
    assert len(orders["东湖中餐"]) == 1
    order = orders["东湖中餐"][0]
    assert order.order_no == "W25"
    assert order.weekday_marks == ("周日",)
    assert order.local_rows == (3,)


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


def test_read_grid_prefers_the_original_value_over_display_text():
    """回读优先取**原值**：12 位电话显示成科学计数时也不能变形。

    2026-10-12 医学院中餐事故的根因就是这个：写进去 `159678999961`，平台把它
    存成数字、`cellText` 显示成 `1.59679E+11`，按显示值算人键就找不到人。
    """
    cli = wc.KdocsCli.__new__(wc.KdocsCli)
    cli._run = lambda *args, **kwargs: {"detail": {"rangeData": [
        {"originRow": 2, "originCol": 2,
         "originalCellValue": "159678999961", "cellText": "1.59679E+11"},
        {"originRow": 3, "originCol": 2,
         "originalCellValue": 159678999961.0, "cellText": "1.59679E+11"},
        # 老响应没有原值字段：退回显示文本，行为与改动前一致
        {"originRow": 4, "originCol": 0, "cellText": "张"},
        # 空格子不返回
        {"originRow": 5, "originCol": 2, "originalCellValue": "", "cellText": ""},
    ]}}
    grid = cli.read_grid("F1", 1, 0, 5, 0, 12)
    assert grid == {(2, 2): "159678999961", (3, 2): "159678999961", (4, 0): "张"}


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


def test_empty_cloud_table_starts_at_first_data_row():
    """空表（还没有任何人）直接写第 3 行起：不插行、不留空行、不排序。"""
    cli = FakeCli(make_grid(BASE_HEADER, []))
    plans = _plan(cli, [CloudOrder("东湖中餐", "首单", "小", "123", "中餐", "经济", 6)],
                  address_order={"东湖中餐": ["小"]})
    plan = plans[0]
    assert plan.changes[0].row == 3
    assert plan.insert_blocks and plan.insert_blocks[0].append_only
    apply_plan(cli, plans, ledger=None, marker_enabled=False)
    assert cli.inserts == [], "空表不需要调用插入行接口"
    assert cli.grid[(2, 0)] == "首单" and cli.grid[(2, 4)] == "1"


def test_interior_blank_row_is_not_dumped_at_table_end():
    """表中间的空行（分组之间的分隔行）要留在原分组里，不能被排序甩到表尾。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "甲", 1: "小", 2: "111", 7: "3"},
        {0: "乙", 1: "小", 2: "222", 7: "3"},
        {11: "分隔行"},                       # 第 5 行：没有姓名，只有别列内容
        {0: "丙", 1: "大西", 2: "333", 7: "3"},
    ]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "新人", "小", "999", "中餐", "经济", 1)],
                 address_order={"东湖中餐": ["小", "大西"]})[0]
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    names = [cli.grid.get((r, 0)) for r in range(2, 7)]
    assert names == ["甲", "乙", "新人", None, "丙"], f"实际 {names}"
    assert cli.grid.get((5, 11)) == "分隔行", "分隔行留在小组之后、大西之前"


# ----------------------------------------------------------------------
# 生效目标（effective_tables）：闪时送云端名单读取与云同步共用同一份判定
# ----------------------------------------------------------------------


class _Cfg:
    def __init__(self, **kwargs):
        self.wps_test_mode = False
        self.wps_tables = {}
        self.wps_production_tables = {}
        self.wps_test_tables = {}
        for key, value in kwargs.items():
            setattr(self, key, value)


PROD = {"东湖中餐": {"file_id": "prod-lunch"},
        "东湖晚餐": {"file_id": "prod-dinner"}}


def test_effective_tables_production_mode():
    config = _Cfg(wps_tables={"东湖中餐": {"file_id": "prod-lunch"}},
                  wps_production_tables=PROD)
    assert wc.effective_tables(config) == {"东湖中餐": {"file_id": "prod-lunch"}}


def test_effective_tables_rejects_non_production_id():
    config = _Cfg(wps_tables={"东湖中餐": {"file_id": "other"}},
                  wps_production_tables=PROD)
    with pytest.raises(WpsCloudError, match="非正式表"):
        wc.effective_tables(config)


def test_effective_tables_test_mode_uses_copies_and_rejects_production():
    config = _Cfg(wps_test_mode=True, wps_production_tables=PROD,
                  wps_test_tables={"东湖中餐": "copy-lunch"})
    assert wc.effective_tables(config) == {"东湖中餐": {"file_id": "copy-lunch"}}

    config.wps_test_tables = {}
    with pytest.raises(WpsCloudError, match="未配置新的测试副本"):
        wc.effective_tables(config)

    config.wps_test_tables = {"东湖中餐": "prod-lunch"}
    with pytest.raises(WpsCloudError, match="包含正式表 ID"):
        wc.effective_tables(config)

    config.wps_test_tables = {"东湖中餐": "H8vzKoTJVrMP7mA9QG591xqS9W8Bg57iG"}
    with pytest.raises(WpsCloudError, match="已过期试验田"):
        wc.effective_tables(config)


# ----------------------------------------------------------------------
# 值原样上传（2026-10-12 需求，对应医学院中餐事故）
#
# 事故链：本地电话 `159678999961`（12 位）→ 平台按数字存 → 显示 `1.59679E+11`
# → 回读只取显示值 → 人键 `('mer','15967911')` 与本地对不上 → 「排序后定位不到」。
# 本组用例把"写进去 / 读回来 / 显示出来"三处的口径钉死。
# ----------------------------------------------------------------------

def test_person_key_normalizes_numeric_phones():
    """人键按**值**算：数值型、`.0` 尾巴都还原成同一串数字。

    注意口径的边界：`1.59679E+11` 这种**已经丢精度的显示文本**永远救不回来
    （它只剩 6 位有效数字）——本项目的做法是不让它出现：写入按文本（前置单引号）、
    回读优先取原值。已损坏的线上格子属于遗留数据，要人工改回文本。
    """
    expected = "159678999961"
    for raw in ("159678999961", "159678999961.0", 159678999961):
        assert person_key("mer", raw)[1] == expected, raw
    # 客户怎么填就怎么留：空格/连字符/+86/前导 0 只丢符号、不做位数校验
    assert normalize_phone("159 6789 9961") == "15967899961"
    assert normalize_phone("+86 159-6789-9961") == "8615967899961"
    assert normalize_phone("015967899961") == "015967899961"


def test_cell_value_text_keeps_numeric_strings_intact():
    assert wc.cell_value_text("159678999961") == "159678999961"
    assert wc.cell_value_text(159678999961.0) == "159678999961"
    assert wc.cell_value_text(159678999961) == "159678999961"
    assert wc.cell_value_text(None) == ""
    assert wc.cell_value_text(" 张 ") == "张"


def test_text_cell_formula_quotes_digit_strings_only():
    """纯数字按文本写（前置单引号），其余值原样写、不改写。"""
    assert text_cell_formula("159678999961") == "'159678999961"
    assert text_cell_formula("015967899961") == "'015967899961"
    assert text_cell_formula("张") == "张"
    assert text_cell_formula("159 6789 9961") == "159 6789 9961"
    assert text_cell_formula("+86 159-6789-9961") == "+86 159-6789-9961"


def test_phone_updates_are_located_by_value_not_display_text():
    """整表重排后按值重新定位：12 位电话的人必须还能被找到。

    走的是事故同款路径：表里有 `mer/159678999961`，本地再给他加 3 餐，
    必须定位到他、把总餐次从 4 累加到 7（而不是报「排序后定位不到」）。
    """
    grid = make_grid(BASE_HEADER, [{0: "mer", 1: "小", 2: "159678999961", 7: "4"}])
    cli = FakeCli(grid)
    plans = _plan(cli, [CloudOrder("东湖中餐", "mer", "小", "159678999961",
                                   "中餐", "经济", 3)],
                  address_order={"东湖中餐": ["小"]})
    result = apply_plan(cli, plans, ledger=None, marker_enabled=False)
    assert result["failed"] == 0, result["sheets"]

    totals = [op for op in cli.writes if any(c["col"] == 8 for c in op["cells"])]
    assert totals, cli.writes
    total_cells = [c for op in totals for c in op["cells"] if c["col"] == 8]
    assert [c["value"] for c in total_cells] == [7]


def test_new_customer_phone_is_written_as_text_so_it_displays_as_filled():
    """写新客户：电话列按文本写（平台才不会把它存成数字、显示成科学计数）。

    姓名/地址等非数字值原样写；餐种等业务值也不带引号。
    """
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "老客户", 1: "小", 2: "111", 7: "3"}]))
    plans = _plan(cli, [CloudOrder("东湖中餐", "新人", "小", "159678999961",
                                   "中餐", "经济", 6)])
    # 关排序：直接看"写新行"这一批格子，不被排序/重定位的中间态干扰
    plans[0].sort_enabled = False
    result = apply_plan(cli, plans, ledger=None, marker_enabled=False)
    assert result["failed"] == 0, result["sheets"]

    phone_col = plans[0].columns["phone"]
    name_col = plans[0].columns["name"]
    written = {c["col"]: c["value"] for op in cli.writes for c in op["cells"]}
    assert written[phone_col] == "'159678999961", written
    assert written[name_col] == "新人", "非数字值不得被改写"
    # 回读校验用同一个值拿到的文本，与本地一致
    assert cli.grid[(plans[0].changes[0].row - 1, phone_col - 1)] == "'159678999961"


def test_sync_is_idempotent_for_text_phone_values():
    """同一批数据第二次同步 = no-op（值一致，不重复加餐、不插行）。"""
    grid = make_grid(BASE_HEADER, [{0: "mer", 1: "小", 2: "'159678999961", 7: "4"}])
    cli = FakeCli(grid)
    orders = [CloudOrder("东湖中餐", "mer", "小", "159678999961", "中餐", "经济", 4)]
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1",
                          "mer", "159678999961", [4])

    first = _plan(cli, orders, ledger=ledger, sort=False)
    apply_plan(cli, first, ledger=ledger, marker_enabled=False)

    second = _plan(cli, orders, ledger=ledger, sort=False)
    assert second[0].changes[0].needs_write is False, "第二次不应再写任何格子"
    result = apply_plan(cli, second, ledger=ledger, marker_enabled=False)
    assert result["failed"] == 0
    assert not cli.inserts, "不得插行"
    assert cli.grid[(2, 7)] == "4", "总餐次不能被重复累加"


# ----------------------------------------------------------------------
# 槽位身份与同步基准：补单重跑 / 临时缺行 / 先减后恢复
# （缺陷排查记录 D02 / D03 / D04 的回归锁）
# ----------------------------------------------------------------------

def _w_order(meals: int, *, order_no: str, row: int) -> CloudOrder:
    """本地排单表里「张」的一行（``order_no`` = 本地「订单」列的 W 编号）。

    订单处理按 W 编号从大到小写行（``app/automation.py``），所以补单会让新单
    排在旧单前面 —— 槽位配对必须靠订单身份，不能靠行号。
    """
    return CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", meals,
                      row=row, order_no=order_no, rows=(row,),
                      weekday_marks=("周五",))


def _cli_with_unrelated() -> FakeCli:
    """云端只有一行无关的人：被观测的人从「新建行」开始。"""
    return FakeCli(make_grid(BASE_HEADER, [
        {0: "李四", 1: "小", 2: "222", 4: "1", 5: "中餐", 6: "经济", 7: "1",
         8: "=SUM(D3)", 9: "=H3-I3"},
    ]))


def _cloud_rows(cli: FakeCli, name: str = "张") -> list[tuple[int, int, str]]:
    """内存云表里某人的每一行：(1-based 行号, 总餐次, 当天标记)。"""
    rows = []
    for (row, col), value in list(cli.grid.items()):
        if col == 0 and str(value).strip() == name:
            rows.append((row + 1,
                         int(str(cli.grid.get((row, 7), "") or "") or 0),
                         str(cli.grid.get((row, 4), "") or "").strip()))
    return sorted(rows)


def _cloud_total(cli: FakeCli, name: str = "张") -> int:
    return sum(total for _row, total, _mark in _cloud_rows(cli, name))


def _disk_slots(path, name: str = "张", phone: str = "111"):
    return SyncLedger(path).synced_slots("2026-09-11", "F1", name, phone)


def _disk_orders(path, name: str = "张", phone: str = "111"):
    return SyncLedger(path).synced_orders("2026-09-11", "F1", name, phone)


def _disk_positions(path, name: str = "张", phone: str = "111"):
    return SyncLedger(path).synced_positions("2026-09-11", "F1", name, phone)


def test_补单重跑按订单身份配对槽位():
    """D02：补单后本地行序变成 [W11, W5]，旧订单 W5 不能当成新槽位再加 6 餐。

    第 1 次只有 W5=6（云端 6、账本 [6]）；第 2 次补 W11=1 只应再加 1 餐
    （合计 7，不是 12）；第 3 次同输入应当无事可做（noop、一个格子都不写）。
    """
    ledger_path = tmp_path_dir() / "d02-state.json"
    cli = _cli_with_unrelated()
    ledger = SyncLedger(ledger_path)

    first = _plan(cli, [_w_order(6, order_no="W5", row=3)],
                  ledger=ledger, marker=False, sort=False)
    apply_plan(cli, first, ledger=ledger, marker_enabled=False)
    assert _cloud_total(cli) == 6, _cloud_rows(cli)
    assert _disk_slots(ledger_path) == [6]
    assert _disk_orders(ledger_path) == ["W5"], "账本要记下槽位的订单身份"

    ledger = SyncLedger(ledger_path)
    second = _plan(cli, [_w_order(1, order_no="W11", row=3),
                         _w_order(6, order_no="W5", row=4)],
                   ledger=ledger, marker=False, sort=False)
    apply_plan(cli, second, ledger=ledger, marker_enabled=False)
    assert _cloud_total(cli) == 7, _cloud_rows(cli)
    assert _disk_slots(ledger_path) == [6, 1], _disk_slots(ledger_path)
    assert _disk_orders(ledger_path) == ["W5", "W11"]

    ledger = SyncLedger(ledger_path)
    third = _plan(cli, [_w_order(1, order_no="W11", row=3),
                        _w_order(6, order_no="W5", row=4)],
                  ledger=ledger, marker=False, sort=False)
    writes_before = len(cli.writes)
    result = apply_plan(cli, third, ledger=ledger, marker_enabled=False)
    assert result["sheets"][0]["status"] == "noop", result["sheets"]
    assert len(cli.writes) == writes_before, "重跑不应再写任何格子"
    assert _cloud_total(cli) == 7, _cloud_rows(cli)


def test_补单后槽位身份对齐():
    """D02 计划层：账本 orders=[W5] + 本地 [W11, W5] → 第 1 槽还是 W5。"""
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张", "111",
                          [6], orders=["W5"])
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "6",
         8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    plan = _plan(cli, [_w_order(1, order_no="W11", row=3),
                       _w_order(6, order_no="W5", row=4)],
                 ledger=ledger, marker=False, sort=False)[0]
    by_slot = {change.slot: change for change in plan.changes}
    assert sorted(by_slot) == [1, 2], plan.changes
    first, second = by_slot[1], by_slot[2]
    assert (first.order_no, first.ledger_prev, first.total_after,
            first.delta) == ("W5", 6, 6, 0)
    assert (second.order_no, second.ledger_prev,
            second.delta) == ("W11", None, 1)


def test_旧账本无orders时退回位置配对():
    """D02 兼容：账本没有订单号（升级后第一批）→ 行数对得上时按位置配对并补记 orders。

    行数与旧槽位数一致时位置配对是**唯一**的配对方式（没有挑行号的余地），
    这时沿用位置配对，但要把身份补进账本供下一批核对。
    """
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张", "111", [6])
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "6",
         8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    plan = _plan(cli, [_w_order(8, order_no="W5", row=3)],
                 ledger=ledger, marker=False, sort=False)[0]
    assert any("无法按订单身份核对" in warn for warn in plan.warnings), plan.warnings

    apply_plan(cli, [plan], ledger=ledger, marker_enabled=False)
    slots = _disk_slots(ledger.path)
    orders = _disk_orders(ledger.path)
    assert orders is not None, "本批必须把订单号记进账本，供下一批核对"
    assert len(orders) == len(slots) == 1, (orders, slots)
    assert orders == ["W5"], orders


def test_旧账本noop批次也补记订单身份():
    """D02 升级窗口：账本没有 orders 时，没有变更的那一批也要补记身份。

    账本没记订单号（旧版本写的）时槽位只能按行号配对；如果只有"真的写了云端"
    的批次才记账本，那些没有变更的批次就永远补不上身份 —— 升级窗口会一直开着：
    升级后先跑一次 noop、再补单重排，旧订单仍会被当成新槽位再加一遍（云端 12 餐）。
    """
    ledger_path = tmp_path_dir() / "upgrade-state.json"
    ledger = SyncLedger(ledger_path)
    ledger.record("2026-09-11", "F1", {"张\u0000111": {"slots": [6], "total": 6}})
    ledger.save()
    assert _disk_orders(ledger_path) is None, "前置：旧账本形态没有订单号"

    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "6",
         8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    # 升级后第一批：本地与云端一致，本来是个 noop
    ledger = SyncLedger(ledger_path)
    first = _plan(cli, [_w_order(6, order_no="W5", row=3)],
                  ledger=ledger, marker=False, sort=False)
    writes = len(cli.writes)
    result = apply_plan(cli, first, ledger=ledger, marker_enabled=False)
    assert result["sheets"][0]["status"] == "noop", result["sheets"]
    assert len(cli.writes) == writes, "没有变更就不该写云端"
    assert _disk_orders(ledger_path) == ["W5"], "noop 批次也要把订单身份补进账本"
    assert _disk_slots(ledger_path) == [6]

    # 补单后本地变成 [W11=1, W5=6]：现在必须能按身份配上，只加新付的 1 餐
    ledger = SyncLedger(ledger_path)
    second = _plan(cli, [_w_order(1, order_no="W11", row=3),
                         _w_order(6, order_no="W5", row=4)],
                   ledger=ledger, marker=False, sort=False)
    apply_plan(cli, second, ledger=ledger, marker_enabled=False)
    assert _cloud_total(cli) == 7, _cloud_rows(cli)
    assert _disk_slots(ledger_path) == [6, 1]
    assert _disk_orders(ledger_path) == ["W5", "W11"]


def test_账本订单号全空时不再静默退回位置配对():
    """账本 orders 全是空串（当时本地「订单」列是空的）＝ 没有身份，必须提示。

    只判断"有没有 orders 这个字段"会把 ``['']`` 当成"有订单号"而一声不吭地
    退回按行号配对（复核反例 2）：行数一致时要提示并补记身份，行数变了要不猜。
    """
    # (a) 行数与旧槽位数一致：位置配对是唯一配对方式 → 提示 + 把身份补进账本
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张", "111", [6],
                          orders=[""])
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "6",
         8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    plan = _plan(cli, [_w_order(8, order_no="W5", row=3)],
                 ledger=ledger, marker=False, sort=False)[0]
    assert any("无法按订单身份核对" in warn for warn in plan.warnings), plan.warnings
    apply_plan(cli, [plan], ledger=ledger, marker_enabled=False)
    assert _cloud_total(cli) == 8, _cloud_rows(cli)
    assert _disk_orders(ledger.path) == ["W5"], "本批要把身份补进账本"

    # (b) 本地行数比旧槽位多：位置配对是在猜 → 失败关闭，云端保持 6 餐
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张", "111", [6],
                          orders=[""])
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "6",
         8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    plan = _plan(cli, [_w_order(1, order_no="W11", row=3),
                       _w_order(6, order_no="W5", row=4)],
                 ledger=ledger, marker=False, sort=False)[0]
    assert any("不写这个人的云端行" in warn for warn in plan.warnings), plan.warnings
    assert plan.changes == [], plan.changes
    apply_plan(cli, [plan], ledger=ledger, marker_enabled=False)
    assert _cloud_total(cli) == 6, _cloud_rows(cli)
    assert _disk_slots(ledger.path) == [6]


def test_旧账本行数变多但旧行对得上时照常写入():
    """失败关闭只针对"旧行被顶掉"：新行排在末尾、旧行逐一对得上时必须照常写入。

    行数变多本身是正常补单（D01 的用例就是这种）—— 一刀切拦掉会让新订单永远
    同步不出去，所以拦的判据是"负增量被丢掉 + 末尾多一整行"这个组合。
    """
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张", "111", [6])
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "6",
         8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    plan = _plan(cli, [_w_order(6, order_no="W5", row=3),
                       _w_order(2, order_no="W11", row=4)],
                 ledger=ledger, marker=False, sort=False)[0]
    assert not any("不写这个人的云端行" in warn for warn in plan.warnings), plan.warnings

    apply_plan(cli, [plan], ledger=ledger, marker_enabled=False)
    assert _cloud_total(cli) == 8, _cloud_rows(cli)
    assert _disk_slots(ledger.path) == [6, 2]
    assert _disk_orders(ledger.path) == ["W5", "W11"]


def test_旧账本行数变多又没有订单身份时不写云端():
    """D02 升级窗口 + 直接补单：配对不成立时失败关闭。

    账本只有 1 个已同步槽位、又没有订单号，本地却有 2 行 —— 按行号硬套会让
    新单占住旧槽位、旧订单落到"没有锚点"的位置被全额再加一遍（复核反例 1：
    云端 12 餐、报告成功）。这里宁可一个格子都不写：不猜，也不重复入账。
    """
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张", "111", [6])
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "6",
         8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    plan = _plan(cli, [_w_order(1, order_no="W11", row=3),
                       _w_order(6, order_no="W5", row=4)],
                 ledger=ledger, marker=False, sort=False)[0]
    assert plan.changes == [], plan.changes
    assert any("不写这个人的云端行" in warn for warn in plan.warnings), plan.warnings

    writes = len(cli.writes)
    apply_plan(cli, [plan], ledger=ledger, marker_enabled=False)
    assert _cloud_total(cli) == 6, _cloud_rows(cli)
    assert len(cli.writes) == writes, "失败关闭：一个格子都不写"
    assert _disk_slots(ledger.path) == [6], "账本锚点不动"
    assert _disk_orders(ledger.path) is None


def test_账本有身份但本地订单列为空且行数变多时不写云端():
    """D02（复核反例 H7）：账本记着身份，本地「订单」列却空着 + 行数变多。

    第 1 批 [W5=6] → 云端 6 餐、账本 [6]/['W5']；第 2 批本地 [''=1, ''=6]
    （订单列空、行序变化）。没有订单号的行没有身份可核对，按行号硬套就会把
    旧订单 W5 当成新槽位再全额加 6 餐（实测云端 12 餐、状态 ok）。与旧账本
    情形一样失败关闭：不写云端、不动账本锚点，亮警告请用户补齐订单号。
    """
    ledger_path = tmp_path_dir() / "d02-anon-state.json"
    cli = _cli_with_unrelated()
    ledger = SyncLedger(ledger_path)
    first = _plan(cli, [_w_order(6, order_no="W5", row=3)],
                  ledger=ledger, marker=False, sort=False)
    apply_plan(cli, first, ledger=ledger, marker_enabled=False)
    assert _cloud_total(cli) == 6, _cloud_rows(cli)
    assert _disk_orders(ledger_path) == ["W5"]

    ledger = SyncLedger(ledger_path)
    plan = _plan(cli, [_w_order(1, order_no="", row=3),
                       _w_order(6, order_no="", row=4)],
                 ledger=ledger, marker=False, sort=False)[0]
    assert plan.changes == [], plan.changes
    assert any("不写这个人的云端行" in warn for warn in plan.warnings), plan.warnings

    writes = len(cli.writes)
    apply_plan(cli, [plan], ledger=ledger, marker_enabled=False)
    assert _cloud_total(cli) == 6, "失败关闭：不许把旧订单 W5 再全额加一遍"
    assert len(cli.writes) == writes, "失败关闭：一个格子都不写"
    assert _disk_slots(ledger_path) == [6], "账本锚点不动"
    assert _disk_orders(ledger_path) == ["W5"], "已知身份不许被空串覆写"


def test_账本有身份但匿名行落在有身份槽位加餐时不写云端():
    """D02 同形的另一种排法：没有订单号的行被套进**账本记着身份**的槽位。

    账本 slots=[6, 1]/orders=['W5', 'W11']，本地两行订单列全空、餐次对调
    [''=1, ''=6]：按行号套 → 第 2 槽从锚点 1 加 5 餐（云端 12 餐）。
    这一行完全可能是另一笔订单，不核对就加餐就是重复入账 → 失败关闭。
    """
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张", "111",
                          [6, 1], orders=["W5", "W11"])
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "6",
         8: "=SUM(D3)", 9: "=H3-I3"},
        {0: "张", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "1",
         8: "=SUM(D4)", 9: "=H4-I4"},
    ]))
    plan = _plan(cli, [_w_order(1, order_no="", row=3),
                       _w_order(6, order_no="", row=4)],
                 ledger=ledger, marker=False, sort=False)[0]
    assert plan.changes == [], plan.changes
    assert any("不写这个人的云端行" in warn for warn in plan.warnings), plan.warnings

    writes = len(cli.writes)
    apply_plan(cli, [plan], ledger=ledger, marker_enabled=False)
    assert _cloud_total(cli) == 7, _cloud_rows(cli)
    assert len(cli.writes) == writes, "失败关闭：一个格子都不写"


def test_账本有身份且旧行对得上时末尾匿名新行照常写入():
    """失败关闭只针对"配对不成立"：身份对得上的旧行 + 末尾一行没有订单号的新行。

    账本 slots=[6]/orders=['W5']，本地 [W5=6, ''=2]：W5 按身份对上（锚点 6，
    无增量），多出来的 2 餐行落在账本之外的末尾槽位（没有旧锚点可顶掉）——
    这是正常补单，必须照常写入，不能一刀切拦掉。
    """
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张", "111",
                          [6], orders=["W5"])
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "6",
         8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    plan = _plan(cli, [_w_order(6, order_no="W5", row=3),
                       _w_order(2, order_no="", row=4)],
                 ledger=ledger, marker=False, sort=False)[0]
    assert not any("不写这个人的云端行" in warn for warn in plan.warnings), plan.warnings

    apply_plan(cli, [plan], ledger=ledger, marker_enabled=False)
    assert _cloud_total(cli) == 8, _cloud_rows(cli)


def test_账本orders长度非法时失败关闭():
    """orders 必须是与 slots 等长的字符串列表；不合法就拒绝读取（不能当空账本）。"""
    path = tmp_path_dir() / "broken-orders.json"
    path.write_text(
        '{"version": 1, "batches": {"2026-09-11": {"F1": {"synced_at": "x", '
        '"people": {"张\\u0000111": {"slots": [6, 6], "orders": ["W1"], '
        '"local": 12, "at": "x"}}}}}}', encoding="utf-8")
    with pytest.raises(LedgerCorruptError):
        SyncLedger(path)

    path.write_text(
        '{"version": 1, "batches": {"2026-09-11": {"F1": {"synced_at": "x", '
        '"people": {"张\\u0000111": {"slots": [6], "orders": [1], '
        '"local": 6, "at": "x"}}}}}}', encoding="utf-8")
    with pytest.raises(LedgerCorruptError):
        SyncLedger(path)


def test_账本本地行数减少时保留末尾旧槽位():
    """D03 单测：本次只有第 1 槽有变更时，第 2 槽的旧锚点不能被截掉。"""
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张", "111", [6, 6])
    change = Change("existing", "张", "111", 4, 1, 5, 6, 7, True,
                    local_meals=7, ledger_prev=6, slot=1)
    entries = wc._ledger_entries([change], only_keys={"张\u0000111"},
                                 ledger=ledger, date_key="2026-09-11", file_id="F1")
    assert entries["张\u0000111"]["slots"] == [7, 6], entries


def test_本地行数减少再恢复不重复加餐():
    """D03 端到端：已同步 [6, 6] → 暂时只剩 [W1=7] → 恢复 [W1=7, W2=6]，仍是 13 餐。"""
    ledger_path = tmp_path_dir() / "d03-state.json"
    cli = _cli_with_unrelated()
    ledger = SyncLedger(ledger_path)

    zero = _plan(cli, [_w_order(6, order_no="W1", row=3),
                       _w_order(6, order_no="W2", row=4)],
                 ledger=ledger, marker=False, sort=False)
    apply_plan(cli, zero, ledger=ledger, marker_enabled=False)
    assert _cloud_total(cli) == 12, _cloud_rows(cli)
    assert _disk_slots(ledger_path) == [6, 6]

    ledger = SyncLedger(ledger_path)
    only_first = _plan(cli, [_w_order(7, order_no="W1", row=3)],
                       ledger=ledger, marker=False, sort=False)
    apply_plan(cli, only_first, ledger=ledger, marker_enabled=False)
    assert _cloud_total(cli) == 13, _cloud_rows(cli)
    assert _disk_slots(ledger_path) == [7, 6], "末尾旧槽位不能被截掉"

    ledger = SyncLedger(ledger_path)
    restored = _plan(cli, [_w_order(7, order_no="W1", row=3),
                           _w_order(6, order_no="W2", row=4)],
                     ledger=ledger, marker=False, sort=False)
    writes_before = len(cli.writes)
    result = apply_plan(cli, restored, ledger=ledger, marker_enabled=False)
    assert _cloud_total(cli) == 13, _cloud_rows(cli)
    assert result["sheets"][0]["status"] == "noop", result["sheets"]
    assert len(cli.writes) == writes_before, "恢复已入账的行不应再写云端"
    assert _disk_slots(ledger_path) == [7, 6]


def test_本地行数减少时journal意图与磁盘账本一致():
    """D03 顺带：journal 记的 ledger_entries 与磁盘账本逐槽位相等。"""
    ledger_path = tmp_path_dir() / "d03-journal-state.json"
    cli = _cli_with_unrelated()
    ledger = SyncLedger(ledger_path)
    journal = SyncJournal(journal_path_for(ledger.path))

    zero = _plan(cli, [_w_order(6, order_no="W1", row=3),
                       _w_order(6, order_no="W2", row=4)],
                 ledger=ledger, marker=False, sort=False)
    apply_plan(cli, zero, ledger=ledger, marker_enabled=False, journal=journal)

    ledger = SyncLedger(ledger_path)
    only_first = _plan(cli, [_w_order(7, order_no="W1", row=3)],
                       ledger=ledger, marker=False, sort=False)
    result = apply_plan(cli, only_first, ledger=ledger, marker_enabled=False,
                        journal=journal)

    saved = SyncJournal(journal_path_for(ledger.path))
    record = saved.get_operation(result["operation_id"])["sheets"]["东湖中餐"]
    assert record["ledger_entries"]["张\u0000111"]["slots"] == [7, 6]
    assert record["ledger_entries"]["张\u0000111"]["slots"] == _disk_slots(ledger.path)


def test_按槽位身份对齐缺行的槽位留空不压缩():
    """D03 单测：账本第 2 槽有身份、本地没有这一行 → 返回里第 2 槽是 ``None``。

    旧实现把后面的行整体前移补空位：第 2 槽拿到第 3 槽的订单，却用第 2 槽的
    锚点做减法 —— 云端永久多入账（复核场景 I）。``None`` 是"本地暂时没有这一
    行"，调用方跳过它；不是"这个槽位可以给别人用"。
    """
    group = [_w_order(2, order_no="W1", row=3), _w_order(6, order_no="W3", row=5)]
    slots = wc._slots_by_identity(group, ["W1", "W2", "W3"])
    assert [item.order_no if item else None for item in slots] == ["W1", None, "W3"], slots

    # 账本没有身份（旧账本 / 订单列全空）时原样返回：没有洞，调用方沿用位置配对
    assert wc._slots_by_identity(group, None) == group
    assert wc._slots_by_identity(group, ["", ""]) == group

    # 带身份但账本里没有的 = 本批新单：放到账本之外的末尾槽位，不许占别人的槽
    appended = wc._slots_by_identity(
        [_w_order(2, order_no="W1", row=3), _w_order(1, order_no="W9", row=4)],
        ["W1", "W2"])
    assert [item.order_no if item else None for item in appended] == ["W1", None, "W9"], appended


def test_本地行数减少丢中间行时按槽位下标对齐():
    """D03 端到端（复核场景 I）：[1,1,6] → 本地暂时 [W1=2, W3=6] → 云端只 +1。

    丢的是中间一行时，保留的后一行必须留在自己的槽位上：不能前移去顶空位、
    拿别人的锚点做减法（旧行为：云端 8 → 14 餐，W2 的行被写进 W3 的 6 餐）。
    """
    ledger_path = tmp_path_dir() / "d03-middle-state.json"
    cli = _cli_with_unrelated()
    ledger = SyncLedger(ledger_path)

    zero = _plan(cli, [_w_order(1, order_no="W1", row=3),
                       _w_order(1, order_no="W2", row=4),
                       _w_order(6, order_no="W3", row=5)],
                 ledger=ledger, marker=False, sort=False)
    apply_plan(cli, zero, ledger=ledger, marker_enabled=False)
    assert _cloud_rows(cli) == [(4, 1, "1"), (5, 1, "1"), (6, 6, "1")], _cloud_rows(cli)
    assert _disk_slots(ledger_path) == [1, 1, 6]
    assert _disk_orders(ledger_path) == ["W1", "W2", "W3"]

    ledger = SyncLedger(ledger_path)
    missing_middle = _plan(cli, [_w_order(2, order_no="W1", row=3),
                                 _w_order(6, order_no="W3", row=5)],
                           ledger=ledger, marker=False, sort=False)
    assert any("本地暂时没有订单「W2」" in warn
               for warn in missing_middle[0].warnings), missing_middle[0].warnings
    apply_plan(cli, missing_middle, ledger=ledger, marker_enabled=False)
    assert _cloud_rows(cli) == [(4, 2, "1"), (5, 1, "1"), (6, 6, "1")], _cloud_rows(cli)
    assert _cloud_total(cli) == 9, "只该加第一行真涨的 1 餐"
    assert _disk_slots(ledger_path) == [2, 1, 6], "缺行槽位的锚点原样保留"
    assert _disk_orders(ledger_path) == ["W1", "W2", "W3"], "槽位身份不许被顶掉"

    ledger = SyncLedger(ledger_path)
    restored = _plan(cli, [_w_order(2, order_no="W1", row=3),
                           _w_order(1, order_no="W2", row=4),
                           _w_order(6, order_no="W3", row=5)],
                     ledger=ledger, marker=False, sort=False)
    writes_before = len(cli.writes)
    result = apply_plan(cli, restored, ledger=ledger, marker_enabled=False)
    assert result["sheets"][0]["status"] == "noop", result["sheets"]
    assert len(cli.writes) == writes_before, "恢复已入账的行不应再写云端"
    assert _cloud_total(cli) == 9, _cloud_rows(cli)


def test_本地行数减少丢第一行时不动云端那一行():
    """D03（复核场景 B2）：已同步 [W1=1, W2=6]，本地暂时只剩 [W2=6] → 仍是 7 餐。

    旧行为把 W2=6 套到第 1 槽（锚点 1）上：云端 7 → 12 餐，还写错物理行。
    """
    ledger_path = tmp_path_dir() / "d03-first-state.json"
    cli = _cli_with_unrelated()
    ledger = SyncLedger(ledger_path)
    zero = _plan(cli, [_w_order(1, order_no="W1", row=3),
                       _w_order(6, order_no="W2", row=4)],
                 ledger=ledger, marker=False, sort=False)
    apply_plan(cli, zero, ledger=ledger, marker_enabled=False)
    assert _cloud_rows(cli) == [(4, 1, "1"), (5, 6, "1")], _cloud_rows(cli)

    ledger = SyncLedger(ledger_path)
    only_second = _plan(cli, [_w_order(6, order_no="W2", row=4)],
                        ledger=ledger, marker=False, sort=False)
    writes = len(cli.writes)
    result = apply_plan(cli, only_second, ledger=ledger, marker_enabled=False)
    assert _cloud_rows(cli) == [(4, 1, "1"), (5, 6, "1")], _cloud_rows(cli)
    assert _cloud_total(cli) == 7, "保留的行要留在自己的槽位上（不能降级成第 1 槽）"
    assert _disk_slots(ledger_path) == [1, 6], "缺的第一槽锚点原样保留"
    assert _disk_orders(ledger_path) == ["W1", "W2"]
    assert len(cli.writes) == writes, "没有增量就不该写云端"
    assert result["failed"] == 0


def test_本地行数减少丢中间行时更新写在正确的物理行上():
    """D03（复核场景 C2）：[6,6,6] → 本地只剩中间 [W2=7] → 第 2 物理行 6→7。

    旧行为把 7 写进第 1 行（W1 的行、值 6），W3 的行被前移顶位：云端行上的值
    与订单对不上（复核："写错物理行"）。
    """
    ledger_path = tmp_path_dir() / "d03-row-state.json"
    cli = _cli_with_unrelated()
    ledger = SyncLedger(ledger_path)
    zero = _plan(cli, [_w_order(6, order_no="W1", row=3),
                       _w_order(6, order_no="W2", row=4),
                       _w_order(6, order_no="W3", row=5)],
                 ledger=ledger, marker=False, sort=False)
    apply_plan(cli, zero, ledger=ledger, marker_enabled=False)
    assert _cloud_total(cli) == 18, _cloud_rows(cli)

    ledger = SyncLedger(ledger_path)
    middle = _plan(cli, [_w_order(7, order_no="W2", row=4)],
                   ledger=ledger, marker=False, sort=False)
    apply_plan(cli, middle, ledger=ledger, marker_enabled=False)
    assert _cloud_rows(cli) == [(4, 6, "1"), (5, 7, "1"), (6, 6, "1")], _cloud_rows(cli)
    assert _disk_slots(ledger_path) == [6, 7, 6]
    assert _disk_orders(ledger_path) == ["W1", "W2", "W3"]


def test_本地订单列为空的行仍按剩下的空位配对():
    """本地「订单」列是空的行没有身份可核对：沿用按行号填空位的旧行为，不许丢。

    账本第 2 槽的身份是 W2，本地第 2 行没有订单号：这一行仍排在剩下的第 2 槽
    （锚点 1）上 —— 不因为"身份对不上"就把它当新单再全额加一遍。
    """
    ledger_path = tmp_path_dir() / "d03-anon-state.json"
    cli = _cli_with_unrelated()
    ledger = SyncLedger(ledger_path)
    zero = _plan(cli, [_w_order(6, order_no="W1", row=3),
                       _w_order(1, order_no="W2", row=4)],
                 ledger=ledger, marker=False, sort=False)
    apply_plan(cli, zero, ledger=ledger, marker_enabled=False)
    assert _disk_slots(ledger_path) == [6, 1]

    ledger = SyncLedger(ledger_path)
    anonymous = wc.CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 1,
                              row=4, order_no="", rows=(4,), weekday_marks=("周五",))
    plan = _plan(cli, [_w_order(6, order_no="W1", row=3), anonymous],
                 ledger=ledger, marker=False, sort=False)
    assert any("无法按订单身份核对" in warn for warn in plan[0].warnings), plan[0].warnings
    writes = len(cli.writes)
    result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False)
    assert result["failed"] == 0
    assert _cloud_total(cli) == 7, _cloud_rows(cli)
    assert len(cli.writes) == writes, "第 2 行锚点 1 = 本地 1 餐，不该再有增量"


def test_负增量被截时账本基准不倒退():
    """D04 单测：云端不扣减时，账本锚点 = 已同步量（6），不是本地现值（4）。"""
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张", "111", [6])
    truncated = Change("existing", "张", "111", 4, 0, 5, 6, 6, True,
                       local_meals=4, ledger_prev=6, slot=1)
    entries = wc._ledger_entries([truncated], only_keys={"张\u0000111"},
                                 ledger=ledger, date_key="2026-09-11", file_id="F1")
    assert entries["张\u0000111"]["slots"] == [6], entries
    assert entries["张\u0000111"]["local"] == 6

    # 增量没被截时锚点 = 本地现值（8）：口径仍与「本批已同步量」一致
    grown = Change("existing", "张", "111", 4, 2, 5, 6, 8, True,
                   local_meals=8, ledger_prev=6, slot=1)
    entries = wc._ledger_entries([grown], only_keys={"张\u0000111"},
                                 ledger=ledger, date_key="2026-09-11", file_id="F1")
    assert entries["张\u0000111"]["slots"] == [8], entries


def test_本地先减后恢复不重复加餐():
    """D04 端到端：[6, 1] → [4, 2] → [6, 2]，合计仍是 8 餐（不是 10 餐）。"""
    ledger_path = tmp_path_dir() / "d04-state.json"
    cli = _cli_with_unrelated()
    ledger = SyncLedger(ledger_path)

    def run(meals_first: int, meals_second: int):
        nonlocal ledger
        plan = _plan(cli, [_w_order(meals_first, order_no="W1", row=3),
                           _w_order(meals_second, order_no="W2", row=4)],
                     ledger=ledger, marker=False, sort=False)
        result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False)
        ledger = SyncLedger(ledger_path)
        return result

    run(6, 1)
    assert _cloud_rows(cli) == [(4, 6, "1"), (5, 1, "1")], _cloud_rows(cli)
    assert _disk_slots(ledger_path) == [6, 1]

    first = run(4, 2)
    assert _cloud_rows(cli) == [(4, 6, "1"), (5, 2, "1")], _cloud_rows(cli)
    assert _cloud_total(cli) == 8, "云端不扣减：第一槽位仍是 6 餐"
    assert _disk_slots(ledger_path) == [6, 2], "账本锚点不能随本地减少而倒退"

    writes_before = len(cli.writes)
    second = run(6, 2)
    assert _cloud_total(cli) == 8, _cloud_rows(cli)
    assert second["sheets"][0]["status"] == "noop", second["sheets"]
    assert len(cli.writes) == writes_before, "恢复本地原值不应再写云端"

    third = run(6, 2)
    assert _cloud_total(cli) == 8, _cloud_rows(cli)
    assert third["failed"] == 0 and first["failed"] == 0


# ----------------------------------------------------------------------
# 整体评审：槽位→云端行不能按位置配对（账本槽位序 ≠ 物理行序）
# ----------------------------------------------------------------------

def test_补单新行插到既有行上方后增量写回自己的行():
    """整体评审：补单新行插到既有行上方后，槽位序与物理行序脱钩。

    第 1 批只有 W5=6（云端 1 行）；第 2 批补 W11=1 —— 新行插到既有行上方，
    物理行序变成 [W11, W5]，而账本槽位序仍是 [W5, W11]（身份序）；第 3 批
    本地把 W11 由 1 加到 3。

    只按位置 ``cloud_rows[slot-1]`` 配对时，第 2 槽会套到 W5 的物理行上：
    W5 的行被写成 8、W11 的行仍是 1 —— 人级合计还是 9、执行照样报 ok，
    但两个物理行上的值与订单对不上（账本 orders 与行内容不符）。
    期望：+2 落在 W11 自己的那一行上（1→3），W5 的行保持 6。
    """
    ledger_path = tmp_path_dir() / "review-slot-order-state.json"
    cli = _cli_with_unrelated()
    ledger = SyncLedger(ledger_path)

    first = _plan(cli, [_w_order(6, order_no="W5", row=3)],
                  ledger=ledger, marker=False, sort=False)
    apply_plan(cli, first, ledger=ledger, marker_enabled=False)
    assert _cloud_rows(cli) == [(4, 6, "1")], _cloud_rows(cli)

    ledger = SyncLedger(ledger_path)
    second = _plan(cli, [_w_order(1, order_no="W11", row=3),
                         _w_order(6, order_no="W5", row=4)],
                   ledger=ledger, marker=False, sort=False)
    apply_plan(cli, second, ledger=ledger, marker_enabled=False)
    assert _cloud_rows(cli) == [(4, 1, "1"), (5, 6, "1")], _cloud_rows(cli)
    assert _disk_slots(ledger_path) == [6, 1]
    assert _disk_orders(ledger_path) == ["W5", "W11"], "账本槽位身份顺序不变"

    ledger = SyncLedger(ledger_path)
    third = _plan(cli, [_w_order(3, order_no="W11", row=3),
                        _w_order(6, order_no="W5", row=4)],
                  ledger=ledger, marker=False, sort=False)
    result = apply_plan(cli, third, ledger=ledger, marker_enabled=False)

    assert result["sheets"][0]["status"] == "ok", result["sheets"]
    assert _cloud_rows(cli) == [(4, 3, "1"), (5, 6, "1")], (
        "第 2 槽的 +2 必须落在 W11 自己的那一行（第 4 行）上，"
        f"不能写进 W5 的行：{_cloud_rows(cli)}")
    assert _cloud_total(cli) == 9, _cloud_rows(cli)
    assert _disk_slots(ledger_path) == [6, 3]
    assert _disk_orders(ledger_path) == ["W5", "W11"]


def test_跨地址组重排后增量写回自己的行():
    """整体评审：D01 地址排序把同一人跨地址组重排后，槽位序也会与物理行序脱钩。

    第 1 批在 B1 建了 W2=6；第 2 批补 A1 的 W7=2 —— 排序后 A1 排到 B1 之前
    （物理行序 [A1=W7, B1=W2]，账本槽位序仍是历史序 [W2, W7]）；第 3 批本地
    把 B1 那一单由 6 加到 8。

    只按位置配对时 +2 会加到 A1 的 2 餐行上（变成 4），B1 的行仍是 6。
    期望：+2 落在 B1 自己的行上，A1 的 2 餐不动。
    """
    ledger_path = tmp_path_dir() / "review-cross-address-state.json"
    cli = _cli_with_unrelated()
    ledger = SyncLedger(ledger_path)

    def order(meals: int, order_no: str, address: str, row: int) -> CloudOrder:
        return CloudOrder("东湖中餐", "张", address, "111", "中餐", "经济", meals,
                          row=row, order_no=order_no, rows=(row,),
                          weekday_marks=("周五",))

    first = _plan(cli, [order(6, "W2", "B1", 3)], ledger=ledger, marker=False)
    apply_plan(cli, first, ledger=ledger, marker_enabled=False)

    ledger = SyncLedger(ledger_path)
    second = _plan(cli, [order(2, "W7", "A1", 3), order(6, "W2", "B1", 4)],
                   ledger=ledger, marker=False)
    apply_plan(cli, second, ledger=ledger, marker_enabled=False)
    # 排序后 A1 排在 B1 之前：物理行序与账本槽位序（历史序）相反
    placed = {item["address"]: item["total"] for item in _person_rows(cli, "张", "111")}
    assert placed == {"A1": "2", "B1": "6"}, placed
    assert _disk_slots(ledger_path) == [6, 2]
    assert _disk_orders(ledger_path) == ["W2", "W7"]

    ledger = SyncLedger(ledger_path)
    third = _plan(cli, [order(2, "W7", "A1", 3), order(8, "W2", "B1", 4)],
                  ledger=ledger, marker=False)
    result = apply_plan(cli, third, ledger=ledger, marker_enabled=False)

    assert result["sheets"][0]["status"] == "ok", result["sheets"]
    rows = {item["address"]: item for item in _person_rows(cli, "张", "111")}
    assert rows["B1"]["total"] == "8", \
        f"B1 那一单的 +2 必须落在 B1 自己的行上：{rows}"
    assert rows["A1"]["total"] == "2", f"A1 那一单不许被加餐：{rows}"
    assert _cloud_total(cli) == 10, _cloud_rows(cli)


def test_槽位物理位置按行号记账本():
    """整体评审单测：账本 positions = 本批结束后每个槽位在云端排第几行。

    槽位序是身份序（历史写入序），物理行序会被插入与排序改掉；账本按物理位置
    记，下一批才能把槽位配到**自己那一行**上（不是数组位置）。
    """
    ledger = _ledger_with(tmp_path_dir(), "2026-09-11", "F1", "张", "111", [6, 1],
                          orders=["W5", "W11"])
    # 第 1 槽（W5）本批落在第 5 行、第 2 槽（W11）落在第 4 行（补单新行在上）
    first = Change("existing", "张", "111", 5, 0, 5, 6, 6, True,
                   local_meals=6, ledger_prev=6, slot=1, order_no="W5")
    second = Change("existing", "张", "111", 4, 2, 5, 1, 3, True,
                    local_meals=3, ledger_prev=1, slot=2, order_no="W11")
    entries = wc._ledger_entries([first, second], only_keys={"张\u0000111"},
                                 ledger=ledger, date_key="2026-09-11", file_id="F1")
    assert entries["张\u0000111"]["positions"] == [2, 1], entries
    assert entries["张\u0000111"]["slots"] == [6, 3], entries

    # 没有变更的槽位（本地暂时缺行）用计划预测的行号：位置照样记全
    entries = wc._ledger_entries([first], only_keys={"张\u0000111"}, ledger=ledger,
                                 date_key="2026-09-11", file_id="F1",
                                 slot_rows={("张", "111"): {1: 5, 2: 4}})
    assert entries["张\u0000111"]["positions"] == [2, 1], entries


def test_账本positions长度非法时失败关闭():
    """positions 必须与 slots 等长；不合法就拒绝读取（不能当没有位置的账本用）。"""
    path = tmp_path_dir() / "broken-positions.json"
    path.write_text(
        '{"version": 1, "batches": {"2026-09-11": {"F1": {"synced_at": "x", '
        '"people": {"张\\u0000111": {"slots": [6, 6], "positions": [1], '
        '"local": 12, "at": "x"}}}}}}', encoding="utf-8")
    with pytest.raises(LedgerCorruptError):
        SyncLedger(path)

    path.write_text(
        '{"version": 1, "batches": {"2026-09-11": {"F1": {"synced_at": "x", '
        '"people": {"张\\u0000111": {"slots": [6], "positions": ["x"], '
        '"local": 6, "at": "x"}}}}}}', encoding="utf-8")
    with pytest.raises(LedgerCorruptError):
        SyncLedger(path)

    # 旧账本没有 positions：正常读，synced_positions 返回 None（回退按位置配对）
    path.write_text(
        '{"version": 1, "batches": {"2026-09-11": {"F1": {"synced_at": "x", '
        '"people": {"张\\u0000111": {"slots": [6], "orders": ["W5"], '
        '"local": 6, "at": "x"}}}}}}', encoding="utf-8")
    ledger = SyncLedger(path)
    assert ledger.synced_positions("2026-09-11", "F1", "张", "111") is None


def test_旧账本noop批次补记槽位物理位置():
    """升级窗口：账本没有 positions 时，没有变更的那一批也要把位置补进账本。

    否则升级后先跑一次 noop、再补单，补单那一批仍要按数组位置配对 —— 本批
    配对结果虽然写对了，但"下一批按位置配对"的窗口一直开在错误状态里。
    """
    ledger_path = tmp_path_dir() / "positions-upgrade-state.json"
    ledger = SyncLedger(ledger_path)
    ledger.record("2026-09-11", "F1",
                  {"张\u0000111": {"slots": [6], "orders": ["W5"], "total": 6}})
    ledger.save()
    assert _disk_positions(ledger_path) is None, "前置：旧账本形态没有 positions"

    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "6",
         8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    ledger = SyncLedger(ledger_path)
    plan = _plan(cli, [_w_order(6, order_no="W5", row=3)],
                 ledger=ledger, marker=False, sort=False)
    writes = len(cli.writes)
    result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False)
    assert result["sheets"][0]["status"] == "noop", result["sheets"]
    assert len(cli.writes) == writes, "没有变更就不该写云端"
    assert _disk_positions(ledger_path) == [1], "noop 批次也要把物理位置补进账本"
    assert _disk_slots(ledger_path) == [6]
    assert _disk_orders(ledger_path) == ["W5"]


def test_位置指向的行被删后按本地餐次重建而不是改写别人的行():
    """槽位位置指向的云端行不存在了（被协作者删了）：重建那一行，不改别的行。

    位置记的是"这个槽位在云端这个人的第几行"，行数变少时第 2 槽的位置就取不到。
    这时宁可重建一行（并按本地餐次补账），也不能拿第 1 槽的行顶上 —— 那会把
    W5 的那一行写成别人的餐次。
    """
    ledger_path = tmp_path_dir() / "positions-missing-row-state.json"
    ledger = SyncLedger(ledger_path)
    ledger.record("2026-09-11", "F1", {"张\u0000111": {
        "slots": [6, 1], "orders": ["W5", "W11"], "positions": [1, 2], "total": 7}})
    ledger.save()
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "6",
         8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    plan = _plan(cli, [_w_order(6, order_no="W5", row=3),
                       _w_order(1, order_no="W11", row=4)],
                 ledger=ledger, marker=False, sort=False)[0]
    assert any("云端没有这一行" in warn for warn in plan.warnings), plan.warnings

    result = apply_plan(cli, [plan], ledger=ledger, marker_enabled=False)
    assert result["failed"] == 0, result["sheets"]
    assert _cloud_rows(cli) == [(3, 6, "1"), (4, 1, "1")], _cloud_rows(cli)
    assert _disk_slots(ledger_path) == [6, 1]
    assert _disk_orders(ledger_path) == ["W5", "W11"]
    # 新行插在第 4 行、张自己的旧行仍在第 3 行：W5 仍是他的第 1 行
    assert _disk_positions(ledger_path) == [1, 2]


def test_排序批次里新增行与错位旧槽位同时写入():
    """整体评审：一批里既有新行、又要在"物理行序≠槽位序"的旧槽位上写增量。

    第 1 批在 B1 建 W2=6；第 2 批补 A1 的 W7=2（排序后 A1 在前：物理行序
    [W7, W2]，槽位序仍是 [W2, W7]）；第 3 批同时做两件事：A1 那单 2→4 餐、
    再补一张 A0 的 W9=2 餐（排序后又插到最前）。

    只按位置配对 + 排序后按地址组定位时，+2 会落到 B1 那一行（6→8），
    A1 那一行仍是 2。期望：A1 自己 2→4、B1 仍是 6、A0 新行 2 餐。
    """
    ledger_path = tmp_path_dir() / "review-sorted-combo-state.json"
    cli = _cli_with_unrelated()
    ledger = SyncLedger(ledger_path)

    def order(meals: int, order_no: str, address: str, row: int) -> CloudOrder:
        return CloudOrder("东湖中餐", "张", address, "111", "中餐", "经济", meals,
                          row=row, order_no=order_no, rows=(row,),
                          weekday_marks=("周五",))

    apply_plan(cli, _plan(cli, [order(6, "W2", "B1", 3)], ledger=ledger,
                          marker=False), ledger=ledger, marker_enabled=False)
    ledger = SyncLedger(ledger_path)
    apply_plan(cli, _plan(cli, [order(2, "W7", "A1", 3), order(6, "W2", "B1", 4)],
                          ledger=ledger, marker=False),
               ledger=ledger, marker_enabled=False)
    assert _disk_positions(ledger_path) == [2, 1], "槽位序 [W2, W7] vs 物理行序 [W7, W2]"

    ledger = SyncLedger(ledger_path)
    third = _plan(cli, [order(2, "W9", "A0", 3), order(4, "W7", "A1", 4),
                        order(6, "W2", "B1", 5)],
                  ledger=ledger, marker=False)
    result = apply_plan(cli, third, ledger=ledger, marker_enabled=False)

    assert result["sheets"][0]["status"] == "ok", result["sheets"]
    rows = {item["address"]: item["total"] for item in _person_rows(cli, "张", "111")}
    assert rows == {"A0": "2", "A1": "4", "B1": "6"}, rows
    assert _cloud_total(cli) == 12, _cloud_rows(cli)
    assert _disk_slots(ledger_path) == [6, 4, 2]
    assert _disk_orders(ledger_path) == ["W2", "W7", "W9"]
