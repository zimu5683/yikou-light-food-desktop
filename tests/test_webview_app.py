from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

from app.webview_app import _frontend_target


def test_production_frontend_uses_file_uri() -> None:
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
    assert Path(unquote(urlparse(target).path)) == frozen_index
