"""pywebview 桥接层：把业务模块暴露给 Web 前端（js_api + 事件推送）。

职责边界：本模块只做「UI 协议」，业务规则全部留在原模块
（automation/sss/processing/updater/credentials/config）。旧 Tkinter 界面
（app/gui.py）里的每一条用户交互在这里都有对应实现，迁移对照表见
tests 与 README。

事件协议（Python → JS，经 ``drain_events(last_sequence, ack_sequence)``）：
每条事件都是 ``{event, payload, event_id, sequence, created_at, timestamp, droppable}``；
前端只在成功应用后推进 cursor，ACK 会推动已确认事件删除；未 ACK 的仍可重放，
sequence 中间缺口和关键事件超限都会返回 ``events:dropped`` 告警。
- log                {ts, level, msg}          结构化日志行
- status             {state}                   ready/running/stopping/success/partial/stopped/error/updating
- task:done          {message, stopped, partial}
- task:error         {message}
- update:available   {tag, current, body, can_auto_install}
- update:latest      {manual}
- update:error       {message}
- update:progress    {downloaded, total}
- update:stage       {stage}
- update:install_error {message}
- update:installed   {message}
- decision           {id, kind, title, message, choices}
- captcha            {id, image}
- address_input      {id, title, message, items[{raw_address, order_numbers,
                     campus, reason, suggested_point}]}
- events:dropped     {dropped_count, first_available_sequence, message}
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import logging
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path as _Path
from typing import Any

from . import __version__
from .api_client import SssApiClient
from .operations import OperationCoordinator
from .redact import redact
from .automation import (
    BrowserNotFoundError,
    browser_description,
    browser_version_warning,
    ensure_browser,
    parse_target_date,
    run_job,
)
from .config import AppConfig, clamp_split_ratio, default_wps_address_order
from .credentials import (delete_password, delete_sss_password, get_password,
                          get_sss_password, set_password, set_sss_password)
from .excel_templates import write_order_template, write_sss_template
from .sss import expected_delivery_date, run_sss_job
from .sss_import import ImportRefused, prepare_day_orders
from .sss_journal import (UncertainJournalError, default_uncertain_path,
                          platform_origin)
from .sss_review import (SCAN_FAILED, pending_views, resolve_records as
                         resolve_uncertain_records, start_review)
from .updater import ReleaseInfo, UpdateError, check_for_update, download_and_install
from .wps_cloud import (KdocsCli, SyncLedger, WpsCloudError,
                        apply_plan, build_plan, effective_tables, format_plan,
                        read_local_orders, read_local_orders_from_bytes,
                        summarize_plan, target_date_for)
from .wps_journal import JournalError, SyncJournal, journal_path_for
from .wps_preview import (PREVIEW_TTL_SECONDS, PreviewStore, canonical_plan,
                          plan_fingerprint)
from .wps_recovery import (recovery_status, recovery_status_error,
                           resolve_pending_operation)
from .wps_summary import execution_summary, planned_summary

logger = logging.getLogger(__name__)

EXCEL_EXTS = {".xlsx", ".xlsm"}
# pywebview 的文件过滤器在 Win/GTK/Cocoa 三端统一使用 "描述 (*.a;*.b)" 写法。
FILE_DIALOG_FILTERS = ["Excel 工作簿 (*.xlsx)", "Excel 启用宏的工作簿 (*.xlsm)", "所有文件 (*)"]

MAX_ORDER_COUNT = 9999

# 事件重放窗口：仅限制可丢失日志的内存占用，关键事件不按固定容量淘汰。
EVENT_HISTORY_LIMIT = 2000
EVENT_DRAIN_LIMIT = 500
# ACK 即代表前端已成功应用并持久化 cursor；确认后即可删除，未 ACK 的仍可重放。
EVENT_ACK_RETAIN = 0
# 关键事件不参与普通淘汰，但必须有上限，否则前端长期不轮询会无限增长。
CRITICAL_EVENT_LIMIT = 500
DROPPABLE_EVENTS = frozenset({"log", "update:progress", "update:stage"})
# 交互请求（decision/captcha）等待上限；到点后 worker 必须能退出，而不是永久阻塞。
DEFAULT_INTERACTION_TIMEOUT_S = 300.0

# 「核对副本」的并发度：每张子表要读 2 张云表，6 张子表串行时最多 12 次往返。
# 并发只改变往返的重叠方式：**正常路径（含全部 5 种 status 与 WpsCloudError）
# 的调用次数与参数完全不变**，因此不额外消耗云端每日额度；只有出现
# ``WpsCloudError`` 之外的意外异常时，最坏会多发 workers-1 次调用（见下方分批
# 提交的注释），串行版本则是 0 次。
#
# 为什么是 2 而不是 4：金山接口的 429002 表示「短时间频繁触发」，而
# ``_TRANSIENT_HINTS`` 并不包含限流提示，`_run` 不会重试它 —— 一旦突发触发，
# 该表就会从「已核对」变成「读不了」，属于可观测的行为偏差。取 2 只把瞬时速率
# 翻倍（串行约 5 次/秒 → 并发约 10 次/秒），在明显提速与不制造突发之间取平衡；
# 这也是项目里最保守的既有取值（app/sss.py 的验证码/登录轮询同样用 2）。
WPS_COPY_CHECK_WORKERS = 2

RETRY_CHOICES = [{"value": "retry", "label": "重试", "style": "primary"},
                 {"value": "skip", "label": "跳过", "style": "neutral"},
                 {"value": "stop", "label": "停止", "style": "danger"}]


class _InteractionCancelled(RuntimeError):
    """交互请求被取消/超时，worker 必须按取消路径退出。"""


@dataclass
class _PendingInteraction:
    event: threading.Event
    holder: list[Any] = field(default_factory=list)
    kind: str = ""
    created_at: float = 0.0


class Bridge:
    """js_api 对象。公开方法（无下划线）均可被前端 Promise 调用。"""

    def __init__(self, config_path: os.PathLike[str] | str | None = None) -> None:
        self._window: Any = None
        # config_path 供测试注入临时配置文件；生产环境沿用默认用户配置目录。
        self._config = AppConfig.load(config_path) if config_path else AppConfig.load()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._closing = False
        self._status = "ready"
        self._reports: list[dict[str, Any]] = []
        # 事件使用「保留 + cursor 重放」而不是「取出即删除」；producer_id 用于
        # 前端识别 Python 进程重启后 sequence 归零，避免误推进旧 cursor。
        self._event_log: deque[dict[str, Any]] = deque()
        self._event_seq = 0
        self._event_producer_id = secrets.token_hex(8)
        self._event_ack_sequence = 0
        self._event_dropped_count = 0
        self._critical_dropped_count = 0
        # 被淘汰事件的 sequence 区间 (start, end, critical_count)，用于精确告警。
        self._event_dropped_ranges: deque[tuple[int, int, int]] = deque()
        self._push_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        # 任务线程当前持有的操作占位（在 _finish_task/_task_error 里释放）。
        self._worker_operation: Any = None
        self._update_checking = False
        self._update_check_operation: Any = None
        self._pending_release: ReleaseInfo | None = None
        self._decision_seq = 0
        self._decisions: dict[str, _PendingInteraction] = {}
        self._interaction_timeout_s = DEFAULT_INTERACTION_TIMEOUT_S
        # 一次性预览令牌表（10 分钟）：wps_upload 必须带 preview_id。
        self._previews = PreviewStore(ttl_seconds=PREVIEW_TTL_SECONDS)
        # 统一操作互斥：订单、闪时送、只读核对、WPS 预览/上传/授权/恢复、更新。
        self._operations = OperationCoordinator()

    # ------------------------------------------------------------------
    # 事件通道：保留窗口 + 前端 cursor 拉取
    #
    # 不用 evaluate_js 推送——它在 WebKitGTK 上并发调用会静默丢结果
    # （症状：日志行成对丢失）。Python 只追加事件、保留重放窗口，前端用
    # ``last_sequence`` 拉取并在成功应用后推进 cursor；事件不会“取出即删”。
    # 关键事件（decision/captcha/task:*/update:*）不参与固定容量淘汰，只有
    # 普通日志/进度会被丢弃，并通过 ``events:dropped`` 明确告警。
    # ------------------------------------------------------------------
    def attach(self, window: Any) -> None:
        """把 pywebview 窗口对象交给桥接层，供窗口动作与关闭流程使用。"""
        self._window = window

    @staticmethod
    def _is_droppable_event(event: str) -> bool:
        return event in DROPPABLE_EVENTS

    def _record_dropped_locked(self, sequence: int, *, critical: bool = False) -> None:
        critical_count = 1 if critical else 0
        entries = list(self._event_dropped_ranges)
        entries.append((sequence, sequence, critical_count))
        entries.sort(key=lambda item: item[0])
        merged: list[tuple[int, int, int]] = []
        for start, end, critical_in_range in entries:
            if merged and start <= merged[-1][1] + 1:
                prev_start, prev_end, prev_critical = merged[-1]
                merged[-1] = (prev_start, max(prev_end, end), prev_critical + critical_in_range)
            else:
                merged.append((start, end, critical_in_range))
        self._event_dropped_ranges = deque(merged)
        self._event_dropped_count += 1
        if critical:
            self._critical_dropped_count += 1

    def _prune_event_log_locked(self) -> None:
        """先按 ACK 清理，再限制总量，最后对关键事件做显式上限告警。"""
        # 1) ACK 推动删除；只保留少量已确认事件，保证页面刷新/重连仍可重放最近一段。
        while (len(self._event_log) > EVENT_ACK_RETAIN
               and int(self._event_log[0].get("sequence") or 0) <= self._event_ack_sequence):
            self._event_log.popleft()
        while self._event_dropped_ranges and self._event_dropped_ranges[0][1] <= self._event_ack_sequence:
            self._event_dropped_ranges.popleft()

        # 2) 总量超限时优先淘汰普通日志/进度；关键事件保留。
        while len(self._event_log) > EVENT_HISTORY_LIMIT:
            dropped = False
            for index, item in enumerate(self._event_log):
                if item.get("droppable"):
                    sequence = int(item.get("sequence") or 0)
                    del self._event_log[index]
                    self._record_dropped_locked(sequence, critical=False)
                    dropped = True
                    break
            if not dropped:
                break

        # 3) 关键事件上限：超过时淘汰最旧的关键事件，并记录 critical 丢失区间。
        while True:
            critical_items = [item for item in self._event_log if not item.get("droppable")]
            if len(critical_items) <= CRITICAL_EVENT_LIMIT:
                break
            oldest = critical_items[0]
            sequence = int(oldest.get("sequence") or 0)
            try:
                self._event_log.remove(oldest)
            except ValueError:  # pragma: no cover - 单线程锁内不应发生
                break
            self._record_dropped_locked(sequence, critical=True)

    def _emit_event(self, event: str, payload: Any = None) -> None:
        with self._push_lock:
            self._event_seq += 1
            sequence = self._event_seq
            now = time.time()
            envelope = {
                "event": event,
                # event_type 与 event 同义，兼容审计报告建议的事件协议字段名。
                "event_type": event,
                "payload": payload,
                "event_id": f"{self._event_producer_id}:{sequence}",
                "sequence": sequence,
                "created_at": now,
                # timestamp 与 created_at 同义，兼容审计报告建议字段名。
                "timestamp": now,
                "droppable": self._is_droppable_event(event),
            }
            self._event_log.append(envelope)
            self._prune_event_log_locked()

    def _dropped_notices_locked(self, cursor: int) -> list[dict[str, Any]]:
        """把丢失区间转换为明确的事件；sequence 取区间末尾，避免同一缺口重复告警。"""
        notices: list[dict[str, Any]] = []
        for start, end, critical_count in list(self._event_dropped_ranges):
            effective_start = max(start, cursor + 1)
            if effective_start > end:
                continue
            dropped_count = end - effective_start + 1
            critical_dropped = min(critical_count, dropped_count)
            now = time.time()
            notices.append({
                "event": "events:dropped",
                "event_type": "events:dropped",
                "payload": {
                    "dropped_count": dropped_count,
                    "critical_dropped_count": critical_dropped,
                    "first_sequence": effective_start,
                    "last_sequence": end,
                    "total_dropped": self._event_dropped_count,
                    "total_critical_dropped": self._critical_dropped_count,
                    "message": (
                        f"事件队列繁忙，已丢弃 sequence {effective_start}-{end} 的 "
                        f"{dropped_count} 条事件"
                        + (f"（其中关键事件 {critical_dropped} 条）" if critical_dropped else "")
                    ),
                },
                # 取区间末尾：前端应用后 cursor 直接越过整个缺口。
                "sequence": end,
                "event_id": f"{self._event_producer_id}:dropped:{start}-{end}",
                "created_at": now,
                "timestamp": now,
                "droppable": False,
                "synthetic": True,
            })
        return notices

    def drain_events(self, last_sequence: int = 0, ack_sequence: int | None = None,
                     producer_id: str = "") -> dict[str, Any]:
        """按 cursor 拉取事件；前端应用成功后用 ``ack_sequence`` 确认。

        ACK 会推动已确认事件删除（保留少量尾部用于重连重放）；sequence
        中间的缺口也会生成明确的 ``events:dropped`` 告警，而不是只跳号。
        """
        try:
            cursor = int(last_sequence or 0)
        except (TypeError, ValueError):
            cursor = 0
        with self._push_lock:
            # 前端可能仍持有上一进程的 producer_id/ACK；只接受当前生产者的 ACK，
            # 且 ACK 不得超过当前已分配的最大 sequence，避免误删新进程事件。
            ack_producer_ok = not producer_id or str(producer_id) == self._event_producer_id
            if ack_sequence is not None and ack_producer_ok:
                try:
                    requested_ack = int(ack_sequence)
                    if 0 <= requested_ack <= self._event_seq:
                        self._event_ack_sequence = max(self._event_ack_sequence, requested_ack)
                except (TypeError, ValueError):
                    pass
            self._prune_event_log_locked()
            available = [item for item in self._event_log
                         if int(item.get("sequence") or 0) > cursor]
            notices = self._dropped_notices_locked(cursor)
            combined = available + notices
            combined.sort(key=lambda item: int(item.get("sequence") or 0))
            events = combined[:EVENT_DRAIN_LIMIT]
            return {
                "events": events,
                "producer_id": self._event_producer_id,
                "latest_sequence": self._event_seq,
                "acked_sequence": self._event_ack_sequence,
                "dropped_count": self._event_dropped_count,
                "critical_dropped_count": self._critical_dropped_count,
                "first_available_sequence": int(self._event_log[0].get("sequence") or 0) if self._event_log else self._event_seq + 1,
            }

    def log(self, message: str, level: str = "INFO") -> None:
        """向前端推一条日志行（``event="log"``，含 ``ts``/``level``/``msg``）。

        出口统一脱敏（手机号 / ``password=…`` 形态的凭据 / Bearer 头）：
        日志会被用户复制粘贴、也可能进入验收证据，不能成为泄露通道。
        """
        self._emit_event("log", {"ts": time.strftime("%H:%M:%S"), "level": level,
                                 "msg": redact(message).rstrip()})

    def _set_status(self, state: str) -> None:
        self._status = state
        self._emit_event("status", {"state": state})

    @property
    def status(self) -> str:
        """当前任务状态（``ready``/``running``/``stopping``/``success``/``partial``/``stopped``/``error``/``updating``）。"""
        return self._status

    # ------------------------------------------------------------------
    # js_api：前端握手与初始状态
    # ------------------------------------------------------------------
    def echo_test(self, message: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """带参调用诊断：验证 pywebview 6 GTK 的 js_api 参数序列化是否正常。"""
        return {"echo": message, "payload_keys": sorted(payload.keys()) if isinstance(payload, dict) else None}

    def bridge_ready(self) -> dict[str, Any]:
        """前端装载完成后的握手。返回初始状态并冲积未发送事件。"""
        config = self._config
        state: dict[str, Any] = {
            "version": __version__,
            "status": self._status,
            "frozen": bool(getattr(sys, "frozen", False)),
            # 前端据此识别 Python 进程重启，避免旧 cursor 与新的 sequence 冲突。
            "event_producer_id": self._event_producer_id,
            "config": {
                "target_url": config.target_url,
                "phone_number": config.phone_number,
                "excel_path": str(config.excel_path) if config.excel_path else "",
                "order_date": config.order_date,
                "order_count": config.order_count,
                "split_ratio": config.split_ratio,
                "sss_url": config.sss_url,
                "sss_account": config.sss_account,
                "sss_excel_path": str(config.sss_excel_path) if config.sss_excel_path else "",
                "sss_order_source": config.sss_order_source,
                "sss_product_name": config.sss_product_name,
                "sss_common_address": config.sss_common_address,
                "sss_use_fixed_address": config.sss_use_fixed_address,
                "sss_fixed_lnt": config.sss_fixed_lnt,
                "sss_fixed_lat": config.sss_fixed_lat,
                "sss_fixed_area_code": config.sss_fixed_area_code,
                "sss_fixed_address_detail": config.sss_fixed_address_detail,
                "sss_dry_run": config.sss_dry_run,
                "sss_preflight": config.sss_preflight,
                "sss_idempotency_field": config.sss_idempotency_field,
                "api_mode": config.api_mode,
                "wps_enabled": config.wps_enabled,
                "wps_test_mode": config.wps_test_mode,
                "wps_test_file_id": config.wps_test_file_id,
                "wps_test_drive_id": config.wps_test_drive_id,
                "wps_test_tables": dict(config.wps_test_tables),
                "wps_drive_id": config.wps_drive_id,
                "wps_cli_path": config.wps_cli_path,
                "wps_tables": dict(config.wps_tables),
                "wps_target_hour_start": config.wps_target_hour_start,
                "wps_target_hour_end": config.wps_target_hour_end,
                "wps_marker_enabled": config.wps_marker_enabled,
            },
            # 与旧 GUI 启动行为一致：按账号从系统凭据管理器读回密码。
            "passwords": {
                "order": get_password(config.phone_number) if config.phone_number else "",
                "sss": get_sss_password(config.sss_account) if config.sss_account else "",
            },
        }
        return state

    # ------------------------------------------------------------------
    # js_api：任务启动/停止（校验逻辑移植自旧 _validate_form/_validate_sss_form）
    # ------------------------------------------------------------------
    def start_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """校验订单表单并启动「订单处理」任务。

        校验失败返回 ``{"ok": False, "fields": {字段: {"message": ...}}}``（**不写配置、
        不写密钥链、不起线程**）；已有任务在跑时返回 ``{"ok": False, "reason": "busy"}``。
        只有全部校验通过才就地更新订单侧配置、落盘、（``remember`` 为真时）保存密码，
        并把配置**深拷贝**交给运行线程。
        """
        if self.worker_alive():
            return {"ok": False, "reason": "busy", "message": "已有任务正在运行，请先停止后再启动", "fields": {}}
        operation, conflict = self._reserve(
            "order", title="订单处理", next_action="等待订单处理结束后重试")
        if conflict is not None:
            return {"ok": False, "reason": "operation_conflict",
                    "message": conflict["reason"], "fields": {},
                    "next_action": conflict["next_action"]}
        fields: dict[str, dict[str, str]] = {}
        url = str(payload.get("url", "")).strip()
        phone = str(payload.get("phone", "")).strip()
        password = str(payload.get("password", ""))
        excel = str(payload.get("excel", "")).strip()
        date_text = str(payload.get("date", "")).strip()
        count_text = str(payload.get("count", "")).strip()
        count: int | None = None
        if count_text:
            try:
                count = int(count_text)
            except (TypeError, ValueError):
                count = 0
            if not 1 <= count <= MAX_ORDER_COUNT:
                fields["count"] = {"message": f"请输入 1～{MAX_ORDER_COUNT} 的整数，或留空处理全部"}
        if not url:
            fields["url"] = {"message": "请输入管理网址"}
        if not phone:
            fields["phone"] = {"message": "请输入手机号或账号"}
        if not password:
            fields["password"] = {"message": "请输入登录密码"}
        excel_error = _excel_field_error(excel)
        if excel_error:
            fields["excel"] = {"message": excel_error}
        try:
            parse_target_date(date_text)
        except ValueError as exc:
            fields["date"] = {"message": str(exc)}
        if fields:
            self._set_status("error")
            self._operations.finish(operation, status="rejected",
                                    reason="validation_failed")
            return {"ok": False, "fields": fields}

        # 就地更新已加载配置并保存，避免用「全默认值新对象」覆盖另一半模式
        # （跑一次订单任务就把闪时送配置重置成默认值的同源问题）。
        # 从占位开始，任何一步失败（写盘、密钥链、起线程）都必须释放占位：
        # 泄漏的占位会表现为"永久忙"，之后所有危险操作都被拒绝。
        def _start() -> dict[str, Any]:
            _apply_order_payload(self._config, {
                "url": url, "phone": phone, "excel": excel, "date": date_text,
                "count": count, "api_mode": bool(payload.get("api_mode", True)),
            })
            self._config.save()
            if payload.get("remember", True):
                set_password(phone, password)
            # 运行线程拿到配置快照，避免任务执行期间被后续防抖保存改写。
            self._worker_operation = operation
            if self._launch("order", copy.deepcopy(self._config), count,
                            password) is False:
                return {"ok": False, "reason": "busy",
                        "message": "已有任务正在运行，请先停止后再启动",
                        "fields": {}}
            return {"ok": True}

        return self._guard_reserved(operation, _start, label="订单处理")

    def start_sss(self, payload: dict[str, Any]) -> dict[str, Any]:
        """校验闪时送表单并启动「闪时送下单」任务。

        与 :meth:`start_order` 同一套路：校验失败只回 ``fields``，不动配置；
        已有任务时回 ``reason="busy"``；成功则就地更新闪时送侧配置后起线程。
        """
        if self.worker_alive():
            return {"ok": False, "reason": "busy", "message": "已有任务正在运行，请先停止后再启动", "fields": {}}
        operation, conflict = self._reserve(
            "sss", title="闪时送下单", next_action="等待闪时送任务结束后重试")
        if conflict is not None:
            return {"ok": False, "reason": "operation_conflict",
                    "message": conflict["reason"], "fields": {},
                    "next_action": conflict["next_action"]}
        fields = {}
        url = str(payload.get("url", "")).strip()
        account = str(payload.get("account", "")).strip()
        password = str(payload.get("password", ""))
        excel = str(payload.get("excel", "")).strip()
        if not url:
            fields["url"] = {"message": "请输入闪时送网址"}
        if not account:
            fields["account"] = {"message": "请输入闪时送账号"}
        if not password:
            fields["password"] = {"message": "请输入登录密码"}
        excel_error = _excel_field_error(excel)
        order_source = str(payload.get("order_source", self._config.sss_order_source) or "").strip().lower()
        order_source = "excel" if order_source == "excel" else "wps"
        # 云端模式下《闪时送.xlsx》只是留档目标：没选文件、文件不在也能下单，
        # 只在下单日志里提示“跳过留档”。本地 Excel 模式仍需严格校验。
        if order_source == "excel" and excel_error:
            fields["excel"] = {"message": excel_error}
        use_fixed_address = bool(payload.get("use_fixed_address", False))
        fixed_lnt = str(payload.get("fixed_lnt", "")).strip()
        fixed_lat = str(payload.get("fixed_lat", "")).strip()
        fixed_area_code = str(payload.get("fixed_area_code", "")).strip()
        fixed_address_detail = str(payload.get("fixed_address_detail", "")).strip()
        if use_fixed_address:
            try:
                float(fixed_lnt)
            except (TypeError, ValueError):
                fields["fixed_lnt"] = {"message": "请输入有效的经度"}
            try:
                float(fixed_lat)
            except (TypeError, ValueError):
                fields["fixed_lat"] = {"message": "请输入有效的纬度"}
            if not fixed_area_code:
                fields["fixed_area_code"] = {"message": "请输入地区编码"}
            if not fixed_address_detail:
                fields["fixed_address_detail"] = {"message": "请输入详细地址"}
        if fields:
            self._set_status("error")
            self._operations.finish(operation, status="rejected",
                                    reason="validation_failed")
            return {"ok": False, "fields": fields}

        # 就地更新并保存：避免全新 AppConfig 把订单处理侧配置重置成默认。
        # 同样走 _guard_reserved：写盘/密钥链/起线程失败都要释放占位。
        def _start() -> dict[str, Any]:
            return self._start_sss_locked(payload, password, operation)
        return self._guard_reserved(operation, _start, label="闪时送下单")

    def _start_sss_locked(self, payload: dict[str, Any], password: str,
                          operation: Any) -> dict[str, Any]:
        """``start_sss`` 占位成功之后的实际逻辑（含写配置与起线程）。

        这里重新从 ``payload``/配置取值，而不是依赖 ``start_sss`` 的局部变量：
        校验与启动被拆成两段（中间隔着占位管理），只有重新取值才能避免
        "在另一段里引用了不存在的局部名"这类错误。
        """
        url = str(payload.get("url", "")).strip()
        account = str(payload.get("account", "")).strip()
        excel = str(payload.get("excel", "")).strip()
        source = str(payload.get("order_source",
                                 self._config.sss_order_source) or "").strip().lower()
        order_source = "excel" if source == "excel" else "wps"
        use_fixed_address = bool(payload.get("use_fixed_address", False))
        fixed_lnt = str(payload.get("fixed_lnt", "")).strip()
        fixed_lat = str(payload.get("fixed_lat", "")).strip()
        fixed_area_code = str(payload.get("fixed_area_code", "")).strip()
        fixed_address_detail = str(payload.get("fixed_address_detail", "")).strip()
        _apply_sss_payload(self._config, {
            "url": url, "account": account, "excel": excel,
            "order_source": order_source,
            "product_name": str(payload.get("product_name", "轻食")).strip() or "轻食",
            "common_address": str(payload.get("common_address", "")).strip(),
            "use_fixed_address": use_fixed_address,
            "fixed_lnt": fixed_lnt, "fixed_lat": fixed_lat,
            "fixed_area_code": fixed_area_code,
            "fixed_address_detail": fixed_address_detail,
            # dry_run：只组装并打印下单报文，不真实提交（联调/验收用）。
            "dry_run": bool(payload.get("dry_run", self._config.sss_dry_run)),
            "preflight": bool(payload.get("preflight", self._config.sss_preflight)),
            "api_mode": bool(payload.get("api_mode", True)),
        })
        self._merge_sss_store_cache_from_disk()
        self._config.save()
        if payload.get("remember", True):
            set_sss_password(account, password)
        self._worker_operation = operation
        if self._launch("sss", copy.deepcopy(self._config), None, password) is False:
            return {"ok": False, "reason": "busy",
                    "message": "已有任务正在运行，请先停止后再启动", "fields": {}}
        return {"ok": True}

    # 表单防抖即时保存：只落盘本次改动，不做启动校验、不触发任务。
    def save_order_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """只更新订单处理侧的配置字段并落盘（不触碰闪时送侧）。写盘失败回 ``write_failed``。"""
        _apply_order_payload(self._config, payload)
        try:
            self._config.save()
        except OSError:
            return {"ok": False, "reason": "write_failed"}
        return {"ok": True, "saved": {
            "order_date": self._config.order_date,
            "order_count": self._config.order_count,
        }}

    def save_sss_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """只更新闪时送侧的配置字段并落盘（不触碰订单处理侧）。写盘失败回 ``write_failed``。"""
        _apply_sss_payload(self._config, payload)
        try:
            self._config.save()
        except OSError:
            return {"ok": False, "reason": "write_failed"}
        return {"ok": True}

    def sss_day_orders(self) -> dict[str, Any]:
        """读取云端当天名单（东湖午餐/东湖晚餐）并留档，但不下单。

        「闪时送下单」页签的「读取云端当天名单」按钮用它：只读取 + 写留档
        Excel + 汇报人数，方便下单前先核对日期与名单。任何失败都只返回原因，
        不会触碰下单流程。
        """
        if self.worker_alive():
            return {"ok": False, "reason": "已有任务正在运行，请先停止后再读取当天名单"}
        # 只读入口也纳入统一协调器：它同样会消耗云端每日额度，并可能写留档 Excel
        # （而留档文件正是闪时送下单的名单来源），与上传/下单并发会互相干扰。
        operation, conflict = self._reserve(
            "sss_day_orders", title="读取云端当天名单",
            summary={"read_only": True, "cloud_write": False})
        if conflict is not None:
            conflict.update({"read_only": True, "cloud_write": False})
            return conflict
        try:
            return self._sss_day_orders_impl()
        finally:
            self._operations.finish(operation, status="success")

    def _sss_day_orders_impl(self) -> dict[str, Any]:
        """``sss_day_orders`` 的实际实现（调用方已取占位）。"""
        try:
            day = prepare_day_orders(self._config,
                                     delivery_date=expected_delivery_date(),
                                     log=lambda message: self.log(message))
        except ImportRefused as exc:
            self.log("读取云端当天名单失败：" + str(exc), "ERROR")
            return {"ok": False, "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 界面需要原样原因
            self.log("读取云端当天名单失败：" + str(exc), "ERROR")
            return {"ok": False, "reason": str(exc)}

        summary = day.as_summary()
        parts = []
        for name, info in (summary.get("meals") or {}).items():
            if info.get("skipped"):
                parts.append(f"{name}不下单（{info.get('reason') or '没有当天列'}）")
            else:
                parts.append(
                    f"{name} {info.get('orders', 0)} 人"
                    f"（标 1 共 {info.get('marked', 0)} 人，"
                    f"其中 {info.get('skipped_address', 0)} 人地址是大西/小不下单）")
        self.log(f"云端当天名单 {summary.get('target_date')} "
                 f"{summary.get('date_text')}：" + ("；".join(parts) or "没有数据"), "OK")
        if summary.get("archive_error"):
            self.log("留档 Excel 写入失败：" + str(summary["archive_error"]), "WARN")
        return {"ok": True, **summary}

    # ------------------------------------------------------------------
    # 闪时送：未决记录列表 / 只读核对 / 人工处置
    #
    # 这三个入口是"上一次下单结果未知"时的唯一出路：只读核对查站内订单并留
    # 证据；人工处置只写本地日志，**永不写云端**、不创建订单。解除阻断后，
    # 用户下一次主动点「开始下单」才会真正发单。
    # ------------------------------------------------------------------
    def _sss_journal_path(self) -> _Path:
        """权威未决日志路径。

        生产环境就是固定的用户数据目录（与 ``sss_journal.default_uncertain_path``
        一致）；只在**显式指定了非默认配置文件**（测试/独立安装）时，才把状态
        放在配置文件旁边，避免测试碰到真实用户的日志。

        刻意**不**从网址/账号/名单来源/Excel 路径推导 —— 那些都是用户可改的
        业务配置，一旦参与文件名，改一下配置就能换一个日志、看不到原来的未决记录。
        """
        explicit = str(getattr(self._config, "sss_uncertain_path", "") or "").strip()
        if explicit:
            return _Path(explicit).expanduser()
        config_path = getattr(self._config, "config_path", None)
        if config_path and _Path(config_path) != AppConfig.default_path():
            return _Path(config_path).parent / "sss_uncertain.json"
        return default_uncertain_path(self._config)

    def _sss_scope(self) -> tuple[str, str, str]:
        """本次运行的 ``(送达日, 账号, 平台 origin)``；网址非法时抛错。"""
        account = str(self._config.sss_account or "")
        origin = platform_origin(self._config)
        return (expected_delivery_date().isoformat(), account, origin)

    def sss_uncertain_records(self) -> dict[str, Any]:
        """列出闪时送未决记录（脱敏）；日志损坏时明确报错而不是谎称"没有"。"""
        journal_path = self._sss_journal_path()
        try:
            delivery_date, account, _origin = self._sss_scope()
        except UncertainJournalError as exc:
            return {"ok": False, "reason": str(exc), "records": [], "counts": {},
                    "journal_unreadable": False, "next_action": "修正闪时送网址"}
        try:
            result = pending_views(journal_path, delivery_date=delivery_date,
                                   account=account, origin=_origin)
        except UncertainJournalError as exc:
            # 关键：损坏时绝不能返回空列表伪装成"没有未决记录"。
            self.log(f"[闪时送] 未决日志不可用：{exc}", "ERROR")
            return {"ok": False, "reason": str(exc), "records": [], "counts": {},
                    "journal_unreadable": True, "error_code": "journal_unreadable",
                    "journal_path": str(journal_path),
                    "next_action": "先人工核对站内订单并修复/移走该文件",
                    "delivery_date": delivery_date, "account": account}
        group_counts = result.get("group_counts") or {}
        other = int(group_counts.get("other_scope", 0) or 0)
        history = int(group_counts.get("history", 0) or 0)
        hints: list[str] = []
        if result["counts"].get("active"):
            hints.append("先做「只读核对」或人工处置")
        if other:
            hints.append("其它范围仍有未决记录：切回原账号/原平台后再核对与处置"
                         "（当前账号/网址下的核对结果不能用于它们）")
        next_action = "；".join(hints)
        result.update({
            "delivery_date": delivery_date,
            "account": account,
            "origin": _origin,
            "other_scope_count": other,
            "history_count": history,
            "next_action": next_action,
        })
        return result

    def start_sss_review(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """闪时送**只读核对**：登录、查订单、保存核对快照。

        只允许登录、查询订单、查询目标时间窗和保存核对快照；**严禁**创建订单
        请求或自动补发（登录本身可能用 POST，"只读"指对远端订单没有写副作用）。

        可靠匹配到的未决记录会被标记为本地已确认（因此本地文件会变），仍然缺失
        或无法确认的记录只生成证据。本入口不会因为 ``remember`` 保存密码。
        """
        data: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
        if self.worker_alive():
            return {"ok": False, "reason": "已有任务正在运行，请先停止后再核对",
                    "records": [], "next_action": "停止当前任务"}
        operation, conflict = self._reserve(
            "sss_review", title="闪时送只读核对",
            summary={"read_only": True, "cloud_write": False})
        if conflict is not None:
            conflict.update({"records": [], "cloud_write": False})
            return conflict
        try:
            result = self._start_sss_review_impl(data)
        except BaseException:
            self._operations.finish(operation, status="error", reason="unexpected")
            raise
        status = "success" if result.get("ok") else "error"
        self._operations.finish(operation, status=status,
                                reason=str(result.get("reason") or ""))
        return result

    def _start_sss_review_impl(self, data: dict[str, Any]) -> dict[str, Any]:
        """``start_sss_review`` 的实际实现（调用方已取占位）。"""
        journal_path = self._sss_journal_path()
        try:
            delivery_date, account, _origin = self._sss_scope()
        except UncertainJournalError as exc:
            return {"ok": False, "reason": str(exc), "records": [],
                    "next_action": "修正闪时送网址"}
        if not account:
            return {"ok": False, "reason": "尚未填写闪时送账号", "records": [],
                    "next_action": "在「闪时送下单」里填写账号"}
        password = str(data.get("password") or "").strip() or get_sss_password(account)
        if not password:
            return {"ok": False, "reason": "没有可用于只读核对的闪时送密码",
                    "records": [], "next_action": "输入密码后重试（不会保存）"}
        url = str(self._config.sss_url or "").strip()
        try:
            read_timeout = float(getattr(self._config, "sss_read_timeout_s", 20.0))
        except (TypeError, ValueError):
            read_timeout = 20.0
        self.log("[闪时送] 开始只读核对：登录 → 查询订单 → 保存核对快照（不下单）")
        client = None
        try:
            client = SssApiClient(url, account, password,
                                  timeout=(5.0, max(1.0, min(120.0, read_timeout))),
                                  pool_size=1)
            captcha = client.fetch_captcha()
            client.login(self._sss_captcha(captcha))
            result = start_review(journal_path, delivery_date=delivery_date,
                                  account=account, origin=_origin,
                                  fetch_json=client.get_json,
                                  log=lambda message: self.log(message))
        except _InteractionCancelled as exc:
            return {"ok": False, "reason": f"验证码输入已取消或超时：{exc}",
                    "records": [], "next_action": "重新核对"}
        except Exception as exc:  # noqa: BLE001 - 界面需要原样原因
            self.log(f"[闪时送] 只读核对失败：{type(exc).__name__}: {exc}", "ERROR")
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}",
                    "records": [], "next_action": "检查账号/验证码/网络后重试"}
        finally:
            if client is not None:
                client.close()
        self.log("[闪时送] 只读核对完成："
                 f"站内已确认 {result.get('confirmed', 0)}、"
                 f"站内没有 {result.get('missing', 0)}、"
                 f"日期不符 {result.get('other_day', 0)}、"
                 f"读取失败 {result.get('scan_failed', 0)}", "OK")
        for item in result.get("results") or []:
            if item.get("classification") == SCAN_FAILED:
                self.log(f"[闪时送] ⚠ {item.get('name')}：{item.get('reason')}", "WARN")
        return result

    def sss_uncertain_resolve(self, payload: dict[str, Any] | None = None,
                             **options: Any) -> dict[str, Any]:
        """人工处置闪时送未决记录；**不会发送任何创建订单请求**。

        ``payload`` 需要 ``decision``（``station_present`` / ``station_absent`` /
        ``keep``）、``confirm``（与 decision 完全相同）、``record_ids``，以及
        ``note``（``station_absent`` 至少 4 个字符）。
        ``station_absent`` 还必须有新鲜的只读核对证据，且所选记录全部是
        "站内确认没有"；解除后**下一次主动运行才会真正发单**。
        """
        data: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
        for key, value in options.items():
            if value is not None:
                data[key] = value
        journal_path = self._sss_journal_path()
        try:
            delivery_date, account, origin = self._sss_scope()
        except UncertainJournalError as exc:
            return {"ok": False, "reason": str(exc), "cloud_write": False,
                    "changed": False, "operations": []}
        # 人工处置也纳入统一协调器：它与下单、只读核对、云同步上传互相排斥。
        operation, conflict = self._reserve(
            "sss_uncertain_resolve", title="闪时送未决处置",
            summary={"record_count": len(data.get("record_ids") or []),
                     "cloud_write": False})
        if conflict is not None:
            conflict.update({"cloud_write": False, "changed": False,
                             "operations": []})
            return conflict
        try:
            result = resolve_uncertain_records(
                journal_path, delivery_date=delivery_date, account=account,
                origin=origin,
                decision=str(data.get("decision") or ""),
                record_ids=data.get("record_ids") or [],
                confirm=str(data.get("confirm") or ""),
                note=str(data.get("note") or ""))
        except UncertainJournalError as exc:
            self.log(f"[闪时送] 人工处置失败：未决日志不可用：{exc}", "ERROR")
            self._operations.finish(operation, status="blocked",
                                    reason=str(exc)[:200],
                                    next_action="fix_journal")
            return {"ok": False, "status": "blocked", "code": "journal_unreadable",
                    "reason": str(exc), "next_action": "先修复未决日志",
                    "cloud_write": False, "changed": False, "operations": []}
        except BaseException as exc:
            self._operations.finish(operation, status="error",
                                    reason=f"{type(exc).__name__}: {exc}")
            raise
        self._operations.finish(
            operation, status="success" if result.get("ok") else "rejected",
            reason=str(result.get("reason") or result.get("code") or "")[:200],
            summary={"changed": bool(result.get("changed")),
                     "reason_code": str(result.get("reason_code") or "")},
            next_action=str(result.get("next_action") or ""))
        if result.get("ok") and result.get("changed"):
            self.log(f"[闪时送] 人工处置未决记录：{result.get('reason')}；"
                     f"备注：{str(data.get('note') or '')[:60]}（未发送任何下单请求）",
                     "WARN")
        return result

    def _launch(self, mode: str, config: AppConfig, count: int | None, password: str) -> bool:
        """启动 worker；已有任务时拒绝，返回 False（不覆盖在跑线程）。

        操作占位由调用方（``start_order``/``start_sss``）预先放进
        ``self._worker_operation``，任务结束时由 ``_finish_task``/``_task_error``
        释放；这里抢不到 worker 就把它按"被拒"结掉。
        """
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                self.log("已有任务在运行，拒绝并发启动", "WARN")
                operation, self._worker_operation = self._worker_operation, None
                self._operations.finish(operation, status="rejected", reason="busy",
                                        next_action="等待当前任务结束")
                return False
            self._stop_event.clear()
            self._set_status("running")
            self.log("开始处理订单..." if mode == "order" else "开始闪时送下单...")
            target = self._run_order if mode == "order" else self._run_sss
            args = (config, count, password) if mode == "order" else (config, password)
            self._worker = threading.Thread(target=target, args=args, daemon=True)
            self._worker.start()
            return True

    def _run_order(self, config: AppConfig, count: int | None, password: str) -> None:
        try:
            result = run_job(config, count, self._stop_event, lambda msg: self.log(msg), password=password,
                             order_decision_callback=self._order_decision,
                             save_decision_callback=self._save_decision,
                             pending_address_callback=self._pending_address_input)
            self._finish_task(f"处理完成：已处理 {result.get('processed', '?')} 项，"
                              f"找到 {result.get('found', '?')} 项", result)
        except BrowserNotFoundError as exc:
            self._task_error(str(exc))
        except Exception as exc:
            self._task_error(str(exc))

    def _run_sss(self, config: AppConfig, password: str) -> None:
        try:
            result = run_sss_job(config, self._stop_event, lambda msg: self.log(msg),
                                 password=password, decision_callback=self._sss_decision,
                                 captcha_callback=self._sss_captcha,
                                 store_cache_callback=self._remember_sss_store_cache)
            status = result.get("status")
            source_label = ("本地 Excel" if result.get("source") == "excel"
                            else "云端当天名单")
            if status == "dry_run":
                message = (f"闪时送干跑完成：已组装 {result.get('previewed', 0)} 单"
                           f"（名单来源：{source_label}），未创建真实订单")
            elif status == "no_orders":
                meals = (result.get("import") or {}).get("meals") or {}
                detail = "；".join(
                    f"{name}不下单（{info.get('reason') or '没有当天列'}）"
                    if info.get("skipped") else f"{name} {info.get('orders', 0)} 人"
                    for name, info in meals.items())
                message = "闪时送没有需要下单的订单" + (f"：{detail}" if detail else "")
            elif status == "insufficient_balance":
                message = (f"闪时送已安全停止：余额不足，本批未提交，"
                           f"预计 {result.get('estimate', '?')} 元")
            elif status == "balance_unknown":
                message = "闪时送已安全停止：余额未知，本批未提交"
            elif status == "preflight_ok":
                message = (f"闪时送预检完成：已有站内匹配 {result.get('created', 0)} 单，"
                           "未提交新订单")
            elif status in {"preflight_uncertain", "duplicate_detected"}:
                message = "闪时送预检停止：未提交新订单，请先处理日志中的风险"
            elif result.get("stopped"):
                reconciliation = "已完成站内对账" if result.get("reconciled") else "站内对账失败"
                message = (f"闪时送任务已停止：{reconciliation}，"
                           f"已确认 {result.get('created', '?')}/{result.get('processed', '?')} 单"
                           + ("" if result.get("reconciled") else "，请勿手动重复提交"))
            elif result.get("uncertain") or not result.get("reconciled"):
                result["partial"] = True
                message = ("闪时送任务结束：站内对账失败，无法确认已创建数量，"
                           "请勿手动重复提交")
            elif result.get("partial"):
                created = result.get("created", "?")
                processed = result.get("processed", "?")
                missing = processed - created if isinstance(processed, int) and isinstance(created, int) else "?"
                message = (f"闪时送任务部分完成：已确认 {created}/{processed} 单，"
                           f"{missing} 单未完成")
            else:
                message = (f"闪时送下单完成：已创建 {result.get('created', '?')} 单，"
                           f"处理 {result.get('processed', '?')} 项")
            self._finish_task(message, result)
        except BrowserNotFoundError as exc:
            self._task_error(str(exc))
        except Exception as exc:
            self._task_error(str(exc))

    def _finish_task(self, message: str, result: dict[str, Any]) -> None:
        stopped = bool(result.get("stopped", self._stop_event.is_set()))
        partial = bool(result.get("partial"))
        self._cancel_pending_interactions("任务结束")
        self.log(message, "OK")
        operation, self._worker_operation = self._worker_operation, None
        if operation is not None:
            # 如实透传业务状态（dry_run / preflight_ok / blocked_by_uncertain /
            # no_orders / insufficient_balance …）：终结由 finish() 显式置
            # active=False 决定，不再靠状态词表，所以任何业务词都能安全展示。
            status = str(result.get("status") or "")
            overall = status or ("partial" if partial
                                 else "stopped" if stopped else "success")
            self._operations.finish(
                operation, status=overall,
                reason=str(result.get("reason") or result.get("block_reason") or ""),
                summary={"message": message, "created": result.get("created"),
                         "processed": result.get("processed"),
                         "uncertain_pending": result.get("uncertain_pending")},
                next_action=str(result.get("next_action") or ""))
        self._worker = None
        self._set_status("partial" if partial else "stopped" if stopped else "success")
        self._emit_event("task:done", {
            "message": message,
            "stopped": stopped,
            "partial": partial,
            "result": result,
        })

    def _merge_sss_store_cache_from_disk(self) -> None:
        """避免运行线程写入的门店缓存被下一次启动时的旧内存配置覆盖。"""
        try:
            stored = AppConfig.load(self._config.config_path)
            if stored.sss_store_name_cached == self._config.sss_store_name:
                self._config.sss_store_id = stored.sss_store_id
                self._config.sss_store_name_cached = stored.sss_store_name_cached
        except Exception:
            pass

    def _remember_sss_store_cache(self, store_name: str, store_id: int) -> None:
        """把 worker 配置快照中发现的门店缓存同步回常驻配置。"""
        try:
            self._config.sss_store_id = int(store_id)
            self._config.sss_store_name_cached = str(store_name)
            self._config.save()
        except Exception:
            pass

    def _task_error(self, message: str) -> None:
        self._cancel_pending_interactions("任务异常")
        self.log("错误: " + message, "ERROR")
        operation, self._worker_operation = self._worker_operation, None
        self._operations.finish(operation, status="error", reason=message)
        self._worker = None
        self._set_status("error")
        self._emit_event("task:error", {"message": message})

    def stop_task(self) -> dict[str, Any]:
        """请求停止当前任务（置停止事件并取消等待中的交互）。没有任务在跑时回 ``{"ok": False}``。"""
        if not self._worker or not self._worker.is_alive():
            return {"ok": False}
        self._stop_event.set()
        # 等待 decision/captcha 的 worker 必须被唤醒，否则 stop 后仍永久挂起。
        self._cancel_pending_interactions("用户停止任务")
        self._set_status("stopping")
        self.log("已请求停止，正在等待浏览器操作结束...")
        return {"ok": True}

    def worker_alive(self) -> bool:
        """当前是否有任务线程在运行。"""
        return bool(self._worker and self._worker.is_alive())

    # ------------------------------------------------------------------
    # js_api：阻塞式决策（旧 askyesnocancel 的异步等价物）
    # ------------------------------------------------------------------
    def _interaction_default(self, kind: str) -> Any:
        if kind == "captcha":
            return ""
        if kind == "address_input":
            return {}
        if kind == "close_confirm":
            return "keep"
        if kind == "save_retry":
            return "cancel"
        return "stop"

    def _register_interaction(self, kind: str) -> tuple[str, _PendingInteraction]:
        with self._push_lock:
            self._decision_seq += 1
            prefix = "c" if kind == "captcha" else "d"
            interaction_id = f"{prefix}{self._decision_seq}"
            entry = _PendingInteraction(event=threading.Event(), kind=kind, created_at=time.time())
            self._decisions[interaction_id] = entry
        return interaction_id, entry

    def _wait_interaction(self, interaction_id: str, entry: _PendingInteraction, *, default: Any = "") -> Any:
        entry.event.wait(self._interaction_timeout_s)
        timed_out = False
        with self._push_lock:
            current = self._decisions.pop(interaction_id, None)
            if current is entry:
                if not entry.holder:
                    entry.holder.append(default)
                timed_out = True
        if timed_out:
            shown = default or "取消"
            self.log(f"交互请求 {interaction_id} 等待超时或窗口关闭，自动按「{shown}」处理", "WARN")
        return entry.holder[0] if entry.holder else default

    def _cancel_pending_interactions(self, reason: str,
                                     except_kinds: frozenset[str] = frozenset()) -> int:
        """唤醒所有等待中的 decision/captcha，避免 worker/关闭线程永久阻塞。"""
        with self._push_lock:
            entries = [
                (interaction_id, entry)
                for interaction_id, entry in list(self._decisions.items())
                if entry.kind not in except_kinds
            ]
            for interaction_id, _entry in entries:
                self._decisions.pop(interaction_id, None)
        for interaction_id, entry in entries:
            if not entry.holder:
                entry.holder.append(self._interaction_default(entry.kind))
            entry.event.set()
        if entries:
            self.log(f"已取消 {len(entries)} 个等待中的交互请求（{reason}）", "WARN")
        return len(entries)

    def _request_decision(self, kind: str, title: str, message: str,
                          choices: list[dict[str, str]]) -> str:
        decision_id, entry = self._register_interaction(kind)
        self._emit_event("decision", {"id": decision_id, "kind": kind, "title": title,
                                "message": message, "choices": choices})
        return str(self._wait_interaction(decision_id, entry, default=self._interaction_default(kind)))

    def resolve_decision(self, decision_id: str, choice: str) -> dict[str, Any]:
        """把用户在决策弹窗里的选择交回等待中的任务线程。

        ``holder`` 收到 ``str(choice)`` 并唤醒事件；**同一个 id 只能兑现一次**，
        重复提交返回 ``{"ok": False}``；未知 id 同样返回 ``{"ok": False}`` 且不抛异常。
        """
        with self._push_lock:
            entry = self._decisions.pop(str(decision_id), None)
            if entry is not None:
                entry.holder.append(str(choice))
                entry.event.set()
        return {"ok": entry is not None}

    def _order_decision(self, code: str, error: str) -> str:
        return self._request_decision(
            "order_retry", "订单定位失败",
            f"订单 {code} 定位失败：\n{error}", RETRY_CHOICES)

    def _sss_decision(self, identifier: str, error: str) -> str:
        return self._request_decision(
            "sss_retry", "下单失败",
            f"订单 {identifier} 创建失败：\n{error}", RETRY_CHOICES)

    def _request_captcha(self, image_bytes: bytes) -> str:
        import base64

        captcha_id, entry = self._register_interaction("captcha")
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        self._emit_event("captcha", {"id": captcha_id, "image": image_b64})
        code = self._wait_interaction(captcha_id, entry, default="")
        if not str(code).strip():
            raise _InteractionCancelled("验证码输入已取消或超时")
        return str(code)

    def _sss_captcha(self, image_bytes: bytes) -> str:
        return self._request_captcha(image_bytes)

    def resolve_captcha(self, captcha_id: str, code: str) -> dict[str, Any]:
        """把用户输入的验证码交回等待中的任务线程；同一 id 只能兑现一次，未知 id 回 ``{"ok": False}``。"""
        with self._push_lock:
            entry = self._decisions.pop(str(captcha_id), None)
            if entry is not None:
                entry.holder.append(str(code))
                entry.event.set()
        return {"ok": entry is not None}

    def _request_address_input(self, items: list[dict[str, Any]]) -> dict[str, str]:
        """向 UI 发起待确认地址填写，阻塞等待返回 {原始地址: 最终地址}。"""
        request_id, entry = self._register_interaction("address_input")
        self._emit_event("address_input", {
            "id": request_id,
            "title": "地址待确认",
            "message": "以下地址无法自动识别，请输入要写入表格的最终地址；留空则保持原待确认流程。",
            "items": items,
        })
        result = self._wait_interaction(request_id, entry,
                                        default=self._interaction_default("address_input"))
        values: Any = result
        if isinstance(result, str):
            try:
                values = json.loads(result)
            except (TypeError, ValueError):
                values = {}
        if not isinstance(values, dict):
            return {}
        cleaned: dict[str, str] = {}
        for key, value in values.items():
            text = str(value or "").strip()
            if text:
                cleaned[str(key)] = text
        return cleaned

    def resolve_address_input(self, input_id: str, entries: Any) -> dict[str, Any]:
        """前端提交待确认地址输入；entries 为 {原始地址: 最终地址} 或 JSON 字符串。"""
        with self._push_lock:
            entry = self._decisions.pop(str(input_id), None)
            if entry is not None:
                entry.holder.append(entries)
                entry.event.set()
        return {"ok": entry is not None}

    def _pending_address_input(self, items: list[dict[str, Any]]) -> dict[str, str]:
        return self._request_address_input(items)

    def _save_decision(self, error: str) -> str:
        return self._request_decision(
            "save_retry", "Excel 文件正在使用",
            "保存失败，Excel 文件可能正在被打开或占用。\n请关闭 Excel 文件后点击“重试保存”。\n\n" + error,
            [{"value": "retry", "label": "重试保存", "style": "primary"},
             {"value": "cancel", "label": "取消", "style": "neutral"}])

    # ------------------------------------------------------------------
    # js_api：文件对话框与模板
    # ------------------------------------------------------------------
    def choose_excel(self, mode: str = "order") -> dict[str, Any]:
        """弹出文件选择框，返回 ``{"path": ..., "error": ...}``。

        ``error`` 由 :func:`_excel_field_error` 给出（空路径 / 文件不存在 / 后缀不是
        ``.xlsx``/``.xlsm``）。注意：只接受对话框返回 ``list``/``tuple`` 的情形。
        """
        import webview

        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False, file_types=FILE_DIALOG_FILTERS)
        path = result[0] if isinstance(result, (list, tuple)) and result else ""
        error = _excel_field_error(path)
        return {"path": path, "error": error}

    def new_template(self, mode: str = "order") -> dict[str, Any]:
        """弹出保存框并生成空白模板（``mode="order"`` 生成排单表，否则生成闪时送表）。

        用户取消时返回 ``{"path": "", "error": ""}``（**不算错误**）；没有 Excel 后缀会
        自动补 ``.xlsx``；写盘失败返回 ``{"path": "", "error": "无法写入模板文件：…"}``。
        """
        import webview

        save_name = "排单.xlsx" if mode == "order" else "闪时送.xlsx"
        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG, file_types=FILE_DIALOG_FILTERS, save_filename=save_name)
        path = result if isinstance(result, str) else (result[0] if isinstance(result, (list, tuple)) and result else "")
        if not path:
            return {"path": "", "error": ""}
        dest = _with_excel_suffix(_Path(path))
        try:
            if mode == "order":
                write_order_template(dest)
            else:
                write_sss_template(dest)
        except Exception as exc:
            return {"path": "", "error": f"无法写入模板文件：\n{exc}"}
        self.log(f"已生成{'排单' if mode == 'order' else '闪时送'}模板：{dest}")
        return {"path": str(dest), "error": ""}

    # ------------------------------------------------------------------
    # js_api：浏览器检查 / 凭据 / 更新
    # ------------------------------------------------------------------
    def check_browser(self) -> dict[str, Any]:
        """启动内置浏览器自检（后台线程），立即返回；结果通过日志事件回报。"""
        self._set_status("updating")
        self.log("正在检查浏览器...")
        threading.Thread(target=self._check_browser_worker, daemon=True).start()
        return {"ok": True}

    def _check_browser_worker(self) -> None:
        try:
            path = ensure_browser()
            self.log(f"内置浏览器可用：{browser_description()}（{path}）", "OK")
            if warning := browser_version_warning():
                self.log(warning, "WARN")
            self._set_status("ready")
        except Exception as exc:
            self._worker = None
            self._set_status("error")
            self._emit_event("task:error", {"message": str(exc)})

    # ------------------------------------------------------------------
    # WPS 云文档同步
    # ------------------------------------------------------------------

    def _wps_cli(self) -> KdocsCli:
        return KdocsCli(self._config.wps_cli_path or None)

    def _wps_effective_tables(self) -> dict[str, dict[str, str]]:
        """返回实际写入目标，并拒绝过期或越权目标（实现在 wps_cloud.effective_tables）。"""
        return effective_tables(self._config)

    def save_wps_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存云文档同步的配置（沿用 AppConfig 的原子保存）。

        关闭云同步（``enabled`` True→False）时**立刻作废**所有未使用的预览令牌：
        否则用户在关闭期间一次上传都不调用，重新开启后旧 ``preview_id`` 仍能真的
        写云端 —— 那是"关闭了还能写"的漏洞。
        """
        cfg = self._config
        was_enabled = bool(cfg.wps_enabled)
        if "enabled" in payload:
            cfg.wps_enabled = bool(payload.get("enabled"))
            if was_enabled and not cfg.wps_enabled:
                invalidated = self._previews.invalidate_all("preview_invalidated")
                if invalidated:
                    self.log(f"[云同步] 已关闭，作废 {invalidated} 份未使用的预览令牌",
                             "WARN")
        if "test_mode" in payload:
            cfg.wps_test_mode = bool(payload.get("test_mode"))
        if "cli_path" in payload:
            cfg.wps_cli_path = str(payload.get("cli_path") or "").strip()
        if "drive_id" in payload:
            cfg.wps_drive_id = str(payload.get("drive_id") or "").strip()
        if "test_file_id" in payload:
            cfg.wps_test_file_id = str(payload.get("test_file_id") or "").strip()
        if "test_drive_id" in payload:
            cfg.wps_test_drive_id = str(payload.get("test_drive_id") or "").strip()
        if "test_tables" in payload:
            from .config import normalize_wps_test_tables
            cfg.wps_test_tables = normalize_wps_test_tables(payload.get("test_tables"))
        if "marker_enabled" in payload:
            cfg.wps_marker_enabled = bool(payload.get("marker_enabled"))
        if "sort_enabled" in payload:
            cfg.wps_sort_enabled = bool(payload.get("sort_enabled"))
        if "address_order" in payload:
            from .config import normalize_wps_address_order
            # 以**当前配置**为底：界面只回传部分子表时，没提到的子表保持原样。
            # （原实现以出厂默认为底，会把用户自定义的表 ID/地址顺序静默重置。）
            cfg.wps_address_order = normalize_wps_address_order(
                payload.get("address_order"), base=cfg.wps_address_order)
        if "tables" in payload:
            from .config import normalize_wps_tables
            cfg.wps_tables = normalize_wps_tables(
                payload.get("tables"), base=cfg.wps_tables)
        try:
            cfg.save()
        except OSError:
            return {"ok": False, "reason": "write_failed"}
        return {"ok": True}

    def restore_wps_production_tables(self) -> dict[str, Any]:
        """把写入目标切回正式排单表（配置里的备份 ID）。"""
        backup = self._config.wps_production_tables or {}
        if not backup:
            return {"ok": False, "reason": "没有保存正式表备份"}
        from .config import normalize_wps_tables
        self._config.wps_tables = normalize_wps_tables(backup, base=backup)
        try:
            self._config.save()
        except OSError:
            return {"ok": False, "reason": "写入配置失败"}
        self.log("[云同步] 写入目标已切回正式排单表", "WARN")
        return {"ok": True, "tables": {k: v.get("file_id", "")
                                       for k, v in self._config.wps_tables.items()}}

    def wps_status(self) -> dict[str, Any]:
        """返回云同步的当前状态（不联网、不写任何东西）。"""
        cfg = self._config
        status: dict[str, Any] = {
            "ok": True,
            "enabled": bool(cfg.wps_enabled),
            "test_mode": bool(cfg.wps_test_mode),
            "cli_path": "",
            "cli_found": False,
            "authenticated": False,
            "target_date": "",
            "weekday_number": 0,
            "excel_path": str(cfg.excel_path) if cfg.excel_path else "",
            "tables": [],
            "marker_enabled": bool(cfg.wps_marker_enabled),
            "sort_enabled": bool(cfg.wps_sort_enabled),
            "address_order": {sheet: list(order)
                              for sheet, order in (cfg.wps_address_order or {}).items()},
            # 出厂默认顺序（界面「恢复默认」用；避免前后端各写一份）
            "address_order_defaults": {
                sheet: list(order)
                for sheet, order in default_wps_address_order().items()},
        }
        try:
            cli = self._wps_cli()
            status["cli_path"] = cli.path
            status["cli_found"] = True
            status["authenticated"] = cli.authenticated()
        except WpsCloudError as exc:
            status["reason"] = str(exc)

        target = target_date_for(start_hour=cfg.wps_target_hour_start,
                                 end_hour=cfg.wps_target_hour_end)
        from .wps_cloud import weekday_number
        status["target_date"] = target.isoformat()
        # 通讯记号写的是**运行日**的周几（实测目标表：周四晚跑记 5、周五晚跑记 6）
        status["weekday_number"] = weekday_number(_dt.date.today())

        ledger = SyncLedger()
        try:
            effective = self._wps_effective_tables()
        except WpsCloudError as exc:
            status["reason"] = str(exc)
            effective = {}
        for sheet, conf in cfg.wps_tables.items():
            file_id = effective.get(sheet, {}).get("file_id", "")
            entry = {
                "sheet": sheet,
                "file_id": conf.get("file_id", ""),
                "effective_file_id": file_id,
                "last_sync": "",
                "last_people": 0,
            }
            summary = ledger.batch_summary(target.isoformat(), file_id)
            if summary:
                entry["last_sync"] = summary["synced_at"]
                entry["last_people"] = summary["people"]
            status["tables"].append(entry)
        for entry, conf in zip(status["tables"], cfg.wps_tables.values()):
            entry["file_id"] = conf.get("file_id", "")
        status["test_file_id"] = cfg.wps_test_file_id
        status["test_tables"] = dict(cfg.wps_test_tables)
        production_ids = {v.get("file_id", "") for v in (cfg.wps_production_tables or {}).values()}
        # 用**实际生效**的目标判断，而不是 cfg.wps_tables ——
        # 测试模式下生效目标是 cfg.wps_test_tables（副本）；拿 wps_tables
        # （可能是正式表 ID）去比，会把"正在写副本"误报成"正在写正式表"。
        try:
            effective_ids = {c.get("file_id", "")
                             for c in self._wps_effective_tables().values()}
        except WpsCloudError:
            effective_ids = {c.get("file_id", "") for c in cfg.wps_tables.values()}
        status["writing_test_copies"] = bool(effective_ids) and not (effective_ids & production_ids)
        status["effective_targets"] = sorted(fid for fid in effective_ids if fid)
        status["production_tables"] = {k: v.get("file_id", "")
                                       for k, v in (cfg.wps_production_tables or {}).items()}
        status["state_path"] = str(ledger.path)
        return status

    # ------------------------------------------------------------------
    # WPS：预览上下文 / 结构化计划 / 预览令牌
    #
    # 预览与上传**共用同一个上下文与同一套只读计划构建**：上传前重新构建一次
    # 并与预览时保存的指纹逐字段比对，任何变化（本地文件、目标表、日期、
    # 排序/标记配置）都在消费令牌与写入之前拒绝。这不是远端 CAS，
    # 只能缩小"只读复核 → 首次写入"的窗口。
    # ------------------------------------------------------------------
    def _wps_context(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """当前云同步上下文的可哈希快照；失败时返回 ``(None, 拒绝结果)``。"""
        cfg = self._config
        try:
            tables = self._wps_effective_tables()
        except WpsCloudError as exc:
            return None, self._wps_reject("wps_disabled", str(exc), "检查云同步配置",
                                          status="rejected", proven_no_write=True)
        if not tables:
            return None, self._wps_reject(
                "no_target_tables", "测试模式未配置测试文件 id（测试副本），拒绝读取与写入",
                "先创建并配置测试副本", status="rejected", proven_no_write=True)
        target = target_date_for(start_hour=cfg.wps_target_hour_start,
                                 end_hour=cfg.wps_target_hour_end)
        excel = str(cfg.excel_path) if cfg.excel_path else ""
        return {
            "enabled": bool(cfg.wps_enabled),
            "test_mode": bool(cfg.wps_test_mode),
            "excel_path": excel,
            "target_date": target.isoformat(),
            "run_date": _dt.date.today().isoformat(),
            "tables": {sheet: str(conf.get("file_id", ""))
                       for sheet, conf in sorted(tables.items())},
            "marker_enabled": bool(cfg.wps_marker_enabled) and not bool(cfg.wps_test_mode),
            "sort_enabled": bool(cfg.wps_sort_enabled),
            "address_order": {sheet: list(order) for sheet, order in
                              sorted((cfg.wps_address_order or {}).items())},
            "cli_path": str(cfg.wps_cli_path or ""),
        }, None

    @staticmethod
    def _wps_context_changes(old: dict[str, Any],
                             new: dict[str, Any]) -> list[str]:
        return sorted(
            key for key in set(old or {}) | set(new or {})
            if (old or {}).get(key) != (new or {}).get(key))

    def _wps_string_tables(self, tables: Any) -> dict[str, dict[str, str]]:
        return {str(sheet): {"file_id": str(conf.get("file_id", ""))}
                for sheet, conf in (tables or {}).items()}

    def _wps_structured_tables(self, plans: list[Any]) -> list[dict[str, Any]]:
        """把计划转成前端可直接渲染的逐表结构，并补每表统计。"""
        tables = canonical_plan(plans)
        for table in tables:
            changes = table.get("changes") or []
            counts = {"to_update": 0, "to_append": 0, "unchanged": 0,
                      "skipped": 0, "warned": len(table.get("warnings") or []),
                      "blocked": 1 if table.get("blocked_reason") else 0}
            for change in changes:
                if change.get("target_blocked"):
                    counts["skipped"] += 1
                elif str(change.get("kind") or "") == "new":
                    counts["to_append"] += 1
                elif change.get("needs_write"):
                    counts["to_update"] += 1
                else:
                    counts["unchanged"] += 1
            table["counts"] = counts
            table["sort"] = {
                "enabled": bool(table.get("sort_enabled")),
                "sort_range": table.get("sort_range") or "",
                "sort_key_col": table.get("sort_key_col") or 0,
                "row_keys_count": len(table.get("row_keys") or []),
                "unknown_addresses": table.get("unknown_addresses") or [],
            }
        return tables

    @staticmethod
    def _wps_blocked_list(tables: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [{"sheet": str(item.get("sheet") or ""),
                 "reason": str(item.get("blocked_reason") or "")}
                for item in tables if item.get("blocked_reason")]

    @staticmethod
    def _wps_warning_list(tables: list[dict[str, Any]]) -> list[str]:
        warnings: list[str] = []
        for item in tables:
            sheet = str(item.get("sheet") or "")
            for warning in item.get("warnings") or []:
                warnings.append(f"{sheet}：{warning}" if sheet else str(warning))
        return warnings

    def _wps_ledger_and_journal(self) -> tuple[SyncLedger, SyncJournal]:
        """账本与意图日志（同目录）。

        两者都可能抛错，**都必须由调用方转成结构化拒绝**（不能抛给前端）：

        * 账本损坏 → :class:`app.wps_cloud.LedgerCorruptError`；
        * 意图日志损坏/版本不支持 → :class:`app.wps_journal.JournalError`。

        路径跟着**配置文件所在目录**走：生产环境就是用户配置目录（与
        ``wps_cloud.default_state_path`` 一致），测试注入临时配置文件时
        状态也落在临时目录里，不会碰到真实用户的账本。
        """
        config_path = getattr(self._config, "config_path", None)
        state_path = None
        if config_path:
            state_path = _Path(config_path).parent / "wps_sync_state.json"
        ledger = SyncLedger(state_path)
        return ledger, SyncJournal(journal_path_for(ledger.path))

    @staticmethod
    def _local_state_error(exc: BaseException) -> str:
        """把本地状态异常归一成"哪一类不可用"，便于给出修复指引。"""
        name = type(exc).__name__
        if name in ("JournalError", "JournalCorruptError"):
            return "意图日志"
        if name == "LedgerCorruptError":
            return "同步账本"
        return "本地状态"

    def _read_local_bytes(self, *, preview_id: str = ""
                          ) -> tuple[bytes, str] | tuple[dict[str, Any], None]:
        """读取本地排单表的**整份字节**并返回 ``(data, sha256)``。

        失败时返回一个结构化拒绝结果（而不是抛异常）；它由调用方直接返回。
        之所以整份读进内存：本项目的排单表只有几十 KB，而"哈希与解析必须同源"
        比省这点内存重要得多。
        """
        cfg = self._config
        if not cfg.excel_path:
            return self._wps_reject("no_local_file", "请先在「订单处理」里选择排单表",
                                    "选择排单表后重新预览", status="rejected",
                                    preview_id=preview_id, proven_no_write=True), None
        try:
            data = _Path(cfg.excel_path).read_bytes()
        except OSError as exc:
            if preview_id:
                self._previews.invalidate(preview_id, "preview_changed")
            return self._wps_reject(
                "local_file_unreadable", f"本地排单表不可读：{exc}",
                "确认文件存在且未被占用后重新预览", status="failed",
                preview_id=preview_id, proven_no_write=True), None
        return data, hashlib.sha256(data).hexdigest()

    def _wps_build_plans(self, context: dict[str, Any],
                         data: bytes | None = None
                         ) -> tuple[list[Any] | None, dict[str, Any] | None]:
        """只读构建计划；失败返回 ``(None, 拒绝结果)``。

        本地状态（账本、意图日志）不可用一律 ``local_state_blocked`` ——
        没有可信的幂等锚点就绝不能写云端，否则会把同一批餐重复累加。
        """
        cfg = self._config
        try:
            tables = self._wps_effective_tables()
        except WpsCloudError as exc:
            return None, self._wps_reject("wps_disabled", str(exc),
                                          "检查云同步配置", status="rejected",
                                          proven_no_write=True)
        try:
            ledger, _journal = self._wps_ledger_and_journal()
        except Exception as exc:  # noqa: BLE001 - 账本/日志损坏都要转成拒绝
            self.log(f"[云同步] 本地状态不可用：{type(exc).__name__}: {exc}", "ERROR")
            return None, self._wps_reject(
                "local_state_blocked",
                f"本地{self._local_state_error(exc)}不可用：{exc}",
                "先修复（或移走）该文件后重新预览", status="blocked",
                proven_no_write=True)
        try:
            cli = self._wps_cli()
            if not cli.authenticated():
                return None, self._wps_reject(
                    "wps_unauthorized", "尚未授权云文档，请先点击「去授权」",
                    "完成云文档授权后重新预览", status="rejected",
                    proven_no_write=True)
            # data 非空时用**同一份字节**解析：哈希与解析必须来自同一次读取，
            # 否则"哈希校验通过"的计划可能来自另一个版本的文件。
            local = (read_local_orders_from_bytes(data, log=self.log)
                     if data is not None
                     else read_local_orders(cfg.excel_path, log=self.log))
            plans = build_plan(
                cli, local_orders=local, tables=tables,
                target=_dt.date.fromisoformat(context["target_date"]),
                ledger=ledger,
                marker_enabled=bool(context["marker_enabled"]),
                run_date=_dt.date.today(),
                address_order=cfg.wps_address_order,
                sort_enabled=bool(context["sort_enabled"]),
                log=self.log)
        except WpsCloudError as exc:
            self.log(f"[云同步] 失败：{exc}", "ERROR")
            return None, self._wps_reject("plan_failed", str(exc),
                                          "检查云端状态/授权后重新预览",
                                          status="failed", proven_no_write=True)
        except Exception as exc:  # noqa: BLE001 - 不能把异常抛给前端
            self.log(f"[云同步] 异常：{type(exc).__name__}: {exc}", "ERROR")
            return None, self._wps_reject("unexpected", f"{type(exc).__name__}: {exc}",
                                          "查看日志后重试", status="failed",
                                          proven_no_write=True)
        return list(plans), None

    # ------------------------------------------------------------------
    # 统一操作互斥
    #
    # 冲突**立即返回**（不排队、不等待）：占位只覆盖内存状态，绝不包住云端
    # 往返或下载，因此不存在"持锁再请求持锁"的嵌套死锁。
    # ------------------------------------------------------------------
    def _reserve(self, mode: str, *, title: str = "",
                 summary: dict[str, Any] | None = None,
                 phase: str = "", next_action: str = ""
                 ) -> tuple[Any, dict[str, Any] | None]:
        """尝试占用操作槽位；冲突时返回 (None, 拒绝结果)。"""
        from .operations import mode_title
        reservation = self._operations.try_reserve(
            mode, summary={"title": title or mode_title(mode), **(summary or {})},
            phase=phase, next_action=next_action or "等待当前操作结束后重试")
        if not reservation.granted:
            payload = self._operations.conflict_payload(
                reservation.conflict or {}, action=title or mode_title(mode))
            self.log(f"[操作互斥] {payload['reason']}", "WARN")
            return None, payload
        return reservation.operation, None

    def _guard_reserved(self, operation: Any, work: Any, *,
                        label: str, action: str = "") -> dict[str, Any]:
        """执行一个已占位的动作，保证**任何异常都会释放占位**并返回结构化错误。

        占位泄漏的后果不是崩溃，而是"永久忙"：后续所有危险操作都会被拒绝。
        因此从 reserve 成功那一刻起，保存配置、写密钥链、起线程、启动授权……
        每一步失败都必须走这里。
        """
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - 界面需要结构化原因，不能抛崩
            self.log(f"[操作互斥] {label}启动失败：{type(exc).__name__}: {exc}", "ERROR")
            self._operations.finish(operation, status="error",
                                    reason=f"{type(exc).__name__}: {exc}")
            return {"ok": False, "reason": "internal_error",
                    "message": f"{label}启动失败：{type(exc).__name__}: {exc}",
                    "next_action": "查看日志后重试", "action": action or label}
        if isinstance(result, dict) and result.get("ok") is False:
            # 业务性失败：把占位状态写成实际结论，避免显示成"还在跑"。
            if self._operations.is_active(operation):
                self._operations.finish(
                    operation, status=str(result.get("status") or "rejected"),
                    reason=str(result.get("reason") or result.get("code") or "")[:200],
                    next_action=str(result.get("next_action") or ""))
        return result if isinstance(result, dict) else {"ok": True}

    def operation_status(self, operation_id: str = "") -> dict[str, Any]:
        """查询当前活动操作与最近一次结果（前端据此禁用按钮/显示进度）。"""
        return self._operations.status(operation_id)

    def _conflict_reject(self, conflict: dict[str, Any], *,
                         preview_id: str = "",
                         action: str = "") -> dict[str, Any]:
        """把占位冲突转成 WPS 入口的拒绝结果（保留 ok/status/reason 与两种统计）。

        冲突发生在**消费预览令牌与任何云端写入之前**，因此可以如实声明
        ``proven_no_write``：这次调用一行都没写。
        """
        payload = self._wps_reject(
            "operation_conflict", str(conflict.get("reason") or "已有其他操作正在进行"),
            str(conflict.get("next_action") or "等待当前操作结束后重试"),
            status="rejected", preview_id=preview_id, proven_no_write=True,
            action=action or "")
        payload["conflicting_operation"] = conflict.get("conflicting_operation")
        payload["operation_id"] = str(conflict.get("operation_id") or "")
        return payload

    def _wps_reject(self, code: str, reason: str, next_action: str = "", *,
                    status: str = "rejected",
                    summary: dict[str, Any] | None = None,
                    preview_id: str = "",
                    proven_no_write: bool = False,
                    planned_summary: dict[str, Any] | None = None,
                    execution_summary: dict[str, Any] | None = None,
                    counts_source: str = "",
                    **extra: Any) -> dict[str, Any]:
        """WPS 预览/上传的统一失败结果。

        默认按**最保守**口径返回执行摘要：``rows`` 全为 ``None``（无法证明）；
        只有调用方明确知道"这次拒绝发生在任何云端写入之前"时才可传
        ``proven_no_write=True``，把 ``rows`` 标成可证明的 0。
        """
        payload: dict[str, Any] = {
            "ok": False,
            "status": str(status),
            "reason": redact(reason or code),
            "code": str(code),
            "next_action": str(next_action or ""),
            "contract_version": 1,
            "preview_id": str(preview_id or ""),
            "summary": dict(summary or {"code": code}),
            "planned_summary": planned_summary,
            "execution_summary": (execution_summary if execution_summary is not None
                                  else _execution_summary_default(
                                      status=status, code=code,
                                      next_action=next_action,
                                      proven_no_write=proven_no_write,
                                      counts_source=counts_source)),
            "tables": [],
            "blocked": [],
            "warnings": [],
            "text": "",
        }
        payload.update(extra)
        return payload

    def _preview_rejection(self, preview_id: str, code: str) -> dict[str, Any]:
        if code == "preview_not_found":
            reason = "预览不存在或已被清理，请重新预览"
        elif code == "preview_expired":
            reason = "预览已过期（超过 10 分钟），请重新预览"
        elif code == "preview_consumed":
            reason = "该预览已使用或已在处理中，不能重放，请重新预览"
        elif code in ("preview_changed", "preview_invalidated"):
            reason = "预览后内容或环境已变化，该预览已失效，请重新预览"
        elif code == "missing_preview":
            reason = "缺少 preview_id：无参上传已禁用，请先预览"
        else:
            reason = f"预览状态不可用（{code}），请重新预览"
        return self._wps_reject(code, reason, "重新调用 wps_preview()",
                                preview_id=preview_id, proven_no_write=True)

    def wps_preview(self) -> dict[str, Any]:
        """只读云端，生成结构化预览并发放 10 分钟一次性上传令牌。

        不写云端、不动账本；成功结果的 ``preview_id`` 必须原样传给
        :meth:`wps_upload`。``planned_summary`` 是**计划**口径，
        ``execution_summary`` 此时只表示"尚未执行"。
        """
        cfg = self._config
        if not cfg.wps_enabled:
            return self._wps_reject(
                "wps_disabled", "云文档同步已关闭，已拒绝预览，未读取也未写入任何云端内容",
                "在「云文档同步」中开启后再试", status="rejected",
                proven_no_write=True)
        if not cfg.excel_path:
            return self._wps_reject("no_local_file", "请先在「订单处理」里选择排单表",
                                    "选择排单表后重新预览", status="rejected",
                                    proven_no_write=True)
        operation, conflict = self._reserve("wps_preview", title="云文档预览")
        if conflict is not None:
            return self._conflict_reject(conflict, action="云文档预览")
        try:
            result = self._wps_preview_impl()
        except BaseException:
            # 意外异常也必须先释放占位，否则一次预览失败会把整个程序锁死。
            self._operations.finish(operation, status="error", reason="unexpected")
            raise
        self._finish_wps_operation(operation, result)
        return result

    def _wps_preview_impl(self) -> dict[str, Any]:
        """``wps_preview`` 的实际实现（调用方已取占位）。"""
        cfg = self._config
        context, error = self._wps_context()
        if error is not None:
            return error
        assert context is not None
        # 先读一次字节快照；后面算指纹与解析计划都用它（禁止混合快照）。
        local_bytes, local_sha = self._read_local_bytes()
        if isinstance(local_bytes, dict):        # 读取失败：已经是拒绝结果
            return local_bytes
        plans, error = self._wps_build_plans(context, data=local_bytes)
        if error is not None:
            return error
        assert plans is not None
        try:
            for plan in plans:
                for warning in (getattr(plan, "warnings", None) or []):
                    self.log(f"[云同步预览] {plan.sheet}：{warning}", "WARN")
            text = format_plan(plans)
            stats = summarize_plan(plans)
        except Exception as exc:  # noqa: BLE001
            return self._wps_reject("preview_format_failed",
                                    f"{type(exc).__name__}: {exc}",
                                    "查看日志后重试", status="failed",
                                    proven_no_write=True)

        tables = self._wps_structured_tables(plans)
        blocked = self._wps_blocked_list(tables)
        warnings = self._wps_warning_list(tables)
        record = self._previews.create(
            local_sha256=local_sha,
            context=context,
            context_fingerprint=plan_fingerprint([context]),
            plan=tables,
            plan_fingerprint=plan_fingerprint(plans),
            summary=stats,
            text=text,
            tables=tables,
            blocked=blocked,
            warnings=warnings,
            target_date=context["target_date"],
            target_tables=self._wps_string_tables(self._wps_effective_tables()),
        )
        planned = planned_summary(plans, stats)
        result: dict[str, Any] = {
            "ok": True,
            "status": "preview_ready",
            "reason": "",
            "code": "",
            "next_action": "wps_upload(preview_id)",
            "summary": stats,
            "stats": stats,
            "planned_summary": planned,
            "execution_summary": execution_summary(
                status="preview_ready", sheets=[], written=0, failed=0,
                uncertain=False, next_action="wps_upload(preview_id)",
                planned=planned, proven_no_write=True, executed=False),
            "text": text,
            "tables": tables,
            "blocked": blocked,
            "warnings": warnings,
            "test_mode": bool(cfg.wps_test_mode),
        }
        result.update(self._previews.public(record))
        return result

    def wps_upload(self, preview_id: str = "") -> dict[str, Any]:
        """真正写云端；必须传入 ``wps_preview()`` 返回的预览令牌。

        执行前会重新只读构建计划并核对上下文与计划指纹；任何变化都在消费令牌
        与写入之前拒绝。通过后消费令牌再 ``apply_plan``（带意图日志与恢复态）。
        """
        pid = str(preview_id or "").strip()
        if not pid:
            return self._wps_reject(
                "missing_preview",
                "无参上传已禁用：请先调用 wps_preview()，再传入 preview_id",
                "重新预览并传入 preview_id", proven_no_write=True)
        cfg = self._config
        if not cfg.wps_enabled:
            if pid:
                # 作废手上这份旧令牌：重新开启后必须重新预览，不能拿旧 id 直接写。
                self._previews.invalidate(pid, "preview_invalidated")
            return self._wps_reject(
                "wps_disabled", "云文档同步已关闭，已拒绝上传，未写入任何云端内容",
                "在「云文档同步」中开启后重新预览", status="rejected",
                preview_id=pid, proven_no_write=True)
        if self.worker_alive():
            return self._wps_reject(
                "busy", "订单/闪时送任务正在运行，请先停止后再上传云文档",
                "等待任务结束后重新上传", status="rejected", preview_id=pid,
                proven_no_write=True)
        operation, conflict = self._reserve("wps_upload",
                                            title="云文档上传",
                                            summary={"preview_id": pid})
        if conflict is not None:
            return self._conflict_reject(conflict, preview_id=pid,
                                         action="云文档上传")
        try:
            result = self._wps_upload_impl(pid, operation)
        except BaseException:
            self._operations.finish(operation, status="error", reason="unexpected")
            raise
        self._finish_wps_operation(operation, result)
        return result

    def _finish_wps_operation(self, operation: Any, result: dict[str, Any]) -> None:
        """按实际返回结果结束占位（状态如实反映这次调用，不一律写 success）。"""
        if operation is None or not self._operations.is_active(operation):
            return
        allowed = {"success", "noop", "partial", "failed", "error", "rejected",
                   "uncertain", "blocked", "not_started", "consumed",
                   "preview_ready"}
        raw = str(result.get("status") or "")
        status = raw if raw in allowed else ("success" if result.get("ok") else "error")
        self._operations.finish(
            operation, status=status,
            reason=str(result.get("code") or result.get("reason") or "")[:200],
            summary={"ok": bool(result.get("ok")), "status": raw},
            next_action=str(result.get("next_action") or "")[:200])

    def _wps_upload_impl(self, pid: str, operation: Any) -> dict[str, Any]:
        """``wps_upload`` 的实际实现（调用方已取占位）。"""
        record, code = self._previews.get(pid)
        if code:
            return self._preview_rejection(pid, code)
        assert record is not None
        context, error = self._wps_context()
        if error is not None:
            self._previews.invalidate(pid, "preview_changed")
            error["preview_id"] = pid
            return error
        assert context is not None
        changed = self._wps_context_changes(record.context, context)
        if changed:
            self._previews.invalidate(pid, "preview_changed")
            return self._wps_reject(
                "preview_changed",
                "预览后本地文件、目标表配置或日期上下文已变化，拒绝上传且未写入任何内容",
                "重新调用 wps_preview()", preview_id=pid, proven_no_write=True,
                changed=changed)
        # 本地文件内容指纹：预览时算过一次，这里**必须重算**再比对，而且比对与
        # 解析必须用**同一份字节**（先整份读进来）。
        # 只比计划指纹不够 —— 改了不影响计划的内容（例如另一张无关子表、单元格格式、
        # 或任何不会改变"要写什么"的编辑）不会被计划指纹发现，而用户的心智是
        # "文件变了，预览就该作废"。
        local_bytes, local_sha = self._read_local_bytes(preview_id=pid)
        if isinstance(local_bytes, dict):        # 读取失败：已经是拒绝结果
            return local_bytes
        if record.local_sha256 and local_sha != record.local_sha256:
            self._previews.invalidate(pid, "preview_changed")
            self.log("[云同步] 本地排单表在预览后发生了变化，拒绝上传", "WARN")
            return self._wps_reject(
                "preview_changed",
                "本地排单表在预览之后被修改过，拒绝上传且未写入任何内容",
                "重新调用 wps_preview()", preview_id=pid,
                proven_no_write=True,
                changed=sorted(set(changed) | {"local_file"}))
        plans, error = self._wps_build_plans(context, data=local_bytes)
        if error is not None:
            error.setdefault("preview_id", pid)
            if error.get("code") == "local_state_blocked":
                self._previews.invalidate(pid, "preview_changed")
            return error
        assert plans is not None
        try:
            fresh_tables = self._wps_structured_tables(plans)
            fresh_fp = plan_fingerprint(plans)
        except Exception as exc:  # noqa: BLE001
            return self._wps_reject("unexpected", f"{type(exc).__name__}: {exc}",
                                    "查看日志后重新预览", status="failed",
                                    preview_id=pid,
                                    counts_source="unknown_after_exception")
        if fresh_fp != record.plan_fingerprint or fresh_tables != record.plan:
            self._previews.invalidate(pid, "preview_changed")
            return self._wps_reject(
                "preview_changed",
                "预览后云端计划已变化（有人改了表或本地表内容变了），拒绝上传且未写入任何内容",
                "重新调用 wps_preview()", preview_id=pid, proven_no_write=True,
                changed=sorted(set(changed) | {"plan"}))
        consumed, consume_code = self._previews.consume(pid)
        if consume_code:
            return self._preview_rejection(pid, consume_code)
        assert consumed is not None
        return self._wps_apply_plan(plans, pid)

    def _wps_apply_plan(self, plans: list[Any], preview_id: str) -> dict[str, Any]:
        """消费令牌后的实际写入（已通过上下文与计划指纹校验）。"""
        cfg = self._config
        summary = summarize_plan(plans)
        planned = planned_summary(plans, summary)
        self._set_status("updating")
        target_date = getattr(plans[0], "target_date", None) if plans else None
        self.log(f"[云同步] 目标日期 "
                 f"{target_date.isoformat() if target_date else '?'}，"
                 f"{'测试模式（只写测试文件）' if cfg.wps_test_mode else '正式模式'}"
                 + ("；按地址顺序重排整表" if cfg.wps_sort_enabled else "；已关闭排序"))
        operation_id = ""
        try:
            ledger, journal = self._wps_ledger_and_journal()
            cli = self._wps_cli()
            result = apply_plan(
                cli, plans, ledger=ledger,
                marker_enabled=bool(cfg.wps_marker_enabled) and not bool(cfg.wps_test_mode),
                log=self.log, journal=journal)
            operation_id = str(result.get("operation_id") or "")
        except (WpsCloudError, JournalError) as exc:
            self.log(f"[云同步] 失败：{exc}", "ERROR")
            self._set_status("ready")
            blocked = type(exc).__name__ in ("LedgerCorruptError", "JournalError",
                                            "JournalCorruptError")
            return self._wps_reject(
                "local_state_blocked" if blocked else "cloud_error", str(exc),
                "先修复本地日志/账本后重新预览" if blocked
                else "检查授权/云端状态后重新预览",
                status="blocked" if blocked else "failed", preview_id=preview_id,
                planned_summary=planned,
                execution_summary=execution_summary(
                    status="blocked" if blocked else "failed", sheets=[],
                    written=0, failed=0, uncertain=None,
                    next_action="fix_journal" if blocked else "repreview",
                    planned=planned, executed=False),
                summary_stats=summary)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[云同步] 异常：{type(exc).__name__}: {exc}", "ERROR")
            self._set_status("ready")
            # 未捕获异常无法证明是否已写入：executed=False + 不宣称零写入。
            return self._wps_reject(
                "unexpected", f"{type(exc).__name__}: {exc}", "查看日志后重新预览",
                status="failed", preview_id=preview_id, planned_summary=planned,
                execution_summary=execution_summary(
                    status="failed", sheets=[], written=0, failed=0,
                    uncertain=None, next_action="manual_reconcile",
                    planned=planned, executed=False),
                counts_source="unknown_after_exception")

        raw_sheets = result.get("sheets")
        sheets = raw_sheets if isinstance(raw_sheets, list) else []
        malformed = raw_sheets is not None and not isinstance(raw_sheets, list)
        for item in sheets:
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            if status == "ok":
                self.log(f"[云同步] ✔ {item['sheet']}："
                         f"{item.get('people', 0)} 行（{item.get('cells', 0)} 格）", "OK")
                if item.get("sort_skipped"):
                    self.log(f"[云同步] ⚠ {item['sheet']}：{item['sort_skipped']}", "WARN")
                if item.get("ledger_error"):
                    self.log(f"[云同步] ✘ {item['sheet']}：账本未记上"
                             f"（{item['ledger_error']}），下次上传可能重复加餐", "ERROR")
            elif status == "blocked":
                self.log(f"[云同步] ⛔ {item['sheet']}：整表未写入（{item.get('reason')}）",
                         "ERROR")
            elif status == "stale_batch":
                self.log(f"[云同步] ⛔ {item['sheet']}：拒绝写入（{item.get('reason')}）",
                         "ERROR")
            elif status in ("verify_failed",):
                self.log(f"[云同步] ✘ {item['sheet']}：写入后回读校验未通过"
                         f"（{'; '.join((item.get('problems') or [])[:3])}）", "ERROR")
            elif status == "verify_unreadable":
                self.log(f"[云同步] ⚠ {item['sheet']}：{item.get('reason')}", "WARN")
            elif status == "failed":
                self.log(f"[云同步] ✘ {item['sheet']}：{item.get('reason', '写入失败')}",
                         "ERROR")
            elif status == "skipped":
                self.log(f"[云同步] — {item['sheet']}：{item.get('reason', '跳过')}")
            elif status == "noop":
                self.log(f"[云同步] — {item['sheet']}：本次无需改动")
        # 用 .get 兜底：统计字典来自可被替换的实现，缺键不该把一次成功的上传
        # 变成异常（日志行不值得让整个结果失败）。
        planned_rows = int(summary.get("to_update", 0)) + int(summary.get("to_append", 0))
        failed_sheets_count = int(result.get("failed", 0) or 0)
        self.log(f"[云同步] 完成：计划改动 {planned_rows} 行，"
                 f"成功 {result.get('written', 0)} 张表，失败 {failed_sheets_count} 张",
                 "OK" if failed_sheets_count == 0 else "ERROR")
        self._set_status("ready")

        any_uncertain = any(bool(item.get("uncertain")) for item in sheets
                            if isinstance(item, dict)) or bool(
            result.get("journal_error"))
        if not sheets and not malformed:
            any_uncertain = None
        if result["failed"] and not any_uncertain:
            overall = "partial" if result["written"] else "failed"
        elif any_uncertain:
            overall = "uncertain"
        else:
            overall = "success"
        next_action = ("manual_reconcile" if any_uncertain
                       else "repreview" if result["failed"] else "none")
        failed_sheets = [item["sheet"] for item in sheets
                         if isinstance(item, dict)
                         and item.get("status") not in ("ok", "noop")]
        return {
            "ok": failed_sheets_count == 0,
            "status": overall,
            "code": "",
            "reason": ("" if failed_sheets_count == 0
                       else f"以下表未完成：{'、'.join(failed_sheets)}，详见日志"),
            "next_action": next_action,
            "target_date": target_date.isoformat() if target_date else "",
            "preview_id": preview_id,
            "operation_id": operation_id,
            "journal_path": str(result.get("journal_path") or ""),
            "summary": summary,
            "stats": summary,
            "planned_summary": planned,
            "execution_summary": execution_summary(
                status=overall, sheets=sheets, written=result.get("written", 0),
                failed=failed_sheets_count, uncertain=any_uncertain,
                malformed=malformed, next_action=next_action, planned=planned,
                # 执行器逐表给出证据后，才能真正声明"这次一个格子都没写"。
                proven_no_write=bool(result.get("proven_no_write"))),
            "result": result,
            "text": format_plan(plans),
            "tables": self._wps_structured_tables(plans),
            "failed_sheets": failed_sheets,
            "test_mode": bool(cfg.wps_test_mode),
        }

    # ------------------------------------------------------------------
    # WPS：部分失败的只读恢复状态与人工处置
    #
    # 恢复入口**永不写云端**：它只重新只读核对云端并把结论写回本地日志。
    # ``retire_guarded`` 只是让旧任务退出"待处理"，同日期 + 同云表的防重复
    # 闸门仍然保留，必须靠实际云端核对（cloud_verified / cloud_untouched）
    # 才能解除。
    # ------------------------------------------------------------------
    def wps_recovery_status(self) -> dict[str, Any]:
        """只读 WPS 恢复查询：只读本地日志，不联网、不写任何文件。"""
        try:
            _ledger, journal = self._wps_ledger_and_journal()
        except WpsCloudError as exc:
            return recovery_status_error("wps_recovery_ledger_unreadable", str(exc))
        except Exception as exc:  # noqa: BLE001
            return recovery_status_error("wps_recovery_journal_unreadable", str(exc))
        return recovery_status(journal=journal)

    def wps_recovery_resolve(self, payload: dict[str, Any] | None = None,
                             **options: Any) -> dict[str, Any]:
        """人工处置未完成的云同步任务；**不会写云端**。

        ``payload`` 需要 ``operation_id`` / ``decision`` / ``confirm``（与
        decision 完全相同）/ ``note``（至少 4 字符），``retire_guarded`` 还要
        ``confirm_structure_checked=True``。
        ``cloud_verified`` / ``cloud_untouched`` 会重新只读核对云端，
        证明不了就保持阻断。
        """
        data: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
        if isinstance(payload, str) and payload.strip():
            data.setdefault("operation_id", payload.strip())
        for key, value in options.items():
            if value is not None:
                data[key] = value
        try:
            ledger, journal = self._wps_ledger_and_journal()
        except WpsCloudError as exc:
            return {"ok": False, "status": "blocked", "code": "ledger_unreadable",
                    "reason": f"账本不可用：{exc}", "next_action": "fix_journal",
                    "cloud_write": False, "changed": False, "operations": []}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "blocked", "code": "journal_unreadable",
                    "reason": f"意图日志不可用：{exc}", "next_action": "fix_journal",
                    "cloud_write": False, "changed": False, "operations": []}
        operation, conflict = self._reserve(
            "wps_recovery_resolve", title="云同步恢复处置",
            summary={"operation_ref": str(data.get("operation_id") or "")})
        if conflict is not None:
            conflict.update({"cloud_write": False, "changed": False, "operations": []})
            return conflict
        try:
            result = self._wps_recovery_resolve_impl(data, ledger, journal)
        except BaseException:
            self._operations.finish(operation, status="error", reason="unexpected")
            raise
        status = "success" if result.get("ok") else "error"
        self._operations.finish(operation, status=status,
                                reason=str(result.get("reason") or ""))
        return result

    def _wps_recovery_resolve_impl(self, data: dict[str, Any], ledger: SyncLedger,
                                   journal: SyncJournal) -> dict[str, Any]:
        """``wps_recovery_resolve`` 的实际实现（调用方已取占位）。"""
        decision = str(data.get("decision") or "")
        cli = None
        if decision in ("cloud_verified", "cloud_untouched"):
            # 需要只读云端证明时才建 CLI：授权缺失时按"证不了"处理，不放行。
            try:
                cli = self._wps_cli()
                if not cli.authenticated():
                    return {"ok": False, "status": "rejected",
                            "code": "wps_unauthorized",
                            "reason": "尚未授权云文档，无法只读核对云端",
                            "next_action": "完成云文档授权后重试",
                            "cloud_write": False, "changed": False, "operations": []}
            except WpsCloudError as exc:
                return {"ok": False, "status": "rejected", "code": "wps_cli_missing",
                        "reason": str(exc), "next_action": "修复 kdocs-cli 后重试",
                        "cloud_write": False, "changed": False, "operations": []}
        result = resolve_pending_operation(
            str(data.get("operation_id") or ""), decision,
            confirm=str(data.get("confirm") or ""),
            note=str(data.get("note") or ""),
            confirm_structure_checked=bool(data.get("confirm_structure_checked", False)),
            ledger=ledger, journal=journal, cli=cli)
        if result.get("ok"):
            self.log(f"[云同步恢复] 处置 {data.get('operation_id')}："
                     f"{result.get('reason_code') or result.get('status')}"
                     f"（人工备注：{str(data.get('note') or '')[:60]}）", "WARN")
        return result

    def wps_check_copies(self) -> dict[str, Any]:
        """核对"当前写入目标"与正式表是否结构一致（只读）。

        测试副本是**静态快照**：只要协作者/用户在 WPS 里改了正式表，
        副本就会过时，测试结果就不能代表线上真实情况。这个检查用来提前发现。
        """
        cfg = self._config
        operation, conflict = self._reserve(
            "wps_check_copies", title="副本一致性核对",
            summary={"read_only": True, "cloud_write": False})
        if conflict is not None:
            conflict.update({"read_only": True})
            return conflict
        try:
            return self._wps_check_copies_impl(cfg)
        finally:
            self._operations.finish(operation, status="success")

    def _wps_check_copies_impl(self, cfg: AppConfig) -> dict[str, Any]:
        """``wps_check_copies`` 的实际实现（调用方已取占位）。"""
        active = self._wps_effective_tables()
        production = cfg.wps_production_tables or {}
        try:
            cli = self._wps_cli()

            def people(file_id: str) -> dict[tuple[str, str], int]:
                """{（姓名, 电话）: 行号} —— 按人比对，而不是按行序。

                行序会因删行/重排变化，但"谁在里面"才是重点，因此用姓名+电话做键；
                顺序差异不算问题。
                """
                grid = cli.read_grid(file_id, 1, 2, 300, 0, 2)
                rows: dict[int, dict[int, str]] = {}
                for (r, c), v in grid.items():
                    rows.setdefault(r, {})[c] = str(v).strip()
                return {(row.get(0, ""), row.get(2, "")): r + 1
                        for r, row in rows.items() if r >= 2 and row.get(0)}

            def probe(sheet: str, conf: dict[str, str]) -> dict[str, Any]:
                """核对单个子表（只读）。

                只读写自己的局部变量，不碰 ``self`` 的任何可变状态，因此可以安全地
                放进工作线程并发执行；结果与串行版本逐字段一致。
                """
                file_id = conf.get("file_id", "")
                prod_id = (production.get(sheet) or {}).get("file_id", "")
                item: dict[str, Any] = {"sheet": sheet, "file_id": file_id,
                                        "production_id": prod_id}
                try:
                    active_people = people(file_id)
                except WpsCloudError as exc:
                    item.update(status="unreadable", reason=str(exc)[:120])
                    return item
                item["rows"] = len(active_people)
                if not prod_id or prod_id == file_id:
                    # 写入目标就是正式表本身（未使用副本），无需比对。
                    item.update(status="same_as_production")
                    return item
                try:
                    prod_people = people(prod_id)
                except WpsCloudError as exc:
                    item.update(status="production_unreadable", reason=str(exc)[:120])
                    return item
                item["production_rows"] = len(prod_people)
                if set(active_people) == set(prod_people):
                    item.update(status="aligned")
                else:
                    prod_names = {k[0] for k in prod_people}
                    active_names = {k[0] for k in active_people}
                    missing = sorted(prod_names - active_names)
                    extra = sorted(active_names - prod_names)
                    if not missing and not extra:
                        # 姓名一致、只是电话不同
                        item.update(status="drifted", phone_mismatch=True)
                    else:
                        item.update(status="drifted", missing=missing[:10],
                                    extra=extra[:10])
                return item

            items = list(active.items())
            results: list[dict[str, Any]] = []
            if len(items) <= 1:
                # 单张表没有可重叠的往返，直接顺序执行，省掉线程池开销。
                results = [probe(sheet, conf) for sheet, conf in items]
            else:
                # ``Executor.map`` 按**提交顺序**产出结果，因此 tables/drifted
                # 的顺序与串行版本完全一致，前端展示不受影响。
                #
                # 分批提交而不是一次提交全部：``probe`` 只把 ``WpsCloudError``
                # 转成状态，其他意外异常会向上抛。若一次提交全部，抛错时已经
                # 发出去的调用收不回来（``cancel()`` 只能取消尚未开始的任务），
                # 最多会白耗 n-1 次云端调用；分批后最多只多耗 workers-1 次，
                # 更贴近串行版本「出错即停」的行为。
                workers = max(1, min(WPS_COPY_CHECK_WORKERS, len(items)))
                with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="wps-copy-check",
                ) as pool:
                    for start in range(0, len(items), workers):
                        batch = items[start:start + workers]
                        results.extend(pool.map(lambda pair: probe(*pair), batch))
        except WpsCloudError as exc:
            return {"ok": False, "reason": str(exc)}

        drifted = [r["sheet"] for r in results if r["status"] == "drifted"]
        for item in results:
            if item["status"] == "drifted":
                self.log(f"[云同步] 副本已过时：{item['sheet']}"
                         f"（正式 {item.get('production_rows')} 人 / 副本 {item.get('rows')} 人）",
                         "WARN")
        if drifted:
            self.log(f"[云同步] 建议重新同步副本：{'、'.join(drifted)}", "WARN")
        return {"ok": True, "drifted": drifted, "tables": results,
                "all_aligned": not drifted}

    def wps_authorize(self) -> dict[str, Any]:
        """启动 kdocs-cli 授权流程（在后台等用户在浏览器里确认）。"""
        try:
            cli = self._wps_cli()
        except WpsCloudError as exc:
            return {"ok": False, "reason": str(exc)}
        operation, conflict = self._reserve(
            "wps_authorize", title="云文档授权",
            next_action="等待授权流程结束（浏览器确认后自动结束）")
        if conflict is not None:
            return conflict
        threading.Thread(target=self._wps_authorize_worker, args=(cli, operation),
                         daemon=True).start()
        return {"ok": True, "hint": "已启动授权，请按日志里的提示在浏览器中确认"}

    def _wps_authorize_worker(self, cli: KdocsCli, operation: Any = None) -> None:
        import subprocess
        try:
            proc = subprocess.run(cli.login_argv(), capture_output=True, text=True,
                                  timeout=330)
            out = (proc.stdout or "") + (proc.stderr or "")
            for line in out.splitlines():
                if line.strip():
                    self.log(f"[云文档授权] {line.strip()}")
            if cli.authenticated():
                self.log("[云文档授权] 授权成功", "OK")
            else:
                self.log("[云文档授权] 未检测到有效授权，请重试", "WARN")
        except Exception as exc:  # noqa: BLE001
            self.log(f"[云文档授权] 失败：{type(exc).__name__}: {exc}", "ERROR")
        finally:
            self._operations.finish(operation, status="success")
            self._emit_event("wps:status", self.wps_status())

    def clear_password(self, mode: str = "order") -> dict[str, Any]:
        """删除本机密钥链里保存的密码。

        ``mode="sss"`` 删闪时送那把，**其余取值一律删管理后台那把**；账号为空时不调用
        密钥链（没存过就没什么可删）。

        **如实报告结果**：密钥环不可用（例如 Linux 上没有 SecretService）或删除失败时
        返回 ``ok=False`` 与原因，绝不能显示"已清除"却什么都没删 —— 用户会以为密码
        已经不存在了。账号为空同理（没有可删的东西）。
        """
        if mode == "sss":
            account = self._config.sss_account.strip()
            label = "闪时送密码"
            remove = delete_sss_password
        else:
            account = self._config.phone_number.strip()
            label = "密码"
            remove = delete_password
        if not account:
            return {"ok": False, "removed": False,
                    "reason": f"还没有填写账号，本机没有可清除的{label}",
                    "next_action": "先填写账号再清除，或直接关闭窗口"}
        if remove(account):
            self.log(f"已清除本机保存的{label}")
            return {"ok": True, "removed": True}
        self.log(f"未能清除本机保存的{label}（系统密钥环不可用或本来就没保存过）", "WARN")
        return {"ok": False, "removed": False,
                "reason": f"系统密钥环拒绝或无法删除本机保存的{label}",
                "next_action": "在系统密钥环里手动删除该条目，或检查密钥环服务是否可用"}

    def check_updates(self, manual: bool = False) -> dict[str, Any]:
        """启动更新检查（后台线程）。正在检查时返回 ``{"ok": False, "reason": "already_checking"}``。"""
        if self._update_checking:
            return {"ok": False, "reason": "already_checking"}
        operation, conflict = self._reserve(
            "check_update", title="检查更新", next_action="等待检查结束后重试")
        if conflict is not None:
            return {"ok": False, "reason": "operation_conflict",
                    "message": conflict["reason"],
                    "next_action": conflict["next_action"]}
        self._update_checking = True
        self._update_check_operation = operation
        self._set_status("updating")

        def _start() -> dict[str, Any]:
            self.log("正在检查更新...")
            threading.Thread(target=self._check_updates_worker,
                             args=(bool(manual),), daemon=True).start()
            return {"ok": True}

        result = self._guard_reserved(operation, _start, label="检查更新")
        if result.get("ok") is False:
            # 线程没起来：把"正在检查"的标记与状态一起还原，别留下永久 busy。
            self._update_checking = False
            self._update_check_operation = None
            self._set_status("ready")
        return result

    def _check_updates_worker(self, manual: bool) -> None:
        try:
            release = check_for_update()
            if release:
                self._pending_release = release
                self._emit_event("update:available", {
                    "tag": release.tag_name,
                    "current": __version__,
                    "body": release.body or "（暂无更新说明）",
                    "can_auto_install": _can_auto_install(),
                })
            elif manual:
                self._set_status("ready")
                self._emit_event("update:latest", {"manual": True, "current": __version__})
            else:
                self.log("已是最新版本")
                self._set_status("ready")
        except UpdateError as exc:
            self._set_status("error")
            self._emit_event("update:error", {"message": str(exc)})
        finally:
            self._update_checking = False
            operation, self._update_check_operation = getattr(
                self, "_update_check_operation", None), None
            self._operations.finish(operation, status="success")

    def install_update(self) -> dict[str, Any]:
        """安装已发现的更新；没有待安装版本时返回 ``{"ok": False, "reason": "no_release"}``。"""
        release = self._pending_release
        if release is None:
            return {"ok": False, "reason": "no_release"}
        operation, conflict = self._reserve(
            "install_update", title="安装更新",
            next_action="更新安装完成后程序会自动重启")
        if conflict is not None:
            return {"ok": False, "reason": "operation_conflict",
                    "message": conflict["reason"],
                    "next_action": conflict["next_action"]}
        self.log(f"获取更新清单完成，正在下载版本 {release.version}...")
        threading.Thread(target=self._install_update_worker,
                         args=(release, operation), daemon=True).start()
        return {"ok": True}

    def _install_update_worker(self, release: ReleaseInfo, operation: Any = None) -> None:
        try:
            download_and_install(
                release,
                progress_callback=lambda downloaded, total: self._emit_event(
                    "update:progress", {"downloaded": downloaded, "total": total}),
                stage_callback=lambda stage: self._emit_event("update:stage", {"stage": stage}),
            )
            self._emit_event("update:installed", {"message": "更新已下载，程序将重启"})
            # 对齐旧版行为：提示后自毁窗口，由更新器外部脚本替换二进制并重启。
            time.sleep(1.5)
            try:
                if self._window is not None:
                    self._window.destroy()
            except Exception:
                pass
        except Exception as exc:
            self._set_status("error")
            self._operations.finish(operation, status="error", reason=str(exc))
            self._emit_event("update:install_error", {"message": str(exc)})

    def open_external(self, url: str) -> dict[str, Any]:
        """用系统默认程序打开外链。

        **协议白名单**：只放行 ``http://`` 与 ``https://``，其余（含 ``file://``）一律忽略。
        恒返回 ``{"ok": True}``。
        """
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            webbrowser.open(url)
        return {"ok": True}

    # ------------------------------------------------------------------
    # js_api：前端回传通道（自动化验证与诊断用；JS→Python 方向可靠）
    # ------------------------------------------------------------------
    def frontend_report(self, payload: dict[str, Any] | str = "") -> dict[str, Any]:
        """前端把运行状态快照回传给 Python（例如渲染完成、收到的事件）。

        自动化验收依赖本通道而非 evaluate_js——后者在新版 WebKitGTK 上
        返回空值不可信。
        """
        with self._push_lock:
            self._reports.append({"ts": time.strftime("%H:%M:%S"), "payload": payload})
            del self._reports[:-50]
        return {"ok": True}

    def pop_reports(self) -> list[dict[str, Any]]:
        """取出并清空前端回传的运行快照（自动化验收用）。"""
        with self._push_lock:
            reports, self._reports = self._reports, []
        return reports

    # ------------------------------------------------------------------
    # js_api：窗口动作与关闭保护
    # ------------------------------------------------------------------
    def window_action(self, action: str) -> dict[str, Any]:
        """标题栏按钮动作：``minimize`` / ``toggle_maximize`` / ``close``。

        ``toggle_maximize`` 靠 ``_maximized`` 自己记状态（pywebview 没有「是否最大化」查询），
        在 maximize 与 restore 之间交替；``close`` 转交 :meth:`request_close`。
        没有窗口时返回 ``{"ok": False}``。
        """
        if self._window is None:
            return {"ok": False}
        if action == "minimize":
            self._window.minimize()
        elif action == "toggle_maximize":
            # pywebview 无「最大化/还原」状态查询；maximize 与 restore 成对调用。
            if getattr(self, "_maximized", False):
                self._window.restore()
                self._maximized = False
            else:
                self._window.maximize()
                self._maximized = True
        elif action == "close":
            return self.request_close()
        return {"ok": True}

    def begin_window_drag(self, x: float, y: float) -> dict[str, Any]:
        """自绘标题栏拖拽（Linux GTK）。

        pywebview 5.4 的 GTK 后端在 frameless + easy_drag=False 时完全不注册
        拖拽处理器，`pywebview-drag-region` CSS 类在其上无效；这里直接调用
        GTK 的 begin_move_drag，把后续拖动交还给窗口管理器。
        x/y 为 JS 事件的 screenX/screenY（X11 下即根窗口坐标）。
        Windows/macOS 走各自的 CSS 类拖拽机制，此方法直接忽略。
        """
        if not sys.platform.startswith("linux"):
            return {"ok": True, "handled": False}
        try:
            # pywebview 5.x/6.x 的 window.gui 都是平台模块；实例注册在
            # BrowserView.instances[window.uid]，其 .window 才是 Gtk.Window。
            from webview.platforms import gtk as gtk_module

            renderer = gtk_module.BrowserView.instances.get(self._window.uid)
            if renderer is None:
                raise RuntimeError("GTK 渲染器实例不存在")
            gtk_win = renderer.window
            # GDK 时间戳是 32 位毫秒（X 服务时间），系统纪元毫秒需截断，否则 OverflowError。
            timestamp = int(time.time() * 1000) & 0xFFFFFFFF
            gtk_win.begin_move_drag(1, int(x), int(y), timestamp)
            return {"ok": True, "handled": True}
        except Exception as exc:
            logger.warning("begin_window_drag 失败: %s", exc)
            return {"ok": False, "handled": False}

    def request_close(self) -> dict[str, Any]:
        """标题栏 ✕ / Alt+F4 共用的关闭入口，带任务运行保护。"""
        if self.worker_alive():
            # 先唤醒 captcha/普通 decision，避免它们继续阻塞 worker；保留
            # close_confirm 自身，防止重复点击关闭时自唤醒。
            self._cancel_pending_interactions("请求关闭窗口", except_kinds=frozenset({"close_confirm"}))
            choice = self._request_decision(
                "close_confirm", "正在处理",
                "任务仍在运行。停止并关闭，还是继续处理？",
                [{"value": "stop_and_close", "label": "停止并关闭", "style": "danger"},
                 {"value": "keep", "label": "继续处理", "style": "primary"},
                 {"value": "cancel", "label": "取消", "style": "neutral"}])
            if choice != "stop_and_close":
                return {"action": "kept"}
            self._stop_and_close()
            return {"action": "accepted"}
        self._closing = True
        threading.Thread(target=self._destroy_soon, daemon=True).start()
        return {"action": "accepted"}

    def _stop_and_close(self) -> None:
        self._closing = True
        self._stop_event.set()
        self._cancel_pending_interactions("停止并关闭")
        self.log("正在停止并清理浏览器，请稍候...")
        def watcher() -> None:
            while self._worker is not None and self._worker.is_alive():
                time.sleep(0.1)
            try:
                if self._window is not None:
                    self._window.destroy()
            except Exception:
                pass
        threading.Thread(target=watcher, daemon=True).start()

    def on_native_closing(self) -> bool:
        """pywebview closing 事件回调：返回 False 取消默认关闭。"""
        if self._closing or not self.worker_alive():
            return True
        # 原生关闭时 JS 可能已不可用；先取消等待中的交互，避免 worker 永久
        # 阻塞在 captcha/decision 上，再由 request_close 的有限等待收尾。
        self._cancel_pending_interactions("原生窗口关闭", except_kinds=frozenset({"close_confirm"}))
        threading.Thread(target=self.request_close, daemon=True).start()
        return False

    def _destroy_soon(self) -> None:
        # 让 request_close 的返回值先送达前端再销毁窗口。
        time.sleep(0.05)
        try:
            if self._window is not None:
                self._window.destroy()
        except Exception:
            pass

    def set_split_ratio(self, ratio: float) -> dict[str, Any]:
        """设置并落盘界面分隔比例（经 :func:`clamp_split_ratio` 夹紧）。"""
        self._config.split_ratio = clamp_split_ratio(ratio)
        try:
            self._config.save()
        except OSError:
            pass
        return {"ok": True, "ratio": self._config.split_ratio}


