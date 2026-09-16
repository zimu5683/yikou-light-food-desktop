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
    """命令行入口：``--apply-update`` / ``--check-browser`` / ``--version`` / ``--self-check`` / ``--wps-check`` / ``--sss-import-check`` 等子命令，无参数时启动 pywebview 窗口。"""
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
        import app.sss_import  # noqa: F401
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
    if "--sss-import-check" in sys.argv:
        # 「闪时送下单」云端名单只读自检：确认 kdocs-cli、生效目标表、当天列与
        # 标 1 人数（含被「大西/小」过滤掉的人数）。不写云端、不写本地任何文件。
        from . import __version__
        from .config import AppConfig
        from .sss_import import (SSS_SHEET_SOURCES, ImportRefused,
                                 collect_day_orders)
        from .wps_cloud import (KdocsCli, WpsCloudError, effective_tables,
                                target_date_for)

        cfg = AppConfig.load()
        print(f"yikou-light-food {__version__}")
        try:
            cli = KdocsCli(cfg.wps_cli_path or None)
        except WpsCloudError as exc:
            print(f"kdocs-cli: 未找到（{exc}）")
            raise SystemExit(1) from None
        print(f"kdocs-cli: {cli.path}")
        print(f"authenticated: {cli.authenticated()}")
        print(f"名单来源: {cfg.sss_order_source}  test_mode: {cfg.wps_test_mode}")
        target = target_date_for(start_hour=cfg.wps_target_hour_start,
                                 end_hour=cfg.wps_target_hour_end)
        print(f"识别日期: {target.isoformat()}（{target.month}.{target.day}）")
        try:
            tables = effective_tables(cfg)
        except WpsCloudError as exc:
            print(f"云端目标不可用：{exc}")
            raise SystemExit(1) from None
        for meal, table in SSS_SHEET_SOURCES.items():
            conf = tables.get(table) or {}
            print(f"  {meal} <- {table}  file_id={conf.get('file_id', '')}")
        try:
            meals = collect_day_orders(cfg, target=target, cli=cli)
        except ImportRefused as exc:
            print(f"读取失败：{exc}")
            raise SystemExit(1) from None
        for meal in meals:
            if meal.skipped:
                print(f"{meal.meal}（{meal.table}）：跳过 —— {meal.skip_reason}")
                continue
            print(f"{meal.meal}（{meal.table}）：当天列「{meal.date_text}」标 1 共 "
                  f"{meal.marked_total} 人，其中地址是大西/小 {meal.skipped_address} 人，"
                  f"实际下单 {meal.order_count} 人")
            for order in meal.orders[:5]:
                print(f"    {order['row']} 行 {order['name']} {order['door']} {order['phone']}")
            if meal.order_count > 5:
                print(f"    …其余 {meal.order_count - 5} 人已省略")
        return
    _enable_high_dpi_awareness()
    from .webview_app import run as webview_run
    webview_run()


if __name__ == "__main__":
    main()
