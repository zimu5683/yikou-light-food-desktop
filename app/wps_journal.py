"""WPS 写入意图日志：把"即将写云端"的意图持久化到本地，崩溃后可只读对账。

设计目标（小而有界）：

* ``apply_plan`` 在任何云端写入之前，先把整批计划（目标表/日期/姓名电话/槽位/
  原值/期望值/账本条目）保存为 ``planned``；
* 每张表第一次真正写入前更新为 ``writing``；写入+回读校验通过后更新为
  ``verified``；校验不确定则更新为 ``uncertain``；
* 日志文件与账本同目录，路径为 ``<ledger>.journal``；
* 日志不可读/损坏时失败关闭，绝不能当"没有未完成操作"继续增量叠加。

桌面端与服务端的差异：桌面端没有角色权限系统（单用户本机程序），因此恢复入口
不做管理员校验，但**明确确认、范围、审计**一个都不能少。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping

try:
    from .wps_atomicio import FileLock, atomic_write_text, lock_path_for
    from .wps_cloud import SyncLedger
except ImportError:  # pragma: no cover - 直接执行模块时
    from wps_atomicio import FileLock, atomic_write_text, lock_path_for
    from wps_cloud import SyncLedger

JOURNAL_VERSION = 1

#: 成功归档后保留在热 journal 的最新 terminal operation 数。
DEFAULT_COMPACT_KEEP_OPERATIONS = 50

OP_STATUSES = {
    "planned", "writing", "ledger_pending", "uncertain",
    "verified", "failed", "not_started", "retired_guarded",
}
PENDING_OP_STATUSES = {"planned", "writing", "ledger_pending", "uncertain"}
SHEET_STATUSES = {
    "planned", "writing", "ledger_pending", "uncertain",
    "verified", "failed_no_write", "not_started", "retired_guarded",
}
PENDING_SHEET_STATUSES = {"planned", "writing", "ledger_pending", "uncertain"}
_TERMINAL_SHEET_STATUSES = {"verified", "failed_no_write", "not_started"}


class JournalError(RuntimeError):
    """意图日志不可用/损坏（调用方必须失败关闭，不能当"没有未完成操作"）。"""


#: 兼容服务端命名：损坏的日志抛这个名字，调用方按同一条路径处理。
JournalCorruptError = JournalError


def journal_path_for(ledger_path: str | os.PathLike[str]) -> Path:
    """账本旁边的意图日志路径；生产/测试都用同一规则，避免碰真实用户账本。"""
    return Path(str(ledger_path) + ".journal")


def new_operation_id() -> str:
    """生成一个 operation id（形如 ``wps-<16位小写hex>``）。"""
    return f"wps-{uuid.uuid4().hex[:16]}"


def _now() -> str:
    # 微秒级：同秒内两次日志更新也能定序（跨进程合并时按 updated_at 取新）。
    return _dt.datetime.now().isoformat(timespec="microseconds")


def _as_status(value: Any) -> str:
    value = str(value or "")
    return value if value in SHEET_STATUSES else "uncertain"


def _op_order_key(operation: Any) -> str:
    if not isinstance(operation, Mapping):
        return ""
    return str(operation.get("updated_at")
               or operation.get("created_at") or "")


def _all_sheets_terminal(operation: Mapping[str, Any]) -> bool:
    sheets = operation.get("sheets")
    if not isinstance(sheets, Mapping) or not sheets:
        return False
    for record in sheets.values():
        if not isinstance(record, Mapping):
            return False
        if str(record.get("status") or "") not in _TERMINAL_SHEET_STATUSES:
            return False
    return True


def _verified_entries_are_in_ledger(operation: Mapping[str, Any], ledger: Any) -> bool:
    """只有磁盘账本仍持有 journal 记录的全部幂等 slots 时才允许归档 verified。"""
    sheets = operation.get("sheets")
    if not isinstance(sheets, Mapping):
        return False
    for record in sheets.values():
        if not isinstance(record, Mapping):
            return False
        entries = record.get("ledger_entries")
        if entries is None:
            continue
        if not isinstance(entries, Mapping):
            return False
        if not entries:
            continue
        if ledger is None:
            return False
        target_date = str(record.get("target_date") or "")
        file_id = str(record.get("file_id") or "")
        for storage_key, payload in entries.items():
            if not isinstance(payload, Mapping):
                return False
            name, separator, phone = str(storage_key).partition("\u0000")
            if not separator:
                return False
            wanted = payload.get("slots")
            if wanted is None:
                return False
            existing = ledger.synced_slots(target_date, file_id, name, phone)
            try:
                if [int(value) for value in (existing or [])] != [int(value) for value in wanted]:
                    return False
            except (TypeError, ValueError):
                return False
    return True


def _load_archive(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": JOURNAL_VERSION, "operations": {}}
    except (OSError, UnicodeDecodeError) as exc:
        raise JournalError("归档日志不可读") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JournalError("归档日志 JSON 损坏") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("operations"), dict):
        raise JournalError("归档日志结构非法")
    return payload


def _sheet_next_action(status: str) -> str:
    return {
        "uncertain": "manual_reconcile",
        "ledger_pending": "recover_journal",
        "writing": "recover_journal",
        "planned": "start",
        "not_started": "repreview",
        "failed_no_write": "none",
        "verified": "none",
        "retired_guarded": "manual_reconcile",
    }.get(status, "manual_reconcile")


class SyncJournal:
    """原子落盘的轻量意图日志；``path=None`` 时为纯内存（只用于无账本调用）。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path else None
        self.data: dict[str, Any] = {"version": JOURNAL_VERSION, "operations": {}}
        self._removed_operations: set[str] = set()
        self._load()

    # ---- 磁盘 ----

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError) as exc:
            raise JournalError("意图日志不可读") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JournalError("意图日志 JSON 损坏") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("operations"), dict):
            raise JournalError("意图日志结构非法")
        version = payload.get("version", JOURNAL_VERSION)
        if not isinstance(version, int) or not (1 <= version <= JOURNAL_VERSION):
            raise JournalError("不支持的意图日志版本")
        for op_id, op in payload["operations"].items():
            if not isinstance(op_id, str) or not isinstance(op, dict):
                raise JournalError("意图日志操作结构非法")
            sheets = op.get("sheets")
            if not isinstance(sheets, dict):
                raise JournalError("意图日志 sheets 结构非法")
            for sheet_key, sheet in sheets.items():
                if not isinstance(sheet_key, str) or not isinstance(sheet, dict):
                    raise JournalError("意图日志子表结构非法")
        self.data = payload

    def _save_unlocked(self) -> Path:
        text = json.dumps(self.data, ensure_ascii=False, indent=2)
        return atomic_write_text(self.path, text)

    def _merge_into(self, fresh: "SyncJournal") -> None:
        """把本内存对象的 operations 合并进磁盘最新快照；不同 operation 不互相覆盖。"""
        mine = self.data.get("operations") or {}
        theirs = fresh.data.get("operations") or {}
        for operation_id, operation in mine.items():
            other = theirs.get(operation_id)
            if not isinstance(other, dict) or _op_order_key(operation) >= _op_order_key(other):
                theirs[operation_id] = operation
        for operation_id in self._removed_operations:
            theirs.pop(operation_id, None)
        fresh.data["operations"] = theirs
        fresh.data.setdefault("version", JOURNAL_VERSION)

    def save(self) -> Path | None:
        """跨进程原子写：数据锁内重读磁盘，合并不同 operation 后再落盘。"""
        if self.path is None:
            return None
        with FileLock(lock_path_for(self.path), timeout=30.0):
            fresh = SyncJournal(self.path)
            self._merge_into(fresh)
            fresh._save_unlocked()
            self.data = fresh.data
            self._removed_operations.clear()
        return self.path

    # ---- 操作生命周期 ----

    def operations(self) -> dict[str, dict[str, Any]]:
        """返回 operations 映射（就地可改；改完由调用方 :meth:`save`）。"""
        return self.data.setdefault("operations", {})

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        """按 id 取某个 operation；不存在返回 ``None``。"""
        op = self.operations().get(operation_id)
        return op if isinstance(op, dict) else None

    def unsupported_statuses(self, operation: Mapping[str, Any]) -> list[str]:
        """返回 operation/sheet 中不在当前版本词表内的原始状态字符串。

        未知状态不是"可忽略的额外字段"，而是必须继续阻断的审计数据；
        调用方只能保留原文并转 uncertain，不能覆盖成 verified。
        """
        unsupported: list[str] = []
        op_status = str(operation.get("status") or "")
        if op_status not in OP_STATUSES:
            unsupported.append(op_status or "<missing>")
        sheets = operation.get("sheets") or {}
        if isinstance(sheets, Mapping):
            for record in sheets.values():
                if not isinstance(record, Mapping):
                    continue
                sheet_status = str(record.get("status") or "")
                if sheet_status not in SHEET_STATUSES:
                    unsupported.append(sheet_status or "<missing>")
        return unsupported

    def pending_operations(self) -> dict[str, dict[str, Any]]:
        """返回仍需人工处置（或可继续）的 operation。"""
        result: dict[str, dict[str, Any]] = {}
        for op_id, op in self.operations().items():
            if not isinstance(op, dict):
                continue
            op_status = str(op.get("status") or "")
            if op_status not in OP_STATUSES or op_status in PENDING_OP_STATUSES:
                result[op_id] = op
                continue
            sheets = op.get("sheets") or {}
            if isinstance(sheets, Mapping):
                for record in sheets.values():
                    if not isinstance(record, Mapping):
                        result[op_id] = op
                        break
                    sheet_status = str(record.get("status") or "")
                    if (record.get("retired_guarded")
                            or sheet_status == "retired_guarded"):
                        # guarded retire 的防重复保护由 has_guard + apply_plan 提供，
                        # 不重新进入全局 pending；原始未知/审计状态可以保留。
                        continue
                    if (sheet_status not in SHEET_STATUSES
                            or sheet_status in PENDING_SHEET_STATUSES):
                        result[op_id] = op
                        break
        return result

    def guarded_operations(self) -> dict[str, dict[str, Any]]:
        """返回显式退出但保留防重复闸门的 operation。"""
        result: dict[str, dict[str, Any]] = {}
        for op_id, op in self.operations().items():
            if not isinstance(op, Mapping):
                continue
            if (str(op.get("status") or "") == "retired_guarded"
                    or bool(op.get("retired_guarded"))):
                result[op_id] = op
                continue
            sheets = op.get("sheets") or {}
            if isinstance(sheets, Mapping) and any(
                    isinstance(record, Mapping)
                    and (record.get("retired_guarded")
                         or str(record.get("status") or "") == "retired_guarded")
                    for record in sheets.values()):
                result[op_id] = op
        return result

    def has_guard(self, target_date: str, file_id: str) -> bool:
        """同一目标日期 + 云表是否仍有未解除的防重复闸门。"""
        for operation in self.guarded_operations().values():
            op_retired = bool(operation.get("retired_guarded")) or (
                str(operation.get("status") or "") == "retired_guarded")
            sheets = operation.get("sheets") or {}
            if not isinstance(sheets, Mapping):
                continue
            for record in sheets.values():
                if not isinstance(record, Mapping):
                    continue
                if not (op_retired or record.get("retired_guarded")
                        or str(record.get("status") or "") == "retired_guarded"):
                    continue
                record_date = str(record.get("target_date") or "")
                if not record_date:
                    record_date = str(operation.get("target_date") or "")
                record_file = str(record.get("file_id") or "")
                if record_date == str(target_date) and record_file == str(file_id):
                    return True
        return False

    def blocking_operations(self, target_date: str, file_id: str, *,
                            exclude_operation_id: str = ""
                            ) -> dict[str, dict[str, Any]]:
        """仍会阻止「同一目标日期 + 同一云表」再次写入的 operation。

        这是**重复累加的最后一道闸门**：只要上一次写入没有走到可证明的终点
        （``verified`` / ``failed_no_write`` / ``not_started``），就不能再写同一张表 ——
        否则"结果未知"的那次如果其实写成功了，这一次会把同一批餐再加一遍。

        两类命中：

        * 未处置的 pending（``planned`` / ``writing`` / ``ledger_pending`` /
          ``uncertain``，以及任何未知状态）—— 结果未知，必须人工处置；
        * ``retired_guarded`` —— 已退出待处理队列，但防重复闸门仍然保留。

        ``exclude_operation_id``：正在执行的本次 operation 必须排除，否则它自己刚
        建出来的 ``planned`` 记录会把本批挡在门外（自锁）。
        """
        key_date = str(target_date or "")
        key_file = str(file_id or "")
        blocked: dict[str, dict[str, Any]] = {}
        for op_id, op in self.operations().items():
            if not isinstance(op, Mapping) or str(op_id) == str(exclude_operation_id or ""):
                continue
            op_retired = bool(op.get("retired_guarded")) or (
                str(op.get("status") or "") == "retired_guarded")
            sheets = op.get("sheets") or {}
            if not isinstance(sheets, Mapping):
                continue
            for record in sheets.values():
                if not isinstance(record, Mapping):
                    continue
                record_date = str(record.get("target_date") or "")
                if not record_date:
                    record_date = str(op.get("target_date") or "")
                if record_date != key_date:
                    continue
                if str(record.get("file_id") or "") != key_file:
                    continue
                status = str(record.get("status") or "")
                retired = bool(op_retired or record.get("retired_guarded")
                               or status == "retired_guarded")
                unknown = (status not in SHEET_STATUSES
                           or status in PENDING_SHEET_STATUSES)
                if retired or unknown:
                    blocked[str(op_id)] = op
                    break
        return blocked

    def create_operation(self, operation_id: str, sheets: Mapping[str, dict[str, Any]],
                         *, target_date: str = "") -> dict[str, Any]:
        """新建一个 operation（状态 planned），并返回它。"""
        now = _now()
        op = {
            "operation_id": operation_id,
            "target_date": target_date,
            "created_at": now,
            "updated_at": now,
            "status": "planned",
            "next_action": "start",
            "sheets": {key: dict(record) for key, record in sheets.items()},
        }
        for record in op["sheets"].values():
            record.setdefault("status", "planned")
            record.setdefault("next_action", "start")
        self._refresh(op)
        self._removed_operations.discard(operation_id)
        self.operations()[operation_id] = op
        return op

    def set_sheet_status(self, operation_id: str, sheet_key: str, status: str,
                         **fields: Any) -> dict[str, Any]:
        """更新某张子表的状态与附带字段，并刷新 operation 汇总状态。"""
        op = self.get_operation(operation_id)
        if op is None:
            raise JournalError("意图日志里没有该操作")
        record = (op.get("sheets") or {}).get(sheet_key)
        if not isinstance(record, dict):
            raise JournalError("意图日志里没有该子表")
        record["status"] = _as_status(status)
        record["next_action"] = fields.pop(
            "next_action", _sheet_next_action(record["status"]))
        record["updated_at"] = _now()
        for key, value in fields.items():
            record[key] = value
        op["updated_at"] = _now()
        self._refresh(op)
        return op

    def remove_operation(self, operation_id: str) -> None:
        """从日志里删除某个 operation（下次 save 时同步删除磁盘条目）。"""
        self.operations().pop(operation_id, None)
        self._removed_operations.add(operation_id)

    def compact(self, ledger: Any = None, *, keep_operations: int = 50,
                dry_run: bool = False) -> dict[str, Any]:
        """归档可证明安全的 terminal operation，避免 verified 日志无限增长。

        只归档 ``verified``（且磁盘账本仍持有全部幂等 slots）与
        ``failed_no_write``/``not_started``；任何未完成或无法证明的一律保留。
        """
        if self.path is None:
            return {"removed": 0, "kept": len(self.operations()), "archived": False}
        archive_path = Path(str(self.path) + ".archive")
        removed: list[str] = []
        with FileLock(lock_path_for(self.path), timeout=30.0):
            fresh = SyncJournal(self.path)
            pending_ids = set(fresh.pending_operations())
            operations = fresh.operations()
            # 只依据磁盘账本做 verified 证明；失败时宁可保留日志。
            ledger_snapshot = None
            ledger_path = getattr(ledger, "path", None) if ledger is not None else None
            if ledger_path:
                try:
                    ledger_snapshot = SyncLedger(ledger_path)
                except Exception:  # noqa: BLE001 - 证明不了就保留
                    ledger_snapshot = None
            candidates: list[str] = []
            for operation_id, operation in operations.items():
                if operation_id in pending_ids or not isinstance(operation, Mapping):
                    continue
                if not _all_sheets_terminal(operation):
                    continue
                statuses = {str(record.get("status") or "")
                            for record in (operation.get("sheets") or {}).values()
                            if isinstance(record, Mapping)}
                if statuses <= {"failed_no_write", "not_started"}:
                    candidates.append(operation_id)
                    continue
                if "verified" in statuses and _verified_entries_are_in_ledger(
                        operation, ledger_snapshot):
                    candidates.append(operation_id)
            candidates.sort(key=lambda op_id: _op_order_key(operations.get(op_id, {})))
            if keep_operations > 0:
                removable = candidates[:-keep_operations]
            else:
                removable = candidates
            if dry_run:
                return {"removed": len(removable), "kept": len(operations),
                        "dry_run": True, "archived": False}
            if removable:
                archive = _load_archive(archive_path)
                archived_ops = archive["operations"]
                for operation_id in removable:
                    archived_ops[operation_id] = operations[operation_id]
                atomic_write_text(
                    archive_path,
                    json.dumps(archive, ensure_ascii=False, indent=2))
                for operation_id in removable:
                    operations.pop(operation_id, None)
                fresh._save_unlocked()
                self.data = fresh.data
                self._removed_operations.clear()
                removed = removable
        return {"removed": len(removed), "kept": len(self.operations()),
                "archived": bool(removed)}

    def _refresh(self, op: dict[str, Any]) -> None:
        statuses = [str(s.get("status", "uncertain"))
                    for s in (op.get("sheets") or {}).values() if isinstance(s, dict)]
        if any(status not in SHEET_STATUSES for status in statuses):
            # 未知/不支持状态：保留 sheet 原始审计值，只把 op 标记为 uncertain 阻断，
            # 绝不落到下面 else 的 verified。
            op["status"], op["next_action"] = "uncertain", "manual_reconcile"
        elif any(status == "uncertain" for status in statuses):
            op["status"], op["next_action"] = "uncertain", "manual_reconcile"
        elif any(status == "retired_guarded" for status in statuses):
            op["status"], op["next_action"] = "retired_guarded", "manual_reconcile"
        elif any(status == "ledger_pending" for status in statuses):
            op["status"], op["next_action"] = "ledger_pending", "recover_journal"
        elif any(status == "writing" for status in statuses):
            op["status"], op["next_action"] = "writing", "recover_journal"
        elif any(status == "planned" for status in statuses):
            op["status"], op["next_action"] = "planned", "start"
        elif any(status == "not_started" for status in statuses):
            op["status"], op["next_action"] = "not_started", "repreview"
        elif any(status == "failed_no_write" for status in statuses):
            op["status"], op["next_action"] = "failed", ""
        else:
            op["status"], op["next_action"] = "verified", ""
        op["updated_at"] = _now()


__all__ = [
    "DEFAULT_COMPACT_KEEP_OPERATIONS",
    "JOURNAL_VERSION",
    "JournalCorruptError",
    "JournalError",
    "OP_STATUSES",
    "PENDING_OP_STATUSES",
    "PENDING_SHEET_STATUSES",
    "SHEET_STATUSES",
    "SyncJournal",
    "journal_path_for",
    "new_operation_id",
]
