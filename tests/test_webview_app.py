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
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("GTK_IM_MODULE", raising=False)
    monkeypatch.setenv("XMODIFIERS", "@im=ibus")
    monkeypatch.delenv("QT_IM_MODULE", raising=False)

    _configure_linux_input_method()

    assert os.environ["GTK_IM_MODULE"] == "ibus"


def test_linux_input_method_keeps_existing_gtk_module(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("GTK_IM_MODULE", "fcitx")
    monkeypatch.setenv("XMODIFIERS", "@im=ibus")

    _configure_linux_input_method()

    assert os.environ["GTK_IM_MODULE"] == "fcitx"


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
