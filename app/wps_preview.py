"""WPS 上传预览令牌与计划指纹。

职责边界（纯内存 + 哈希，绝不写云端/账本）：

* 生成一次性预览令牌，默认 10 分钟过期；
* 记录本地排单表 SHA-256、生效目标表、配置/日期上下文指纹、计划内容指纹；
* 只读查询令牌状态；消费（valid -> consumed）与判定失效（valid -> invalidated）
  都在锁内原子完成。

上传前复核由 :mod:`app.bridge` 重新只读构建计划，再与本模块保存的指纹比对。
这**不是**远端 CAS：只能缩短"只读复核 → 首次写入"的窗口。
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

PREVIEW_TTL_SECONDS = 600.0
MAX_PREVIEW_ENTRIES = 64


def sha256_file(path: str | Path) -> str:
    """读取文件字节并返回 SHA-256 十六进制摘要；文件不存在/不可读时抛 OSError。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _jsonable(value: Any) -> Any:
    """把计划对象里的 date/dataclass/Path/dict/tuple 转成稳定 JSON 结构。"""
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # dataclass 或其他普通对象：优先 asdict，否则退回字符串，保证可哈希。
    try:
        from dataclasses import asdict, is_dataclass
        if is_dataclass(value):
            return _jsonable(asdict(value))
    except Exception:  # pragma: no cover - 兜底
        pass
    return str(value)


def fingerprint_payload(payload: Any) -> str:
    """稳定 JSON 序列化后取 SHA-256；字典键排序，list 顺序保留。"""
    text = json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default)


def _row_key_pairs(raw: Any) -> list[list[Any]]:
    if not isinstance(raw, Mapping):
        return []
    pairs: list[tuple[Any, Any]] = []
    for key, value in raw.items():
        try:
            sort_key: Any = int(key)
        except (TypeError, ValueError):
            sort_key = str(key)
        pairs.append((sort_key, [sort_key, _jsonable(value)]))
    pairs.sort(key=lambda item: (isinstance(item[0], str), item[0]))
    return [pair for _, pair in pairs]


