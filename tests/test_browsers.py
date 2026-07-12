from app import automation


def test_detect_browsers_finds_macos_app_paths(monkeypatch, tmp_path):
    chrome = tmp_path / "Google Chrome.app/Contents/MacOS/Google Chrome"
    edge = tmp_path / "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
    chrome.parent.mkdir(parents=True)
    edge.parent.mkdir(parents=True)
    chrome.touch()
    edge.touch()
    monkeypatch.setattr(automation.sys, "platform", "darwin")
    monkeypatch.setattr(automation.os, "name", "posix")
    monkeypatch.setattr(automation.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(automation, "_macos_browser_paths", lambda browser: [edge if browser == "msedge" else chrome])
    monkeypatch.setattr(automation, "_playwright_chromium_path", lambda: None)
    monkeypatch.setattr(automation.shutil, "which", lambda _: None)
    result = automation.detect_browsers()
    assert result["chrome"].endswith("Google Chrome")
    assert result["msedge"].endswith("Microsoft Edge")


def test_detect_browsers_finds_playwright_chromium(monkeypatch):
    monkeypatch.setattr(automation.shutil, "which", lambda _: None)
    monkeypatch.setattr(automation, "_playwright_chromium_path", lambda: automation.Path("C:/tmp/chromium"))
    monkeypatch.setattr(automation.Path, "is_file", lambda self: str(self) == "C:\\tmp\\chromium")
    result = automation.detect_browsers()
    assert result["chromium"] == "C:\\tmp\\chromium"


def test_ensure_browser_installs_chromium_when_missing(monkeypatch):
    monkeypatch.setattr(automation, "detect_browsers", lambda: {"msedge": None, "chrome": None, "chromium": None})
    calls = []
    monkeypatch.setattr(automation, "_install_chromium", lambda: calls.append(True))
    monkeypatch.setattr(automation, "detect_browsers", lambda: ({"msedge": None, "chrome": None, "chromium": None} if not calls else {"msedge": None, "chrome": None, "chromium": "/tmp/chromium"}))
    monkeypatch.setattr(automation.sys, "frozen", False, raising=False)
    assert automation.ensure_browser() == "chromium"
    assert calls == [True]