# ----------------------------------------------------------------------
# 模块级工具
# ----------------------------------------------------------------------
def _execution_summary_default(*, status: str, code: str, next_action: str,
                               proven_no_write: bool,
                               counts_source: str = "") -> dict[str, Any]:
    """拒绝路径的默认执行摘要：``proven_no_write`` 决定行数是否可证明为 0。"""
    return execution_summary(
        status=status, sheets=[], written=0, failed=0,
        uncertain=None if not proven_no_write else False,
        next_action=next_action or code, proven_no_write=proven_no_write,
        executed=False, counts_source=counts_source)


def _can_auto_install() -> bool:
    """Windows / Linux / macOS 打包版均支持自用自动更新。"""
    return (
        os.name == "nt"
        or sys.platform.startswith("linux")
        or sys.platform == "darwin"
    ) and getattr(sys, "frozen", False)


def _excel_field_error(path: str) -> str:
    """与旧 GUI 的 Excel 字段校验完全一致：存在 + 后缀。空路径视为「未选择」。"""
    if not path:
        return "请选择存在的 Excel 文件"
    candidate = _Path(path)
    if not candidate.is_file():
        return "请选择存在的 Excel 文件"
    if candidate.suffix.lower() not in EXCEL_EXTS:
        return "请选择 .xlsx 或 .xlsm 文件"
    return ""


