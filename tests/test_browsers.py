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


def test_linux_browser_paths_resolves_distro_commands(monkeypatch):
    def fake_which(name):
        return "/usr/bin/google-chrome-stable" if name == "google-chrome-stable" else None

    monkeypatch.setattr(automation.shutil, "which", fake_which)
    # The POSIX literals must compare equal even when the suite runs on Windows,
    # where Path renders them with backslashes.
    chrome = [str(path).replace("\\", "/") for path in automation._linux_browser_paths("chrome")]
    assert "/usr/bin/google-chrome-stable" in chrome
    assert "/opt/google/chrome/chrome" in chrome
    assert automation._linux_browser_paths("msedge") == [automation.Path("/opt/microsoft/msedge/msedge")]


def test_detect_browsers_finds_linux_chrome(monkeypatch, tmp_path):
    chrome = tmp_path / "opt/google/chrome/chrome"
    chrome.parent.mkdir(parents=True)
    chrome.touch()
    monkeypatch.setattr(automation.os, "name", "posix")
    monkeypatch.setattr(automation.sys, "platform", "linux")
    monkeypatch.setattr(automation, "_linux_browser_paths", lambda browser: [chrome] if browser == "chrome" else [])
    monkeypatch.setattr(automation.shutil, "which", lambda _: None)
    monkeypatch.setattr(automation, "_playwright_chromium_path", lambda: None)
    result = automation.detect_browsers()
    assert result["chrome"] == str(chrome)
    assert result["msedge"] is None


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


def test_ensure_browser_explicit_install_works_when_frozen(monkeypatch):
    # 打包版里显式入口（「检查浏览器」按钮 / --install-browser）必须可以安装。
    calls = []
    monkeypatch.setattr(
        automation, "detect_browsers",
        lambda: ({"msedge": None, "chrome": None, "chromium": None} if not calls
                 else {"msedge": None, "chrome": None, "chromium": "/tmp/chromium"}),
    )
    monkeypatch.setattr(automation, "_install_chromium", lambda: calls.append(True))
    monkeypatch.setattr(automation.sys, "frozen", True, raising=False)
    assert automation.ensure_browser() == "chromium"
    assert calls == [True]


def test_launch_browser_does_not_silently_install_when_frozen(monkeypatch):
    # 打包版任务启动时不静默下载浏览器，缺浏览器时交由 GUI 显式引导。
    captured = {}
    monkeypatch.setattr(automation.sys, "frozen", True, raising=False)

    def fake_ensure(mode="auto", allow_install=True):
        captured["allow_install"] = allow_install
        return "msedge"

    monkeypatch.setattr(automation, "ensure_browser", fake_ensure)
    monkeypatch.setattr(automation, "detect_browsers",
                        lambda: {"msedge": "C:/edge.exe", "chrome": None, "chromium": None})

    class FakeChromium:
        def launch(self, **_kwargs):
            return "browser"

    class FakePlaywright:
        chromium = FakeChromium()

    assert automation._launch_browser(FakePlaywright(), "auto", True) == "browser"
    assert captured["allow_install"] is False
