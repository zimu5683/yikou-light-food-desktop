"""``Bridge.wps_check_copies`` 的行为回归锁。

这个方法原本串行读 6 张子表（最多 12 次云端只读往返），第 2 轮把它改成
**按表并发**。并发只允许改变往返的重叠方式，绝不允许改变：

* 调用次数（云端有每日额度，约 150~200 次）；
* 每次调用的参数；
* ``tables`` / ``drifted`` 的顺序（前端 ``CloudForm.tsx`` 直接展示）；
* 每种状态分支的判定结果与字段；
* 返回结构与 ``all_aligned``。

因此这里用「workers=1 的串行结果」与「workers=N 的并发结果」逐字段对比，
把等价性钉死在测试里。
"""
from __future__ import annotations

import threading
import time

import pytest

from app import bridge as bridge_module
from app.bridge import Bridge
from app.wps_cloud import WpsCloudError

# 云表读取的真实参数：worksheet_id=1, row 2..300, col 0..2。
_EXPECTED_READ_ARGS = (1, 2, 300, 0, 2)


def _grid(*people: tuple[str, str]) -> dict[tuple[int, int], str]:
    """把 ``(姓名, 电话)`` 列表铺成 read_grid 的返回格式（0-based 行/列）。

    第 1 行是表头，因此第一个人落在 0-based 行 2（即 1-based 第 3 行）。
    """
    grid: dict[tuple[int, int], str] = {}
    for offset, (name, phone) in enumerate(people):
        row = 2 + offset
        grid[(row, 0)] = name
        grid[(row, 2)] = phone
    return grid


class _FakeCli:
    """记录每次读调用的假 CLI；可在读上人为加延迟以观察是否真的重叠。"""

    def __init__(self, grids: dict[str, dict[tuple[int, int], str]],
                 *, fail: set[str] | None = None, delay: float = 0.0) -> None:
        self.grids = grids
        self.fail = fail or set()
        self.delay = delay
        self.calls: list[tuple[str, tuple[int, ...]]] = []
        self._lock = threading.Lock()
        self._inflight = 0
        self.max_inflight = 0

    def read_grid(self, file_id: str, *args: int, **kwargs: object):
        with self._lock:
            self.calls.append((file_id, args))
            self._inflight += 1
            self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            if self.delay:
                time.sleep(self.delay)
            if file_id in self.fail:
                raise WpsCloudError(f"无法读取 {file_id}")
            return dict(self.grids.get(file_id, {}))
        finally:
            with self._lock:
                self._inflight -= 1


def _install(bridge: Bridge, monkeypatch, tables: dict[str, dict[str, str]],
             cli: _FakeCli) -> None:
    """让 ``wps_check_copies`` 使用假表与假 CLI。"""
    monkeypatch.setattr(bridge_module, "effective_tables", lambda _cfg: tables)
    monkeypatch.setattr(bridge, "_wps_cli", lambda: cli)


def _bridge(tmp_path) -> Bridge:
    return Bridge(config_path=str(tmp_path / "config.json"))


# 覆盖全部 5 种 status 分支的场景。语义提醒：``active`` 是**被核对的副本**
# （即当前写入目标），``production`` 是正式表，因此
# ``missing`` = 正式表有、副本没有（副本漏了人），
# ``extra``   = 副本有、正式表没有（副本留了旧人）。
#   东湖中餐  aligned              —— 副本与正式表人员一致（各读 1 次）
#   东湖晚餐  drifted(缺人+多人)    —— 副本漏了钱七、多了赵六
#   衣锦中餐  drifted(电话不同)     —— 姓名集合一致、电话不同
#   衣锦晚餐  same_as_production   —— 未使用副本（只读 1 次）
#   医学院中餐 unreadable           —— 副本读不了
#   医学院晚餐 production_unreadable —— 正式表读不了
_SCENARIO: dict[str, dict[str, str]] = {
    "东湖中餐": {"file_id": "copy-a"},
    "东湖晚餐": {"file_id": "copy-b"},
    "衣锦中餐": {"file_id": "copy-c"},
    "衣锦晚餐": {"file_id": "prod-d"},
    "医学院中餐": {"file_id": "copy-e"},
    "医学院晚餐": {"file_id": "copy-f"},
}
_PRODUCTION: dict[str, dict[str, str]] = {
    "东湖中餐": {"file_id": "prod-a"},
    "东湖晚餐": {"file_id": "prod-b"},
    "衣锦中餐": {"file_id": "prod-c"},
    "衣锦晚餐": {"file_id": "prod-d"},  # 与写入目标相同 → same_as_production
    "医学院中餐": {"file_id": "prod-e"},
    "医学院晚餐": {"file_id": "prod-f"},
}
_GRIDS = {
    "copy-a": _grid(("张三", "13800000000"), ("李四", "13900000000")),
    "prod-a": _grid(("张三", "13800000000"), ("李四", "13900000000")),
    "copy-b": _grid(("王五", "13700000000"), ("赵六", "13600000000")),
    "prod-b": _grid(("王五", "13700000000"), ("钱七", "13200000000")),
    "copy-c": _grid(("孙七", "13500000000")),
    "prod-c": _grid(("孙七", "13511111111")),
    "copy-e": _grid(("周八", "13400000000")),
    "copy-f": _grid(("吴九", "13300000000")),
    "prod-f": _grid(("吴九", "13300000000")),
}
# 这些表读取时抛错：copy-e → unreadable，prod-f → production_unreadable。
_FAIL = {"copy-e", "prod-f"}

