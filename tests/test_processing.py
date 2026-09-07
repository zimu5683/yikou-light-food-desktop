"""processing 纯函数的单元测试：收货人解析、楼栋判定与地址段提取。"""
from __future__ import annotations

import datetime as dt

from openpyxl import Workbook

from app.models import MealInfo, OrderInfo
from app.processing import (
    get_address_base_sheet_name,
    get_donghu_address_segment,
    get_first_empty_row,
    get_yijin_address_from_product_note,
    parse_meal_text,
    parse_receiver_info,
    write_order_row,
)


def test_parse_receiver_info_accepts_common_formats():
    assert parse_receiver_info("张三（13800000001）") == ("张三", "13800000001")
    assert parse_receiver_info("张三(13800000001)") == ("张三", "13800000001")
    assert parse_receiver_info("张三，13800000001") == ("张三", "13800000001")
    assert parse_receiver_info("张三:13800000001") == ("张三", "13800000001")
    assert parse_receiver_info("李四 13900000002") == ("李四", "13900000002")
    assert parse_receiver_info("王五") == ("王五", "")
    assert parse_receiver_info("") == ("", "")
    assert parse_receiver_info(None) == ("", "")


def test_address_base_sheet_maps_keywords_and_nonglin_road():
    assert get_address_base_sheet_name("联建1栋302") == "衣锦"
    assert get_address_base_sheet_name("lianjian 101") == "衣锦"
    assert get_address_base_sheet_name("衣锦校区") == "衣锦"
    assert get_address_base_sheet_name("医学院宿舍") == "医学院"
    assert get_address_base_sheet_name("东湖小区3栋") == "东湖"
    # 农林路默认归东湖，除非明确提到联建。
    assert get_address_base_sheet_name("农林路2号") == "东湖"
    assert get_address_base_sheet_name("农林路联建门口") == "衣锦"
    assert get_address_base_sheet_name("完全陌生的地址") is None


def test_donghu_segment_extracts_room_from_landmarks():
    assert get_donghu_address_segment("东湖大西12栋A101") == "A101"
    assert get_donghu_address_segment("小西3幢B202") == "B202"
    assert get_donghu_address_segment("东湖大西活动室") == "大西"
    assert get_donghu_address_segment("东湖小西") == "小西"
    assert get_donghu_address_segment("其他地址") == "其他地址"


def test_yijin_note_picks_cabinet_or_gate():
    assert get_yijin_address_from_product_note("备注：联建门口外卖柜自提") == "外卖柜"
    assert get_yijin_address_from_product_note("放校门口") == "校门口"
    assert get_yijin_address_from_product_note("") == "校门口"


def test_parse_meal_text_extracts_grade_and_count():
    meals = parse_meal_text("豪华轻食六餐x2（午餐）", "午餐")
    assert len(meals) == 1
    assert meals[0].total_meals == 6
    assert meals[0].grade == "豪华"
    assert meals[0].count == 2
    assert meals[0].meal_type == "午餐"

    single = parse_meal_text("经济轻食单点（晚餐）", "晚餐")
    assert single[0].total_meals == 1
    assert single[0].grade == "经济"

    plain = parse_meal_text("轻食套餐", "午餐")
    assert plain[0].meal_type == "午餐"
    assert plain[0].total_meals is None
    assert plain[0].count == 1


def test_write_order_row_appends_lunch_and_dinner_columns():
    wb = Workbook()
    ws = wb.active
    order = OrderInfo(order_no="W1", name="张三", address="大西A101", phone="13800000001")
    lunch = MealInfo(total_meals=6, grade="经济", count=1, meal_type="午餐")

    assert write_order_row(ws, order, lunch, "午餐") == 3
    assert [ws["A3"].value, ws["B3"].value, ws["C3"].value, ws["D3"].value,
            ws["E3"].value, ws["F3"].value] == ["W1", "张三", "大西A101", "13800000001", "经济", 6]

    dinner = MealInfo(total_meals=1, grade="豪华", count=1, meal_type="晚餐")
    assert write_order_row(ws, order, dinner, "晚餐") == 3
    assert [ws["G3"].value, ws["H3"].value, ws["K3"].value, ws["L3"].value] == \
        ["W1", "张三", "豪华", 1]

    assert write_order_row(ws, order, lunch, "午餐") == 4


def test_get_first_empty_row_skips_leading_placeholder_rows():
    ws = Workbook().active
    ws["A1"] = "表头"
    ws["A2"] = "占位"
    assert get_first_empty_row(ws) == 3


