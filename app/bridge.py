"""pywebview 桥接层：把业务模块暴露给 Web 前端（js_api + 事件推送）。

职责边界：本模块只做「UI 协议」，业务规则全部留在原模块
（automation/sss/processing/updater/credentials/config）。旧 Tkinter 界面
（app/gui.py）里的每一条用户交互在这里都有对应实现，迁移对照表见
tests 与 README。

事件协议（Python → JS，经 ``window.__bridge.dispatch``）：
- log                {ts, level, msg}          结构化日志行
- status             {state}                   ready/running/stopping/success/stopped/error/updating
- task:done          {message, stopped}
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
"""
from __future__ import annotations

import os
import sys
import threading
import time
import logging
import webbrowser
from collections import deque
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

RETRY_CHOICES = [{"value": "retry", "label": "重试", "style": "primary"},
                 {"value": "skip", "label": "跳过", "style": "neutral"},
                 {"value": "stop", "label": "停止", "style": "danger"}]


class Bridge:
    """js_api 对象。公开方法（无下划线）均可被前端 Promise 调用。"""

    def __init__(self) -> None:
        self._window: Any = None
        self._config = AppConfig.load()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._closing = False
        self._status = "ready"
        self._reports: list[dict[str, Any]] = []
        self._event_queue: deque[dict[str, Any]] = deque()
        self._push_lock = threading.Lock()
        self._update_checking = False
        self._pending_release: ReleaseInfo | None = None
        self._decision_seq = 0
        self._decisions: dict[str, tuple[threading.Event, list[str]]] = {}

    # ------------------------------------------------------------------
    # 事件通道：队列 + 前端轮询
    #
    # 不用 evaluate_js 推送——它在 WebKitGTK 上并发调用会静默丢结果
    # （症状：日志行成对丢失）。改为旧 Tkinter 版验证过的
    # 「事件入队 + 前端定时 drain」模式，两个方向都只走可靠通道：
    # JS→Python = js_api 调用，Python→JS = drain_events 返回值。
    # ------------------------------------------------------------------
    def attach(self, window: Any) -> None:
        self._window = window

    def _emit_event(self, event: str, payload: Any = None) -> None:
        with self._push_lock:
            self._event_queue.append({"event": event, "payload": payload})
            while len(self._event_queue) > 500:
                self._event_queue.popleft()

    def drain_events(self) -> dict[str, Any]:
        """前端定时拉取事件（日志/状态/决策/更新进度）。"""
        with self._push_lock:
            events = list(self._event_queue)
            self._event_queue.clear()
        return {"events": events}

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
            "config": {
                "target_url": config.target_url,
                "phone_number": config.phone_number,
                "excel_path": str(config.excel_path) if config.excel_path else "",
                "order_date": config.order_date,
                "split_ratio": config.split_ratio,
                "sss_url": config.sss_url,
                "sss_account": config.sss_account,
                "sss_excel_path": str(config.sss_excel_path) if config.sss_excel_path else "",
                "sss_product_name": config.sss_product_name,
                "sss_common_address": config.sss_common_address,
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
        fields: dict[str, dict[str, str]] = {}
        url = str(payload.get("url", "")).strip()
        phone = str(payload.get("phone", "")).strip()
        password = str(payload.get("password", ""))
        excel = str(payload.get("excel", "")).strip()
        date_text = str(payload.get("date", "")).strip()
        try:
            count = int(str(payload.get("count", "0")))
        except (TypeError, ValueError):
            count = 0
        if not 1 <= count <= MAX_ORDER_COUNT:
            fields["count"] = {"message": f"请输入 1～{MAX_ORDER_COUNT} 的整数"}
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

        config = AppConfig(target_url=url, phone_number=phone, excel_path=excel,
                           order_date=date_text, browser_mode="auto",
                           api_mode=bool(payload.get("api_mode", True)))
        config.save()
        if payload.get("remember", True):
            set_password(phone, password)
        self._launch("order", config, count, password)
        return {"ok": True}

    def start_sss(self, payload: dict[str, Any]) -> dict[str, Any]:
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
        if fields:
            self._set_status("error")
            return {"ok": False, "fields": fields}

        config = AppConfig(
            sss_url=url, sss_account=account, sss_excel_path=excel,
            sss_product_name=str(payload.get("product_name", "轻食")).strip() or "轻食",
            sss_common_address=str(payload.get("common_address", "")).strip(),
            # dry_run：只组装并打印下单报文，不真实提交（联调/验收用）。
            sss_dry_run=bool(payload.get("dry_run", False)),
            browser_mode="auto",
            api_mode=bool(payload.get("api_mode", True)),
        )
        config.save()
        if payload.get("remember", True):
            set_sss_password(account, password)
        self._launch("sss", config, None, password)
        return {"ok": True}

    def _launch(self, mode: str, config: AppConfig, count: int | None, password: str) -> None:
        self._stop_event.clear()
        self._set_status("running")
        self.log("开始处理订单..." if mode == "order" else "开始闪时送下单...")
        target = self._run_order if mode == "order" else self._run_sss
        args = (config, count, password) if mode == "order" else (config, password)
        self._worker = threading.Thread(target=target, args=args, daemon=True)
        self._worker.start()

    def _run_order(self, config: AppConfig, count: int, password: str) -> None:
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
                                 captcha_callback=self._sss_captcha)
            self._finish_task(f"闪时送下单完成：已创建 {result.get('created', '?')} 单，"
                              f"处理 {result.get('processed', '?')} 项", result)
        except BrowserNotFoundError as exc:
            self._task_browser_missing(str(exc))
        except Exception as exc:
            self._task_error(str(exc))

    def _finish_task(self, message: str, result: dict[str, Any]) -> None:
        stopped = self._stop_event.is_set()
        self.log(message, "OK")
        self._worker = None
        # 顺手修正旧账：停止后的收尾不再显示「处理完成」，而是「已停止」。
        self._set_status("stopped" if stopped else "success")
        self._emit_event("task:done", {"message": message, "stopped": stopped, "result": result})

    def _task_error(self, message: str) -> None:
        self.log("错误: " + message, "ERROR")
        self._worker = None
        self._set_status("error")
        self._emit_event("task:error", {"message": message})

    def _task_browser_missing(self, message: str) -> None:
        self.log(message, "ERROR")
        self._worker = None
        self._set_status("error")
        self._emit_event("task:browser_missing", {"message": message})

    def stop_task(self) -> dict[str, Any]:
        if not self._worker or not self._worker.is_alive():
            return {"ok": False}
        self._stop_event.set()
        self._set_status("stopping")
        self.log("已请求停止，正在等待浏览器操作结束...")
        return {"ok": True}

    def worker_alive(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    # ------------------------------------------------------------------
    # js_api：阻塞式决策（旧 askyesnocancel 的异步等价物）
    # ------------------------------------------------------------------
    def _request_decision(self, kind: str, title: str, message: str,
                          choices: list[dict[str, str]]) -> str:
        with self._push_lock:
            self._decision_seq += 1
            decision_id = f"d{self._decision_seq}"
            decided = threading.Event()
            holder: list[str] = []
            self._decisions[decision_id] = (decided, holder)
        self._emit_event("decision", {"id": decision_id, "kind": kind, "title": title,
                                "message": message, "choices": choices})
        decided.wait()
        return holder[0]

    def resolve_decision(self, decision_id: str, choice: str) -> dict[str, Any]:
        entry = self._decisions.pop(str(decision_id), None)
        if entry is not None:
            decided, holder = entry
            holder.append(str(choice))
            decided.set()
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

        with self._push_lock:
            self._decision_seq += 1
            captcha_id = f"c{self._decision_seq}"
            decided = threading.Event()
            holder: list[str] = []
            self._decisions[captcha_id] = (decided, holder)
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        self._emit_event("captcha", {"id": captcha_id, "image": image_b64})
        decided.wait()
        return holder[0]

    def _sss_captcha(self, image_bytes: bytes) -> str:
        return self._request_captcha(image_bytes)

    def resolve_captcha(self, captcha_id: str, code: str) -> dict[str, Any]:
        entry = self._decisions.pop(str(captcha_id), None)
        if entry is not None:
            decided, holder = entry
            holder.append(str(code))
            decided.set()
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
    return (os.name == "nt" or sys.platform.startswith("linux")) and getattr(sys, "frozen", False)


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
