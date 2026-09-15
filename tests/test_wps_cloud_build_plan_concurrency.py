"""``build_plan`` 多子表并发化的行为回归锁。

``build_plan`` 原本对每个子表**串行**读云端（``sheets_info`` + ``read_grid`` +
``read_grid(with_format=True)``，6 张表共 18 次往返）。第 3 轮把循环体原样抽成
``_build_sheet_plan`` 并**按子表分批并发**。

并发只允许改变往返的重叠方式，绝不允许改变：

* 调用次数与参数（云端每日额度约 150~200 次，预览 + 上传各跑一次）；
* ``plans`` 的顺序（必须等于 ``local_orders`` 的顺序）；
* 每份 ``SheetPlan`` 的逐字段内容；
* 异常类型与传播行为。

注意：改动前 ``tests/test_wps_cloud.py`` 里的 ~50 处 ``build_plan`` 调用**全部是
单表场景**，多子表顺序完全没被覆盖 —— 本文件补上这一块。
"""
from __future__ import annotations

import datetime as dt
import threading
import time

import pytest

from app import wps_cloud
from app.wps_cloud import CloudOrder, WpsCloudError, build_plan

SHEETS = ["东湖中餐", "东湖晚餐", "衣锦中餐", "衣锦晚餐", "医学院中餐", "医学院晚餐"]
TARGET = dt.date(2026, 9, 11)
# 真实布局：姓名|地址|电话|日期列…|类型|餐种|总餐次|已出餐|剩余餐|备注
_HEADER = {0: "姓名", 1: "地址", 2: "电话", 3: "9.9 周二", 4: "9.10 周三",
           5: "9.11 周四", 6: "类型", 7: "餐种", 8: "总餐次", 9: "已出餐",
           10: "剩余餐", 11: "备注"}


def _grid(people=(), *, date_mark="1"):
    """构造一张合法的云端网格（HEADER_ROW=2、FIRST_DATA_ROW=3）。"""
    g = {(1, col): text for col, text in _HEADER.items()}
    for i, (name, addr, phone) in enumerate(people):
        r = 2 + i                                  # 0-based → 1-based 第 3 行
        g[(r, 0)] = name
        g[(r, 1)] = addr
        g[(r, 2)] = phone
        g[(r, 3)] = date_mark
        g[(r, 6)] = "经济餐"
        g[(r, 7)] = "中餐"
        g[(r, 8)] = "6"
        g[(r, 9)] = "=SUM(D3:F3)"
        g[(r, 10)] = "=I3-J3"
    return g


class FakeCli:
    """多表假 CLI，记录每次调用并统计并发度。"""

    def __init__(self, grids, *, unreadable=(), fail_read=(), delay=0.0):
        self.grids = dict(grids)
        self.unreadable = set(unreadable)
        self.fail_read = set(fail_read)
        self.delay = delay
        self.calls: list[tuple] = []
        self._lock = threading.Lock()
        self._inflight = 0
        self.max_inflight = 0

    def _enter(self, *call):
        with self._lock:
            self.calls.append(call)
            self._inflight += 1
            self.max_inflight = max(self.max_inflight, self._inflight)

    def _leave(self):
        with self._lock:
            self._inflight -= 1

    def sheets_info(self, file_id):
        self._enter("sheets_info", file_id)
        try:
            if self.delay:
                time.sleep(self.delay)
            if file_id in self.unreadable:
                return []
            return [{"sheetId": 1, "sheetName": "S", "rowTo": 200, "colTo": 60}]
        finally:
            self._leave()

    def read_grid(self, file_id, ws, rf, rt, cf, ct, *, with_format=False):
        self._enter("read_fmt" if with_format else "read_grid",
                    file_id, ws, rf, rt, cf, ct)
        try:
            if self.delay:
                time.sleep(self.delay)
            if file_id in self.fail_read:
                raise WpsCloudError(f"模拟读取失败 {file_id}")
            hits = {k: v for k, v in self.grids.get(file_id, {}).items()
                    if rf <= k[0] <= rt and cf <= k[1] <= ct}
            if with_format:
                return {k: {"text": str(v), "fill": "#ffffff"} for k, v in hits.items()}
            return hits
        finally:
            self._leave()


