"""``Bridge.start_order`` 的回归锁（改动前这个方法一次都没被测过）。

它是「订单处理」任务的**唯一入口**，48 行里同时承担三件事，每件出错都有实际后果：

1. **表单校验** —— 校验错了要么让用户无法启动，要么把坏参数放进自动化流程。
2. **副作用顺序** —— 只有校验**全部通过**才允许写配置、写密钥链、起线程。
   如果顺序写反（先存密码再校验），用户输错一次就会把**错误的密码存进系统密钥链**。
3. **就地更新配置** —— 方法里明确记着一个历史坑：用「全默认值的新对象」覆盖会
   把**另一半模式（闪时送）的配置重置成默认值**。所以必须验「只动订单侧字段」。

测试一律 monkeypatch ``set_password`` 与 ``_launch``，**不碰真实密钥链、不起真线程**。
"""
from __future__ import annotations


import pytest

from app import bridge as bridge_module
from app.bridge import MAX_ORDER_COUNT, Bridge


def _bridge(tmp_path) -> Bridge:
    return Bridge(config_path=str(tmp_path / "config.json"))


def _excel(tmp_path, name: str = "排单.xlsx"):
    path = tmp_path / name
    path.write_bytes(b"x")
    return path


def _payload(tmp_path, **overrides):
    base = {
        "url": "https://m.icall.me/admin/#/login",
        "phone": "13800000000",
        "password": "PW",
        "excel": str(_excel(tmp_path)),
        "date": "",
        "count": "",
        "api_mode": True,
        "remember": True,
    }
    base.update(overrides)
    return base


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    """一个不会真的写密钥链、不会真的起线程的 Bridge。"""
    b = _bridge(tmp_path)
    monkeypatch.setattr(bridge_module, "set_password",
                        lambda *a, **k: True)
    monkeypatch.setattr(b, "_launch", lambda *a, **k: True)
    return b


# ----------------------------------------------------------------------
# 忙时拒绝
# ----------------------------------------------------------------------
def test_refuses_when_a_task_is_already_running(bridge, tmp_path):
    class _Alive:
        def is_alive(self) -> bool:
            return True

    bridge._worker = _Alive()  # type: ignore[assignment]
    got = bridge.start_order(_payload(tmp_path))

    assert got["ok"] is False and got["reason"] == "busy"
    assert "已有任务正在运行" in got["message"]


def test_refuses_when_launch_reports_busy(bridge, tmp_path, monkeypatch):
    """入口检查通过、但 _launch 仍可能因为竞态返回 False。"""
    monkeypatch.setattr(bridge, "_launch", lambda *a, **k: False)
    got = bridge.start_order(_payload(tmp_path))
    assert got["ok"] is False and got["reason"] == "busy"


# ----------------------------------------------------------------------
# 逐字段校验
# ----------------------------------------------------------------------
@pytest.mark.parametrize("field,override,message", [
    ("url", {"url": ""}, "请输入管理网址"),
    ("url", {"url": "   "}, "请输入管理网址"),
    ("phone", {"phone": ""}, "请输入手机号或账号"),
    ("password", {"password": ""}, "请输入登录密码"),
    ("excel", {"excel": ""}, "请选择存在的 Excel 文件"),
    ("excel", {"excel": "/no/such/file.xlsx"}, "请选择存在的 Excel 文件"),
    ("date", {"date": "9.16"}, "目标日期格式必须为 YYYY-MM-DD"),
    ("date", {"date": "2026-13-01"}, "目标日期格式必须为 YYYY-MM-DD"),
])
def test_each_invalid_field_is_reported(bridge, tmp_path, field, override, message):
    got = bridge.start_order(_payload(tmp_path, **override))

    assert got["ok"] is False
    assert message in got["fields"][field]["message"]
    assert set(got["fields"]) == {field}, "只应报出错的那个字段"


def test_excel_suffix_must_be_xlsx_or_xlsm(bridge, tmp_path):
    got = bridge.start_order(_payload(tmp_path, excel=str(_excel(tmp_path, "名单.csv"))))
    assert "请选择 .xlsx 或 .xlsm 文件" in got["fields"]["excel"]["message"]


def test_xlsm_is_accepted(bridge, tmp_path):
    got = bridge.start_order(_payload(tmp_path, excel=str(_excel(tmp_path, "名单.xlsm"))))
    assert got == {"ok": True}


@pytest.mark.parametrize("count", ["", " "])
def test_empty_count_means_all_orders(bridge, tmp_path, count):
    got = bridge.start_order(_payload(tmp_path, count=count))
    assert got == {"ok": True}
    assert bridge._config.order_count is None


def test_json_null_count_is_stringified_and_rejected(bridge, tmp_path):
    """记录实际行为：payload 里的 ``None`` 会被 ``str()`` 成字符串 ``"None"``。

    于是它**不是**「留空」，而是「非法整数」被拒。前端始终传字符串，所以实际不会
    触发；写下来是为了避免后人以为 ``None`` 等价于留空。
    """
    got = bridge.start_order(_payload(tmp_path, count=None))

    assert got["ok"] is False
    assert "count" in got["fields"]


@pytest.mark.parametrize("count", ["0", "-1", "abc", str(MAX_ORDER_COUNT + 1), "1.5"])
def test_bad_count_is_rejected(bridge, tmp_path, count):
    got = bridge.start_order(_payload(tmp_path, count=count))
    assert got["ok"] is False
    assert f"1～{MAX_ORDER_COUNT}" in got["fields"]["count"]["message"]