# 调用次数：2+2+2（三张要比对）+1（same_as_production）+1（副本读失败）
#           +2（副本成功、正式表读失败）= 10
_EXPECTED_CALLS = 10


def _run(tmp_path, monkeypatch, workers: int, *, delay: float = 0.0,
         fail: set[str] | None = None):
    monkeypatch.setattr(bridge_module, "WPS_COPY_CHECK_WORKERS", workers)
    bridge = _bridge(tmp_path)
    bridge._config.wps_production_tables = _PRODUCTION
    cli = _FakeCli(_GRIDS, fail=_FAIL if fail is None else fail, delay=delay)
    _install(bridge, monkeypatch, _SCENARIO, cli)
    return bridge.wps_check_copies(), cli, bridge


# ----------------------------------------------------------------------
# 核心：串行 ↔ 并发 结果等价
# ----------------------------------------------------------------------
def test_parallel_result_is_identical_to_serial(tmp_path, monkeypatch):
    """workers=1 与 workers=4 的返回结构必须逐字段相同。"""
    serial, _, _ = _run(tmp_path, monkeypatch, workers=1)
    parallel, _, _ = _run(tmp_path, monkeypatch, workers=4)

    assert parallel == serial
    assert parallel["ok"] is True
    # 场景本身要真的覆盖到各个分支，否则这条测试没有意义。
    statuses = {item["status"] for item in parallel["tables"]}
    assert statuses == {"aligned", "drifted", "same_as_production",
                        "unreadable", "production_unreadable"}


def test_tables_keep_submission_order_under_concurrency(tmp_path, monkeypatch):
    result, _, _ = _run(tmp_path, monkeypatch, workers=4)
    assert [item["sheet"] for item in result["tables"]] == list(_SCENARIO)
    assert result["drifted"] == ["东湖晚餐", "衣锦中餐"]
    assert result["all_aligned"] is False


def test_call_count_and_arguments_are_unchanged(tmp_path, monkeypatch):
    """并发不得改变调用次数与每次调用的参数（云端每日额度敏感）。"""
    serial, serial_cli, _ = _run(tmp_path, monkeypatch, workers=1)
    parallel, parallel_cli, _ = _run(tmp_path, monkeypatch, workers=4)

    assert sorted(parallel_cli.calls) == sorted(serial_cli.calls)
    assert len(parallel_cli.calls) == _EXPECTED_CALLS
    assert all(args == _EXPECTED_READ_ARGS for _, args in parallel_cli.calls)
    assert parallel["tables"] == serial["tables"]


# ----------------------------------------------------------------------
# 并发确实生效（否则这次优化等于没做）
# ----------------------------------------------------------------------
class _Boom(Exception):
    """模拟 read_grid 里未被包装成 WpsCloudError 的意外异常。"""


class _ExplodingCli(_FakeCli):
    """在第 ``explode_at`` 次调用时抛出意外异常（默认第 1 次）。"""

    def __init__(self, grids, explode_at: int = 1, **kwargs):
        super().__init__(grids, **kwargs)
        self.explode_at = explode_at

    def read_grid(self, file_id: str, *args: int, **kwargs: object):
        with self._lock:
            self.calls.append((file_id, args))
        if len(self.calls) >= self.explode_at:
            raise _Boom("接口返回了畸形字段")
        return dict(self.grids.get(file_id, {}))


