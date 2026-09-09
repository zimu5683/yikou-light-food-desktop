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


def test_order_log_shows_platform_address_to_point_arrow():
    from app.automation import _format_order_summary

    order = OrderInfo(
        order_no="W8",
        name="测试用户",
        address="A5",
        delivery_address="浙江省杭州市临安区浙江农林大学(东湖校区) A5 506",
        lunch=[MealInfo(total_meals=1, meal_type="午餐")],
    )
    line = _format_order_summary(order)
    assert "…校区) A5 506 → A5" in line or "A5 506 → A5" in line


def test_order_log_address_unchanged_has_no_arrow():
    from app.automation import _format_order_summary

    order = OrderInfo(
        order_no="W8",
        name="测试用户",
        address="大西",
        delivery_address="大西",
        lunch=[MealInfo(total_meals=1, meal_type="午餐")],
    )
    line = _format_order_summary(order)
    assert "→" not in line
    assert "大西" in line


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


def test_sss_transport_defaults_persist(tmp_path):
    path = tmp_path / "config.json"
    config = AppConfig()
    assert config.sss_dry_run is True
    assert config.sss_max_workers == 4
    assert config.sss_read_timeout_s == 20.0
    assert config.sss_unit_price == 1.9
    assert config.sss_idempotency_field == ""
    config.save(path)
    loaded = AppConfig.load(path)
    assert loaded.sss_dry_run is True
    assert loaded.sss_max_workers == 4
    assert loaded.sss_read_timeout_s == 20.0
    assert loaded.sss_unit_price == 1.9


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


def test_bridge_reports_stopped_sss_task_as_reconciled(tmp_path, monkeypatch):
    import app.bridge as bridge_mod

    bridge = bridge_mod.Bridge(config_path=str(tmp_path / "config.json"))
    captured = {}
    monkeypatch.setattr(bridge_mod, "run_sss_job", lambda *args, **kwargs: {
        "processed": 3, "created": 2, "stopped": True, "reconciled": True,
    })
    monkeypatch.setattr(bridge, "_finish_task",
                        lambda message, result: captured.update(message=message, result=result))
    bridge._run_sss(bridge._config, "ignored")
    assert captured["message"] == "闪时送任务已停止：已完成站内对账，已确认 2/3 单"


def test_bridge_reports_partial_sss_task_without_success_state(tmp_path, monkeypatch):
    import app.bridge as bridge_mod

    bridge = bridge_mod.Bridge(config_path=str(tmp_path / "config.json"))
    monkeypatch.setattr(bridge_mod, "run_sss_job", lambda *args, **kwargs: {
        "processed": 3, "created": 2, "stopped": False,
        "partial": True, "reconciled": True,
    })
    bridge._run_sss(bridge._config, "ignored")

    events = bridge.drain_events()["events"]
    done = next(event for event in events if event["event"] == "task:done")
    assert bridge.status == "partial"
    assert done["payload"]["partial"] is True
    assert done["payload"]["message"] == "闪时送任务部分完成：已确认 2/3 单，1 单未完成"


def test_bridge_start_sss_preserves_store_cache_written_by_worker(tmp_path, monkeypatch):
    from app.bridge import Bridge

    path = tmp_path / "config.json"
    workbook = tmp_path / "闪时送.xlsx"
    workbook.touch()
    bridge = Bridge(config_path=str(path))
    # 模拟 worker 使用配置快照查询到门店后，通过 callback 回写常驻配置。
    bridge._remember_sss_store_cache("一口轻食", 211053)
    monkeypatch.setattr(bridge, "_launch", lambda *args: None)

    result = bridge.start_sss({
        "url": "https://sss.example.com/takeout",
        "account": "18758187837",
        "password": "secret",
        "excel": str(workbook),
        "product_name": "轻食",
        "common_address": "",
        "use_fixed_address": True,
        "fixed_lnt": "119.728224",
        "fixed_lat": "30.256632",
        "fixed_area_code": "330110",
        "fixed_address_detail": "浙江农林大学东湖校区",
        "remember": False,
        "dry_run": True,
        "api_mode": True,
    })
    assert result["ok"] is True
    loaded = AppConfig.load(path)
    assert loaded.sss_store_id == 211053
    assert loaded.sss_store_name_cached == "一口轻食"


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


def test_config_save_merges_untouched_fields_after_concurrent_write(tmp_path):
    """两个实例并发保存时，未修改字段应保留磁盘上的新值。"""
    from app.config import AppConfig

    path = tmp_path / "config.json"
    AppConfig(order_date="2026-09-01").save(path)
    first = AppConfig.load(path)
    second = AppConfig.load(path)

    first.order_date = "2026-09-02"
    second.sss_account = "second-writer"
    second.save()
    first.save()

    loaded = AppConfig.load(path)
    assert loaded.order_date == "2026-09-02"
    assert loaded.sss_account == "second-writer"


def test_config_save_uses_atomic_backup(tmp_path):
    from app.config import AppConfig

    path = tmp_path / "config.json"
    AppConfig(order_date="2026-09-01").save(path)
    AppConfig(order_date="2026-09-02").save(path)

    assert AppConfig.load(path).order_date == "2026-09-02"
    assert path.with_name("config.json.bak").is_file()
    # 保存目录里不残留临时文件。
    assert not list(tmp_path.glob(".config.json.*.tmp"))


def test_bridge_reports_reconciliation_failure_as_uncertain(tmp_path, monkeypatch):
    import app.bridge as bridge_mod

    bridge = bridge_mod.Bridge(config_path=str(tmp_path / "config.json"))
    monkeypatch.setattr(bridge_mod, "run_sss_job", lambda *args, **kwargs: {
        "processed": 2, "created": 0, "stopped": False,
        "partial": False, "reconciled": False, "uncertain": True,
    })
    bridge._run_sss(bridge._config, "ignored")

    events = bridge.drain_events()["events"]
    done = next(event for event in events if event["event"] == "task:done")
    assert "站内对账失败" in done["payload"]["message"]
    assert "请勿手动重复提交" in done["payload"]["message"]
    assert bridge.status == "partial"
