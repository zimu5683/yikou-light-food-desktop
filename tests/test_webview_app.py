from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import pytest

from app.webview_app import _configure_linux_input_method, _frontend_target


def test_production_frontend_uses_file_uri() -> None:
    dist_index = Path(__file__).resolve().parent.parent / "frontend" / "dist" / "index.html"
    if not dist_index.is_file():
        # Tests 工作流不构建前端（release 工作流才构建）；无产物时跳过。
        pytest.skip("frontend/dist/index.html 尚未构建")
    target, debug = _frontend_target()

    assert debug is False
    parsed = urlparse(target)
    assert parsed.scheme == "file"
    assert Path(unquote(parsed.path)).is_file()
    assert Path(unquote(parsed.path)).name == "index.html"


def test_dev_server_target_is_preserved(monkeypatch) -> None:
    dev_url = "http://127.0.0.1:5173/"
    monkeypatch.setenv("YIKOU_DEV_SERVER", dev_url)

    target, debug = _frontend_target()

    assert target == dev_url
    assert debug is True


def test_frozen_frontend_target_is_file_uri(monkeypatch, tmp_path: Path) -> None:
    frozen_frontend = tmp_path / "frontend"
    frozen_frontend.mkdir()
    frozen_index = frozen_frontend / "index.html"
    frozen_index.write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)
    monkeypatch.delenv("YIKOU_DEV_SERVER", raising=False)

    target, debug = _frontend_target()

    assert debug is False
    assert urlparse(target).scheme == "file"
    assert Path(url2pathname(unquote(urlparse(target).path))) == frozen_index


