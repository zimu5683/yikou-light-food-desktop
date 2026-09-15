"""``app.automation`` 里两个纯解析函数的回归锁（改动前完全未被引用）。

* ``parse_meal_rows`` 把网站订单表格里的「商品名 + 数量」解析成 ``MealInfo``：
  **总餐次、经济/豪华、份数**全靠它。解析错了会直接写错排单表的餐数与档次。
* ``extract_product_note_text`` 收集商品名下方的自由备注（例如「不要辣」），
  会被带进排单表供协作者看。

两个函数都不碰网络，纯逻辑，因此测试起来零风险。
"""
from __future__ import annotations

import pytest

from app.automation import extract_product_note_text, parse_meal_rows


def _rows(*pairs):
    return [{"product": product, "qty": qty} for product, qty in pairs]


# ----------------------------------------------------------------------
# parse_meal_rows：按餐别筛选
# ----------------------------------------------------------------------
def test_only_rows_of_the_requested_meal_type_are_returned():
    rows = _rows(("经济（午餐）", "x1"), ("豪华（晚餐）", "x1"))
    lunch = parse_meal_rows(rows, "午餐")
    dinner = parse_meal_rows(rows, "晚餐")

    assert [m.meal_type for m in lunch] == ["午餐"]
    assert [m.meal_type for m in dinner] == ["晚餐"]


def test_rows_without_any_meal_label_are_ignored():
    """商品名里没有（午餐）/（晚餐）标记时不该产出任何条目。"""
    assert parse_meal_rows(_rows(("随便一个商品", "x3")), "午餐") == []
    assert parse_meal_rows([], "午餐") == []


def test_missing_product_or_qty_keys_do_not_crash():
    assert parse_meal_rows([{}], "午餐") == []
    assert parse_meal_rows([{"qty": "x2"}], "午餐") == []


# ----------------------------------------------------------------------
# parse_meal_rows：总餐次 / 档次 / 份数
# ----------------------------------------------------------------------
@pytest.mark.parametrize("product,expected_meals", [
    ("豪华六餐（午餐）", 6),
    ("经济六餐（午餐）", 6),
    ("单点（午餐）", 1),
    ("普通套餐（午餐）", None),
    # 下面两条专治「判定放宽」：含「六」但不是「六餐」、含「单」但不是「单点」。
    # 少了它们，把判定写成 `"六" in segment` / `"单" in segment` 也能蒙混过关。
    ("六号套餐（午餐）", None),
    ("单纯套餐（午餐）", None),
])
def test_total_meals_is_read_from_the_segment_before_the_label(product, expected_meals):
    got = parse_meal_rows(_rows((product, "x1")), "午餐")
    assert [m.total_meals for m in got] == [expected_meals]


def test_label_with_empty_leading_segment_yields_no_entry():
    """``"（午餐）"`` 前面是空片段 → 直接跳过，不产出条目。"""
    assert parse_meal_rows(_rows(("（午餐）", "x1")), "午餐") == []


@pytest.mark.parametrize("product,expected_grade", [
    ("经济餐（午餐）", "经济"),
    ("豪华套餐（午餐）", "豪华"),
    ("经济豪华（午餐）", "经济"),      # 先判经济
    ("普通套餐（午餐）", None),
    # 含「经」但不是「经济」→ 必须判为 None；防止判定放宽成 `"经" in segment`
    ("经典型套餐（午餐）", None),
    # 含「豪」但不是「豪华」→ 同理
    ("豪享套餐（午餐）", None),
])
def test_grade_is_read_from_the_segment_before_the_label(product, expected_grade):
    got = parse_meal_rows(_rows((product, "x1")), "午餐")
    assert [m.grade for m in got] == [expected_grade]


@pytest.mark.parametrize("qty,expected", [
    ("x3", 3), ("X3", 3), ("x10", 10), ("共 x4 份", 4),
    ("", 1), ("x", 1), ("没有数量", 1),
])
def test_count_comes_from_xN_in_qty_and_defaults_to_one(qty, expected):
    got = parse_meal_rows(_rows(("经济（午餐）", qty)), "午餐")
    assert [m.count for m in got] == [expected]


def test_grade_and_meals_after_the_label_are_not_picked_up():
    """标签**之后**的文字属于下一个片段 —— 不能被算进当前条目。

    这是 ``REG_MEAL_SPLIT`` + ``segments[:-1]`` 的既有语义：
    ``"（午餐）豪华"`` 的「豪华」落在最后一段，会被丢弃 → 不产出条目。
    """
    assert parse_meal_rows(_rows(("（午餐）豪华", "x1")), "午餐") == []