def test_historical_order_writes_to_a_dated_sheet():
    from app.automation import _write_order

    workbook = Workbook()
    order = OrderInfo(order_no="W2", name="张三", address="大西A101", phone="13800000001")
    meal = MealInfo(total_meals=6, grade="经济", count=1, meal_type="午餐")
    target_date = dt.date(2026, 9, 3)

    _write_order(workbook, order, meal, "午餐", target_date=target_date, today=dt.date(2026, 9, 4))
    sheet = workbook["2026年9月3日 周四"]

    assert [sheet.cell(1, column).value for column in range(1, 5)] == ["取单号", "姓名", "地址", "电话"]
    assert [sheet.cell(2, column).value for column in range(1, 5)] == ["W2", "张三", "大西A101", "13800000001"]
    assert sheet.cell(2, 8).value == 1  # 周四
    assert sheet.cell(2, 12).value == "午餐"


def test_weekday_sheet_fills_lunch_dinner_columns_contiguously(tmp_path):
    """回归：周表的中餐/晚餐两栏必须各自从第 3 行连续填充，不能对角错位。"""
    from openpyxl import load_workbook

    from app.automation import _write_order
    from app.excel_templates import write_order_template

    excel = tmp_path / "排单.xlsx"
    write_order_template(excel)
    wb = load_workbook(excel)

    today = dt.date(2026, 9, 7)  # 周一 → 写入「周二」表

    def commit(order_no: str, meals: list[tuple[str, str, int]]):
        order = OrderInfo(order_no=order_no, name=f"顾客{order_no}", address="校门口",
                          phone="13800000000", address_base_sheet="衣锦")
        for meal_type, grade, total in meals:
            meal = MealInfo(total_meals=total, grade=grade, count=1, meal_type=meal_type)
            _write_order(wb, order, meal, meal_type, target_date=today, today=today)

    # 交替顺序写：午餐、晚餐、午+晚双餐订单 —— 旧实现会因整表 max_row 定位而错位。
    commit("W19", [("午餐", "豪华", 1)])
    commit("W18", [("晚餐", "经济", 1)])
    commit("W14", [("午餐", "经济", 6), ("晚餐", "豪华", 1)])

    sheet = wb["周二"]
    # 中餐栏(A)与晚餐栏(G)各自从第 3 行开始连续、顶部对齐；双餐订单两栏落在同一行。
    assert [sheet["A3"].value, sheet["A4"].value] == ["W19", "W14"]
    assert sheet["A5"].value is None
    assert [sheet["G3"].value, sheet["G4"].value] == ["W18", "W14"]
    assert sheet["G5"].value is None
    # 表头(第2行)与合并标题(第1行)保持完整。
    assert [sheet.cell(2, c).value for c in range(1, 7)] == ["订单", "姓名", "地址", "电话", "餐种", "餐次"]
    assert sorted(str(m) for m in sheet.merged_cells.ranges) == ["A1:F1", "G1:L1", "N1:S1"]
    # 总餐区逐条汇总每一餐：W19午、W18晚、W14午、W14晚。
    assert [sheet[f"N{r}"].value for r in range(3, 7)] == ["W19", "W18", "W14", "W14"]
    assert sheet["N7"].value is None
    wb.close()


def test_group_orders_by_pick_keeps_api_order_and_skips_empty_pick_no():
    from app.automation import _group_orders_by_pick

    rows = [
        {"order_id": "3", "pick_no": "W1", "date": dt.date(2026, 9, 3)},
        {"order_id": "2", "pick_no": "", "date": dt.date(2026, 9, 3)},
        {"order_id": "1", "pick_no": "W8", "date": dt.date(2026, 7, 6)},
    ]
    grouped = _group_orders_by_pick(rows)
    assert set(grouped) == {"W1", "W8"}
    assert grouped["W1"][0]["order_id"] == "3"


def test_group_orders_by_pick_groups_repeated_pick_numbers_across_days():
    """取单号跨天重复：同号不同日期的行都保留，顺序与新单在前一致。"""
    from app.automation import _group_orders_by_pick

    rows = [
        {"order_id": "30", "pick_no": "W2", "date": dt.date(2026, 9, 3)},
        {"order_id": "7", "pick_no": "W2", "date": dt.date(2026, 7, 6)},
        {"order_id": "6", "pick_no": "W1", "date": dt.date(2026, 7, 6)},
    ]
    grouped = _group_orders_by_pick(rows)
    assert [r["order_id"] for r in grouped["W2"]] == ["30", "7"]
    assert [r["order_id"] for r in grouped["W1"]] == ["6"]


