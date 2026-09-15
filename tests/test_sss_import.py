"""Tests for app.sss_import —— 闪时送下单前的「云端当天名单」导入（全部离线）。

覆盖：当天列定位（含忽略协作者标记列）、地址过滤（大西/小）、数据校验、
留档写回与 E1、缺当天列跳过、云端错误拒绝、日期闸门，以及与 run_sss_job 的接入。
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app import sss
from app import sss_import as si
from app.wps_cloud import WpsCloudError

LUNCH_FILE = "F_LUNCH"
DINNER_FILE = "F_DINNER"
# 目标日期用固定值，避免测试依赖真实时钟。
TARGET = dt.date(2026, 9, 16)
HEADER_DATE = "9.16 周三"


class _Stop:
    def is_set(self) -> bool:
        return False

    def set(self) -> None:  # pragma: no cover - 只用于满足接口
        pass


class FakeCli:
    """KdocsCli 替身：{file_id: {(0-based 行, 0-based 列): 文本}}。"""

    def __init__(self, sheets: dict[str, dict[tuple[int, int], str]] | None = None,
                 *, error: str = ""):
        self.sheets = dict(sheets or {})
        self.error = error
        self.calls: list[str] = []

    def sheets_info(self, file_id: str):
        self.calls.append(f"sheets_info:{file_id}")
        if self.error:
            raise WpsCloudError(self.error)
        if file_id not in self.sheets:
            return []
        return [{"sheetId": 1, "sheetName": "Sheet1", "rowTo": 60, "colTo": 20}]

    def read_grid(self, file_id, worksheet_id, row_from, row_to, col_from, col_to,
                  *, with_format: bool = False):
        self.calls.append(f"read_grid:{file_id}")
        if self.error:
            raise WpsCloudError(self.error)
        grid = self.sheets.get(file_id) or {}
        return {key: value for key, value in grid.items()
                if row_from <= key[0] <= row_to and col_from <= key[1] <= col_to}


def build_sheet(rows, *, headers=("名字", "地址", "电话", HEADER_DATE, "类型"),
                markers=()):
    """构造云端表网格：第 2 行表头、第 3 行起数据。

    ``rows``：``(姓名, 地址, 电话, 当天列的值)``；``markers``：额外的
    ``(0-based 行, 0-based 列, 文本)``，用于模拟协作者写在备注右侧的标记。
    """
    grid: dict[tuple[int, int], str] = {}
    for col, text in enumerate(headers):
        grid[(1, col)] = text
    for offset, row in enumerate(rows):
        line = 2 + offset
        for col, value in enumerate(row):
            if value is not None and value != "":
                grid[(line, col)] = str(value)
    for line, col, text in markers:
        grid[(line, col)] = text
    return grid


def make_config(tmp_path=None, **overrides):
    path = str(tmp_path / "闪时送.xlsx") if tmp_path is not None else ""
    config = SimpleNamespace(
        wps_cli_path="",
        wps_test_mode=False,
        wps_tables={"东湖中餐": {"file_id": LUNCH_FILE},
                    "东湖晚餐": {"file_id": DINNER_FILE}},
        wps_production_tables={"东湖中餐": {"file_id": LUNCH_FILE},
                               "东湖晚餐": {"file_id": DINNER_FILE}},
        wps_test_tables={},
        wps_target_hour_start=20,
        wps_target_hour_end=10,
        sss_excel_path=path or None,
        sss_order_source="wps",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def write_sss_excel(path, *, lunch_rows=(("旧人", "b1", "13900000000"),),
                    dinner_rows=(), date_text="9.15 周二"):
    from openpyxl import Workbook

    workbook = Workbook()
    for index, (name, rows) in enumerate((("午餐", lunch_rows), ("晚餐", dinner_rows))):
        sheet = workbook.active if index == 0 else workbook.create_sheet(title=name)
        if index == 0:
            sheet.title = name
        sheet.merge_cells("A1:C1")
        sheet["A1"] = name
        sheet.append(["姓名", "地址", "电话"])
        for row in rows:
            sheet.append(list(row))
        sheet["E1"] = date_text
    workbook.save(str(path))
    workbook.close()


def read_excel(path):
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), data_only=True)
    try:
        result = {}
        for sheet in workbook.worksheets:
            # 固定读 3~8 行：空单元格在保存后不落盘，用固定范围才能验证“已清空”。
            rows = [[sheet.cell(row, col).value for col in (1, 2, 3)]
                    for row in range(3, 9)]
            result[sheet.title] = {"e1": sheet.cell(1, 5).value, "rows": rows,
                                   "phone_format": sheet.cell(3, 3).number_format}
        return result
    finally:
        workbook.close()


# ----------------------------------------------------------------------
# 地址过滤
# ----------------------------------------------------------------------

def test_address_group_aliases_and_variants():
    assert si.should_skip_address("大西")
    assert si.should_skip_address("小")
    assert si.should_skip_address("小西")        # 云同步里的别名
    assert si.should_skip_address(" 小 ")
    assert si.should_skip_address("大西 ")
    assert not si.should_skip_address("b2")
    assert not si.should_skip_address("C3")
    assert not si.should_skip_address("学三")
    assert not si.should_skip_address("")


# ----------------------------------------------------------------------
# 云端读取
# ----------------------------------------------------------------------

def test_read_cloud_meal_exports_only_rows_marked_one():
    grid = build_sheet([
        ("张三", "b2", "13800000001", "1"),
        ("李四", "b3", "13800000002", "0"),
        ("王五", "C3", "13800000003", ""),
        ("赵六", "D1", "13800000004", "2"),
        ("钱七", "b5", "13800000005", "1"),
    ])
    cli = FakeCli({LUNCH_FILE: grid})
    meal = si.read_cloud_meal(cli, file_id=LUNCH_FILE, table="东湖中餐",
                              meal="午餐", target=TARGET)
    assert meal.date_text == HEADER_DATE
    assert meal.marked_total == 2
    assert meal.skipped_address == 0
    assert [order["name"] for order in meal.orders] == ["张三", "钱七"]
    assert meal.orders[0]["row"] == 3          # 云端行号（1-based）
    assert meal.orders[1]["door"] == "b5"
    # 只读：每张表 2 次调用
    assert len(cli.calls) == 2


def test_read_cloud_meal_ignores_collaborator_marker_column():
    """备注右侧的「9.16 周三」是协作者的标记列，绝不能当成日期列。"""
    grid = build_sheet(
        [("张三", "b2", "13800000001", "1")],
        markers=[(1, 7, HEADER_DATE), (2, 7, "3")],   # 第 8 列：标记列 + 周几数字
    )
    meal = si.read_cloud_meal(FakeCli({LUNCH_FILE: grid}), file_id=LUNCH_FILE,
                              table="东湖中餐", meal="午餐", target=TARGET)
    assert meal.date_text == HEADER_DATE
    assert meal.order_count == 1


def test_read_cloud_meal_missing_target_column_skips_meal():
    grid = build_sheet([("张三", "b2", "13800000001", "1")],
                       headers=("名字", "地址", "电话", "9.15 周二", "类型"))
    meal = si.read_cloud_meal(FakeCli({LUNCH_FILE: grid}), file_id=LUNCH_FILE,
                              table="东湖中餐", meal="午餐", target=TARGET)
    assert meal.skipped is True
    assert meal.order_count == 0
    assert "9.16" in meal.skip_reason


def test_read_cloud_meal_respects_header_column_order():
    # 电话列被挪到第 2 列：列位置必须按表头文字定位，不能写死。
    grid = {
        (1, 0): "名字", (1, 1): "电话", (1, 2): "地址",
        (1, 3): HEADER_DATE, (1, 4): "类型",
        (2, 0): "张三", (2, 1): "13800000001", (2, 2): "b2", (2, 3): "1",
    }
    meal = si.read_cloud_meal(FakeCli({LUNCH_FILE: grid}), file_id=LUNCH_FILE,
                              table="东湖中餐", meal="午餐", target=TARGET)
    assert meal.orders[0]["name"] == "张三"
    assert meal.orders[0]["door"] == "b2"
    assert meal.orders[0]["phone"] == "13800000001"


def test_read_cloud_meal_refuses_on_broken_header():
    grid = build_sheet([("张三", "b2", "13800000001", "1")],
                       headers=("甲", "乙", "丙", HEADER_DATE, "类型"))
    with pytest.raises(si.ImportRefused, match="表头异常"):
        si.read_cloud_meal(FakeCli({LUNCH_FILE: grid}), file_id=LUNCH_FILE,
                           table="东湖中餐", meal="午餐", target=TARGET)


def test_read_cloud_meal_dedupes_same_person():
    grid = build_sheet([
        ("张三", "b2", "13800000001", "1"),
        ("张三", "b2", "13800000001", "1"),
    ])
    meal = si.read_cloud_meal(FakeCli({LUNCH_FILE: grid}), file_id=LUNCH_FILE,
                              table="东湖中餐", meal="午餐", target=TARGET)
    assert meal.order_count == 1
    assert meal.warnings and "重复" in meal.warnings[0]


def test_collect_day_orders_skips_unconfigured_table():
    config = make_config()
    config.wps_tables = {"东湖中餐": {"file_id": LUNCH_FILE}}
    config.wps_production_tables = dict(config.wps_tables)
    cli = FakeCli({LUNCH_FILE: build_sheet([("张三", "b2", "13800000001", "1")])})
    meals = si.collect_day_orders(config, target=TARGET, cli=cli)
    by_meal = {meal.meal: meal for meal in meals}
    assert by_meal["午餐"].order_count == 1
    assert by_meal["晚餐"].skipped is True
    assert "未配置" in by_meal["晚餐"].skip_reason


def test_collect_day_orders_cloud_error_mentions_local_excel():
    cli = FakeCli(error="今日云文档调用额度已用尽（金山接口限流）")
    with pytest.raises(si.ImportRefused) as excinfo:
        si.collect_day_orders(make_config(), target=TARGET, cli=cli)
    assert "本地 Excel" in str(excinfo.value)


# ----------------------------------------------------------------------
# 数据校验：只针对要下单的人
# ----------------------------------------------------------------------

def test_skipped_address_rows_do_not_trigger_validation():
    grid = build_sheet([
        ("大西住户", "大西", "139", "1"),         # 电话不合法，但不下单 → 不影响
        ("小住户", "小", "", "1"),
        ("正常", "b2", "13800000001", "1"),
    ])
    cli = FakeCli({LUNCH_FILE: grid,
                   DINNER_FILE: build_sheet([("晚餐人", "C3", "13800000009", "1")])})
    meals = si.collect_day_orders(make_config(), target=TARGET, cli=cli)
    lunch = meals[0]
    assert lunch.marked_total == 3
    assert lunch.skipped_address == 2
    assert [order["name"] for order in lunch.orders] == ["正常"]


def test_invalid_order_refuses_and_leaves_excel_untouched(tmp_path):
    path = tmp_path / "闪时送.xlsx"
    write_sss_excel(path)
    before = path.read_bytes()
    grid = build_sheet([
        ("张三", "b2", "1380000", "1"),           # 10 位电话
        ("李四", "b3", "13800000002", "1"),
    ])
    cli = FakeCli({LUNCH_FILE: grid,
                   DINNER_FILE: build_sheet([("王五", "C3", "13800000003", "1")])})
    with pytest.raises(si.ImportRefused) as excinfo:
        si.collect_day_orders(make_config(tmp_path), target=TARGET, cli=cli)
    message = str(excinfo.value)
    # 报错必须点名"哪一格、现在是什么"，用户可直接照着去云端改
    assert "第 3 行 张三" in message
    assert "C3" in message and "「1380000」" in message and "11 位手机号" in message
    assert path.read_bytes() == before


def test_validation_reports_empty_and_missing_cells():
    grid = build_sheet([
        ("张三", "b2", "", "1"),                  # 电话空
        ("", "b3", "13800000002", "1"),           # 姓名空
        ("李四", "", "13800000003", "1"),         # 地址空
    ])
    cli = FakeCli({LUNCH_FILE: grid,
                   DINNER_FILE: build_sheet([("王五", "C3", "13800000004", "1")])})
    with pytest.raises(si.ImportRefused) as excinfo:
        si.collect_day_orders(make_config(), target=TARGET, cli=cli)
    message = str(excinfo.value)
    assert "C3 是空的，不是 11 位手机号" in message
    assert "A4 是空的" in message
    assert "B5 是空的" in message


# ----------------------------------------------------------------------
# 日期闸门
# ----------------------------------------------------------------------

def test_check_target_day_matches_and_mismatch():
    si.check_target_day(TARGET, TARGET)
    with pytest.raises(si.ImportRefused, match="日期不匹配"):
        si.check_target_day(TARGET, dt.date(2026, 9, 17))


def test_prepare_refuses_mismatch_before_writing(tmp_path):
    path = tmp_path / "闪时送.xlsx"
    write_sss_excel(path)
    before = path.read_bytes()
    cli = FakeCli({LUNCH_FILE: build_sheet([("张三", "b2", "13800000001", "1")])})
    with pytest.raises(si.ImportRefused, match="日期不匹配"):
        si.prepare_day_orders(make_config(tmp_path), now=dt.datetime(2026, 9, 16, 17, 0),
                              delivery_date=dt.date(2026, 9, 17), cli=cli)
    assert path.read_bytes() == before
    assert cli.calls == []          # 闸门在读取云端之前


def test_prepare_target_date_windows():
    cli = FakeCli({LUNCH_FILE: build_sheet([("张三", "b2", "13800000001", "1")]),
                   DINNER_FILE: build_sheet([("王五", "C3", "13800000003", "1")])})
    config = make_config()
    # 用户口径：9.15 20:00 ~ 9.16 10:00 之间识别的都是 9.16
    for moment in (dt.datetime(2026, 9, 15, 20, 0),
                   dt.datetime(2026, 9, 15, 23, 59),
                   dt.datetime(2026, 9, 16, 2, 0),
                   dt.datetime(2026, 9, 16, 9, 0),
                   dt.datetime(2026, 9, 16, 15, 0)):
        day = si.prepare_day_orders(config, now=moment, cli=cli)
        assert day.target_date == dt.date(2026, 9, 16), moment
    # 晚上 20:00 之后 → 次日
    day = si.prepare_day_orders(config, now=dt.datetime(2026, 9, 16, 20, 30), cli=cli)
    assert day.target_date == dt.date(2026, 9, 17)


def test_ordering_target_date_matches_delivery_rule_in_window():
    """窗口内识别日期必须等于实际送达日期（否则日期闸门会拒单）。"""
    for moment in (dt.datetime(2026, 9, 15, 20, 0),
                   dt.datetime(2026, 9, 15, 23, 0),
                   dt.datetime(2026, 9, 16, 2, 0),
                   dt.datetime(2026, 9, 16, 9, 0)):
        target = si.ordering_target_date(moment)
        delivery = dt.date.fromisoformat(sss.compute_delivery_time(False, moment)[:10])
        assert target == delivery, moment


# ----------------------------------------------------------------------
# 留档
# ----------------------------------------------------------------------

def test_prepare_archives_orders_and_date(tmp_path):
    path = tmp_path / "闪时送.xlsx"
    write_sss_excel(path,
                    lunch_rows=(("旧人", "b1", "13900000000"),
                                ("旧人2", "b1", "13900000001"),
                                ("旧人3", "b1", "13900000002")),
                    dinner_rows=(("旧晚餐", "b1", "13900000003"),))
    grid = build_sheet([
        ("张三", "b2", "13800000001", "1"),
        ("大西住户", "大西", "13800000002", "1"),
        ("李四", "b12", "13800000003", "1"),
    ])
    cli = FakeCli({LUNCH_FILE: grid,
                   DINNER_FILE: build_sheet([("王五", "C3", "13800000005", "1")])})
    day = si.prepare_day_orders(make_config(tmp_path), now=dt.datetime(2026, 9, 16, 9, 0),
                                cli=cli)
    assert day.total == 3                     # 午餐 2 人 + 晚餐 1 人
    content = read_excel(path)
    assert content["午餐"]["e1"] == HEADER_DATE
    assert content["午餐"]["rows"][:2] == [["张三", "b2", "13800000001"],
                                           ["李四", "b12", "13800000003"]]
    # 旧数据被清空，没有残留
    assert content["午餐"]["rows"][2] == [None, None, None]
    assert content["午餐"]["phone_format"] == "@"
    assert content["晚餐"]["rows"][0] == ["王五", "C3", "13800000005"]
    assert content["晚餐"]["e1"] == HEADER_DATE


def test_prepare_clears_skipped_meal_and_e1(tmp_path):
    path = tmp_path / "闪时送.xlsx"
    write_sss_excel(path, dinner_rows=(("旧晚餐", "b1", "13900000003"),))
    cli = FakeCli({LUNCH_FILE: build_sheet([("张三", "b2", "13800000001", "1")]),
                   DINNER_FILE: build_sheet([("王五", "C3", "13800000003", "1")],
                                            headers=("名字", "地址", "电话", "9.15 周二", "类型"))})
    si.prepare_day_orders(make_config(tmp_path), now=dt.datetime(2026, 9, 16, 9, 0), cli=cli)
    content = read_excel(path)
    assert content["晚餐"]["rows"][0] == [None, None, None]
    assert content["晚餐"]["e1"] is None
    assert content["午餐"]["rows"][0] == ["张三", "b2", "13800000001"]


def test_archive_failure_is_warning_only(tmp_path):
    bad = tmp_path / "坏文件.xlsx"
    bad.write_bytes(b"not an xlsx")
    cli = FakeCli({LUNCH_FILE: build_sheet([("张三", "b2", "13800000001", "1")]),
                   DINNER_FILE: build_sheet([("王五", "C3", "13800000003", "1")])})
    day = si.prepare_day_orders(make_config(tmp_path, sss_excel_path=str(bad)),
                                now=dt.datetime(2026, 9, 16, 9, 0), cli=cli)
    assert day.archive_error
    assert day.orders_by_sheet["午餐"][0]["name"] == "张三"      # 名单仍然可用


def test_prepare_without_excel_only_logs(tmp_path):
    logs: list[str] = []
    cli = FakeCli({LUNCH_FILE: build_sheet([("张三", "b2", "13800000001", "1")]),
                   DINNER_FILE: build_sheet([("王五", "C3", "13800000003", "1")])})
    day = si.prepare_day_orders(make_config(tmp_path, sss_excel_path=None),
                                now=dt.datetime(2026, 9, 16, 9, 0), cli=cli,
                                log=logs.append)
    assert day.total == 2
    assert any("跳过留档" in line for line in logs)


# ----------------------------------------------------------------------
# 与 run_sss_job 的接入
# ----------------------------------------------------------------------

def test_run_sss_job_wps_source_refuses_before_login(monkeypatch):
    created: list[str] = []

    class BoomClient:  # pragma: no cover - 只要被构造就说明流程走错了
        def __init__(self, *args, **kwargs):
            created.append("client")

    monkeypatch.setattr(sss, "SssApiClient", BoomClient)

    def refuse(config, **kwargs):
        raise si.ImportRefused("云端读取失败：额度用尽")

    monkeypatch.setattr(sss, "prepare_day_orders", refuse)
    config = make_config(sss_dry_run=False, api_mode=True, sss_account="18758187837")
    with pytest.raises(si.ImportRefused, match="额度用尽"):
        sss.run_sss_job(config, _Stop(), lambda message: None, password="x")
    assert created == []


def test_run_sss_job_wps_source_uses_memory_orders(monkeypatch, tmp_path):
    path = tmp_path / "闪时送.xlsx"
    write_sss_excel(path)          # 留档文件里是「旧人」，不允许被当成下单名单
    orders = {
        "午餐": [{"row": 12, "name": "云端人", "door": "b2", "phone": "13800000001"}],
        "晚餐": [{"row": 15, "name": "云端晚餐", "door": "C3", "phone": "13800000002"}],
    }
    day = si.DayOrders(target_date=TARGET, orders_by_sheet=orders)
    monkeypatch.setattr(sss, "prepare_day_orders", lambda config, **kwargs: day)

    logs: list[str] = []
    config = make_config(tmp_path, sss_dry_run=True)
    result = sss.run_sss_job(config, _Stop(), logs.append, password="x")
    assert result["status"] == "dry_run"
    assert result["source"] == "wps"
    assert result["processed"] == 2
    assert result["import"]["target_date"] == TARGET.isoformat()
    preview = "\n".join(logs)
    assert "云端人" in preview and "云端晚餐" in preview
    assert "旧人" not in preview


def test_run_sss_job_wps_source_no_orders_returns_status(monkeypatch):
    day = si.DayOrders(target_date=TARGET, orders_by_sheet={"午餐": [], "晚餐": []})
    monkeypatch.setattr(sss, "prepare_day_orders", lambda config, **kwargs: day)
    logs: list[str] = []
    result = sss.run_sss_job(make_config(sss_dry_run=True), _Stop(), logs.append,
                             password="x")
    assert result["status"] == "no_orders"
    assert result["processed"] == 0
    assert any("云端名单为空" in line for line in logs)
