"""前后端契约回归锁：TypeScript 接口声明的字段，Python 必须真的返回。

**为什么需要这个文件**：`frontend/src/lib/bridge.ts` 用 TS 接口描述 Python 侧的返回
结构，但两边是**不同语言、没有任何编译期或运行期链接**：

* Python 改了返回字段名（例如 `weekday_number` → `marker_weekday`），
  `pytest` 全绿 —— 因为 Python 测试断言的还是新名字；
* 前端 `pnpm test` 也全绿 —— 因为那 2 条测试只覆盖事件游标；
* 结果界面上读到 `undefined`，**静默失灵**，只有人肉点开才发现。

本文件从 `bridge.ts` 里解析出接口字段，再调用真实的 Python 生产函数，断言
**TS 标记为必需（无 `?`）的字段一个都不能少**。Python 多返回字段是无害的（TS 会忽略），
因此只做单向校验。

解析用正则而不是 TS 编译器：这些接口写法规整，正则足够，且不给项目引入新依赖。

**本文件的边界（变异测试确认过，不要误以为它更强）**：
只校验**字段是否存在**，不校验**值的正确性**。例如把
``status["weekday_number"] = ...`` 改写成写进别的键，键仍在（初始字典里已置 0），
本文件不会报错 —— 那种错误由 ``test_bridge_api_surface.py`` 里的**值断言**负责。
两者是互补的：**本文件防「前后端字段名漂移」，那边防「值算错」**。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import bridge as bridge_module
from app.bridge import Bridge
from app.wps_cloud import SyncLedger, WpsCloudError

BRIDGE_TS = Path(__file__).resolve().parent.parent / "frontend" / "src" / "lib" / "bridge.ts"


def ts_fields(interface: str) -> dict[str, bool]:
    """返回 ``{字段名: 是否可选}``。"""
    source = BRIDGE_TS.read_text(encoding="utf-8")
    match = re.search(rf"export interface {interface} \{{(.*?)\n\}}", source, re.S)
    assert match, f"bridge.ts 里找不到接口 {interface}"
    body = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)
    body = re.sub(r"//.*", "", body)
    fields: dict[str, bool] = {}
    for line in body.splitlines():
        found = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)(\?)?\s*:", line)
        if found:
            fields[found.group(1)] = found.group(2) == "?"
    return fields


def assert_required_present(interface: str, payload: dict) -> None:
    required = [name for name, optional in ts_fields(interface).items() if not optional]
    missing = [name for name in required if name not in payload]
    assert not missing, (
        f"前端接口 {interface} 要求的字段在 Python 返回值里缺失：{missing}；"
        f"这会让界面读到 undefined（Python 实际返回：{sorted(payload)}）")


def _bridge(tmp_path) -> Bridge:
    return Bridge(config_path=str(tmp_path / "config.json"))


# ----------------------------------------------------------------------
# bridge_ready → AppState / AppConfigState
# ----------------------------------------------------------------------
def test_bridge_ready_satisfies_app_state_contract(tmp_path):
    assert_required_present("AppState", _bridge(tmp_path).bridge_ready())


def test_bridge_ready_config_satisfies_app_config_state_contract(tmp_path):
    state = _bridge(tmp_path).bridge_ready()
    assert_required_present("AppConfigState", state["config"])


# ----------------------------------------------------------------------
# wps_status → WpsStatus / WpsTableState
# ----------------------------------------------------------------------
@pytest.fixture
def wps_bridge(tmp_path, monkeypatch):
    """让 wps_status 在不联网、不碰真实用户目录的前提下可跑。"""
    monkeypatch.setattr(bridge_module, "SyncLedger",
                        lambda *a, **k: SyncLedger(tmp_path / "ledger.json"))
    bridge = _bridge(tmp_path)
    bridge._config.wps_tables = {"东湖中餐": {"file_id": "F1"}}
    bridge._config.wps_production_tables = {"东湖中餐": {"file_id": "P1"}}

    class _Cli:
        path = "/fake/kdocs-cli"

        def authenticated(self) -> bool:
            return True

    monkeypatch.setattr(bridge, "_wps_cli", lambda: _Cli())
    return bridge


def test_wps_status_satisfies_wps_status_contract(wps_bridge):
    assert_required_present("WpsStatus", wps_bridge.wps_status())


def test_wps_status_tables_satisfy_wps_table_state_contract(wps_bridge):
    tables = wps_bridge.wps_status()["tables"]
    assert tables, "至少要有一张表才能校验行结构"
    for entry in tables:
        assert_required_present("WpsTableState", entry)


def test_wps_status_satisfies_contract_even_without_cli(wps_bridge, monkeypatch):
    """CLI 缺失走的是另一条分支，同样要满足契约。"""
    def boom():
        raise WpsCloudError("kdocs-cli 未找到")

    monkeypatch.setattr(wps_bridge, "_wps_cli", boom)
    assert_required_present("WpsStatus", wps_bridge.wps_status())


# ----------------------------------------------------------------------
# wps_check_copies → WpsCopyCheck / WpsCopyCheckItem
# ----------------------------------------------------------------------
def test_wps_check_copies_satisfies_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "COPY"}})
    bridge = _bridge(tmp_path)

    class _Cli:
        path = "/fake/kdocs-cli"

        def read_grid(self, file_id, *args, **kwargs):
            return {(2, 0): "张", (2, 2): "111"}

    monkeypatch.setattr(bridge, "_wps_cli", lambda: _Cli())
    result = bridge.wps_check_copies()

    assert_required_present("WpsCopyCheck", result)
    for item in result["tables"]:
        assert_required_present("WpsCopyCheckItem", item)


def test_wps_check_copies_failure_shape_satisfies_contract(tmp_path, monkeypatch):
    """``{"ok": False, "reason": ...}`` 也是合法返回，前端只会读这两个可选项。"""
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "COPY"}})

    def boom():
        raise WpsCloudError("kdocs-cli 未找到")

    monkeypatch.setattr(bridge, "_wps_cli", boom)
    assert_required_present("WpsCopyCheck", bridge.wps_check_copies())


# ----------------------------------------------------------------------
# 解析器本身要可靠（否则上面几条会「假通过」）
# ----------------------------------------------------------------------
def test_interface_parser_reads_real_definitions():
    app_config = ts_fields("AppConfigState")
    assert len(app_config) >= 30, "解析到的字段太少，正则很可能失效了"
    # 必需与可选字段都要能区分出来
    assert app_config["target_url"] is False
    assert app_config["sss_idempotency_field"] is True

    assert ts_fields("WpsTableState") == {
        "sheet": False, "file_id": False, "effective_file_id": False,
        "last_sync": False, "last_people": False}

    with pytest.raises(AssertionError):
        ts_fields("这个接口不存在")


def test_contract_check_would_catch_a_renamed_field(tmp_path, monkeypatch):
    """自检：把 Python 返回的某个字段改名，契约校验必须报错。

    没有这条，「契约测试」可能只是恰好通过而已。
    """
    bridge = _bridge(tmp_path)
    original = bridge.bridge_ready

    def renamed():
        state = original()
        state["config"]["weekday_number"] = state["config"].pop("wps_marker_enabled")
        return state

    monkeypatch.setattr(bridge, "bridge_ready", renamed)
    with pytest.raises(AssertionError, match="wps_marker_enabled"):
        assert_required_present("AppConfigState", bridge.bridge_ready()["config"])
