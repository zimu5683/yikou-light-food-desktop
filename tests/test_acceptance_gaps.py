"""验收矩阵（A01–A20）的**缺口补测**：逐项对应文档第 4.1 节。

已有的覆盖在哪（本文件不重复）：

* A01/A02/A03/A04/A05/A06/A07 —— ``tests/test_wps_cloud.py`` 与
  ``tests/test_wps_cloud_safety.py``（增量累加、重复上传零写入、协作者 0 保护、
  多槽位、批次日期闸门、排序前回滚、排序后不误删）；
* A11/A17（部分）—— ``tests/test_wps_recovery.py``；
* A14/A15 —— ``tests/test_sss.py``（站内对账是闪时送唯一的防重复闸门）。
  A13/A16/A18 对应的**闪时送未决日志**一族已在 2026-10-12 随机制移除
  （见 README「重复下单的最后一道防线」与发布记录）。

本文件补的是审计确认的缺口 + 本轮修复的回归锁：

* A08 预览后本地文件变化 → 上传前拒绝（重算 SHA-256）
* A09 预览令牌过期 / 关闭后重新开启的复用
* A10 意图日志损坏 → 结构化拒绝且零云端写入
* A19 浏览器（Playwright）路径：异常归类与接口路径一致（结果未知不自动重发）
* 新增：未处置的 ``uncertain`` 必须阻断同日期 + 同表重传（重复累加的最后防线）
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import threading
import types
from pathlib import Path

import pytest

from app import sss as sss_module
from app.sss import run_sss_job
from app.wps_cloud import CloudOrder, SyncLedger, apply_plan, build_plan
from app.wps_journal import SyncJournal, journal_path_for
from tests.sss_test_support import _config, _write_sss_excel
from tests.test_wps_cloud import BASE_HEADER, FakeCli, make_grid

TARGET = dt.date(2026, 9, 11)


# ======================================================================
# A08：预览之后本地文件变化 → 上传必须在写入前拒绝
# ======================================================================

def test_upload_rejects_when_the_local_file_content_changed(tmp_path, monkeypatch):
    """只比计划指纹不够：任何内容变化都要让预览令牌作废（用户心智与文档要求）。"""
    from app import bridge as bridge_module
    from app.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._config.wps_enabled = True
    excel = tmp_path / "排单.xlsx"
    excel.write_bytes(b"original-content")
    bridge._config.excel_path = excel

    calls: list[str] = []
    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "F1"}})
    monkeypatch.setattr(bridge_module, "read_local_orders_from_bytes",
                        lambda data, log=None: {"东湖中餐": []})
    monkeypatch.setattr(bridge_module, "build_plan", lambda cli, **kw: [])
    monkeypatch.setattr(bridge_module, "format_plan", lambda plans: "")
    monkeypatch.setattr(bridge_module, "summarize_plan", lambda plans: {})

    class _Cli:
        path = "/fake/kdocs-cli"

        def authenticated(self) -> bool:
            return True

    monkeypatch.setattr(bridge, "_wps_cli", lambda: _Cli())
    monkeypatch.setattr(bridge_module, "apply_plan",
                        lambda *a, **k: calls.append("apply") or {
                            "sheets": [], "written": 0, "failed": 0})

    preview = bridge.wps_preview()
    assert preview["ok"] is True and preview["preview_id"]

    # 预览之后改文件内容（模拟用户在 Excel 里又编辑过并保存）
    excel.write_bytes(b"changed-content-after-preview")

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False
    assert got["code"] == "preview_changed"
    assert "local_file" in (got.get("changed") or [])
    assert got["execution_summary"]["proven_no_write"] is True
    assert calls == [], "文件变了就绝不能调用 apply_plan"


def test_upload_accepts_when_the_file_is_untouched(tmp_path, monkeypatch):
    """反向锁：文件没变时不能因为新加的指纹校验而误拒。"""
    from app import bridge as bridge_module
    from app.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._config.wps_enabled = True
    excel = tmp_path / "排单.xlsx"
    excel.write_bytes(b"stable-content")
    bridge._config.excel_path = excel

    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "F1"}})
    monkeypatch.setattr(bridge_module, "read_local_orders_from_bytes",
                        lambda data, log=None: {"东湖中餐": []})
    monkeypatch.setattr(bridge_module, "build_plan", lambda cli, **kw: [])
    monkeypatch.setattr(bridge_module, "format_plan", lambda plans: "")
    monkeypatch.setattr(bridge_module, "summarize_plan", lambda plans: {})

    class _Cli:
        path = "/fake/kdocs-cli"

        def authenticated(self) -> bool:
            return True

    monkeypatch.setattr(bridge, "_wps_cli", lambda: _Cli())
    monkeypatch.setattr(bridge_module, "apply_plan",
                        lambda *a, **k: {"sheets": [], "written": 0, "failed": 0,
                                         "proven_no_write": True})

    preview = bridge.wps_preview()
    got = bridge.wps_upload(preview["preview_id"])
    assert got["ok"] is True


# ======================================================================
# A09：令牌过期 / 关闭后重新开启都拒绝
# ======================================================================

def test_preview_token_expires_after_ttl():
    """直接测令牌表：过期后既不能查成 valid，也不能被消费。"""
    from app.wps_preview import PreviewStore

    store = PreviewStore(ttl_seconds=600)
    record = store.create(local_sha256="sha", context={}, context_fingerprint="ctx",
                          plan=[], plan_fingerprint="plan", summary={}, text="",
                          tables=[], blocked=[], warnings=[], target_date="2026-09-11",
                          target_tables={}, now=1_000.0)

    assert store.get(record.preview_id, now=1_000.0)[1] == ""
    assert store.get(record.preview_id, now=1_599.0)[1] == "", "600 秒内仍有效"
    assert store.get(record.preview_id, now=1_601.0)[1] == "preview_expired"
    assert store.consume(record.preview_id, now=1_601.0)[1] == "preview_expired"


def test_upload_rejects_an_expired_token(tmp_path, monkeypatch):
    """桥接层：过期令牌必须返回 preview_expired 且不调用 apply_plan。"""
    from app import bridge as bridge_module
    from app.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._config.wps_enabled = True
    excel = tmp_path / "排单.xlsx"
    excel.write_bytes(b"x")
    bridge._config.excel_path = excel

    calls: list[str] = []
    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "F1"}})
    monkeypatch.setattr(bridge_module, "read_local_orders_from_bytes",
                        lambda data, log=None: {"东湖中餐": []})
    monkeypatch.setattr(bridge_module, "build_plan", lambda cli, **kw: [])
    monkeypatch.setattr(bridge_module, "format_plan", lambda plans: "")
    monkeypatch.setattr(bridge_module, "summarize_plan", lambda plans: {})
    monkeypatch.setattr(bridge_module, "apply_plan",
                        lambda *a, **k: calls.append("apply") or {})

    class _Cli:
        path = "/fake/kdocs-cli"

        def authenticated(self) -> bool:
            return True

    monkeypatch.setattr(bridge, "_wps_cli", lambda: _Cli())
    preview = bridge.wps_preview()
    pid = preview["preview_id"]

    # 手动把令牌改成 1 秒 TTL 并推到过期（不改生产 TTL）。
    record, code = bridge._previews.get(pid)
    assert code == "" and record is not None
    record.expires_at = 1.0

    got = bridge.wps_upload(pid)

    assert got["ok"] is False and got["code"] == "preview_expired"
    assert got["execution_summary"]["proven_no_write"] is True
    assert calls == []


def test_reenabling_sync_does_not_revive_old_tokens(tmp_path, monkeypatch):
    """关闭 → 重新开启后，关闭前拿到的 preview_id 依然不能用。

    这条比"关闭窗口内被拒"更强：如果只是靠 `wps_enabled=False` 短路，
    重新开启后旧令牌就会复活并真的写云端。
    """
    from app import bridge as bridge_module
    from app.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._config.wps_enabled = True
    excel = tmp_path / "排单.xlsx"
    excel.write_bytes(b"x")
    bridge._config.excel_path = excel

    calls: list[str] = []
    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "F1"}})
    monkeypatch.setattr(bridge_module, "read_local_orders_from_bytes",
                        lambda data, log=None: {"东湖中餐": []})
    monkeypatch.setattr(bridge_module, "build_plan", lambda cli, **kw: [])
    monkeypatch.setattr(bridge_module, "format_plan", lambda plans: "")
    monkeypatch.setattr(bridge_module, "summarize_plan", lambda plans: {})
    monkeypatch.setattr(bridge_module, "apply_plan",
                        lambda *a, **k: calls.append("apply") or {})

    class _Cli:
        path = "/fake/kdocs-cli"

        def authenticated(self) -> bool:
            return True

    monkeypatch.setattr(bridge, "_wps_cli", lambda: _Cli())
    preview = bridge.wps_preview()
    pid = preview["preview_id"]

    assert bridge.save_wps_config({"enabled": False})["ok"] is True
    assert bridge.save_wps_config({"enabled": True})["ok"] is True

    got = bridge.wps_upload(pid)

    assert got["ok"] is False, "重新开启后旧令牌不能复活"
    assert got["code"] in ("preview_invalidated", "preview_not_found")
    assert calls == []


# ======================================================================
# A10：意图日志损坏 → 结构化拒绝 + 零云端写入
# ======================================================================

def _broken_journal_bridge(tmp_path, monkeypatch):
    from app import bridge as bridge_module
    from app.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._config.wps_enabled = True
    excel = tmp_path / "排单.xlsx"
    excel.write_bytes(b"x")
    bridge._config.excel_path = excel

    calls: list[str] = []
    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "F1"}})
    monkeypatch.setattr(bridge_module, "read_local_orders_from_bytes",
                        lambda data, log=None: {"东湖中餐": []})
    monkeypatch.setattr(bridge_module, "build_plan", lambda cli, **kw: [])
    monkeypatch.setattr(bridge_module, "format_plan", lambda plans: "")
    monkeypatch.setattr(bridge_module, "summarize_plan", lambda plans: {})
    monkeypatch.setattr(bridge_module, "apply_plan",
                        lambda *a, **k: calls.append("apply") or {})

    class _Cli:
        path = "/fake/kdocs-cli"

        def authenticated(self) -> bool:
            return True

    monkeypatch.setattr(bridge, "_wps_cli", lambda: _Cli())
    return bridge, calls


def _corrupt_the_journal(bridge) -> Path:
    ledger, journal = bridge._wps_ledger_and_journal()
    path = journal_path_for(ledger.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 不是合法 JSON", encoding="utf-8")
    return path


def test_corrupt_journal_blocks_preview_without_raising(tmp_path, monkeypatch):
    """预览也必须拒绝：读不出"上次写到哪"就不该继续往下走。"""
    bridge, calls = _broken_journal_bridge(tmp_path, monkeypatch)
    _corrupt_the_journal(bridge)

    got = bridge.wps_preview()          # 不能抛异常

    assert got["ok"] is False
    assert got["code"] == "local_state_blocked"
    assert got["status"] == "blocked"
    assert got["execution_summary"]["proven_no_write"] is True
    assert calls == []


def test_corrupt_journal_blocks_upload_without_raising(tmp_path, monkeypatch):
    """A10 的核心：日志坏了要在写入之前拒绝，且返回结构化结果。"""
    bridge, calls = _broken_journal_bridge(tmp_path, monkeypatch)
    preview = bridge.wps_preview()
    assert preview["ok"] is True
    pid = preview["preview_id"]

    _corrupt_the_journal(bridge)
    got = bridge.wps_upload(pid)        # 不能抛异常

    assert got["ok"] is False
    assert got["code"] == "local_state_blocked"
    assert got["execution_summary"]["proven_no_write"] is True
    assert calls == [], "日志不可读时一行都不能写"


def test_corrupt_ledger_blocks_upload_without_raising(tmp_path, monkeypatch):
    """账本（幂等锚点）损坏同样失败关闭 —— 否则会把同一批餐重复加一遍。"""
    bridge, calls = _broken_journal_bridge(tmp_path, monkeypatch)
    ledger, _journal = bridge._wps_ledger_and_journal()
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text("{ 坏账本", encoding="utf-8")

    got = bridge.wps_preview()

    assert got["ok"] is False and got["code"] == "local_state_blocked"
    assert calls == []


# ======================================================================
# 新增（本轮修复）：未处置的 uncertain 必须阻断同日期 + 同表重传
# ======================================================================

@pytest.fixture
def wps_env(tmp_path: Path):
    ledger = SyncLedger(tmp_path / "state.json")
    journal = SyncJournal(journal_path_for(ledger.path))
    return ledger, journal


def _wps_plan(cli, meals: int = 6):
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", meals,
                         row=3, rows=(3,))]
    return build_plan(cli, local_orders={"东湖中餐": orders},
                      tables={"东湖中餐": {"file_id": "F1"}},
                      target=TARGET, ledger=None)


def test_unresolved_wps_write_blocks_the_same_batch_reupload(wps_env):
    """上一次写入结果未知（verify_failed → uncertain）后，再上传同表必须被拒。

    这是重复累加的**最后一道防线**：如果那次其实写成功了，这一次会把同一批餐
    再加一遍。``retire_guarded`` 与 pending 都必须挡住。
    """
    ledger, journal = wps_env
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]),
                  corrupt_write=True)
    first = apply_plan(cli, _wps_plan(cli), ledger=ledger, marker_enabled=False,
                       journal=journal)
    assert first["sheets"][0]["status"] == "verify_failed"

    cli2 = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    second = apply_plan(cli2, _wps_plan(cli2), ledger=ledger, marker_enabled=False,
                        journal=SyncJournal(journal_path_for(ledger.path)))

    assert second["sheets"][0]["status"] == "stale_batch"
    assert "未处置的同目标写入" in second["sheets"][0]["reason"]
    assert not cli2.writes and not cli2.inserts, "有未处置记录时一个写请求都不该发"
    assert second["proven_no_write"] is True


def test_verified_write_does_not_block_the_next_batch(wps_env):
    """反向锁：正常成功后同一张表仍然可以继续上传（否则功能不可用）。"""
    ledger, journal = wps_env
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    first = apply_plan(cli, _wps_plan(cli), ledger=ledger, marker_enabled=False,
                       journal=journal)
    assert first["sheets"][0]["status"] == "ok"

    cli2 = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "9",
         8: "=SUM(D3)", 9: "=H3-I3"}]))
    second = apply_plan(cli2, _wps_plan(cli2), ledger=ledger, marker_enabled=False,
                        journal=SyncJournal(journal_path_for(ledger.path)))
    assert second["sheets"][0]["status"] in ("ok", "noop"), second["sheets"]


def test_blocked_result_reports_proven_no_write_and_zero_counts(wps_env):
    """§2.1.4：整批阻断必须明确报告零写入、零账本更新、可证明未写入。"""
    ledger, _journal = wps_env
    cli = FakeCli(make_grid(BASE_HEADER, []))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6, row=3,
                         rows=(3,), weekday_marks=("周五",))]
    plans = build_plan(cli, local_orders={"东湖中餐": orders},
                       tables={"东湖中餐": {"file_id": "F1"}},
                       target=dt.date(2026, 9, 12), ledger=ledger)
    reads_before = len(cli.reads)

    result = apply_plan(cli, plans, ledger=ledger, marker_enabled=False)

    assert result["proven_no_write"] is True
    assert result["cloud_writes"] == 0
    assert result["ledger_updates"] == 0
    sheet = result["sheets"][0]
    assert sheet["status"] == "blocked"
    assert sheet["cloud_writes"] == 0 and sheet["ledger_updates"] == 0
    assert len(cli.reads) == reads_before, "被闸门拒绝的表连只读请求都不该发"


# ======================================================================
# A19：浏览器表单路径 —— 提交前留痕 + 异常归类与接口路径一致
# ======================================================================

class _FakeLocator:
    """候选链定位用的假定位器：key 在 page.visible 里才算可见。"""

    def __init__(self, page, key, found, picked=""):
        self._page, self._key, self._found, self.picked = page, key, found, picked

    @property
    def first(self):
        return self

    @property
    def last(self):
        self.picked = "last"
        return self

    def filter(self, has_text=None, has_text_re=None):
        # 生产代码把 has_text_re 编译成 re.Pattern 经 has_text 传入
        # （如“确\s*定”容忍两字按钮空隙）：假实现不改命中判定。
        if isinstance(has_text, re.Pattern):
            has_text = None
        key = has_text if has_text else self._key
        return _FakeLocator(self._page, key,
                            self._found and key in self._page.visible, self.picked)

    def count(self):
        return 1 if self._found else 0

    def wait_for(self, state="visible", timeout=None):
        if not self._found:
            raise TimeoutError(f"fake wait timeout: {self._key}")

    def click(self, timeout=None):
        self._page._click(self._key, self.picked)

    def fill(self, value, timeout=None):
        self._page.form[self._key] = str(value)

    def press(self, _key, timeout=None) -> None: ...

    def type(self, text, timeout=None):
        self._page.form["__typed__"] = str(text)

    def element_handle(self, timeout=None):
        return object() if self._found else None


class _FakeKeyboard:
    def __init__(self, page):
        self._page = page

    def press(self, key):
        if key == "Escape":
            self._page.mask_visible = False


class _FakePage:
    """浏览器表单分支的假页面。

    evaluate() 只服务 GET（门店/常用地址/余额/列表对账）；建单走 UI 表单：
    ``visible`` 集合决定候选链能否命中，点下最终确定（普通 pick 的「确定」）
    视为服务端受理成功 —— 订单进入列表供对账、遮罩收场。
    ``modal_never_closes`` / ``create_never_recovers`` 模拟"最终确定已点击但
    结果无法确认"的两种故障，此时**不**落单（对应服务端实际拒绝/超时的
    未知结果，由只读对账判缺、未决日志保持 unresolved）。
    """

    _ADDR_DETAIL = "浙江农林大学东湖校区"

    def __init__(self, *, list_records=None, modal_never_closes=False,
                 create_never_recovers=False, missing_element=""):
        self.calls: list[tuple[str, str]] = []
        self.confirms: list[dict] = []
        self._list_records = list_records or []
        self._modal_never_closes = modal_never_closes
        self._create_never_recovers = create_never_recovers
        self.visible = {
            "创建订单", "即时单", "预约单", "自动分单", "一口轻食",
            "常用地址", "嗯哼", "确定",
            ".ant-picker-ok button",
            ".ant-modal-footer .ant-btn-primary",
            "请选择日期时间",
            "#receiveName", "#receiveMobile",
            "//*[contains(text(),'名称')]/following::input[1]",
            "#doorNum",
        }
        if missing_element:
            self.visible.discard(missing_element)
        self.form: dict[str, str] = {}
        self.mask_visible = True
        # 置为 True 后列表接口返回 500：模拟"查站失败"（对账必须失败关闭）。
        self.list_fails = False
        self.keyboard = _FakeKeyboard(self)

    def _click(self, key: str, picked: str) -> None:
        self.calls.append(("click", f"{key}@{picked or 'first'}"))
        # 最终确定（地址确定是 pick="last"）；首选候选是 css（has_text_re），
        # 命中 key 是 css 字符串，文本“确定”仅作旧候选链兜底。
        if key in ("确定", ".ant-modal-footer .ant-btn-primary") and picked == "":
            self.confirms.append(dict(self.form))
            if self._modal_never_closes:
                return  # 结果未知：遮罩收不了场，不落单，交由只读对账判定
            self.mask_visible = False  # 遮罩收场
            if self._create_never_recovers:
                self.visible.discard("创建订单")  # 但「创建订单」不恢复：同样结果未知
                return
            self._list_records = [*self._list_records, {
                "recipientName": self.form.get("#receiveName", ""),
                "recipientPhone": [self.form.get("#receiveMobile", "")],
                "recipientAddress": f"{self._ADDR_DETAIL}"
                                    f"{self.form.get('#doorNum', '')}",
                "expectedDeliveryTime": self.form.get("__typed__", ""),
                "status": 2,
            }]
            self.mask_visible = False
            if self._create_never_recovers:
                self.visible.discard("创建订单")

    # ---- 候选链定位表面（_build_locator 消费） ----
    def locator(self, selector):
        if selector == "text=创建订单":
            return _FakeLocator(self, "创建订单", "创建订单" in self.visible)
        return _FakeLocator(self, selector, selector in self.visible)

    def get_by_text(self, text):
        return _FakeLocator(self, text, text in self.visible)

    def get_by_placeholder(self, placeholder):
        return _FakeLocator(self, placeholder, placeholder in self.visible)

    def get_by_role(self, role, **kwargs):
        name = str(kwargs.get("name") or role)
        return _FakeLocator(self, name, name in self.visible)

    # ---- 其它 ----
    def goto(self, *_a, **_k) -> None: ...

    def wait_for_selector(self, selector, state=None, timeout=None):
        if state == "hidden":
            if self.mask_visible:
                raise TimeoutError("fake mask still visible")
            return None
        raise AssertionError(f"unexpected wait_for_selector: {selector} {state}")

    def wait_for_timeout(self, *_a, **_k) -> None: ...

    def evaluate(self, _js: str, arg: dict) -> str:
        method = str(arg.get("method") or "")
        path = str(arg.get("path") or "")
        self.calls.append((method, path))
        assert method != "POST", "表单模式下建单不再走页面内 fetch POST"
        if "list" in path and self.list_fails:
            raise RuntimeError("对账列接口 500")
        if "list" in path:
            payload = {"success": True, "result": {"records": self._list_records,
                                                  "total": len(self._list_records)}}
        elif "store" in path:
            payload = {"success": True, "result": [{"name": "一口轻食", "id": 7}]}
        elif "account" in path:
            payload = {"success": True, "result": {"totalAmount": 1000.0}}
        elif "customerAddress" in path:
            # 「嗯哼」常用地址（2026-09-05 实测字段结构：code=区划码、position=地址）
            payload = {"success": True, "result": [{
                "id": 75812, "name": "嗯哼", "longitude": "119.728224",
                "latitude": "30.256632", "code": "330110",
                "position": self._ADDR_DETAIL}]}
        else:
            payload = {"success": True, "result": []}
        return json.dumps({"http": 200, "text": json.dumps(payload, ensure_ascii=False)})


@pytest.fixture
def browser_env(tmp_path, monkeypatch):
    """把 Playwright 整体替换成假实现，只保留 sss.py 的浏览器分支逻辑。"""
    _write_sss_excel(tmp_path)
    cfg = _config(tmp_path, sss_order_source="excel", api_mode=False,
                  sss_max_workers=1)

    page = _FakePage()

    class _FakeBrowser:
        def new_page(self):
            return page

        def new_browser_cdp_session(self):
            raise AssertionError("不应真的做 CDP")

        def close(self) -> None: ...

    class _FakePlaywright:
        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None: ...

    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: _FakePlaywright()
    fake_playwright = types.ModuleType("playwright")
    fake_playwright.sync_api = fake_sync_api
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    monkeypatch.setattr(sss_module, "_launch_browser",
                        lambda playwright, headless: _FakeBrowser())
    monkeypatch.setattr(sss_module, "_ensure_logged_in",
                        lambda page_, account, password, stop, cb: None)
    monkeypatch.setattr(sss_module, "_minimize_window", lambda browser, cb: None)
    return cfg, page


def test_browser_path_submits_and_reconciles(browser_env):
    """A19：浏览器表单路径真的点下最终确定，并以站内对账确认为准。"""
    cfg, page = browser_env

    result = run_sss_job(cfg, threading.Event(), progress_callback=lambda _m: None,
                         password="pw", captcha_callback=lambda _img: "1234")

    assert result["created"] == 2, "假实现里两单都应成功"
    assert result["reconciled"] is True
    assert result["uncertain"] is False
    assert len(page.confirms) == 2, "表单路径应该真的点下最终确定"
    assert all(method != "POST" for method, _path in page.calls), (
        "表单模式下建单不再走页面内 fetch POST")


def test_browser_path_modal_stuck_ends_at_the_reconciliation_verdict(
        browser_env, monkeypatch):
    """A19：弹窗收不了场 → 提交层按未知处理；站内对账查得到就据实给结论。

    平台驳回/超时都表现为"点了最终确定却没拿到收场信号"；此时**只看站内**：
    假页面里这一单并没落单，于是收尾对账如实给出"确认 0/2、2 单未完成"，
    而不是含糊的"结果未知"。绝不因为一次没收场就自动重发同一单。
    """
    cfg, page = browser_env
    monkeypatch.setattr(sss_module, "_UI_MODAL_CLOSE_TIMEOUT_S", 0.6)
    monkeypatch.setattr(sss_module, "_UI_MODAL_POLL_MS", 100)
    page._modal_never_closes = True

    result = run_sss_job(cfg, threading.Event(),
                         progress_callback=lambda _m: None,
                         password="pw", captcha_callback=lambda _img: "1234")

    assert result["created"] == 0, "弹窗没收场就不该被当成确认成功"
    assert result["reconciled"] is True
    assert result["partial"] is True, "站内对账确认没落单，据实报部分完成"
    assert result["uncertain"] is False
    assert len(page.confirms) == 2, "假页面里每单只点一次，绝不自动重发同一单"


def test_browser_path_modal_stuck_with_failed_reconciliation_is_unknown(
        browser_env, monkeypatch):
    """A19：弹窗收不了场 **且**站内查不出来 → 结果未知，绝不显示成干净失败。"""
    cfg, page = browser_env
    monkeypatch.setattr(sss_module, "_UI_MODAL_CLOSE_TIMEOUT_S", 0.6)
    monkeypatch.setattr(sss_module, "_UI_MODAL_POLL_MS", 100)
    page._modal_never_closes = True
    # 对账列接口整体 500：form 提交照常走，但查站失败。
    original_click = page._click

    def click_then_break_list(key: str, picked: str) -> None:
        original_click(key, picked)
        page.list_fails = True

    page._click = click_then_break_list

    result = run_sss_job(cfg, threading.Event(),
                         progress_callback=lambda _m: None,
                         password="pw", captcha_callback=lambda _img: "1234")

    assert result["uncertain"] is True, (
        "查站失败 + 弹窗没收场 = 结果未知；显示成干净失败会让用户直接重跑")
    assert result["reconciled"] is False
    assert result["created"] == 0


def test_browser_path_stuck_page_after_modal_close_falls_back_to_reconciliation(
        browser_env, tmp_path):
    """遮罩收场但「创建订单」不恢复：提交层同样按未知处理，由站内对账定论。"""
    _write_sss_excel(tmp_path, rows=1)  # 单单场景，避免把重登路径搅进来
    cfg, page = browser_env
    page._create_never_recovers = True

    result = run_sss_job(cfg, threading.Event(),
                         progress_callback=lambda _m: None,
                         password="pw", captcha_callback=lambda _img: "1234")

    assert result["created"] == 0
    assert result["reconciled"] is True
    assert result["partial"] is True, "查站确认没落单 → 部分完成，而不是含糊的未知"
    assert len(page.confirms) == 1, "只点过一次最终确定，没有自动重发"


def test_browser_path_pre_submit_failure_is_a_definite_failure(browser_env):
    """反向锁：最终确定**之前**的确定性失败 → 这单确定没创建。"""
    cfg, page = browser_env
    page.visible.discard("#receiveName")  # 表单中途丢失姓名输入框

    result = run_sss_job(cfg, threading.Event(),
                         progress_callback=lambda _m: None,
                         password="pw", captcha_callback=lambda _img: "1234")

    assert result["created"] == 0
    assert page.confirms == [], "最终确定从未被点击 —— 这单确定没有创建"


def test_missing_success_field_is_treated_as_unknown():
    """响应里没有 success 字段（协议变了/网关 JSON）→ 未知，不是拒绝。"""
    from app.sss import _check_success, _SubmissionUncertain

    with pytest.raises(_SubmissionUncertain):
        _check_success({"code": 0, "msg": "ok"})
    with pytest.raises(_SubmissionUncertain):
        _check_success({})


def test_explicit_false_is_a_definite_rejection():
    """显式 success=false 仍然是"服务端拒绝"（可安全关闭记录）。"""
    from app.sss import _check_success

    with pytest.raises(LookupError) as excinfo:
        _check_success({"success": False, "message": "商品已下架"})
    assert not isinstance(excinfo.value, sss_module._SubmissionUncertain)


# ======================================================================
# 操作槽位：完成态不能继续显示 active（§2.3）
# ======================================================================

def test_finished_operations_never_report_active(tmp_path, monkeypatch):
    """业务状态词可以是任何值；终结后 `active` 必须为假（前端据此解禁按钮）。"""
    from app import bridge as bridge_module
    from app.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._config.wps_enabled = True
    excel = tmp_path / "排单.xlsx"
    excel.write_bytes(b"x")
    bridge._config.excel_path = excel
    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "F1"}})
    monkeypatch.setattr(bridge_module, "read_local_orders_from_bytes",
                        lambda data, log=None: {"东湖中餐": []})
    monkeypatch.setattr(bridge_module, "build_plan", lambda cli, **kw: [])
    monkeypatch.setattr(bridge_module, "format_plan", lambda plans: "")
    monkeypatch.setattr(bridge_module, "summarize_plan", lambda plans: {})

    class _Cli:
        path = "/fake/kdocs-cli"

        def authenticated(self) -> bool:
            return True

    monkeypatch.setattr(bridge, "_wps_cli", lambda: _Cli())
    bridge.wps_preview()

    status = bridge.operation_status()
    assert status["active"] is False, "preview_ready 是完成态，不能继续显示进行中"
    assert status["operation"] is None
    assert status["last"]["status"] == "preview_ready"
    assert status["last"]["active"] is False


def test_unknown_business_status_is_still_terminal(tmp_path):
    """任意业务状态（这里用 sss 的 no_orders）终结后都不能停留在 active。"""
    from app.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    operation, conflict = bridge._reserve("sss", title="闪时送下单")
    assert conflict is None
    bridge._worker_operation = operation
    bridge._finish_task("闪时送没有需要下单的订单", {"status": "no_orders",
                                                    "processed": 0, "created": 0})

    status = bridge.operation_status()
    assert status["active"] is False
    assert status["last"]["status"] == "no_orders"
    assert status["last"]["active"] is False


def test_conflict_reports_the_running_operation(tmp_path):
    """冲突时必须能说明"谁在跑、下一步做什么"。"""
    from app.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._config.wps_enabled = True
    bridge._config.excel_path = tmp_path / "排单.xlsx"
    bridge._config.excel_path.write_bytes(b"x")
    operation, _ = bridge._reserve("order", title="订单处理", phase="登录")
    try:
        got = bridge.operation_status()
        assert got["ok"] is True, "只读查询不该被互斥挡住"
        conflict = bridge.wps_preview()
        assert conflict["ok"] is False
        assert conflict["code"] == "operation_conflict"
        assert "订单处理" in conflict["reason"]
        assert conflict["next_action"]
        assert conflict["conflicting_operation"]["mode"] == "order"
    finally:
        bridge._operations.finish(operation, status="success")


# ======================================================================
# A28：Bridge 全异常路径都不留僵死占位
# ======================================================================

def test_a28_bridge_releases_the_slot_on_internal_failures(tmp_path, monkeypatch):
    """保存配置/写密钥链/起线程失败后，占位必须释放，下一个操作仍可启动。"""
    from app import bridge as bridge_module
    from app.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    excel = tmp_path / "排单.xlsx"
    _write_workbook(excel)

    payload = {
        "url": "https://m.icall.me/admin/#/login", "phone": "13800000000",
        "password": "pw", "excel": str(excel), "date": "", "count": "",
        "remember": True, "api_mode": True,
    }

    def boom_save(*_a, **_k):
        raise OSError("磁盘满")

    monkeypatch.setattr(type(bridge._config), "save", boom_save)
    got = bridge.start_order(payload)
    assert got["ok"] is False and got["reason"] == "internal_error"
    assert bridge.operation_status()["active"] is False, "失败后不能留下僵死占位"

    monkeypatch.undo()
    started: list[str] = []
    monkeypatch.setattr(bridge, "_launch",
                        lambda mode, cfg, count, password: started.append(mode) or True)
    assert bridge.start_order(payload)["ok"] is True
    assert started == ["order"], "上一次失败不能影响下一次启动"

    # 密钥链失败同样要释放
    bridge.operation_status()  # 触发一次读取（不改变状态）
    bridge._operations.finish(bridge._worker_operation, status="success")
    bridge._worker_operation = None

    def boom_password(*_a, **_k):
        raise RuntimeError("keyring 不可用")

    monkeypatch.setattr(bridge_module, "set_password", boom_password)
    got = bridge.start_order(payload)
    assert got["ok"] is False and got["reason"] == "internal_error"
    assert bridge.operation_status()["active"] is False


def test_a28_conflict_does_not_disturb_the_running_operation(tmp_path):
    """冲突返回不能影响正在跑的那个操作（它的槽位与状态必须原样保留）。"""
    from app.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._config.wps_enabled = True
    bridge._config.excel_path = _write_workbook(tmp_path / "排单.xlsx")

    operation, _conflict = bridge._reserve("order", title="订单处理", phase="登录")
    try:
        got = bridge.check_updates()
        assert got["ok"] is False and got["reason"] == "operation_conflict"
        status = bridge.operation_status()
        assert status["active"] is True
        assert status["operation"]["mode"] == "order"
        assert status["operation"]["phase"] == "登录"
    finally:
        bridge._operations.finish(operation, status="success")


# ======================================================================
# A29：只有样式变化 / 同尺寸同 mtime 的改动也作废令牌
# ======================================================================

def test_a29_style_only_change_invalidates_the_token(tmp_path, monkeypatch):
    """只改单元格格式（数据完全一样）也要让预览令牌失效。"""
    from openpyxl import load_workbook

    from app import bridge as bridge_module
    from app.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._config.wps_enabled = True
    excel = _write_workbook(tmp_path / "排单.xlsx")
    bridge._config.excel_path = excel

    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "F1"}})
    monkeypatch.setattr(bridge_module, "build_plan", lambda cli, **kw: [])
    monkeypatch.setattr(bridge_module, "format_plan", lambda plans: "")
    monkeypatch.setattr(bridge_module, "summarize_plan", lambda plans: {})

    class Cli:
        path = "/fake/kdocs-cli"

        def authenticated(self) -> bool:
            return True

    monkeypatch.setattr(bridge, "_wps_cli", lambda: Cli())
    calls: list[str] = []
    monkeypatch.setattr(bridge_module, "apply_plan",
                        lambda *a, **k: calls.append("apply") or {
                            "sheets": [], "written": 0, "failed": 0})

    preview = bridge.wps_preview()
    assert preview["ok"] is True

    # 只改字体（数据一个字节都不变）
    from openpyxl.styles import Font

    workbook = load_workbook(excel)
    sheet = workbook["东湖中餐"]
    sheet["B3"].font = Font(bold=True)
    workbook.save(excel)

    got = bridge.wps_upload(preview["preview_id"])
    assert got["ok"] is False and got["code"] == "preview_changed"
    assert "local_file" in (got.get("changed") or [])
    assert calls == [], "样式变了也不能上传"


def test_a29_same_size_change_is_caught_by_the_content_hash(tmp_path, monkeypatch):
    """同尺寸改动（内容变了但长度相同）同样要被内容哈希挡住。"""
    from app import bridge as bridge_module
    from app.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._config.wps_enabled = True
    excel = tmp_path / "排单.xlsx"
    excel.write_bytes(b"A" * 4096)
    bridge._config.excel_path = excel

    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "F1"}})
    monkeypatch.setattr(bridge_module, "read_local_orders_from_bytes",
                        lambda data, log=None: {"东湖中餐": []})
    monkeypatch.setattr(bridge_module, "build_plan", lambda cli, **kw: [])
    monkeypatch.setattr(bridge_module, "format_plan", lambda plans: "")
    monkeypatch.setattr(bridge_module, "summarize_plan", lambda plans: {})

    class Cli:
        path = "/fake/kdocs-cli"

        def authenticated(self) -> bool:
            return True

    monkeypatch.setattr(bridge, "_wps_cli", lambda: Cli())
    monkeypatch.setattr(bridge_module, "apply_plan",
                        lambda *a, **k: {"sheets": [], "written": 0, "failed": 0})

    preview = bridge.wps_preview()
    assert preview["ok"] is True

    same_size = b"B" * 4096          # 长度相同、内容不同
    excel.write_bytes(same_size)
    got = bridge.wps_upload(preview["preview_id"])
    assert got["ok"] is False and got["code"] == "preview_changed"


def _write_workbook(path):
    """写一个能被 openpyxl 解析的最小排单表。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "东湖中餐"
    ws.cell(2, 2, "姓名")
    ws.cell(3, 2, "老人")
    ws.cell(3, 3, "小")
    ws.cell(3, 4, "13800000000")
    ws.cell(3, 14, 6)
    wb.save(path)
    return path
