"""统一操作互斥：同一时刻只允许一个危险操作，冲突立即返回不排队。

为什么重要：订单任务、闪时送下单、云文档上传/预览/授权/恢复、更新安装都要读写
同一批本地状态（Excel、账本、未决日志）和远端数据。原本只有 `_worker_lock`
保护任务线程，于是"任务在跑的时候点上传"这类组合没有任何拦截。
"""
from __future__ import annotations

import pytest

from app.bridge import Bridge
from app.operations import MODES, OperationCoordinator, mode_title


def test_reserve_conflict_and_release():
    coordinator = OperationCoordinator()
    first = coordinator.try_reserve("order", summary={"title": "订单处理"},
                                    next_action="稍后重试")
    assert first.granted and first.operation is not None

    second = coordinator.try_reserve("wps_upload")
    assert second.granted is False
    conflict = second.conflict or {}
    assert conflict["code"] == "operation_conflict"
    assert "订单处理" in conflict["message"]
    assert "云文档上传" in conflict["message"], "要说清被拒的是哪次操作"
    assert conflict["conflicting_operation"]["mode"] == "order"

    coordinator.finish(first.operation, status="success")
    third = coordinator.try_reserve("wps_upload")
    assert third.granted is True
    coordinator.finish(third.operation, status="success")


def test_finish_is_idempotent_and_status_is_readable():
    coordinator = OperationCoordinator()
    reservation = coordinator.try_reserve("sss", summary={"title": "闪时送下单"})
    operation = reservation.operation
    coordinator.update(operation, phase="submitting", summary={"processed": 5})
    assert coordinator.status()["operation"]["phase"] == "submitting"

    coordinator.finish(operation, status="partial", reason="2 单未完成",
                       next_action="start_sss_review")
    coordinator.finish(operation, status="success")   # 重复结束不应改变结论

    status = coordinator.status(operation.operation_id)
    assert status["ok"] is True and status["active"] is False
    assert status["operation"]["status"] == "partial"
    assert status["operation"]["next_action"] == "start_sss_review"
    assert status["operation"]["summary"]["processed"] == 5


@pytest.mark.parametrize("status_name", ["preview_ready", "preflight_ok", "dry_run"])
def test_special_completion_statuses_are_terminal(status_name):
    coordinator = OperationCoordinator()
    reservation = coordinator.try_reserve("wps_preview")

    coordinator.finish(reservation.operation, status=status_name)

    status = coordinator.status()
    assert status["active"] is False
    assert status["last"]["status"] == status_name
    assert status["last"]["active"] is False


def test_unknown_operation_id_is_reported_not_guessed():
    coordinator = OperationCoordinator()
    coordinator.try_reserve("order")
    status = coordinator.status("op-does-not-exist")
    assert status["ok"] is False and status["reason_code"] == "operation_not_found"


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        OperationCoordinator().try_reserve("no-such-mode")


def test_mode_titles_cover_every_mode():
    for mode in MODES:
        assert mode_title(mode) and mode_title(mode) != mode


def _bridge(tmp_path) -> Bridge:
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._config.wps_enabled = True
    bridge._config.excel_path = tmp_path / "排单.xlsx"
    bridge._config.excel_path.write_bytes(b"x")
    return bridge


def test_task_occupies_slot_until_it_finishes(tmp_path):
    """任务启动后占住槽位，直到任务结束才释放。"""
    bridge = _bridge(tmp_path)
    captured: dict = {}

    def fake_launch(mode, config, count, password):
        captured["operation"] = bridge._worker_operation
        return True

    bridge._launch, original = fake_launch, bridge._launch
    try:
        assert bridge.start_order({
            "url": "https://m.icall.me/admin/#/login", "phone": "13800000000",
            "password": "pw", "excel": str(tmp_path / "排单.xlsx"),
            "date": "", "count": "", "remember": False}) == {"ok": True}
    finally:
        bridge._launch = original

    assert captured["operation"] is not None, "启动任务必须占住操作槽位"
    active = bridge.operation_status()
    assert active["active"] is True
    assert active["operation"]["mode"] == "order"
    assert active["operation"]["title"] == "订单处理"

    bridge._operations.finish(captured["operation"], status="success")
    assert bridge.operation_status()["active"] is False, "任务结束后必须释放"


def test_cloud_entries_conflict_with_a_running_task(tmp_path):
    """任务在跑时，云同步预览/上传/恢复与闪时送只读核对都必须被拒绝。"""
    bridge = _bridge(tmp_path)
    operation, _conflict = bridge._reserve("order", title="订单处理")
    try:
        preview = bridge.wps_preview()
        assert preview["ok"] is False and preview["code"] == "operation_conflict"
        assert preview["next_action"]
        assert preview["execution_summary"]["proven_no_write"] is True

        upload = bridge.wps_upload("pv-anything")
        assert upload["ok"] is False and upload["code"] == "operation_conflict"

        review = bridge.start_sss_review()
        assert review["ok"] is False and review["code"] == "operation_conflict"
        assert review["records"] == []

        resolve = bridge.wps_recovery_resolve({"operation_id": "wps-x"})
        assert resolve["ok"] is False and resolve["code"] == "operation_conflict"
        assert resolve["cloud_write"] is False
    finally:
        bridge._operations.finish(operation, status="success")

    # 释放后可以重新占位（证明冲突不是永久锁死）
    fresh, conflict = bridge._reserve("wps_preview", title="云文档预览")
    assert conflict is None
    bridge._operations.finish(fresh, status="success")


def test_sss_task_conflicts_with_the_wps_upload(tmp_path):
    """反向组合：闪时送任务在跑时不能上传云文档。"""
    bridge = _bridge(tmp_path)
    operation, _conflict = bridge._reserve("sss", title="闪时送下单")
    try:
        upload = bridge.wps_upload("pv-x")
        assert upload["ok"] is False and upload["code"] == "operation_conflict"
        assert "闪时送下单" in upload["reason"]
    finally:
        bridge._operations.finish(operation, status="success")


def test_update_entries_are_coordinated(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(bridge, "_check_updates_worker",
                        lambda manual: calls.append("check"))
    operation, _conflict = bridge._reserve("order", title="订单处理")
    try:
        blocked = bridge.check_updates()
        assert blocked["ok"] is False and blocked["reason"] == "operation_conflict"
    finally:
        bridge._operations.finish(operation, status="success")

    assert bridge.check_updates()["ok"] is True
    assert calls == ["check"]
    # 结束检查占位（worker 被替换成了记录函数，手动释放）
    active = bridge.operation_status()["operation"]
    bridge._operations.finish(
        bridge._operations._active if active else None, status="success")


def test_operation_status_reflects_the_actual_result(tmp_path, monkeypatch):
    """占位结束后 operation 状态要如实反映这次调用的结果，而不是一律 success。"""
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_wps_preview_impl", lambda: {
        "ok": False, "code": "wps_disabled", "status": "rejected",
        "reason": "同步已关闭"})
    got = bridge.wps_preview()
    assert got["ok"] is False

    last = bridge.operation_status()["last"]
    assert last["status"] == "rejected"
    assert last["mode"] == "wps_preview"
    assert last["reason"] == "wps_disabled"
    assert bridge.operation_status()["active"] is False