def _change_to_dict(change: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in (
        "kind", "name", "phone", "row", "slot", "delta", "target_col",
        "total_before", "total_after", "target_ok", "target_occupied",
        "address", "meal_type", "meal_kind", "detail", "fill_type", "fill_kind",
        "fill_formula", "insert_row", "local_meals", "ledger_prev",
    ):
        out[name] = _get(change, name)
    out["local_rows"] = list(_get(change, "local_rows", ()) or ())
    target_blocked = _get(change, "target_blocked", None)
    if target_blocked is None:
        target_blocked = bool(str(out.get("target_occupied") or "").strip())
    out["target_blocked"] = bool(target_blocked)
    needs_write = _get(change, "needs_write", None)
    if needs_write is None:
        needs_write = bool(out.get("delta"))
    out["needs_write"] = bool(needs_write)
    return out


def _insert_block_to_dict(block: Any) -> dict[str, Any]:
    return {
        "position": _get(block, "position"),
        "count": _get(block, "count"),
        "first_row": _get(block, "first_row"),
        "address": _get(block, "address", ""),
        "append_only": bool(_get(block, "append_only", False)),
    }


def _plan_to_dict(plan: Any) -> dict[str, Any]:
    change_values = _get(plan, "changes", []) or []
    block_values = _get(plan, "insert_blocks", []) or []
    columns = _get(plan, "columns", {}) or {}
    warnings = _get(plan, "warnings", []) or []
    unknown = _get(plan, "unknown_addresses", []) or []
    date_cols = _get(plan, "date_cols", []) or []
    format_rows = _get(plan, "format_rows", []) or []
    target_date = _get(plan, "target_date")
    return {
        "sheet": str(_get(plan, "sheet", "") or ""),
        "file_id": str(_get(plan, "file_id", "") or ""),
        "drive_id": str(_get(plan, "drive_id", "") or ""),
        "target_date": (target_date.isoformat() if hasattr(target_date, "isoformat")
                        else str(target_date or "")),
        "target_col": _get(plan, "target_col", 0),
        "target_header": str(_get(plan, "target_header", "") or ""),
        "weekday_number": _get(plan, "weekday_number", 0),
        "append_row": _get(plan, "append_row", 0),
        "last_data_row": _get(plan, "last_data_row", 0),
        "columns": {str(key): value for key, value in sorted(columns.items())},
        "marker_col": _get(plan, "marker_col", 0),
        "date_cols": [int(col) for col in date_cols],
        "sort_enabled": bool(_get(plan, "sort_enabled", False)),
        "sort_key_col": _get(plan, "sort_key_col", 0),
        "sort_range": str(_get(plan, "sort_range", "") or ""),
        "sort_probe_col": _get(plan, "sort_probe_col", 0),
        "sort_mismatch": bool(_get(plan, "sort_mismatch", False)),
        "row_keys": _row_key_pairs(_get(plan, "row_keys", {}) or {}),
        "format_rows": sorted({int(row) for row in format_rows if row is not None}),
        "unknown_addresses": [str(item) for item in unknown],
        "blocked_reason": str(_get(plan, "blocked_reason", "") or ""),
        "previous_batch": str(_get(plan, "previous_batch", "") or ""),
        "warnings": [str(item) for item in warnings],
        "changes": [_change_to_dict(change) for change in change_values],
        "insert_blocks": [_insert_block_to_dict(block) for block in block_values],
    }


def canonical_plan(plans: Iterable[Any]) -> list[dict[str, Any]]:
    """把计划列表转成稳定可比较/可哈希的结构。"""
    return [_plan_to_dict(plan) for plan in plans]


def plan_fingerprint(plans: Iterable[Any]) -> str:
    """计划内容指纹（排序无关的稳定 JSON 哈希）。"""
    return fingerprint_payload(canonical_plan(plans))


@dataclass
class WpsPreviewRecord:
    """一次预览的完整上下文（令牌 + 指纹 + 结构化计划）。"""

    preview_id: str
    created_at: float
    expires_at: float
    ttl_seconds: int
    local_sha256: str
    context: dict[str, Any]
    context_fingerprint: str
    plan: list[dict[str, Any]]
    plan_fingerprint: str
    summary: dict[str, Any]
    text: str
    tables: list[dict[str, Any]]
    blocked: list[dict[str, Any]]
    warnings: list[str]
    target_date: str
    target_tables: dict[str, Any]
    consumed_at: float | None = None
    invalidated_at: float | None = None
    invalidated_reason: str = ""

    def state(self, now: float | None = None) -> str:
        """令牌状态：``consumed`` / ``expired`` / ``invalidated`` / ``valid``。"""
        now = time.time() if now is None else float(now)
        if self.consumed_at is not None:
            return "consumed"
        if now >= self.expires_at:
            return "expired"
        if self.invalidated_at is not None:
            return "invalidated"
        return "valid"


class PreviewStore:
    """有界、线程安全的一次性预览令牌表。"""

    def __init__(self, *, ttl_seconds: float = PREVIEW_TTL_SECONDS,
                 max_entries: int = MAX_PREVIEW_ENTRIES,
                 prefix: str = "pv") -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self.prefix = str(prefix or "pv")
        self._lock = threading.RLock()
        self._items: "OrderedDict[str, WpsPreviewRecord]" = OrderedDict()

    # -- 内部 ----------------------------------------------------------
    def _now(self, now: float | None = None) -> float:
        return time.time() if now is None else float(now)

    def _prune_locked(self, now: float) -> None:
        for preview_id, record in list(self._items.items()):
            if record.expires_at <= now:
                del self._items[preview_id]
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def _new_id_locked(self, now: float) -> str:
        while True:
            candidate = f"{self.prefix}-{int(now * 1000)}-{secrets.token_hex(6)}"
            if candidate not in self._items:
                return candidate

    # -- 公开 API ------------------------------------------------------
    def create(self, *, local_sha256: str, context: dict[str, Any],
               context_fingerprint: str, plan: list[dict[str, Any]],
               plan_fingerprint: str, summary: dict[str, Any], text: str,
               tables: list[dict[str, Any]], blocked: list[dict[str, Any]],
               warnings: list[str], target_date: str,
               target_tables: dict[str, Any],
               ttl_seconds: float | None = None,
               now: float | None = None) -> WpsPreviewRecord:
        """登记一次预览并返回它的记录（含一次性 ``preview_id``）。"""
        current = self._now(now)
        ttl = self.ttl_seconds if ttl_seconds is None else max(1.0, float(ttl_seconds))
        with self._lock:
            self._prune_locked(current)
            preview_id = self._new_id_locked(current)
            record = WpsPreviewRecord(
                preview_id=preview_id,
                created_at=current,
                expires_at=current + ttl,
                ttl_seconds=int(round(ttl)),
                local_sha256=str(local_sha256 or ""),
                context=copy.deepcopy(dict(context or {})),
                context_fingerprint=str(context_fingerprint or ""),
                plan=copy.deepcopy(list(plan or [])),
                plan_fingerprint=str(plan_fingerprint or ""),
                summary=copy.deepcopy(dict(summary or {})),
                text=str(text or ""),
                tables=copy.deepcopy(list(tables or [])),
                blocked=copy.deepcopy(list(blocked or [])),
                warnings=list(warnings or []),
                target_date=str(target_date or ""),
                target_tables=copy.deepcopy(dict(target_tables or {})),
            )
            self._items[preview_id] = record
            self._prune_locked(current)
            return record

    def get(self, preview_id: str,
            now: float | None = None) -> tuple[WpsPreviewRecord | None, str]:
        """查询而不改变状态；返回 ``(record, error_code)``。"""
        key = str(preview_id or "").strip()
        current = self._now(now)
        if not key:
            return None, "missing_preview"
        with self._lock:
            # 先取记录再判过期：否则 _prune_locked 会把刚过期的令牌直接删掉，
            # 客户端只能拿到模糊的 preview_not_found，无法区分"过期"。
            record = self._items.get(key)
            if record is None:
                self._prune_locked(current)
                return None, "preview_not_found"
            if record.consumed_at is not None:
                return record, "preview_consumed"
            if current >= record.expires_at:
                # 不在这里删除：同一令牌被重复尝试时应给出**同一个**明确原因
                # （"已过期"比第二次变成"不存在"更有用）。容量与过期清理由
                # create() 的 _prune_locked 负责。
                return record, "preview_expired"
            if record.invalidated_at is not None:
                return record, (record.invalidated_reason or "preview_invalidated")
            self._prune_locked(current)
            return record, ""

    def consume(self, preview_id: str,
                now: float | None = None) -> tuple[WpsPreviewRecord | None, str]:
        """原子消费（只有 valid 可通过）；返回记录与错误码。"""
        key = str(preview_id or "").strip()
        current = self._now(now)
        with self._lock:
            record, code = self.get(key, now=current)
            if code:
                return record, code
            assert record is not None
            record.consumed_at = current
            return record, ""

    def invalidate(self, preview_id: str, reason: str = "preview_changed",
                   now: float | None = None) -> bool:
        """把仍有效的预览标记为失效；已消费/不存在返回 ``False``。"""
        key = str(preview_id or "").strip()
        current = self._now(now)
        with self._lock:
            record, code = self.get(key, now=current)
            if code or record is None:
                return False
            record.invalidated_at = current
            record.invalidated_reason = str(reason or "preview_invalidated")
            return True

    def invalidate_all(self, reason: str = "preview_invalidated",
                       now: float | None = None) -> int:
        """把所有仍可用的预览一次性作废，返回作废数量。

        用途：云同步被关闭（``wps_enabled`` True→False）时必须**立刻**作废所有未用
        令牌，而不是等下一次上传才"懒作废" —— 否则客户端在关闭窗口内一次上传都
        不调用，重新开启后旧 ``preview_id`` 仍能真的写云端。
        """
        current = self._now(now)
        count = 0
        with self._lock:
            for record in self._items.values():
                if record.consumed_at is not None or record.invalidated_at is not None:
                    continue
                if current - record.created_at > record.ttl_seconds:
                    continue
                record.invalidated_at = current
                record.invalidated_reason = str(reason or "preview_invalidated")
                count += 1
        return count

    def public(self, record: WpsPreviewRecord,
               now: float | None = None) -> dict[str, Any]:
        """令牌的对外安全视图（不含计划全文与个人信息）。"""
        current = self._now(now)
        return {
            "preview_id": record.preview_id,
            "created_at": _dt.datetime.fromtimestamp(record.created_at).isoformat(
                timespec="seconds"),
            "expires_at": _dt.datetime.fromtimestamp(record.expires_at).isoformat(
                timespec="seconds"),
            "expires_in": max(0, int(record.expires_at - current)),
            "ttl_seconds": record.ttl_seconds,
            "local_sha256": record.local_sha256,
            "context_fingerprint": record.context_fingerprint,
            "plan_fingerprint": record.plan_fingerprint,
            "fingerprint": {
                "local_sha256": record.local_sha256,
                "context": record.context_fingerprint,
                "plan": record.plan_fingerprint,
            },
            "target_date": record.target_date,
            "target_tables": copy.deepcopy(record.target_tables),
            "state": record.state(now=current),
        }


__all__ = [
    "MAX_PREVIEW_ENTRIES",
    "PREVIEW_TTL_SECONDS",
    "PreviewStore",
    "WpsPreviewRecord",
    "canonical_plan",
    "fingerprint_payload",
    "plan_fingerprint",
    "sha256_file",
]
