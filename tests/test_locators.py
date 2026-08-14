"""Tests for the locator candidate-chain configuration."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

from app import automation, locators
from app.locators import DEFAULT_LOCATORS, load_locators, user_locators_path


def test_user_locators_path_lives_in_user_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(locators, "user_data_dir", lambda: tmp_path)
    assert user_locators_path() == tmp_path / "locators.json"


def test_load_locators_publishes_template_on_first_run(monkeypatch, tmp_path):
    monkeypatch.setattr(locators, "user_data_dir", lambda: tmp_path)
    table = load_locators()
    assert table == DEFAULT_LOCATORS
    target = tmp_path / "locators.json"
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["订单菜单"]["goto"] == "{base}order"
    assert payload["外送订单"]["candidates"]


def test_load_locators_prefers_user_override(monkeypatch, tmp_path):
    monkeypatch.setattr(locators, "user_data_dir", lambda: tmp_path)
    (tmp_path / "locators.json").write_text(
        json.dumps({"订单菜单": {"goto": "{base}custom"}}), encoding="utf-8"
    )
    table = load_locators()
    assert table["订单菜单"]["goto"] == "{base}custom"
    assert "外送订单" not in table


def test_load_locators_keeps_malformed_override_and_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(locators, "user_data_dir", lambda: tmp_path)
    target = tmp_path / "locators.json"
    target.write_text("{broken", encoding="utf-8")
    assert load_locators() == DEFAULT_LOCATORS
    assert target.read_text(encoding="utf-8") == "{broken"


def test_locator_step_falls_back_to_defaults():
    assert automation._locator_step({}, "订单菜单")["goto"] == "{base}order"
    assert automation._locator_step({"订单菜单": {"goto": "{base}x"}}, "订单菜单")["goto"] == "{base}x"


def test_base_url_hash_routing():
    config = SimpleNamespace(target_url="https://m.icall.me/admin/#/login")
    assert automation._base_url(config) == "https://m.icall.me/admin/#"


def test_base_url_history_routing():
    config = SimpleNamespace(target_url="https://m.icall.me/admin/login")
    assert automation._base_url(config) == "https://m.icall.me/admin/"


class _StubLocator:
    def __init__(self, count=0, text="", parent_text=""):
        self._count = count
        self._text = text
        self._parent_text = parent_text
        self.filters = []
        self.waited = []

    def filter(self, **kwargs):
        self.filters.append(kwargs)
        return self

    def count(self):
        return self._count

    @property
    def first(self):
        return self

    def nth(self, index):
        self.nth_index = index
        return self

    def wait_for(self, **kwargs):
        self.waited.append(kwargs)
        return None

    def inner_text(self):
        return self._text

    def locator(self, _selector):
        return _StubLocator(count=1, text=self._parent_text)


class _StubPage:
    def __init__(self, **by_kind):
        self.by_kind = by_kind
        self.calls = []

    def locator(self, selector):
        self.calls.append(("locator", selector))
        return self.by_kind.get("locator", _StubLocator())

    def get_by_role(self, role, **kwargs):
        self.calls.append(("role", role, kwargs))
        return self.by_kind.get("role", _StubLocator())

    def get_by_text(self, text):
        self.calls.append(("text", text))
        return self.by_kind.get("text", _StubLocator())

    def get_by_placeholder(self, text):
        self.calls.append(("placeholder", text))
        return self.by_kind.get("placeholder", _StubLocator())


def test_build_locator_kinds_and_filters():
    page = _StubPage()
    automation._build_locator(page, {"css": ".el-menu-item"})
    assert page.calls[-1][0] == "locator"
    loc = automation._build_locator(page, {"css": ".el-menu-item", "has_text_re": "订单"})
    assert isinstance(loc.filters[0]["has_text"], re.Pattern)
    automation._build_locator(page, {"role": "menuitem", "name_re": "订单|Order"})
    assert page.calls[-1][0] == "role"
    assert page.calls[-1][2]["name"].pattern == "订单|Order"
    automation._build_locator(page, {"role": "tab"})
    assert page.calls[-1][0] == "role" and page.calls[-1][2] == {}
    automation._build_locator(page, {"placeholder": "登录密码"})
    assert page.calls[-1][0] == "placeholder"
    automation._build_locator(page, {"text": "立即登录"})
    assert page.calls[-1][0] == "text"
    automation._build_locator(page, {"text_re": "订单|Order"})
    assert page.calls[-1][0] == "text"
    assert page.calls[-1][1].pattern == "订单|Order"


def test_find_by_candidates_skips_missing_and_returns_first_hit():
    class Page(_StubPage):
        def locator(self, selector):
            self.calls.append(("locator", selector))
            return _StubLocator(count=0 if selector == "a" else 1)

    page = Page()
    step = {"candidates": [{"css": "a"}, {"css": "b"}]}
    element, index = automation._find_by_candidates(page, step, 1000)
    assert index == 1
    assert element is not None
    assert [call[0] for call in page.calls] == ["locator", "locator"]


def test_find_by_candidates_returns_none_when_all_miss():
    page = _StubPage()
    step = {"candidates": [{"css": "a"}, {"role": "tab", "name_re": "x"}, {"text": "y"}]}
    element, index = automation._find_by_candidates(page, step, 1000)
    assert element is None and index == -1
    assert [call[0] for call in page.calls] == ["locator", "role", "text"]


def test_label_reads_value_from_parent_text():
    element = _StubLocator(count=1, text="收件人", parent_text="收件人：张三 13800000000")
    page = _StubPage(text=element)
    table = {"labels": {"收货人": {"candidates": [{"text_re": "收货人|收件人"}]}}}
    assert automation._label(page, "收货人", 1000, table) == "张三 13800000000"
    assert page.calls[0][0] == "text"
    assert page.calls[0][1].pattern == "收货人|收件人"


def test_label_returns_empty_when_no_candidate_matches():
    page = _StubPage()
    assert automation._label(page, "收货人", 1000, {}) == ""


def test_extract_meal_info_falls_through_table_candidates():
    class Page:
        def __init__(self):
            self.selectors = []
            self.results = {"b": [{"product": "红烧肉（午餐）", "qty": "x2"}]}

        def eval_on_selector_all(self, selector, _expression):
            self.selectors.append(selector)
            return self.results.get(selector)

    page = Page()
    table = {"meal_table_row": {"candidates": [{"css": "a"}, {"css": "b"}]}}
    meals = automation.extract_meal_info(page, "午餐", table)
    assert page.selectors == ["a", "b"]
    assert meals[0].count == 2
    assert meals[0].grade is None
