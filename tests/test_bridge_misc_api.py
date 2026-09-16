"""``Bridge`` 里几个「杂项」js_api 的回归锁（改动前都未测过）。

这些方法单看都很小，但各自有一处容易写错、且出错后不易察觉的地方：

* ``choose_excel`` / ``new_template``：**对话框的返回值形态不止一种** ——
  pywebview 的 ``create_file_dialog`` 可能返回 ``list``、``tuple``、``str`` 或 ``None``。
  取错就会拿到空路径或 ``TypeError``。``new_template`` 还会在写盘失败时给出明确原因。
* ``open_external``：只放行 ``http(s)`` —— 这是**白名单**，写漏了就等于把
  ``file://`` 之类交给系统去打开。
* ``begin_window_drag``：只在 Linux GTK 上有意义，其它平台必须**原样返回
  ``handled=False``**，不能抛异常（它在窗口拖拽的每次 mouseDown 上被调用）。
* ``check_browser`` / ``check_updates``：都是「置状态 → 起后台线程 → 立刻返回」，
  以及 ``check_updates`` 的**防重入**守卫。
"""
from __future__ import annotations

import sys
import types

import pytest

from app import bridge as bridge_module
from app.bridge import Bridge


def _bridge(tmp_path) -> Bridge:
    return Bridge(config_path=str(tmp_path / "config.json"))


class _FakeWindow:
    def __init__(self, dialog_result) -> None:
        self.dialog_result = dialog_result
        self.dialog_calls: list[dict] = []

    def create_file_dialog(self, *args, **kwargs):
        self.dialog_calls.append({"args": args, "kwargs": kwargs})
        return self.dialog_result


@pytest.fixture
def fake_webview(monkeypatch):
    """`choose_excel` / `new_template` 在函数内 ``import webview``，这里塞一个替身。"""
    module = types.ModuleType("webview")
    module.OPEN_DIALOG = "open"
    module.SAVE_DIALOG = "save"
    monkeypatch.setitem(sys.modules, "webview", module)
    return module


@pytest.fixture
def no_threads(monkeypatch):
    """拦住后台线程，只记录「有没有起线程」。"""
    started: list[dict] = []

    class _FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.daemon = bool(kwargs.get("daemon"))

        def start(self):
            started.append(self.kwargs)

    monkeypatch.setattr(bridge_module.threading, "Thread", _FakeThread)
    return started


# ----------------------------------------------------------------------
# choose_excel
# ----------------------------------------------------------------------
def test_choose_excel_accepts_list_and_tuple(tmp_path, fake_webview):
    excel = tmp_path / "排单.xlsx"
    excel.write_bytes(b"x")

    for dialog_result in ([str(excel)], (str(excel),)):
        bridge = _bridge(tmp_path)
        bridge.attach(_FakeWindow(dialog_result))
        got = bridge.choose_excel("order")
        assert got == {"path": str(excel), "error": ""}, dialog_result


def test_choose_excel_does_not_accept_a_bare_string(tmp_path, fake_webview):
    """记录实际行为（与 ``new_template`` **不一致**）：这里只认 list/tuple。

    ``choose_excel`` 写的是 ``result[0] if isinstance(result, (list, tuple)) and result``，
    所以对话框若直接返回字符串会被当成「没选」。``new_template`` 则额外判断了 ``str``。
    当前 pywebview 打开对话框返回的是 list，所以不影响使用；写下来是因为这种
    「长得像却不一致」的兄弟方法最容易在改动时被顺手改错。
    """
    excel = tmp_path / "排单.xlsx"
    excel.write_bytes(b"x")
    bridge = _bridge(tmp_path)
    bridge.attach(_FakeWindow(str(excel)))

    got = bridge.choose_excel("order")

    assert got == {"path": "", "error": "请选择存在的 Excel 文件"}


