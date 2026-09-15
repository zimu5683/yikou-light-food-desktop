"""``Bridge`` 的「设置类」js_api 回归锁（改动前这三个方法都没被测过）。

覆盖三个与**用户设置**直接相关的入口：

* ``save_wps_config`` —— 云同步设置。它用的是**白名单**写法（`if "x" in payload`），
  所以**没传的键必须原样保留**：界面只改一项时，绝不能把其余设置清空。
* ``clear_password`` —— 删除密钥链里的密码。删错**命名空间**会把另一个平台的密码也
  抹掉（第 14 轮已锁过命名空间，这里锁「删哪个/什么时候不删」）。
* ``resolve_decision`` / ``resolve_captcha`` —— 把用户在弹窗里的选择交回等待中的任务
  线程。**同一个 id 只能兑现一次**，否则重复提交（双击）会让等待方拿到错值。
"""
from __future__ import annotations

import pytest

from app import bridge as bridge_module
from app.bridge import Bridge


def _bridge(tmp_path) -> Bridge:
    return Bridge(config_path=str(tmp_path / "config.json"))


# ----------------------------------------------------------------------
# save_wps_config：白名单语义
# ----------------------------------------------------------------------
def test_absent_keys_are_left_untouched(tmp_path):
    """只传一个键时，其余设置必须原样保留 —— 这是白名单写法的意义所在。"""
    bridge = _bridge(tmp_path)
    cfg = bridge._config
    cfg.wps_enabled = True
    cfg.wps_cli_path = "/原始/路径"
    cfg.wps_drive_id = "原始DRIVE"
    cfg.wps_marker_enabled = True

    assert bridge.save_wps_config({"test_mode": True}) == {"ok": True}

    assert cfg.wps_test_mode is True
    assert cfg.wps_enabled is True
    assert cfg.wps_cli_path == "/原始/路径"
    assert cfg.wps_drive_id == "原始DRIVE"
    assert cfg.wps_marker_enabled is True


# payload 键 → (配置属性, 初始哨兵值, 发送的新值)。
# 新值必须与哨兵值**可区分**，否则「更新没生效」会被误判成通过。
KEY_CASES = {
    # ⚠️ 布尔字段的**哨兵值必须是 True**：若哨兵取 False，那么「无条件赋值 + 键缺失」
    # 这种 bug 会算出 bool(None) == False，与「保持原值」结果相同，测试就抓不到
    # （变异测试实测确认）。哨兵取 True、新值取 False，才能把两者区分开。
    "enabled": ("wps_enabled", True, False),
    "test_mode": ("wps_test_mode", True, False),
    "marker_enabled": ("wps_marker_enabled", True, False),
    "sort_enabled": ("wps_sort_enabled", True, False),
    "cli_path": ("wps_cli_path", "当前路径", "新路径"),
    "drive_id": ("wps_drive_id", "当前DRIVE", "新DRIVE"),
    "test_file_id": ("wps_test_file_id", "当前FID", "新FID"),
    "test_drive_id": ("wps_test_drive_id", "当前TDID", "新TDID"),
    "test_tables": ("wps_test_tables", {"东湖中餐": "当前T"}, {"东湖中餐": "新T"}),
}
# 除 KEY_CASES 外还要一起盯着「不该被动到」的字段
EXTRA_SENTINELS = {"wps_address_order": {"衣锦中餐": ["外卖柜"]},
                   "wps_tables": {"东湖中餐": {"file_id": "当前表"}}}