def _scenario(n_sheets=None, *, people_per_sheet=3, unreadable=(), fail_read=()):
    sheets = SHEETS if n_sheets is None else SHEETS[:n_sheets]
    grids, tables, orders = {}, {}, {}
    for idx, sheet in enumerate(sheets):
        fid = f"F{idx}"
        grids[fid] = _grid([(f"客户{idx}{i}", ["小", "大西", "b2"][i % 3],
                             f"1380000{idx}{i:03d}") for i in range(people_per_sheet)])
        tables[sheet] = {"file_id": fid, "drive_id": ""}
        orders[sheet] = [CloudOrder(sheet, f"客户{idx}0", "小", f"1380000{idx}000",
                                    "中餐", "经济", 6)]
    cli = FakeCli(grids, unreadable=unreadable, fail_read=fail_read)
    return sheets, tables, orders, cli


def _run(workers, *, n_sheets=None, delay=0.0, **kw):
    wps_cloud.WPS_PLAN_WORKERS = workers
    sheets, tables, orders, cli = _scenario(n_sheets, **kw)
    cli.delay = delay
    plans = build_plan(cli, local_orders=orders, tables=tables, target=TARGET,
                       ledger=None, address_order={}, sort_enabled=True)
    return plans, cli, sheets


# ----------------------------------------------------------------------
# 顺序（改动前完全没被覆盖）
# ----------------------------------------------------------------------
def test_plans_follow_local_orders_order():
    plans, _, sheets = _run(2)
    assert [p.sheet for p in plans] == sheets


def test_order_is_preserved_even_when_completion_is_reversed():
    """故意让后面的表先读完：结果顺序仍必须是 local_orders 的顺序。"""
    wps_cloud.WPS_PLAN_WORKERS = 2
    sheets = SHEETS[:4]
    grids, tables, orders = {}, {}, {}
    for idx, sheet in enumerate(sheets):
        fid = f"F{idx}"
        grids[fid] = _grid([(f"人{idx}", "小", f"13800000000{idx}")])
        tables[sheet] = {"file_id": fid, "drive_id": ""}
        orders[sheet] = [CloudOrder(sheet, f"人{idx}", "小", f"13800000000{idx}",
                                    "中餐", "经济", 3)]

    class ReversedCli(FakeCli):
        def sheets_info(self, file_id):
            # 越靠后的表越快返回 → 完成顺序与提交顺序相反
            time.sleep(0.05 * (len(sheets) - int(file_id[1:])))
            return super().sheets_info(file_id)

    cli = ReversedCli(grids)
    plans = build_plan(cli, local_orders=orders, tables=tables, target=TARGET,
                       ledger=None, address_order={}, sort_enabled=True)
    assert [p.sheet for p in plans] == sheets


# ----------------------------------------------------------------------
# 等价性：串行 ↔ 并发
# ----------------------------------------------------------------------
def test_parallel_equals_serial_field_by_field():
    serial, _, _ = _run(1)
    parallel, _, _ = _run(2)
    assert [repr(p) for p in parallel] == [repr(p) for p in serial]


def test_parallel_equals_serial_with_unreadable_sheet():
    serial, _, _ = _run(1, unreadable={"F2"})
    parallel, _, _ = _run(2, unreadable={"F2"})
    assert [repr(p) for p in parallel] == [repr(p) for p in serial]
    # 不可读的子表仍要产出一份带警告的计划，位置不变。
    assert "云端文件不可读" in "".join(parallel[2].warnings)


def test_call_count_and_arguments_unchanged():
    serial, scli, sheets = _run(1)
    parallel, pcli, _ = _run(2)
    assert sorted(pcli.calls) == sorted(scli.calls)
    # 每张表 3 次：sheets_info + read_grid + read_fmt。
    assert len(pcli.calls) == len(sheets) * 3

    for kind, fid, *rest in pcli.calls:
        fid = fid  # 仅用于报错信息
        if kind == "sheets_info":
            assert rest == []
        elif kind == "read_grid":
            # (worksheet_id, row_from, row_to, col_from, col_to)
            assert tuple(rest) == (1, 0, 200, 0, 60), (kind, rest)
        else:
            # 学格式那次只读「姓名列 ~ 备注列」：name_col=1, remark_col=12
            assert tuple(rest) == (1, 2, 200, 0, 11), (kind, rest)


