"""``Bridge.wps_preview`` 的回归锁（改动前未测过）。

它是「云文档同步」的**预览**入口 —— 用户看着它输出的内容决定要不要真的上传。
两个方向都危险：

* **guard 漏了**：带着未授权 / 没选排单表 / 测试副本没配好的状态去读云端；
* **guard 多了或搞错**：把本该能预览的情况挡掉，用户只能盲传。

其中一条是**安全属性**：测试模式下必须把 ``marker_enabled`` **强制关掉**。
协作者的通讯记号写在正式表上，测试模式若把它也写了，等于在别人的正式表里留下痕迹。

另外它是**只读**的：不写云端、不动本地账本/意图日志。这一点也要钉住。
"""
from __future__ import annotations

import datetime as _dt

import pytest

from app import bridge as bridge_module
from app.bridge import Bridge
from app.wps_cloud import WpsCloudError


def write_minimal_workbook(path, sheets=("东湖中餐", "衣锦中餐", "医学院中餐",
                                         "东湖晚餐", "衣锦晚餐", "医学院晚餐")):
    """写一个能被 openpyxl 真正解析的最小排单表。

    预览/上传现在对**同一份字节**做哈希与解析（R09），因此测试也必须提供
    真实可解析的工作簿，而不是任意字节。
    """
    from openpyxl import Workbook

    wb = Workbook()
    first = True
    for name in sheets:
        ws = wb.active if first else wb.create_sheet()
        ws.title = name
        first = False
    wb.save(path)
    return path


def _bridge(tmp_path) -> Bridge:
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._config.wps_enabled = True
    bridge._config.excel_path = write_minimal_workbook(tmp_path / "排单.xlsx")
    return bridge


class _Cli:
    path = "/fake/kdocs-cli"

    def __init__(self, *, authenticated: bool = True) -> None:
        self._authenticated = authenticated

    def authenticated(self) -> bool:
        return self._authenticated


@pytest.fixture
def preview_env(tmp_path, monkeypatch):
    """把 wps_preview 的外部依赖全部替换成可控替身，并记录调用参数。"""
    captured: dict = {"build_plan": [], "read_calls": []}

    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "F1"}})
    monkeypatch.setattr(bridge_module, "read_local_orders",
                        lambda path, log=None: captured["read_calls"].append(path) or
                        {"东湖中餐": []})

    class _Plan:
        sheet = "东湖中餐"
        warnings = ["名单里有 2 个清单外地址"]

    def fake_build_plan(cli, **kwargs):
        captured["build_plan"].append(kwargs)
        return [_Plan()]

    monkeypatch.setattr(bridge_module, "build_plan", fake_build_plan)
    monkeypatch.setattr(bridge_module, "format_plan", lambda plans: "预览正文")
    monkeypatch.setattr(bridge_module, "summarize_plan",
                        lambda plans: {"to_update": 1, "to_append": 2})

    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_wps_cli", lambda: _Cli())
    return bridge, captured, monkeypatch


def _state_files(bridge: Bridge) -> list:
    """预置的账本/日志路径上实际存在的文件（预览必须一个都不产生）。"""
    from pathlib import Path
    directory = Path(bridge._config.config_path).parent
    return sorted(p.name for p in directory.iterdir()
                  if p.name.endswith((".json", ".journal", ".lock", ".oplock"))
                  and p.name != "config.json")


# ----------------------------------------------------------------------
# 逐个前置守卫：都要返回 ok=False + 明确原因，且绝不抛异常
# ----------------------------------------------------------------------
def test_missing_excel_path_is_refused(tmp_path, preview_env):
    bridge, captured, _ = preview_env
    bridge._config.excel_path = None

    got = bridge.wps_preview()

    assert got["ok"] is False
    assert "排单表" in got["reason"]
    assert captured["build_plan"] == [], "前置条件不满足时不该去读云端"


def test_disabled_sync_is_refused_before_reading_cloud(preview_env):
    """云同步关闭时连只读都不做（不是"前端隐藏按钮"就算数）。"""
    bridge, captured, _ = preview_env
    bridge._config.wps_enabled = False

    got = bridge.wps_preview()

    assert got["ok"] is False and got["code"] == "wps_disabled"
    assert captured["build_plan"] == []


def test_unconfigured_tables_are_refused(preview_env):
    bridge, _, monkeypatch = preview_env
    monkeypatch.setattr(bridge_module, "effective_tables", lambda _cfg: {})

    got = bridge.wps_preview()

    assert got["ok"] is False
    assert "测试文件 id" in got["reason"]


def test_effective_tables_error_is_surfaced(preview_env):
    bridge, _, monkeypatch = preview_env

    def boom(_cfg):
        raise WpsCloudError("目标表已过期")

    monkeypatch.setattr(bridge_module, "effective_tables", boom)

    got = bridge.wps_preview()

    assert got["ok"] is False and got["reason"] == "目标表已过期"