def _with_excel_suffix(path: _Path) -> _Path:
    if path.suffix.lower() not in EXCEL_EXTS:
        return path.with_suffix(".xlsx")
    return path


def _apply_order_payload(cfg: AppConfig, p: dict[str, Any]) -> None:
    """就地更新订单处理侧配置字段（不触碰闪时送侧配置）。"""
    cfg.target_url = str(p.get("url", cfg.target_url) or "").strip()
    cfg.phone_number = str(p.get("phone", cfg.phone_number) or "").strip()
    excel = str(p.get("excel", "") or "").strip()
    if excel:
        cfg.excel_path = _Path(excel)
    cfg.order_date = str(p.get("date", cfg.order_date) or "").strip()
    if "count" in p:
        cfg.order_count = p["count"]  # None 表示「处理全部」，其余为 int|None
    cfg.api_mode = bool(p.get("api_mode", cfg.api_mode))


def _apply_sss_payload(cfg: AppConfig, p: dict[str, Any]) -> None:
    """就地更新闪时送侧配置字段（不触碰订单处理侧配置）。

    空值如实覆盖（用户清空输入就保存为空），与订单侧一致；真正开始下单时
    start_sss 会做非空校验，防抖保存本身不做启动校验。
    """
    cfg.sss_url = str(p.get("url", cfg.sss_url) or "").strip()
    cfg.sss_account = str(p.get("account", cfg.sss_account) or "").strip()
    excel = str(p.get("excel", "") or "").strip()
    if excel:
        cfg.sss_excel_path = _Path(excel)
    if "order_source" in p:
        source = str(p.get("order_source") or "").strip().lower()
        cfg.sss_order_source = "excel" if source == "excel" else "wps"
    cfg.sss_product_name = str(p.get("product_name", cfg.sss_product_name) or "").strip()
    cfg.sss_common_address = str(p.get("common_address", cfg.sss_common_address) or "").strip()
    use_fixed = bool(p.get("use_fixed_address", cfg.sss_use_fixed_address))
    cfg.sss_use_fixed_address = use_fixed
    if use_fixed:
        try:
            cfg.sss_fixed_lnt = float(p.get("fixed_lnt", cfg.sss_fixed_lnt))
        except (TypeError, ValueError):
            pass
        try:
            cfg.sss_fixed_lat = float(p.get("fixed_lat", cfg.sss_fixed_lat))
        except (TypeError, ValueError):
            pass
        cfg.sss_fixed_area_code = str(p.get("fixed_area_code", cfg.sss_fixed_area_code) or "").strip()
        cfg.sss_fixed_address_detail = str(p.get("fixed_address_detail", cfg.sss_fixed_address_detail) or "").strip()
    cfg.sss_dry_run = bool(p.get("dry_run", cfg.sss_dry_run))
    cfg.sss_preflight = bool(p.get("preflight", cfg.sss_preflight))
    cfg.api_mode = bool(p.get("api_mode", cfg.api_mode))