def test_unexpected_exception_propagates_and_stops_early(tmp_path, monkeypatch):
    """非 WpsCloudError 的意外异常必须原样抛出，且**不能**把剩下的表全读一遍。

    串行版本出错即停；并发版本因为已有任务在途，最多多发 workers-1 次调用。
    这里锁死这个上界，防止「一次性提交全部」导致白耗近一整轮额度。
    """
    monkeypatch.setattr(bridge_module, "WPS_COPY_CHECK_WORKERS", 2)
    bridge = _bridge(tmp_path)
    bridge._config.wps_production_tables = _PRODUCTION
    cli = _ExplodingCli(_GRIDS, explode_at=1)
    _install(bridge, monkeypatch, _SCENARIO, cli)

    with pytest.raises(_Boom):
        bridge.wps_check_copies()

    workers = bridge_module.WPS_COPY_CHECK_WORKERS
    # 串行只需 1 次；并发最多多发 workers-1 次（2 路并发即最多 2 次）。
    assert len(cli.calls) <= workers, f"多发调用过多：{len(cli.calls)} 次"
    assert len(cli.calls) < len(_SCENARIO), "不应把后续批次也发出去"


def test_workers_never_drops_to_zero(tmp_path, monkeypatch):
    """并发度取 0/负数时不能变成 ValueError，应退化为 1 路。"""
    monkeypatch.setattr(bridge_module, "WPS_COPY_CHECK_WORKERS", 0)
    bridge = _bridge(tmp_path)
    bridge._config.wps_production_tables = _PRODUCTION
    cli = _FakeCli(_GRIDS, fail=_FAIL)
    _install(bridge, monkeypatch, _SCENARIO, cli)

    result = bridge.wps_check_copies()

    assert result["ok"] is True
    assert [item["sheet"] for item in result["tables"]] == list(_SCENARIO)


def test_default_workers_stay_conservative_for_rate_limit(tmp_path, monkeypatch):
    """默认并发度必须保守：429002（短时间频繁触发）不会被重试。

    并发度调大只会提高瞬时请求速率；一旦突发触发限流，该表就会从「已核对」
    变成「读不了」，这是可观测的行为偏差。因此把上界钉在 2。
    """
    assert 1 <= bridge_module.WPS_COPY_CHECK_WORKERS <= 2


def test_reads_actually_overlap(tmp_path, monkeypatch):
    _, cli, _ = _run(tmp_path, monkeypatch, workers=4, delay=0.05)
    assert cli.max_inflight >= 2, "并发度没有生效，仍在串行读"


def test_concurrency_is_faster_than_serial(tmp_path, monkeypatch):
    start = time.perf_counter()
    _run(tmp_path, monkeypatch, workers=1, delay=0.03)
    serial_s = time.perf_counter() - start

    start = time.perf_counter()
    _run(tmp_path, monkeypatch, workers=4, delay=0.03)
    parallel_s = time.perf_counter() - start

    # 11 次读、每次 30ms：串行 ≈ 0.33s，4 并发 ≈ 0.12s。留足余量避免 CI 抖动。
    assert parallel_s < serial_s * 0.75, f"串行 {serial_s:.3f}s / 并发 {parallel_s:.3f}s"


def test_single_table_skips_thread_pool(tmp_path, monkeypatch):
    """只有一张表时没有可重叠的往返，走顺序分支即可。"""
    monkeypatch.setattr(bridge_module, "WPS_COPY_CHECK_WORKERS", 4)
    bridge = _bridge(tmp_path)
    bridge._config.wps_production_tables = {"东湖中餐": {"file_id": "prod-a"}}
    cli = _FakeCli(_GRIDS)
    _install(bridge, monkeypatch, {"东湖中餐": {"file_id": "copy-a"}}, cli)

    result = bridge.wps_check_copies()

    assert result["ok"] is True
    assert [item["status"] for item in result["tables"]] == ["aligned"]
    assert len(cli.calls) == 2