@pytest.mark.parametrize("dialog_result", [None, "", [], (), ["", ],])
def test_choose_excel_reports_error_when_nothing_chosen(tmp_path, fake_webview, dialog_result):
    bridge = _bridge(tmp_path)
    bridge.attach(_FakeWindow(dialog_result))

    got = bridge.choose_excel("order")

    assert got["path"] == ""
    assert got["error"] == "请选择存在的 Excel 文件"


def test_choose_excel_rejects_a_non_excel_file(tmp_path, fake_webview):
    other = tmp_path / "名单.csv"
    other.write_bytes(b"x")
    bridge = _bridge(tmp_path)
    bridge.attach(_FakeWindow([str(other)]))

    got = bridge.choose_excel("order")

    assert got["path"] == str(other)
    assert "xlsx" in got["error"]


def test_choose_excel_uses_the_excel_file_filter(tmp_path, fake_webview):
    bridge = _bridge(tmp_path)
    window = _FakeWindow([])
    bridge.attach(window)

    bridge.choose_excel("order")

    assert window.dialog_calls[0]["args"][0] == "open"
    assert window.dialog_calls[0]["kwargs"]["file_types"] == bridge_module.FILE_DIALOG_FILTERS


# ----------------------------------------------------------------------
# new_template
# ----------------------------------------------------------------------
def test_new_template_save_name_depends_on_mode(tmp_path, fake_webview, monkeypatch):
    written: list[tuple[str, str]] = []
    monkeypatch.setattr(bridge_module, "write_order_template",
                        lambda dest: written.append(("order", str(dest))))
    monkeypatch.setattr(bridge_module, "write_sss_template",
                        lambda dest: written.append(("sss", str(dest))))

    for mode, expected_name in (("order", "排单.xlsx"), ("sss", "闪时送.xlsx")):
        bridge = _bridge(tmp_path)
        window = _FakeWindow(str(tmp_path / expected_name))
        bridge.attach(window)
        got = bridge.new_template(mode)
        assert window.dialog_calls[0]["kwargs"]["save_filename"] == expected_name
        assert got == {"path": str(tmp_path / expected_name), "error": ""}
    assert [kind for kind, _ in written] == ["order", "sss"]


def test_new_template_appends_xlsx_when_suffix_missing(tmp_path, fake_webview, monkeypatch):
    monkeypatch.setattr(bridge_module, "write_order_template", lambda dest: None)
    bridge = _bridge(tmp_path)
    bridge.attach(_FakeWindow(str(tmp_path / "新表")))

    got = bridge.new_template("order")

    assert got["path"].endswith("新表.xlsx"), "没有 Excel 后缀时要补上"


def test_new_template_cancelled_returns_empty_without_error(tmp_path, fake_webview, monkeypatch):
    """用户点「取消」不是错误 —— 不能弹红字。"""
    monkeypatch.setattr(bridge_module, "write_order_template",
                        lambda dest: pytest.fail("取消时不该写文件"))
    bridge = _bridge(tmp_path)
    bridge.attach(_FakeWindow(None))

    assert bridge.new_template("order") == {"path": "", "error": ""}


def test_new_template_write_failure_is_reported(tmp_path, fake_webview, monkeypatch):
    def boom(_dest):
        raise OSError("磁盘只读")

    monkeypatch.setattr(bridge_module, "write_order_template", boom)
    bridge = _bridge(tmp_path)
    bridge.attach(_FakeWindow(str(tmp_path / "排单.xlsx")))

    got = bridge.new_template("order")

    assert got["path"] == ""
    assert "无法写入模板文件" in got["error"] and "磁盘只读" in got["error"]


def test_new_template_logs_the_generated_path(tmp_path, fake_webview, monkeypatch):
    monkeypatch.setattr(bridge_module, "write_sss_template", lambda dest: None)
    bridge = _bridge(tmp_path)
    bridge.attach(_FakeWindow(str(tmp_path / "闪时送.xlsx")))

    bridge.new_template("sss")

    logs = [e["payload"]["msg"] for e in bridge.drain_events(0)["events"]
            if e["event"] == "log"]
    assert any("已生成闪时送模板" in line for line in logs)