@pytest.mark.parametrize("sent_key", sorted(KEY_CASES))
def test_omitting_any_single_key_preserves_its_current_value(tmp_path, sent_key):
    """白名单语义要**逐个键**验证：只传一个键时，其余每个键都必须原样保留。

    只测其中一个键是不够的 —— 变异测试发现，把某个键的 ``if "x" in payload``
    改成无条件赋值时，只要测试的 payload 里恰好带着那个键，就抓不到。
    """
    bridge = _bridge(tmp_path)
    cfg = bridge._config
    sentinels = {attr: old for attr, old, _ in KEY_CASES.values()}
    sentinels.update(EXTRA_SENTINELS)
    for attr, value in sentinels.items():
        setattr(cfg, attr, value)

    attr, old_value, new_value = KEY_CASES[sent_key]
    bridge.save_wps_config({sent_key: new_value})

    assert getattr(cfg, attr) == new_value, f"{sent_key} 应当被更新"
    assert new_value != old_value, "用例设计错误：新值必须与哨兵值不同"
    for other_attr, sentinel in sentinels.items():
        if other_attr == attr:
            continue
        assert getattr(cfg, other_attr) == sentinel, (
            f"只传了 {sent_key}，{other_attr} 却被改动了")


def test_empty_payload_changes_nothing(tmp_path):
    """一个键都不传时，所有设置都必须原样保留（等价于只落一次盘）。"""
    bridge = _bridge(tmp_path)
    cfg = bridge._config
    cfg.wps_enabled = True
    cfg.wps_cli_path = "保持"
    cfg.wps_marker_enabled = False

    assert bridge.save_wps_config({}) == {"ok": True}

    assert cfg.wps_enabled is True
    assert cfg.wps_cli_path == "保持"
    assert cfg.wps_marker_enabled is False


def test_present_keys_are_updated(tmp_path):
    bridge = _bridge(tmp_path)
    cfg = bridge._config

    bridge.save_wps_config({
        "enabled": True, "test_mode": True, "cli_path": " /usr/bin/kdocs ",
        "drive_id": " D1 ", "test_file_id": " F1 ", "test_drive_id": " D2 ",
        "marker_enabled": False, "sort_enabled": False,
    })

    assert cfg.wps_enabled is True and cfg.wps_test_mode is True
    assert cfg.wps_cli_path == "/usr/bin/kdocs", "字符串要去空白"
    assert cfg.wps_drive_id == "D1"
    assert cfg.wps_test_file_id == "F1"
    assert cfg.wps_test_drive_id == "D2"
    assert cfg.wps_marker_enabled is False and cfg.wps_sort_enabled is False


def test_falsy_strings_are_coerced_to_empty(tmp_path):
    bridge = _bridge(tmp_path)
    bridge.save_wps_config({"cli_path": "x", "drive_id": "y"})
    bridge.save_wps_config({"cli_path": None, "drive_id": ""})
    assert bridge._config.wps_cli_path == ""
    assert bridge._config.wps_drive_id == ""


@pytest.mark.parametrize("given,expected", [
    (True, True), (False, False), (1, True), (0, False), (None, False), ("", False),
])
def test_boolean_fields_follow_bool_conversion(tmp_path, given, expected):
    bridge = _bridge(tmp_path)
    bridge.save_wps_config({"enabled": given})
    assert bridge._config.wps_enabled is expected


@pytest.mark.parametrize("given", ["false", "0", "no"])
def test_nonempty_strings_are_truthy_even_when_they_look_false(tmp_path, given):
    """记录实际行为：``bool("false")`` 是 **True**。

    前端传的是真正的布尔（Radix Switch），所以实际不会触发；但若哪天改成表单字符串
    提交，``"false"`` 会**打开**这个开关而不是关闭它。写下来以免日后误判。
    """
    bridge = _bridge(tmp_path)
    bridge.save_wps_config({"enabled": given})
    assert bridge._config.wps_enabled is True


