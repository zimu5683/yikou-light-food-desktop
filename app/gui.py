"""Tkinter user interface for the order processor."""
from __future__ import annotations

import queue
import os
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

from .automation import BrowserNotFoundError, ensure_browser, run_job
from .config import AppConfig
from .credentials import delete_password, get_password, set_password
from . import __version__
from .updater import ReleaseInfo, UpdateError, check_for_update, download_and_install


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("一口轻食 - 订单处理")
        self.geometry("980x700")
        self.minsize(820, 600)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self._closing = False
        self._log_lines: list[str] = []
        self._search_var = tk.StringVar()
        self.configure(bg="#f4f7fb")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_widgets()
        self._load_saved_values()
        self.after(100, self._drain_events)
        self.after(700, self.check_for_updates)

    def _build_widgets(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("App.TFrame", background="#f4f7fb")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("Title.TLabel", background="#f4f7fb", foreground="#17324d", font=("Segoe UI", 22, "bold"))
        style.configure("Subtitle.TLabel", background="#f4f7fb", foreground="#60758a", font=("Segoe UI", 10))
        style.configure("Section.TLabel", background="#ffffff", foreground="#17324d", font=("Segoe UI", 12, "bold"))
        style.configure("Field.TLabel", background="#ffffff", foreground="#425466", font=("Segoe UI", 10, "bold"))
        style.configure("TEntry", padding=8, fieldbackground="#fbfcfe")
        style.configure("TSpinbox", padding=6)
        style.configure("TButton", padding=(12, 7), font=("Segoe UI", 10))
        style.configure("Primary.TButton", background="#1677d2", foreground="#ffffff", padding=(18, 8), font=("Segoe UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", "#0f5eaa"), ("disabled", "#a8bdd3")])
        style.configure("Danger.TButton", foreground="#b42318")

        outer = ttk.Frame(self, style="App.TFrame", padding=(28, 24))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        ttk.Label(outer, text="一口轻食", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text="订单自动处理中心  |  配置任务并实时查看运行状态", style="Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 18))

        card = ttk.Frame(outer, style="Card.TFrame", padding=20)
        card.grid(row=2, column=0, sticky="nsew")
        card.columnconfigure(1, weight=1)
        card.rowconfigure(9, weight=1)
        ttk.Label(card, text="任务配置", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 14))
        fields = [("管理网址", "url"), ("手机号 / 账号", "phone"), ("Excel 文件", "excel")]
        self.vars: dict[str, tk.StringVar] = {key: tk.StringVar() for _, key in fields}
        for row, (label, key) in enumerate(fields, start=1):
            ttk.Label(card, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 14), pady=6)
            ttk.Entry(card, textvariable=self.vars[key]).grid(row=row, column=1, sticky="ew", pady=6)
            if key == "excel":
                ttk.Button(card, text="选择文件", command=self._choose_excel).grid(row=row, column=2, padx=(10, 0), pady=6)
        ttk.Label(card, text="登录密码", style="Field.TLabel").grid(row=4, column=0, sticky="w", padx=(0, 14), pady=6)
        self.password = tk.StringVar()
        ttk.Entry(card, textvariable=self.password, show="*").grid(row=4, column=1, sticky="ew", pady=6)
        self.remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(card, text="保存到系统凭据管理器", variable=self.remember).grid(row=4, column=2, padx=(10, 0), sticky="w")
        ttk.Label(card, text="待处理订单数", style="Field.TLabel").grid(row=5, column=0, sticky="w", padx=(0, 14), pady=6)
        self.order_count = tk.StringVar(value="1")
        ttk.Spinbox(card, from_=1, to=9999, textvariable=self.order_count, width=10).grid(row=5, column=1, sticky="w", pady=6)

        buttons = ttk.Frame(card, style="Card.TFrame")
        buttons.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(14, 12))
        self.start_button = ttk.Button(buttons, text="开始处理", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(buttons, text="停止", style="Danger.TButton", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="安装 / 检查浏览器", command=self.install_browser).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="清除已保存密码", command=self.clear_password).pack(side="left")
        ttk.Button(buttons, text="检查更新", command=lambda: self.check_for_updates(manual=True)).pack(side="left", padx=(8, 0))
        self.status = tk.StringVar(value="就绪")
        ttk.Label(buttons, textvariable=self.status, foreground="#60758a", background="#ffffff").pack(side="right")

        search = ttk.Frame(card, style="Card.TFrame")
        search.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(4, 4))
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text="搜索运行日志", style="Field.TLabel").grid(row=0, column=0, padx=(0, 8))
        search_entry = ttk.Entry(search, textvariable=self._search_var)
        search_entry.grid(row=0, column=1, sticky="ew")
        search_entry.bind("<Return>", lambda _event: self.search_log())
        ttk.Button(search, text="搜索", command=self.search_log).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(search, text="清除", command=self.clear_log_search).grid(row=0, column=3, padx=(8, 0))

        ttk.Label(card, text="运行日志", style="Section.TLabel").grid(row=8, column=0, columnspan=3, sticky="w", pady=(12, 6))
        log_frame = ttk.Frame(card, style="Card.TFrame")
        log_frame.grid(row=9, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=12, state="disabled", wrap="word", bg="#172333", fg="#d8e6f3", insertbackground="#ffffff", relief="flat", padx=12, pady=10, font=("Consolas", 10))
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

    def _load_saved_values(self) -> None:
        config = AppConfig.load()
        self.vars["url"].set(config.target_url)
        self.vars["phone"].set(config.phone_number)
        self.vars["excel"].set(str(config.excel_path) if config.excel_path else "")
        self.password.set(get_password(config.phone_number) or "")

    def _choose_excel(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Excel 文件", "*.xlsx *.xlsm"), ("所有文件", "*.*")])
        if path:
            self.vars["excel"].set(path)

    def _config(self) -> AppConfig:
        try:
            count = int(self.order_count.get())
            if count < 1:
                raise ValueError
        except ValueError as exc:
            raise ValueError("待处理订单数必须是大于等于 1 的整数") from exc
        return AppConfig(target_url=self.vars["url"].get().strip(), phone_number=self.vars["phone"].get().strip(),
                         excel_path=self.vars["excel"].get().strip(), browser_mode="auto")

    def _append(self, text: str) -> None:
        self._log_lines.append(text.rstrip())
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def search_log(self) -> None:
        query = self._search_var.get().strip().lower()
        if not query:
            return
        for index, line in enumerate(self._log_lines):
            if query in line.lower():
                self.log.configure(state="normal")
                self.log.tag_remove("search_hit", "1.0", "end")
                start = f"{index + 1}.0"
                end = f"{index + 1}.end"
                self.log.tag_add("search_hit", start, end)
                self.log.tag_configure("search_hit", background="#fff2a8")
                self.log.see(start)
                self.log.configure(state="disabled")
                return
        messagebox.showinfo("搜索结果", f"未找到包含“{self._search_var.get().strip()}”的订单日志。")

    def clear_log_search(self) -> None:
        self._search_var.set("")
        self.log.configure(state="normal")
        self.log.tag_remove("search_hit", "1.0", "end")
        self.log.configure(state="disabled")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self._append(value)
                elif kind == "done":
                    self._append(value)
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.worker = None
                elif kind == "error":
                    self._append("错误: " + value)
                    messagebox.showerror("处理失败", value)
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.worker = None
                elif kind == "browser_missing":
                    self._append(value)
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.worker = None
                    self.choose_browser_download()
                elif kind == "update":
                    release = value
                    if isinstance(release, ReleaseInfo):
                        details = release.body or "（暂无更新说明）"
                        can_auto_install = os.name == "nt" and getattr(sys, "frozen", False)
                        action = "是否立即下载并安装？" if can_auto_install else "是否打开 GitHub Release 下载页面？"
                        prompt = f"发现新版本 {release.tag_name}（当前版本 {__version__}）\n\n更新内容：\n{details}\n\n{action}"
                        if messagebox.askyesno("发现新版本", prompt):
                            if can_auto_install:
                                self._install_update(release)
                            elif release.html_url:
                                webbrowser.open(release.html_url)
                elif kind == "update_latest":
                    messagebox.showinfo("检查更新", f"当前已是最新版本（{__version__}）。")
                elif kind == "update_error":
                    self._append("检查更新失败：" + value)
                elif kind == "update_progress":
                    downloaded, total = value
                    self._set_update_progress(downloaded, total)
                elif kind == "update_install_error":
                    self._close_update_progress()
                    self._append("更新失败：" + value)
                    messagebox.showerror("更新失败", value)
                elif kind == "update_installed":
                    self._append(value)
                    self._set_update_progress(1, 1)
                    messagebox.showinfo("更新完成", "更新已下载，点击确定后程序将关闭并自动重启。")
                    self._closing = True
                    self.destroy()
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _on_close(self) -> None:
        if not self.worker or not self.worker.is_alive():
            self.destroy()
            return
        if self._closing:
            return
        action = messagebox.askyesnocancel("正在处理", "任务仍在运行。点击“是”停止并关闭，点击“否”继续处理，点击“取消”返回。")
        if action is None or action is False:
            return
        self._closing = True
        self.stop_event.set()
        self._append("正在停止并清理浏览器，请稍候...")
        self.after(100, self._wait_for_worker_close)

    def _wait_for_worker_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.after(100, self._wait_for_worker_close)
        else:
            self.destroy()

    def start(self) -> None:
        try:
            config = self._config()
            count = int(self.order_count.get())
            if not config.excel_path or not config.excel_path.is_file():
                raise ValueError("Excel 文件不存在，请重新选择")
            if config.excel_path.suffix.lower() not in {".xlsx", ".xlsm"}:
                raise ValueError("请选择 .xlsx 或 .xlsm Excel 文件")
            if not config.url or not config.phone or not self.password.get():
                raise ValueError("请填写网址、账号和密码")
        except ValueError as exc:
            messagebox.showwarning("配置不完整", str(exc))
            return
        config.save()
        if self.remember.get():
            set_password(config.phone_number, self.password.get())
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._append("开始处理订单...")
        self.worker = threading.Thread(target=self._run, args=(config, count, self.password.get()), daemon=True)
        self.worker.start()

    def _run(self, config: AppConfig, count: int, password: str) -> None:
        try:
            result = run_job(config, count, self.stop_event, lambda msg: self.events.put(("log", msg)), password=password,
                             order_decision_callback=self._order_decision,
                             save_decision_callback=self._save_decision)
            self.events.put(("done", f"处理完成：{result}"))
        except BrowserNotFoundError as exc:
            self.events.put(("browser_missing", str(exc)))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def stop(self) -> None:
        if not self.worker or not self.worker.is_alive():
            return
        action = messagebox.askyesnocancel("暂停处理", "是否停止当前任务？点击“是”停止，点击“否”继续处理，点击“取消”返回。")
        if action:
            self.stop_event.set()
            self._append("已请求停止，正在等待浏览器操作结束...")

    def _order_decision(self, code: str, error: str) -> str:
        result: queue.Queue[str] = queue.Queue(maxsize=1)
        def ask() -> None:
            choice = messagebox.askyesnocancel("订单定位失败", f"订单 {code} 定位失败：\n{error}\n\n是=重试，否=跳过，取消=停止")
            result.put("retry" if choice is True else "skip" if choice is False else "stop")
        self.after(0, ask)
        return result.get()

    def _save_decision(self, error: str) -> str:
        result: queue.Queue[str] = queue.Queue(maxsize=1)
        def ask() -> None:
            choice = messagebox.askretrycancel(
                "Excel 文件正在使用",
                "保存失败，Excel 文件可能正在被打开或占用。\n请关闭 Excel 文件后点击“重试保存”。\n\n" + error,
            )
            result.put("retry" if choice else "cancel")
        self.after(0, ask)
        return result.get()

    def install_browser(self) -> None:
        self._append("正在检查浏览器...")
        threading.Thread(target=self._install_browser_worker, daemon=True).start()

    def _install_browser_worker(self) -> None:
        try:
            path = ensure_browser("auto")
            self.events.put(("log", f"浏览器可用：{path}"))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def choose_browser_download(self) -> None:
        choice = messagebox.askyesno("未安装浏览器", "打开 Microsoft Edge 下载页面吗？选择“否”将打开 Google Chrome 下载页面。")
        webbrowser.open("https://www.microsoft.com/edge/download" if choice else "https://www.google.com/chrome/")

    def check_for_updates(self, manual: bool = False) -> None:
        if getattr(self, "_update_checking", False):
            return
        self._update_checking = True
        self._append("正在检查更新...")
        threading.Thread(target=self._check_updates_worker, args=(manual,), daemon=True).start()

    def _check_updates_worker(self, manual: bool) -> None:
        try:
            release = check_for_update()
            self.events.put(("update", release) if release else ("update_latest", "") if manual else ("log", "已是最新版本"))
        except UpdateError as exc:
            self.events.put(("update_error", str(exc)))
        finally:
            self._update_checking = False

    def _install_update(self, release: ReleaseInfo) -> None:
        self._append(f"正在下载版本 {release.version}...")
        self._show_update_progress(release)
        threading.Thread(target=self._install_update_worker, args=(release,), daemon=True).start()

    def _install_update_worker(self, release: ReleaseInfo) -> None:
        try:
            download_and_install(
                release,
                progress_callback=lambda downloaded, total: self.events.put(("update_progress", (downloaded, total))),
            )
            self.events.put(("update_installed", "更新已下载，程序将重启"))
        except Exception as exc:
            self.events.put(("update_install_error", str(exc)))

    def _show_update_progress(self, release: ReleaseInfo) -> None:
        existing = getattr(self, "_update_progress_window", None)
        if existing and existing.winfo_exists():
            existing.lift()
            return
        dialog = tk.Toplevel(self)
        self._update_progress_window = dialog
        dialog.title("正在更新")
        dialog.geometry("480x170")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)
        content = ttk.Frame(dialog, padding=22)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text=f"正在下载一口轻食 {release.tag_name}", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self._update_progress_text = tk.StringVar(value="正在连接 GitHub...")
        ttk.Label(content, textvariable=self._update_progress_text).pack(anchor="w", pady=(8, 8))
        self._update_progressbar = ttk.Progressbar(content, maximum=100, mode="indeterminate")
        self._update_progressbar.pack(fill="x")
        self._update_progressbar.start(12)
        ttk.Label(content, text="下载完成后程序会自动关闭、替换并重新启动。", foreground="#60758a").pack(anchor="w", pady=(10, 0))
        dialog.grab_set()

    def _set_update_progress(self, downloaded: int, total: int | None) -> None:
        dialog = getattr(self, "_update_progress_window", None)
        if not dialog or not dialog.winfo_exists():
            return
        downloaded_mb = downloaded / (1024 * 1024)
        if total:
            self._update_progressbar.stop()
            self._update_progressbar.configure(mode="determinate", value=min(downloaded * 100 / total, 100))
            self._update_progress_text.set(f"已下载 {downloaded_mb:.1f} MB / {total / (1024 * 1024):.1f} MB（{min(downloaded * 100 / total, 100):.0f}%）")
        else:
            self._update_progress_text.set(f"已下载 {downloaded_mb:.1f} MB")

    def _close_update_progress(self) -> None:
        dialog = getattr(self, "_update_progress_window", None)
        if dialog and dialog.winfo_exists():
            self._update_progressbar.stop()
            dialog.grab_release()
            dialog.destroy()

    def clear_password(self) -> None:
        phone = self.vars["phone"].get().strip()
        if phone:
            delete_password(phone)
        self.password.set("")
        self._append("已清除本机保存的密码")


def main() -> None:
    App().mainloop()
