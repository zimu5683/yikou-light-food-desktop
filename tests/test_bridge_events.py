"""桥接事件协议：保留窗口、cursor 重放、关键事件与交互超时。"""
from __future__ import annotations

import threading
import time

from app.bridge import Bridge


class _AliveWorker:
    def is_alive(self) -> bool:
        return True


def test_event_replay_window_keeps_critical_events(tmp_path, monkeypatch):
    monkeypatch.setattr("app.bridge.EVENT_HISTORY_LIMIT", 10)
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    for index in range(50):
        bridge.log(f"log {index}")
    bridge._emit_event("task:done", {"message": "done", "stopped": False,
                                     "partial": False, "result": {}})

    first = bridge.drain_events(0)
    events = first["events"]
    assert first["producer_id"]
    assert events[0]["event"] == "events:dropped"
    assert events[0]["event_type"] == "events:dropped"
    assert all("event_id" in event and "sequence" in event
               and "created_at" in event and "timestamp" in event
               for event in events)
    assert any(event["event"] == "task:done" for event in events)
    assert any("丢弃" in event["payload"]["message"]
               for event in events if event["event"] == "events:dropped")

    # 同一 cursor 重复拉取会返回相同 event_id；应用层按 event_id 去重即可。
    second = bridge.drain_events(0)["events"]
    assert [event["event_id"] for event in second] == [event["event_id"] for event in events]

    # 未 ACK 前仍可从 cursor 0 重放（模拟前端断线/刷新）。
    replay = bridge.drain_events(0)["events"]
    assert any(event["event"] == "task:done" for event in replay)
    # ACK 后已确认事件被删除；推进 cursor 不再返回。
    assert bridge.drain_events(0, first["latest_sequence"])["events"] == []
    assert len(bridge._event_log) == 0


def test_decision_timeout_unblocks_worker(tmp_path):
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._interaction_timeout_s = 0.05
    result: list[str] = []

    worker = threading.Thread(
        target=lambda: result.append(
            bridge._request_decision("sss_retry", "t", "m", [])),
        daemon=True,
    )
    worker.start()
    worker.join(1.0)

    assert not worker.is_alive()
    assert result == ["stop"]
    assert bridge._decisions == {}


def test_address_input_event_and_resolve(tmp_path):
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._interaction_timeout_s = 2.0
    result: list[dict[str, str]] = []
    items = [{"raw_address": "教学楼-南门", "order_numbers": ["W16", "W15"],
              "campus": "东湖农林", "confidence": "unknown", "reason": "未识别",
              "suggested_point": ""}]

    worker = threading.Thread(
        target=lambda: result.append(bridge._pending_address_input(items)),
        daemon=True,
    )
    worker.start()
    for _ in range(100):
        event = next((e for e in list(bridge._event_log)
                      if e.get("event") == "address_input"), None)
        if event:
            break
        time.sleep(0.01)
    assert event, "应发出 address_input 事件"
    request_id = event["payload"]["id"]
    assert event["payload"]["items"][0]["order_numbers"] == ["W16", "W15"]
    assert bridge.resolve_address_input(request_id, {"教学楼-南门": "教5"}) == {"ok": True}
    worker.join(1.0)

    assert not worker.is_alive()
    assert result == [{"教学楼-南门": "教5"}]
    assert bridge._decisions == {}


def test_address_input_timeout_returns_empty_mapping(tmp_path):
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._interaction_timeout_s = 0.05
    result: list[dict[str, str]] = []

    worker = threading.Thread(
        target=lambda: result.append(bridge._pending_address_input([
            {"raw_address": "x", "order_numbers": ["W1"], "campus": "未知",
             "confidence": "unknown", "reason": "", "suggested_point": ""}])),
        daemon=True,
    )
    worker.start()
    worker.join(1.0)

    assert not worker.is_alive()
    assert result == [{}]
    assert bridge._decisions == {}


def test_stop_task_wakes_captcha_waiter(tmp_path):
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._worker = _AliveWorker()  # type: ignore[assignment]
    bridge._interaction_timeout_s = 5.0
    errors: list[BaseException] = []

    def request_captcha() -> None:
        try:
            bridge._sss_captcha(b"\x89PNG\r\n")
        except BaseException as exc:  # noqa: BLE001 - 测试取消路径
            errors.append(exc)

    worker = threading.Thread(target=request_captcha, daemon=True)
    worker.start()
    time.sleep(0.05)
    result = bridge.stop_task()
    worker.join(1.0)

    assert result == {"ok": True}
    assert not worker.is_alive()
    assert errors and "验证码" in str(errors[0])
    assert bridge._decisions == {}
    assert bridge._stop_event.is_set()


def test_request_close_timeout_leaves_no_pending_interaction(tmp_path):
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._worker = _AliveWorker()  # type: ignore[assignment]
    bridge._interaction_timeout_s = 0.05

    result = bridge.request_close()

    assert result == {"action": "kept"}
    assert bridge._decisions == {}


def test_ack_sequence_prunes_acknowledged_events(tmp_path, monkeypatch):
    monkeypatch.setattr("app.bridge.EVENT_ACK_RETAIN", 1)
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    for index in range(5):
        bridge.log(f"log {index}")

    result = bridge.drain_events(0, ack_sequence=3)

    # ACK 只删除已确认的 1..3；未确认的 4..5 必须保留。
    assert [event["sequence"] for event in bridge._event_log] == [4, 5]
    assert [event["sequence"] for event in result["events"]] == [4, 5]
    assert result["acked_sequence"] == 3