def test_unauthenticated_cli_is_refused(preview_env):
    bridge, captured, monkeypatch = preview_env
    monkeypatch.setattr(bridge, "_wps_cli", lambda: _Cli(authenticated=False))

    got = bridge.wps_preview()

    assert got["ok"] is False
    assert "去授权" in got["reason"]
    assert captured["build_plan"] == []


def test_cloud_error_while_building_the_plan_is_surfaced(preview_env):
    bridge, _, monkeypatch = preview_env

    def boom(*_a, **_k):
        raise WpsCloudError("云端表读不了")

    monkeypatch.setattr(bridge_module, "build_plan", boom)

    got = bridge.wps_preview()

    assert got["ok"] is False and got["reason"] == "云端表读不了"


def test_unexpected_exception_is_reported_not_raised(preview_env):
    """本地 Excel 坏了之类的意外错误也要变成 reason，不能把异常抛给前端。"""
    bridge, _, monkeypatch = preview_env

    def boom(*_a, **_k):
        raise KeyError("bad workbook")

    # 预览现在从**字节**解析（哈希与解析同源），所以异常注入点在这里。
    monkeypatch.setattr(bridge_module, "read_local_orders_from_bytes", boom)

    got = bridge.wps_preview()

    assert got["ok"] is False
    assert got["reason"] == "KeyError: 'bad workbook'", "要带异常类型，便于排查"


# ----------------------------------------------------------------------
# 安全属性：测试模式下强制关掉通讯记号
# ----------------------------------------------------------------------
def test_marker_is_forced_off_in_test_mode(preview_env):
    """测试模式绝不能写协作者的通讯记号（那是正式表上的东西）。"""
    bridge, captured, _ = preview_env
    bridge._config.wps_marker_enabled = True
    bridge._config.wps_test_mode = True

    bridge.wps_preview()

    assert captured["build_plan"][0]["marker_enabled"] is False


def test_marker_follows_config_outside_test_mode(preview_env):
    bridge, captured, _ = preview_env
    bridge._config.wps_marker_enabled = True
    bridge._config.wps_test_mode = False

    bridge.wps_preview()

    assert captured["build_plan"][0]["marker_enabled"] is True


def test_sort_and_address_order_come_from_config(preview_env):
    bridge, captured, _ = preview_env
    bridge._config.wps_sort_enabled = False
    bridge._config.wps_address_order = {"东湖中餐": ["小", "大西"]}

    bridge.wps_preview()

    kwargs = captured["build_plan"][0]
    assert kwargs["sort_enabled"] is False
    assert kwargs["address_order"] == {"东湖中餐": ["小", "大西"]}
    assert kwargs["run_date"] == _dt.date.today()


def test_target_date_uses_the_configured_window(preview_env):
    from app.wps_cloud import target_date_for

    bridge, captured, _ = preview_env
    bridge._config.wps_target_hour_start = 20
    bridge._config.wps_target_hour_end = 10

    got = bridge.wps_preview()

    expected = target_date_for(start_hour=20, end_hour=10)
    assert got["target_date"] == expected.isoformat()
    assert captured["build_plan"][0]["target"] == expected


# ----------------------------------------------------------------------
# 只读保证 + 成功返回形状 + 预览令牌
# ----------------------------------------------------------------------
def test_preview_never_writes_the_ledger_or_journal(preview_env):
    bridge, _, _ = preview_env
    got = bridge.wps_preview()
    assert got["ok"] is True
    assert _state_files(bridge) == [], "预览是只读的，绝不能落账本或意图日志"


def test_success_shape(preview_env):
    bridge, _, _ = preview_env

    got = bridge.wps_preview()

    assert got["ok"] is True
    assert got["text"] == "预览正文"
    assert got["summary"] == {"to_update": 1, "to_append": 2}
    assert got["status"] == "preview_ready"
    assert got["preview_id"] and got["preview_id"].startswith("pv-")
    assert got["expires_in"] > 0 and got["ttl_seconds"] == 600
    assert got["state"] == "valid"
    # test_mode 如实反映当前配置（注意 AppConfig 默认**开启**测试模式）
    assert got["test_mode"] == bridge._config.wps_test_mode
    assert _dt.date.fromisoformat(got["target_date"])


def test_preview_separates_planned_and_execution_summaries(preview_env):
    """预览只给**计划**口径；执行口径必须明确标注"尚未执行"。"""
    bridge, _, _ = preview_env
    got = bridge.wps_preview()

    planned = got["planned_summary"]
    assert planned["kind"] == "plan"
    assert planned["rows"]["to_update"] == 1 and planned["rows"]["to_append"] == 2

    executed = got["execution_summary"]
    assert executed["kind"] == "execution"
    assert executed["executed"] is False
    assert executed["proven_no_write"] is True
    assert executed["rows"]["verified"] == 0
    assert executed["rows"]["planned"] == 3, "计划口径要单独标注，不能顶替实际行数"
    assert "wps_upload" in got["next_action"]


