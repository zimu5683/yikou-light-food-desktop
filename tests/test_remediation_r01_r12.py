"""R01–R12 修复的回归锁：每条对应文档 §0.3 的一个编号。

放在一起的原因：这些编号是验收报告要逐条回答的对象，测试与缺陷一一对应，
改名或删除就会让报告里的"已修复"失去证据。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import threading

import pytest

from app import sss as sss_module
from app import wps_recovery as recovery_module
from app.sss_journal import (append_records, batch_key,
                             batch_lock_path, blocking_state, journal_lock_path,
                             load_journal)
from app.wps_atomicio import (AtomicWriteError, FileLock, LockTimeout,
                              normalise_lock_identity)
from app.wps_cloud import (CloudOrder, SyncLedger, WpsCloudError, apply_plan,
                           build_plan)
from app.wps_journal import SyncJournal, journal_path_for
from tests.test_process_and_crash import (_MultiSheetCli, SIX_SHEETS,
                                          _six_table_fixture)
from tests.test_wps_cloud import BASE_HEADER, FakeCli, make_grid


# ======================================================================
# R01：文件锁的所有权、重入、异常清理与路径归一
# ======================================================================

def test_r01_interleaved_release_does_not_leak_the_lock(tmp_path):
    """先释放"先获取的实例"不能把锁泄漏掉（原来的 bug：计数错减，永不释放）。"""
    path = tmp_path / "x.lock"
    first, second = FileLock(path, timeout=0.2), FileLock(path, timeout=0.2)
    first.acquire()
    second.acquire()
    first.release()
    second.release()

    probe = FileLock(path, timeout=0.2)
    probe.acquire()
    probe.release()


def test_r01_outer_holder_keeps_the_lock_after_inner_release(tmp_path):
    path = tmp_path / "x.lock"
    outer, inner = FileLock(path, timeout=0.2), FileLock(path, timeout=0.2)
    outer.acquire()
    inner.acquire()
    inner.release()

    blocked: list[bool] = []

    def contender() -> None:
        try:
            lock = FileLock(path, timeout=0.15)
            lock.acquire()
            blocked.append(False)
            lock.release()
        except LockTimeout:
            blocked.append(True)

    thread = threading.Thread(target=contender)
    thread.start()
    thread.join()
    assert blocked == [True], "外层未释放时竞争者不能进入"

    outer.release()
    thread = threading.Thread(target=contender)
    thread.start()
    thread.join()
    assert blocked[-1] is False


def test_r01_extra_release_is_idempotent(tmp_path):
    path = tmp_path / "x.lock"
    lock = FileLock(path, timeout=0.2)
    lock.acquire()
    lock.release()
    lock.release()
    lock.release()
    probe = FileLock(path, timeout=0.2)
    probe.acquire()
    probe.release()


def test_r01_exception_path_releases_the_thread_lock(tmp_path, monkeypatch):
    """没有锁原语（RuntimeError）时也要把线程锁还回去，不能永久卡住同路径。"""
    from app import wps_atomicio as atomicio

    path = tmp_path / "x.lock"

    def boom(self, fd):
        raise RuntimeError("no primitive")

    monkeypatch.setattr(atomicio.FileLock, "_try_lock", boom)
    with pytest.raises(RuntimeError):
        FileLock(path, timeout=0.1).acquire()
    monkeypatch.undo()

    again = FileLock(path, timeout=0.3)
    again.acquire()
    again.release()


def test_r01_open_failure_raises_atomic_write_error(tmp_path, monkeypatch):
    from app import wps_atomicio as atomicio

    path = tmp_path / "x.lock"

    def boom(self):
        raise OSError("open failed")

    monkeypatch.setattr(atomicio.FileLock, "_open", boom)
    with pytest.raises(AtomicWriteError):
        FileLock(path, timeout=0.1).acquire()
    monkeypatch.undo()
    probe = FileLock(path, timeout=0.3)
    probe.acquire()
    probe.release()


def test_r01_path_aliases_share_one_lock(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir)

    assert (normalise_lock_identity(real_dir / "s.lock")
            == normalise_lock_identity(link / "s.lock"))
    assert (normalise_lock_identity(real_dir / "s.lock")
            == normalise_lock_identity(real_dir / ".." / "real" / "s.lock"))

    holder = FileLock(real_dir / "s.lock", timeout=0.2)
    holder.acquire()
    blocked: list[bool] = []

    def contender() -> None:
        try:
            lock = FileLock(link / "s.lock", timeout=0.15)
            lock.acquire()
            blocked.append(False)
            lock.release()
        except LockTimeout:
            blocked.append(True)

    thread = threading.Thread(target=contender)
    thread.start()
    thread.join()
    assert blocked == [True], "别名必须被识别为同一把锁"
    holder.release()


def test_r01_batch_lock_is_a_separate_file_from_the_data_lock(tmp_path):
    """批次锁与数据锁必须分离：两者共用一个文件就只能靠同线程重入避免死锁。"""
    state = tmp_path / "sss_uncertain.json"
    assert batch_lock_path(state) != journal_lock_path(state)
    assert str(batch_lock_path(state)).endswith(".batch.lock")

    # 批次锁被别的线程持有时，数据锁仍然可以正常读写（不会互相卡死）。
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        lock = FileLock(batch_lock_path(state), timeout=0.0)
        lock.acquire()
        held.set()
        release.wait(10)
        lock.release()

    thread = threading.Thread(target=holder)
    thread.start()
    held.wait(5)
    try:
        append_records(state, batch_key("2026-09-12", "", "1"),
                       [{"identifier": "a", "fingerprint": {}}],
                       meta={"account": "1", "platform": "https://x"})
        assert load_journal(state)["records"], "数据锁不该被批次锁挡住"
    finally:
        release.set()
        thread.join()


# ======================================================================
# R02：取锁后重读权威阻断状态
# ======================================================================

def test_r02_in_lock_reread_blocks_when_a_record_appears_in_the_window(
        tmp_path, monkeypatch):
    """先读空日志 → 窗口期出现未决记录 → 取锁后重读必须拒绝提交。"""

    state = tmp_path / "sss_uncertain.json"
    monkeypatch.setenv("YIKOU_SSS_UNCERTAIN_PATH", str(state))

    calls = {"blocking_state": 0, "posts": 0}
    real_blocking_state = sss_module.blocking_state

    def spying_blocking_state(path, *, delivery_date, account, origin):
        calls["blocking_state"] += 1
        result = real_blocking_state(path, delivery_date=delivery_date,
                                     account=account, origin=origin)
        if calls["blocking_state"] == 1:
            # 第一次（取锁前）返回"没有阻断"，并在窗口期把记录写下去。
            append_records(path, batch_key(delivery_date, "", account),
                           [{"identifier": "窗口期记录", "fingerprint": {}}],
                           meta={"account": account, "platform": origin,
                                 "delivery_date": delivery_date})
            assert result["blocked"] is False
        return result

    monkeypatch.setattr(sss_module, "blocking_state", spying_blocking_state)

    class Client:
        def fetch_captcha(self): return b"png"

        def login(self, code): return {"success": True}

        def get_json(self, path):
            if "list" in path:
                return {"success": True, "result": {"records": [], "total": 0}}
            if "store" in path:
                return {"success": True, "result": [{"name": "一口轻食", "id": 7}]}
            if "account" in path:
                return {"success": True, "result": {"totalAmount": 1000.0}}
            return {"success": True, "result": []}

        def post_json(self, path, payload):
            calls["posts"] += 1
            return {"success": True}

        def fork(self): return self

        def close(self): pass

    cfg = _excel_config(tmp_path, state)
    monkeypatch.setattr(sss_module, "SssApiClient", lambda *a, **k: Client())
    result = sss_module.run_sss_job(cfg, threading.Event(),
                                    progress_callback=lambda _m: None,
                                    password="pw", captcha_callback=lambda _i: "1234")

    assert calls["blocking_state"] >= 2, "必须既有取锁前的检查，也有锁内的重读"
    assert calls["posts"] == 0, "锁内重读发现记录后不能发 POST"
    assert result["status"] == "blocked_by_uncertain"


# ======================================================================
# R04：核对证据与处置原子校验
# ======================================================================

def test_r04_guard_runs_inside_the_data_lock(tmp_path):
    """guard 拿到的必须是**即将被写入的那份数据**，且在锁内执行。"""
    from app.sss_journal import guarded_close_records

    state = tmp_path / "sss_uncertain.json"
    key = batch_key("2026-09-12", "", "1")
    append_records(state, key, [{"identifier": "a", "fingerprint": {}}],
                   meta={"account": "1", "platform": "https://x"})

    seen: list[int] = []

    def guard(payload):
        seen.append(len(payload.get("records", [])))
        # 锁内再写一次会自锁（数据锁不可重入跨实例），所以这里只读。
        return True, "", ""

    closed, code, message = guarded_close_records(
        state, key, ["a"], status="resolved", reason="", guard=guard)
    assert closed == 1 and code == ""
    assert seen == [1]


def test_r04_guard_rejection_writes_nothing(tmp_path):
    from app.sss_journal import guarded_close_records

    state = tmp_path / "sss_uncertain.json"
    key = batch_key("2026-09-12", "", "1")
    append_records(state, key, [{"identifier": "a", "fingerprint": {}}],
                   meta={"account": "1", "platform": "https://x"})
    before = pathlib.Path(state).read_text(encoding="utf-8")

    closed, code, message = guarded_close_records(
        state, key, ["a"], status="resolved", reason="",
        guard=lambda payload: (False, "review_stale", "证据已过期"))

    assert closed == 0 and code == "review_stale"
    assert pathlib.Path(state).read_text(encoding="utf-8") == before


def test_r04_resolve_is_in_the_operation_coordinator(tmp_path):
    """人工处置必须占协调器槽位：与下单/上传互斥。"""
    from app.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    operation, _conflict = bridge._reserve("order", title="订单处理")
    try:
        got = bridge.sss_uncertain_resolve({
            "decision": "keep", "confirm": "keep", "note": "先不动",
            "record_ids": ["a"]})
        assert got["ok"] is False and got["code"] == "operation_conflict"
        assert got["cloud_write"] is False
    finally:
        bridge._operations.finish(operation, status="success")


# ======================================================================
# R05：查站完整性 / 邻日 / 歧义
# ======================================================================

def _review_server(records):
    """站内列表替身：第一页返回给定记录，第二页起为空（分页自然结束）。"""
    calls = {"n": 0}

    def fetch(path):
        calls["n"] += 1
        if calls["n"] > 1:
            return {"success": True, "result": {"records": [], "total": len(records)}}
        return {"success": True, "result": {"records": records,
                                            "total": len(records)}}
    return fetch


def _station_record(name="张", phone="13800000000", when="2026-09-12 11:00:00",
                    address="浙江农林大学东湖校区A1"):
    return {"recipientName": name, "recipientPhone": [phone],
            "recipientAddress": address, "expectedDeliveryTime": when, "status": 2}


@pytest.fixture
def review_journal(tmp_path):
    from app.sss_journal import append_records, batch_key

    state = tmp_path / "sss_uncertain.json"
    append_records(state, batch_key("2026-09-12", "", "18758187837"),
                   [{"identifier": "第 3 行 张", "sheet": "午餐", "batch_id": "b",
                     "fingerprint": {"receive_name": "张",
                                     "receive_phone": "13800000000",
                                     "door_num": "A1",
                                     "address_detail": "浙江农林大学东湖校区",
                                     "expected_delivery_time": "2026-09-12 11:00:00"}}],
                   meta={"account": "18758187837", "platform": "https://sss.example.com",
                         "delivery_date": "2026-09-12"})
    return state


def _review(state, fetch):
    from app.sss_review import start_review

    return start_review(state, delivery_date="2026-09-12", account="18758187837",
                        origin="https://sss.example.com", fetch_json=fetch)


def test_r05_clean_absence_is_still_station_missing(review_journal):
    """反向锁：站内确实没有时必须给 station_missing（不能"一律阻断"假修复）。"""
    got = _review(review_journal, _review_server([]))
    assert got["counts"] == {"station_missing": 1}, got["results"]


def test_r05_off_by_one_day_is_not_missing(review_journal):
    got = _review(review_journal, _review_server(
        [_station_record(when="2026-09-13 11:00:00")]))
    assert got["results"][0]["classification"] == "station_found_other_day"
    assert "2026-09-13" in got["results"][0]["reason"]


def test_r05_conflicting_address_is_not_missing(review_journal):
    got = _review(review_journal, _review_server(
        [_station_record(address="某个别的地方X9")]))
    assert got["results"][0]["classification"] == "station_found_other_day"


def test_r05_duplicate_page_is_scan_failed(review_journal):
    """分页不可信（重复页）时必须 scan_failed，不能给出"站内没有"。"""
    def dup_fetch(path):
        return {"success": True,
                "result": {"records": [_station_record()] * 100, "total": 10 ** 6}}

    got = _review(review_journal, dup_fetch)
    assert got["results"][0]["classification"] == "scan_failed"
    assert "分页不可信" in got["results"][0]["reason"]


def test_r05_read_failure_is_scan_failed(review_journal):
    def boom(path):
        raise RuntimeError("500")

    got = _review(review_journal, boom)
    assert got["results"][0]["classification"] == "scan_failed"
    assert "无法判定" in got["results"][0]["reason"]


def test_r05_scan_failed_keeps_the_gate_closed(review_journal):
    from app.sss_review import resolve_records

    def boom(path):
        raise RuntimeError("500")

    _review(review_journal, boom)
    record_id = load_journal(review_journal)["records"][0]["journal_id"]
    got = resolve_records(review_journal, delivery_date="2026-09-12",
                          account="18758187837", origin="https://sss.example.com",
                          decision="station_absent", record_ids=[record_id],
                          confirm="station_absent", note="读取失败但我认为没有")
    assert got["ok"] is False and got["code"] == "station_not_absent"
    assert blocking_state(review_journal, delivery_date="2026-09-12",
                          account="18758187837",
                          origin="https://sss.example.com")["blocked"] is True


# ======================================================================
# R06：六表同批
# ======================================================================

def test_r06_second_sheet_is_not_rejected_by_the_batch_own_write(tmp_path):
    """本批第一张表记账后，第二张表不能被自己的记账判成 stale。"""
    ledger = SyncLedger(tmp_path / "state.json")
    journal = SyncJournal(journal_path_for(ledger.path))
    cli = _MultiSheetCli(_six_table_fixture())
    tables = {sheet: {"file_id": f"F{i}"} for i, sheet in enumerate(SIX_SHEETS)}
    local = {sheet: [CloudOrder(sheet, f"老人{i}", "小", f"111{i}", "中餐", "经济",
                                6, row=3, rows=(3,))]
             for i, sheet in enumerate(SIX_SHEETS)}

    plans = build_plan(cli, local_orders=local, tables=tables,
                       target=dt.date(2026, 9, 11), ledger=ledger,
                       sort_enabled=False)
    result = apply_plan(cli, plans, ledger=ledger, marker_enabled=False,
                        journal=journal)
    assert [item["status"] for item in result["sheets"]] == ["ok"] * 6


def test_r06_external_ledger_change_still_blocks(tmp_path):
    """本批合法的记账要放行，**外部**改动仍然必须挡住。"""
    ledger = SyncLedger(tmp_path / "state.json")
    journal = SyncJournal(journal_path_for(ledger.path))
    cli = _MultiSheetCli(_six_table_fixture())
    tables = {sheet: {"file_id": f"F{i}"} for i, sheet in enumerate(SIX_SHEETS)}
    local = {sheet: [CloudOrder(sheet, f"老人{i}", "小", f"111{i}", "中餐", "经济",
                                6, row=3, rows=(3,))]
             for i, sheet in enumerate(SIX_SHEETS)}
    plans = build_plan(cli, local_orders=local, tables=tables,
                       target=dt.date(2026, 9, 11), ledger=ledger,
                       sort_enabled=False)

    # 模拟"计划构建之后，另一个进程改了账本"
    ledger.merge_entries("2026-09-11", "别的表", {"别人\u00002": {"slots": [1]}})

    result = apply_plan(cli, plans, ledger=ledger, marker_enabled=False,
                        journal=journal)
    assert all(item["status"] == "stale_batch" for item in result["sheets"])
    assert not cli.writes and not cli.inserts


def test_r06_plans_with_mixed_baselines_are_rejected_whole(tmp_path):
    """来自两次不同预览的计划不能混在一批里执行。"""
    ledger = SyncLedger(tmp_path / "state.json")
    journal = SyncJournal(journal_path_for(ledger.path))
    cli = _MultiSheetCli(_six_table_fixture())
    tables = {sheet: {"file_id": f"F{i}"} for i, sheet in enumerate(SIX_SHEETS[:2])}
    local = {sheet: [CloudOrder(sheet, f"老人{i}", "小", f"111{i}", "中餐", "经济",
                                6, row=3, rows=(3,))]
             for i, sheet in enumerate(SIX_SHEETS[:2])}
    first = build_plan(cli, local_orders=local, tables=tables,
                       target=dt.date(2026, 9, 11), ledger=ledger,
                       sort_enabled=False)
    ledger.merge_entries("2026-09-11", "别的表", {"别人\u00002": {"slots": [1]}})
    second = build_plan(cli, local_orders=local, tables=tables,
                        target=dt.date(2026, 9, 11), ledger=ledger,
                        sort_enabled=False)

    mixed = [first[0], second[1]]
    result = apply_plan(cli, mixed, ledger=ledger, marker_enabled=False,
                        journal=journal)
    assert all(item["status"] == "stale_batch" for item in result["sheets"])
    assert "基线不一致" in result["sheets"][0]["reason"]
    assert not cli.writes


# ======================================================================
# R09：本地文件字节快照（哈希与解析同源）
# ======================================================================

def test_r09_reader_parses_from_the_same_bytes(tmp_path):
    """读取器必须先整份读入再解析：改文件后旧字节仍能读出旧内容。"""
    from openpyxl import Workbook

    from app.wps_cloud import read_local_orders, read_local_orders_from_bytes

    path = tmp_path / "排单.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "东湖中餐"
    ws.cell(2, 2, "姓名")
    ws.cell(3, 2, "老人")
    ws.cell(3, 3, "小")
    ws.cell(3, 4, "13800000000")
    ws.cell(3, 14, 6)
    wb.save(path)

    snapshot = path.read_bytes()
    orders = read_local_orders_from_bytes(snapshot, sheets=("东湖中餐",))
    assert [order.name for order in orders["东湖中餐"]] == ["老人"]

    # 文件被替换后：旧字节仍然解析出旧内容（证明解析不依赖路径）
    ws.cell(3, 2, "新人")
    wb.save(path)
    still_old = read_local_orders_from_bytes(snapshot, sheets=("东湖中餐",))
    assert [order.name for order in still_old["东湖中餐"]] == ["老人"]
    assert [order.name for order in
            read_local_orders(path, sheets=("东湖中餐",))["东湖中餐"]] == ["新人"]


def test_r09_upload_uses_one_byte_snapshot(tmp_path, monkeypatch):
    """上传时哈希与解析必须是同一份字节：解析期间替换文件也不能产生混合计划。"""
    from app import bridge as bridge_module
    from app.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._config.wps_enabled = True
    excel = tmp_path / "排单.xlsx"
    _write_workbook(excel)
    bridge._config.excel_path = excel

    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "F1"}})
    monkeypatch.setattr(bridge_module, "build_plan", lambda cli, **kw: [])
    monkeypatch.setattr(bridge_module, "format_plan", lambda plans: "")
    monkeypatch.setattr(bridge_module, "summarize_plan", lambda plans: {})

    seen: list[bytes] = []

    def reader(data, log=None):
        seen.append(data)
        return {"东湖中餐": []}

    monkeypatch.setattr(bridge_module, "read_local_orders_from_bytes", reader)

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
    original = seen[-1]
    # 解析期间（在 reader 内部）替换文件：解析用的仍然是那一次读到的字节
    _write_workbook(excel, name="篡改后的人")
    got = bridge.wps_upload(preview["preview_id"])
    assert got["code"] == "preview_changed", got.get("reason")
    assert calls == []
    assert seen[-1] == original, "上传必须复用同一次读取的字节"


# ======================================================================
# R10：插入异常与回滚证明
# ======================================================================

def test_r10_insert_succeeded_then_client_error_is_uncertain(tmp_path):
    """服务端已插入、客户端只拿到异常 → 不能报"未写入"，必须 uncertain。"""
    ledger = SyncLedger(tmp_path / "state.json")
    journal = SyncJournal(journal_path_for(ledger.path))

    class HalfBrokenCli(FakeCli):
        """insert_rows 真的插了行，然后才抛错（模拟响应超时）。"""

        def insert_rows(self, file_id, worksheet_id, *, row, count):
            super().insert_rows(file_id, worksheet_id, row=row, count=count)
            raise WpsCloudError("响应超时")

    cli = HalfBrokenCli(make_grid(BASE_HEADER, [{0: "老人", 1: "小", 2: "111",
                                                7: "5"}]))
    orders = [CloudOrder("东湖中餐", "新人", "小", "999", "中餐", "经济", 1,
                         row=3, rows=(3,))]
    plans = build_plan(cli, local_orders={"东湖中餐": orders},
                       tables={"东湖中餐": {"file_id": "F1"}},
                       target=dt.date(2026, 9, 11), ledger=ledger,
                       sort_enabled=False)
    result = apply_plan(cli, plans, ledger=ledger, marker_enabled=False,
                        journal=journal)

    sheet = result["sheets"][0]
    assert sheet["status"] == "failed"
    assert sheet.get("uncertain") is True, sheet
    assert sheet.get("cloud_writes") is None, "不能声明零写入"
    assert result["proven_no_write"] is False, "写入结果未知时不能声明零写入"


def test_r10_rollback_refuses_when_the_block_was_changed(tmp_path):
    """待删除区间里出现别人的行 → 绝不删，按未知处置。"""
    ledger = SyncLedger(tmp_path / "state.json")
    journal = SyncJournal(journal_path_for(ledger.path))

    class CollaboratorCli(FakeCli):
        """插入后协作者又往同一区间插了一行（模拟协作编辑）。"""

        def insert_rows(self, file_id, worksheet_id, *, row, count):
            super().insert_rows(file_id, worksheet_id, row=row, count=count)
            # 协作者的行混进我们即将删除的区间
            self.grid[(row, 0)] = "协作者"

        def write_cells(self, file_id, worksheet_id, cells):
            if any(str(cell.get("value") or "") == "新人" for cell in cells):
                raise WpsCloudError("写入失败")
            super().write_cells(file_id, worksheet_id, cells)

    cli = CollaboratorCli(make_grid(BASE_HEADER, [{0: "老人", 1: "小", 2: "111",
                                                  7: "5"}]))
    orders = [CloudOrder("东湖中餐", "新人", "小", "999", "中餐", "经济", 1,
                         row=3, rows=(3,))]
    plans = build_plan(cli, local_orders={"东湖中餐": orders},
                       tables={"东湖中餐": {"file_id": "F1"}},
                       target=dt.date(2026, 9, 11), ledger=ledger,
                       sort_enabled=False)
    result = apply_plan(cli, plans, ledger=ledger, marker_enabled=False,
                        journal=journal)

    names = {str(value) for value in cli.grid.values()}
    assert "协作者" in names, "不能删掉协作者的行"
    assert result["sheets"][0].get("uncertain") is True
    assert result["proven_no_write"] is False


def test_r10_rolled_back_sheet_is_not_proven_no_write(tmp_path):
    """"写过后完整回滚"必须与"从未写入"区分：不能声明 proven_no_write。"""
    ledger = SyncLedger(tmp_path / "state.json")
    journal = SyncJournal(journal_path_for(ledger.path))

    class BrokenAfterInsert(FakeCli):
        """插入成功，随后写姓名列失败（回滚可以证明成功）。"""

        def write_cells(self, file_id, worksheet_id, cells):
            if any(str(cell.get("value") or "") == "新人" for cell in cells):
                raise WpsCloudError("写入失败")
            super().write_cells(file_id, worksheet_id, cells)

    cli = BrokenAfterInsert(make_grid(BASE_HEADER, [{0: "老人", 1: "小", 2: "111",
                                                    7: "5"}]))
    orders = [CloudOrder("东湖中餐", "新人", "小", "999", "中餐", "经济", 1,
                         row=3, rows=(3,))]
    plans = build_plan(cli, local_orders={"东湖中餐": orders},
                       tables={"东湖中餐": {"file_id": "F1"}},
                       target=dt.date(2026, 9, 11), ledger=ledger,
                       sort_enabled=False)
    result = apply_plan(cli, plans, ledger=ledger, marker_enabled=False,
                        journal=journal)

    sheet = result["sheets"][0]
    assert sheet.get("rolled_back") is True, sheet
    assert result["proven_no_write"] is False, (
        "写进去过再删回来 ≠ 从未写入，不能声明 proven_no_write")


# ======================================================================
# R07 / R12 的补充锁（跨进程部分在 test_process_and_crash.py）
# ======================================================================

def test_r07_recovery_reloads_authoritative_state_inside_the_lock(tmp_path):
    """处置必须基于磁盘上的最新状态：内存里那份过时对象不作数。"""
    import datetime as _dt

    ledger = SyncLedger(tmp_path / "state.json")
    journal = SyncJournal(journal_path_for(ledger.path))
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]),
                  corrupt_write=True)
    plans = build_plan(cli, local_orders={"东湖中餐": [
        CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6, row=3, rows=(3,))]},
        tables={"东湖中餐": {"file_id": "F1"}}, target=_dt.date(2026, 9, 11),
        ledger=ledger)
    result = apply_plan(cli, plans, ledger=ledger, marker_enabled=False,
                        journal=journal)
    operation_id = result["operation_id"]

    # 用一份"过时的内存对象"去处置：磁盘上已经被另一个进程 retire 过
    stale_journal = SyncJournal(journal_path_for(ledger.path))
    fresh = SyncJournal(journal_path_for(ledger.path))
    fresh.set_sheet_status(operation_id, "东湖中餐", "retired_guarded",
                           retired_guarded=True, note="另一个进程已处置")
    fresh.save()

    got = recovery_module.resolve_pending_operation(
        operation_id, "retire_guarded", confirm="retire_guarded",
        note="本机也来处置一次", confirm_structure_checked=True,
        ledger=ledger, journal=stale_journal)

    assert got["ok"] is True and got["reason_code"] == "already_retired"
    assert got["changed"] is False, "重复处置不能再次改动状态"


def test_r12_recovery_status_target_ref_hides_the_file_id(tmp_path):
    """恢复状态里不出现原始云表 file_id（只给不可逆的 target_ref）。"""
    import datetime as _dt

    ledger = SyncLedger(tmp_path / "state.json")
    journal = SyncJournal(journal_path_for(ledger.path))
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]),
                  corrupt_write=True)
    plans = build_plan(cli, local_orders={"东湖中餐": [
        CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6, row=3, rows=(3,))]},
        tables={"东湖中餐": {"file_id": "SecretFileId123"}},
        target=_dt.date(2026, 9, 11), ledger=ledger)
    apply_plan(cli, plans, ledger=ledger, marker_enabled=False, journal=journal)

    status = recovery_module.recovery_status(
        ledger=ledger, journal=SyncJournal(journal_path_for(ledger.path)))
    blob = json.dumps(status, ensure_ascii=False)
    assert "SecretFileId123" not in blob
    assert "wps-target:" in blob


# ======================================================================
# 小工具
# ======================================================================

def _write_workbook(path: pathlib.Path, *, name: str = "老人") -> pathlib.Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "东湖中餐"
    ws.cell(2, 2, "姓名")
    ws.cell(3, 2, name)
    ws.cell(3, 3, "小")
    ws.cell(3, 4, "13800000000")
    ws.cell(3, 14, 6)
    wb.save(path)
    return path


def _excel_config(tmp_path, state_path):
    from app.config import AppConfig

    excel = tmp_path / "闪时送.xlsx"
    workbook = __import__("openpyxl").Workbook()
    sheet = workbook.active
    sheet.title = "午餐"
    sheet.cell(2, 1, "姓名")
    sheet.cell(3, 1, "客户0")
    sheet.cell(3, 2, "A1")
    sheet.cell(3, 3, "13800000000")
    workbook.save(excel)

    cfg = AppConfig(config_path=str(tmp_path / "config.json"))
    cfg.sss_order_source = "excel"
    cfg.sss_excel_path = excel
    cfg.sss_account = "18758187837"
    cfg.sss_url = "https://sss.example.com/takeout"
    cfg.sss_dry_run = False
    cfg.sss_uncertain_path = str(state_path)
    return cfg