# ----------------------------------------------------------------------
# open_external：只放行 http(s)
# ----------------------------------------------------------------------
@pytest.mark.parametrize("url", ["https://github.com/x", "http://example.com/a?b=c"])
def test_open_external_opens_http_and_https(tmp_path, monkeypatch, url):
    opened: list[str] = []
    monkeypatch.setattr(bridge_module.webbrowser, "open", opened.append)

    assert _bridge(tmp_path).open_external(url) == {"ok": True}

    assert opened == [url]


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "javascript:alert(1)", "ftp://x/y",
    "mailto:a@b.c", "", "github.com", "  https://x  ",
])
def test_open_external_ignores_anything_else(tmp_path, monkeypatch, url):
    """白名单之外的协议一律不交给系统打开（每次都返回 ok，不报错也不执行）。"""
    opened: list[str] = []
    monkeypatch.setattr(bridge_module.webbrowser, "open", opened.append)

    assert _bridge(tmp_path).open_external(url) == {"ok": True}

    assert opened == [], f"{url!r} 不该被打开"


def test_open_external_tolerates_non_string(tmp_path, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(bridge_module.webbrowser, "open", opened.append)
    assert _bridge(tmp_path).open_external(None) == {"ok": True}  # type: ignore[arg-type]
    assert opened == []


# ----------------------------------------------------------------------
# begin_window_drag：非 Linux 直接忽略
# ----------------------------------------------------------------------
def test_begin_window_drag_is_ignored_off_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module.sys, "platform", "win32")
    assert _bridge(tmp_path).begin_window_drag(10, 20) == {"ok": True, "handled": False}


def test_begin_window_drag_never_raises_when_gtk_is_unavailable(tmp_path, monkeypatch):
    """它挂在每次 mouseDown 上，出任何问题都只能返回 handled=False，不能抛。"""
    monkeypatch.setattr(bridge_module.sys, "platform", "linux")
    bridge = _bridge(tmp_path)
    bridge.attach(_FakeWindow(None))
    # 让 ``from webview.platforms import gtk`` 失败
    monkeypatch.setitem(sys.modules, "webview", None)
    monkeypatch.setitem(sys.modules, "webview.platforms", None)

    got = bridge.begin_window_drag(10, 20)

    assert got == {"ok": False, "handled": False}


# ----------------------------------------------------------------------
# check_browser / check_updates：置状态 → 起线程 → 立刻返回
# ----------------------------------------------------------------------
def test_check_browser_sets_status_and_starts_a_worker(tmp_path, no_threads):
    bridge = _bridge(tmp_path)

    assert bridge.check_browser() == {"ok": True}

    assert bridge.status == "updating"
    assert len(no_threads) == 1
    assert no_threads[0]["target"] == bridge._check_browser_worker
    logs = [e["payload"]["msg"] for e in bridge.drain_events(0)["events"]
            if e["event"] == "log"]
    assert any("检查浏览器" in line for line in logs)


def test_check_updates_starts_a_worker_with_the_manual_flag(tmp_path, no_threads):
    bridge = _bridge(tmp_path)

    assert bridge.check_updates(manual=True) == {"ok": True}

    assert bridge.status == "updating"
    assert no_threads[0]["args"] == (True,)


def test_check_updates_refuses_when_already_checking(tmp_path, no_threads):
    """防重入：正在检查时再点必须被挡掉，而不是起第二个线程。"""
    bridge = _bridge(tmp_path)
    bridge.check_updates()

    got = bridge.check_updates()

    assert got == {"ok": False, "reason": "already_checking"}
    assert len(no_threads) == 1, "不该起第二个线程"


def test_check_updates_defaults_to_not_manual(tmp_path, no_threads):
    bridge = _bridge(tmp_path)
    bridge.check_updates()
    assert no_threads[0]["args"] == (False,)