def test_middle_sequence_gap_emits_dropped_notice(tmp_path, monkeypatch):
    monkeypatch.setattr("app.bridge.EVENT_HISTORY_LIMIT", 10)
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._emit_event("task:done", {"message": "done", "stopped": False,
                                     "partial": False, "result": {}})
    for index in range(20):
        bridge.log(f"log {index}")

    events = bridge.drain_events(0)["events"]
    sequences = [event["sequence"] for event in events]
    dropped = next(event for event in events if event["event"] == "events:dropped")

    assert sequences[0] == 1  # 最前面的关键事件仍在
    assert dropped["payload"]["first_sequence"] == 2
    assert dropped["payload"]["last_sequence"] == 12
    assert dropped["payload"]["dropped_count"] == 11
    assert any(event["event"] == "task:done" for event in events)


def test_critical_events_have_explicit_cap_notice(tmp_path, monkeypatch):
    monkeypatch.setattr("app.bridge.CRITICAL_EVENT_LIMIT", 2)
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    for index in range(4):
        bridge._emit_event("task:error", {"message": str(index)})

    events = bridge.drain_events(0)["events"]
    dropped = next(event for event in events if event["event"] == "events:dropped")

    assert dropped["payload"]["critical_dropped_count"] == 2
    assert bridge._critical_dropped_count == 2
    assert sum(1 for event in bridge._event_log if not event["droppable"]) == 2


def test_ack_from_other_producer_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr("app.bridge.EVENT_ACK_RETAIN", 1)
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    for index in range(3):
        bridge.log(f"log {index}")

    bridge.drain_events(0, ack_sequence=999, producer_id="stale-producer")
    assert bridge._event_ack_sequence == 0
    assert [event["sequence"] for event in bridge._event_log] == [1, 2, 3]

    bridge.drain_events(0, ack_sequence=2, producer_id=bridge._event_producer_id)
    assert [event["sequence"] for event in bridge._event_log] == [3]


def test_dropped_ranges_merge_even_when_critical_pruned_after_logs(tmp_path, monkeypatch):
    monkeypatch.setattr("app.bridge.EVENT_HISTORY_LIMIT", 3)
    monkeypatch.setattr("app.bridge.CRITICAL_EVENT_LIMIT", 1)
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._emit_event("task:done", {"message": "1", "stopped": False,
                                     "partial": False, "result": {}})
    for index in range(3):
        bridge.log(f"log {index}")
    bridge._emit_event("task:error", {"message": "2"})

    events = bridge.drain_events(0)["events"]
    dropped = next(event for event in events if event["event"] == "events:dropped")
    assert dropped["payload"]["first_sequence"] == 1
    assert dropped["payload"]["last_sequence"] == 3
    assert dropped["payload"]["critical_dropped_count"] == 1


# ----------------------------------------------------------------------
# 闪时送云端当天名单：只读取 + 留档，不下单
# ----------------------------------------------------------------------


def test_sss_day_orders_rejects_while_worker_running(tmp_path):
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._worker = _AliveWorker()
    result = bridge.sss_day_orders()
    assert result["ok"] is False
    assert "正在运行" in result["reason"]


def test_sss_day_orders_reports_import_refusal(tmp_path, monkeypatch):
    from app.sss_import import ImportRefused

    def refuse(config, **kwargs):
        raise ImportRefused("读取云端表「东湖中餐」失败：未授权")

    monkeypatch.setattr("app.bridge.prepare_day_orders", refuse)
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    result = bridge.sss_day_orders()
    assert result["ok"] is False
    assert "未授权" in result["reason"]
    logs = [event["payload"]["msg"] for event in bridge.drain_events(0)["events"]
            if event["event"] == "log"]
    assert any("读取云端当天名单失败" in line for line in logs)


def test_sss_day_orders_returns_counts_and_logs(tmp_path, monkeypatch):
    import datetime as dt

    from app.sss_import import DayOrders, MealImport

    meals = [
        MealImport(meal="午餐", table="东湖中餐", date_text="9.16 周三",
                   marked_total=45, skipped_address=12,
                   orders=[{"row": 3, "name": "张三", "door": "b2",
                            "phone": "13800000001"}]),
        MealImport(meal="晚餐", table="东湖晚餐", skipped=True,
                   skip_reason="云端表「东湖晚餐」没有 9.16 这一列，当天不送这一餐"),
    ]
    day = DayOrders(target_date=dt.date(2026, 9, 16), meals=meals,
                    orders_by_sheet={"午餐": meals[0].orders, "晚餐": []})

    monkeypatch.setattr("app.bridge.prepare_day_orders",
                        lambda config, **kwargs: day)
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    result = bridge.sss_day_orders()
    assert result["ok"] is True
    assert result["target_date"] == "2026-09-16"
    assert result["date_text"] == "9.16 周三"
    assert result["total"] == 1
    assert result["meals"]["午餐"] == {
        "table": "东湖中餐", "marked": 45, "skipped_address": 12, "orders": 1,
        "date_text": "9.16 周三", "skipped": False, "reason": "", "warnings": [],
    }
    assert result["meals"]["晚餐"]["skipped"] is True
    logs = [event["payload"]["msg"] for event in bridge.drain_events(0)["events"]
            if event["event"] == "log"]
    assert any("午餐 1 人" in line and "大西/小" in line for line in logs)
    assert any("晚餐不下单" in line for line in logs)


def test_sss_config_defaults_to_wps_source_and_roundtrips(tmp_path):
    from app.config import AppConfig

    path = tmp_path / "config.json"
    config = AppConfig(config_path=str(path))
    assert config.sss_order_source == "wps"
    config.sss_order_source = "excel"
    config.save()
    assert AppConfig.load(path).sss_order_source == "excel"
    # 非法值一律回落到云端模式
    assert AppConfig(sss_order_source="weird").sss_order_source == "wps"
