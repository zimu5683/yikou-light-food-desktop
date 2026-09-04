from app import automation
from types import SimpleNamespace
import datetime as dt


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


def test_find_order_cell_skips_checked_occurrence_across_pages():
    class Locator:
        def __init__(self, page, kind):
            self.page = page
            self.kind = kind

        @property
        def first(self):
            return self

        def filter(self, **_kwargs):
            return self

        def count(self):
            return int(self.kind == "cell")

        def wait_for(self, **_kwargs):
            return None

        def is_visible(self):
            return False

        def get_attribute(self, _name):
            return None

        def is_disabled(self):
            return False

        def click(self):
            if self.kind == "next":
                self.page.page_number += 1

    class Page:
        def __init__(self):
            self.page_number = 1

        def locator(self, selector):
            if "li.number" in selector:
                return Locator(self, "first-page")
            if "btn-next" in selector:
                return Locator(self, "next")
            return Locator(self, "cell")

        def wait_for_timeout(self, _milliseconds):
            return None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

    config = SimpleNamespace(order_search_timeout_ms=1000, retry_wait_ms=200,
                             order_search_attempts=1, max_page_search=3)
    page = Page()
    automation._find_order_cell(page, "W1", config, None, occurrence=1)
    assert page.page_number == 2


def test_ensure_waimai_tab_recovers_bounce_by_direct_goto(monkeypatch):
    """被 SPA 弹回 #/home 后，必须先直达回列表页再点 Tab（Tab 在首页不存在）。"""
    class Page:
        def __init__(self):
            self.url = "https://m.icall.me/admin/#/home"
            self.gotos = []

        def goto(self, target, **_kwargs):
            self.gotos.append(target)
            self.url = target

    page = Page()
    navigated = []
    monkeypatch.setattr(automation, "_navigate",
                        lambda *args, **kwargs: navigated.append(args[1]))
    automation._ensure_waimai_tab(page, None, "https://m.icall.me/admin/#", 1000, None,
                                  "https://m.icall.me/admin/#/order/takeOutList")
    assert page.gotos == ["https://m.icall.me/admin/#/order/takeOutList"]
    assert navigated == ["外送订单"]