def test_filter_rows_by_date_keeps_only_target_date():
    from app.automation import _filter_rows_by_date

    target = dt.date(2026, 9, 7)
    rows = [
        {"order_id": "1", "pick_no": "W8", "date": target},
        {"order_id": "2", "pick_no": "W7", "date": dt.date(2026, 9, 6)},
        {"order_id": "3", "pick_no": "W3", "date": target},
        {"order_id": "4", "pick_no": "W1", "date": None},
    ]
    filtered = _filter_rows_by_date(rows, target)
    assert [r["order_id"] for r in filtered] == ["1", "3"]


def test_order_numbers_for_date_returns_descending_existing_numbers():
    from app.automation import _order_numbers_for_date

    rows_by_pick = {
        "W8": [{"order_id": "1", "date": dt.date(2026, 9, 7)}],
        "W3": [{"order_id": "2", "date": dt.date(2026, 9, 7)}],
        "W1": [{"order_id": "3", "date": dt.date(2026, 9, 7)}],
    }
    # 留空/0 表示全部
    assert _order_numbers_for_date(rows_by_pick, None) == [8, 3, 1]
    assert _order_numbers_for_date(rows_by_pick, 0) == [8, 3, 1]
    # 指定数量时只保留当天存在且不超过该编号的
    assert _order_numbers_for_date(rows_by_pick, 3) == [3, 1]
    # 没有订单时为空
    assert _order_numbers_for_date({}, None) == []


def test_api_list_waimai_orders_falls_back_to_page_size_50_on_exception():
    from app.automation import _api_list_waimai_orders

    calls: list[str] = []

    def api_get(path: str) -> dict:
        calls.append(path)
        if "pageSize=100" in path or "pageSize=200" in path:
            raise RuntimeError("large pageSize unsupported")
        return {
            "data": {
                "list": [{
                    "id": "1", "pickNo": "W1", "storeId": "1",
                    "created_at": "2026-09-07 10:00:00",
                }],
                "total": 1,
            }
        }

    rows = _api_list_waimai_orders(api_get, dt.date(2026, 9, 7))
    assert rows and rows[0]["pick_no"] == "W1"
    # 默认 200 失败 -> 回退 100 失败 -> 回退 50 成功
    assert len(calls) == 3
    assert "pageSize=50" in calls[2]


def test_prefetch_order_details_keeps_only_successes_in_order():
    from app.automation import _prefetch_order_details

    def api_get(path: str) -> dict:
        if "1001" in path:
            return {
                "data": {
                    "address": {"contact": "张三", "mobile": "13800000000",
                                "address": "浙江农林大学东湖校区"},
                    "goods": [{"name": "轻食（午餐）", "num": 1}],
                }
            }
        return {"data": {}}

    rows_by_pick = {
        "W1": [{"order_id": "1001", "store_id": "1"}],
        "W2": [{"order_id": "1002", "store_id": "1"}],
    }
    result = _prefetch_order_details(api_get, rows_by_pick, [1, 2], max_workers=2)
    assert set(result) == {1}
    assert result[1].name == "张三"


def test_api_list_waimai_orders_concurrent_pages_fetches_all():
    import re

    from app.automation import _api_list_waimai_orders

    calls: list[str] = []

    def api_get(path: str) -> dict:
        calls.append(path)
        match = re.search(r"pageNo=(\d+)", path)
        page_no = int(match.group(1)) if match else 1
        batch = []
        for i in range(10):
            idx = (page_no - 1) * 10 + i
            if idx < 25:
                batch.append({
                    "id": str(idx + 1),
                    "pickNo": f"W{idx + 1}",
                    "storeId": "1",
                    "created_at": "2026-09-07 10:00:00",
                })
        return {"data": {"list": batch, "total": 25}}

    rows = _api_list_waimai_orders(api_get, dt.date(2026, 9, 7), concurrent_pages=True)
    assert len(rows) == 25
    assert rows[0]["pick_no"] == "W1"
    assert rows[-1]["pick_no"] == "W25"
    assert any("pageNo=2" in call for call in calls)
    assert any("pageNo=3" in call for call in calls)