def test_one_row_with_several_labels_yields_one_entry_each():
    """一行里出现多个（午餐）时，每段各产一条，且共享同一个份数。"""
    got = parse_meal_rows(_rows(("豪华（午餐）经济（午餐）六餐（午餐）", "x7")), "午餐")
    assert [(m.total_meals, m.grade, m.count) for m in got] == [
        (None, "豪华", 7), (None, "经济", 7), (6, None, 7)]


def test_empty_segments_between_labels_are_skipped():
    """连续两个标签之间的空片段不产出条目。"""
    got = parse_meal_rows(_rows(("经济（午餐）（午餐）", "x2")), "午餐")
    assert [(m.grade, m.count) for m in got] == [("经济", 2)]


def test_multiple_rows_are_preserved_in_order():
    got = parse_meal_rows(_rows(("豪华（午餐）", "x1"),
                                ("经济（午餐）", "x2"),
                                ("豪华（午餐）", "x3")), "午餐")
    assert [(m.grade, m.count) for m in got] == [("豪华", 1), ("经济", 2), ("豪华", 3)]


# ----------------------------------------------------------------------
# extract_product_note_text
# ----------------------------------------------------------------------
class _Page:
    """假 page：按 CSS 选择器返回产品文本；记录调用顺序与注入的 JS。"""

    def __init__(self, by_css: dict[str, list[str]]) -> None:
        self.by_css = by_css
        self.calls: list[str] = []
        self.scripts: list[str] = []

    def eval_on_selector_all(self, css: str, js: str) -> list[str]:
        self.calls.append(css)
        self.scripts.append(js)
        if css == ".boom":
            raise RuntimeError("选择器执行失败")
        return self.by_css.get(css, [])


def _locators(*css_list: str) -> dict:
    return {"meal_table_row": {"candidates": [{"css": css} for css in css_list]}}


def test_first_candidate_with_data_wins_and_empty_ones_are_skipped():
    page = _Page({".a": [], ".good": ["套餐A\n不要辣"]})
    assert extract_product_note_text(page, _locators(".a", ".good")) == "不要辣"
    assert page.calls == [".a", ".good"]


def test_failing_candidate_is_skipped():
    page = _Page({".good": ["套餐C\n备注C"]})
    assert extract_product_note_text(page, _locators(".boom", ".good")) == "备注C"
    assert page.calls == [".boom", ".good"]


def test_first_line_is_the_product_name_and_the_rest_are_notes():
    page = _Page({".good": ["套餐A\n不要辣\n少放盐", "套餐B\n多加饭"]})
    assert extract_product_note_text(page, _locators(".good")) == "不要辣 少放盐 多加饭"


def test_products_without_notes_contribute_nothing():
    page = _Page({".good": ["套餐A", "套餐B"]})
    assert extract_product_note_text(page, _locators(".good")) == ""


def test_blank_lines_inside_notes_are_dropped_and_surrounding_space_trimmed():
    page = _Page({".good": ["A\n第一行\n\n  第二行  "]})
    assert extract_product_note_text(page, _locators(".good")) == "第一行 第二行"


def test_the_first_candidate_with_data_wins_even_if_later_ones_also_have_data():
    """两个候选**都有**数据时必须取第一个。

    只测「前面为空」是不够的：把 ``break`` 改成 ``continue`` 也能通过，
    那样就会静默采用最后一个候选的数据。
    """
    page = _Page({".first": ["A\n备注一"], ".second": ["B\n备注二"]})
    assert extract_product_note_text(page, _locators(".first", ".second")) == "备注一"
    assert page.calls == [".first"], "已有数据后不该再试后续候选"


def test_no_data_or_all_candidates_failing_returns_empty_string():
    assert extract_product_note_text(_Page({".good": []}), _locators(".good")) == ""
    assert extract_product_note_text(_Page({}), _locators(".boom")) == ""


def test_injected_script_reads_the_product_and_quantity_columns():
    """注入浏览器的 JS 必须去读**第 1 列（品名）**与**第 3 列（数量）**。

    假 page 不会真的执行 JS，所以光看返回值发现不了 JS 被写坏；
    这里对脚本内容做一次契约断言（不是复述实现，只钉住它依赖的列号）。
    """
    page = _Page({".good": ["套餐A\n备注"]})
    extract_product_note_text(page, _locators(".good"))

    assert len(page.scripts) == 1
    script = page.scripts[0]
    assert "td:nth-child(1)" in script, "品名在第 1 列"
    assert "innerText" in script, "必须取可见文本"


def test_candidates_without_css_are_skipped():
    locators = {"meal_table_row": {"candidates": [{}, {"css": ".good"}]}}
    page = _Page({".good": ["A\n备注"]})
    assert extract_product_note_text(page, locators) == "备注"
    assert page.calls == [".good"]
