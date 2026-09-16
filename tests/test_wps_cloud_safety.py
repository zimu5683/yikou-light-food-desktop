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

import pytest

from app.wps_cloud import (CloudOrder, SheetPlan, SyncLedger, WpsCloudError,
                           _rollback_inserts, apply_plan, build_plan)

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
        """回读公式：与真实接口一致，只返回以 = 开头的格子。

        缺了它，新客户行的「已出餐/剩余餐」公式回读为空 → 连**成功路径**都会被判
        ``verify_failed``（第 11 轮那批测试只走失败路径，所以一直没暴露）。
        """
        return {k: v for k, v in self.grid.items()
                if row_from <= k[0] <= row_to and col_from <= k[1] <= col_to
                and str(v).startswith("=")}

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


# ----------------------------------------------------------------------
# README：校验不通过则报告并**不更新本地账本**
# ----------------------------------------------------------------------
def _ledger(tmp_path):
    from app.wps_cloud import SyncLedger
    return SyncLedger(tmp_path / "wps_sync_state.json")


def _batch_people(ledger, target, file_id="F1"):
    """返回账本里该 (日期, 文件) 批次记下的人数；没有批次时返回 None。"""
    batch = ledger.data.get("batches", {}).get(target.isoformat(), {}).get(file_id)
    return None if not batch else len(batch.get("people", {}))


def test_ledger_is_updated_when_verification_passes(tmp_path):
    cli = FakeCli(make_grid(BASE_HEADER, _existing_rows()))
    plans = _plans(cli, name="老客户")
    ledger = _ledger(tmp_path)

    result = apply_plan(cli, plans, ledger=ledger, marker_enabled=False)

    assert result["failed"] == 0
    assert _batch_people(ledger, dt.date(2026, 9, 11)) == 1
    # 落盘后重新读回来也还在
    assert _batch_people(_ledger(tmp_path), dt.date(2026, 9, 11)) == 1


def _existing_customer_plans(cli):
    """构造一个**老客户**的计划（不插行、不排序），让它直接走到「逐格回读校验」。"""
    orders = [CloudOrder("东湖中餐", "老客户", "小", "111", "中餐", "经济", 6)]
    return build_plan(cli, local_orders={"东湖中餐": orders},
                      tables={"东湖中餐": {"file_id": "F1"}},
                      target=dt.date(2026, 9, 11), ledger=None,
                      address_order={}, sort_enabled=False)


def test_ledger_is_not_updated_when_verification_fails(tmp_path):
    """README 的安全边界：**校验不通过则报告并不更新本地账本**。

    实现上靠 ``if problems: ... continue`` 在校验失败时**跳过 ledger.record**。
    这条一旦写反（把 record 提到 continue 之前），账本就会记下**并没真正写成功**的
    餐次数，下次同步会据此算出错误的差额。

    注意必须用**老客户 + 关排序**：新客户会先经历插行/排序/重定位，写坏时会**更早**
    以 ``failed`` 退出，根本走不到 ``verify_failed`` 这一步（变异测试发现过这个盲点）。
    """
    cli = FakeCli(make_grid(BASE_HEADER, _existing_rows()))
    plans = _existing_customer_plans(cli)
    ledger = _ledger(tmp_path)
    # 「写入表面成功、内容却没变」→ 回读校验必然失败
    cli.write_cells = lambda *a, **k: None  # type: ignore[method-assign]

    result = apply_plan(cli, plans, ledger=ledger, marker_enabled=False)

    assert result["failed"] == 1
    assert result["sheets"][0]["status"] == "verify_failed"
    assert _batch_people(ledger, dt.date(2026, 9, 11)) is None, "校验没过不能记进账本"


def test_ledger_records_nothing_when_the_whole_run_fails(tmp_path):
    cli = FakeCli(make_grid(BASE_HEADER, _existing_rows()), fail_write=True)
    plans = _plans(cli)
    ledger = _ledger(tmp_path)

    result = apply_plan(cli, plans, ledger=ledger, marker_enabled=False)

    assert result["failed"] == 1
    assert ledger.data.get("batches", {}) == {}
    assert _batch_people(ledger, dt.date(2026, 9, 11)) is None


