"""pywebview 窗口启动器：加载 Web 前端并挂接桥接层（替代旧 app/gui.py 的 Tk 根窗口）。

前端加载优先级：
1. ``YIKOU_DEV_SERVER`` 环境变量（Vite 开发服务器，热更新调试）；
2. PyInstaller 打包内 ``frontend/index.html``（spec 会把 ``frontend/dist`` 以
   ``frontend`` 目录名放进 bundle）；
3. 仓库内 ``frontend/dist/index.html``（本地生产模式验证）。
"""
from __future__ import annotations

import os
import sys
import logging
from pathlib import Path

from .bridge import Bridge
from .config import user_data_dir

WINDOW_TITLE = "一口轻食 - 订单处理"
logger = logging.getLogger(__name__)


def _frontend_target() -> tuple[str, bool]:
    """返回 (加载目标, 是否调试模式)。"""
    dev_server = os.environ.get("YIKOU_DEV_SERVER", "").strip()
    if dev_server:
        return dev_server, True

    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "frontend" / "index.html")
    repo_root = Path(__file__).resolve().parent.parent
    candidates.append(repo_root / "frontend" / "dist" / "index.html")
    for candidate in candidates:
        if candidate.is_file():
            # A bare path is routed through pywebview's localhost asset
            # server. Use a real file URI for the bundled single HTML.
            return candidate.resolve().as_uri(), False

    raise RuntimeError(
        "未找到前端构建产物 frontend/dist/index.html。\n"
        "请先构建前端：cd frontend && pnpm install && pnpm build\n"
        "或以开发模式启动：YIKOU_DEV_SERVER=http://localhost:5173 python run.py"
    )


def run() -> None:
    try:
        import webview
    except (ImportError, ValueError) as exc:
        if sys.platform.startswith("linux"):
            raise RuntimeError(
                "Linux 图形后端不可用：请安装 GTK 3 和 WebKitGTK 4.0/4.1 的 Python 绑定（PyGObject）。"
            ) from exc
        raise RuntimeError("无法加载 pywebview 图形后端") from exc

    if sys.platform.startswith("linux"):
        # WebKitGTK 2.46+ 在部分驱动下整窗纯黑/纯白（DMABUF 合成 bug）。
        os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
        # WebKitGTK 2.52+ 的 bubblewrap 沙箱在受限父进程（CI/Xvfb、容器、
        # 自动化 Shell）下会静默杀死渲染进程（症状：页面加载完成但 DOM 全空）。
        # 本应用只渲染随包分发的自有内容，关闭渲染进程沙箱风险可接受。
        # 旧变量 WEBKIT_FORCE_SANDBOX 在 2.52 已失效，2.52 起用本变量。
        os.environ.setdefault("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS", "1")

    bridge = Bridge()
    try:
        target, debug = _frontend_target()
    except RuntimeError:
        logger.exception("前端构建产物检查失败")
        raise

    try:
        window = webview.create_window(
            WINDOW_TITLE,
            target,
            js_api=bridge,
            width=1080,
            height=720,
            min_size=(820, 620),
            # 无边框 + 自绘标题栏：拖拽区域由前端 `.pywebview-drag-region` 声明。
            frameless=True,
            easy_drag=False,
        )
    except Exception as exc:
        if sys.platform.startswith("linux"):
            raise RuntimeError(
                "Linux pywebview 窗口创建失败：请确认 GTK/WebKitGTK 运行库已安装，并检查 DISPLAY 或 Wayland 会话。"
            ) from exc
        raise RuntimeError("pywebview 窗口创建失败") from exc
    bridge.attach(window)
    window.events.closing += bridge.on_native_closing
    try:
        webview.start(
            debug=debug,
            # 关闭私有模式：让 localStorage / cookie 持久化，否则每次重启
            # 主题等本地设置都会回到默认浅色。
            private_mode=False,
            storage_path=str(user_data_dir() / "webview"),
        )
    except Exception as exc:
        if sys.platform.startswith("linux"):
            raise RuntimeError("Linux WebView 启动失败：GTK/WebKitGTK 初始化或渲染进程异常。") from exc
        raise RuntimeError("pywebview 启动失败") from exc