def test_sheets_without_file_id_are_skipped_without_any_call():
    wps_cloud.WPS_PLAN_WORKERS = 2
    _, tables, orders, cli = _scenario(3)
    tables["东湖晚餐"] = {"file_id": "", "drive_id": ""}   # 配置缺 file_id
    orders["幽灵表"] = [CloudOrder("幽灵表", "鬼", "小", "13000000000", "中餐", "经济", 1)]

    plans = build_plan(cli, local_orders=orders, tables=tables, target=TARGET,
                       ledger=None, address_order={}, sort_enabled=True)

    assert [p.sheet for p in plans] == ["东湖中餐", "衣锦中餐"]
    assert all(call[1] != "幽灵表" for call in cli.calls)
    assert len(cli.calls) == 2 * 3


def test_empty_local_orders_returns_empty_and_makes_no_call():
    wps_cloud.WPS_PLAN_WORKERS = 2
    cli = FakeCli({})
    assert build_plan(cli, local_orders={}, tables={}, target=TARGET,
                      ledger=None, address_order={}, sort_enabled=True) == []
    assert cli.calls == []


def test_single_sheet_uses_sequential_path():
    plans, cli, _ = _run(2, n_sheets=1)
    assert len(plans) == 1 and len(cli.calls) == 3
    assert cli.max_inflight == 1, "单表不该起线程池"


# ----------------------------------------------------------------------
# 并发确实生效 / 并发度保守
# ----------------------------------------------------------------------
def test_reads_actually_overlap():
    _, cli, _ = _run(2, delay=0.05)
    assert cli.max_inflight >= 2, "并发度没有生效"


def test_concurrency_is_faster_than_serial():
    t0 = time.perf_counter()
    _run(1, delay=0.03)
    serial = time.perf_counter() - t0

    t0 = time.perf_counter()
    _run(2, delay=0.03)
    parallel = time.perf_counter() - t0

    assert parallel < serial * 0.8, f"串行 {serial:.3f}s / 并发 {parallel:.3f}s"


def test_default_workers_stay_conservative_for_rate_limit():
    """429002「短时间频繁触发」不会被重试，并发度必须保守。"""
    assert 1 <= wps_cloud.WPS_PLAN_WORKERS <= 2


def test_workers_never_drops_to_zero():
    plans, _, sheets = _run(0)
    assert [p.sheet for p in plans] == sheets


# ----------------------------------------------------------------------
# 异常路径
# ----------------------------------------------------------------------
def test_cloud_error_propagates_and_extra_calls_are_bounded():
    """某张表读取失败时必须原样抛出，且不能把剩下的表全读一遍。

    串行版本出错即停；并发版本已有任务在途，最多多发 workers-1 张表的调用。
    """
    wps_cloud.WPS_PLAN_WORKERS = 2
    sheets, tables, orders, cli = _scenario(unreadable=())
    cli.fail_read = {"F1"}          # 第 2 张表在 read_grid 时炸
    with pytest.raises(WpsCloudError, match="模拟读取失败 F1"):
        build_plan(cli, local_orders=orders, tables=tables, target=TARGET,
                   ledger=None, address_order={}, sort_enabled=True)

    workers = wps_cloud.WPS_PLAN_WORKERS
    # 串行需要 (1 张完整 + 1 张读到一半) = 4 次；并发最多多发 workers-1 张表的调用。
    assert len(cli.calls) <= 4 + (workers - 1) * 3
    assert len(cli.calls) < len(sheets) * 3, "不应把后续批次也发出去"

    # 对照：串行版本发出的调用更少。
    sheets2, tables2, orders2, cli2 = _scenario(unreadable=())
    cli2.fail_read = {"F1"}
    wps_cloud.WPS_PLAN_WORKERS = 1
    with pytest.raises(WpsCloudError):
        build_plan(cli2, local_orders=orders2, tables=tables2, target=TARGET,
                   ledger=None, address_order={}, sort_enabled=True)
    assert len(cli2.calls) <= len(cli.calls)


def test_unreadable_sheet_does_not_stop_the_others():
    plans, _, sheets = _run(2, unreadable={"F3"})
    assert [p.sheet for p in plans] == sheets
    assert plans[3].warnings
    assert not plans[0].warnings


