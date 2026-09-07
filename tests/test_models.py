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
    from app.automation import _format_order_meals, _format_order_summary

    order = OrderInfo(
        order_no="W8",
        name="测试用户",
        lunch=[MealInfo(total_meals=6, grade="经济", count=2, meal_type="午餐")],
    )
    assert "午餐经济6餐 x2" in _format_order_meals(order)
    assert _format_order_summary(order) == "W8｜测试用户｜未填写｜未填写｜午餐经济6餐 x2"


def test_new_config_has_no_implicit_current_directory_workbook():
    assert AppConfig().excel_path is None


def test_config_persists_order_date(tmp_path):
    config = AppConfig(order_date="2026-09-03")
    path = tmp_path / "config.json"
    config.save(path)
    loaded = AppConfig.load(path)
    assert loaded.order_date == "2026-09-03"


def test_config_persists_order_count(tmp_path):
    from app.config import AppConfig

    path = tmp_path / "config.json"
    AppConfig(order_count=6).save(path)
    assert AppConfig.load(path).order_count == 6
    # None（留空=全部）也应往返无损，并兼容旧配置缺失该键。
    AppConfig(order_count=None).save(path)
    assert AppConfig.load(path).order_count is None


def test_bridge_save_order_config_preserves_sss_side(tmp_path):
    """就地保存订单侧配置时，闪时送侧既有字段不被重置成默认。"""
    from app.bridge import Bridge

    path = tmp_path / "config.json"
    bridge = Bridge(config_path=str(path))
    bridge._config.sss_account = "keep-sss"
    bridge._config.sss_fixed_lnt = 119.7
    bridge._config.save()

    result = bridge.save_order_config({
        "url": "https://order.example.com",
        "phone": "13800000000",
        "excel": str(tmp_path / "排单.xlsx"),
        "date": "2026-09-07",
        "count": 6,
        "api_mode": True,
    })
    assert result["ok"] is True
    loaded = AppConfig.load(path)
    assert loaded.target_url == "https://order.example.com"
    assert loaded.order_date == "2026-09-07"
    assert loaded.order_count == 6
    assert loaded.sss_account == "keep-sss"  # 闪时送侧不受影响
    assert loaded.sss_fixed_lnt == 119.7


def test_bridge_save_sss_config_preserves_order_side(tmp_path):
    """就地保存闪时送侧配置时，订单侧既有字段不被重置成默认。"""
    from app.bridge import Bridge

    path = tmp_path / "config.json"
    bridge = Bridge(config_path=str(path))
    bridge._config.order_date = "2026-09-07"
    bridge._config.order_count = 3
    bridge._config.target_url = "https://order.example.com"
    bridge._config.save()

    result = bridge.save_sss_config({
        "account": "sss-user", "url": "https://sss.example.com",
        "use_fixed_address": True, "fixed_lnt": "120.1", "fixed_lat": "30.2",
        "fixed_area_code": "330110", "fixed_address_detail": "衣锦校区",
    })
    assert result["ok"] is True
    loaded = AppConfig.load(path)
    assert loaded.sss_account == "sss-user"
    assert loaded.sss_fixed_lnt == 120.1
    assert loaded.sss_fixed_lat == 30.2
    # 订单侧不受影响
    assert loaded.order_date == "2026-09-07"
    assert loaded.order_count == 3
    assert loaded.target_url == "https://order.example.com"


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


def test_malformed_config_is_backed_up_before_reset(tmp_path):
    from app.config import AppConfig

    target = tmp_path / "config.json"
    target.write_text("{broken", encoding="utf-8")

    config = AppConfig.load(target)

    # GUI 随后会用默认值覆盖写回，因此坏文件必须先留档供用户检查恢复。
    assert config.excel_path is None
    assert not target.exists()
    assert target.with_name("config.json.bak").read_text(encoding="utf-8") == "{broken"
