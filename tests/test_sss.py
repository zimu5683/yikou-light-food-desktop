"""Tests for the 闪时送 (sss) order placement module."""
from __future__ import annotations

import datetime as dt
import math
import re
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from app import sss
from app.locators import SSS_LOCATORS, load_sss_locators, sss_user_locators_path


def test_compute_delivery_time_lunch_before_16():
    now = dt.datetime(2026, 8, 14, 9, 30)
    assert sss.compute_delivery_time(False, now) == "2026-08-14 11:00:00"


def test_compute_delivery_time_dinner_before_16():
    now = dt.datetime(2026, 8, 14, 9, 30)
    assert sss.compute_delivery_time(True, now) == "2026-08-14 17:00:00"


def test_compute_delivery_time_rolls_to_next_day_after_16():
    now = dt.datetime(2026, 8, 14, 18, 0)
    assert sss.compute_delivery_time(False, now) == "2026-08-15 11:00:00"
    assert sss.compute_delivery_time(True, now) == "2026-08-15 17:00:00"


def test_compute_delivery_time_rolls_to_next_day_between_16_and_20():
    now = dt.datetime(2026, 8, 14, 17, 0)
    assert sss.compute_delivery_time(False, now) == "2026-08-15 11:00:00"


def test_load_sss_orders_reads_two_sheets_and_stops_on_blank_row(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    lunch = wb.active
    lunch.title = "午餐"
    dinner = wb.create_sheet("晚餐")
    lunch.append(["姓名", "门牌号", "电话", "送达时间"])  # 第 1 行表头
    lunch.append(["表头占位", "", "", ""])                 # 第 2 行占位
    lunch.append(["张三", "A101", "13800000001", "11:00"])  # 第 3 行
    lunch.append(["李四", "B202", "13800000002", "11:00"])  # 第 4 行
    lunch.append([None, None, None, None])                 # 空行×3 才终止
    lunch.append([None, None, None, None])
    lunch.append([None, None, None, None])
    lunch.append(["不应读取", "C303", "13800000003", "11:00"])
    dinner.append(["姓名", "门牌号", "电话", "送达时间"])
    dinner.append(["表头占位", "", "", ""])
    dinner.append(["王五", "D404", "13800000004", "17:00"])

    path = tmp_path / "闪时送.xlsx"
    wb.save(path)
    wb.close()

    result = sss.load_sss_orders(path)
    assert set(result) == {"午餐", "晚餐"}
    assert len(result["午餐"]) == 2
    assert result["午餐"][0]["name"] == "张三"
    assert result["午餐"][0]["door"] == "A101"
    assert result["午餐"][0]["phone"] == "13800000001"
    assert result["午餐"][0]["row"] == 3
    assert result["午餐"][1]["name"] == "李四"
    assert len(result["晚餐"]) == 1
    assert result["晚餐"][0]["name"] == "王五"


def test_load_sss_orders_tolerates_single_blank_row(tmp_path):
    """中间单个空行不再截断：连续 3 个空行才结束。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "午餐"
    ws.append(["姓名", "门牌号", "电话", "送达时间"])
    ws.append(["表头占位", "", "", ""])
    ws.append(["张三", "A101", "13800000001", "11:00"])
    ws.append([None, None, None, None])
    ws.append(["李四", "B202", "13800000002", "11:00"])
    ws.append([None, None, None, None])
    ws.append([None, None, None, None])
    ws.append([None, None, None, None])
    ws.append(["不应读取", "C303", "13800000003", "11:00"])
    path = tmp_path / "闪时送.xlsx"
    wb.save(path)
    wb.close()

    result = sss.load_sss_orders(path)
    assert [o["name"] for o in result["午餐"]] == ["张三", "李四"]


def test_validate_sss_orders_normalises_phone_and_rejects_invalid_rows():
    valid = {"午餐": [{
        "row": 3, "name": " 张三 ", "door": " Ａ 1 ",
        "phone": "１３８ ００００ ０００１",
    }]}
    sss._validate_sss_orders(valid)
    assert valid["午餐"][0] == {
        "row": 3, "name": "张三", "door": "A 1", "phone": "13800000001",
    }
    assert sss._normalise_phone(13800000001.0) == "13800000001"

    invalid = {"午餐": [
        {"row": 3, "name": "", "door": "A1", "phone": "13800000001"},
        {"row": 4, "name": "李四", "door": "", "phone": "13800000001.5"},
    ]}
    with pytest.raises(ValueError, match=r"午餐第 3 行.*午餐第 4 行"):
        sss._validate_sss_orders(invalid)


def test_dry_run_validates_all_excel_rows_before_any_network_path(tmp_path):
    from openpyxl import Workbook
    from types import SimpleNamespace

    wb = Workbook()
    ws = wb.active
    ws.title = "午餐"
    ws.append(["姓名", "门牌号", "电话"])
    ws.append(["表头占位", "", ""])
    ws.append(["张三", "A101", "not-a-phone"])
    path = tmp_path / "闪时送.xlsx"
    wb.save(path)
    wb.close()

    with pytest.raises(ValueError, match=r"午餐第 3 行.*11 位电话"):
        sss.run_sss_job(SimpleNamespace(sss_excel_path=str(path), sss_dry_run=True, sss_order_source="excel"),
                        object())


def test_substitute_tokens_replaces_placeholders_in_candidates():
    step = {"candidates": [{"text": "{label}"}, {"css": ".x", "has_text": "{label}"}]}
    out = sss._substitute_tokens(step, label="嗯哼")
    assert out["candidates"][0]["text"] == "嗯哼"
    assert out["candidates"][1]["has_text"] == "嗯哼"


def test_sss_locators_are_separate_from_default_table(monkeypatch, tmp_path):
    from app import locators as locators_mod

    monkeypatch.setattr(locators_mod, "user_data_dir", lambda: tmp_path)
    assert sss_user_locators_path() == tmp_path / "sss_locators.json"
    table = load_sss_locators()
    assert table["创建订单"]["candidates"]
    assert table["地址选项"]["candidates"][0]["has_text"] == "{label}"
    # 默认表（管理后台）与闪时送表互不污染。
    assert "门店地址" not in table
    assert "创建订单" not in locators_mod.DEFAULT_LOCATORS


def test_sss_locators_include_required_steps():
    for step in ("创建订单", "预约单选项", "一口轻食选项", "送达时间输入",
                 "顾客姓名", "顾客电话", "门牌号", "最终确定"):
        assert step in SSS_LOCATORS
        assert SSS_LOCATORS[step]["candidates"]


def test_build_order_payload_matches_captured_schema():
    import datetime as dt

    order = {"row": 3, "name": "张三", "door": "A101", "phone": "13800000001"}
    address = {"lnt": 119.727873, "lat": 30.257483,
               "areaCode": "330112", "addressDetail": "浙江农林大学东湖校区"}
    payload = sss.build_order_payload(order, False, 211053, address, "轻食",
                                      now=dt.datetime(2026, 9, 5, 18, 0))
    assert payload == {
        "expectedDeliveryTime": "2026-09-06 11:00:00",  # 18 点后顺延次日
        "goodsDetail": [{"goodsName": "轻食", "goodsNum": 1}],
        "orderType": 2,
        "receiveName": "张三",
        "receivePhone": "13800000001",
        "storeId": 211053,
        "receiveAddress": {
            "lnt": 119.727873, "lat": 30.257483,
            "areaCode": "330112",
            "addressDetail": "浙江农林大学东湖校区",
            "doorNum": "A101",
        },
    }


def test_match_record_prefers_keyword_and_single_record_fallback():
    records = [{"id": 1, "name": "其他"}, {"id": 2, "name": "一口轻食"}]
    assert sss._match_record(records, "一口轻食", "门店")["id"] == 2
    assert sss._match_record([{"id": 9}], "", "门店")["id"] == 9


def test_address_from_record_reads_marklnglat_like_frontend():
    record = {"name": "嗯哼", "markLnglat": {
        "longitude": 119.727873, "latitude": 30.257483,
        "adcode": "330112", "address": "浙江省杭州市临安区浙江农林大学东湖校区"}}
    addr = sss._address_from_record(record)
    assert addr == {"lnt": 119.727873, "lat": 30.257483,
                    "areaCode": "330112",
                    "addressDetail": "浙江省杭州市临安区浙江农林大学东湖校区"}


def test_address_from_record_reads_real_frequent_address_fields():
    """2026-09-05 实测的「嗯哼」记录结构：code=区划码、position=地址文本。"""
    record = {"id": 75812, "longitude": "119.728224", "latitude": "30.256632",
              "code": "330110", "position": "浙江农林大学东湖校区",
              "houseNum": "图书馆", "contactName": "嗯哼"}
    addr = sss._address_from_record(record)
    assert addr == {"lnt": 119.728224, "lat": 30.256632,
                    "areaCode": "330110", "addressDetail": "浙江农林大学东湖校区"}


def test_fixed_address_from_config_reads_config():
    from types import SimpleNamespace

    cfg = SimpleNamespace(
        sss_fixed_lnt="119.728224",
        sss_fixed_lat="30.256632",
        sss_fixed_area_code="330110",
        sss_fixed_address_detail="浙江农林大学东湖校区",
    )
    assert sss._fixed_address_from_config(cfg) == {
        "lnt": 119.728224,
        "lat": 30.256632,
        "areaCode": "330110",
        "addressDetail": "浙江农林大学东湖校区",
    }


def test_fixed_address_from_config_missing_coords_raises():
    from types import SimpleNamespace

    cfg = SimpleNamespace(
        sss_fixed_lnt=None,
        sss_fixed_lat=None,
        sss_fixed_area_code="330110",
        sss_fixed_address_detail="浙江农林大学东湖校区",
    )
    try:
        sss._fixed_address_from_config(cfg)
    except LookupError:
        pass
    else:
        raise AssertionError("缺少经纬度时应抛出 LookupError")


def test_normalize_timeout_converts_milliseconds():
    from app.api_client import _normalize_timeout

    connect, read = _normalize_timeout(8000)
    assert (connect, read) == (5.0, 8.0)
    assert _normalize_timeout(15) == (5.0, 15.0)
    assert _normalize_timeout((3.0, 9.0)) == (3.0, 9.0)


def test_match_record_prefers_named_fields():
    records = [
        {"id": 1, "remark": "一口轻食"},
        {"id": 2, "name": "一口轻食"},
    ]
    assert sss._match_record(records, "一口轻食", "门店", fields=("name",))["id"] == 2


def test_collect_tasks_reuses_expected_time():
    orders = {"午餐": [
        {"row": 3, "name": "A", "door": "1", "phone": "13800000001"},
        {"row": 4, "name": "B", "door": "2", "phone": "13800000002"},
    ]}
    address = {"lnt": 1.0, "lat": 2.0, "areaCode": "330110", "addressDetail": "X"}
    tasks = sss._collect_tasks(orders, 99, address, "轻食")
    assert len(tasks) == 2
    assert tasks[0]["payload"]["expectedDeliveryTime"] == tasks[1]["payload"]["expectedDeliveryTime"]


def test_submit_tasks_concurrent_faster_than_serial():
    import time

    tasks = [{"identifier": f"t{i}", "payload": {"i": i}} for i in range(10)]

    def submit(payload):
        time.sleep(0.05)
        return {"success": True}

    class Stop:
        def is_set(self):
            return False

    t0 = time.perf_counter()
    result = sss._submit_tasks_concurrent(tasks, submit, Stop(), None, max_workers=5)
    concurrent_elapsed = time.perf_counter() - t0
    assert result.succeeded == {f"t{i}" for i in range(10)} and result.failures == []
    t0 = time.perf_counter()
    result = sss._submit_tasks_concurrent(tasks, submit, Stop(), None, max_workers=1)
    serial_elapsed = time.perf_counter() - t0
    assert result.succeeded == {f"t{i}" for i in range(10)} and result.failures == []
    assert concurrent_elapsed < serial_elapsed / 2


def test_submit_tasks_concurrent_keeps_configured_workers_in_flight():
    """并发的**确定性**证据：同一时刻确实有 ``max_workers`` 个 POST 在途。

    不依赖墙钟（CI 上墙钟断言会假失败，见 tests/test_bridge_wps_copy_check.py
    的同类说明）：4 个 worker 必须同时在途屏障才会通过，串行执行只会超时。
    """
    import threading
    from types import SimpleNamespace

    assert sss.resolve_create_workers(SimpleNamespace(sss_max_workers=4)) == 4
    assert sss.resolve_create_workers(SimpleNamespace(sss_max_workers=1)) == 1

    lock = threading.Lock()
    in_flight = 0
    max_in_flight = 0
    barrier = threading.Barrier(4, timeout=5)

    def submit(payload):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        try:
            barrier.wait()
        finally:
            with lock:
                in_flight -= 1
        return {"success": True}

    tasks = [{"identifier": f"t{i}", "payload": {"i": i}} for i in range(8)]

    class Stop:
        def is_set(self):
            return False

    result = sss._submit_tasks_concurrent(tasks, submit, Stop(), None, max_workers=4)

    assert result.succeeded == {f"t{i}" for i in range(8)}
    assert result.failures == []
    assert max_in_flight == 4, "并发度没有生效（仍在串行提交）"
    assert max_in_flight <= 4, "在途请求数不得超过配置的并发路数"


@pytest.mark.parametrize("given,expected", [
    (1, 1), (4, 4), (8, 8), (20, 20), (99, 20), (0, 1), (-3, 1),
    (None, 1), ("abc", 1), ("7", 7),
])
def test_resolve_create_workers_clamps_config(given, expected):
    """配置里的并发路数被夹到 [1, 20]；残缺取值回退串行而不是静默放大。"""
    from types import SimpleNamespace

    assert sss.resolve_create_workers(SimpleNamespace(sss_max_workers=given)) == expected


def test_resolve_create_workers_defaults_to_four():
    """出厂默认 4 路；配置对象缺字段这种异常入参回退 1 路（串行）。"""
    from types import SimpleNamespace

    from app.config import AppConfig

    assert sss.resolve_create_workers(AppConfig()) == 4
    assert sss.resolve_create_workers(SimpleNamespace()) == 1


# ----------------------------------------------------------------------
# 提交节流（平台对同一账号的建单受理节奏约 1 单 / 2.1 秒，超速会被快速驳回）
# 2026-10-09 移植自服务端 v3.6.18（commit 03413f6）；只加节奏控制，不改结果分类。
# ----------------------------------------------------------------------
INTERNAL_ERROR = "java.lang.IndexOutOfBoundsException: Index: 0, Size: 0"


class _NeverStopped:
    def is_set(self):
        return False


def test_min_interval_spaces_post_starts():
    """配置提交最小间隔后，相邻两次 POST 的起点至少间隔该值。"""
    import time

    tasks = [{"identifier": f"t{i}", "payload": {"i": i}} for i in range(4)]
    starts = []

    def submit(payload):
        starts.append(time.perf_counter())
        return {"success": True}

    result = sss._submit_tasks_concurrent(tasks, submit, _NeverStopped(), None,
                                          max_workers=4, min_interval_s=0.15)
    assert result.succeeded == {f"t{i}" for i in range(4)}
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
    assert len(gaps) == 3
    assert all(gap >= 0.12 for gap in gaps), gaps


def test_zero_interval_keeps_immediate_dispatch_without_penalty():
    """0 = 关闭节流：不排队等待、也不产生任何节流日志（与引入节流前的行为一致）。"""
    tasks = [{"identifier": f"t{i}", "payload": {"i": i}} for i in range(3)]
    logs = []

    def submit(payload):
        return {"success": False, "message": INTERNAL_ERROR}

    result = sss._submit_tasks_concurrent(tasks, submit, _NeverStopped(), logs.append,
                                          max_workers=3)
    # 桌面版把 success=false 的平台响应归为显式失败（可人工重试），与服务端把
    # 内部异常归为 uncertain 不同——本次移植只加节流，不改这个分类。
    assert {identifier for identifier, _detail in result.failures} == {
        f"t{i}" for i in range(3)}
    assert result.succeeded == set()
    assert not any("提交最小间隔" in message for message in logs)


def test_platform_fast_reject_leaves_the_interval_untouched(monkeypatch):
    """平台快速驳回（IndexOutOfBounds）不再放宽间隔：节奏恒定，分类不变。

    2026-10-12 移除自适应放宽前的行为是每次驳回 ×1.5（上限 10 秒）。实测它既
    不能阻止驳回，又把下单节奏拖慢数倍，因此直接删除；这条用例锁死间隔不变。
    """
    traces = []
    monkeypatch.setattr(sss, "_trace", traces.append)
    tasks = [{"identifier": f"t{i}", "payload": {"i": i}} for i in range(4)]

    def submit(payload):
        return {"success": False, "message": INTERNAL_ERROR}

    result = sss._submit_tasks_concurrent(tasks, submit, _NeverStopped(), None,
                                          max_workers=4, min_interval_s=0.15)
    # 4 单全部被明确拒绝 → 显式失败（分类与引入放宽时一致），且全部是快速返回
    assert {identifier for identifier, _detail in result.failures} == {
        f"t{i}" for i in range(4)}
    assert result.succeeded == set()
    intervals = {line.rsplit("min_interval_s=", 1)[1] for line in traces
                 if "min_interval_s=" in line}
    assert intervals == {"0.150"}, traces


def test_pacer_aborts_waiting_on_stop_or_halt():
    """节流等待期间出现停止/401/余额不足：请求未发出，返回 False。"""
    from threading import Event

    pacer = sss._SubmitPacer(4.0)
    stopped = Event()
    stopped.set()
    assert pacer.wait(stopped) is False
    assert pacer.wait(Event(), halted=lambda: True) is False


def test_disabled_pacer_never_waits_or_aborts():
    """关闭节流时不引入任何新的等待/中止语义（保持原有行为）。"""
    from threading import Event

    pacer = sss._SubmitPacer(0.0)
    stopped = Event()
    stopped.set()
    assert pacer.wait(stopped) is True


def test_pacer_interval_never_drifts(monkeypatch):
    """节流间隔恒定：连发若干次（含被平台快速驳回）后仍是初始值。"""
    traces = []
    monkeypatch.setattr(sss, "_trace", traces.append)
    tasks = [{"identifier": f"t{i}", "payload": {"i": i}} for i in range(4)]

    def submit(payload):
        return {"success": False, "message": INTERNAL_ERROR}

    sss._submit_tasks_concurrent(tasks, submit, _NeverStopped(), None,
                                 max_workers=4, min_interval_s=0.15)
    intervals = [line.rsplit("min_interval_s=", 1)[1] for line in traces
                 if "min_interval_s=" in line]
    assert intervals and set(intervals) == {"0.150"}, traces


# ----------------------------------------------------------------------
# 接口模式：提交前静默 + 平台驳回自动重试（v3.6.7，与浏览器模式对齐）
# ----------------------------------------------------------------------

BUSINESS_REJECT = {"success": False, "message": "商品已下架"}


def _only_task():
    return [{"identifier": "t0", "payload": {"i": 0}}]


def test_retryable_reject_only_matches_platform_internal_errors():
    """只有"明确拒绝且属平台内部错误"才可重试；业务驳回/未知响应都不行。"""
    assert sss._is_retryable_reject(
        {"success": False, "message": INTERNAL_ERROR}) is True
    assert sss._is_retryable_reject(
        {"success": False,
         "message": "java.lang.RuntimeException: 系统异常"}) is True
    assert sss._is_retryable_reject(BUSINESS_REJECT) is False
    assert sss._is_retryable_reject({"success": False, "message": "地址无效"}) is False
    assert sss._is_retryable_reject(None) is False


def test_api_submit_retries_after_platform_rejection():
    """平台内部错误驳回 → 自动重试同一单；第 3 次尝试成功。"""
    responses = [
        {"success": False, "message": INTERNAL_ERROR},
        {"success": False, "message": INTERNAL_ERROR},
        {"success": True},
    ]
    posts = []
    logs = []

    def submit(payload):
        posts.append(payload)
        return responses[len(posts) - 1]

    result = sss._submit_tasks_concurrent(
        _only_task(), submit, _NeverStopped(), logs.append, max_workers=1,
        submit_attempts=3)
    assert result.succeeded == {"t0"}
    assert len(posts) == 3
    assert result.failures == [] and result.uncertain == []
    assert any("平台驳回（第 1/3 次）" in message for message in logs)
    assert any("平台驳回（第 2/3 次）" in message for message in logs)
    assert any("第 3 次提交后成功（平台驳回重试生效）" in message for message in logs)


def test_api_submit_retry_exhausted_is_a_failure():
    """连续驳回直到用尽尝试次数 → 仍按显式失败记录，失败明细只打印一次。"""
    posts = []
    logs = []

    def submit(payload):
        posts.append(payload)
        return {"success": False, "message": INTERNAL_ERROR}

    result = sss._submit_tasks_concurrent(
        _only_task(), submit, _NeverStopped(), logs.append, max_workers=1,
        submit_attempts=3)
    assert len(posts) == 3
    assert [identifier for identifier, _detail in result.failures] == ["t0"]
    assert sum(1 for message in logs if message.startswith("提交失败：")) == 1


def test_api_submit_does_not_retry_business_rejections():
    """业务类驳回是确定性的：不重试，直接按失败交给人工。"""
    posts = []
    logs = []

    def submit(payload):
        posts.append(payload)
        return BUSINESS_REJECT

    result = sss._submit_tasks_concurrent(
        _only_task(), submit, _NeverStopped(), logs.append, max_workers=1,
        submit_attempts=3)
    assert len(posts) == 1, "业务驳回不该重试（白打请求还多消耗平台序号）"
    assert [identifier for identifier, _detail in result.failures] == ["t0"]


def test_api_submit_never_retries_an_uncertain_post():
    """回归锁：超时/无 success 字段 = 结果未知，**绝不重发**（重复下单红线）。"""
    for uncertain_response in (None, {"code": 0, "msg": "ok"}):
        posts = []

        def submit(payload, _response=uncertain_response):
            posts.append(payload)
            if _response is None:
                raise sss._SubmissionUncertain("读取超时")
            return _response

        result = sss._submit_tasks_concurrent(
            _only_task(), submit, _NeverStopped(), None, max_workers=1,
            submit_attempts=3)
        assert len(posts) == 1, "结果未知的 POST 一次都不能重发"
        assert [identifier for identifier, _detail in result.uncertain] == ["t0"]
        assert result.succeeded == set()


def test_api_submit_never_retries_balance_depletion():
    """余额不足是 LookupError 家族：不能被"明确拒绝 → 重试"分支吃掉。"""
    posts = []

    def submit(payload):
        posts.append(payload)
        return {"success": False, "message": "余额不足，请充值"}

    result = sss._submit_tasks_concurrent(
        _only_task(), submit, _NeverStopped(), None, max_workers=1,
        submit_attempts=3)
    assert len(posts) == 1
    assert "余额不足" in result.balance_error
    assert result.succeeded == set()


def test_api_retries_obey_the_submit_interval():
    """重试同样吃提交节流：相邻 POST 起点（含重试）至少间隔最小间隔。"""
    import time

    starts = []

    def submit(payload):
        starts.append(time.perf_counter())
        return {"success": False, "message": INTERNAL_ERROR}

    result = sss._submit_tasks_concurrent(
        _only_task(), submit, _NeverStopped(), None, max_workers=1,
        min_interval_s=0.15, submit_attempts=3)
    assert len(starts) == 3
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
    assert all(gap >= 0.12 for gap in gaps), gaps
    assert [identifier for identifier, _detail in result.failures] == ["t0"]


def test_api_retry_stops_when_halted_before_the_retry():
    """轮到重试时已被停止/中止 → 不再派发，且该单仍记 failure（它确实发出过）。"""
    from threading import Event

    stop = Event()
    posts = []

    def submit(payload):
        posts.append(payload)
        stop.set()  # 首次被驳回的同一刻用户点了停止
        return {"success": False, "message": INTERNAL_ERROR}

    result = sss._submit_tasks_concurrent(
        _only_task(), submit, stop, None, max_workers=1,
        min_interval_s=0.05, submit_attempts=3)
    assert len(posts) == 1, "停止后不能再发第二次"
    assert [identifier for identifier, _detail in result.failures] == ["t0"], (
        "已经发出过请求的单绝不能被记成「未发送」")


def test_api_retry_wait_aborts_on_stop_without_sending_again():
    """重试的节流等待期间被停止 → 放弃重试，但结论仍是 failure（不是未发送）。"""
    import threading as _threading

    stop = _threading.Event()
    posts = []

    def submit(payload):
        posts.append(payload)
        if len(posts) == 1:
            _threading.Timer(0.05, stop.set).start()
        return {"success": False, "message": INTERNAL_ERROR}

    result = sss._submit_tasks_concurrent(
        _only_task(), submit, stop, None, max_workers=1,
        min_interval_s=0.4, submit_attempts=3)
    assert len(posts) == 1, "等待期间收到停止，绝不能补发"
    assert [identifier for identifier, _detail in result.failures] == ["t0"]


def test_api_settle_delays_each_post_attempt():
    """每次 POST（含重试）之前都先静默。"""
    import time

    starts = []

    def submit(payload):
        starts.append(time.perf_counter())
        return {"success": False, "message": INTERNAL_ERROR}

    sss._submit_tasks_concurrent(
        _only_task(), submit, _NeverStopped(), None, max_workers=1,
        pre_submit_settle_s=0.15, submit_attempts=2)
    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.15, starts


def test_api_settle_is_excluded_from_the_http_latency_window(monkeypatch):
    """静默不计入 HTTP 耗时：否则埋点里的耗时会把静默时长算进去。"""
    traces = []
    monkeypatch.setattr(sss, "_trace", traces.append)

    def submit(payload):
        return {"success": False, "message": INTERNAL_ERROR}

    sss._submit_tasks_concurrent(
        _only_task(), submit, _NeverStopped(), None, max_workers=1,
        min_interval_s=0.05, pre_submit_settle_s=1.6, submit_attempts=1)
    post_lines = [line for line in traces if line.startswith("POST ")]
    assert len(post_lines) == 1, traces
    elapsed = float(post_lines[0].split(": ", 1)[1].split("s ->", 1)[0])
    assert elapsed < 1.0, (
        f"静默 1.6s 若被算进 HTTP 耗时，埋点会记成 {elapsed:.2f}s：{post_lines[0]}")


def test_run_sss_job_api_mode_recovers_from_platform_rejections(
        monkeypatch, tmp_path):
    """端到端（接口模式）：平台驳回被自动重试救回，最终由站内对账确认。"""
    from tests.sss_test_support import _FakeClient, _config, _write_sss_excel

    _write_sss_excel(tmp_path)
    cfg = _config(tmp_path, sss_order_source="excel", sss_max_workers=1,
                  sss_submit_min_interval_s=0.0)
    client = _FakeClient(order_reject_times=1)
    monkeypatch.setattr(sss, "SssApiClient", lambda *a, **k: client)
    monkeypatch.setattr(sss, "_API_SUBMIT_SETTLE_S", 0.0)
    logs: list[str] = []

    result = sss.run_sss_job(cfg, threading.Event(), progress_callback=logs.append,
                             password="pw", captcha_callback=lambda _img: "1234")

    assert result["created"] == 2, logs
    assert result["uncertain"] is False
    assert client.rejects == 1
    # 被驳回的那一单多打了一次 POST：1 驳回 + 1 重试 + 1 正常 = 3 次
    assert len(client.posts) == 3, client.posts
    assert any("平台驳回重试生效" in message for message in logs), logs


def test_submission_trace_records_the_active_interval(monkeypatch):
    """埋点带上请求发出时生效的提交最小间隔，供后续取证对照。

    服务端把该值写进逐单诊断记录（``min_interval_s``）；桌面版没有诊断文件，
    对应的逐单记录就是 ``YIKOU_SSS_TRACE=1`` 的 POST 埋点，字段名保持一致。
    """
    traces = []
    monkeypatch.setattr(sss, "_trace", traces.append)
    task = {"identifier": "t0", "payload": {"i": 0}}

    def submit(payload):
        return {"success": True}

    sss._submit_tasks_concurrent([task], submit, _NeverStopped(), None, 1,
                                 min_interval_s=2.5)
    assert any("min_interval_s=2.500" in message for message in traces), traces


def test_pacing_wait_aborts_on_halt_without_sending():
    """节流等待期间出现 401（halted）：该任务确实没有发出，不算失败也不算未知。

    「未发送」与「已发送未知」是两种不同语义：前者重登后可以补发，后者必须
    先查站。节流只允许把等待中被中止的任务归入前者，且不产生任何 POST。
    """
    tasks = [{"identifier": f"t{i}", "payload": {"i": i}} for i in range(2)]
    posts = []

    def submit(payload):
        posts.append(payload["i"])
        raise sss._AuthExpired("401")

    result = sss._submit_tasks_concurrent(tasks, submit, _NeverStopped(), None,
                                          max_workers=2, min_interval_s=2.5)
    assert len(posts) == 1, "被中止的节流等待不能发出 POST"
    assert result.auth_error == "401"
    not_posted = [task["identifier"] for task in tasks
                  if task["payload"]["i"] not in posts]
    assert len(not_posted) == 1
    recorded = ({identifier for identifier, _detail in result.failures}
                | {identifier for identifier, _detail in result.uncertain}
                | result.succeeded)
    assert not_posted[0] not in recorded


def test_round_summary_reports_the_active_pacing():
    """本轮汇总行（对齐服务端 v3.6.18）：只报耗时/吞吐，不再附提交最小间隔。

    2026-10-12 移除自适应放宽时一并去掉汇总行里的间隔后缀；逐单间隔仍可从
    ``YIKOU_SSS_TRACE=1`` 的 POST 埋点（``min_interval_s``）读到。
    """
    logs = []
    tasks = [{"identifier": "t0", "payload": {}}]

    def submit(payload):
        return {"success": True}

    sss._submit_tasks_concurrent(tasks, submit, _NeverStopped(), logs.append, 1,
                                 min_interval_s=2.5)
    assert any("本轮提交 1 单" in message for message in logs), logs
    assert not any("提交最小间隔" in message for message in logs), logs


@pytest.mark.parametrize("given,expected", [
    (2.5, 2.5), (0, 0.0), (3, 3.0), (-1, 0.0), (999, 60.0), ("4.5", 4.5),
])
def test_resolve_submit_min_interval_takes_the_config_value_clamped(given, expected):
    from types import SimpleNamespace

    assert sss.resolve_submit_min_interval_s(
        SimpleNamespace(sss_submit_min_interval_s=given)) == expected


def test_resolve_submit_min_interval_falls_back_to_paced_default():
    """配置残缺/坏值时按出厂默认（2.5 秒）保守节流，而不是全速连发。"""
    from types import SimpleNamespace

    from app.config import AppConfig

    assert sss.resolve_submit_min_interval_s(AppConfig()) == 2.5
    assert sss.resolve_submit_min_interval_s(SimpleNamespace()) == 2.5
    assert sss.resolve_submit_min_interval_s(
        SimpleNamespace(sss_submit_min_interval_s="abc")) == 2.5
    assert sss.resolve_submit_min_interval_s(
        SimpleNamespace(sss_submit_min_interval_s=float("nan"))) == 2.5


def test_submit_tasks_concurrent_reports_failures():
    tasks = [{"identifier": f"t{i}", "payload": {"i": i}} for i in range(4)]

    def submit(payload):
        if payload["i"] == 2:
            return {"success": False, "message": "boom"}
        return {"success": True}

    class Stop:
        def is_set(self):
            return False

    result = sss._submit_tasks_concurrent(tasks, submit, Stop(), None, max_workers=2)
    assert result.succeeded == {"t0", "t1", "t3"}
    assert result.failures == [("t2", "boom")]


def test_submit_tasks_concurrent_auth_expired_aborts():
    tasks = [{"identifier": f"t{i}", "payload": {"i": i}} for i in range(4)]

    def submit(payload):
        raise sss._AuthExpired("401")

    class Stop:
        def is_set(self):
            return False

    result = sss._submit_tasks_concurrent(tasks, submit, Stop(), None, max_workers=1)
    assert result.auth_error == "401"
    assert result.succeeded == set()


def test_cached_store_id_hit_and_miss():
    from types import SimpleNamespace

    assert sss._cached_store_id(SimpleNamespace(sss_store_id=5, sss_store_name_cached="一口轻食"), "一口轻食") == 5
    assert sss._cached_store_id(SimpleNamespace(sss_store_id=5, sss_store_name_cached="别家"), "一口轻食") is None
    assert sss._cached_store_id(SimpleNamespace(sss_store_id=None, sss_store_name_cached="一口轻食"), "一口轻食") is None


def test_prepare_store_and_address_uses_cache(tmp_path):
    from types import SimpleNamespace

    calls = []

    def fetch_json(path):
        calls.append(path)
        raise AssertionError("命中缓存时不应发起任何查询")

    cfg = SimpleNamespace(sss_store_id=211053, sss_store_name_cached="一口轻食",
                          sss_fixed_lnt=1.0, sss_fixed_lat=2.0,
                          sss_fixed_area_code="330110", sss_fixed_address_detail="X")
    store_id, address = sss._prepare_store_and_address(
        fetch_json, cfg, "一口轻食", "", True, None)
    assert store_id == 211053
    assert address["lnt"] == 1.0
    assert calls == []


def test_dry_run_skips_network(tmp_path):
    from openpyxl import Workbook
    from types import SimpleNamespace

    wb = Workbook()
    ws = wb.active
    ws.title = "午餐"
    ws.append(["姓名", "门牌号", "电话", "送达时间"])
    ws.append(["占位", "", "", ""])
    ws.append(["张三", "A101", "13800000001", "11:00"])
    path = tmp_path / "闪时送.xlsx"
    wb.save(path)
    wb.close()

    cfg = SimpleNamespace(
        sss_excel_path=str(path), sss_order_source="excel", sss_account="18758187837",
        sss_dry_run=True, sss_store_name="一口轻食", sss_common_address="嗯哼",
        sss_use_fixed_address=True, sss_fixed_lnt=1.0, sss_fixed_lat=2.0,
        sss_fixed_area_code="330110", sss_fixed_address_detail="X",
        sss_product_name="轻食", api_mode=True, element_timeout_ms=8000,
        sss_url="https://example.invalid", sss_store_id=None,
        sss_store_name_cached="", sss_max_workers=8,
    )

    class Stop:
        def is_set(self):
            return False

    logs = []
    result = sss.run_sss_job(cfg, Stop(), logs.append, password="x",
                             captcha_callback=lambda img: (_ for _ in ()).throw(
                                 AssertionError("干跑不应索取验证码")))
    assert result["status"] == "dry_run"
    assert result["processed"] == 1
    assert result["created"] == 0
    assert result["previewed"] == 1
    assert result["submitted"] == 0
    assert result["reconciled"] is False
    assert any("干跑" in msg for msg in logs)


def test_submit_tasks_concurrent_auth_expired_keeps_successes():
    """401 中断后结构化结果保留已成功集合，主线程可只补提剩余项。"""
    tasks = [{"identifier": f"t{i}", "payload": {"i": i}} for i in range(4)]
    calls = []

    def submit(payload):
        calls.append(payload["i"])
        if payload["i"] == 0:
            return {"success": True}
        raise sss._AuthExpired("401")

    class Stop:
        def is_set(self):
            return False

    result = sss._submit_tasks_concurrent(tasks, submit, Stop(), None, max_workers=1)
    assert result.auth_error == "401"
    assert result.succeeded == {"t0"}


def test_check_success_detects_balance_depleted():
    for msg in ["余额不足，请充值", "账户余额不足", "Balance insufficient", "欠费停服"]:
        try:
            sss._check_success({"success": False, "message": msg})
        except sss._BalanceDepleted:
            pass
        else:
            raise AssertionError(f"应识别为余额不足：{msg}")
    try:
        sss._check_success({"success": False, "message": "地址无效"})
    except sss._BalanceDepleted:
        raise AssertionError("普通失败不应误判为余额不足")
    except LookupError:
        pass
    sss._check_success({"success": True})


def test_submit_tasks_concurrent_balance_aborts_batch():
    tasks = [{"identifier": f"t{i}", "payload": {"i": i}} for i in range(4)]

    def submit(payload):
        if payload["i"] == 1:
            return {"success": False, "message": "余额不足，请充值"}
        return {"success": True}

    class Stop:
        def __init__(self):
            self.flag = False

        def is_set(self):
            return self.flag

        def set(self):
            self.flag = True

    result = sss._submit_tasks_concurrent(tasks, submit, Stop(), None, max_workers=1)
    assert "余额不足" in result.balance_error
    assert result.succeeded == {"t0"}


def test_query_balance_parses_amounts():
    total, frozen = sss.query_balance(
        lambda p: {"success": True, "result": {"totalAmount": 323.9, "freezeAmount": -1189.4}})
    assert (total, frozen) == (323.9, -1189.4)
    assert sss.query_balance(lambda p: {"success": False}) == (None, None)

    def boom(path):
        raise RuntimeError("net down")

    assert sss.query_balance(boom) == (None, None)


def _task(identifier="t1", *, name="张三", phone="13800000001", door="B1",
          delivery="2026-09-10 11:00:00"):
    payload = {
        "receiveName": name,
        "receivePhone": phone,
        "expectedDeliveryTime": delivery,
        "receiveAddress": {"doorNum": door},
    }
    return {"identifier": identifier, "payload": payload,
            "fingerprint": sss._payload_fingerprint(payload)}


def _station_record(task):
    payload = task["payload"]
    return {
        "receiveName": payload["receiveName"],
        "receivePhone": payload["receivePhone"],
        "expectedDeliveryTime": payload["expectedDeliveryTime"],
        "receiveAddress": {"doorNum": payload["receiveAddress"]["doorNum"]},
    }


def test_sss_client_retries_get_but_not_post():
    from app.api_client import SssApiClient

    client = SssApiClient("https://example.invalid/takeout", "a", "p")
    try:
        adapter = client.session.get_adapter("https://example.invalid")
        assert set(adapter.max_retries.allowed_methods) == {"GET"}
    finally:
        client.close()


def test_sss_client_treats_non_json_401_as_auth_expired():
    from app.api_client import ApiError, SssApiClient

    class Html401:
        status_code = 401

        def json(self):
            raise ValueError("HTML login page")

    client = SssApiClient("https://example.invalid/takeout", "a", "p")
    client.session.request = lambda *args, **kwargs: Html401()  # type: ignore[method-assign]
    try:
        with pytest.raises(ApiError, match="401"):
            client.post_json("/consumer/order/one-touch-send/create-order-from-client", {})
    finally:
        client.close()


def test_api_submitter_uses_forked_worker_session_and_closes_it():
    class Worker:
        def __init__(self):
            self.closed = False

        def post_json(self, path, payload):
            assert path == sss._CREATE_ORDER_PATH
            return {"success": True, "payload": payload}

        def close(self):
            self.closed = True

    class Root:
        def __init__(self):
            self.workers = []

        def fork(self):
            worker = Worker()
            self.workers.append(worker)
            return worker

    root = Root()
    submit, close = sss._make_api_submitter(root)
    assert submit({"x": 1})["success"] is True
    assert len(root.workers) == 1
    close()
    assert root.workers[0].closed is True


def test_reconcile_normalizes_door_and_keeps_lunch_dinner_separate():
    lunch = _task("lunch", door="b1", delivery="2026-09-10 11:00:00")
    dinner = _task("dinner", door="B1", delivery="2026-09-10 17:00:00")

    def fetch(path):
        assert "startTime" not in path and "statusList" not in path
        return {"success": True, "result": {"records": [_station_record(lunch)], "total": 1}}

    result = sss._reconcile_tasks([lunch, dinner], fetch)
    assert result.confirmed == {"lunch"}
    assert [task["identifier"] for task in result.missing] == ["dinner"]
    assert result.duplicate_count == 0


# ---------------------------------------------------------------------------
# D06：地址门牌数字边界（B10 与 B1 不是同一处）
# 2026-10-13 第二轮收紧：同栋的房号/单元/楼层写法（…B1 101室、…B1101）视为同址
# ---------------------------------------------------------------------------

def test_地址兼容_b1_与_b10_门牌数字边界不得被吞并():
    """包含匹配必须在门牌数字边界处停下，但纯格式差异仍要兼容（D06）。

    ``…东湖校区b1`` 是 ``…东湖校区b10`` 的前缀，包含匹配若只看子串会判成
    同一处，把 B10 的订单误当成 B1 任务的完成证据；缺省市区前缀、多「号」
    尾缀这类纯格式差异则必须继续判为同址，否则会重复下单。
    """
    assert sss._addresses_compatible("浙江农林大学东湖校区b10",
                                     "浙江农林大学东湖校区b1") is False
    # 门牌数字的其它后缀扩张同样不再互认
    assert sss._addresses_compatible("12", "1202") is False
    assert sss._addresses_compatible("1", "11") is False
    # 纯格式差异仍是同址
    assert sss._addresses_compatible("东湖校区b1", "浙江农林大学东湖校区b1") is True
    assert sss._addresses_compatible("东湖校区b1号", "东湖校区b1") is True


def test_地址兼容_楼内房号单元楼层视为同址():
    """同一栋楼写了房号/单元/楼层仍是同一送餐点（2026-10-13 D06 收紧口径）。

    ``…b1101室``/``…b12单元``/``…b13层`` 与裸数字串 ``…b1101`` 都按“B1 楼内
    编号”理解，判同址；否则站内已有订单会被当成“还没下”而重复下单（方向与
    D06 本体相反）。
    """
    for station, task in (
        ("东湖校区b1101室", "东湖校区b1"),
        ("东湖校区b12单元", "东湖校区b1"),
        ("东湖校区b13层", "东湖校区b1"),
        ("东湖校区b1101", "东湖校区b1"),
    ):
        assert sss._addresses_compatible(station, task) is True, station
        assert sss._addresses_compatible(task, station) is True, station


def test_地址兼容_一位两位数字扩展仍判不同址():
    """1–2 位数字扩展与楼栋号无法区分，仍判不同址（D06 本体保护不回退）。"""
    assert sss._addresses_compatible("东湖校区b100", "东湖校区b1") is False
    assert sss._addresses_compatible("东湖校区b12栋", "东湖校区b1") is False
    assert sss._addresses_compatible("东湖校区b12楼", "东湖校区b1") is False
    assert sss._addresses_compatible("12", "1202") is False


def test_对账_站内_b10_记录不得确认_b1_任务():
    """站内概要地址「…东湖校区B10」不能成为本次「…东湖校区B1」任务的完成证据。

    姓名/电话/预约送达时间完全相同、只有门牌数字不同（B1 是 B10 的前缀）：
    该记录必须按「他人订单」忽略，任务仍算缺失，而不是判成已在站内不下单。
    """
    payload = {
        "receiveName": "张三",
        "receivePhone": "13800000001",
        "expectedDeliveryTime": "2026-09-10 11:00:00",
        "receiveAddress": {"addressDetail": "浙江农林大学东湖校区", "doorNum": "B1"},
    }
    task = {"identifier": "t1", "payload": payload,
            "fingerprint": sss._payload_fingerprint(payload)}
    record = {
        "id": 9001,
        "recipientName": "张三",
        "recipientPhone": ["13800000001"],
        "recipientAddress": "浙江农林大学东湖校区B10",
        "expectedDeliveryTime": "2026-09-10 11:00:00",
        "status": 2,
    }

    def fetch(path):
        return {"success": True, "result": {"records": [record], "total": 1}}

    result = sss._reconcile_tasks([task], fetch)
    assert result.confirmed == set()
    assert result.missing == [task]
    assert result.matched_count == 0


def test_对账_站内房间号写法应确认_b1_任务():
    """站内概要地址写了房号（…B1 101室）时，应确认为本次 B1 任务的完成证据。

    折叠后 ``…b1101室`` 是 ``…b1`` 加房号，不能再判成“他人订单”——否则任务
    留在 missing 里会被重新下单（2026-10-13 D06 收紧口径）。
    """
    payload = {
        "receiveName": "张三",
        "receivePhone": "13800000001",
        "expectedDeliveryTime": "2026-09-10 11:00:00",
        "receiveAddress": {"addressDetail": "浙江农林大学东湖校区", "doorNum": "B1"},
    }
    task = {"identifier": "t1", "payload": payload,
            "fingerprint": sss._payload_fingerprint(payload)}
    record = {
        "id": 9002,
        "recipientName": "张三",
        "recipientPhone": ["13800000001"],
        "recipientAddress": "浙江农林大学东湖校区B1 101室",
        "expectedDeliveryTime": "2026-09-10 11:00:00",
        "status": 2,
    }

    def fetch(path):
        return {"success": True, "result": {"records": [record], "total": 1}}

    result = sss._reconcile_tasks([task], fetch)
    assert result.confirmed == {"t1"}
    assert result.missing == []


def _b10_only_station_fixture(tmp_path, *, reject=False, station_door="B10"):
    """名单只送到 B1；站内只有同一客户的概要订单，地址是「…东湖校区B10」。

    站内记录的姓名/电话/预约送达时间与任务完全相同，只有门牌数字不同
    （B1 是 B10 的前缀）——这正是 D06 的触发条件。``reject=True`` 时下单
    会被站内以业务理由明确拒绝（确定未建单）；``station_door`` 可换成同栋
    的房号/单元写法（如 ``"B1 101室"``），用于 D06 收紧口径的端到端用例。
    """
    from openpyxl import Workbook

    from tests.sss_test_support import _FakeClient, _config

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "午餐"
    sheet.cell(2, 1, "姓名")
    sheet.cell(3, 1, "张三")
    sheet.cell(3, 2, "B1")
    sheet.cell(3, 3, "13800000001")
    excel_path = tmp_path / "闪时送.xlsx"
    workbook.save(excel_path)
    workbook.close()

    config = _config(tmp_path, sss_order_source="excel", sss_max_workers=1,
                     sss_submit_min_interval_s=0.0)
    orders = sss.load_sss_orders(excel_path)
    sss._validate_sss_orders(orders)
    tasks = sss._collect_tasks(orders, 7, sss._fixed_address_from_config(config),
                               config.sss_product_name,
                               account=config.sss_account, now=dt.datetime.now())
    assert len(tasks) == 1, tasks
    payload = tasks[0]["payload"]
    address = payload.get("receiveAddress") or {}
    assert address.get("doorNum") == "B1", address
    station_record = {
        "id": 9001,
        "recipientName": payload["receiveName"],
        "recipientPhone": [payload["receivePhone"]],
        "recipientAddress": f"{address.get('addressDetail', '')}{station_door}",
        "expectedDeliveryTime": payload["expectedDeliveryTime"],
        "status": 2,
    }

    class _B10OnlyClient(_FakeClient):
        """站内预置 B10 订单：列表接口只读 ``list_records``。"""

        def __init__(self):
            super().__init__(order_fail=reject)
            self.list_records = [dict(station_record)]

    return config, _B10OnlyClient()


def test_端到端_站内只有_b10_时_b1_仍会真实下单并确认(monkeypatch, tmp_path):
    """端到端（D06）：站内 B10 不能顶替本次 B1 任务，必须真实提交一次并等到确认。"""
    config, client = _b10_only_station_fixture(tmp_path)
    monkeypatch.setattr(sss, "SssApiClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(sss, "_API_SUBMIT_SETTLE_S", 0.0)

    logs: list[str] = []
    result = sss.run_sss_job(config, threading.Event(), progress_callback=logs.append,
                             password="pw", captcha_callback=lambda _img: "1234")

    assert len(client.posts) == 1, logs
    assert (client.posts[0].get("receiveAddress") or {}).get("doorNum") == "B1"
    assert result["created"] == 1, logs
    assert result["reconciled"] is True, logs
    assert result["partial"] is False, logs


def test_端到端_站内明确拒绝_b1_时如实报部分完成(monkeypatch, tmp_path):
    """端到端（D06）：B1 被真实提交后遭站内业务拒绝，如实报部分完成而非「已确认」。"""
    config, client = _b10_only_station_fixture(tmp_path, reject=True)
    monkeypatch.setattr(sss, "SssApiClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(sss, "_API_SUBMIT_SETTLE_S", 0.0)

    logs: list[str] = []
    result = sss.run_sss_job(config, threading.Event(), progress_callback=logs.append,
                             password="pw", captcha_callback=lambda _img: "1234")

    assert len(client.posts) == 1, logs
    assert (client.posts[0].get("receiveAddress") or {}).get("doorNum") == "B1"
    assert result["created"] == 0, logs
    assert result["partial"] is True, logs


@pytest.mark.parametrize("station_door", ["B1 101室", "B1 2单元", "B1101"])
def test_端到端_站内房间号写法时不再重复下单(monkeypatch, tmp_path, station_door):
    """端到端（D06 收紧）：站内已有同栋房号/单元写法的订单时，不得再 POST 一遍。"""
    config, client = _b10_only_station_fixture(tmp_path, station_door=station_door)
    monkeypatch.setattr(sss, "SssApiClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(sss, "_API_SUBMIT_SETTLE_S", 0.0)

    logs: list[str] = []
    result = sss.run_sss_job(config, threading.Event(), progress_callback=logs.append,
                             password="pw", captcha_callback=lambda _img: "1234")

    assert client.posts == [], logs
    assert result["created"] == 1, logs
    assert result["reconciled"] is True, logs
    assert result["partial"] is False, logs


def test_list_pending_orders_paginates():
    task = _task()
    calls = []
    unrelated = [{"receiveName": f"u{i}", "receivePhone": "1",
                  "expectedDeliveryTime": "2026-09-10 11:00:00",
                  "receiveAddress": {"doorNum": "X"}} for i in range(100)]

    def fetch(path):
        calls.append(path)
        if "pageNo=1" in path:
            return {"success": True, "result": {"records": unrelated, "total": 101}}
        return {"success": True, "result": {"records": [_station_record(task)], "total": 101}}

    result = sss._reconcile_tasks([task], fetch)
    assert result.confirmed == {"t1"}
    assert len(calls) == 2
    assert all("sortType=1" in path and "sort=1" in path for path in calls)
    assert all("startTime" not in path and "endTime" not in path
               and "statusList" not in path for path in calls)


def test_build_list_prefilter_uses_epoch_ms_window_with_margin():
    task = _task(delivery="2026-09-11 11:00:00")
    prefilter = sss._build_list_prefilter([task])

    # 服务端 time 型参数只接受 epoch 毫秒；字符串会被 Spring 拒绝。
    assert set(prefilter) == {"startTime", "endTime"}
    assert isinstance(prefilter["startTime"], int) and isinstance(prefilter["endTime"], int)
    start = dt.datetime.fromtimestamp(prefilter["startTime"] / 1000)
    end = dt.datetime.fromtimestamp(prefilter["endTime"] / 1000)
    # 目标日 09-11，前后各放宽 1 天 → 09-10 00:00:00 ~ 09-12 23:59:59
    assert start.strftime("%Y-%m-%d %H:%M:%S") == "2026-09-10 00:00:00"
    assert end.strftime("%Y-%m-%d %H:%M:%S") == "2026-09-12 23:59:59"
    # 刻意不带 status：同一批订单派发后会由 2 变成 3，硬编码会把这批单筛没。
    assert "status" not in prefilter and "statusList" not in prefilter


def test_build_list_prefilter_is_empty_without_parseable_days():
    assert sss._build_list_prefilter([]) == {}
    assert sss._build_list_prefilter([{"fingerprint": None}]) == {}


def test_list_pending_orders_sends_prefilter_and_keeps_local_day_filter():
    task = _task(delivery="2026-09-11 11:00:00")
    # 站内返回了一条「窗口内、但不是目标日」的记录：预筛只做粗筛，
    # 真正的判定必须仍由本地过滤负责，不能被算成匹配。
    other_day = _task("other", name="李四", phone="13900000002",
                      delivery="2026-09-10 11:00:00")

    def fetch(path):
        assert "startTime=" in path and "endTime=" in path
        return {"success": True, "result": {"records": [_station_record(task),
                                                        _station_record(other_day)],
                                            "total": 2}}

    records = sss._list_pending_orders(
        fetch, [task], prefilter=sss._build_list_prefilter([task]))
    assert [record["receiveName"] for record in records] == ["张三"]


class _PageCappedClient:
    """站内替身：遵守 ``pageNo``，但每页最多返回 ``page_cap`` 条（D11 触发条件）。

    与 ``_FakeClient`` 的差别只在列表分页：这里响应带回服务端**自报**的
    ``size``/``pages``/``current``（``pagination_metadata=False`` 时故意只回
    ``records``），用于验证「按响应分页大小翻页」与「无法证明读完就 fail-closed」。
    """

    def __init__(self, records, *, page_cap=None, pagination_metadata=True):
        self.site_records = [dict(record) for record in records]
        self.page_cap = page_cap
        self.pagination_metadata = pagination_metadata
        self.posts: list[dict] = []
        self.pages_requested: list[int] = []
        self.requested_page_sizes: list[int] = []

    def fetch_captcha(self):
        return b"png"

    def login(self, _code):
        return {"success": True}

    def fork(self):
        return self

    def close(self):
        return None

    def get_json(self, path):
        if "list" in path:
            return self._list(path)
        if "store" in path:
            return {"success": True, "result": [{"name": "一口轻食", "id": 7}]}
        if "account" in path:
            return {"success": True, "result": {"totalAmount": 1000.0}}
        return {"success": True, "result": []}

    def post_json(self, _path, payload):
        """照常记录 POST 并建单：有没有重复下单，由测试断言兜住。"""
        self.posts.append(payload)
        address = payload.get("receiveAddress") or {}
        self.site_records.append({
            "recipientName": payload.get("receiveName", ""),
            "recipientPhone": [payload.get("receivePhone", "")],
            "recipientAddress": f"{address.get('addressDetail', '')}"
                                f"{address.get('doorNum', '')}",
            "expectedDeliveryTime": payload.get("expectedDeliveryTime", ""),
            "status": 2,
        })
        return {"success": True}

    def _list(self, path):
        query = parse_qs(urlsplit(path).query)
        page_no = int((query.get("pageNo") or ["1"])[0])
        page_size = int((query.get("pageSize") or [str(sss._LIST_PAGE_SIZE)])[0])
        self.pages_requested.append(page_no)
        self.requested_page_sizes.append(page_size)
        effective = min(page_size, self.page_cap) if self.page_cap else page_size
        total = len(self.site_records)
        start = (page_no - 1) * effective
        page = self.site_records[start:start + effective]
        result: dict = {"records": [dict(record) for record in page]}
        if self.pagination_metadata:
            result.update({
                "total": total,
                "size": effective,
                "pages": max(1, math.ceil(total / effective)),
                "current": page_no,
            })
        return {"success": True, "result": result}


def _page_capped_fixture(tmp_path):
    """站内 11 单：前 10 单是其他客户，第 11 单才是本次目标（正好落在第 2 页）。"""
    from openpyxl import Workbook

    from tests.sss_test_support import _config

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "午餐"
    sheet.cell(2, 1, "姓名")
    sheet.cell(3, 1, "张三")
    sheet.cell(3, 2, "C3")
    sheet.cell(3, 3, "13900000001")
    excel_path = tmp_path / "闪时送.xlsx"
    workbook.save(excel_path)
    workbook.close()

    config = _config(tmp_path, sss_order_source="excel", sss_max_workers=1,
                     sss_submit_min_interval_s=0.0)
    orders = sss.load_sss_orders(excel_path)
    sss._validate_sss_orders(orders)
    tasks = sss._collect_tasks(orders, 7, sss._fixed_address_from_config(config),
                               config.sss_product_name,
                               account=config.sss_account, now=dt.datetime.now())
    assert len(tasks) == 1
    payload = tasks[0]["payload"]
    address = payload.get("receiveAddress") or {}
    target = {
        "id": "OLD-ZS-001",
        "recipientName": payload.get("receiveName", ""),
        "recipientPhone": [payload.get("receivePhone", "")],
        "recipientAddress": f"{address.get('addressDetail', '')}"
                            f"{address.get('doorNum', '')}",
        "expectedDeliveryTime": payload.get("expectedDeliveryTime", ""),
        "status": 2,
    }
    others = [{
        "id": f"OTHER-{index:03d}",
        "recipientName": f"其他客户{index}",
        "recipientPhone": [f"138000000{index:02d}"],
        "recipientAddress": f"其他小区{index}号",
        "expectedDeliveryTime": target["expectedDeliveryTime"],
        "status": 2,
    } for index in range(10)]
    return config, tasks[0], [*others, target]


def test_list_sweep_follows_server_pagination_metadata(monkeypatch, tmp_path):
    """实际分页大小被限小时必须按响应翻页：第 2 页的已有订单不能漏读。

    请求 pageSize=100、服务端每页只给 10 条（响应自报 size=10/pages=2/current）。
    若结束判定仍用请求值，第 1 页 10 < 100 就被当成「读完了」，第 2 页的目标
    订单永远看不到，进而被当成「站内不存在」重复下单。
    """
    config, task, site_records = _page_capped_fixture(tmp_path)
    client = _PageCappedClient(site_records, page_cap=10)

    swept = sss._list_pending_orders(client.get_json, [task])
    assert [record["id"] for record in swept] == [
        *(f"OTHER-{index:03d}" for index in range(10)), "OLD-ZS-001"]
    assert client.pages_requested == [1, 2], client.pages_requested
    assert client.requested_page_sizes[0] == sss._LIST_PAGE_SIZE

    # 端到端：站内已有该订单 → 一次 POST 都不发，且如实报「1 单已确认」。
    monkeypatch.setattr(sss, "SssApiClient", lambda *a, **k: client)
    monkeypatch.setattr(sss, "_API_SUBMIT_SETTLE_S", 0.0)
    logs: list[str] = []
    result = sss.run_sss_job(config, threading.Event(), progress_callback=logs.append,
                             password="pw", captcha_callback=lambda _img: "1234")

    assert client.posts == [], [payload.get("receiveName") for payload in client.posts]
    assert result["created"] == 1, logs
    assert result["partial"] is False, logs
    assert result["reconciled"] is True, logs


def test_list_sweep_fails_closed_when_pagination_unprovable(monkeypatch, tmp_path):
    """响应只有 records、没有 total/pages/size 且条数少于请求值：无法证明读完。

    「读不到」不能被当成「站内没有」：必须抛错停止，绝不能据此继续下单。
    """
    config, task, site_records = _page_capped_fixture(tmp_path)
    client = _PageCappedClient(site_records, page_cap=10, pagination_metadata=False)

    with pytest.raises(LookupError, match="无法确认已读完"):
        sss._list_pending_orders(client.get_json, [task])
    assert client.pages_requested == [1], client.pages_requested

    monkeypatch.setattr(sss, "SssApiClient", lambda *a, **k: client)
    monkeypatch.setattr(sss, "_API_SUBMIT_SETTLE_S", 0.0)
    logs: list[str] = []
    result = sss.run_sss_job(config, threading.Event(), progress_callback=logs.append,
                             password="pw", captcha_callback=lambda _img: "1234")

    assert client.posts == []
    assert result["created"] == 0, logs
    assert result["reconciled"] is False, logs
    assert result["uncertain"] is True, logs


def test_list_sweep_fails_closed_when_server_ignores_page_no():
    """服务端回显的页号与请求不一致（忽略了 pageNo）时必须停止，不能同页空转。"""
    task = _task()
    payload = {"success": True, "result": {
        "records": [dict(_station_record(task)) for _ in range(10)],
        "total": 11, "size": 10, "pages": 2, "current": 1}}

    def fetch(_path):
        return payload

    with pytest.raises(LookupError, match="pageNo"):
        sss._list_pending_orders(fetch, [task])


def test_reconcile_tasks_forwards_prefilter():
    task = _task()
    seen = []

    def fetch(path):
        seen.append(path)
        return {"success": True, "result": {"records": [_station_record(task)], "total": 1}}

    result = sss._reconcile_tasks([task], fetch, prefilter={"startTime": 1, "endTime": 2})
    assert result.confirmed == {"t1"}
    assert "startTime=1" in seen[0] and "endTime=2" in seen[0]


def test_safe_reconcile_falls_back_to_full_scan_when_prefilter_misses(monkeypatch):
    """预筛窗口漏单时必须用全量扫描复核，绝不能据此误判缺失。"""
    monkeypatch.setattr(sss, "_SSS_SERVER_PREFILTER", True)
    task = _task()
    calls = []

    def fetch(path):
        calls.append("window" if "startTime=" in path else "full")
        if "startTime=" in path:
            return {"success": True, "result": {"records": [], "total": 0}}
        return {"success": True, "result": {"records": [_station_record(task)], "total": 1}}

    logs = []
    result = sss._safe_reconcile([task], fetch, logs.append, "测试对账", zero_retry_delay=0)
    assert result is not None and result.confirmed == {"t1"} and not result.missing
    # 预筛空 → 立即重查一次（区分“刚写入不可见”）→ 仍空 → 全量复核。
    # 全量复核之前还有**时间窗自检**的两次只读探针（pageSize=1，不带过滤 +
    # 带窗口），用来确认服务端还接受 startTime/endTime；它只读、不改判定结论。
    assert calls[0] == "window" and calls[1] == "window"
    assert calls[-1] == "full", calls
    assert calls.count("full") >= 1 and "window" in calls
    assert "全量复核确认为 1/1 单" in "\n".join(logs)


def test_safe_reconcile_zero_window_retry_recovers_without_full_scan(monkeypatch):
    """预筛窗口 0 条但重查即命中时，只多花一次重查，不应退化成全量扫描。"""
    monkeypatch.setattr(sss, "_SSS_SERVER_PREFILTER", True)
    task = _task()
    calls = []

    def fetch(path):
        calls.append("window" if "startTime=" in path else "full")
        if "startTime=" not in path:
            raise AssertionError("重查命中后不应再做全量扫描")
        if calls.count("window") == 1:
            return {"success": True, "result": {"records": [], "total": 0}}
        return {"success": True, "result": {"records": [_station_record(task)], "total": 1}}

    logs = []
    result = sss._safe_reconcile([task], fetch, logs.append, "测试对账", zero_retry_delay=0)
    assert result is not None and result.confirmed == {"t1"} and not result.missing
    assert calls == ["window", "window"]
    assert "重查一次" in "\n".join(logs)


def test_safe_reconcile_skips_full_scan_when_prefilter_matches(monkeypatch):
    monkeypatch.setattr(sss, "_SSS_SERVER_PREFILTER", True)
    task = _task()
    calls = []

    def fetch(path):
        calls.append(path)
        return {"success": True, "result": {"records": [_station_record(task)], "total": 1}}

    result = sss._safe_reconcile([task], fetch, None, "测试对账")
    assert result is not None and not result.missing
    assert len(calls) == 1
    assert "startTime=" in calls[0]


def test_safe_reconcile_uses_full_scan_when_prefilter_request_fails(monkeypatch):
    """预筛请求报错时仍然降级全量扫描，而不是直接判对账失败。"""
    monkeypatch.setattr(sss, "_SSS_SERVER_PREFILTER", True)
    task = _task()
    calls = []

    def fetch(path):
        calls.append(path)
        if "startTime=" in path:
            raise RuntimeError("预筛参数被拒绝")
        return {"success": True, "result": {"records": [_station_record(task)], "total": 1}}

    result = sss._safe_reconcile([task], fetch, None, "测试对账")
    assert result is not None and result.confirmed == {"t1"}
    assert len(calls) == 2


def test_safe_reconcile_without_prefilter_never_scans_twice(monkeypatch):
    monkeypatch.setattr(sss, "_SSS_SERVER_PREFILTER", False)
    task = _task()
    calls = []

    def fetch(path):
        calls.append(path)
        return {"success": True, "result": {"records": [], "total": 0}}

    result = sss._safe_reconcile([task], fetch, None, "测试对账")
    assert result is not None and result.missing
    assert all("startTime" not in path for path in calls)


def test_reconcile_fails_closed_when_list_record_cannot_build_fingerprint():
    task = _task()
    malformed = {
        "receiveName": "张三",
        "receivePhone": "13800000001",
        "expectedDeliveryTime": "2026-09-10 11:00:00",
        "receiveAddress": {},
    }
    with pytest.raises(LookupError, match="无法安全对账"):
        sss._reconcile_tasks(
            [task], lambda path: {"success": True, "result": {"list": [malformed], "total": 1}})


def test_reconcile_detects_extra_duplicate_and_duplicate_excel_rows():
    first = _task("first")
    second = _task("second")
    records = [_station_record(first), _station_record(first), _station_record(first)]

    result = sss._reconcile_tasks(
        [first, second], lambda path: {"success": True, "result": {"records": records, "total": 3}})
    assert result.confirmed == {"first", "second"}
    assert result.missing == []
    assert result.duplicate_count == 1


def test_submit_stop_does_not_schedule_new_tasks_and_closes_sessions():
    calls = []
    closed = []

    class Stop:
        flag = False

        def is_set(self):
            return self.flag

        def set(self):
            self.flag = True

    stop = Stop()

    def submit(payload):
        calls.append(payload["i"])
        stop.set()
        return {"success": True}

    tasks = [{"identifier": f"t{i}", "payload": {"i": i}} for i in range(4)]
    result = sss._submit_tasks_concurrent(tasks, submit, stop, None, max_workers=1,
                                          on_stop=lambda: closed.append(True))
    assert calls == [0]
    assert closed == [True]
    assert result.succeeded == {"t0"}
    assert result.stopped is True


def test_uncertain_post_is_confirmed_by_reconciliation_without_retry():
    task = _task()
    on_site = []
    submits = []

    def fetch(path):
        return {"success": True, "result": {"records": list(on_site), "total": len(on_site)}}

    def factory():
        def submit(payload):
            submits.append(payload)
            on_site.append(_station_record(task))
            raise sss._SubmissionUncertain("ReadTimeout")

        return submit, lambda: None

    class Stop:
        def is_set(self):
            return False

        def set(self):
            raise AssertionError("已对账成功时不应停止")

    final, reconciled = sss._run_reconciled_submission(
        [task], factory, fetch, Stop(), None, None, max_workers=1)
    assert reconciled is True
    assert final is not None and final.confirmed == {"t1"}
    assert len(submits) == 1


def test_auth_expiry_relogs_once_then_only_submits_missing_task():
    task = _task()
    on_site = []
    submits = []
    relogins = []

    def fetch(path):
        return {"success": True, "result": {"records": list(on_site), "total": len(on_site)}}

    def factory():
        def submit(payload):
            submits.append(payload)
            if len(submits) == 1:
                raise sss._AuthExpired("401")
            on_site.append(_station_record(task))
            return {"success": True}

        return submit, lambda: None

    class Stop:
        def is_set(self):
            return False

        def set(self):
            raise AssertionError("401 恢复成功时不应停止")

    final, reconciled = sss._run_reconciled_submission(
        [task], factory, fetch, Stop(), None, None, max_workers=1,
        relogin=lambda: relogins.append(True))
    assert reconciled is True
    assert final is not None and final.confirmed == {"t1"}
    assert len(submits) == 2
    assert relogins == [True]


# ---------------------------------------------------------------------------
# P0 订单幂等性：完整指纹、对账延迟、并发互斥
# ---------------------------------------------------------------------------

def _full_task(identifier="full", *, name="张三", store_id=211053, goods_name="轻食",
               address_detail="浙江农林大学东湖校区", door="A101",
               account="18758187837", created_at=None):
    payload = {
        "receiveName": name,
        "receivePhone": "13800000001",
        "expectedDeliveryTime": "2026-09-10 11:00:00",
        "orderType": 2,
        "storeId": store_id,
        "goodsDetail": [{"goodsName": goods_name, "goodsNum": 1}],
        "receiveAddress": {
            "lnt": 119.728224,
            "lat": 30.256632,
            "areaCode": "330110",
            "addressDetail": address_detail,
            "doorNum": door,
        },
    }
    task = {
        "identifier": identifier,
        "payload": payload,
        "account": account,
        "fingerprint": sss._payload_fingerprint(payload, account=account),
    }
    if created_at is not None:
        task["created_at"] = created_at
    return task


def _full_station_record(task, **overrides):
    payload = task["payload"]
    address = payload["receiveAddress"]
    record = {
        "id": 1,
        "orderSn": "SN-1",
        "receiveName": payload["receiveName"],
        "receivePhone": payload["receivePhone"],
        "expectedDeliveryTime": payload["expectedDeliveryTime"],
        "orderType": payload["orderType"],
        "storeId": payload["storeId"],
        "goodsDetail": payload["goodsDetail"],
        "receiveAddress": dict(address),
        "user": {"mobile": task.get("account", "")},
        "created_at": task.get("created_at") or "2026-09-09 10:00:00",
    }
    record.update(overrides)
    return record


def _summary_station_record(task, **overrides):
    """订单列表接口 2026-09 实测的概要结构：字段名 recipient*、电话为数组、
    地址拍平成一整串，且不含 storeId/goodsDetail/坐标。"""
    payload = task["payload"]
    address = payload["receiveAddress"]
    record = {
        "id": 201489339,
        "pickUpNumber": "86",
        "recipientName": payload["receiveName"],
        "recipientPhone": [payload["receivePhone"]],
        "recipientAddress": (address["addressDetail"] + address["doorNum"]),
        "expectedDeliveryTime": payload["expectedDeliveryTime"],
        "orderTime": 1788991733000,
        "sendTime": 1788991737000,
        "status": 2,
    }
    record.update(overrides)
    return record


def test_summary_record_parses_recipient_fields_and_array_phone():
    task = _full_task()
    fp = sss._order_record_fingerprint(_summary_station_record(task))
    assert fp.receive_name == "张三"
    assert fp.receive_phone == "13800000001"
    assert fp.address_detail == "浙江农林大学东湖校区a101"
    assert fp.expected_delivery_time == task["payload"]["expectedDeliveryTime"]


def test_reconcile_matches_summary_schema_record():
    task = _full_task()
    result = sss._reconcile_tasks(
        [task], lambda path: {"success": True,
                              "result": {"records": [_summary_station_record(task)], "total": 1}})
    assert result.confirmed == {"full"}
    assert result.missing == []
    assert result.duplicate_count == 0


def test_reconcile_matches_summary_address_with_prefix_variation():
    """站内地址整串带省市区前缀时，应能兼容同一订单而不是误判为其他订单。"""
    task = _full_task(door="A101", address_detail="浙江农林大学东湖校区")
    record = _summary_station_record(
        task, recipientAddress="浙江省杭州市临安区浙江农林大学东湖校区A101")
    result = sss._reconcile_tasks(
        [task], lambda path: {"success": True, "result": {"records": [record], "total": 1}})
    assert result.confirmed == {"full"}
    assert result.missing == []


def test_reconcile_ignores_same_person_other_door_without_raising():
    """同人同时间的旧订单若门牌不同，应视为其他订单忽略，不得 fail-closed 整批。"""
    task = _full_task(door="A101")
    other = _summary_station_record(task, recipientAddress="浙江农林大学东湖校区学3")
    result = sss._reconcile_tasks(
        [task], lambda path: {"success": True, "result": {"records": [other], "total": 1}})
    assert result.confirmed == set()
    assert [item["identifier"] for item in result.missing] == ["full"]


def test_reconcile_ignores_unrelated_summary_records_without_raising():
    """概要结构里别人的订单不能触发 fail-closed（此前会因缺字段整批报错）。"""
    task = _full_task()
    unrelated = _summary_station_record(
        task, recipientName="程", recipientPhone=["18545726939"],
        recipientAddress="浙江农林大学东湖校区学3")
    result = sss._reconcile_tasks(
        [task], lambda path: {"success": True, "result": {"records": [unrelated], "total": 1}})
    assert result.confirmed == set()
    assert result.missing == [task]


def test_reconcile_fails_closed_when_summary_record_lacks_core_fields():
    task = _full_task()
    broken = _summary_station_record(task, recipientName="", recipientPhone=[])
    with pytest.raises(LookupError, match="无法安全对账"):
        sss._reconcile_tasks(
            [task], lambda path: {"success": True, "result": {"records": [broken], "total": 1}})


def test_payload_fingerprint_contains_all_context_fields():
    task = _full_task()
    fp = sss._payload_fingerprint(task["payload"], account=task["account"])
    assert fp.store_id == "211053"
    assert fp.goods_name == "轻食"
    assert fp.goods_num == "1"
    assert fp.address_detail == "浙江农林大学东湖校区"
    assert fp.area_code == "330110"
    assert fp.lnt == "119.728224"
    assert fp.lat == "30.256632"
    assert fp.order_type == "2"
    assert fp.account == "18758187837"
    assert sss._order_record_fingerprint(_full_station_record(task)) == fp


@pytest.mark.parametrize("change", [
    {"storeId": 999},
    {"goodsDetail": [{"goodsName": "豪华餐", "goodsNum": 1}]},
    {"receiveAddress": {"lnt": 1.0, "lat": 2.0, "areaCode": "330110",
                        "addressDetail": "别的地址", "doorNum": "A101"}},
    {"receiveAddress": {"lnt": 119.728224, "lat": 30.256632, "areaCode": "999999",
                        "addressDetail": "浙江农林大学东湖校区", "doorNum": "A101"}},
])
def test_reconcile_requires_full_fingerprint(change):
    task = _full_task()
    record = _full_station_record(task)
    record.update(change)
    result = sss._reconcile_tasks(
        [task], lambda path: {"success": True, "result": {"records": [record], "total": 1}})
    assert result.confirmed == set()
    assert [item["identifier"] for item in result.missing] == ["full"]


def test_reconcile_rejects_similar_order_created_before_batch_window():
    task = _full_task()
    record = _full_station_record(task, created_at="2020-01-01 00:00:00")
    result = sss._reconcile_tasks(
        [task], lambda path: {"success": True, "result": {"records": [record], "total": 1}},
        created_after=1_700_000_000.0)
    assert result.confirmed == set()
    assert [item["identifier"] for item in result.missing] == ["full"]


def test_reconcile_delay_polls_without_duplicate_post(monkeypatch):
    task = _full_task()
    on_site = []
    submits = []
    fetches = []

    def fetch(path):
        fetches.append(path)
        # 初始对账（1 次）后，服务端列表延迟两次，第三次才返回订单。
        if len(fetches) < 3:
            return {"success": True, "result": {"records": [], "total": 0}}
        return {"success": True, "result": {"records": [dict(on_site[0])], "total": 1}}

    def factory():
        def submit(payload):
            submits.append(payload)
            on_site.append(_full_station_record(
                task, created_at=int(dt.datetime.now().timestamp() * 1000)))
            return {"success": True}

        return submit, lambda: None

    class Stop:
        def is_set(self):
            return False

        def set(self):
            raise AssertionError("对账延迟不应停止任务")

    monkeypatch.setattr(sss, "_RECONCILE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(sss, "_PREFILTER_ZERO_RETRY_DELAY_S", 0)
    final, reconciled = sss._run_reconciled_submission(
        [task], factory, fetch, Stop(), None, None, max_workers=1)
    assert reconciled is True
    assert final is not None and final.confirmed == {"full"}
    assert len(submits) == 1, "对账延迟期间禁止重复 POST"
    assert len(fetches) >= 3


def test_sss_job_lock_rejects_concurrent_run(tmp_path):
    from types import SimpleNamespace

    class Stop:
        def is_set(self):
            return False

    sss._SSS_RUN_LOCK.acquire()
    try:
        with pytest.raises(RuntimeError, match="拒绝并发执行"):
            sss.run_sss_job(SimpleNamespace(sss_excel_path=str(tmp_path / "missing.xlsx"), sss_order_source="excel"),
                            Stop())
    finally:
        sss._SSS_RUN_LOCK.release()


def test_auth_expiry_preserves_successes_confirmed_after_relogin():
    first = _full_task("first")
    second = _full_task("second", door="B202")
    on_site = []
    submits = []
    relogins = []

    def fetch(path):
        return {"success": True, "result": {"records": list(on_site), "total": len(on_site)}}

    def factory():
        def submit(payload):
            submits.append(payload)
            if len(submits) == 1:
                on_site.append(_full_station_record(
                    first, created_at=int(dt.datetime.now().timestamp() * 1000)))
                return {"success": True}
            if len(submits) == 2:
                raise sss._AuthExpired("401")
            on_site.append(_full_station_record(
                second, created_at=int(dt.datetime.now().timestamp() * 1000)))
            return {"success": True}

        return submit, lambda: None

    class Stop:
        def is_set(self):
            return False

        def set(self):
            raise AssertionError("401 恢复成功时不应停止")

    final, reconciled = sss._run_reconciled_submission(
        [first, second], factory, fetch, Stop(), None, None, max_workers=1,
        relogin=lambda: relogins.append(True))
    assert reconciled is True
    assert final is not None and final.confirmed == {"first", "second"}
    assert len(submits) == 3
    assert relogins == [True]


def _同名同址双任务():
    """两条指纹完全相同、标识不同的任务（D09 的触发条件）。

    同一餐里两行同名/同址/同电话/同送达时间：站内一条订单能同时「匹配」两条
    任务，但一条站内记录只能充当一条任务的完成证据。
    """
    first = _full_task("第 3 行 张三")
    second = _full_task("第 4 行 张三")
    assert first["fingerprint"] == second["fingerprint"], "两条任务的指纹必须相同"
    return first, second


def _站内唯一匹配记录(task, *, order_id=None, created_ms=None):
    """站内只有这一条匹配记录（概要结构；可选补订单号与创建时间）。"""
    payload = task["payload"]
    address = payload["receiveAddress"]
    record = {
        "recipientName": payload["receiveName"],
        "recipientPhone": [payload["receivePhone"]],
        "recipientAddress": f"{address['addressDetail']}{address['doorNum']}",
        "expectedDeliveryTime": payload["expectedDeliveryTime"],
        "status": 2,
    }
    if order_id is not None:
        record["id"] = order_id
    if created_ms is not None:
        record["createTime"] = created_ms
    return record


@pytest.mark.parametrize("variant", ["缺创建时间", "带订单号且创建于本批窗口内"])
def test_同一张站内记录只能确认一条任务(variant, monkeypatch):
    """D09 回归：站内只有 1 条匹配记录时，它只能确认 1 条任务。

    两条任务指纹相同，站内只有一条匹配记录；另一条被站内明确拒绝（「商品已
    下架」，确定未建单）。旧代码：提交前拿这条记录确认「第 3 行 张三」，
    收尾对账又拿同一张记录确认「第 4 行 张三」，两轮 confirmed 取并集后把
    「只发出 1 次失败请求、站内仍只有 1 单」报成两单全部完成。

    两个变体分别覆盖「记录缺订单号与创建时间」与「记录带订单号、创建于本批
    时间窗口内」——证明并非只有列表缺字段时才会发生。
    """
    first, second = _同名同址双任务()
    record = _站内唯一匹配记录(first)
    if variant == "带订单号且创建于本批窗口内":
        record["id"] = "OLD-001"
        record["createTime"] = int((dt.datetime.now().timestamp() - 30) * 1000)
    submits = []

    def fetch(path):
        return {"success": True, "result": {"records": [dict(record)], "total": 1}}

    def factory():
        def submit(payload):
            submits.append(payload)
            return {"success": False, "message": "商品已下架"}

        return submit, lambda: None

    class Stop:
        def is_set(self):
            return False

        def set(self):
            raise AssertionError("明确拒绝 + 未对账成功时不应停止")

    monkeypatch.setattr(sss, "_RECONCILE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(sss, "_PREFILTER_ZERO_RETRY_DELAY_S", 0)

    final, reconciled = sss._run_reconciled_submission(
        [first, second], factory, fetch, Stop(), None, None, max_workers=1)

    assert reconciled is True
    assert final is not None
    assert len(final.confirmed) == 1, final
    assert len(final.missing) == 1, final
    assert (final.confirmed | {task["identifier"] for task in final.missing}) == {
        "第 3 行 张三", "第 4 行 张三"}, final
    assert final.duplicate_count == 0
    assert len(submits) == 1, "只允许提交那条没有被旧记录确认的任务"


def test_collect_tasks_reuses_stable_client_request_id_on_retry():
    orders = {"午餐": [{"row": 3, "name": "张三", "door": "A101",
                        "phone": "13800000001"}]}
    address = {"lnt": 1.0, "lat": 2.0, "areaCode": "330110", "addressDetail": "X"}
    first = sss._collect_tasks(orders, 99, address, "轻食",
                               account="acct", batch_id="batch-a")
    second = sss._collect_tasks(orders, 99, address, "轻食",
                                account="acct", batch_id="batch-b")
    assert first[0]["client_request_id"] == second[0]["client_request_id"]
    assert "clientRequestId" not in first[0]["payload"]

    enabled = sss._collect_tasks(orders, 99, address, "轻食", account="acct",
                                 idempotency_field="clientRequestId")
    assert enabled[0]["payload"]["clientRequestId"] == enabled[0]["client_request_id"]


# ---------------------------------------------------------------------------
# 闪时送登录态失效识别（HTTP 200 + code=10000）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"success": False, "message": "token失效，请重新登陆", "code": 10000},
    {"success": False, "message": "登录态失效，请重新登录", "code": 10000},
    {"success": False, "message": "TOKEN INVALID", "code": 500},
    {"success": False, "message": "Unauthorized", "code": 500},
])
def test_sss_client_detects_auth_expired_payload(payload):
    from app.api_client import ApiError, SssApiClient

    class Response:
        status_code = 200

        def json(self):
            return payload

    client = SssApiClient("https://example.invalid/takeout", "a", "p")
    client.session.request = lambda *args, **kwargs: Response()  # type: ignore[method-assign]
    try:
        with pytest.raises(ApiError, match="登录态已失效|token|TOKEN|Unauthorized"):
            client.get_json(sss._ORDER_LIST_PATH)
    finally:
        client.close()


def test_post_one_maps_code_10000_api_error_to_auth_expired():
    class Client:
        def post_json(self, path, payload):
            raise sss.ApiError("闪时送登录态已失效（code=10000，token失效，请重新登陆），请重新登录")

    with pytest.raises(sss._AuthExpired, match="10000"):
        sss._post_one(Client(), {})


def test_query_balance_propagates_auth_expired_instead_of_silent_none():
    def fetch_auth(path):
        raise sss.ApiError("闪时送登录态已失效（code=10000，token失效），请重新登录")

    with pytest.raises(sss._AuthExpired):
        sss.query_balance(fetch_auth)

    def fetch_payload(path):
        return {"success": False, "message": "登录态失效，请重新登录", "code": 10000}

    with pytest.raises(sss._AuthExpired):
        sss.query_balance(fetch_payload)


def test_prepare_store_and_address_serial_mode_uses_single_thread():
    import threading
    from types import SimpleNamespace

    threads = []
    responses = {
        sss._STORE_LIST_PATH: {"result": {"records": [{"id": 1, "name": "一口轻食"}]}},
        sss._FREQUENT_ADDR_PATH: {"result": {"records": [{
            "contactName": "嗯哼", "longitude": 1.0, "latitude": 2.0,
            "code": "330110", "position": "X"}]}},
    }

    def fetch_json(path):
        threads.append(threading.current_thread().name)
        return responses[path]

    cfg = SimpleNamespace(sss_store_id=None, sss_store_name_cached="")
    store_id, address = sss._prepare_store_and_address(
        fetch_json, cfg, "一口轻食", "嗯哼", False, None, parallel=False)
    assert store_id == 1
    assert address["areaCode"] == "330110"
    assert len(set(threads)) == 1


def test_with_auth_relogin_retries_once_after_token_expired():
    calls = []

    def operation():
        calls.append("operation")
        if calls.count("operation") == 1:
            raise sss._AuthExpired("token失效，请重新登陆 code=10000")
        return "ok"

    result = sss._with_auth_relogin(operation, lambda: calls.append("relogin"), None, "读取余额")
    assert result == "ok"
    assert calls == ["operation", "relogin", "operation"]


def test_preflight_reads_but_never_submits(monkeypatch, tmp_path):
    """预检：真实登录 + 读门店/余额/订单列表对账，但绝不发送任何下单 POST。"""
    from openpyxl import Workbook
    from types import SimpleNamespace

    wb = Workbook()
    ws = wb.active
    ws.title = "午餐"
    ws.append(["午餐", None, None, None])
    ws.append(["姓名", "门牌号", "电话", "送达时间"])
    ws.append(["张三", "A101", "13800000001", "11:00"])
    path = tmp_path / "闪时送.xlsx"
    wb.save(path)
    wb.close()

    task = _full_task()
    # 送达时间由 run_sss_job 按当前时钟计算（16 点后顺延次日），站内记录必须
    # 使用同一时间才可能对上，测试里动态取一次避免跨天导致的假失败。
    task["payload"]["expectedDeliveryTime"] = sss.compute_delivery_time(False)
    task["fingerprint"] = sss._payload_fingerprint(task["payload"], account=task["account"])
    expected_record = _full_station_record(task)
    posts = []
    gets = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_captcha(self):
            return b"\x89PNG fake"

        def login(self, code):
            assert code == "1234"

        def get_json(self, path):
            gets.append(path)
            if "queryStoreAddresses" in path:
                return {"success": True,
                        "result": {"records": [{"id": 211053, "name": "一口轻食"}],
                                   "total": 1}}
            if "get-login-user-account" in path:
                return {"success": True,
                        "result": {"totalAmount": 500.0, "freezeAmount": 0.0}}
            if "one-touch-send/list" in path:
                return {"success": True,
                        "result": {"records": [expected_record], "total": 1}}
            raise AssertionError(f"未预期的 GET：{path}")

        def post_json(self, path, body=None):
            posts.append((path, body))
            raise AssertionError("预检模式绝不能发送下单 POST")

        def close(self):
            pass

    monkeypatch.setattr(sss, "SssApiClient", FakeClient)

    cfg = SimpleNamespace(
        sss_excel_path=str(path), sss_order_source="excel", sss_account="18758187837",
        sss_dry_run=False, sss_preflight=True, sss_store_name="一口轻食",
        sss_common_address="嗯哼", sss_use_fixed_address=True,
        sss_fixed_lnt=119.728224, sss_fixed_lat=30.256632,
        sss_fixed_area_code="330110",
        sss_fixed_address_detail="浙江农林大学东湖校区",
        sss_product_name="轻食", api_mode=True,
        element_timeout_ms=8000, sss_url="https://example.invalid",
        sss_store_id=None, sss_store_name_cached="", sss_max_workers=4,
        sss_unit_price=1.9, sss_read_timeout_s=20.0, sss_idempotency_field="",
    )

    class Stop:
        def is_set(self):
            return False

    logs = []
    result = sss.run_sss_job(cfg, Stop(), logs.append, password="x",
                             captcha_callback=lambda img: "1234")

    assert result["status"] == "preflight_ok"
    assert result["submitted"] == 0
    assert result["created"] == 1
    assert result["stopped"] is False
    assert posts == []
    list_paths = [path for path in gets if "one-touch-send/list" in path]
    assert list_paths
    # 预筛只带 epoch 毫秒时间窗；无效的 statusList 一律不发。
    assert all("startTime=" in path and "endTime=" in path for path in list_paths)
    assert all("statusList" not in path for path in gets)


def test_preflight_succeeds_when_station_has_no_orders(monkeypatch, tmp_path):
    """站内一条订单都没有时，预检应正常完成（preflight_ok）而不是报不确定。"""
    from openpyxl import Workbook
    from types import SimpleNamespace

    wb = Workbook()
    ws = wb.active
    ws.title = "午餐"
    ws.append(["午餐", None, None, None])
    ws.append(["姓名", "门牌号", "电话", "送达时间"])
    ws.append(["张三", "A101", "13800000001", "11:00"])
    path = tmp_path / "闪时送.xlsx"
    wb.save(path)
    wb.close()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_captcha(self):
            return b"\x89PNG fake"

        def login(self, code):
            pass

        def get_json(self, path):
            if "queryStoreAddresses" in path:
                return {"success": True,
                        "result": {"records": [{"id": 211053, "name": "一口轻食"}],
                                   "total": 1}}
            if "get-login-user-account" in path:
                return {"success": True,
                        "result": {"totalAmount": 500.0, "freezeAmount": 0.0}}
            if "one-touch-send/list" in path:
                return {"success": True, "result": {"records": [], "total": 0}}
            raise AssertionError(f"未预期的 GET：{path}")

        def post_json(self, path, body=None):
            raise AssertionError("预检模式绝不能发送下单 POST")

        def close(self):
            pass

    monkeypatch.setattr(sss, "SssApiClient", FakeClient)

    cfg = SimpleNamespace(
        sss_excel_path=str(path), sss_order_source="excel", sss_account="18758187837",
        sss_dry_run=False, sss_preflight=True, sss_store_name="一口轻食",
        sss_common_address="嗯哼", sss_use_fixed_address=True,
        sss_fixed_lnt=119.728224, sss_fixed_lat=30.256632,
        sss_fixed_area_code="330110",
        sss_fixed_address_detail="浙江农林大学东湖校区",
        sss_product_name="轻食", api_mode=True,
        element_timeout_ms=8000, sss_url="https://example.invalid",
        sss_store_id=None, sss_store_name_cached="", sss_max_workers=4,
        sss_unit_price=1.9, sss_read_timeout_s=20.0, sss_idempotency_field="",
    )

    class Stop:
        def is_set(self):
            return False

    logs = []
    result = sss.run_sss_job(cfg, Stop(), logs.append, password="x",
                             captcha_callback=lambda img: "1234")

    assert result["status"] == "preflight_ok"
    assert result["submitted"] == 0
    assert result["created"] == 0
    assert result["reconciled"] is True
    assert result["uncertain"] is False


def test_preflight_stops_when_order_list_is_unreadable(monkeypatch, tmp_path):
    """预检遇到对账失败必须返回 uncertain 并停止，且仍然不发 POST。"""
    from openpyxl import Workbook
    from types import SimpleNamespace

    wb = Workbook()
    ws = wb.active
    ws.title = "午餐"
    ws.append(["午餐", None, None, None])
    ws.append(["姓名", "门牌号", "电话", "送达时间"])
    ws.append(["张三", "B1", "13800000001", "11:00"])
    path = tmp_path / "闪时送.xlsx"
    wb.save(path)
    wb.close()

    posts = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_captcha(self):
            return b"\x89PNG fake"

        def login(self, code):
            pass

        def get_json(self, path):
            if "queryStoreAddresses" in path:
                return {"success": True,
                        "result": {"records": [{"id": 211053, "name": "一口轻食"}],
                                   "total": 1}}
            if "get-login-user-account" in path:
                return {"success": True,
                        "result": {"totalAmount": 500.0, "freezeAmount": 0.0}}
            if "one-touch-send/list" in path:
                return {"success": True, "code": 200,
                        "result": {"total": 0, "size": 10, "current": 1, "pages": 0}}
            raise AssertionError(f"未预期的 GET：{path}")

        def post_json(self, path, body=None):
            posts.append((path, body))
            raise AssertionError("预检模式绝不能发送下单 POST")

        def close(self):
            pass

    monkeypatch.setattr(sss, "SssApiClient", FakeClient)

    cfg = SimpleNamespace(
        sss_excel_path=str(path), sss_order_source="excel", sss_account="18758187837",
        sss_dry_run=False, sss_preflight=True, sss_store_name="一口轻食",
        sss_common_address="嗯哼", sss_use_fixed_address=True,
        sss_fixed_lnt=1.0, sss_fixed_lat=2.0, sss_fixed_area_code="330110",
        sss_fixed_address_detail="X", sss_product_name="轻食", api_mode=True,
        element_timeout_ms=8000, sss_url="https://example.invalid",
        sss_store_id=None, sss_store_name_cached="", sss_max_workers=4,
        sss_unit_price=1.9, sss_read_timeout_s=20.0, sss_idempotency_field="",
    )

    class Stop:
        def is_set(self):
            return False

    logs = []
    result = sss.run_sss_job(cfg, Stop(), logs.append, password="x",
                             captcha_callback=lambda img: "1234")

    assert result["status"] == "preflight_uncertain"
    assert result["stopped"] is True
    assert result["reconciled"] is False
    assert result["uncertain"] is True
    assert posts == []
    assert any("缺少 records/list" in msg for msg in logs)


def test_configured_workers_reach_the_submission_call_site(monkeypatch, tmp_path):
    """配置的并发路数必须真的传到提交层，且日志如实打印（不再被硬编码成 1 路）。"""
    from openpyxl import Workbook
    from types import SimpleNamespace

    wb = Workbook()
    ws = wb.active
    ws.title = "午餐"
    ws.append(["午餐", None, None, None])
    ws.append(["姓名", "门牌号", "电话", "送达时间"])
    ws.append(["张三", "B1", "13800000001", "11:00"])
    path = tmp_path / "闪时送.xlsx"
    wb.save(path)
    wb.close()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_captcha(self):
            return b"\x89PNG fake"

        def login(self, code):
            pass

        def get_json(self, path):
            if "queryStoreAddresses" in path:
                return {"success": True,
                        "result": {"records": [{"id": 211053, "name": "一口轻食"}],
                                   "total": 1}}
            if "get-login-user-account" in path:
                return {"success": True,
                        "result": {"totalAmount": 500.0, "freezeAmount": 0.0}}
            raise AssertionError(f"提交被替换后不应再查站：{path}")

        def post_json(self, path, body=None):
            raise AssertionError("提交被替换后不应发出 POST")

        def close(self):
            pass

    monkeypatch.setattr(sss, "SssApiClient", FakeClient)

    seen: list[int] = []

    def fake_run(tasks, factory, fetch_json, stop_event, callback, decision_callback,
                 max_workers, **kwargs):
        seen.append(max_workers)
        return sss._Reconciliation(set(), [], 0, 0), True

    monkeypatch.setattr(sss, "_run_reconciled_submission", fake_run)

    cfg = SimpleNamespace(
        sss_excel_path=str(path), sss_order_source="excel", sss_account="18758187837",
        sss_dry_run=False, sss_preflight=False, sss_store_name="一口轻食",
        sss_common_address="嗯哼", sss_use_fixed_address=True,
        sss_fixed_lnt=1.0, sss_fixed_lat=2.0, sss_fixed_area_code="330110",
        sss_fixed_address_detail="X", sss_product_name="轻食", api_mode=True,
        element_timeout_ms=8000, sss_url="https://example.invalid",
        sss_store_id=None, sss_store_name_cached="", sss_max_workers=4,
        sss_unit_price=1.9, sss_read_timeout_s=30.0, sss_idempotency_field="",
    )

    class Stop:
        def is_set(self):
            return False

    logs = []
    result = sss.run_sss_job(cfg, Stop(), logs.append, password="x",
                             captcha_callback=lambda img: "1234")

    assert seen == [4], "配置的 4 路并发没有传到提交层"
    assert any("创建订单 4 路并发提交" in msg for msg in logs), logs
    assert result["processed"] == 1


@pytest.mark.parametrize("interval,in_seconds", [(2.5, "2.5s"), (0.0, None)])
def test_configured_submit_interval_reaches_the_submission_call_site(
        monkeypatch, tmp_path, interval, in_seconds):
    """提交最小间隔必须真的传到提交层；开始行只在启用时追加间隔（0 = 关闭）。"""
    from openpyxl import Workbook
    from types import SimpleNamespace

    wb = Workbook()
    ws = wb.active
    ws.title = "午餐"
    ws.append(["午餐", None, None, None])
    ws.append(["姓名", "门牌号", "电话", "送达时间"])
    ws.append(["张三", "B1", "13800000001", "11:00"])
    path = tmp_path / "闪时送.xlsx"
    wb.save(path)
    wb.close()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_captcha(self):
            return b"\x89PNG fake"

        def login(self, code):
            pass

        def get_json(self, path):
            if "queryStoreAddresses" in path:
                return {"success": True,
                        "result": {"records": [{"id": 211053, "name": "一口轻食"}],
                                   "total": 1}}
            if "get-login-user-account" in path:
                return {"success": True,
                        "result": {"totalAmount": 500.0, "freezeAmount": 0.0}}
            raise AssertionError(f"提交被替换后不应再查站：{path}")

        def post_json(self, path, body=None):
            raise AssertionError("提交被替换后不应发出 POST")

        def close(self):
            pass

    monkeypatch.setattr(sss, "SssApiClient", FakeClient)

    seen: list[float] = []

    def fake_run(tasks, factory, fetch_json, stop_event, callback, decision_callback,
                 max_workers, **kwargs):
        seen.append(kwargs.get("submit_min_interval_s"))
        return sss._Reconciliation(set(), [], 0, 0), True

    monkeypatch.setattr(sss, "_run_reconciled_submission", fake_run)

    cfg = SimpleNamespace(
        sss_excel_path=str(path), sss_order_source="excel", sss_account="18758187837",
        sss_dry_run=False, sss_preflight=False, sss_store_name="一口轻食",
        sss_common_address="嗯哼", sss_use_fixed_address=True,
        sss_fixed_lnt=1.0, sss_fixed_lat=2.0, sss_fixed_area_code="330110",
        sss_fixed_address_detail="X", sss_product_name="轻食", api_mode=True,
        element_timeout_ms=8000, sss_url="https://example.invalid",
        sss_store_id=None, sss_store_name_cached="", sss_max_workers=4,
        sss_submit_min_interval_s=interval,
        sss_unit_price=1.9, sss_read_timeout_s=30.0, sss_idempotency_field="",
    )

    class Stop:
        def is_set(self):
            return False

    logs = []
    result = sss.run_sss_job(cfg, Stop(), logs.append, password="x",
                             captcha_callback=lambda img: "1234")

    assert seen == [interval], "配置的提交最小间隔没有传到提交层"
    start_lines = [msg for msg in logs if "开始下单" in msg]
    assert start_lines, logs
    if in_seconds is None:
        assert "提交最小间隔" not in start_lines[0], start_lines[0]
    else:
        assert f"，提交最小间隔 {in_seconds}" in start_lines[0], start_lines[0]
    assert result["processed"] == 1


def test_balance_guard_stops_before_any_submit(monkeypatch, tmp_path):
    """余额未知或不足时必须在任何 POST 之前停止整批。"""
    from openpyxl import Workbook
    from types import SimpleNamespace

    wb = Workbook()
    ws = wb.active
    ws.title = "午餐"
    ws.append(["午餐", None, None, None])
    ws.append(["姓名", "门牌号", "电话", "送达时间"])
    ws.append(["张三", "B1", "13800000001", "11:00"])
    path = tmp_path / "闪时送.xlsx"
    wb.save(path)
    wb.close()

    posts = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_captcha(self):
            return b"\x89PNG fake"

        def login(self, code):
            pass

        def get_json(self, path):
            if "queryStoreAddresses" in path:
                return {"success": True,
                        "result": {"records": [{"id": 211053, "name": "一口轻食"}],
                                   "total": 1}}
            if "get-login-user-account" in path:
                # 可用余额 0.5 < 预计 1.9
                return {"success": True,
                        "result": {"totalAmount": 0.5, "freezeAmount": 0.0}}
            raise AssertionError(f"余额不足时不应继续查询：{path}")

        def post_json(self, path, body=None):
            posts.append((path, body))
            raise AssertionError("余额不足绝不能发送下单 POST")

        def close(self):
            pass

    monkeypatch.setattr(sss, "SssApiClient", FakeClient)

    cfg = SimpleNamespace(
        sss_excel_path=str(path), sss_order_source="excel", sss_account="18758187837",
        sss_dry_run=False, sss_preflight=False, sss_store_name="一口轻食",
        sss_common_address="嗯哼", sss_use_fixed_address=True,
        sss_fixed_lnt=1.0, sss_fixed_lat=2.0, sss_fixed_area_code="330110",
        sss_fixed_address_detail="X", sss_product_name="轻食", api_mode=True,
        element_timeout_ms=8000, sss_url="https://example.invalid",
        sss_store_id=None, sss_store_name_cached="", sss_max_workers=4,
        sss_unit_price=1.9, sss_read_timeout_s=20.0, sss_idempotency_field="",
    )

    class Stop:
        def is_set(self):
            return False

    logs = []
    result = sss.run_sss_job(cfg, Stop(), logs.append, password="x",
                             captcha_callback=lambda img: "1234")

    assert result["status"] == "insufficient_balance"
    assert result["stopped"] is True
    assert result["submitted"] == 0
    assert posts == []
    assert any("余额不足" in msg for msg in logs)


def test_list_records_rejects_shell_without_records():
    """服务端对不支持的过滤参数返回无 records 空壳，必须 fail-closed 且报错点名结构。"""
    import pytest

    shell = {"success": True, "code": 200,
             "result": {"total": 0, "size": 10, "current": 1, "pages": 0}}
    with pytest.raises(LookupError, match="缺少 records/list"):
        sss._list_records(shell)


def test_local_filter_keeps_only_wanted_delivery_day():
    task = _task(delivery="2026-09-10 11:00:00")
    same_day = _station_record(task)
    other_day = dict(_station_record(task))
    other_day["expectedDeliveryTime"] = "2026-09-11 11:00:00"

    def fetch(path):
        assert "startTime" not in path and "statusList" not in path
        return {"success": True,
                "result": {"records": [same_day, other_day], "total": 2}}

    result = sss._reconcile_tasks([task], fetch)
    assert result.confirmed == {"t1"}
    assert result.duplicate_count == 0


# ----------------------------------------------------------------------
# 改动前完全未被引用的 expected_delivery_date
# ----------------------------------------------------------------------
def test_expected_delivery_date_follows_the_16h_rollover_rule():
    """16 点后下单顺延次日；午餐与晚餐共用同一条规则（仅时刻不同）。

    云端名单的「识别日期」必须与它一致，否则程序会**拒绝下单**
    （见 ``sss_import.check_target_day``），所以这条边界要钉死。
    """
    import datetime as dt

    from app.sss import expected_delivery_date

    same_day = [0, 9, 15]          # 00:00 ~ 15:59 → 运行日
    next_day = [16, 19, 20, 23]    # 16:00 ~ 23:59 → 次日
    for hour in same_day:
        now = dt.datetime(2026, 9, 15, hour, 30)
        assert expected_delivery_date(now) == dt.date(2026, 9, 15), hour
    for hour in next_day:
        now = dt.datetime(2026, 9, 15, hour, 30)
        assert expected_delivery_date(now) == dt.date(2026, 9, 16), hour


def test_expected_delivery_date_agrees_with_compute_delivery_time():
    """两者必须永远一致 —— 它们算的是同一件事（日期），只是往返形式不同。"""
    import datetime as dt

    from app.sss import compute_delivery_time, expected_delivery_date

    for hour in range(24):
        now = dt.datetime(2026, 9, 15, hour, 0)
        date_part = expected_delivery_date(now).isoformat()
        assert compute_delivery_time(False, now).startswith(date_part)
        assert compute_delivery_time(True, now).startswith(date_part)


def test_expected_delivery_date_rolls_over_month_boundary():
    import datetime as dt

    from app.sss import expected_delivery_date

    assert expected_delivery_date(dt.datetime(2026, 9, 30, 20, 0)) == dt.date(2026, 10, 1)
    assert expected_delivery_date(dt.datetime(2026, 12, 31, 16, 0)) == dt.date(2027, 1, 1)


# ===================== 浏览器表单模式（原 闪时送.py 弹窗流程） =====================
#
# FakePage/FakeLocator 只实现 _ui_create_order 用到的 Playwright 最小表面：
# 候选链定位（css/text/placeholder 的“是否可见”由 visible 集合决定）、点击、
# 填充、按键、遮罩可见性轮询。真实浏览器交互不在离线测试范围（与旧约定一致）。
#
# 平台驳回（2026-10-12 实测）：点了最终确定但弹窗不关，原表单数据不丢，
# 再点一次即成功。FakePage.reject_times 模拟「前 N 次确定都被驳回」。


class FakeLocator:
    def __init__(self, page, key, found, picked=""):
        self._page, self._key, self._found, self.picked = page, key, found, picked

    @property
    def first(self):
        return self

    @property
    def last(self):
        self.picked = "last"
        return self

    def filter(self, has_text=None, has_text_re=None):
        # 生产代码把 has_text_re 编译成 re.Pattern 经 has_text 传入
        # （如“确\s*定”容忍两字按钮空隙）：假实现不改命中判定。
        if isinstance(has_text, re.Pattern):
            has_text = None
        key = has_text if has_text else self._key
        return FakeLocator(self._page, key,
                           self._found and key in self._page.visible, self.picked)

    def count(self):
        return 1 if self._found else 0

    def wait_for(self, state="visible", timeout=None):
        if not self._found:
            raise TimeoutError(f"fake wait timeout: {self._key}")

    def click(self, timeout=None):
        self._page.record("click", self._key, self.picked)
        # 只有**真正点下**才改变页面状态：wait_for 探测不能触发受理。
        self._page.apply_click(self._key, self.picked)

    def fill(self, value, timeout=None):
        self._page.record("fill", self._key, str(value))

    def press(self, key, timeout=None):
        self._page.record("press", self._key, key)

    def type(self, text, timeout=None):
        self._page.record("type", self._key, str(text))

    def element_handle(self, timeout=None):
        return object() if self._found else None


class FakePage:
    def __init__(self, visible, mask_visible=True, *, reject_times=0):
        self.visible = set(visible)
        self.mask_visible = mask_visible
        # 遮罩可见性与 visible 集合保持同步：生产代码用
        # ``.ant-modal-mask:visible`` 计数判定收场，两个表示必须一致。
        if mask_visible:
            self.visible.add(".ant-modal-mask:visible")
        else:
            self.visible.discard(".ant-modal-mask:visible")
        # 前 reject_times 次点「最终确定」都被平台驳回（弹窗不关、无报错框按钮）。
        self.reject_times = reject_times
        self.final_clicks = 0   # 「最终确定」被真正点下的次数
        self.reject_clicks = 0  # 驳回后"点掉报错"的次数（实测里点的是同一个按钮）
        self.actions = []
        self.on_click = None

    def record(self, kind, key, value=""):
        self.actions.append((kind, key, value))
        if kind == "click" and self.on_click:
            self.on_click(key, value)

    def apply_click(self, key, picked):
        """把点击翻译成页面状态变化：最终确定按驳回预算决定收不收场。

        ``picked`` 区分两个按钮：地址确定与"点掉报错"是 ``"last"``，最终确定
        是首命中（""）。因此地址确定不会被算作提交；驳回报错在实测里点到的
        很可能就是同一个按钮，这里按"再点一次确定"处理并单独计数。
        """
        if key not in ("确定", ".ant-modal-footer .ant-btn-primary"):
            return
        if picked != "":
            if self.final_clicks > self.reject_clicks:
                self.reject_clicks += 1
            return
        self.final_clicks += 1
        if self.final_clicks <= self.reject_times:
            return  # 平台驳回：弹窗不关、原表单数据不丢，等下一次点击
        self.mask_visible = False
        self.visible.discard(".ant-modal-mask:visible")

    # ---- _build_locator 的最小表面 ----
    def locator(self, selector):
        if selector == "text=创建订单":
            return FakeLocator(self, "创建订单", "创建订单" in self.visible)
        return FakeLocator(self, selector, selector in self.visible)

    def get_by_text(self, text):
        return FakeLocator(self, text, text in self.visible)

    def get_by_placeholder(self, placeholder):
        return FakeLocator(self, placeholder, placeholder in self.visible)

    def get_by_role(self, role, **kwargs):
        name = str(kwargs.get("name") or role)
        return FakeLocator(self, name, name in self.visible)

    def wait_for_selector(self, selector, state=None, timeout=None):
        if state == "hidden":
            if self.mask_visible:
                raise TimeoutError("fake mask still visible")
            return None
        raise AssertionError(f"unexpected wait_for_selector: {selector} {state}")

    def wait_for_timeout(self, ms):
        # 不真睡（否则整条流程要按 0.5s + 重试间隔累计）；把调用记进 actions，
        # 用例据此断言"填完→首次点击"确实有静默。
        self.actions.append(("wait", "timeout", int(ms)))

    def evaluate(self, script, arg=None):
        self.record("evaluate", "js", "")
        return None


_NAME_INPUT_XPATH = "//*[contains(text(),'名称')]/following::input[1]"


def _ui_visible_set():
    """一次完整表单流程需要可见的全部元素键（按候选链第一个命中项取键）。"""
    return {
        "创建订单", "即时单", "预约单", "自动分单", "一口轻食",
        "常用地址", "嗯哼", "确定",
        ".ant-picker-ok button",
        ".ant-modal-footer .ant-btn-primary",  # 地址确定/最终确定的 css 候选
        ".ant-modal-footer .ant-btn-primary:visible",  # 驳回后重试点击的候选
        ".ant-modal-mask:visible",  # 遮罩可见性轮询
        "请选择日期时间",  # 送达时间输入 placeholder
        "#receiveName", "#receiveMobile", _NAME_INPUT_XPATH, "#doorNum",
    }


def _ui_payload():
    import datetime as dt2

    order = {"row": 3, "name": "张三", "door": "A101", "phone": "13800000001"}
    address = {"lnt": 119.728224, "lat": 30.256632,
               "areaCode": "330110", "addressDetail": "浙江农林大学东湖校区"}
    return sss.build_order_payload(order, False, 211053, address, "轻食",
                                   now=dt2.datetime(2026, 10, 11, 10, 0))


def test_ui_create_order_success_drives_full_form_flow(monkeypatch, tmp_path):
    from app import automation as automation_mod

    monkeypatch.setattr(automation_mod, "user_data_dir", lambda: tmp_path)
    # mask_visible 的收场由 FakePage._apply_click 按点击语义驱动（首点即受理）。
    page = FakePage(_ui_visible_set(), mask_visible=True)

    result = sss._ui_create_order(
        page, _ui_payload(), SSS_LOCATORS, store_name="一口轻食",
        address_label="嗯哼", callback=None)

    assert result == {"success": True}
    clicks = [a for a in page.actions if a[0] == "click"]
    keys = [(k, picked) for _kind, k, picked in clicks]
    # 弹窗流程的关键点击按顺序出现：创建订单 → 预约单 → 门店 → 地址 → 确定(last)
    assert keys.index(("创建订单", "")) < keys.index(("预约单", ""))
    assert keys.index(("预约单", "")) < keys.index(("一口轻食", ""))
    assert keys.index(("一口轻食", "")) < keys.index(("嗯哼", ""))
    assert ("嗯哼", "") in keys
    # 地址弹窗的确定取最后一个匹配（pick="last"），最终确定是普通 pick；
    # 两者首选候选都是 css（has_text_re），命中 key 是 css 字符串。
    assert (".ant-modal-footer .ant-btn-primary", "last") in keys
    assert (".ant-modal-footer .ant-btn-primary", "") in keys
    assert ("确定", "last") not in keys and ("确定", "") not in keys
    fills = {key: value for kind, key, value in page.actions if kind == "fill"}
    assert fills["#receiveName"] == "张三"
    assert fills["#receiveMobile"] == "13800000001"
    assert fills[_NAME_INPUT_XPATH] == "轻食"
    assert fills["#doorNum"] == "A101"
    # 送达时间：全选删除后逐字键入报文里的 expectedDeliveryTime
    typed = [value for kind, key, value in page.actions if kind == "type"]
    assert typed == ["2026-10-11 11:00:00"]
    presses = [value for kind, key, value in page.actions if kind == "press"]
    assert presses == ["ControlOrMeta+a", "Delete"]
    # 收场一次成功：只点了一次最终确定，重试路径完全没被触发
    assert page.final_clicks == 1, keys


def test_ui_create_order_delays_before_the_first_confirm_click(monkeypatch, tmp_path):
    """填完所有字段后必须先静默 _UI_FINAL_CONFIRM_DELAY_S 再点最终确定。"""
    from app import automation as automation_mod

    monkeypatch.setattr(automation_mod, "user_data_dir", lambda: tmp_path)
    page = FakePage(_ui_visible_set(), mask_visible=True)

    sss._ui_create_order(page, _ui_payload(), SSS_LOCATORS,
                         store_name="一口轻食", address_label="嗯哼",
                         callback=None)

    kinds = [(kind, value) for kind, _k, value in page.actions]
    last_fill = max(i for i, (kind, _v) in enumerate(kinds) if kind == "fill")
    first_confirm = next(
        i for i, a in enumerate(page.actions)
        if a == ("click", ".ant-modal-footer .ant-btn-primary", ""))
    assert last_fill < first_confirm, page.actions
    # 中间那次等待必须是 0.5s 静默（用户要求：填完不要立刻点）
    waits = [(i, value) for i, (kind, _k, value) in enumerate(page.actions)
             if kind == "wait"]
    assert waits == [(last_fill + 1, int(sss._UI_FINAL_CONFIRM_DELAY_S * 1000))], waits


def test_ui_create_order_retries_after_platform_rejection(monkeypatch, tmp_path):
    """平台驳回（弹窗不关、原表单不丢）→ 点掉报错后重试，第 2 轮成功。"""
    from app import automation as automation_mod

    monkeypatch.setattr(automation_mod, "user_data_dir", lambda: tmp_path)
    page = FakePage(_ui_visible_set(), mask_visible=True, reject_times=1)
    logs: list[str] = []

    result = sss._ui_create_order(
        page, _ui_payload(), SSS_LOCATORS, store_name="一口轻食",
        address_label="嗯哼", callback=logs.append)

    assert result == {"success": True}
    # 最终确定被点两次：首点被驳回、再点收场（表单数据不重填）
    assert page.final_clicks == 2
    assert any("平台驳回（第 1 次）" in line for line in logs)
    assert any("平台驳回重试生效" in line for line in logs)
    # 重试不是"重新填表"：姓名只填过一次
    fills = [a for a in page.actions if a[:2] == ("fill", "#receiveName")]
    assert len(fills) == 1


def test_ui_create_order_retries_up_to_three_rounds(monkeypatch, tmp_path):
    """连续两次驳回后第 3 轮成功（实测里存在这种单）。"""
    from app import automation as automation_mod

    monkeypatch.setattr(automation_mod, "user_data_dir", lambda: tmp_path)
    page = FakePage(_ui_visible_set(), mask_visible=True, reject_times=2)

    result = sss._ui_create_order(
        page, _ui_payload(), SSS_LOCATORS, store_name="一口轻食",
        address_label="嗯哼", callback=None)

    assert result == {"success": True}
    assert page.final_clicks == 3


def test_ui_create_order_retry_exhausted_is_uncertain(monkeypatch, tmp_path):
    """3 轮都收不了场 → 抛结果未知（交给收尾站内对账），绝不判"失败"。"""
    from app import automation as automation_mod

    monkeypatch.setattr(automation_mod, "user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(sss, "_UI_MODAL_CLOSE_TIMEOUT_S", 0.6)
    monkeypatch.setattr(sss, "_UI_MODAL_POLL_MS", 100)
    monkeypatch.setattr(sss, "_UI_REJECT_SETTLE_WINDOW_S", 0.0)
    page = FakePage(_ui_visible_set(), mask_visible=True, reject_times=99)
    logs: list[str] = []

    with pytest.raises(sss._SubmissionUncertain):
        sss._ui_create_order(page, _ui_payload(), SSS_LOCATORS,
                             store_name="一口轻食", address_label="嗯哼",
                             callback=logs.append)
    assert page.final_clicks == sss._UI_FINAL_CONFIRM_ATTEMPTS
    assert any(f"重试 {sss._UI_FINAL_CONFIRM_ATTEMPTS} 次仍未收场" in line
               for line in logs)


def test_ui_create_order_no_retry_when_the_reject_click_fails(monkeypatch, tmp_path):
    """点不到报错/确定按钮（弹窗被遮挡等）→ 立即停止重试，交给收尾等待判定。"""
    from app import automation as automation_mod

    monkeypatch.setattr(automation_mod, "user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(sss, "_UI_REJECT_SETTLE_WINDOW_S", 0.0)
    page = FakePage(_ui_visible_set(), mask_visible=True)
    page.visible.discard(".ant-modal-footer .ant-btn-primary")  # 脚部按钮整体消失

    with pytest.raises(sss.LocatorError):
        sss._ui_create_order(page, _ui_payload(), SSS_LOCATORS,
                             store_name="一口轻食", address_label="嗯哼",
                             callback=None)


def test_ui_create_order_raises_auth_expired_when_logged_out():
    visible = _ui_visible_set()
    visible.discard("创建订单")
    page = FakePage(visible)
    with pytest.raises(sss._AuthExpired):
        sss._ui_create_order(page, _ui_payload(), SSS_LOCATORS,
                             store_name="一口轻食", address_label="嗯哼",
                             callback=None)


def test_ui_create_order_fails_before_final_confirm_keeps_no_uncertainty(monkeypatch, tmp_path):
    from app import automation as automation_mod

    monkeypatch.setattr(automation_mod, "user_data_dir", lambda: tmp_path)
    visible = _ui_visible_set()
    visible.discard("#receiveName")  # 中途丢一个输入框
    page = FakePage(visible)
    with pytest.raises(sss.LocatorError):
        sss._ui_create_order(page, _ui_payload(), SSS_LOCATORS,
                             store_name="一口轻食", address_label="嗯哼",
                             callback=None)
    # 最终确定从未被点击 → 这单确定没有创建（failure 语义，不是 uncertain）
    assert ("确定", "") not in page.actions


def test_ui_create_order_uncertain_when_modal_never_closes(monkeypatch):
    monkeypatch.setattr(sss, "_UI_MODAL_CLOSE_TIMEOUT_S", 0.6)
    monkeypatch.setattr(sss, "_UI_MODAL_POLL_MS", 100)
    monkeypatch.setattr(sss, "_UI_REJECT_SETTLE_WINDOW_S", 0.0)
    # 每次最终确定都被驳回：遮罩永远不收场
    page = FakePage(_ui_visible_set(), mask_visible=True, reject_times=99)
    with pytest.raises(sss._SubmissionUncertain):
        sss._ui_create_order(page, _ui_payload(), SSS_LOCATORS,
                             store_name="一口轻食", address_label="嗯哼",
                             callback=None)


def test_ui_create_order_uncertain_when_create_button_never_recovers(monkeypatch):
    monkeypatch.setattr(sss, "_UI_MODAL_CLOSE_TIMEOUT_S", 0.6)
    monkeypatch.setattr(sss, "_UI_MODAL_POLL_MS", 100)
    monkeypatch.setattr(sss, "_UI_RECOVER_TIMEOUT_MS", 300)
    page = FakePage(_ui_visible_set(), mask_visible=True)

    def on_click(key, picked):
        if key in ("确定", ".ant-modal-footer .ant-btn-primary") and picked == "":
            page.mask_visible = False       # 遮罩收场
            page.visible.discard("创建订单")  # 但页面再也没恢复
    page.on_click = on_click
    with pytest.raises(sss._SubmissionUncertain):
        sss._ui_create_order(page, _ui_payload(), SSS_LOCATORS,
                             store_name="一口轻食", address_label="嗯哼",
                             callback=None)


def test_store_option_step_substitutes_store_name_from_config():
    default = sss._store_option_step(SSS_LOCATORS, "一口轻食")
    assert default["candidates"][0]["has_text"] == "一口轻食"
    other = sss._store_option_step(SSS_LOCATORS, "东门店")
    assert other["candidates"][0]["has_text"] == "东门店"


def test_store_option_step_falls_back_when_override_hardcodes_default():
    """旧版 sss_locators.json 把门店文字写死成「一口轻食」：换门店名时兜底。"""
    override = {"一口轻食选项": {"candidates": [
        {"css": ".ant-select-dropdown .ant-select-item-option", "has_text": "一口轻食"},
        {"text": "一口轻食"},
    ]}}
    step = sss._store_option_step(override, "东门店")
    assert step["candidates"][0]["has_text"] == "东门店"
    assert step["candidates"][1]["text"] == "东门店"


def test_sss_locators_store_option_uses_store_token():
    assert SSS_LOCATORS["一口轻食选项"]["candidates"][0]["has_text"] == "{store}"


def test_serial_submission_runs_in_the_calling_thread():
    """单路提交必须在**调用线程**内联执行（回归：2026-10-11 线上事故）。

    浏览器表单模式的 ``submit`` 驱动 Playwright 同步 API，而 Playwright 把
    greenlet 绑定在创建它的线程上：把 submit 丢进 ThreadPoolExecutor 工作线程
    会每单立刻抛 ``Cannot switch to a different thread``（102 单全部瞬间失败、
    每单平均 0.0 秒，表单一次都没打开）。max_workers=1 时不存在流水线提速，
    内联执行语义等价且规避线程约束。
    """
    caller = threading.current_thread()
    seen = []
    tasks = [{"identifier": f"t{i}", "payload": {"i": i}} for i in range(3)]

    def submit(payload):
        seen.append(threading.current_thread())
        return {"success": True}

    sss._submit_tasks_concurrent(tasks, submit, threading.Event(), None, 1,
                                 min_interval_s=0)
    assert seen and all(thread is caller for thread in seen), (
        "单路提交必须留在调用线程（Playwright 同步 API 线程约束）")


def test_serial_submission_failure_detail_is_emitted_immediately():
    """失败/未知明细要立刻逐单透出：收尾对账失败时它们是唯一的现场证据。"""
    logs: list[str] = []
    tasks = [{"identifier": "t0", "payload": {"i": 0}}]

    def submit(payload):
        raise RuntimeError("boom-细节")

    sss._submit_tasks_concurrent(tasks, submit, threading.Event(), logs.append, 1)
    assert any("提交失败：t0" in m and "boom-细节" in m for m in logs), logs

    def submit_uncertain(payload):
        raise sss._SubmissionUncertain("弹窗没关上")

    logs.clear()
    sss._submit_tasks_concurrent(tasks, submit_uncertain, threading.Event(),
                                 logs.append, 1)
    assert any("提交结果未知：t0" in m and "弹窗没关上" in m for m in logs), logs


# ---------------------------------------------------------------------------
# D07：午餐 / 晚餐任务身份唯一（同行同名不得互相顶替）
# ---------------------------------------------------------------------------

def _双表同行同名工作簿(tmp_path):
    """临时工作簿：午餐/晚餐第 3 行都是同一客户（同名、同门牌、同电话）。

    这正是 D07 的触发条件——两表行号与姓名完全相同，但送达时间不同
    （午餐 11:00、晚餐 17:00），是两个互不相同的任务。
    """
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)
    for sheet_name, delivery in (("午餐", "11:00"), ("晚餐", "17:00")):
        sheet = workbook.create_sheet(sheet_name)
        sheet.cell(1, 1, sheet_name)          # 第 1 行标题
        sheet.cell(2, 1, "姓名")               # 第 2 行表头
        sheet.cell(3, 1, "张三")               # 第 3 行：两表同名同门牌同电话
        sheet.cell(3, 2, "B1")
        sheet.cell(3, 3, "13800000001")
        sheet.cell(3, 4, delivery)
    path = tmp_path / "闪时送.xlsx"
    workbook.save(path)
    workbook.close()
    return path


def _assert_确认数与部分完成不矛盾(result, logs):
    """D07 的对外症状：确认数少于处理数，却报成「不是部分完成」。

    任务身份碰撞会让确认集合 ``set[str]`` 折叠掉一单，于是 ``created`` 少算
    而 ``missing`` 仍为空，得到一个自相矛盾的结果。这条不变量必须先于任何
    具体数值断言。
    """
    created, processed = result["created"], result["processed"]
    assert not (created < processed and result["partial"] is False), (
        f"确认 {created}/{processed} 却不是部分完成：任务身份被合并或状态口径矛盾；"
        f"日志：{logs}")


def test_任务标识_午餐晚餐同行同名也必须互不相同():
    """同一行号同一姓名出现在两张表时，任务身份必须仍然唯一（D07）。

    标识只有「第 N 行 姓名」时，午餐第 3 行的张三与晚餐第 3 行的张三是同一个
    字符串：确认集合（``set[str]``）与错误归集字典（``dict[str, str]``）都会把
    两个任务折叠成一个，created 少算且失败原因互相串台。
    """
    order = {"row": 3, "name": "张三", "door": "B1", "phone": "13800000001"}
    address = {"lnt": 1.0, "lat": 2.0, "areaCode": "330110",
               "addressDetail": "浙江农林大学东湖校区"}

    tasks = sss._collect_tasks({"午餐": [dict(order)], "晚餐": [dict(order)]},
                               7, address, "轻食", account="18758187837")

    assert [task["sheet"] for task in tasks] == ["午餐", "晚餐"]
    identifiers = [task["identifier"] for task in tasks]
    assert identifiers == ["午餐 第 3 行 张三", "晚餐 第 3 行 张三"], identifiers
    assert len(set(identifiers)) == 2, f"两个不同任务拿到了同一个标识：{identifiers}"


def test_端到端_午餐晚餐同行同名时确认数必须等于处理数(monkeypatch, tmp_path):
    """端到端（D07）：两表第 3 行同名同门牌，两次提交必须都算成一单。

    使用接受下单的站内替身：POST 恰好 2 次、站内 2 条记录，结果必须是
    processed=2 / created=2 / partial=False，收尾播报「确认 2/2」。
    """
    from tests.sss_test_support import _FakeClient, _config

    _双表同行同名工作簿(tmp_path)
    config = _config(tmp_path, sss_order_source="excel", sss_max_workers=1,
                     sss_submit_min_interval_s=0.0)
    client = _FakeClient()
    monkeypatch.setattr(sss, "SssApiClient", lambda *a, **k: client)
    monkeypatch.setattr(sss, "_API_SUBMIT_SETTLE_S", 0.0)

    logs: list[str] = []
    result = sss.run_sss_job(config, threading.Event(), progress_callback=logs.append,
                             password="pw", captcha_callback=lambda _img: "1234")

    assert result["processed"] == 2, logs
    assert len(client.posts) == 2, [payload.get("expectedDeliveryTime")
                                    for payload in client.posts]
    assert len(client.list_records) == 2, client.list_records
    assert result["created"] == 2, logs
    assert result["partial"] is False, logs
    assert result["reconciled"] is True, logs
    assert any("确认 2/2" in line for line in logs), logs
    _assert_确认数与部分完成不矛盾(result, logs)


def test_端到端_确实少一单时必须如实报部分完成(monkeypatch, tmp_path):
    """反向锁（D07）：created < processed 只能是「真的少了一单」，且必须报 partial。

    让晚餐那一单被站内业务拒绝（「商品已下架」＝确定未建单）：此时确认 1/2 是
    真实缺失，partial 必须为 True。与上一条用例合起来锁住本缺陷的对外症状——
    既不允许「少了却不承认」，也不允许把真实缺失吞成完成。
    """
    from tests.sss_test_support import _FakeClient, _config

    class _晚餐被拒客户端(_FakeClient):
        """接受午餐提交，明确拒绝晚餐（业务拒绝 → 确定未建单，不自动重试）。"""

        def post_json(self, path, payload):
            if str(payload.get("expectedDeliveryTime") or "").endswith("17:00:00"):
                self.posts.append(payload)
                return {"success": False, "message": "商品已下架"}
            return super().post_json(path, payload)

    _双表同行同名工作簿(tmp_path)
    config = _config(tmp_path, sss_order_source="excel", sss_max_workers=1,
                     sss_submit_min_interval_s=0.0)
    client = _晚餐被拒客户端()
    monkeypatch.setattr(sss, "SssApiClient", lambda *a, **k: client)
    monkeypatch.setattr(sss, "_API_SUBMIT_SETTLE_S", 0.0)

    logs: list[str] = []
    result = sss.run_sss_job(config, threading.Event(), progress_callback=logs.append,
                             password="pw", captcha_callback=lambda _img: "1234")

    assert len(client.posts) == 2, [payload.get("expectedDeliveryTime")
                                    for payload in client.posts]
    assert len(client.list_records) == 1, client.list_records
    assert result["processed"] == 2, logs
    assert result["created"] == 1, logs
    assert result["partial"] is True, logs
    _assert_确认数与部分完成不矛盾(result, logs)


# ---------------------------------------------------------------------------
# D09 / D10：跨轮对账的「已消费记录」账目与重登补发范围
# ---------------------------------------------------------------------------

def test_重登后不自动补发已发送但结果未知的任务(monkeypatch):
    """D10 回归：重登后的补发名单必须扣掉「本轮已发出、结果不可确认」的任务。

    张三的第一次 POST 已在站内建单，但客户端读响应超时（结果未知）；李四的
    第一次 POST 因登录态失效被拒（服务端未建单）。重登后列表仍读不到张三的
    订单（可见性延迟），旧代码据此把张三也当成「缺失」再补发一次——同一次
    运行里重复下单。期望：张三只提交 1 次，李四走完恢复流程；站内合计 2 单、
    没有重复；张三一直未被确认时必须如实留在 missing 里，不能假装完成。
    """
    from app.api_client import ApiError, SssTransportError

    zhangsan = _full_task("午餐 第 3 行 张三", name="张三", door="A101")
    lisi = _full_task("午餐 第 4 行 李四", name="李四", door="A202")

    class _延迟可见客户端:
        """站内替身：订单能建单，但李四补发成功之前列表一直读不到已建订单。

        ``site_orders`` 是站内真实订单（最终计数用它），``readable`` 是列表接口
        当前能读到的订单——「读不到」不等于「没建单」，这正是 D10 的判定条件。
        """

        def __init__(self):
            self.site_orders: list[dict] = []
            self.readable: list[dict] = []
            self.posts: list[dict] = []

        def _record(self, payload):
            address = payload.get("receiveAddress") or {}
            return {
                "id": f"NEW-{len(self.site_orders) + 1:03d}",
                "recipientName": payload.get("receiveName", ""),
                "recipientPhone": [payload.get("receivePhone", "")],
                "recipientAddress": (f"{address.get('addressDetail', '')}"
                                     f"{address.get('doorNum', '')}"),
                "expectedDeliveryTime": payload.get("expectedDeliveryTime", ""),
                "status": 2,
            }

        # ---- SssApiClient 接口 ----
        def fetch_captcha(self):
            return b"png"

        def login(self, _code):
            return {"success": True}

        def fork(self):
            return self

        def close(self):
            pass

        def get_json(self, path):
            if "list" in path:
                records = [dict(record) for record in self.readable]
                return {"success": True,
                        "result": {"records": records, "total": len(records)}}
            if "store" in path:
                return {"success": True, "result": [{"name": "一口轻食", "id": 7}]}
            if "account" in path:
                return {"success": True, "result": {"totalAmount": 1000.0}}
            return {"success": True, "result": []}

        def post_json(self, _path, payload):
            self.posts.append(payload)
            if len(self.posts) == 1:
                # 张三：服务端已建单，但客户端读响应超时（结果未知）
                self.site_orders.append(self._record(payload))
                raise SssTransportError("读取超时")
            if len(self.posts) == 2:
                # 李四：与真机一致的登录失效（HTTP 200 + code=10000），未建单
                raise ApiError("闪时送登录态已失效（code=10000，token失效，"
                               "请重新登陆），请重新登录")
            # 重登后的补发：这张订单列表读得到，张三那张仍然读不到
            record = self._record(payload)
            self.site_orders.append(record)
            self.readable.append(record)
            return {"success": True}

    client = _延迟可见客户端()
    monkeypatch.setattr(sss, "_API_SUBMIT_SETTLE_S", 0.0)
    monkeypatch.setattr(sss, "_RECONCILE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(sss, "_PREFILTER_ZERO_RETRY_DELAY_S", 0)

    final, reconciled = sss._run_reconciled_submission(
        [zhangsan, lisi],
        lambda: sss._make_api_submitter(client),
        client.get_json,
        threading.Event(),
        None,
        None,
        max_workers=1,
        relogin=lambda: client.login("1234"))

    assert reconciled is True
    assert final is not None
    assert [payload["receiveName"] for payload in client.posts] == ["张三", "李四", "李四"], (
        "结果未知的张三不得在重登后被自动补发；李四（确定未建单）走恢复流程")
    assert final.duplicate_count == 0, final
    assert final.confirmed == {"午餐 第 4 行 李四"}, final
    assert [task["identifier"] for task in final.missing] == ["午餐 第 3 行 张三"], (
        "未被确认的未知单必须如实留在 missing 里")
    assert sorted(record["recipientName"] for record in client.site_orders) == ["张三", "李四"], (
        "站内合计 2 单：张三 1 单、李四 1 单，没有重复")


def test_端到端_重登不会自动重发结果未知的已受理订单(monkeypatch, tmp_path):
    """D10 端到端：站内列表看不见已受理订单时，重登补发不得再发一次。

    与上一条同一替身，走完整 ``run_sss_job``：POST 序列必须是
    张三 → 李四 → 李四，站内合计 2 单；张三一直没被确认，所以结果只能是
    created=1 / partial=True（而不是被误报成全部完成）。
    """
    from tests.sss_test_support import _FakeClient, _config
    from app.api_client import ApiError, SssTransportError

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "午餐"
    sheet.cell(2, 1, "姓名")
    sheet.cell(3, 1, "张三")
    sheet.cell(3, 2, "A101")
    sheet.cell(3, 3, "13800000001")
    sheet.cell(4, 1, "李四")
    sheet.cell(4, 2, "A202")
    sheet.cell(4, 3, "13800000002")
    excel_path = tmp_path / "闪时送.xlsx"
    workbook.save(excel_path)
    workbook.close()

    class _可见性延迟客户端(_FakeClient):
        """复用 _FakeClient 的登录/门店/余额，只替换列表与下单行为。"""

        def __init__(self):
            super().__init__()
            self.site_orders = []
            self.readable = []

        def get_json(self, path):
            if "list" not in path:
                return super().get_json(path)
            records = [dict(record) for record in self.readable]
            return {"success": True, "result": {"records": records,
                                               "total": len(records)}}

        def post_json(self, _path, payload):
            self.posts.append(payload)
            address = payload.get("receiveAddress") or {}
            record = {
                "id": f"NEW-{len(self.site_orders) + 1:03d}",
                "recipientName": payload.get("receiveName", ""),
                "recipientPhone": [payload.get("receivePhone", "")],
                "recipientAddress": (f"{address.get('addressDetail', '')}"
                                     f"{address.get('doorNum', '')}"),
                "expectedDeliveryTime": payload.get("expectedDeliveryTime", ""),
                "status": 2,
            }
            if len(self.posts) == 1:
                self.site_orders.append(record)          # 已建单，但读响应超时
                raise SssTransportError("读取超时")
            if len(self.posts) == 2:
                raise ApiError("闪时送登录态已失效（code=10000，token失效，"
                               "请重新登陆），请重新登录")
            self.site_orders.append(record)
            self.readable.append(record)
            return {"success": True}

    config = _config(tmp_path, sss_order_source="excel", sss_max_workers=1,
                     sss_submit_min_interval_s=0.0)
    client = _可见性延迟客户端()
    monkeypatch.setattr(sss, "SssApiClient", lambda *a, **k: client)
    monkeypatch.setattr(sss, "_API_SUBMIT_SETTLE_S", 0.0)
    monkeypatch.setattr(sss, "_RECONCILE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(sss, "_PREFILTER_ZERO_RETRY_DELAY_S", 0)

    logs: list[str] = []
    result = sss.run_sss_job(config, threading.Event(), progress_callback=logs.append,
                             password="pw", captcha_callback=lambda _img: "1234")

    assert [payload["receiveName"] for payload in client.posts] == ["张三", "李四", "李四"], logs
    assert sorted(record["recipientName"] for record in client.site_orders) == ["张三", "李四"], logs
    assert result["processed"] == 2, logs
    assert result["created"] == 1, logs
    assert result["partial"] is True, logs
    assert result["reconciled"] is True, logs
    assert any("不会自动补发" in line for line in logs), logs
    _assert_确认数与部分完成不矛盾(result, logs)


def test_端到端_站内只有一条记录时只能确认一单并如实报部分完成(monkeypatch, tmp_path):
    """D09 端到端：两条同名同址同电话的任务 + 站内 1 条旧记录 + 明确拒绝。

    旧代码返回 processed=2 / created=2 / partial=False（把一张旧记录当成两条
    任务的完成证据）；期望 created=1 且 partial=True，站内订单数仍为 1。
    """
    from openpyxl import Workbook

    from tests.sss_test_support import _FakeClient, _config

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "午餐"
    sheet.cell(2, 1, "姓名")
    for row in (3, 4):                     # 同一客户两行：指纹相同、标识不同
        sheet.cell(row, 1, "张三")
        sheet.cell(row, 2, "B2")
        sheet.cell(row, 3, "13900000001")
    excel_path = tmp_path / "闪时送.xlsx"
    workbook.save(excel_path)
    workbook.close()

    config = _config(tmp_path, sss_order_source="excel", sss_max_workers=1,
                     sss_submit_min_interval_s=0.0)
    orders = sss.load_sss_orders(excel_path)
    sss._validate_sss_orders(orders)
    tasks = sss._collect_tasks(orders, 7, sss._fixed_address_from_config(config),
                               config.sss_product_name,
                               account=config.sss_account, now=dt.datetime.now())
    assert [task["identifier"] for task in tasks] == ["午餐 第 3 行 张三", "午餐 第 4 行 张三"]
    payload = tasks[0]["payload"]
    address = payload["receiveAddress"]
    station_record = {
        "recipientName": payload["receiveName"],
        "recipientPhone": [payload["receivePhone"]],
        "recipientAddress": f"{address['addressDetail']}{address['doorNum']}",
        "expectedDeliveryTime": payload["expectedDeliveryTime"],
        "status": 2,
    }

    class _站内仅一单客户端(_FakeClient):
        """站内已有一条匹配旧记录；下单被明确拒绝（商品已下架，确定未建单）。"""

        def __init__(self):
            super().__init__(order_fail=True)
            self.list_records = [dict(station_record)]

        def get_json(self, path):
            if "list" in path:
                # 送达时间按查询时刻刷新：夹具在测试开头算过一遍，run_sss_job 内部
                # 又算一遍，跨过 16:00/20:00 餐次边界时两边会得到不同的送达日。
                for record in self.list_records:
                    record["expectedDeliveryTime"] = sss.compute_delivery_time(
                        False, dt.datetime.now())
            return super().get_json(path)

    client = _站内仅一单客户端()
    monkeypatch.setattr(sss, "SssApiClient", lambda *a, **k: client)
    monkeypatch.setattr(sss, "_API_SUBMIT_SETTLE_S", 0.0)
    monkeypatch.setattr(sss, "_RECONCILE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(sss, "_PREFILTER_ZERO_RETRY_DELAY_S", 0)

    logs: list[str] = []
    result = sss.run_sss_job(config, threading.Event(), progress_callback=logs.append,
                             password="pw", captcha_callback=lambda _img: "1234")

    assert len(client.posts) == 1, logs
    assert len(client.list_records) == 1, client.list_records
    assert result["processed"] == 2, logs
    assert result["created"] == 1, logs
    assert result["partial"] is True, logs
    _assert_确认数与部分完成不矛盾(result, logs)
