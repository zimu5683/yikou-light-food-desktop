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
