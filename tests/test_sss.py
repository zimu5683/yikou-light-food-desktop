"""Tests for the 闪时送 (sss) order placement module."""
from __future__ import annotations

import datetime as dt

from app import sss
from app.locators import SSS_LOCATORS, load_sss_locators, sss_user_locators_path


def test_compute_delivery_time_lunch_before_16():
    now = dt.datetime(2026, 8, 14, 9, 30)
    assert sss.compute_delivery_time(False, now) == "2026-08-14 11:00:00"


def test_compute_delivery_time_dinner_before_16():
    now = dt.datetime(2026, 8, 14, 9, 30)
    assert sss.compute_delivery_time(True, now) == "2026-08-14 17:00:00"


def test_compute_delivery_time_rolls_to_next_day_after_16():
    now = dt.datetime(2026, 8, 14, 18, 0)
    assert sss.compute_delivery_time(False, now) == "2026-08-15 11:00:00"
    assert sss.compute_delivery_time(True, now) == "2026-08-15 17:00:00"


def test_compute_delivery_time_rolls_to_next_day_between_16_and_20():
    now = dt.datetime(2026, 8, 14, 17, 0)
    assert sss.compute_delivery_time(False, now) == "2026-08-15 11:00:00"


def test_load_sss_orders_reads_two_sheets_and_stops_on_blank_row(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    lunch = wb.active
    lunch.title = "午餐"
    dinner = wb.create_sheet("晚餐")
    lunch.append(["姓名", "门牌号", "电话", "送达时间"])  # 第 1 行表头
    lunch.append(["表头占位", "", "", ""])                 # 第 2 行占位
    lunch.append(["张三", "A101", "13800000001", "11:00"])  # 第 3 行
    lunch.append(["李四", "B202", "13800000002", "11:00"])  # 第 4 行
    lunch.append([None, None, None, None])                 # 空行终止
    lunch.append(["不应读取", "C303", "13800000003", "11:00"])
    dinner.append(["姓名", "门牌号", "电话", "送达时间"])
    dinner.append(["表头占位", "", "", ""])
    dinner.append(["王五", "D404", "13800000004", "17:00"])

    path = tmp_path / "闪时送.xlsx"
    wb.save(path)
    wb.close()

    result = sss.load_sss_orders(path)
    assert set(result) == {"午餐", "晚餐"}
    assert len(result["午餐"]) == 2
    assert result["午餐"][0]["name"] == "张三"
    assert result["午餐"][0]["door"] == "A101"
    assert result["午餐"][0]["phone"] == "13800000001"
    assert result["午餐"][0]["row"] == 3
    assert result["午餐"][1]["name"] == "李四"
    assert len(result["晚餐"]) == 1
    assert result["晚餐"][0]["name"] == "王五"


def test_substitute_tokens_replaces_placeholders_in_candidates():
    step = {"candidates": [{"text": "{label}"}, {"css": ".x", "has_text": "{label}"}]}
    out = sss._substitute_tokens(step, label="嗯哼")
    assert out["candidates"][0]["text"] == "嗯哼"
    assert out["candidates"][1]["has_text"] == "嗯哼"


def test_sss_locators_are_separate_from_default_table(monkeypatch, tmp_path):
    from app import locators as locators_mod

    monkeypatch.setattr(locators_mod, "user_data_dir", lambda: tmp_path)
    assert sss_user_locators_path() == tmp_path / "sss_locators.json"
    table = load_sss_locators()
    assert table["创建订单"]["candidates"]
    assert table["地址选项"]["candidates"][0]["has_text"] == "{label}"
    # 默认表（管理后台）与闪时送表互不污染。
    assert "门店地址" not in table
    assert "创建订单" not in locators_mod.DEFAULT_LOCATORS


def test_sss_locators_include_required_steps():
    for step in ("创建订单", "预约单选项", "一口轻食选项", "送达时间输入",
                 "顾客姓名", "顾客电话", "门牌号", "最终确定"):
        assert step in SSS_LOCATORS
        assert SSS_LOCATORS[step]["candidates"]


def test_build_order_payload_matches_captured_schema():
    import datetime as dt

    order = {"row": 3, "name": "张三", "door": "A101", "phone": "13800000001"}
    address = {"lnt": 119.727873, "lat": 30.257483,
               "areaCode": "330112", "addressDetail": "浙江农林大学东湖校区"}
    payload = sss.build_order_payload(order, False, 211053, address, "轻食",
                                      now=dt.datetime(2026, 9, 5, 18, 0))
    assert payload == {
        "expectedDeliveryTime": "2026-09-06 11:00:00",  # 18 点后顺延次日
        "goodsDetail": [{"goodsName": "轻食", "goodsNum": 1}],
        "orderType": 2,
        "receiveName": "张三",
        "receivePhone": "13800000001",
        "storeId": 211053,
        "receiveAddress": {
            "lnt": 119.727873, "lat": 30.257483,
            "areaCode": "330112",
            "addressDetail": "浙江农林大学东湖校区",
            "doorNum": "A101",
        },
    }


def test_match_record_prefers_keyword_and_single_record_fallback():
    records = [{"id": 1, "name": "其他"}, {"id": 2, "name": "一口轻食"}]
    assert sss._match_record(records, "一口轻食", "门店")["id"] == 2
    assert sss._match_record([{"id": 9}], "", "门店")["id"] == 9


def test_address_from_record_reads_marklnglat_like_frontend():
    record = {"name": "嗯哼", "markLnglat": {
        "longitude": 119.727873, "latitude": 30.257483,
        "adcode": "330112", "address": "浙江省杭州市临安区浙江农林大学东湖校区"}}
    addr = sss._address_from_record(record)
    assert addr == {"lnt": 119.727873, "lat": 30.257483,
                    "areaCode": "330112",
                    "addressDetail": "浙江省杭州市临安区浙江农林大学东湖校区"}


def test_address_from_record_reads_real_frequent_address_fields():
    """2026-09-05 实测的「嗯哼」记录结构：code=区划码、position=地址文本。"""
    record = {"id": 75812, "longitude": "119.728224", "latitude": "30.256632",
              "code": "330110", "position": "浙江农林大学东湖校区",
              "houseNum": "图书馆", "contactName": "嗯哼"}
    addr = sss._address_from_record(record)
    assert addr == {"lnt": 119.728224, "lat": 30.256632,
                    "areaCode": "330110", "addressDetail": "浙江农林大学东湖校区"}
