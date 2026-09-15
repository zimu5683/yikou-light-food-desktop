"""``app.config`` 规整函数 + ``sss_import.normalise_phone`` 的行为回归锁。

这些是**纯函数**，却是「配置正确性」和「谁能收到订单」的守门人：

* ``normalize_wps_*`` 决定程序到底读写哪几张云端表 —— 规整错了就会写到错误的
  表格；``normalize_wps_tables`` 末尾会**丢掉没有 file_id 的条目**，这决定了
  哪些子表参与同步。
* ``normalize_wps_address_order`` 里**显式空列表是有效值**（= 该表按地址升序），
  文档明确记着踩过「空即忽略」的坑。
* ``normalise_phone`` 决定手机号能否通过校验，进而决定某个客户会不会被下单。

改动前 ``tests/`` 里没有任何一处直接调用过这些函数（只有 ``AppConfig`` 被用过），
因此本文件全是新增断言，**不改动任何产品代码**。
"""
from __future__ import annotations

import pytest

from app.config import (DEFAULT_ADDRESS_ORDER, DEFAULT_WPS_PRODUCTION_TABLES,
                        MAX_SPLIT_RATIO, MIN_SPLIT_RATIO, clamp_split_ratio,
                        default_wps_address_order, default_wps_production_tables,
                        default_wps_tables, normalize_wps_address_order,
                        normalize_wps_production_tables, normalize_wps_tables,
                        normalize_wps_test_tables)
from app.sss_import import address_group, normalise_phone, should_skip_address

FALLBACK_RATIO = 0.38


# ----------------------------------------------------------------------
# 默认值必须是「副本」：共享可变对象会让一处修改污染全局
# ----------------------------------------------------------------------
def test_default_tables_return_independent_copies():
    first = default_wps_tables()
    second = default_wps_tables()
    first["东湖中餐"]["file_id"] = "篡改"
    first["新表"] = {"file_id": "X"}

    assert second["东湖中餐"]["file_id"] != "篡改"
    assert "新表" not in second
    # 也不能污染模块级常量本身
    assert default_wps_tables()["东湖中餐"]["file_id"] != "篡改"


def test_default_address_order_returns_independent_lists():
    first = default_wps_address_order()
    second = default_wps_address_order()
    first["东湖中餐"].append("注入的地址")
    first["医学院中餐"].append("注入的地址")

    assert "注入的地址" not in second["东湖中餐"]
    assert second["医学院中餐"] == []
    assert "注入的地址" not in DEFAULT_ADDRESS_ORDER["东湖中餐"]


def test_default_production_tables_return_independent_copies():
    first = default_wps_production_tables()
    first["东湖中餐"]["file_id"] = "篡改"
    assert default_wps_production_tables()["东湖中餐"]["file_id"] != "篡改"
    assert DEFAULT_WPS_PRODUCTION_TABLES["东湖中餐"]["file_id"] != "篡改"


# ----------------------------------------------------------------------
# normalize_wps_tables：决定「同步哪几张表」
# ----------------------------------------------------------------------
def test_normalize_wps_tables_falls_back_to_base_for_non_dict():
    result = normalize_wps_tables(None)
    assert result == default_wps_tables()
    assert normalize_wps_tables("不是字典") == default_wps_tables()
    assert normalize_wps_tables([1, 2, 3]) == default_wps_tables()


def test_normalize_wps_tables_accepts_plain_string_entry():
    result = normalize_wps_tables({"东湖中餐": "  NEW_ID  "})
    assert result["东湖中餐"]["file_id"] == "NEW_ID"


def test_normalize_wps_tables_accepts_dict_entry_with_drive_id():
    result = normalize_wps_tables(
        {"东湖中餐": {"file_id": "F1", "drive_id": "D1"}})
    assert result["东湖中餐"] == {"file_id": "F1", "drive_id": "D1"}


def test_normalize_wps_tables_keeps_base_value_when_override_is_blank():
    base = {"东湖中餐": {"file_id": "BASE_ID", "drive_id": "BASE_DRIVE"}}
    for blank in ("", "   ", None, {"file_id": ""}, {"file_id": "  "}, {"drive_id": "D2"}):
        result = normalize_wps_tables({"东湖中餐": blank}, base=base)
        assert result["东湖中餐"]["file_id"] == "BASE_ID", blank
    # drive_id 为空时同样不覆盖
    result = normalize_wps_tables({"东湖中餐": {"file_id": "F9", "drive_id": ""}},
                                  base=base)
    assert result["东湖中餐"] == {"file_id": "F9", "drive_id": "BASE_DRIVE"}


def test_normalize_wps_tables_drops_entries_without_file_id():
    """没有 file_id 的子表不参与同步 —— 必须被丢掉，而不是留个空壳。"""
    base = {"东湖中餐": {"file_id": "F1"}}
    result = normalize_wps_tables(
        {"幽灵表": {"file_id": ""}, "另一个幽灵": {"drive_id": "D"}}, base=base)
    assert result == {"东湖中餐": {"file_id": "F1"}}
    # base 里本来就缺 file_id 的条目也会被清掉
    result = normalize_wps_tables({}, base={"东湖中餐": {"file_id": "F1"},
                                            "空壳": {"drive_id": "D"}})
    assert result == {"东湖中餐": {"file_id": "F1"}}


