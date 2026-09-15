"""``wps_cloud`` 的安全承诺回归锁（对应 README「安全边界」一节）。

这些承诺是**最高风险**的一类行为：写的是协作者维护的**正式云端排单表**，
一旦回滚/校验失灵，云端就会留下烂尾空行或半成品。但改动前：

* ``_rollback_inserts`` 在 ``apply_plan`` 里被调用两次，**却一个测试都没有**；
* README 的「排序失败 → 把插进去的新行整块删掉，**云端恢复原状**」**无人验证**；
* 「**排序成功之后**的一步失败则不再删行（新行已散落到各地址组，删行会删错人）」
  这条**刻意的不回滚**约定同样没有测试——它和上一条正好相反，最容易写反。

本文件把这三条钉死。
"""
from __future__ import annotations

import datetime as dt


from app.wps_cloud import (CloudOrder, SheetPlan, WpsCloudError, _rollback_inserts,
                           apply_plan, build_plan)

BASE_HEADER = {0: "名字", 1: "地址", 2: "电话", 3: "9.10 周四",
               4: "9.11 周五", 5: "类型", 6: "餐种", 7: "总餐次",
               8: "已出餐", 9: "剩余餐", 10: "备注"}


def make_grid(header, rows):
    grid = {}
    for col, text in header.items():
        grid[(1, col)] = text
    for idx, row in enumerate(rows, start=2):
        for col, text in row.items():
            if text not in (None, ""):
                grid[(idx, col)] = str(text)
    return grid


class FakeCli:
    """记录 delete_rows / sort_range 调用，并可注入失败。"""

    def __init__(self, grid, *, fail_write=False, fail_sort=False,
                 fail_read_after_sort=False):
        self.grid = dict(grid)
        self.fail_write = fail_write
        self.fail_sort = fail_sort
        self.fail_read_after_sort = fail_read_after_sort
        self.sorted = False
        self.sorted_and_cleaned = False
        self.deleted_rows: list[tuple[int, int]] = []
        self.inserts: list[tuple[int, int]] = []
        self.sorts: list[dict] = []
        self.path = "/fake/kdocs-cli"

    def sheets_info(self, file_id):
        return [{"sheetId": 1, "sheetName": "Sheet1", "rowTo": 200, "colTo": 50}]

    def read_grid(self, file_id, ws, row_from, row_to, col_from, col_to, *,
                  with_format=False):
        if self.sorted_and_cleaned and self.fail_read_after_sort and not with_format:
            raise WpsCloudError("模拟排序后回读失败")
        hits = {k: v for k, v in self.grid.items()
                if row_from <= k[0] <= row_to and col_from <= k[1] <= col_to}
        if with_format:
            return {k: {"text": str(v), "fill": ""} for k, v in hits.items()}
        return hits

    def write_cells(self, file_id, ws, cells):
        if self.fail_write:
            raise WpsCloudError("模拟写入失败")
        for cell in cells:
            self.grid[(int(cell["row"]) - 1, int(cell["col"]) - 1)] = str(cell["value"])

    def read_formulas(self, file_id, ws, row_from, row_to, col_from, col_to):
        return {}

    def insert_rows(self, file_id, ws, *, row, count):
        self.inserts.append((row, count))
        lo = row - 1
        shifted = {}
        for (r, c), v in self.grid.items():
            shifted[(r if r < lo else r + count, c)] = v
        self.grid = shifted

    def delete_rows(self, file_id, ws, *, row, count):
        self.deleted_rows.append((row, count))
        lo, hi = row - 1, row - 1 + count - 1
        shifted = {}
        for (r, c), v in self.grid.items():
            if r < lo:
                shifted[(r, c)] = v
            elif r > hi:
                shifted[(r - count, c)] = v
        self.grid = shifted

    def sort_range(self, file_id, ws, *, range_ref, key, order, header):
        if self.fail_sort:
            raise WpsCloudError("模拟排序失败")
        self.sorts.append({"range": range_ref, "key": key})
        self.sorted = True

    def delete_columns(self, file_id, ws, *, column, rows):
        # 排序辅助列删完之后才轮到 read_person_rows 回读；失败注入点放在这里
        # 才是真实的时序（放在 sort_range 里会被提前复位）。
        self.sorted_and_cleaned = True

    def write_format_ops(self, file_id, ws, ops):
        pass

    def read_cell_format(self, file_id, ws, row, col):
        return {}


def _plans(cli, *, name="新人", sort=True, address_order=None):
    orders = [CloudOrder("东湖中餐", name, "小", "999", "中餐", "经济", 6)]
    return build_plan(cli, local_orders={"东湖中餐": orders},
                      tables={"东湖中餐": {"file_id": "F1"}},
                      target=dt.date(2026, 9, 11), ledger=None, address_order={},
                      sort_enabled=sort)


def _existing_rows():
    return [{0: "老客户", 1: "小", 2: "111", 7: "3"},
            {0: "老客户二", 1: "b2", 2: "222", 7: "3"}]