@pytest.mark.parametrize("count,expected", [("1", 1), ("7", 7), (str(MAX_ORDER_COUNT), MAX_ORDER_COUNT)])
def test_valid_count_is_stored_as_int(bridge, tmp_path, count, expected):
    assert bridge.start_order(_payload(tmp_path, count=count)) == {"ok": True}
    assert bridge._config.order_count == expected


def test_empty_date_is_valid_and_means_today(bridge, tmp_path):
    """``parse_target_date("")`` 返回**今天**，所以空日期不算错误。"""
    assert bridge.start_order(_payload(tmp_path, date="")) == {"ok": True}
    assert bridge._config.order_date == ""


def test_all_bad_fields_are_reported_together(bridge, tmp_path):
    got = bridge.start_order(_payload(tmp_path, url="", phone="", password="", excel=""))
    assert got["ok"] is False
    assert set(got["fields"]) == {"url", "phone", "password", "excel"}


def test_validation_failure_sets_error_status(bridge, tmp_path):
    bridge.start_order(_payload(tmp_path, url=""))
    assert bridge.status == "error"   # status 是 property，不是方法


# ----------------------------------------------------------------------
# 校验失败时**不得有任何副作用**（顺序错了会把错密码存进密钥链）
# ----------------------------------------------------------------------
def test_validation_failure_writes_nothing(bridge, tmp_path, monkeypatch):
    stored: list[tuple] = []
    launched: list[tuple] = []
    monkeypatch.setattr(bridge_module, "set_password",
                        lambda *a, **k: stored.append(a) or True)
    monkeypatch.setattr(bridge, "_launch",
                        lambda *a, **k: launched.append(a) or True)
    before = bridge._config.target_url

    got = bridge.start_order(_payload(tmp_path, url="", password="错密码"))

    assert got["ok"] is False
    assert stored == [], "校验没过绝不能写密钥链"
    assert launched == [], "校验没过绝不能起线程"
    assert bridge._config.target_url == before, "校验没过不能改配置"


def test_validation_failure_does_not_touch_the_config_file(bridge, tmp_path):
    path = tmp_path / "config.json"
    bridge.start_order(_payload(tmp_path, url=""))
    # 失败路径不应落盘（成功路径才会 save）
    assert not path.exists() or "url" not in path.read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# 成功路径
# ----------------------------------------------------------------------
def test_success_updates_only_the_order_side_of_the_config(bridge, tmp_path):
    """方法里记着的历史坑：用「全默认值新对象」覆盖会把闪时送配置重置。

    这里先把闪时送侧字段设成非默认值，再跑一次订单任务，它们必须原样保留。
    """
    cfg = bridge._config
    cfg.sss_account = "sss-user"
    cfg.sss_product_name = "自定义商品"
    cfg.sss_common_address = "自定义地址"
    cfg.sss_dry_run = False

    assert bridge.start_order(_payload(tmp_path, phone="13900000000")) == {"ok": True}

    assert cfg.phone_number == "13900000000"
    assert cfg.sss_account == "sss-user"
    assert cfg.sss_product_name == "自定义商品"
    assert cfg.sss_common_address == "自定义地址"
    assert cfg.sss_dry_run is False


def test_success_persists_the_config(bridge, tmp_path):
    from app.config import AppConfig

    bridge.start_order(_payload(tmp_path, phone="13900000000"))
    assert AppConfig.load(str(tmp_path / "config.json")).phone_number == "13900000000"


def test_success_stores_the_password_when_remembering(bridge, tmp_path, monkeypatch):
    stored: list[tuple] = []
    monkeypatch.setattr(bridge_module, "set_password",
                        lambda *a, **k: stored.append(a) or True)

    bridge.start_order(_payload(tmp_path, phone="13900000000", password="SECRET"))

    assert stored == [("13900000000", "SECRET")]


def test_success_skips_storing_when_remember_is_false(bridge, tmp_path, monkeypatch):
    stored: list[tuple] = []
    monkeypatch.setattr(bridge_module, "set_password",
                        lambda *a, **k: stored.append(a) or True)

    bridge.start_order(_payload(tmp_path, remember=False))

    assert stored == [], "未勾选「记住密码」时不该写密钥链"


def test_launch_gets_a_snapshot_not_the_live_config(bridge, tmp_path, monkeypatch):
    """运行线程拿到的是**深拷贝**：任务跑起来后界面再改配置也不该影响它。"""
    captured: list = []
    monkeypatch.setattr(bridge, "_launch",
                        lambda mode, config, count, password: captured.append(config) or True)

    bridge.start_order(_payload(tmp_path, phone="13900000000"))
    snapshot = captured[0]

    assert snapshot is not bridge._config
    bridge._config.phone_number = "改动后的值"
    assert snapshot.phone_number == "13900000000", "快照不该被后续改动影响"


def test_launch_receives_the_expected_arguments(bridge, tmp_path, monkeypatch):
    captured: list = []
    monkeypatch.setattr(bridge, "_launch",
                        lambda mode, config, count, password: captured.append(
                            (mode, count, password)) or True)

    bridge.start_order(_payload(tmp_path, count="5", password="SECRET"))

    assert captured == [("order", 5, "SECRET")]


def test_api_mode_flag_is_carried_into_the_config(bridge, tmp_path):
    bridge.start_order(_payload(tmp_path, api_mode=False))
    assert bridge._config.api_mode is False


def test_success_returns_ok_without_fields(bridge, tmp_path):
    assert bridge.start_order(_payload(tmp_path)) == {"ok": True}