def test_tables_and_address_order_go_through_normalizers(tmp_path):
    bridge = _bridge(tmp_path)

    bridge.save_wps_config({
        "tables": {"  东湖中餐  ": " F1 ", "幽灵": {"file_id": ""}},
        "test_tables": {"东湖中餐": " T1 ", "": "X"},
        "address_order": {"衣锦中餐": "外卖柜\n\n 校门口 "},
    })

    # 归一化生效：去空白、丢掉没有 file_id 的条目
    assert bridge._config.wps_tables["东湖中餐"] == {"file_id": "F1"}
    assert "幽灵" not in bridge._config.wps_tables
    assert bridge._config.wps_test_tables == {"东湖中餐": "T1"}
    assert bridge._config.wps_address_order["衣锦中餐"] == ["外卖柜", "校门口"]


def test_partial_tables_payload_resets_other_sheets_to_factory_defaults(tmp_path):
    """⚠️ 记录一个**潜在隐患**（既有行为，未改动）：

    ``tables`` 与 ``address_order`` 走的是 ``normalize_*(payload.get(...))``，
    而 ``normalize_*`` 在没传 ``base`` 时以**出厂默认**为底 —— 不是以**当前配置**为底。
    于是只要界面回传的 tables 是**部分的**，没提到的子表就会被**重置成出厂 file_id**，
    用户自定义过的表 ID 会被静默丢弃。

    对比：同一个方法里其它字段用的是「缺键则保留原值」的白名单语义，两者并不一致。
    当前前端回传的是完整列表（``wps_status()["tables"]``），所以实际不会触发；
    但若哪天改成「只发改动项」，这里就会丢配置。**是否修正需要用户决策**
    （改成 ``base=cfg.wps_tables`` 是有意为之的行为变更，不在「功能不变」范围内）。
    """
    bridge = _bridge(tmp_path)
    cfg = bridge._config
    cfg.wps_tables = {"东湖中餐": {"file_id": "CUSTOM_DH"},
                      "衣锦中餐": {"file_id": "CUSTOM_YJ"}}

    bridge.save_wps_config({"tables": {"东湖中餐": "NEW_ID"}})

    assert cfg.wps_tables["东湖中餐"] == {"file_id": "NEW_ID"}
    # 没提到的子表被拉回出厂默认（而不是保留 CUSTOM_YJ）
    assert cfg.wps_tables["衣锦中餐"]["file_id"] != "CUSTOM_YJ"
    assert len(cfg.wps_tables) == 6, "出厂默认的 6 张表都会补回来"


def test_config_is_persisted(tmp_path):
    from app.config import AppConfig

    bridge = _bridge(tmp_path)
    bridge.save_wps_config({"enabled": True, "cli_path": "/x/kdocs"})

    back = AppConfig.load(str(tmp_path / "config.json"))
    assert back.wps_enabled is True and back.wps_cli_path == "/x/kdocs"


def test_write_failure_is_reported_not_raised(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)

    def boom(*_a, **_k):
        raise OSError("磁盘满了")

    monkeypatch.setattr(bridge._config, "save", boom)

    assert bridge.save_wps_config({"enabled": True}) == {"ok": False, "reason": "write_failed"}


def test_empty_payload_is_a_noop_but_still_saves(tmp_path):
    bridge = _bridge(tmp_path)
    before = bridge._config.wps_cli_path
    assert bridge.save_wps_config({}) == {"ok": True}
    assert bridge._config.wps_cli_path == before