# ----------------------------------------------------------------------
# 各状态分支的字段契约（前端 bridge.ts 的 WpsCopyCheckItem）
# ----------------------------------------------------------------------
def test_status_field_contract(tmp_path, monkeypatch):
    result, _, _ = _run(tmp_path, monkeypatch, workers=4)
    by_sheet = {item["sheet"]: item for item in result["tables"]}

    assert by_sheet["东湖中餐"]["status"] == "aligned"
    assert by_sheet["东湖中餐"]["rows"] == 2
    assert by_sheet["东湖中餐"]["production_rows"] == 2

    drifted = by_sheet["东湖晚餐"]
    assert drifted["status"] == "drifted"
    assert drifted["missing"] == ["钱七"]   # 正式表有、副本漏了
    assert drifted["extra"] == ["赵六"]     # 副本有、正式表没有
    assert drifted["rows"] == 2
    assert drifted["production_rows"] == 2

    phone = by_sheet["衣锦中餐"]
    assert phone["status"] == "drifted"
    assert phone.get("phone_mismatch") is True
    assert "missing" not in phone and "extra" not in phone

    same = by_sheet["衣锦晚餐"]
    assert same["status"] == "same_as_production"
    assert "rows" in same

    unreadable = by_sheet["医学院中餐"]
    assert unreadable["status"] == "unreadable"
    assert unreadable["reason"]

    prod_bad = by_sheet["医学院晚餐"]
    assert prod_bad["status"] == "production_unreadable"
    assert prod_bad["reason"]
    assert prod_bad["rows"] == 1


def test_unreadable_path_uses_injected_failure(tmp_path, monkeypatch):
    """read_grid 抛错时必须落成 unreadable，而不是让异常冒出去。"""
    result, _, _ = _run(tmp_path, monkeypatch, workers=4, fail={"copy-a"})
    by_sheet = {item["sheet"]: item for item in result["tables"]}
    assert by_sheet["东湖中餐"]["status"] == "unreadable"
    assert "无法读取 copy-a" in by_sheet["东湖中餐"]["reason"]
    # 其余表照常核对，不受影响。
    assert by_sheet["东湖晚餐"]["status"] == "drifted"


def test_cli_unavailable_returns_not_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "effective_tables", lambda _cfg: _SCENARIO)
    bridge = _bridge(tmp_path)

    def boom():
        raise WpsCloudError("kdocs-cli 未找到")

    monkeypatch.setattr(bridge, "_wps_cli", boom)
    result = bridge.wps_check_copies()

    assert result == {"ok": False, "reason": "kdocs-cli 未找到"}


def test_effective_tables_error_propagates_as_before(tmp_path, monkeypatch):
    """``_wps_effective_tables`` 在 try 之外，其异常行为不能被改动。"""
    bridge = _bridge(tmp_path)

    def boom(_cfg):
        raise WpsCloudError("目标表已过期")

    monkeypatch.setattr(bridge_module, "effective_tables", boom)
    with pytest.raises(WpsCloudError):
        bridge.wps_check_copies()


def test_drifted_tables_are_logged(tmp_path, monkeypatch):
    _, _, bridge = _run(tmp_path, monkeypatch, workers=4)
    logs = [event["payload"]["msg"] for event in bridge.drain_events(0)["events"]
            if event["event"] == "log"]
    assert any("副本已过时：东湖晚餐" in line for line in logs)
    assert any("副本已过时：衣锦中餐" in line for line in logs)
    assert any("建议重新同步副本" in line for line in logs)


def test_all_aligned_scenario(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "WPS_COPY_CHECK_WORKERS", 4)
    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "copy-a"},
                                      "东湖晚餐": {"file_id": "copy-b"}})
    bridge = _bridge(tmp_path)
    bridge._config.wps_production_tables = {
        "东湖中餐": {"file_id": "prod-东湖中餐"},
        "东湖晚餐": {"file_id": "prod-东湖晚餐"},
    }
    cli = _FakeCli({
        "copy-a": _grid(("张三", "13800000000")),
        "prod-东湖中餐": _grid(("张三", "13800000000")),
        "copy-b": _grid(("王五", "13700000000")),
        "prod-东湖晚餐": _grid(("王五", "13700000000")),
    })
    _install(bridge, monkeypatch, {"东湖中餐": {"file_id": "copy-a"},
                                   "东湖晚餐": {"file_id": "copy-b"}}, cli)

    result = bridge.wps_check_copies()

    assert result["ok"] is True
    assert result["drifted"] == []
    assert result["all_aligned"] is True
    assert all(item["status"] == "aligned" for item in result["tables"])


def test_empty_targets_returns_aligned(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "effective_tables", lambda _cfg: {})
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_wps_cli", lambda: _FakeCli({}))

    result = bridge.wps_check_copies()

    assert result == {"ok": True, "drifted": [], "tables": [], "all_aligned": True}
