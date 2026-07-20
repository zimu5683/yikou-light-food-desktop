from app import automation
from types import SimpleNamespace


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
    expected = str(automation.Path("C:/tmp/chromium"))
    monkeypatch.setattr(automation.shutil, "which", lambda _: None)
    monkeypatch.setattr(automation, "_playwright_chromium_path", lambda: automation.Path("C:/tmp/chromium"))
    monkeypatch.setattr(automation.Path, "is_file", lambda self: str(self) == expected)
    result = automation.detect_browsers()
    assert result["chromium"] == expected


def test_ensure_browser_installs_chromium_when_missing(monkeypatch):
    monkeypatch.setattr(automation, "detect_browsers", lambda: {"msedge": None, "chrome": None, "chromium": None})
    calls = []
    monkeypatch.setattr(automation, "_install_chromium", lambda: calls.append(True))
    monkeypatch.setattr(automation, "detect_browsers", lambda: ({"msedge": None, "chrome": None, "chromium": None} if not calls else {"msedge": None, "chrome": None, "chromium": "/tmp/chromium"}))
    monkeypatch.setattr(automation.sys, "frozen", False, raising=False)
    assert automation.ensure_browser() == "chromium"
    assert calls == [True]


def test_find_order_cell_traverses_pagination():
    class Locator:
        def __init__(self, page, kind):
            self.page = page
            self.kind = kind

        @property
        def first(self):
            return self

        def filter(self, **_kwargs):
            return self

        def is_visible(self):
            return False

        def get_attribute(self, _name):
            return None

        def count(self):
            return int(self.kind == "cell" and self.page.page_number == 2)

        def wait_for(self, **_kwargs):
            return None

        def is_disabled(self):
            return False

        def click(self):
            if self.kind == "next":
                self.page.page_number += 1

    class Page:
        page_number = 1

        def locator(self, selector):
            if "li.number" in selector:
                return Locator(self, "first")
            if "btn-next" in selector:
                return Locator(self, "next")
            return Locator(self, "cell")

        def wait_for_timeout(self, _milliseconds):
            return None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

    page = Page()
    config = SimpleNamespace(
        order_search_timeout_ms=1000,
        retry_wait_ms=200,
        order_search_attempts=1,
        max_page_search=3,
    )
    cell = automation._find_order_cell(page, "W1", config, None)
    assert cell.count() == 1
    assert page.page_number == 2