def test_split_refund_orders_categorises_by_state():
    """退款订单分类：state=8 已退款 / state=7 申请退款中，正常单保留。"""
    from app.automation import _filter_rows_by_date, _group_orders_by_pick, \
        _order_numbers_for_date, split_refund_orders

    rows = [
        {"pick_no": "W8", "date": dt.date(2026, 9, 7), "state": 8},
        {"pick_no": "W7", "date": dt.date(2026, 9, 7), "state": 7},
        {"pick_no": "W6", "date": dt.date(2026, 9, 7), "state": 6},
        {"pick_no": "W5", "date": dt.date(2026, 9, 7)},
    ]
    rpb = _group_orders_by_pick(_filter_rows_by_date(rows, dt.date(2026, 9, 7)))
    numbers = _order_numbers_for_date(rpb, None)
    normal, refunded, applied = split_refund_orders(rpb, numbers)
    assert normal == [6, 5]
    assert refunded == [8]
    assert applied == [7]


def test_parse_batch_keeps_refund_state():
    """列表接口解析保留 state 字段；缺失时按非退款容忍。"""
    from app.automation import _api_list_waimai_orders

    def api_get(path: str) -> dict:
        return {"data": {"list": [
            {"id": "1", "pickNo": "W8", "created_at": "2026-09-07 10:00:00", "state": 8},
            {"id": "2", "pickNo": "W7", "created_at": "2026-09-07 10:00:00", "state": 7},
            {"id": "3", "pickNo": "W6", "created_at": "2026-09-07 10:00:00", "state": 6},
            {"id": "4", "pickNo": "W5", "created_at": "2026-09-07 10:00:00"},
        ], "total": 4}}

    rows = _api_list_waimai_orders(api_get, dt.date(2026, 9, 7))
    by_pick = {r["pick_no"]: r for r in rows}
    assert by_pick["W8"]["state"] == 8
    assert by_pick["W7"]["state"] == 7
    assert by_pick["W6"]["state"] == 6
    assert by_pick["W5"].get("state") is None


def test_run_job_skips_refunded_orders_and_reports_summary(tmp_path):
    """端到端：退款订单不写表，日志与返回值含两类退款汇总。"""
    import threading

    from openpyxl import load_workbook

    from app import automation as automod
    from app.automation import run_job
    from app.excel_templates import write_order_template

    excel = tmp_path / "排单.xlsx"
    write_order_template(excel)

    class FakeConfig:
        excel_path = str(excel)
        target_url = "https://example.com"
        phone_number = "13900000000"
        order_date = "2026-09-07"
        api_mode = True
        element_timeout_ms = 8000
        browser_mode = "auto"

    def api_get(path: str) -> dict:
        if "/channel/order?" in path and "pageNo=1" in path:
            return {"data": {"list": [
                {"id": "101", "pickNo": "W8", "storeId": "1",
                 "created_at": "2026-09-07 10:00:00", "state": 8},
                {"id": "102", "pickNo": "W7", "storeId": "1",
                 "created_at": "2026-09-07 10:00:00", "state": 7},
                {"id": "103", "pickNo": "W6", "storeId": "1",
                 "created_at": "2026-09-07 10:00:00", "state": 6},
            ], "total": 3}}
        if "/channel/order/103" in path:
            return {"data": {"id": "103", "pickNo": "W6",
                    "address": {"contact": "张三", "mobile": "13800000000",
                                "address": "浙江农林大学东湖校区"},
                    "goods": [{"name": "单点经济餐（午餐） x1", "num": 1}]}}
        return {"data": {}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def login(self):
            pass

        def get_json(self, path):
            return api_get(path)

    orig_client = automod.AdminApiClient
    automod.AdminApiClient = FakeClient
    logs: list[str] = []
    try:
        result = run_job(FakeConfig(), None, threading.Event(), lambda m: logs.append(m),
                         password="pw", order_decision_callback=lambda c, e: "skip",
                         save_decision_callback=lambda e: "cancel")
    finally:
        automod.AdminApiClient = orig_client

    assert result["processed"] == 1  # 只有 W6
    assert result["found"] == 1
    assert result["refunded"] == [8]
    assert result["refund_applied"] == [7]
    full_log = "\n".join(logs)
    assert "退款成功（已退款，不排单）：W8" in full_log
    assert "还在申请退款（待审批，不排单）：W7" in full_log

    wb = load_workbook(excel)
    cells = [cell.value for ws in wb.worksheets for row in ws.iter_rows() for cell in row]
    wb.close()
    assert "W6" in cells
    assert "W7" not in cells
    assert "W8" not in cells
