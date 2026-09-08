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


def test_run_job_skips_refunded_orders_and_reports_summary(tmp_path, monkeypatch):
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
    monkeypatch.setattr(automod, "load_aliases", lambda: {})
    monkeypatch.setattr(automod, "aliases_path", lambda: tmp_path / "address_aliases.json")
    monkeypatch.setattr(
        automod, "write_pending",
        lambda items, target_date: tmp_path / "pending_addresses.json",
    )
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


def test_run_job_writes_pending_addresses_after_certain_orders(tmp_path, monkeypatch):
    """确定点先写，已知校区异常写表尾，未知校区写待确认专表。"""
    import json
    import threading

    from openpyxl import load_workbook

    from app import automation as automod
    from app.automation import run_job
    from app.excel_templates import write_order_template

    today = dt.date.today()
    date_text = today.isoformat()
    excel = tmp_path / "排单.xlsx"
    report = tmp_path / "pending_addresses.json"
    write_order_template(excel)

    class FakeConfig:
        excel_path = str(excel)
        target_url = "https://example.com"
        phone_number = "13900000000"
        order_date = date_text
        api_mode = True
        element_timeout_ms = 8000
        browser_mode = "auto"

    addresses = {
        "3": "浙江农林大学东湖校区 A5 506",
        "2": "浙江农林大学东湖校区 B2号楼 b1-2外卖柜",
        "1": "完全陌生地址",
    }

    def api_get(path: str) -> dict:
        if "/channel/order?" in path and "pageNo=1" in path:
            return {"data": {"list": [
                {"id": oid, "pickNo": f"W{oid}", "storeId": "1",
                 "created_at": f"{date_text} 10:00:00", "state": 6}
                for oid in ("3", "2", "1")
            ], "total": 3}}
        for oid, address in addresses.items():
            if f"/channel/order/{oid}" in path:
                return {"data": {
                    "id": oid,
                    "address": {"contact": f"顾客{oid}", "mobile": "13800000000",
                                "address": address},
                    "goods": [{"name": "单点经济餐（午餐）", "num": 1}],
                }}
        return {"data": {}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def login(self):
            pass

        def get_json(self, path):
            return api_get(path)

    monkeypatch.setattr(automod, "AdminApiClient", FakeClient)
    monkeypatch.setattr(automod, "load_aliases", lambda: {})
    monkeypatch.setattr(automod, "aliases_path", lambda: tmp_path / "address_aliases.json")
    monkeypatch.setattr(
        automod, "write_pending",
        lambda items, target_date: report.write_text(
            json.dumps({"target_date": str(target_date), "items": items}, ensure_ascii=False),
            encoding="utf-8",
        ) or report,
    )

    result = run_job(FakeConfig(), None, threading.Event(), password="pw")

    assert result["processed"] == 3
    assert result["found"] == 3
    assert result["address_pending"] == 2
    assert result["address_pending_orders"] == ["W2", "W1"]

    wb = load_workbook(excel)
    weekday = automod.WEEKDAYS[(today.weekday() + 1) % 7]
    day_sheet = wb[weekday]
    assert [day_sheet["A3"].value, day_sheet["C3"].value] == ["W3", "A5"]
    assert [day_sheet["A4"].value, day_sheet["C4"].value] == ["W2", addresses["2"]]
    assert [day_sheet["N3"].value, day_sheet["N4"].value] == ["W3", "W2"]
    campus = wb["东湖中餐"]
    assert [campus["A3"].value, campus["C3"].value] == ["W3", "A5"]
    assert [campus["A4"].value, campus["C4"].value] == ["W2", addresses["2"]]
    review = wb["待确认地址"]
    assert [review["A2"].value, review["C2"].value] == ["W1", addresses["1"]]
    wb.close()

    items = json.loads(report.read_text(encoding="utf-8"))["items"]
    assert [item["order_numbers"] for item in items] == [["W2"], ["W1"]]


def test_pending_report_deduplicates_address_and_keeps_all_orders():
    from app.automation import _pending_report_items, _prepare_order_address

    raw = "浙江农林大学东湖校区 B1号楼 b2宿舍楼"
    orders = [
        OrderInfo(order_no="W2", address=raw, delivery_address=raw),
        OrderInfo(order_no="W1", address=raw, delivery_address=raw),
    ]
    for order in orders:
        _prepare_order_address(order, {})

    items = _pending_report_items(orders)
    assert len(items) == 1
    assert items[0]["order_numbers"] == ["W2", "W1"]
    assert items[0]["raw_address"] == raw
    assert items[0]["confidence"] == "medium"


def test_historical_pending_order_is_appended_after_certain_order():
    from app.automation import _prepare_order_address, _write_order

    workbook = Workbook()
    meal = MealInfo(total_meals=1, grade="经济", meal_type="午餐")
    certain = OrderInfo(
        order_no="W2", name="正常", phone="13800000000",
        address="浙江农林大学东湖校区 A5 506",
        delivery_address="浙江农林大学东湖校区 A5 506",
    )
    pending = OrderInfo(
        order_no="W1", name="待确认", phone="13800000001",
        address="浙江农林大学东湖校区 B1号楼 b2宿舍楼",
        delivery_address="浙江农林大学东湖校区 B1号楼 b2宿舍楼",
    )
    _prepare_order_address(certain, {})
    _prepare_order_address(pending, {})
    pending.address = pending.delivery_address
    target = dt.date(2026, 9, 3)

    _write_order(workbook, certain, meal, "午餐", target_date=target,
                 today=dt.date(2026, 9, 4))
    _write_order(workbook, pending, meal, "午餐", target_date=target,
                 today=dt.date(2026, 9, 4))

    sheet = workbook["2026年9月3日 周四"]
    assert [sheet["A2"].value, sheet["C2"].value] == ["W2", "A5"]
    assert [sheet["A3"].value, sheet["C3"].value] == [
        "W1", pending.delivery_address,
    ]


def _build_sheet(ws, addresses, campus_title):
    ws.title = campus_title
    ws.append(["标题", None])
    ws.append(["订单", "姓名", "地址", "电话", "周一"])
    for i, addr in enumerate(addresses, 1):
        ws.append([f"W{i}", f"姓名{i}", addr, f"13{i:09d}", 1])
    return ws


def _col_c(ws):
    return [ws.cell(row=r, column=3).value for r in range(3, ws.max_row + 1)
            if ws.cell(row=r, column=1).value not in (None, "")]


def test_sort_donghu_sub_sheet_order():
    from openpyxl import Workbook
    from app.processing import sort_campus_sub_sheets

    wb = Workbook()
    ws = wb.active
    _build_sheet(ws, ["D2", "B5", "大西", "小西", "A1", "浙江省杭州市临安区浙江农林大学(东湖校区) 其他", "C9"], "东湖中餐")
    sort_campus_sub_sheets(wb)
    assert _col_c(ws) == ["大西", "小西", "A1", "B5", "C9", "D2", "浙江省杭州市临安区浙江农林大学(东湖校区) 其他"]


def test_sort_yijin_sub_sheet_order():
    from openpyxl import Workbook
    from app.processing import sort_campus_sub_sheets

    wb = Workbook()
    ws = wb.active
    _build_sheet(ws, ["外卖柜", "校门口", "浙江省杭州市临安区浙江农林大学联建公寓 E2"], "衣锦中餐")
    sort_campus_sub_sheets(wb)
    assert _col_c(ws) == ["校门口", "外卖柜", "浙江省杭州市临安区浙江农林大学联建公寓 E2"]


def test_sort_yixue_sub_sheet_order():
    from openpyxl import Workbook
    from app.processing import sort_campus_sub_sheets

    wb = Workbook()
    ws = wb.active
    _build_sheet(ws, ["医8号", "医3号", "医10号", "5号楼", "未识别点"], "医学院中餐")
    sort_campus_sub_sheets(wb)
    # 医N号按数字升序在前，之后其他地址保持原相对顺序
    assert _col_c(ws)[:4] == ["医3号", "医8号", "医10号", "5号楼"]


def test_sort_leaves_weekday_sheet_untouched():
    from openpyxl import Workbook
    from app.processing import sort_campus_sub_sheets

    wb = Workbook()
    ws = wb.active
    ws.title = "周一"
    _build_sheet(ws, ["D2", "A1"], "周一")  # 周一表不参与排序
    ws2 = wb.create_sheet("东湖晚餐")
    _build_sheet(ws2, ["D2", "A1"], "东湖晚餐")
    sort_campus_sub_sheets(wb)
    assert _col_c(ws) == ["D2", "A1"]
    assert _col_c(ws2) == ["A1", "D2"]


def test_clear_campus_sub_sheets_keeps_headers():
    from openpyxl import Workbook
    from app.processing import clear_campus_sub_sheets

    wb = Workbook()
    ws = wb.active
    ws.title = "东湖中餐"
    ws.merge_cells("A1:N1")
    ws["A1"] = "东湖中餐"
    headers = ["订单", "姓名", "地址", "电话", "周一", "周二", "周三", "周四", "周五", "周六", "周日", "类型", "餐种", "餐次"]
    ws.append(headers)
    ws.append(["W1", "张三", "大西", "138", 1, None, None, None, None, None, None, "中餐", "经济", 1])
    ws.append(["W2", "李四", "A5", "139", 1, None, None, None, None, None, None, "中餐", "经济", 1])
    ws2 = wb.create_sheet("周一")
    ws2.merge_cells("A1:F1")
    ws2["A1"] = "中餐"
    ws2.append(["订单", "姓名", "地址", "电话", "餐种", "餐次"])
    ws2.append(["W1", "张三", "大西", "138", "经济", 1])

    cleared = clear_campus_sub_sheets(wb)
    assert cleared == ["东湖中餐"]
    # 第1行标题、第2行表头不变
    assert ws["A1"].value == "东湖中餐"
    assert ws.cell(2, 1).value == "订单"
    assert ws.cell(2, 3).value == "地址"
    # 第3行起全部清空
    assert ws.cell(3, 1).value is None
    assert ws.cell(3, 3).value is None
    assert ws.cell(4, 1).value is None
    # 周表不动
    assert ws2.cell(3, 1).value == "W1"


def test_clear_campus_sub_sheets_empty_sheet_not_reported():
    from openpyxl import Workbook
    from app.processing import clear_campus_sub_sheets

    wb = Workbook()
    ws = wb.active
    ws.title = "医学院中餐"
    ws.merge_cells("A1:N1")
    ws["A1"] = "医学院中餐"
    ws.append(["订单", "姓名", "地址", "电话"])
    assert clear_campus_sub_sheets(wb) == []


def test_sort_handles_gap_rows_and_compacts():
    """回归：数据区中间有空行、数据在后时，排序必须扫描全表并压缩。"""
    from openpyxl import Workbook
    from app.processing import sort_campus_sub_sheets

    wb = Workbook()
    ws = wb.active
    ws.title = "东湖中餐"
    ws.append(["东湖中餐"])
    ws.append(["订单", "姓名", "地址", "电话", "周一"])
    for _ in range(5):
        ws.append([None] * 5)  # 中间空行
    for i, addr in enumerate(["D2", "大西", "B2"], 1):
        ws.append([f"W{i}", f"n{i}", addr, "1", 1])

    assert sort_campus_sub_sheets(wb) == ["东湖中餐"]
    got = [(ws.cell(r, 1).value, ws.cell(r, 3).value)
           for r in range(3, ws.max_row + 1) if ws.cell(r, 1).value not in (None, "")]
    assert got == [("W2", "大西"), ("W3", "B2"), ("W1", "D2")]
    # 尾部无残留重复行
    assert ws.cell(6, 1).value is None


def test_clear_deletes_rows_and_shrinks_sheet():
    """回归：清空必须真删行，max_row 收缩，新数据从第3行起写。"""
    from openpyxl import Workbook
    from app.processing import clear_campus_sub_sheets

    wb = Workbook()
    ws = wb.active
    ws.title = "东湖晚餐"
    ws.append(["东湖晚餐"])
    ws.append(["订单", "姓名", "地址", "电话", "周一"])
    for i in range(3):
        ws.append([f"W{i}", "n", "大西", "1", 1])

    assert clear_campus_sub_sheets(wb) == ["东湖晚餐"]
    assert ws.max_row == 2
    assert ws["A1"].value == "东湖晚餐"
    assert ws.cell(2, 1).value == "订单"
