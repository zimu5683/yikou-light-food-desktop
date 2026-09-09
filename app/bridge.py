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
- task:browser_missing {message}
- update:available   {tag, current, body, can_auto_install}
- update:latest      {manual}
- update:error       {message}
- update:progress    {downloaded, total}
- update:stage       {stage}
- update:install_error {message}
- update:installed   {message}
- decision           {id, kind, title, message, choices}
- captcha            {id, image}
- events:dropped     {dropped_count, first_available_sequence, message}
"""
from __future__ import annotations

import copy
import os
import secrets
import sys
import threading
import time
import logging
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path as _Path
from typing import Any

from . import __version__
from .automation import BrowserNotFoundError, ensure_browser, parse_target_date, run_job
from .config import AppConfig, clamp_split_ratio
from .credentials import (delete_password, delete_sss_password, get_password,
                          get_sss_password, set_password, set_sss_password)
from .excel_templates import write_order_template, write_sss_template
from .sss import run_sss_job
from .updater import ReleaseInfo, UpdateError, check_for_update, download_and_install

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

RETRY_CHOICES = [{"value": "retry", "label": "重试", "style": "primary"},
                 {"value": "skip", "label": "跳过", "style": "neutral"},
                 {"value": "stop", "label": "停止", "style": "danger"}]


class _InteractionCancelled(RuntimeError):
    """交互请求被取消/超时，worker 必须按取消路径退出。"""


@dataclass
class _PendingInteraction:
    event: threading.Event
    holder: list[str] = field(default_factory=list)
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
        self._update_checking = False
        self._pending_release: ReleaseInfo | None = None
        self._decision_seq = 0
        self._decisions: dict[str, _PendingInteraction] = {}
        self._interaction_timeout_s = DEFAULT_INTERACTION_TIMEOUT_S

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
        self._emit_event("log", {"ts": time.strftime("%H:%M:%S"), "level": level, "msg": message.rstrip()})

    def _set_status(self, state: str) -> None:
        self._status = state
        self._emit_event("status", {"state": state})

    @property
    def status(self) -> str:
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
                "sss_product_name": config.sss_product_name,
                "sss_common_address": config.sss_common_address,
                "sss_use_fixed_address": config.sss_use_fixed_address,
                "sss_fixed_lnt": config.sss_fixed_lnt,
                "sss_fixed_lat": config.sss_fixed_lat,
                "sss_fixed_area_code": config.sss_fixed_area_code,
                "sss_fixed_address_detail": config.sss_fixed_address_detail,
                "sss_dry_run": config.sss_dry_run,
                "sss_idempotency_field": config.sss_idempotency_field,
                "api_mode": config.api_mode,
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
        if self.worker_alive():
            return {"ok": False, "reason": "busy", "message": "已有任务正在运行，请先停止后再启动", "fields": {}}
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
            return {"ok": False, "fields": fields}

        # 就地更新已加载配置并保存，避免用「全默认值新对象」覆盖另一半模式
        # （跑一次订单任务就把闪时送配置重置成默认值的同源问题）。
        _apply_order_payload(self._config, {
            "url": url, "phone": phone, "excel": excel, "date": date_text,
            "count": count, "api_mode": bool(payload.get("api_mode", True)),
        })
        self._config.save()
        if payload.get("remember", True):
            set_password(phone, password)
        # 运行线程拿到配置快照，避免任务执行期间被后续防抖保存改写。
        if self._launch("order", copy.deepcopy(self._config), count, password) is False:
            return {"ok": False, "reason": "busy", "message": "已有任务正在运行，请先停止后再启动", "fields": {}}
        return {"ok": True}

    def start_sss(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.worker_alive():
            return {"ok": False, "reason": "busy", "message": "已有任务正在运行，请先停止后再启动", "fields": {}}
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
        if excel_error:
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
            return {"ok": False, "fields": fields}

        # 就地更新并保存：避免全新 AppConfig 把订单处理侧配置重置成默认。
        _apply_sss_payload(self._config, {
            "url": url, "account": account, "excel": excel,
            "product_name": str(payload.get("product_name", "轻食")).strip() or "轻食",
            "common_address": str(payload.get("common_address", "")).strip(),
            "use_fixed_address": use_fixed_address,
            "fixed_lnt": fixed_lnt, "fixed_lat": fixed_lat,
            "fixed_area_code": fixed_area_code,
            "fixed_address_detail": fixed_address_detail,
            # dry_run：只组装并打印下单报文，不真实提交（联调/验收用）。
            "dry_run": bool(payload.get("dry_run", self._config.sss_dry_run)),
            "api_mode": bool(payload.get("api_mode", True)),
        })
        self._merge_sss_store_cache_from_disk()
        self._config.save()
        if payload.get("remember", True):
            set_sss_password(account, password)
        if self._launch("sss", copy.deepcopy(self._config), None, password) is False:
            return {"ok": False, "reason": "busy", "message": "已有任务正在运行，请先停止后再启动", "fields": {}}
        return {"ok": True}

    # 表单防抖即时保存：只落盘本次改动，不做启动校验、不触发任务。
    def save_order_config(self, payload: dict[str, Any]) -> dict[str, Any]:
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
        _apply_sss_payload(self._config, payload)
        try:
            self._config.save()
        except OSError:
            return {"ok": False, "reason": "write_failed"}
        return {"ok": True}

    def _launch(self, mode: str, config: AppConfig, count: int | None, password: str) -> bool:
        """启动 worker；已有任务时拒绝，返回 False（不覆盖在跑线程）。"""
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                self.log("已有任务在运行，拒绝并发启动", "WARN")
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
                             save_decision_callback=self._save_decision)
            self._finish_task(f"处理完成：已处理 {result.get('processed', '?')} 项，"
                              f"找到 {result.get('found', '?')} 项", result)
        except BrowserNotFoundError as exc:
            self._task_browser_missing(str(exc))
        except Exception as exc:
            self._task_error(str(exc))

    def _run_sss(self, config: AppConfig, password: str) -> None:
        try:
            result = run_sss_job(config, self._stop_event, lambda msg: self.log(msg),
                                 password=password, decision_callback=self._sss_decision,
                                 captcha_callback=self._sss_captcha,
                                 store_cache_callback=self._remember_sss_store_cache)
            if result.get("stopped"):
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
            self._task_browser_missing(str(exc))
        except Exception as exc:
            self._task_error(str(exc))

    def _finish_task(self, message: str, result: dict[str, Any]) -> None:
        stopped = bool(result.get("stopped", self._stop_event.is_set()))
        partial = bool(result.get("partial"))
        self._cancel_pending_interactions("任务结束")
        self.log(message, "OK")
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
        self._worker = None
        self._set_status("error")
        self._emit_event("task:error", {"message": message})

    def _task_browser_missing(self, message: str) -> None:
        self._cancel_pending_interactions("浏览器缺失")
        self.log(message, "ERROR")
        self._worker = None
        self._set_status("error")
        self._emit_event("task:browser_missing", {"message": message})

    def stop_task(self) -> dict[str, Any]:
        if not self._worker or not self._worker.is_alive():
            return {"ok": False}
        self._stop_event.set()
        # 等待 decision/captcha 的 worker 必须被唤醒，否则 stop 后仍永久挂起。
        self._cancel_pending_interactions("用户停止任务")
        self._set_status("stopping")
        self.log("已请求停止，正在等待浏览器操作结束...")
        return {"ok": True}

    def worker_alive(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    # ------------------------------------------------------------------
    # js_api：阻塞式决策（旧 askyesnocancel 的异步等价物）
    # ------------------------------------------------------------------
    def _interaction_default(self, kind: str) -> str:
        if kind == "captcha":
            return ""
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

    def _wait_interaction(self, interaction_id: str, entry: _PendingInteraction, *, default: str) -> str:
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
        return self._wait_interaction(decision_id, entry, default=self._interaction_default(kind))

    def resolve_decision(self, decision_id: str, choice: str) -> dict[str, Any]:
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
        with self._push_lock:
            entry = self._decisions.pop(str(captcha_id), None)
            if entry is not None:
                entry.holder.append(str(code))
                entry.event.set()
        return {"ok": entry is not None}

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
        import webview

        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False, file_types=FILE_DIALOG_FILTERS)
        path = result[0] if isinstance(result, (list, tuple)) and result else ""
        error = _excel_field_error(path)
        return {"path": path, "error": error}

    def new_template(self, mode: str = "order") -> dict[str, Any]:
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
        self._set_status("updating")
        self.log("正在检查浏览器...")
        threading.Thread(target=self._check_browser_worker, daemon=True).start()
        return {"ok": True}

    def _check_browser_worker(self) -> None:
        try:
            path = ensure_browser("auto")
            self.log(f"浏览器可用：{path}", "OK")
            self._set_status("ready")
        except Exception as exc:
            self._worker = None
            self._set_status("error")
            self._emit_event("task:error", {"message": str(exc)})

    def clear_password(self, mode: str = "order") -> dict[str, Any]:
        if mode == "sss":
            account = self._config.sss_account.strip()
            if account:
                delete_sss_password(account)
            self.log("已清除本机保存的闪时送密码")
        else:
            account = self._config.phone_number.strip()
            if account:
                delete_password(account)
            self.log("已清除本机保存的密码")
        return {"ok": True}

    def check_updates(self, manual: bool = False) -> dict[str, Any]:
        if self._update_checking:
            return {"ok": False, "reason": "already_checking"}
        self._update_checking = True
        self._set_status("updating")
        self.log("正在检查更新...")
        threading.Thread(target=self._check_updates_worker, args=(bool(manual),), daemon=True).start()
        return {"ok": True}

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

    def install_update(self) -> dict[str, Any]:
        release = self._pending_release
        if release is None:
            return {"ok": False, "reason": "no_release"}
        self.log(f"获取更新清单完成，正在下载版本 {release.version}...")
        threading.Thread(target=self._install_update_worker, args=(release,), daemon=True).start()
        return {"ok": True}

    def _install_update_worker(self, release: ReleaseInfo) -> None:
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
            self._emit_event("update:install_error", {"message": str(exc)})

    def open_external(self, url: str) -> dict[str, Any]:
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
        with self._push_lock:
            reports, self._reports = self._reports, []
        return reports

    # ------------------------------------------------------------------
    # js_api：窗口动作与关闭保护
    # ------------------------------------------------------------------
    def window_action(self, action: str) -> dict[str, Any]:
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
        self._config.split_ratio = clamp_split_ratio(ratio)
        try:
            self._config.save()
        except OSError:
            pass
        return {"ok": True, "ratio": self._config.split_ratio}


# ----------------------------------------------------------------------
# 模块级工具
# ----------------------------------------------------------------------
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
    cfg.api_mode = bool(p.get("api_mode", cfg.api_mode))
