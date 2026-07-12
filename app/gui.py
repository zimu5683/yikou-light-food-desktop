"""Tkinter user interface for the order processor."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .automation import ensure_browser, run_job
from .config import AppConfig
from .credentials import delete_password, get_password, set_password


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("一口轻食订单处理")
        self.geometry("760x570")
        self.minsize(680, 480)
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self._build_widgets()
        self._load_saved_values()
        self.after(100, self._drain_events)

    def _build_widgets(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        fields = [("管理网址", "url"), ("手机号/账号", "phone"), ("Excel 文件", "excel")]
        self.vars: dict[str, tk.StringVar] = {key: tk.StringVar() for _, key in fields}
        for row, (label, key) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=6)
            ttk.Entry(frame, textvariable=self.vars[key]).grid(row=row, column=1, sticky="ew", pady=6)
            if key == "excel":
                ttk.Button(frame, text="选择", command=self._choose_excel).grid(row=row, column=2, padx=(8, 0))
        ttk.Label(frame, text="登录密码").grid(row=3, column=0, sticky="w", pady=6)
        self.password = tk.StringVar()
        ttk.Entry(frame, textvariable=self.password, show="*").grid(row=3, column=1, sticky="ew", pady=6)
        self.remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="保存密码到系统凭据管理器", variable=self.remember).grid(row=3, column=2, padx=(8, 0))
        ttk.Label(frame, text="待处理订单数").grid(row=4, column=0, sticky="w", pady=6)
        self.order_count = tk.StringVar(value="1")
        ttk.Spinbox(frame, from_=1, to=9999, textvariable=self.order_count, width=10).grid(row=4, column=1, sticky="w", pady=6)

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=3, sticky="w", pady=(12, 8))
        self.start_button = ttk.Button(buttons, text="开始处理", command=self.start)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(buttons, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="安装/检查浏览器", command=self.install_browser).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="清除已保存密码", command=self.clear_password).pack(side="left")

        ttk.Label(frame, text="运行日志").grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 4))
        self.log = tk.Text(frame, height=18, state="disabled", wrap="word")
        self.log.grid(row=7, column=0, columnspan=3, sticky="nsew")
        frame.rowconfigure(7, weight=1)

    def _load_saved_values(self) -> None:
        config = AppConfig.load()
        self.vars["url"].set(config.target_url)
        self.vars["phone"].set(config.phone_number)
        self.vars["excel"].set(config.excel_path)
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
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
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
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def start(self) -> None:
        try:
            config = self._config()
            count = int(self.order_count.get())
            if not config.excel_path.exists():
                raise ValueError("Excel 文件不存在，请重新选择")
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
            result = run_job(config, count, self.stop_event, lambda msg: self.events.put(("log", msg)), password=password)
            self.events.put(("done", f"处理完成：{result}"))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def stop(self) -> None:
        self.stop_event.set()
        self._append("已请求停止，正在等待浏览器操作结束...")

    def install_browser(self) -> None:
        self._append("正在检查浏览器...")
        threading.Thread(target=self._install_browser_worker, daemon=True).start()

    def _install_browser_worker(self) -> None:
        try:
            path = ensure_browser("auto")
            self.events.put(("log", f"浏览器可用：{path}"))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def clear_password(self) -> None:
        phone = self.vars["phone"].get().strip()
        if phone:
            delete_password(phone)
        self.password.set("")
        self._append("已清除本机保存的密码")


def main() -> None:
    App().mainloop()