def test_linux_input_method_gtk_module_is_filled_from_ibus(monkeypatch) -> None:
    """X11 会话下、ibus 在跑：补上 GTK_IM_MODULE=ibus（GTK3 不会从 XMODIFIERS 推断）。"""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("GTK_IM_MODULE", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("XMODIFIERS", "@im=ibus")
    monkeypatch.delenv("QT_IM_MODULE", raising=False)
    monkeypatch.setattr("app.webview_app._running_input_method", lambda: "ibus")

    _configure_linux_input_method()

    assert os.environ["GTK_IM_MODULE"] == "ibus"


def test_linux_input_method_keeps_existing_gtk_module(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("GTK_IM_MODULE", "fcitx")
    monkeypatch.setenv("XMODIFIERS", "@im=ibus")

    _configure_linux_input_method()

    assert os.environ["GTK_IM_MODULE"] == "fcitx"


def test_linux_wayland_does_not_force_gtk_im_module(monkeypatch) -> None:
    """Wayland 下绝不能替用户设置 GTK_IM_MODULE。

    Wayland 上 GTK3 走合成器的 text-input 协议（im-wayland.so）就能打中文；强行
    设成 ibus/fcitx 会换成传统 X11 模块，输入法反而失效 —— 这就是"程序里打不出
    中文、别的程序都正常"的根因。
    """
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("GTK_IM_MODULE", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XMODIFIERS", "@im=ibus")
    monkeypatch.setenv("QT_IM_MODULE", "ibus")
    monkeypatch.setattr("app.webview_app._running_input_method", lambda: "ibus")

    _configure_linux_input_method()

    assert "GTK_IM_MODULE" not in os.environ


def test_linux_wayland_detected_without_session_type(monkeypatch) -> None:
    """没有 XDG_SESSION_TYPE 但存在 WAYLAND_DISPLAY 时，同样按 Wayland 处理。"""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("GTK_IM_MODULE", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XMODIFIERS", "@im=ibus")

    _configure_linux_input_method()

    assert "GTK_IM_MODULE" not in os.environ


def test_linux_x11_prefers_running_daemon_over_stale_env(monkeypatch) -> None:
    """装了 fcitx5 但环境变量还留着 @im=ibus 时，按**实际在跑的**守护进程选 fcitx5。"""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("GTK_IM_MODULE", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("XMODIFIERS", "@im=ibus")
    monkeypatch.setenv("QT_IM_MODULE", "ibus")
    monkeypatch.setattr("app.webview_app._running_input_method", lambda: "fcitx5")

    _configure_linux_input_method()

    assert os.environ["GTK_IM_MODULE"] == "fcitx5"


def test_linux_x11_falls_back_to_env_hint_when_no_daemon(monkeypatch) -> None:
    """探测不到守护进程时退回环境变量提示，保持旧行为。"""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("GTK_IM_MODULE", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("XMODIFIERS", "@im=ibus")
    monkeypatch.setattr("app.webview_app._running_input_method", lambda: "")

    _configure_linux_input_method()

    assert os.environ["GTK_IM_MODULE"] == "ibus"


def test_linux_x11_leaves_module_unset_when_nothing_detected(monkeypatch) -> None:
    """既没探测到守护进程、环境变量也没有提示：什么都不设（让 GTK 自己选）。"""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("GTK_IM_MODULE", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    for key in ("XMODIFIERS", "QT_IM_MODULE", "QT_IM_MODULES", "INPUT_METHOD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("app.webview_app._running_input_method", lambda: "")

    _configure_linux_input_method()

    assert "GTK_IM_MODULE" not in os.environ


def test_running_input_method_sees_ibus_on_this_machine() -> None:
    """本机（或任何有 /proc 的 Linux）探测不应抛异常；返回值必须是已知模块名之一。"""
    from app.webview_app import _running_input_method

    assert _running_input_method() in ("", "ibus", "fcitx", "fcitx5")


def test_running_input_method_prefers_fcitx5_over_ibus(monkeypatch) -> None:
    """comm 里同时出现 fcitx5 和 ibus 时选 fcitx5（现代 fcitx 优先）。"""
    import io

    import app.webview_app as wa

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    def fake_scandir(_path):
        return iter([Entry("1"), Entry("2"), Entry("3"), Entry("self")])

    real_open = open

    def fake_open(path, *args, **kwargs):
        if str(path).endswith("/comm"):
            pid = str(path).split("/")[2]
            return io.StringIO({"1": "ibus-daemon\n", "2": "fcitx5\n",
                                "3": "bash\n"}[pid])
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(wa.os, "scandir", fake_scandir)
    monkeypatch.setattr("builtins.open", fake_open)

    assert wa._running_input_method() == "fcitx5"


def test_gui_ready_writes_health_marker_without_window_event(tmp_path, monkeypatch) -> None:
    """健康标记必须在 GUI 循环起来时就写出，**不能**等窗口的 shown 事件。

    pywebview 的 shown 事件来自 WebKit 的 notify::visible，在部分 Wayland/WebKitGTK
    组合上不会触发；靠它会让新版启动成功却等不到标记，被更新脚本回滚
    （本机 update.log：'health marker timeout; rolling back to previous version'）。
    """
    import app.webview_app as wa

    marker = tmp_path / "health.json"
    monkeypatch.setenv("YIKOU_UPDATE_HEALTH_FILE", str(marker))
    monkeypatch.setenv("YIKOU_UPDATE_HEALTH_TOKEN", "tok-123")
    monkeypatch.setattr("sys.platform", "win32")   # 跳过 GLib 分支，专测直接写入

    wa.mark_gui_started()

    assert marker.is_file(), "健康标记没有写出来"
    assert "tok-123" in marker.read_text(encoding="utf-8")


def test_mark_startup_healthy_is_noop_without_marker_env(monkeypatch, tmp_path) -> None:
    """普通启动（更新器没设标记路径）时不该产生任何文件。"""
    import app.webview_app as wa

    monkeypatch.delenv("YIKOU_UPDATE_HEALTH_FILE", raising=False)
    monkeypatch.delenv("YIKOU_UPDATE_HEALTH_TOKEN", raising=False)

    wa.mark_gui_started()      # 不抛异常即可

    assert not list(tmp_path.iterdir())


def test_main_self_check_imports_critical_modules(monkeypatch, capsys):
    from app.main import main

    monkeypatch.setattr("sys.argv", ["yikou-light-food", "--self-check"])
    main()
    assert "self-check OK" in capsys.readouterr().out


def test_update_health_marker_roundtrip(tmp_path, monkeypatch):
    from app.update_health import mark_startup_healthy, wait_for_health

    marker = tmp_path / "health.json"
    monkeypatch.setenv("YIKOU_UPDATE_HEALTH_FILE", str(marker))
    monkeypatch.setenv("YIKOU_UPDATE_HEALTH_TOKEN", "token-123")
    mark_startup_healthy("3.1.0")

    assert wait_for_health(marker, "token-123", timeout=0.5)
    assert not wait_for_health(marker, "wrong-token", timeout=0.01)


# ----------------------------------------------------------------------
# 改动前完全未被引用的 begin_update_health_check
# ----------------------------------------------------------------------
def test_begin_update_health_check_returns_unique_token_and_marker(tmp_path):
    """更新器靠「唯一 token + 唯一标记文件」区分『二进制已替换』与『GUI 真起来了』。

    token 或路径重复会让健康检查误判成功 → 坏版本不会被回滚。
    """
    from app.update_health import begin_update_health_check

    first_token, first_marker = begin_update_health_check(tmp_path)
    second_token, second_marker = begin_update_health_check(tmp_path)

    assert first_token != second_token
    assert first_marker != second_marker
    assert len(first_token) == 32 and all(c in "0123456789abcdef" for c in first_token)


def test_begin_update_health_check_places_marker_inside_directory(tmp_path):
    import os
    from pathlib import Path

    from app.update_health import begin_update_health_check

    token, marker = begin_update_health_check(tmp_path)
    path = Path(marker)

    assert path.parent == tmp_path
    assert str(os.getpid()) in path.name
    assert token[:8] in path.name
    # 只算出路径，不该把文件真的建出来（要等 GUI 启动后才写）
    assert not path.exists()


def test_begin_update_health_check_removes_stale_marker(tmp_path, monkeypatch):
    """同一路径上若已存在陈旧标记，必须被清掉。

    标记名里带随机 token，正常调用永远算不出同一个路径，所以这里把
    ``secrets.token_hex`` 固定住，专门覆盖那行 ``marker.unlink``。
    """
    import os
    from pathlib import Path

    from app.update_health import begin_update_health_check

    monkeypatch.setattr("app.update_health.secrets.token_hex", lambda _n: "ab" * 16)
    expected = tmp_path / f".yikou-update-health-{os.getpid()}-abababab.json"
    expected.write_text("陈旧的标记", encoding="utf-8")

    token, marker = begin_update_health_check(tmp_path)

    assert Path(marker) == expected
    assert token == "ab" * 16
    assert not expected.exists(), "陈旧标记必须被清掉，否则会被误判成启动成功"


def test_begin_update_health_check_tolerates_missing_directory(tmp_path):
    """目录不存在时不该抛错（只算路径，不建目录、不建文件）。"""
    from pathlib import Path

    from app.update_health import begin_update_health_check

    target = tmp_path / "还不存在"
    _token, marker = begin_update_health_check(target)

    assert Path(marker).parent == target
    assert not target.exists()
