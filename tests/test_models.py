from app.models import MealInfo, OrderInfo
from app.config import AppConfig


def test_meal_info_to_dict_is_serialisable():
    meal = MealInfo(total_meals=2, grade="经济", count=3, meal_type="午餐")
    assert meal.to_dict() == {
        "total_meals": 2,
        "grade": "经济",
        "count": 3,
        "meal_type": "午餐",
    }


def test_order_info_to_dict_serialises_nested_meals():
    order = OrderInfo(
        order_no="T-001",
        name="测试用户",
        phone="13800000000",
        lunch=[MealInfo(total_meals=1, meal_type="午餐")],
    )
    payload = order.to_dict()
    assert payload["order_no"] == "T-001"
    assert payload["lunch"][0]["meal_type"] == "午餐"


def test_save_workbook_retries_after_locked_file():
    from app.automation import _save_workbook_with_retry

    class LockedWorkbook:
        def __init__(self):
            self.calls = 0

        def save(self, path):
            self.calls += 1
            if self.calls == 1:
                raise PermissionError("file is locked")

    workbook = LockedWorkbook()
    _save_workbook_with_retry(workbook, "locked.xlsx", lambda error: "retry")
    assert workbook.calls == 2


def test_order_log_contains_order_details():
    from app.automation import _format_order_meals

    order = OrderInfo(
        order_no="W8",
        name="测试用户",
        lunch=[MealInfo(total_meals=6, grade="经济", count=2, meal_type="午餐")],
    )
    assert "午餐经济6餐 x2" in _format_order_meals(order)


def test_new_config_has_no_implicit_current_directory_workbook():
    assert AppConfig().excel_path is None


def test_xlsm_workbook_is_loaded_with_vba_preserved(tmp_path):
    from app.automation import _load_order_workbook

    calls = []
    workbook = object()

    def loader(path, **kwargs):
        calls.append((path, kwargs))
        return workbook

    path = tmp_path / "orders.xlsm"
    assert _load_order_workbook(path, loader) is workbook
    assert calls == [(path, {"keep_vba": True})]
