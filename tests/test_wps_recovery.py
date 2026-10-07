"""WPS 意图日志 + 部分失败恢复：写入态、防重复闸门、只读核对与人工处置。

这些是**安全属性**测试，覆盖：

* ``apply_plan`` 在任何写入之前把意图落盘，逐表推进状态；
* 批次日期闸门被拒的表记 ``not_started``，零云端写请求；
* ``retire_guarded`` 只退出待处理队列，**同日期 + 同云表仍然拒绝重放**；
* 预览之后账本被别的动作改过 → 拒绝写入（stale）；
* ``cloud_verified`` / ``cloud_untouched`` 必须由实际只读核对证明，证明不了保持阻断；
* 恢复入口永不写云端；重复处置幂等。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from app import wps_recovery as rec
from app.wps_cloud import CloudOrder, SyncLedger, build_plan
from app.wps_cloud import apply_plan as cloud_apply_plan
from app.wps_journal import SyncJournal, journal_path_for
from tests.test_wps_cloud import BASE_HEADER, FakeCli, make_grid


TARGET = dt.date(2026, 9, 11)


def _orders(*specs):
    return [CloudOrder("东湖中餐", name, "小", phone, "中餐", "经济", meals,
                       row=row, rows=(row,))
            for name, phone, meals, row in specs]


def _plan(cli, orders, *, ledger=None, target=TARGET):
    return build_plan(cli, local_orders={"东湖中餐": orders},
                      tables={"东湖中餐": {"file_id": "F1"}},
                      target=target, ledger=ledger)[0]


@pytest.fixture
def env(tmp_path: Path):
    """临时账本 + 同目录意图日志（绝不碰真实用户状态）。"""
    ledger = SyncLedger(tmp_path / "state.json")
    journal = SyncJournal(journal_path_for(ledger.path))
    return ledger, journal


# ----------------------------------------------------------------------
# 写入态：意图先落盘、成功后结案
# ----------------------------------------------------------------------

def test_successful_apply_records_verified_journal(env):
    ledger, journal = env
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    plans = [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)]

    result = cloud_apply_plan(cli, plans, ledger=ledger, marker_enabled=False,
                              journal=journal)

    operation_id = result["operation_id"]
    assert operation_id.startswith("wps-")
    saved = SyncJournal(journal_path_for(ledger.path))
    op = saved.get_operation(operation_id)
    assert op is not None and op["status"] == "verified"
    sheet = op["sheets"]["东湖中餐"]
    assert sheet["status"] == "verified"
    assert sheet["evidence"] == "executor_cloud_readback"
    assert sheet["cloud_checked"] is True
    assert sheet["ledger_entries"]["张\u0000111"]["slots"] == [6]
    assert saved.pending_operations() == {}, "全部 verified 后不该留在待处理队列"


def test_blocked_sheet_is_recorded_not_started(env):
    """批次日期闸门拒绝的表：journal 记 not_started，云端零请求。"""
    ledger, journal = env
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6, row=3,
                         rows=(3,), weekday_marks=("周五",))]
    plans = build_plan(cli, local_orders={"东湖中餐": orders},
                       tables={"东湖中餐": {"file_id": "F1"}},
                       target=dt.date(2026, 9, 12), ledger=ledger)
    reads_before = len(cli.reads)

    result = cloud_apply_plan(cli, plans, ledger=ledger, marker_enabled=False,
                              journal=journal)

    assert result["sheets"][0]["status"] == "blocked"
    assert len(cli.reads) == reads_before, "被拒绝的表连只读请求都不该发"
    op = SyncJournal(journal_path_for(ledger.path)).get_operation(
        result["operation_id"])
    assert op["sheets"]["东湖中餐"]["status"] == "not_started"
    assert op["status"] == "not_started"


def test_failed_write_before_any_write_is_failed_no_write(env):
    """插入行失败（还没写任何格子）→ failed_no_write，可证明零写入。"""
    ledger, journal = env
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]),
                  fail_write=True)
    plans = [_plan(cli, _orders(("新人", "999", 1, 3)), ledger=ledger)]

    result = cloud_apply_plan(cli, plans, ledger=ledger, marker_enabled=False,
                              journal=journal)

    assert result["failed"] == 1
    op = SyncJournal(journal_path_for(ledger.path)).get_operation(
        result["operation_id"])
    sheet = op["sheets"]["东湖中餐"]
    assert sheet["status"] == "failed_no_write", (
        "插入行已回滚 + 只读核对证明云端与基线一致 = 可证明零写入")
    assert sheet["cloud_checked"] is True
    assert sheet["evidence"] == "executor_cloud_readback"


def test_verify_failure_is_uncertain_and_has_evidence(env):
    """接口假装成功但内容没变：必须记 uncertain（不是 verified，也不是零写入）。"""
    ledger, journal = env
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]),
                  corrupt_write=True)
    plans = [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)]

    result = cloud_apply_plan(cli, plans, ledger=ledger, marker_enabled=False,
                              journal=journal)

    assert result["sheets"][0]["status"] == "verify_failed"
    saved = SyncJournal(journal_path_for(ledger.path))
    op = saved.get_operation(result["operation_id"])
    assert op["sheets"]["东湖中餐"]["status"] == "uncertain"
    assert result["operation_id"] in saved.pending_operations(), (
        "不确定的表要留在待处理队列")


# ----------------------------------------------------------------------
# 防重复闸门：retired_guarded 不能变成"可以自动重传"
# ----------------------------------------------------------------------

def test_retire_guarded_still_blocks_the_same_batch(env):
    """退出待处理队列后，同日期 + 同云表的再次上传仍然被拒绝。"""
    ledger, journal = env
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]),
                  corrupt_write=True)
    plans = [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)]
    first = cloud_apply_plan(cli, plans, ledger=ledger, marker_enabled=False,
                             journal=journal)
    operation_id = first["operation_id"]

    retired = rec.resolve_pending_operation(
        operation_id, "retire_guarded", confirm="retire_guarded",
        note="已人工核对云端表结构", confirm_structure_checked=True,
        ledger=ledger, journal=journal)
    assert retired["ok"] is True and retired["changed"] is True
    assert retired["cloud_write"] is False

    saved = SyncJournal(journal_path_for(ledger.path))
    assert operation_id not in saved.pending_operations()
    assert saved.has_guard(TARGET.isoformat(), "F1") is True

    # 同批次再次上传：必须在任何写请求之前被拒绝
    cli2 = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    plans2 = [_plan(cli2, _orders(("张", "111", 6, 3)), ledger=ledger)]
    again = cloud_apply_plan(cli2, plans2, ledger=ledger, marker_enabled=False,
                             journal=SyncJournal(journal_path_for(ledger.path)))
    assert again["sheets"][0]["status"] == "stale_batch"
    assert not cli2.writes and not cli2.inserts, "有闸门时一个云端写请求都不该发"


def test_retire_guarded_requires_confirmation_and_note(env):
    ledger, journal = env
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]),
                  corrupt_write=True)
    result = cloud_apply_plan(
        cli, [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)],
        ledger=ledger, marker_enabled=False, journal=journal)
    op_id = result["operation_id"]

    no_confirm = rec.resolve_pending_operation(
        op_id, "retire_guarded", confirm="retire_guarded", note="已核对表结构",
        ledger=ledger, journal=journal)
    assert no_confirm["ok"] is False
    assert no_confirm["code"] == "manual_confirmation_required"

    short_note = rec.resolve_pending_operation(
        op_id, "retire_guarded", confirm="retire_guarded", note="嗯",
        confirm_structure_checked=True, ledger=ledger, journal=journal)
    assert short_note["ok"] is False and short_note["code"] == "note_too_short"

    mismatch = rec.resolve_pending_operation(
        op_id, "retire_guarded", confirm="cloud_untouched", note="已核对表结构",
        confirm_structure_checked=True, ledger=ledger, journal=journal)
    assert mismatch["ok"] is False and mismatch["code"] == "confirm_mismatch"

    bad_decision = rec.resolve_pending_operation(
        op_id, "whatever", confirm="whatever", note="已核对表结构",
        ledger=ledger, journal=journal)
    assert bad_decision["ok"] is False and bad_decision["code"] == "decision_invalid"


def test_retire_guarded_is_idempotent(env):
    ledger, journal = env
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]),
                  corrupt_write=True)
    result = cloud_apply_plan(
        cli, [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)],
        ledger=ledger, marker_enabled=False, journal=journal)
    op_id = result["operation_id"]
    kwargs = dict(confirm="retire_guarded", note="已人工核对云端表结构",
                  confirm_structure_checked=True, ledger=ledger, journal=journal)
    first = rec.resolve_pending_operation(op_id, "retire_guarded", **kwargs)
    second = rec.resolve_pending_operation(op_id, "retire_guarded", **kwargs)
    assert first["ok"] and second["ok"]
    assert second["reason_code"] == "already_retired"
    assert second["changed"] is False, "重复提交不能再次改动状态"


def test_stale_ledger_digest_blocks_the_upload(env):
    """预览之后账本被别的动作改过 → 拒绝写入（避免重复加餐）。"""
    ledger, journal = env
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    plans = [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)]
    # 模拟"预览之后另一个窗口上传过"：账本摘要变了
    ledger.merge_entries(TARGET.isoformat(), "F1", {"别人\u00002": {"slots": [1]}})

    result = cloud_apply_plan(cli, plans, ledger=ledger, marker_enabled=False,
                              journal=journal)

    assert result["sheets"][0]["status"] == "stale_batch"
    assert not cli.writes and not cli.inserts


# ----------------------------------------------------------------------
# 只读核对分类
# ----------------------------------------------------------------------

class RecordingCli(FakeCli):
    """只读核对用的替身：额外记录 read_formulas 调用。"""

    def read_formulas(self, file_id, worksheet_id, row_from, row_to, col_from, col_to):
        self.reads.append(("read_formulas", row_from, row_to, col_from, col_to))
        return super().read_formulas(file_id, worksheet_id, row_from, row_to,
                                    col_from, col_to)


def _journal_record(cli, orders, *, corrupt=False, ledger=None):
    cli.corrupt_write = corrupt
    result = cloud_apply_plan(
        cli, [_plan(cli, orders, ledger=ledger)], ledger=ledger,
        marker_enabled=False, journal=SyncJournal(None))
    return result


def test_classify_proves_verified_after_complete_write(env):
    ledger, journal = env
    cli = RecordingCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    result = cloud_apply_plan(
        cli, [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)],
        ledger=ledger, marker_enabled=False, journal=journal)
    op = journal.get_operation(result["operation_id"])
    record = op["sheets"]["东湖中餐"]
    assert rec.classify_journal_sheet(cli, record)["state"] == "verified"


def test_classify_proves_not_started_when_cloud_is_untouched(env):
    """把日志记录的状态改回 writing（模拟崩溃在半途），云端仍是基线。"""
    ledger, journal = env
    cli = RecordingCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    plans = [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)]
    # 让写入整体失败：用 fail_write，但先在 writing 之前把云端冻结
    cli.corrupt_write = True
    result = cloud_apply_plan(cli, plans, ledger=ledger, marker_enabled=False,
                              journal=journal)
    op = journal.get_operation(result["operation_id"])
    record = op["sheets"]["东湖中餐"]
    # 现状：corrupt_write 假装成功但云端没变 → 既非完整期望值也非完整原值？其实
    # 云端与基线完全一致，因此可证明 not_started。
    assert rec.classify_journal_sheet(cli, record)["state"] == "not_started"


def test_classify_is_uncertain_when_cloud_unreadable(env):
    """读不出来绝不能当成"没写过"。"""
    ledger, journal = env
    cli = RecordingCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    result = cloud_apply_plan(
        cli, [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)],
        ledger=ledger, marker_enabled=False, journal=journal)
    record = journal.get_operation(result["operation_id"])["sheets"]["东湖中餐"]

    class Broken(FakeCli):
        def read_grid(self, *_a, **_k):
            from app.wps_cloud import WpsCloudError
            raise WpsCloudError("接口挂了")

    classified = rec.classify_journal_sheet(Broken({}), record)
    assert classified["state"] == "uncertain"
    assert "cloud_unreadable" in classified["reason"]


# ----------------------------------------------------------------------
# 人工处置：cloud_verified / cloud_untouched 必须靠只读云端证明
# ----------------------------------------------------------------------

def test_cloud_verified_merges_ledger_and_clears(env):
    ledger, journal = env
    cli = RecordingCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    result = cloud_apply_plan(
        cli, [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)],
        ledger=ledger, marker_enabled=False, journal=journal)
    op_id = result["operation_id"]
    # 把状态人为退回 writing（模拟"程序在写完之后崩了，账本没记上"）
    journal.set_sheet_status(op_id, "东湖中餐", "writing", worksheet_id=1)
    journal.save()
    ledger.merge_entries(TARGET.isoformat(), "F1", {})   # 确保磁盘账本在

    resolved = rec.resolve_pending_operation(
        op_id, "cloud_verified", confirm="cloud_verified",
        note="已逐格核对云端，全部一致", ledger=ledger,
        journal=SyncJournal(journal_path_for(ledger.path)), cli=cli)

    assert resolved["ok"] is True and resolved["status"] == "resolved"
    assert resolved["cloud_write"] is False
    saved = SyncJournal(journal_path_for(ledger.path))
    assert saved.get_operation(op_id)["sheets"]["东湖中餐"]["status"] == "verified"
    assert saved.get_operation(op_id)["sheets"]["东湖中餐"]["cloud_checked"] is True
    assert ledger.synced_slots(TARGET.isoformat(), "F1", "张", "111") == [6]


def test_cloud_verified_refuses_when_cloud_does_not_match(env):
    """云端对不上（表头日期不符）时不能靠人工勾选放行。"""
    ledger, journal = env
    cli = RecordingCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    result = cloud_apply_plan(
        cli, [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)],
        ledger=ledger, marker_enabled=False, journal=journal)
    op_id = result["operation_id"]
    journal.set_sheet_status(op_id, "东湖中餐", "writing", worksheet_id=1)
    journal.save()

    # 目标日期列被协作者改成了别的日期
    broken = FakeCli(make_grid({**BASE_HEADER, 4: "9.12 周六"},
                               [{0: "张", 2: "111", 7: "3"}]))
    resolved = rec.resolve_pending_operation(
        op_id, "cloud_verified", confirm="cloud_verified",
        note="已逐格核对云端，全部一致", ledger=ledger,
        journal=SyncJournal(journal_path_for(ledger.path)), cli=broken)

    assert resolved["ok"] is False and resolved["status"] == "uncertain"
    assert resolved["operations"][0]["reason"] == "cloud_verify_failed"
    # 核对不通过就不能结案：记录必须留在待处理队列里继续阻断
    saved = SyncJournal(journal_path_for(ledger.path))
    assert saved.get_operation(op_id)["sheets"]["东湖中餐"]["status"] == "writing"
    assert op_id in saved.pending_operations()


def test_cloud_untouched_clears_only_with_evidence(env):
    ledger, journal = env
    cli = RecordingCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    plans = [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)]
    cli.corrupt_write = True
    result = cloud_apply_plan(cli, plans, ledger=ledger, marker_enabled=False,
                              journal=journal)
    op_id = result["operation_id"]
    journal.set_sheet_status(op_id, "东湖中餐", "writing", worksheet_id=1)
    journal.save()

    resolved = rec.resolve_pending_operation(
        op_id, "cloud_untouched", confirm="cloud_untouched",
        note="已确认云端没有任何本批痕迹", ledger=ledger,
        journal=SyncJournal(journal_path_for(ledger.path)), cli=cli)

    assert resolved["ok"] is True
    saved = SyncJournal(journal_path_for(ledger.path))
    assert saved.get_operation(op_id)["sheets"]["东湖中餐"]["status"] == "not_started"
    assert saved.has_guard(TARGET.isoformat(), "F1") is False


def test_resolve_without_cli_never_clears(env):
    ledger, journal = env
    cli = RecordingCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]),
                       corrupt_write=True)
    result = cloud_apply_plan(
        cli, [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)],
        ledger=ledger, marker_enabled=False, journal=journal)
    op_id = result["operation_id"]

    resolved = rec.resolve_pending_operation(
        op_id, "cloud_untouched", confirm="cloud_untouched", note="我认为没写过",
        ledger=ledger, journal=SyncJournal(journal_path_for(ledger.path)), cli=None)

    assert resolved["ok"] is False
    assert resolved["operations"][0]["reason"] == "cli_required"


def test_keep_notes_but_keeps_blocking(env):
    ledger, journal = env
    cli = RecordingCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]),
                       corrupt_write=True)
    result = cloud_apply_plan(
        cli, [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)],
        ledger=ledger, marker_enabled=False, journal=journal)
    op_id = result["operation_id"]

    kept = rec.resolve_pending_operation(
        op_id, "keep", confirm="keep", note="先保持阻断，明天人工看",
        ledger=ledger, journal=SyncJournal(journal_path_for(ledger.path)))

    assert kept["ok"] is True and kept["status"] == "uncertain"
    saved = SyncJournal(journal_path_for(ledger.path))
    assert op_id in saved.pending_operations(), "keep 不能解除阻断"
    assert saved.get_operation(op_id)["sheets"]["东湖中餐"]["reason"].startswith("先保持阻断")


# ----------------------------------------------------------------------
# 只读状态
# ----------------------------------------------------------------------

def test_recovery_status_reports_pending_and_actions(env):
    ledger, journal = env
    cli = RecordingCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]),
                       corrupt_write=True)
    result = cloud_apply_plan(
        cli, [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)],
        ledger=ledger, marker_enabled=False, journal=journal)

    status = rec.recovery_status(
        ledger, journal=SyncJournal(journal_path_for(ledger.path)))

    assert status["ok"] is True and status["read_only"] is True
    assert status["queried_cloud"] is False
    assert status["pending_count"] == 1
    op = status["pending_operations"][0]
    assert op["operation_id"] == result["operation_id"]
    sheet = op["sheets"][0]
    assert sheet["status"] == "uncertain"
    assert "cloud_verified" in sheet["allowed_actions"]
    assert "retire_guarded" in sheet["allowed_actions"]
    assert sheet["people"][0]["name"] == "张"
    assert sheet["people"][0]["total_before"] == 3
    assert sheet["people"][0]["total_after"] == 9


def test_recovery_status_fails_closed_on_corrupt_journal(env):
    """日志损坏时返回明确错误，绝不返回"没有未完成操作"的空摘要。"""
    ledger, journal = env
    Path(journal_path_for(ledger.path)).write_text("{ 不是 json", encoding="utf-8")

    status = rec.recovery_status(ledger)

    assert status["ok"] is False
    assert status["error_code"] == "wps_recovery_journal_unreadable"
    assert status["operations"] == []
    assert status["next_action"] == "fix_journal"


def test_recovery_status_counts_guarded_separately(env):
    ledger, journal = env
    cli = RecordingCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]),
                       corrupt_write=True)
    result = cloud_apply_plan(
        cli, [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)],
        ledger=ledger, marker_enabled=False, journal=journal)
    rec.resolve_pending_operation(
        result["operation_id"], "retire_guarded", confirm="retire_guarded",
        note="已人工核对云端表结构", confirm_structure_checked=True,
        ledger=ledger, journal=SyncJournal(journal_path_for(ledger.path)))

    status = rec.recovery_status(
        ledger, journal=SyncJournal(journal_path_for(ledger.path)))

    assert status["pending_count"] == 0
    assert status["guarded_count"] == 1
    assert status["operations"][0]["retired_guarded"] is True


def test_journal_compaction_keeps_pending_records(env):
    """归档只清可证明安全的 terminal 记录，未完成的一律保留。"""
    ledger, journal = env
    cli = RecordingCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    good = cloud_apply_plan(
        cli, [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)],
        ledger=ledger, marker_enabled=False, journal=journal)
    bad_cli = FakeCli(make_grid(BASE_HEADER, [{0: "李", 2: "222", 7: "1"}]),
                      corrupt_write=True)
    bad = cloud_apply_plan(
        bad_cli, [_plan(bad_cli, _orders(("李", "222", 2, 3)), ledger=ledger)],
        ledger=ledger, marker_enabled=False, journal=journal)

    compacted = SyncJournal(journal_path_for(ledger.path)).compact(
        ledger, keep_operations=0)

    assert compacted["removed"] == 1, "只归档已证明的 verified 记录"
    remaining = SyncJournal(journal_path_for(ledger.path))
    assert good["operation_id"] not in remaining.operations()
    assert bad["operation_id"] in remaining.operations()


def test_compact_archive_is_readable_json(env):
    ledger, journal = env
    cli = RecordingCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    result = cloud_apply_plan(
        cli, [_plan(cli, _orders(("张", "111", 6, 3)), ledger=ledger)],
        ledger=ledger, marker_enabled=False, journal=journal)
    SyncJournal(journal_path_for(ledger.path)).compact(ledger, keep_operations=0)

    archive = Path(str(journal_path_for(ledger.path)) + ".archive")
    payload = json.loads(archive.read_text(encoding="utf-8"))
    assert result["operation_id"] in payload["operations"]
