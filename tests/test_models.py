from app.models import MealInfo, OrderInfo


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
