"""R01–R12 修复的回归锁：每条对应文档 §0.3 的一个编号。

放在一起的原因：这些编号是验收报告要逐条回答的对象，测试与缺陷一一对应，
改名或删除就会让报告里的"已修复"失去证据。

R02/R04/R05 三条锁定的是**闪时送未决日志**（锁内重读、核对证据原子校验、
查站完整性）；该机制已于 2026-10-12 整体移除，对应的回归锁随之删除，
历史结论见发布记录与 README。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import threading

import pytest

from app import wps_recovery as recovery_module
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