def test_test_mode_flag_is_reported(preview_env):
    bridge, _, _ = preview_env
    bridge._config.wps_test_mode = True
    assert bridge.wps_preview()["test_mode"] is True


def test_plan_warnings_are_logged_as_warnings(preview_env):
    bridge, _, _ = preview_env

    bridge.wps_preview()

    events = [e for e in bridge.drain_events(0)["events"] if e["event"] == "log"]
    warned = [e["payload"]["msg"] for e in events
              if e["payload"]["level"] == "WARN" and "云同步预览" in e["payload"]["msg"]]
    assert warned and "清单外地址" in warned[0]


# ----------------------------------------------------------------------
# 上传必须绑定预览令牌
# ----------------------------------------------------------------------
def test_upload_without_preview_id_is_refused(preview_env):
    """无参上传已禁用：必须先预览，再传 preview_id。"""
    bridge, _, _ = preview_env

    got = bridge.wps_upload()

    assert got["ok"] is False and got["code"] == "missing_preview"
    assert got["execution_summary"]["proven_no_write"] is True
    assert got["execution_summary"]["rows"]["verified"] == 0


def test_upload_with_unknown_preview_id_is_refused(preview_env):
    bridge, _, _ = preview_env

    got = bridge.wps_upload("pv-does-not-exist")

    assert got["ok"] is False and got["code"] == "preview_not_found"
    assert got["execution_summary"]["proven_no_write"] is True


def test_upload_rejects_when_context_changed_after_preview(preview_env):
    """预览之后改了排序开关：上下文指纹变化 → 拒绝上传且不消费令牌。"""
    bridge, _, _ = preview_env
    preview = bridge.wps_preview()
    bridge._config.wps_sort_enabled = not bridge._config.wps_sort_enabled

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False and got["code"] == "preview_changed"
    assert got["changed"] and "sort_enabled" in got["changed"]
    assert got["execution_summary"]["proven_no_write"] is True


def test_upload_consumes_the_token_only_once(preview_env):
    """令牌是一次性的：同一 preview_id 第二次上传必须被拒绝。"""
    bridge, _, monkeypatch = preview_env
    calls: list[dict] = []

    def fake_apply(cli, plans, **kwargs):
        calls.append(kwargs)
        return {"sheets": [{"sheet": "东湖中餐", "status": "noop"}],
                "written": 0, "failed": 0, "operation_id": "", "journal_path": ""}

    monkeypatch.setattr(bridge_module, "apply_plan", fake_apply)
    preview = bridge.wps_preview()
    first = bridge.wps_upload(preview["preview_id"])
    assert first["ok"] is True and len(calls) == 1

    second = bridge.wps_upload(preview["preview_id"])
    assert second["ok"] is False and second["code"] == "preview_consumed"
    assert len(calls) == 1, "已消费的令牌不能再次触发写入"


def test_disabling_sync_invalidates_outstanding_previews(preview_env):
    """关闭云同步必须立刻作废手上所有令牌，重新开启也不能拿旧 id 写。"""
    bridge, _, _ = preview_env
    preview = bridge.wps_preview()
    bridge.save_wps_config({"enabled": False})

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False
    assert got["code"] in ("preview_invalidated", "wps_disabled")


def test_upload_execution_summary_reports_verified_rows(preview_env):
    """执行摘要的行数只认逐表回读校验通过的表（people 计数）。"""
    bridge, _, monkeypatch = preview_env

    def fake_apply(cli, plans, **kwargs):
        return {"sheets": [{"sheet": "东湖中餐", "status": "ok", "people": 7}],
                "written": 1, "failed": 0, "operation_id": "wps-abc",
                "journal_path": "/tmp/x.journal"}

    monkeypatch.setattr(bridge_module, "apply_plan", fake_apply)
    got = bridge.wps_upload(bridge.wps_preview()["preview_id"])

    assert got["ok"] is True
    executed = got["execution_summary"]
    assert executed["sheets"]["verified"] == 1
    assert executed["rows"]["verified"] == 7
    assert executed["rows_unknown"] is False
    assert executed["proven_no_write"] is False


def test_upload_of_uncertain_sheet_never_claims_zero_rows(preview_env):
    """部分失败/未知时行数必须是 null，绝不用计划数顶替。"""
    bridge, _, monkeypatch = preview_env

    def fake_apply(cli, plans, **kwargs):
        return {"sheets": [{"sheet": "东湖中餐", "status": "failed",
                            "reason": "写入失败", "uncertain": True}],
                "written": 0, "failed": 1, "operation_id": "", "journal_path": ""}

    monkeypatch.setattr(bridge_module, "apply_plan", fake_apply)
    got = bridge.wps_upload(bridge.wps_preview()["preview_id"])

    assert got["ok"] is False
    executed = got["execution_summary"]
    assert executed["rows"]["verified"] is None
    assert executed["rows_unknown"] is True
    assert executed["next_action"] == "manual_reconcile"