# ----------------------------------------------------------------------
# clear_password：删哪一把、什么时候不删
# ----------------------------------------------------------------------
@pytest.fixture
def deleted(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(bridge_module, "delete_password",
                        lambda account: calls.append(("admin", account)))
    monkeypatch.setattr(bridge_module, "delete_sss_password",
                        lambda account: calls.append(("sss", account)))
    return calls


def test_clear_order_password_targets_the_admin_credential(tmp_path, deleted):
    bridge = _bridge(tmp_path)
    bridge._config.phone_number = "13800000000"

    assert bridge.clear_password("order") == {"ok": True}

    assert deleted == [("admin", "13800000000")]


def test_clear_sss_password_targets_the_sss_credential(tmp_path, deleted):
    bridge = _bridge(tmp_path)
    bridge._config.sss_account = "sss-user"

    assert bridge.clear_password("sss") == {"ok": True}

    assert deleted == [("sss", "sss-user")], "必须删闪时送那一把，不能误删管理后台的"


def test_unknown_mode_falls_back_to_the_admin_credential(tmp_path, deleted):
    """记录实际行为：mode 不是 ``"sss"`` 的一律走管理后台分支。"""
    bridge = _bridge(tmp_path)
    bridge._config.phone_number = "13800000000"

    bridge.clear_password("乱写")

    assert deleted == [("admin", "13800000000")]


def test_empty_account_never_touches_the_keychain(tmp_path, deleted):
    """账号为空时「没存过密码」，不该去删（也不该删到别人的）。"""
    bridge = _bridge(tmp_path)
    bridge._config.phone_number = "   "
    bridge._config.sss_account = ""

    bridge.clear_password("order")
    bridge.clear_password("sss")

    assert deleted == []


def test_clear_password_always_reports_ok_and_logs_each_mode(tmp_path, deleted):
    bridge = _bridge(tmp_path)

    assert bridge.clear_password("order") == {"ok": True}
    assert bridge.clear_password("sss") == {"ok": True}

    logs = [e["payload"]["msg"] for e in bridge.drain_events(0)["events"]
            if e["event"] == "log"]
    assert any(line == "已清除本机保存的密码" for line in logs)
    assert any(line == "已清除本机保存的闪时送密码" for line in logs)


# ----------------------------------------------------------------------
# resolve_decision / resolve_captcha：同一个 id 只能兑现一次
# ----------------------------------------------------------------------
def test_resolving_a_pending_decision_delivers_the_choice(tmp_path):
    bridge = _bridge(tmp_path)
    interaction_id, entry = bridge._register_interaction("decision")

    assert bridge.resolve_decision(interaction_id, "retry") == {"ok": True}

    assert entry.holder == ["retry"]
    assert entry.event.is_set(), "必须唤醒等待中的 worker"


def test_resolving_a_pending_captcha_delivers_the_code(tmp_path):
    bridge = _bridge(tmp_path)
    captcha_id, entry = bridge._register_interaction("captcha")

    assert captcha_id.startswith("c"), "验证码的 id 前缀应为 c"
    assert bridge.resolve_captcha(captcha_id, "1234") == {"ok": True}

    assert entry.holder == ["1234"]
    assert entry.event.is_set()


def test_the_same_id_can_only_be_resolved_once(tmp_path):
    """重复提交（双击）第二次必须报 ok=False，而不是再塞一个值。"""
    bridge = _bridge(tmp_path)
    interaction_id, entry = bridge._register_interaction("decision")

    assert bridge.resolve_decision(interaction_id, "retry") == {"ok": True}
    assert bridge.resolve_decision(interaction_id, "skip") == {"ok": False}

    assert entry.holder == ["retry"], "第二次的值不能进 holder"


def test_unknown_id_is_reported_not_raised(tmp_path):
    bridge = _bridge(tmp_path)
    assert bridge.resolve_decision("d999", "retry") == {"ok": False}
    assert bridge.resolve_captcha("c999", "1234") == {"ok": False}


def test_non_string_values_are_coerced(tmp_path):
    bridge = _bridge(tmp_path)
    interaction_id, entry = bridge._register_interaction("decision")

    assert bridge.resolve_decision(interaction_id, 42) == {"ok": True}  # type: ignore[arg-type]

    assert entry.holder == ["42"]


def test_resolving_one_id_does_not_affect_another(tmp_path):
    bridge = _bridge(tmp_path)
    first_id, first = bridge._register_interaction("decision")
    second_id, second = bridge._register_interaction("decision")

    bridge.resolve_decision(first_id, "retry")

    assert first.event.is_set()
    assert not second.event.is_set(), "不该顺带唤醒另一个等待者"
    assert bridge.resolve_decision(second_id, "skip") == {"ok": True}