# ----------------------------------------------------------------------
# _rollback_inserts 本身
# ----------------------------------------------------------------------
def _plan_obj() -> SheetPlan:
    return SheetPlan(sheet="东湖中餐", file_id="F1")


def test_rollback_does_nothing_when_no_insert_happened():
    cli = FakeCli(make_grid(BASE_HEADER, _existing_rows()))
    messages: list[str] = []

    _rollback_inserts(cli, _plan_obj(), 1, [], messages.append)

    assert cli.deleted_rows == []
    assert messages == []


def test_rollback_deletes_from_the_last_block_backwards():
    """必须**从后往前**删：先删靠前的块会把后面块的行号整体上移，删错行。"""
    cli = FakeCli(make_grid(BASE_HEADER, _existing_rows()))
    messages: list[str] = []

    _rollback_inserts(cli, _plan_obj(), 1, [(4, 2), (10, 3)], messages.append)

    assert cli.deleted_rows == [(10, 3), (4, 2)], "应从最靠后的插入块开始删"
    assert any("已回滚 2 处插入" in m and "云端恢复原状" in m for m in messages)


def test_rollback_warns_and_stops_when_a_delete_fails():
    """删除失败时**不能抛异常**（那会盖掉原始错误），要告警并停手。"""
    cli = FakeCli(make_grid(BASE_HEADER, _existing_rows()))

    def boom(file_id, ws, *, row, count):
        raise WpsCloudError("模拟删除失败")

    cli.delete_rows = boom  # type: ignore[method-assign]
    messages: list[str] = []

    _rollback_inserts(cli, _plan_obj(), 1, [(4, 2), (10, 3)], messages.append)

    assert any("回滚插入失败" in m and "请人工检查" in m for m in messages)
    assert not any("云端恢复原状" in m for m in messages)


# ----------------------------------------------------------------------
# README：新客户行写入失败 → 回滚，云端恢复原状
# ----------------------------------------------------------------------
def test_write_failure_rolls_back_inserted_rows():
    cli = FakeCli(make_grid(BASE_HEADER, _existing_rows()), fail_write=True)
    plans = _plans(cli)

    result = apply_plan(cli, plans, ledger=None, marker_enabled=False)

    assert cli.inserts, "应先插入过新行，否则这条测试没意义"
    assert cli.deleted_rows == cli.inserts, "插入的块必须被原样删掉"
    assert result["failed"] == 1
    assert result["sheets"][0]["status"] == "failed"
    # 回滚后云端回到原状：总行数与原来一致
    names = [v for (r, c), v in cli.grid.items() if c == 0 and r >= 2]
    assert names == ["老客户", "老客户二"]


# ----------------------------------------------------------------------
# README：排序失败 → 整块删掉，云端恢复原状
# ----------------------------------------------------------------------
def test_sort_failure_rolls_back_inserted_rows():
    cli = FakeCli(make_grid(BASE_HEADER, _existing_rows()), fail_sort=True)
    plans = _plans(cli)

    result = apply_plan(cli, plans, ledger=None, marker_enabled=False)

    assert cli.sorts == [], "排序确实失败了"
    assert cli.deleted_rows == cli.inserts, "排序失败必须回滚插入"
    assert result["failed"] == 1
    assert "排序失败" in result["sheets"][0]["reason"]
    names = [v for (r, c), v in cli.grid.items() if c == 0 and r >= 2]
    assert names == ["老客户", "老客户二"], "云端应恢复原状"


# ----------------------------------------------------------------------
# README：排序**成功之后**的一步失败则不再删行（否则会删错人）
# ----------------------------------------------------------------------
def test_failure_after_successful_sort_does_not_roll_back():
    """这条与上一条**相反**，最容易被写反。

    排序一旦生效，新行已经散落到各地址组里，此时「整块删掉插入位置」删的是
    别人；因此只报告失败、提示重新上传（重复执行是安全的），**绝不回滚**。
    """
    cli = FakeCli(make_grid(BASE_HEADER, _existing_rows()),
                  fail_read_after_sort=True)
    plans = _plans(cli)
    messages: list[str] = []

    result = apply_plan(cli, plans, ledger=None, marker_enabled=False,
                        log=lambda *a: messages.append(str(a[0])))

    assert cli.sorts, "排序本身应当成功"
    assert cli.deleted_rows == [], "排序成功之后绝不能再删行"
    assert result["failed"] == 1
    assert result["sheets"][0]["status"] == "failed"
    assert "回读失败" in result["sheets"][0]["reason"]
    # 面向用户的提示必须告诉他「重传一次即可」，而不是让他以为云端坏了
    assert any("重新上传" in m and "重复执行安全" in m for m in messages), messages
    # 新行的信息仍留在云端（等用户重传），没有被抹掉
    names = [v for (r, c), v in cli.grid.items() if c == 0 and r >= 2]
    assert "新人" in names