def test_normalize_wps_tables_strips_and_skips_unusable_keys():
    result = normalize_wps_tables(
        {"  东湖中餐  ": "  F1  ", "": "F2", "   ": "F3", 42: "F4"},
        base={"东湖中餐": {"file_id": "OLD"}})
    assert result == {"东湖中餐": {"file_id": "F1"}}


def test_empty_base_is_falsy_and_falls_back_to_factory_defaults():
    """``base or default_*()`` 是有意的 falsy 兜底：传空 dict 等于没传。

    写进测试是为了防止误以为「base={} 就能拿到空结果」——那会让人以为
    可以把同步目标清空，实际仍会拿到出厂的 6 张表。
    """
    assert normalize_wps_tables({}, base={}) == default_wps_tables()
    assert normalize_wps_address_order({}, base={}) == default_wps_address_order()
    assert normalize_wps_tables({}, base=None) == default_wps_tables()


def test_normalize_wps_tables_adds_new_sheet_on_top_of_base():
    base = {"东湖中餐": {"file_id": "F1"}}
    result = normalize_wps_tables({"新增表": "F2"}, base=base)
    assert result == {"东湖中餐": {"file_id": "F1"}, "新增表": {"file_id": "F2"}}


def test_normalize_wps_tables_ignores_unknown_conf_types():
    base = {"东湖中餐": {"file_id": "F1"}}
    result = normalize_wps_tables({"东湖中餐": 123, "别的": object()}, base=base)
    assert result == {"东湖中餐": {"file_id": "F1"}}


# ----------------------------------------------------------------------
# normalize_wps_address_order：显式空列表是有效值（文档记录的坑）
# ----------------------------------------------------------------------
def test_normalize_wps_address_order_keeps_explicit_empty_list():
    """**空列表 = 该表按地址升序**，绝不能被「空即忽略」吞掉。"""
    result = normalize_wps_address_order({"医学院中餐": []})
    assert result["医学院中餐"] == []
    # 未提及的子表仍保留默认
    assert result["东湖中餐"] == list(DEFAULT_ADDRESS_ORDER["东湖中餐"])


def test_normalize_wps_address_order_accepts_multiline_string():
    result = normalize_wps_address_order({"衣锦中餐": "外卖柜\n校门口\n\n  小  \n"})
    assert result["衣锦中餐"] == ["外卖柜", "校门口", "小"]


def test_normalize_wps_address_order_accepts_list_and_tuple():
    assert normalize_wps_address_order({"衣锦中餐": ["a", "b"]})["衣锦中餐"] == ["a", "b"]
    assert normalize_wps_address_order({"衣锦中餐": ("a", "b")})["衣锦中餐"] == ["a", "b"]


def test_normalize_wps_address_order_keeps_base_for_unusable_values():
    result = normalize_wps_address_order({"东湖中餐": 42, "衣锦中餐": None})
    assert result["东湖中餐"] == list(DEFAULT_ADDRESS_ORDER["东湖中餐"])
    assert result["衣锦中餐"] == list(DEFAULT_ADDRESS_ORDER["衣锦中餐"])


def test_normalize_wps_address_order_strips_items_and_skips_bad_keys():
    result = normalize_wps_address_order(
        {"  衣锦中餐  ": ["  外卖柜 ", "", "   ", None], "": ["x"], 7: ["y"]})
    assert result["衣锦中餐"] == ["外卖柜"]


def test_normalize_wps_address_order_falls_back_for_non_dict():
    assert normalize_wps_address_order("nope") == default_wps_address_order()
    assert normalize_wps_address_order(None) == default_wps_address_order()


def test_normalize_wps_address_order_lists_are_independent():
    result = normalize_wps_address_order({})
    result["东湖中餐"].append("注入")
    assert "注入" not in default_wps_address_order()["东湖中餐"]


# ----------------------------------------------------------------------
# normalize_wps_test_tables：默认必须为空（避免回退到过期副本）
# ----------------------------------------------------------------------
def test_normalize_wps_test_tables_defaults_to_empty():
    """默认**不能**继承正式表或历史副本，否则测试模式会写错表。"""
    assert normalize_wps_test_tables(None) == {}
    assert normalize_wps_test_tables("nope") == {}
    assert normalize_wps_test_tables({}) == {}


def test_normalize_wps_test_tables_requires_both_sides_non_blank():
    result = normalize_wps_test_tables({
        "东湖中餐": "  F1  ",
        "衣锦中餐": "",
        "   ": "F3",
        "医学院中餐": "   ",
        "": "F4",
    })
    assert result == {"东湖中餐": "F1"}