def test_ensure_waimai_tab_skips_work_when_active(monkeypatch):
    """Tab 已选中时零操作：既不 goto 也不点击，避免触发表格重刷。"""

    class Page:
        url = "https://m.icall.me/admin/#/order/takeOutList"

        def goto(self, *_args, **_kwargs):
            raise AssertionError("不应 goto")

    monkeypatch.setattr(automation, "_waimai_tab_active", lambda _page: True)
    monkeypatch.setattr(automation, "_navigate",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应点击 Tab")))
    automation._ensure_waimai_tab(Page(), None, "https://m.icall.me/admin/#", 1000, None,
                                  "https://m.icall.me/admin/#/order/takeOutList")


def test_read_order_retries_once_after_bounce(monkeypatch):
    """读取期间被弹回首页时：第一次读空 → 直达详情重读 → 读到数据。"""
    class Page:
        def __init__(self):
            self.url = "https://m.icall.me/admin/#/home"

        def goto(self, target, **_kwargs):
            self.url = target

    page = Page()
    detail = "https://m.icall.me/admin/#/order/detail?id=1"

    def fake_contact(p, _timeout, _locators):
        return ("张三", "13800000000") if p.url == detail else ("", "")

    monkeypatch.setattr(automation, "_read_contact", fake_contact)
    monkeypatch.setattr(automation, "_read_address", lambda p, _t, _l: "医学院1栋" if p.url == detail else "")
    monkeypatch.setattr(automation, "get_address_base_sheet_name", lambda _a: "医学院")
    monkeypatch.setattr(automation, "extract_meal_info", lambda _p, _t, _l=None: [])
    order = automation._read_order(page, "W2", 1000, None, None, detail)
    assert order is not None
    assert order.name == "张三"
    assert page.url == detail


def test_order_from_api_parses_detail_json(monkeypatch):
    """DOM 读空时从详情接口 JSON 构造订单（站点前端渲染缺陷的兜底）。"""

    class Page:
        url = "https://m.icall.me/admin/#/order/detail?id=12555008&storeId=4026"

        def evaluate(self, *_args, **_kwargs):
            return automation.json.dumps({
                "code": 200,
                "data": {
                    "created_at": "2026-09-03 14:20:00",
                    "mobile": "19012767657",
                    "address": {"contact": "崔", "mobile": "19012767657",
                                "address": "浙江省杭州市临安区浙江农林大学(东湖校区)",
                                "description": "D2楼"},
                    "user": {"nickname": "用户_6467080"},
                    "goods": [{"name": "单点经济餐（午餐）", "num": 2,
                               "attrData": {"matal": "东湖校区配送寝室楼下外卖柜"}}],
                },
            }, ensure_ascii=False)

    order = automation._order_from_api(Page(), "W2", Page.url)
    assert order is not None
    assert order.name == "崔"
    assert order.phone == "19012767657"
    assert "东湖校区" in order.address and "D2楼" in order.address
    assert order.lunch and order.lunch[0].grade == "经济"
    assert order.lunch[0].count == 2
    assert order.lunch[0].meal_type == "午餐"
    assert order.metadata["created_at"] == "2026-09-03 14:20:00"


def test_target_and_api_dates_are_parsed_and_future_dates_rejected():
    assert automation.parse_target_date("", today=dt.date(2026, 9, 4)) == dt.date(2026, 9, 4)
    assert automation.parse_target_date("2026-09-03", today=dt.date(2026, 9, 4)) == dt.date(2026, 9, 3)
    assert automation.parse_order_created_date("2026/09/03 09:30") == dt.date(2026, 9, 3)
    timestamp_date = dt.datetime.fromtimestamp(1_788_364_800).date()
    assert automation.parse_order_created_date(1_788_364_800_000) == timestamp_date
    import pytest
    with pytest.raises(ValueError, match="不能晚于今天"):
        automation.parse_target_date("2026-09-05", today=dt.date(2026, 9, 4))


def test_read_order_returns_none_when_always_empty(monkeypatch):
    """两次都读到空数据（含直达后仍空壳）时返回 None，交给上层报错。"""

    class Page:
        url = "https://m.icall.me/admin/#/order/detail?id=1"

        def goto(self, target, **_kwargs):
            pass

    monkeypatch.setattr(automation, "_read_contact", lambda p, _t, _l: ("", ""))
    monkeypatch.setattr(automation, "_read_address", lambda p, _t, _l: "")
    monkeypatch.setattr(automation, "get_address_base_sheet_name", lambda _a: None)
    monkeypatch.setattr(automation, "extract_meal_info", lambda _p, _t, _l=None: [])
    assert automation._read_order(Page(), "W2", 1000, None, None) is None


def test_waimai_tab_active_checks_class_only():
    class Tab:
        def __init__(self, count, cls):
            self._count, self._cls = count, cls

        def count(self):
            return self._count

        def get_attribute(self, name):
            return self._cls if name == "class" else None

    class Page:
        def __init__(self, tab):
            self._tab = tab

        def get_by_role(self, *_args, **_kwargs):
            return self

        def locator(self, *_args, **_kwargs):
            return self

        @property
        def first(self):
            return self._tab

    active = Page(Tab(1, "el-tabs__item is-active"))
    inactive = Page(Tab(1, "el-tabs__item"))
    missing = Page(Tab(0, ""))
    assert automation._waimai_tab_active(active) is True
    assert automation._waimai_tab_active(inactive) is False
    assert automation._waimai_tab_active(missing) is False


def test_open_detail_reports_clear_error_when_click_misses():
    import pytest

    class Btn:
        def click(self, **_kwargs):
            return None

    class Row:
        def locator(self, *_args, **_kwargs):
            return self

        @property
        def first(self):
            return Btn()

    class Page:
        url = "https://m.icall.me/admin/#/order/takeOutList"

        def wait_for_url(self, *_args, **_kwargs):
            raise TimeoutError("timeout")

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

    config = SimpleNamespace(
        order_search_timeout_ms=1000,
        retry_wait_ms=200,
        order_search_attempts=1,
        max_page_search=1,
    )
    with pytest.raises(LookupError, match="详情未点开"):
        automation._open_detail(Page(), "W8", Row(), config, None, 1000)


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