class _Boom(Exception):
    """模拟 read_grid / sheets_info 里没被包装成 WpsCloudError 的意外异常。"""


class _ExplodingCli(FakeCli):
    """第 ``explode_at`` 次调用时抛非 WpsCloudError 的意外异常。"""

    def __init__(self, grids, *, explode_at: int, **kwargs):
        super().__init__(grids, **kwargs)
        self.explode_at = explode_at

    def _maybe_explode(self) -> None:
        if len(self.calls) >= self.explode_at:
            raise _Boom("接口返回了畸形字段")

    def sheets_info(self, file_id):
        result = super().sheets_info(file_id)
        self._maybe_explode()
        return result

    def read_grid(self, file_id, ws, rf, rt, cf, ct, *, with_format=False):
        result = super().read_grid(file_id, ws, rf, rt, cf, ct, with_format=with_format)
        self._maybe_explode()
        return result


def _explode_run(workers, explode_at):
    wps_cloud.WPS_PLAN_WORKERS = workers
    _, tables, orders, _ = _scenario()
    cli = _ExplodingCli({f"F{i}": _grid([(f"人{i}", "小", f"13800000000{i}")])
                         for i in range(len(SHEETS))}, explode_at=explode_at)
    with pytest.raises(_Boom, match="畸形字段"):
        build_plan(cli, local_orders=orders, tables=tables, target=TARGET,
                   ledger=None, address_order={}, sort_enabled=True)
    return len(cli.calls)


@pytest.mark.parametrize("explode_at", [1, 5, 9, 13, 18])
def test_unexpected_exception_propagates_and_extra_calls_are_bounded(explode_at):
    """非 WpsCloudError 的意外异常也必须原样抛出，且不能把剩下的表全读一遍。

    串行版本出错即停；并发版本已有任务在途，最多多发 workers-1 张表的调用
    （workers=2 → 最多 3 次）。这里锁死这个上界，防止「一次提交全部」导致
    白耗近一整轮云端额度。
    """
    serial = _explode_run(1, explode_at)
    parallel = _explode_run(2, explode_at)
    workers = wps_cloud.WPS_PLAN_WORKERS
    assert parallel <= serial + (workers - 1) * 3, f"多发过多：{serial} → {parallel}"
    assert parallel < len(SHEETS) * 3 or explode_at >= len(SHEETS) * 3


def test_unexpected_exception_is_never_swallowed():
    """worker 线程里的非 WpsCloudError 必须能穿透线程池抛给调用方。"""
    wps_cloud.WPS_PLAN_WORKERS = 2
    _, tables, orders, _ = _scenario()
    cli = _ExplodingCli({f"F{i}": _grid() for i in range(len(SHEETS))}, explode_at=4)
    with pytest.raises(_Boom):
        build_plan(cli, local_orders=orders, tables=tables, target=TARGET,
                   ledger=None, address_order={}, sort_enabled=True)


@pytest.mark.parametrize("delay", [0.0, 0.02])
@pytest.mark.parametrize("fail_id", ["F0", "F2"])
def test_extra_calls_bounded_regardless_of_failure_timing(fail_id, delay):
    """失败「耗时」不影响多发上界。

    曾担心：云端秒失败时线程池会取消同批的兄弟任务，而 200ms 后才失败时
    兄弟任务已经跑完 —— 两种时序下的多发调用量是否都还有界？实测「秒失败」与
    「延迟失败」两种时序，多发一律 ≤ workers-1 张表 × 3 次。
    """
    workers = 2

    def calls_for(worker_count: int) -> int:
        wps_cloud.WPS_PLAN_WORKERS = worker_count
        _, tables, orders, _ = _scenario()
        cli = FakeCli({f"F{i}": _grid([(f"人{i}", "小", f"13800000000{i}")])
                       for i in range(len(SHEETS))},
                      fail_read={fail_id}, delay=delay)
        with pytest.raises(WpsCloudError):
            build_plan(cli, local_orders=orders, tables=tables, target=TARGET,
                       ledger=None, address_order={}, sort_enabled=True)
        return len(cli.calls)

    serial = calls_for(1)
    parallel = calls_for(workers)
    assert parallel <= serial + (workers - 1) * 3, f"{serial} → {parallel}"
    assert parallel < len(SHEETS) * 3, "不应把剩下的子表全部读完"