# ----------------------------------------------------------------------
# normalize_wps_production_tables：底表必须是「正式表备份」而不是当前生效表
# ----------------------------------------------------------------------
def test_normalize_wps_production_tables_uses_independent_production_base():
    """文档记录的坑：拿当前生效表当底表，备份就失去意义。"""
    result = normalize_wps_production_tables({"东湖中餐": "NEW_PROD"})
    assert result["东湖中餐"]["file_id"] == "NEW_PROD"
    # 没被覆盖的子表仍来自正式表备份
    assert (result["衣锦中餐"]["file_id"]
            == DEFAULT_WPS_PRODUCTION_TABLES["衣锦中餐"]["file_id"])
    # 且不等于「当前生效表」的默认值来源被污染
    assert result is not DEFAULT_WPS_PRODUCTION_TABLES


def test_normalize_wps_production_tables_tolerates_garbage():
    result = normalize_wps_production_tables(12345)
    assert result == default_wps_production_tables()


# ----------------------------------------------------------------------
# clamp_split_ratio：界面分隔比例必须落在可用区间
# ----------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (0.0, MIN_SPLIT_RATIO),
    (0.1, MIN_SPLIT_RATIO),
    (-1, MIN_SPLIT_RATIO),
    (MIN_SPLIT_RATIO, MIN_SPLIT_RATIO),
    (0.38, 0.38),
    (0.5, 0.5),
    (MAX_SPLIT_RATIO, MAX_SPLIT_RATIO),
    (0.9, MAX_SPLIT_RATIO),
    (1.5, MAX_SPLIT_RATIO),
])
def test_clamp_split_ratio_clamps_into_range(value, expected):
    assert clamp_split_ratio(value) == expected


@pytest.mark.parametrize("value", [None, "abc", object(), [], {}, float("nan")])
def test_clamp_split_ratio_falls_back_for_unusable_values(value):
    assert clamp_split_ratio(value) == FALLBACK_RATIO


def test_clamp_split_ratio_accepts_numeric_strings():
    assert clamp_split_ratio("0.5") == 0.5
    assert clamp_split_ratio("9") == MAX_SPLIT_RATIO


def test_clamp_split_ratio_never_returns_out_of_range():
    for step in range(-20, 120):
        ratio = step / 100
        assert MIN_SPLIT_RATIO <= clamp_split_ratio(ratio) <= MAX_SPLIT_RATIO


# ----------------------------------------------------------------------
# normalise_phone：只做「规范化」，不做长度校验（校验在 validate_orders）
# ----------------------------------------------------------------------
def test_normalise_phone_rejects_bool_and_none():
    """bool 是 int 的子类，天真的实现会把 True 变成 '1'/'True'。"""
    assert normalise_phone(True) == ""
    assert normalise_phone(False) == ""
    assert normalise_phone(None) == ""


def test_normalise_phone_accepts_integers():
    assert normalise_phone(13800000000) == "13800000000"
    assert normalise_phone(0) == "0"


def test_normalise_phone_accepts_exact_integer_floats_only():
    """openpyxl 会把整数字段读成 float；但 138.5 不能被截断成 138。"""
    assert normalise_phone(13800000000.0) == "13800000000"
    assert normalise_phone(138.5) == ""
    assert normalise_phone(float("nan")) == ""


def test_normalise_phone_strips_all_whitespace():
    assert normalise_phone(" 138 0000 0000 ") == "13800000000"
    assert normalise_phone("138\t0000\n0000") == "13800000000"


def test_normalise_phone_normalizes_fullwidth_digits():
    """中文输入法/Excel 里常见的全角数字必须转成半角。"""
    assert normalise_phone("１３８００００００００") == "13800000000"


def test_normalise_phone_does_not_validate_length_or_digits():
    """本函数只规范化；长度/纯数字校验由 validate_orders 用 _PHONE_RE 负责。

    把这个行为写进测试，是为了防止有人误以为「返回非空即合法」。
    """
    assert normalise_phone("123") == "123"
    assert normalise_phone("abc") == "abc"


# ----------------------------------------------------------------------
# address_group / should_skip_address：决定「谁不需要闪时送」
# ----------------------------------------------------------------------
@pytest.mark.parametrize("raw,key", [
    ("B5", "b5"),
    ("b5", "b5"),
    (" b5 ", "b5"),
    ("B 5", "b5"),
    ("小西", "小"),
    (" 小 ", "小"),
    ("大西", "大西"),
    ("学三", "学三"),
])
def test_address_group_normalizes_case_space_and_alias(raw, key):
    assert address_group(raw) == key


@pytest.mark.parametrize("raw", ["", None, "   "])
def test_address_group_returns_empty_for_blank(raw):
    assert address_group(raw) == ""


@pytest.mark.parametrize("raw", ["大西", "小", "小西", " 小 ", "大西 ", "小西 "])
def test_should_skip_address_matches_skip_groups(raw):
    assert should_skip_address(raw) is True


@pytest.mark.parametrize("raw", ["b2", "C3", "学三", "外卖柜", "", None, "   "])
def test_should_skip_address_leaves_others_alone(raw):
    """空地址**不能**被当成「小」而跳过 —— 否则会静默漏单。"""
    assert should_skip_address(raw) is False