def test_apply_plan_without_a_ledger_still_works(tmp_path):
    """``ledger=None`` 是合法调用（历史行为），不能因此报错。"""
    cli = FakeCli(make_grid(BASE_HEADER, _existing_rows()))
    plans = _plans(cli, name="老客户")
    result = apply_plan(cli, plans, ledger=None, marker_enabled=False)
    assert result["failed"] == 0 and result["written"] == 1


# ----------------------------------------------------------------------
# README：任何失败都只写日志，**不会影响本地排单任务**
# ----------------------------------------------------------------------
@pytest.mark.parametrize("kwargs", [
    {"fail_write": True},
    {"fail_sort": True},
    {"fail_read_after_sort": True},
])
def test_cloud_failures_never_raise_out_of_apply_plan(tmp_path, kwargs):
    """云同步的每一种失败都必须被**消化成返回值**，绝不向上抛。

    这是 README「任何失败都只写日志，不会影响本地排单任务」的落点：
    云同步是本地排单之外的附加动作，它炸了不能把调用方（排单任务）一起带崩。
    """
    cli = FakeCli(make_grid(BASE_HEADER, _existing_rows()), **kwargs)
    plans = _plans(cli)

    result = apply_plan(cli, plans, ledger=None, marker_enabled=False)   # 不应抛

    assert result["failed"] == 1
    assert isinstance(result["sheets"], list) and result["sheets"][0]["status"] != "ok"


def test_unexpected_exception_propagates_out_of_apply_plan(tmp_path):
    """记录实际分层：``apply_plan`` **只吞 WpsCloudError**，其它意外异常照抛。

    这不是缺陷 —— 它是纯云端逻辑层，让编程错误暴露出来更好排查；真正的安全边界在
    上一层 ``Bridge.wps_upload``（见下一条）。写下来免得后人误以为这层也兜底。
    """
    cli = FakeCli(make_grid(BASE_HEADER, _existing_rows()))
    plans = _plans(cli)

    def boom(*_a, **_k):
        raise KeyError("坏数据")

    cli.write_cells = boom  # type: ignore[method-assign]

    with pytest.raises(KeyError):
        apply_plan(cli, plans, ledger=None, marker_enabled=False)


def test_wps_upload_swallows_every_exception(tmp_path, monkeypatch):
    """README「任何失败都只写日志，不会影响本地排单任务」的**落点在这一层**。

    ``wps_upload`` 末尾有 ``except Exception``，把任何异常转成
    ``{"ok": False, "reason": "类型: 消息"}``，界面只会看到一条红色日志，
    排单任务不会被云同步的意外错误带崩。
    """
    from app import bridge as bridge_module
    from app.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._config.excel_path = tmp_path / "排单.xlsx"
    bridge._config.excel_path.write_bytes(b"x")
    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "F1"}})
    monkeypatch.setattr(
        bridge_module, "read_local_orders",
        lambda path, log=None: {"东湖中餐": [
            CloudOrder("东湖中餐", "某人", "小", "13800000000", "中餐", "经济", 6)]})
    monkeypatch.setattr(bridge_module, "SyncLedger",
                        lambda *a, **k: SyncLedger(tmp_path / "l.json"))

    class _Cli:
        path = "/fake/kdocs-cli"

        def authenticated(self) -> bool:
            return True

    monkeypatch.setattr(bridge, "_wps_cli", lambda: _Cli())

    def boom(*_a, **_k):
        raise KeyError("坏数据")

    monkeypatch.setattr(bridge_module, "build_plan", boom)

    got = bridge.wps_upload()          # 不应抛

    assert got["ok"] is False
    assert got["reason"] == "KeyError: '坏数据'"
    logs = [e["payload"] for e in bridge.drain_events(0)["events"] if e["event"] == "log"]
    assert any(line["level"] == "ERROR" and "异常" in line["msg"] for line in logs)
