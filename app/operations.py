"""统一操作协调器：让"同一时刻只允许一个危险操作"变成一条明确规则。

为什么需要它：桌面端原本只用 ``_worker_lock`` 保护订单/闪时送任务，于是
"任务在跑的时候点云同步上传"、"更新安装中途又点了只读核对"这类组合没有任何
拦截。它们各自读写同一批状态（Excel、账本、未决日志、云端表），互相覆盖的
后果是数据错乱甚至重复下单。

设计要点：

* **占位只覆盖内存状态**（微秒级），绝不包住慢操作（云端往返、下载）：冲突
  立即返回，不排队、不等待，也不存在"持锁再请求持锁"的嵌套死锁；
* 每个操作有稳定的 ``operation_id`` 与阶段（``phase``），前端据此禁用按钮、
  显示"谁在跑、跑到哪一步"；
* 状态查询不需要占位：``status()`` 只读返回当前活动操作与最近一次结果；
* ``order``/``sss`` 的实际执行仍在各自的 worker 线程里，协调器只记录
  "这个模式被占了"，由持有人在结束时 ``finish``。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

#: 受统一互斥保护的操作模式（键 = 前端可见的模式名）。
MODES = (
    "order", "sss", "sss_review", "sss_uncertain_resolve", "sss_day_orders",
    "wps_preview", "wps_upload", "wps_authorize", "wps_check_copies",
    "wps_recovery_resolve", "check_update", "install_update",
)

#: 互斥组：同组内的模式彼此排斥。``wps_preview`` 是只读的，但仍与
#: ``wps_upload`` 等写操作互斥（读到的计划必须是稳定快照）。
_EXCLUSIVE_MODES = frozenset(MODES)

_MODE_TITLES = {
    "order": "订单处理",
    "sss": "闪时送下单",
    "sss_review": "闪时送只读核对",
    "sss_uncertain_resolve": "闪时送未决处置",
    "sss_day_orders": "读取云端当天名单",
    "wps_check_copies": "副本一致性核对",
    "wps_preview": "云文档预览",
    "wps_upload": "云文档上传",
    "wps_authorize": "云文档授权",
    "wps_recovery_resolve": "云同步恢复处置",
    "check_update": "检查更新",
    "install_update": "安装更新",
}

#: 业务状态里属于"终结"的一批（仅供展示与统计参考）。
#: **active 判定不依赖它** —— 见 :class:`Operation` 的说明。
_TERMINAL_STATUSES = frozenset({
    "success", "noop", "partial", "failed", "error", "rejected", "stopped",
    "uncertain", "blocked", "recovered", "not_started", "consumed",
    "preview_ready", "preflight_ok", "dry_run",
})


def mode_title(mode: str) -> str:
    """模式的中文名（界面提示用）。"""
    return _MODE_TITLES.get(str(mode or ""), str(mode or ""))


@dataclass
class Operation:
    """一个被占用的操作。

    ``active`` 是**显式字段**，不由 ``status`` 推断：``status`` 是结果描述
    （可以是任何业务词，例如 ``preview_ready`` / ``dry_run`` / ``blocked_by_uncertain``），
    一旦终结就必须立刻不再显示为"进行中"。用状态词表去推断会让没列进词表的
    业务状态永远停留在 active，前端就会一直禁用按钮。
    """

    operation_id: str
    mode: str
    status: str = "running"
    phase: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    finished_at: float = 0.0
    reason: str = ""
    next_action: str = ""
    active: bool = True

    def public(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "mode": self.mode,
            "title": mode_title(self.mode),
            "status": self.status,
            "active": bool(self.active),
            "phase": self.phase,
            "reason": self.reason,
            "next_action": self.next_action,
            "summary": dict(self.summary),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at) if self.finished_at else "",
        }


@dataclass
class Reservation:
    """``try_reserve`` 的返回值：要么拿到占位，要么带着冲突信息被拒绝。"""

    granted: bool
    operation: Operation | None = None
    conflict: dict[str, Any] | None = None


def _iso(timestamp: float) -> str:
    if not timestamp:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(timestamp))


class OperationCoordinator:
    """线程安全的操作占位表。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: Operation | None = None
        self._last: Operation | None = None

    # ---- 占位 ----
    def try_reserve(self, mode: str, *, summary: Mapping[str, Any] | None = None,
                    phase: str = "", next_action: str = "",
                    operation_id: str = "") -> Reservation:
        """尝试占用某个模式；已被占用时**立即**返回冲突（不排队）。"""
        mode_text = str(mode or "")
        if mode_text not in _EXCLUSIVE_MODES:
            raise ValueError(f"未知的操作模式：{mode_text}")
        with self._lock:
            current = self._active
            if current is not None and current.active:
                return Reservation(granted=False, operation=None, conflict={
                    "code": "operation_conflict",
                    "operation_id": current.operation_id,
                    "message": (f"已有操作正在进行：{mode_title(current.mode)}"
                                + (f"（{current.phase}）" if current.phase else "")
                                + f"，已拒绝本次{mode_title(mode_text)}"),
                    "next_action": (current.next_action
                                    or "等待当前操作结束后重试"),
                    "conflicting_operation": current.public(),
                })
            operation = Operation(
                operation_id=str(operation_id or f"op-{uuid.uuid4().hex[:16]}"),
                mode=mode_text, status="running", phase=str(phase),
                summary=dict(summary or {}), started_at=time.time(),
                next_action=str(next_action))
            self._active = operation
            return Reservation(granted=True, operation=operation)

    def update(self, operation: Operation | None, *, phase: str = "",
               summary: Mapping[str, Any] | None = None,
               next_action: str = "") -> None:
        """推进阶段/摘要（供长操作在关键节点调用）。已被终结时静默返回。"""
        if operation is None:
            return
        with self._lock:
            if not operation.active:
                return
            if phase:
                operation.phase = str(phase)
            if summary:
                operation.summary.update(dict(summary))
            if next_action:
                operation.next_action = str(next_action)

    def finish(self, operation: Operation | None, *, status: str = "success",
               reason: str = "", summary: Mapping[str, Any] | None = None,
               next_action: str = "") -> None:
        """结束操作并释放占位；重复调用幂等。"""
        if operation is None:
            return
        with self._lock:
            if not operation.active:
                return
            operation.active = False
            operation.status = str(status or "success")
            operation.reason = str(reason or "")
            operation.next_action = str(next_action or "")
            if summary:
                operation.summary.update(dict(summary))
            operation.finished_at = time.time()
            if self._active is operation:
                self._active = None
            self._last = operation

    def is_active(self, operation: Operation | None) -> bool:
        """该操作是否仍占用槽位。"""
        if operation is None:
            return False
        with self._lock:
            return bool(operation.active and self._active is operation)

    def active_operation(self) -> Operation | None:
        """当前占用槽位的操作（没有则 ``None``）。"""
        with self._lock:
            current = self._active
            if current is None or not current.active:
                return None
            return current

    # ---- 查询 ----
    def status(self, operation_id: str = "") -> dict[str, Any]:
        """只读状态：``operation_id`` 为空返回当前活动操作，否则按 id 查。"""
        key = str(operation_id or "").strip()
        with self._lock:
            active = self.active_operation()
            last = self._last
            if not key:
                return {
                    "ok": True,
                    "active": bool(active),
                    "operation": active.public() if active else None,
                    "last": last.public() if last else None,
                }
            for candidate in (active, last):
                if candidate is not None and candidate.operation_id == key:
                    return {"ok": True, "active": bool(active),
                            "operation": candidate.public(),
                            "last": last.public() if last else None}
        return {
            "ok": False,
            "active": bool(active),
            "operation": active.public() if active else None,
            "last": last.public() if last else None,
            "reason": "未找到该 operation_id（可能已完成并被后续操作替换）",
            "reason_code": "operation_not_found",
        }

    def conflict_payload(self, conflict: Mapping[str, Any],
                         *, action: str = "") -> dict[str, Any]:
        """把冲突转成各入口统一的拒绝结果（保留 ok/status/reason 字段）。"""
        data = dict(conflict or {})
        return {
            "ok": False,
            "status": "rejected",
            "code": "operation_conflict",
            "reason": str(data.get("message") or "已有其他操作正在进行，已拒绝本次操作"),
            "next_action": str(data.get("next_action") or "等待当前操作结束后重试"),
            "operation_id": str(data.get("operation_id") or ""),
            "conflicting_operation": data.get("conflicting_operation"),
            "action": str(action or ""),
        }


__all__ = [
    "MODES",
    "Operation",
    "OperationCoordinator",
    "Reservation",
    "mode_title",
]
