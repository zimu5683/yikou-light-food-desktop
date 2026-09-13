"""Application entry point."""
from __future__ import annotations

import ctypes
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "app"


def _enable_high_dpi_awareness() -> str:
    """Enable per-monitor DPI awareness before a window is created (Windows)."""
    if os.name != "nt":
        return "unsupported"
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except (AttributeError, OSError, TypeError):
        pass
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE == 2.
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except (AttributeError, OSError, TypeError):
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError, TypeError):
        pass
    return "unavailable"


def main() -> None:
    if "--apply-update" in sys.argv:
        index = sys.argv.index("--apply-update")
        if len(sys.argv) < index + 3:
            raise SystemExit(2)
        from .updater import UpdateError, apply_pending_update
        try:
            apply_pending_update(sys.argv[index + 1], sys.argv[index + 2])
        except UpdateError as exc:
            # The updater copy has no console; show a native error dialog so a
            # replacement failure is no longer silent.
            try:
                ctypes.windll.user32.MessageBoxW(None, str(exc), "一口轻食更新失败", 0x10)
            except Exception:
                pass
            raise SystemExit(1)
        return
    if "--check-browser" in sys.argv or "--install-browser" in sys.argv:
        # 内置浏览器自检：打印 Chromium 路径与版本，缺失时以非零码退出。
        from .automation import (
            BrowserNotFoundError,
            browser_description,
            browser_version_warning,
            ensure_browser,
        )
        try:
            path = ensure_browser()
        except BrowserNotFoundError as exc:
            print(exc, file=sys.stderr)
            raise SystemExit(1) from None
        print(f"{browser_description()} {path}")
        if warning := browser_version_warning():
            print(f"警告：{warning}")
        return
    if "--version" in sys.argv:
        from . import __version__
        print(f"yikou-light-food {__version__}")
        return
    if "--self-check" in sys.argv:
        # 更新替换前由 updater 调用：只验证打包产物能导入关键模块，不启动 GUI。
        import app.automation  # noqa: F401
        import app.bridge  # noqa: F401
        import app.excel_templates  # noqa: F401
        import app.sss  # noqa: F401
        import app.updater  # noqa: F401
        import app.webview_app  # noqa: F401
        import app.wps_cloud  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: F401
            Ed25519PublicKey,
        )
        from . import __version__
        print(f"self-check OK {__version__}")
        return
    if "--wps-check" in sys.argv:
        # 打包后自检：确认随包分发的 kdocs-cli 能被找到、授权状态可读，
        # 并打印 6 个写入目标。只读，不写任何云端内容。
        from . import __version__
        from .config import AppConfig
        from .wps_cloud import KdocsCli, WpsCloudError

        cfg = AppConfig.load()
        print(f"yikou-light-food {__version__}")
        try:
            cli = KdocsCli(cfg.wps_cli_path or None)
            print(f"kdocs-cli: {cli.path}")
            print(f"authenticated: {cli.authenticated()}")
        except WpsCloudError as exc:
            print(f"kdocs-cli: 未找到（{exc}）")
        print(f"wps_enabled: {cfg.wps_enabled}  test_mode: {cfg.wps_test_mode}")
        targets = cfg.wps_test_tables if cfg.wps_test_mode else {
            sheet: conf.get("file_id", "") for sheet, conf in cfg.wps_tables.items()
        }
        print(f"{'测试副本' if cfg.wps_test_mode else '正式表'}目标 {len(targets)} 张：")
        for sheet, value in targets.items():
            conf = {"file_id": value} if isinstance(value, str) else value
            print(f"  {sheet} -> {conf.get('file_id', '')}")
        return
    _enable_high_dpi_awareness()
    from .webview_app import run as webview_run
    webview_run()


if __name__ == "__main__":
    main()
