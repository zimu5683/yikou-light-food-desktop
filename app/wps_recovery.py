"""WPS 部分失败恢复：只读云端分类 + 人工处置（绝不自动补写云端）。

契约（与参考实现一致，桌面端为单用户本机程序，因此不引入角色权限，但
**明确确认、范围、审计**一个都不能少）：

* :func:`recovery_status` 只读本地日志，返回逐表状态与可用的下一步动作；
  它**不联网**，也绝不修改任何文件。
* :func:`resolve_pending_operation` 支持四类动作：

  ``cloud_verified``
      人工主张"云端已完整成功"——但仍会**重新只读云端**逐格核对，
      只有证明 ``verified`` 才补账本并结案。
  ``cloud_untouched``
      人工主张"完全没执行"——同样必须由只读核对证明 ``not_started``。
  ``retire_guarded``
      有审计地退出全局待处理队列，**保留**同日期 + 同云表的防重复闸门：
      未知写入结果**不会**因此变成"可以自动重传"。
  ``keep``
      只写核对备注、保持阻断。

本模块**永不写云端**（``cloud_write`` 恒为 ``False``），只写本地日志审计。
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
from collections import Counter
from typing import Any, Mapping, Sequence

try:
    from .wps_atomicio import (FileLock, lock_path_for,
                               operation_lock_path_for)
    from .wps_cloud import (CELL_MARK, FIRST_DATA_ROW, HEADER_ROW, MAX_READ_CELLS,
                            MAX_SCAN_ROW, SyncLedger, WpsCloudError, _address_key,
                            _as_int, column_name, parse_date_header, person_key)
    from .redact import mask_phone
    from .wps_journal import SHEET_STATUSES, SyncJournal, journal_path_for
except ImportError:  # pragma: no cover - 直接执行模块时
    from wps_atomicio import FileLock, lock_path_for
    from wps_cloud import (CELL_MARK, FIRST_DATA_ROW, HEADER_ROW, MAX_READ_CELLS,
                           MAX_SCAN_ROW, SyncLedger, WpsCloudError, _address_key,
                           _as_int, column_name, parse_date_header, person_key)
    from redact import mask_phone
    from wps_journal import SHEET_STATUSES, SyncJournal, journal_path_for

#: 允许的处置动作（别名在入口处归一）。
RESOLVE_DECISIONS = ("retire_guarded", "cloud_verified", "cloud_untouched", "keep")
RETIRE_ALIASES = frozenset({
    "retire", "retire_manual", "retire_old_operation", "guarded_retire",
    "abandon", "abandon_guarded", "manual_retire", "audited_retire",
})
#: 审计备注最短长度：强制用户写下"为什么可以这样处置"。
RESOLVE_MIN_NOTE = 4

_TERMINAL_STATUSES = {"verified", "failed_no_write", "not_started"}


def _uncertain(reason: str, problems: list[str] | None = None) -> dict[str, Any]:
    return {"state": "uncertain", "reason": reason,
            "problems": problems or [], "next_action": "manual_reconcile"}


def target_ref(file_id: Any, sheet: Any, target_date: Any) -> str:
    """给界面的目标标识（不回传原始 file_id，避免日志/截图泄露表 ID）。"""
    raw = f"{file_id}|{sheet}|{target_date}".encode("utf-8")
    return f"wps-target:{hashlib.sha256(raw).hexdigest()[:12]}"


def _read_region(cli: Any, file_id: str, worksheet_id: int,
                 row_from: int, row_to: int, col_from: int, col_to: int
                 ) -> dict[tuple[int, int], str]:
    """按单次读取上限分块读取 0-based 闭区间，返回 0-based 键。"""
    if row_to < row_from or col_to < col_from:
        return {}
    width = col_to - col_from + 1
    chunk = max(1, MAX_READ_CELLS // max(1, width), 1)
    grid: dict[tuple[int, int], str] = {}
    start = row_from
    while start <= row_to:
        end = min(row_to, start + chunk - 1)
        grid.update(cli.read_grid(file_id, worksheet_id, start, end,
                                  col_from, col_to))
        start = end + 1
    return grid


def _cell(grid: Mapping[tuple[int, int], Any], row: int, col: int) -> str:
    """1-based 行/列取值；统一去空白。"""
    if not row or not col:
        return ""
    return str(grid.get((row - 1, col - 1), "") or "").strip()


def _as_int_field(intent: Mapping[str, Any], key: str) -> int:
    try:
        return int(intent.get(key, 0))
    except (TypeError, ValueError):
        return 0


def _expected_formulas(record: Mapping[str, Any], intent: Mapping[str, Any],
                       row: int) -> tuple[str, str] | None:
    if not intent.get("formula_needed") or not row:
        return None
    columns = record.get("columns") or {}
    date_cols = [int(c) for c in (record.get("date_cols") or []) if int(c or 0) > 0]
    served = int(columns.get("served") or 0)
    left = int(columns.get("left") or 0)
    total = int(columns.get("total") or 0)
    if not (date_cols and served and left and total):
        return None
    return (
        f"=SUM({column_name(min(date_cols))}{row}:{column_name(max(date_cols))}{row})",
        f"={column_name(total)}{row}-{column_name(served)}{row}",
    )


def classify_journal_sheet(cli: Any, record: Mapping[str, Any]) -> dict[str, Any]:
    """只读云端，对一条子表日志分类；任何异常/歧义都归为 ``uncertain``。

    三种结论：

    * ``verified``：云端每一格都等于"写完之后应有的值"，且人员行结构完整；
    * ``not_started``：云端与写前基线**逐行一致**，找不到本操作的任何痕迹；
    * ``uncertain``：其余一切（含读不出来、结构对不上、部分写入）。

    "读不出来"必须归 uncertain，绝不能传成"没写过"。
    """
    raw_status = str(record.get("status") or "")
    if raw_status not in SHEET_STATUSES:
        return _uncertain("unsupported_status")
    try:
        file_id = str(record.get("file_id") or "")
        worksheet_id = int(record.get("worksheet_id") or 0)
        target_col = int(record.get("target_col") or 0)
        columns = record.get("columns") or {}
        intent_list = record.get("intents") or []
        if not isinstance(columns, Mapping) or not isinstance(intent_list, list):
            return _uncertain("journal_invalid")
        if not file_id or not worksheet_id or not target_col:
            return _uncertain("journal_invalid")
        target = _dt.date.fromisoformat(str(record.get("target_date") or ""))
    except (TypeError, ValueError) as exc:
        return _uncertain(f"journal_invalid:{exc}")

    name_col = int(columns.get("name") or 1)
    phone_col = int(columns.get("phone") or (name_col + 1))
    address_col = int(columns.get("address") or 0)
    type_col = int(columns.get("type") or 0)
    kind_col = int(columns.get("kind") or 0)
    total_col = int(columns.get("total") or 0)
    served_col = int(columns.get("served") or 0)
    left_col = int(columns.get("left") or 0)
    marker_col = int(record.get("marker_col") or 0)
    marker_expected = str(record.get("marker_expected") or "")
    sort_key_col = int(record.get("sort_key_col") or 0)

    base_rows = [
        int(record.get("last_data_row") or 0),
        int(record.get("append_row") or 0),
        max([int(item[0]) for item in (record.get("baseline_name_rows") or [])] or [0]),
        max([int(i.get("row_hint") or 0) for i in intent_list
             if isinstance(i, Mapping)] or [0]),
    ]
    row_from = HEADER_ROW - 1
    row_to = min(max([FIRST_DATA_ROW, *base_rows]) + 10, MAX_SCAN_ROW)
    cols = [name_col, phone_col, target_col]
    cols += [c for c in (address_col, type_col, kind_col, total_col, served_col, left_col)
             if c]
    cols += [int(c) for c in (record.get("date_cols") or []) if int(c or 0) > 0]
    if marker_expected and marker_col:
        cols.append(marker_col)
    if sort_key_col:
        cols.append(sort_key_col)
    col_from, col_to = min(cols) - 1, max(cols) - 1

    try:
        grid = _read_region(cli, file_id, worksheet_id, row_from, row_to,
                            col_from, col_to)
        header_text = _cell(grid, HEADER_ROW, target_col)
        parsed = parse_date_header(header_text)
        if parsed != (target.month, target.day):
            return _uncertain(
                "target_date_mismatch",
                [f"第 {target_col} 列表头为「{header_text}」，"
                 f"目标日期为 {target.isoformat()}"])
        formula_needed = any(
            bool(i.get("formula_needed")) for i in intent_list
            if isinstance(i, Mapping))
        formula_grid = (cli.read_formulas(file_id, worksheet_id, row_from, row_to,
                                         col_from, col_to)
                        if formula_needed else {})
    except WpsCloudError as exc:
        return _uncertain(f"cloud_unreadable:{exc}")
    except Exception as exc:  # noqa: BLE001 - 恢复路径不允许把未知异常当成功
        return _uncertain(f"recovery_error:{type(exc).__name__}:{exc}")

    # 读取云端人员行（姓名+电话做键，保留槽位顺序）。
    rows_by_key: dict[tuple[str, str], list[int]] = {}
    actual_signature: list[tuple[int, str, str]] = []
    address_by_row: dict[int, str] = {}
    for (row0, col0), text in grid.items():
        if row0 < FIRST_DATA_ROW - 1 or col0 != name_col - 1:
            continue
        name = str(text or "").strip()
        if not name:
            continue
        phone = _cell(grid, row0 + 1, phone_col)
        key = person_key(name, phone)
        row = row0 + 1
        rows_by_key.setdefault(key, []).append(row)
        actual_signature.append((row, name, key[1]))
        if address_col:
            address_by_row[row] = _cell(grid, row, address_col)
    for rows in rows_by_key.values():
        rows.sort()
    actual_signature.sort()

    baseline_raw = record.get("baseline_name_rows") or []
    baseline_signature: list[tuple[int, str, str]] = []
    try:
        for item in baseline_raw:
            baseline_signature.append((int(item[0]), str(item[1]), str(item[2])))
    except (TypeError, ValueError, IndexError) as exc:
        return _uncertain(f"journal_invalid_baseline:{exc}")
    baseline_signature.sort()
    baseline_counts = Counter((name, phone) for _row, name, phone in baseline_signature)

    # 意图槽位映射：优先使用排序身份标记（若崩溃时辅助列还在），否则
    # 新增槽位按地址优先匹配、老槽位按剩余行升序，避免"新行排前面"导致 slot 错位。
    mapped_rows: dict[int, int] = {}
    if sort_key_col:
        token_to_row: dict[str, int] = {}
        for (row0, col0), text in grid.items():
            if col0 != sort_key_col - 1:
                continue
            raw = str(text or "")
            if ":" in raw:
                token = raw.rsplit(":", 1)[1].strip()
                if token:
                    token_to_row[token] = row0 + 1
        for index, item in enumerate(intent_list):
            if not isinstance(item, Mapping):
                continue
            token = str(item.get("sort_token") or "")
            if token and token in token_to_row:
                mapped_rows[index] = token_to_row[token]

    for key, candidates in rows_by_key.items():
        pending = [(idx, item) for idx, item in enumerate(intent_list)
                   if isinstance(item, Mapping)
                   and (str(item.get("name") or ""), str(item.get("phone_key") or "")) == key
                   and idx not in mapped_rows]
        if not pending:
            continue
        used = {int(row) for row in mapped_rows.values()}
        free = [row for row in candidates if row not in used]
        for idx, item in sorted(
                (pair for pair in pending if pair[1].get("kind") == "new"),
                key=lambda pair: (int(pair[1].get("slot") or 0), pair[0])):
            wanted = _address_key(item.get("address"))
            chosen = next((row for row in free
                           if wanted and _address_key(address_by_row.get(row)) == wanted),
                          None)
            if chosen is None:
                slot = int(item.get("slot") or 0)
                chosen = free[slot - 1] if 0 < slot <= len(free) else (free[0] if free else None)
            if chosen is None:
                break
            mapped_rows[idx] = chosen
            free.remove(chosen)
        for idx, item in sorted(
                (pair for pair in pending if pair[0] not in mapped_rows),
                key=lambda pair: (int(pair[1].get("slot") or 0), pair[0])):
            if not free:
                break
            wanted = _address_key(item.get("address"))
            chosen = next((row for row in free
                           if wanted and _address_key(address_by_row.get(row)) == wanted),
                          free[0])
            mapped_rows[idx] = chosen
            free.remove(chosen)

    # 结构核对分两套：
    #   verified：基线 + 本次新建槽位；
    #   not_started：必须与基线逐行一致（插入/排序都没痕迹）。
    expected_counts = Counter(baseline_counts)
    for item in intent_list:
        if not isinstance(item, Mapping):
            continue
        key = (str(item.get("name") or ""), str(item.get("phone_key") or ""))
        if item.get("kind") == "new":
            expected_counts[key] += 1
    actual_counts = Counter({key: len(rows) for key, rows in rows_by_key.items()})
    expected_keys = {key for key, count in expected_counts.items() if count}
    verified_problems: list[str] = []
    if set(actual_counts) != expected_keys:
        extra = sorted(key[0] for key in set(actual_counts) - expected_keys)
        missing = sorted(key[0] for key in expected_keys - set(actual_counts))
        if extra:
            verified_problems.append(f"云端多出人员行：{'、'.join(extra[:5])}")
        if missing:
            verified_problems.append(f"云端缺少人员行：{'、'.join(missing[:5])}")
    for key, count in expected_counts.items():
        if count and int(actual_counts.get(key, 0)) != int(count):
            verified_problems.append(
                f"{key[0]}：云端 {actual_counts.get(key, 0)} 行，期望 {count} 行")
    structure_ok = not verified_problems
    baseline_counts_actual = Counter((name, phone) for _row, name, phone in actual_signature)
    baseline_structure_ok = (actual_signature == baseline_signature
                            and baseline_counts_actual == baseline_counts)

    marker_actual = (_cell(grid, HEADER_ROW, marker_col)
                     if (marker_expected and marker_col) else "")
    marker_ok = (not marker_expected) or marker_actual == marker_expected
    helper_nonempty = False
    if sort_key_col:
        helper_nonempty = any(
            _cell(grid, row, sort_key_col) for row in range(FIRST_DATA_ROW, row_to + 1))

    expected_matches: list[bool] = []
    original_matches: list[bool] = []
    problems: list[str] = []
    for index, item in enumerate(intent_list):
        if not isinstance(item, Mapping):
            problems.append(f"意图 {index} 结构非法")
            expected_matches.append(False)
            original_matches.append(False)
            continue
        name = str(item.get("name") or "")
        slot = int(item.get("slot") or 0)
        row = mapped_rows.get(index)
        expected_total = _as_int_field(item, "total_after")
        before_total = _as_int_field(item, "total_before")
        expected_target = str(item.get("target_expected") or CELL_MARK)
        before_target = str(item.get("target_before") or "")
        kind = str(item.get("kind") or "")
        actual_total = (_as_int(_cell(grid, row or 0, total_col))
                        if (row and total_col) else None)
        actual_target = _cell(grid, row or 0, target_col) if row else ""
        actual_type = _cell(grid, row or 0, type_col) if (row and type_col) else ""
        actual_kind = _cell(grid, row or 0, kind_col) if (row and kind_col) else ""
        actual_address = address_by_row.get(row or 0, "") if row else ""

        want_type = str(item.get("meal_type") or "")
        want_kind = str(item.get("meal_kind") or "")
        need_type = bool(type_col and want_type and (kind == "new" or item.get("fill_type")))
        need_kind = bool(kind_col and want_kind and (kind == "new" or item.get("fill_kind")))
        formulas = _expected_formulas(record, item, row or 0)
        formula_ok = True
        if formulas and row:
            exp_served, exp_left = formulas
            formula_ok = (str(formula_grid.get((row - 1, served_col - 1), "")) == exp_served
                          and str(formula_grid.get((row - 1, left_col - 1), "")) == exp_left)

        exp_match = bool(row)
        if total_col and actual_total != expected_total:
            exp_match = False
        if actual_target != expected_target:
            exp_match = False
        if need_type and actual_type != want_type:
            exp_match = False
        if need_kind and actual_kind != want_kind:
            exp_match = False
        if kind == "new" and address_col and item.get("address"):
            if _address_key(actual_address) != _address_key(item.get("address")):
                exp_match = False
        if not formula_ok:
            exp_match = False
        expected_matches.append(exp_match)

        if kind == "new":
            orig_match = row is None
        else:
            orig_match = bool(row)
            if total_col and actual_total != before_total:
                orig_match = False
            if actual_target != before_target:
                orig_match = False
            if need_type and actual_type != "":
                orig_match = False
            if need_kind and actual_kind != "":
                orig_match = False
            if formulas and row:
                exp_served, exp_left = formulas
                if (str(formula_grid.get((row - 1, served_col - 1), "")) == exp_served
                        and str(formula_grid.get((row - 1, left_col - 1), "")) == exp_left):
                    orig_match = False
        original_matches.append(orig_match)

        if not exp_match:
            problems.append(
                f"{name} 槽位 {slot}：期望 total={expected_total}/mark={expected_target!r}，"
                f"实际 total={actual_total}/mark={actual_target!r}"
                + (f"/类型={actual_type!r}/餐种={actual_kind!r}"
                   if (need_type or need_kind) else ""))

    all_expected = bool(intent_list) and all(expected_matches)
    all_original = all(original_matches) and baseline_structure_ok
    if helper_nonempty:
        verified_problems.append("排序辅助列仍有残留内容")
        structure_ok = False
    if verified_problems:
        problems = verified_problems + problems
    if all_expected and structure_ok and marker_ok and not helper_nonempty:
        return {"state": "verified", "reason": "", "problems": [],
                "next_action": "none"}
    if all_original and baseline_structure_ok and not helper_nonempty and not (
            marker_expected and marker_actual == marker_expected):
        return {"state": "not_started", "reason": "云端与基线一致，未发现本操作写入痕迹",
                "problems": [], "next_action": "repreview"}
    if not problems:
        problems.append("云端状态既不是完整期望值，也不是完整原值：无法确认是否写入")
    if marker_expected and not marker_ok:
        problems.append(f"通讯记号应为 {marker_expected}，实际 {marker_actual!r}")
    return _uncertain("partial_or_ambiguous", problems[:20])


# ----------------------------------------------------------------------
# 账本合并（恢复证明成功后补锚点）
# ----------------------------------------------------------------------

def _merge_into_ledger(ledger: SyncLedger, target_date: str, file_id: str,
                       entries: Mapping[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """在给定账本快照上计算恢复合并结果；不落盘。"""
    to_record: dict[str, Any] = {}
    for storage_key, payload in entries.items():
        if not isinstance(payload, Mapping):
            return False, "journal_ledger_entry_invalid", {}
        name, separator, phone = str(storage_key).partition("\u0000")
        if not separator:
            return False, "journal_ledger_key_invalid", {}
        existing = ledger.synced_slots(target_date, file_id, name, phone)
        wanted = payload.get("slots")
        if existing is None:
            to_record[str(storage_key)] = dict(payload)
            continue
        old_slots = [int(v) for v in existing]
        new_slots = [int(v) for v in wanted] if wanted is not None else []
        if old_slots == new_slots:
            continue          # 已经恢复过，幂等
        if old_slots == new_slots[:len(old_slots)]:
            # 本地新增了槽位：新 slots 以旧 slots 为前缀，补全幂等锚点。
            to_record[str(storage_key)] = dict(payload)
            continue
        if new_slots == old_slots[:len(new_slots)]:
            # 本次云端验证只涉及较少槽位：旧账本已有更长前缀锚点，保持不动。
            continue
        return False, f"ledger_slot_conflict:{name}", {}
    return True, "", to_record


def _merge_ledger(ledger: SyncLedger | None, record: Mapping[str, Any]) -> tuple[bool, str]:
    """恢复证明完整成功后，把日志里的账本锚点安全并入磁盘账本。"""
    entries = record.get("ledger_entries") or {}
    if not isinstance(entries, Mapping):
        return False, "journal_ledger_entries_invalid"
    if not entries:
        return True, ""
    if ledger is None:
        return False, "ledger_missing"
    target_date = str(record.get("target_date") or "")
    file_id = str(record.get("file_id") or "")
    path = getattr(ledger, "path", None)
    snapshot = copy.deepcopy(ledger.data)
    snapshot_digest = getattr(ledger, "_loaded_digest", None)
    try:
        if path:
            with FileLock(lock_path_for(path), timeout=30.0):
                fresh = SyncLedger(path)
                ok, reason, to_record = _merge_into_ledger(
                    fresh, target_date, file_id, entries)
                if not ok:
                    return False, reason
                if to_record:
                    fresh.record(target_date, file_id, to_record)
                    fresh._save_unlocked()
                ledger.data = fresh.data
                ledger._loaded_digest = fresh._loaded_digest
        else:
            ok, reason, to_record = _merge_into_ledger(
                ledger, target_date, file_id, entries)
            if not ok:
                return False, reason
            if to_record:
                ledger.record(target_date, file_id, to_record)
                ledger.save()
    except BaseException as exc:  # noqa: BLE001 - 失败要如实报告并回滚内存
        ledger.data = snapshot
        if hasattr(ledger, "_loaded_digest"):
            ledger._loaded_digest = snapshot_digest
        if not isinstance(exc, Exception):
            raise
        return False, f"ledger_merge_failed:{type(exc).__name__}:{exc}"
    return True, ""


# ----------------------------------------------------------------------
# 只读状态
# ----------------------------------------------------------------------

def _sheet_actions(display_status: str, next_action: str) -> list[str]:
    """某张表当前允许的处置动作（界面据此禁用按钮）。"""
    if display_status in _TERMINAL_STATUSES:
        return []
    if display_status == "retired_guarded":
        return ["cloud_verified", "cloud_untouched", "keep"]
    actions = ["cloud_verified", "cloud_untouched", "retire_guarded", "keep"]
    return actions


def _safe_sheet(record: Mapping[str, Any], sheet_key: str) -> dict[str, Any]:
    status = str(record.get("status") or "uncertain")
    raw_intents = record.get("intents") or []
    people = [{"name": str(item.get("name") or ""),
               # 恢复列表会出现在界面/截图/验收证据里：手机号按脱敏口径给出。
               "phone": mask_phone(item.get("phone") or ""),
               "slot": int(item.get("slot") or 0),
               "local_meals": int(item.get("local_meals") or 0),
               "total_before": int(item.get("total_before") or 0),
               "total_after": int(item.get("total_after") or 0)}
              for item in raw_intents if isinstance(item, Mapping)]
    return {
        "sheet": str(record.get("sheet") or sheet_key),
        "status": status,
        "display_status": ("retired_guarded"
                           if (record.get("retired_guarded")
                               or status == "retired_guarded") else status),
        "reason": str(record.get("reason") or "")[:300],
        "problems": [str(item)[:300] for item in (record.get("problems") or [])][:20],
        "next_action": str(record.get("next_action") or ""),
        "allowed_actions": _sheet_actions(
            "retired_guarded" if (record.get("retired_guarded")
                                  or status == "retired_guarded") else status,
            str(record.get("next_action") or "")),
        "target_date": str(record.get("target_date") or ""),
        "target_ref": target_ref(record.get("file_id"), record.get("sheet"), 
                                 record.get("target_date")),
        "people": people[:50],
        "people_count": len(people),
        "cloud_checked": bool(record.get("cloud_checked", False)),
        "evidence": str(record.get("evidence") or "local_journal"),
        "retire_note": str(record.get("retire_note") or ""),
        "retired_at": str(record.get("retired_at") or ""),
        "prior_status": str(record.get("prior_status") or ""),
    }


def journal_for_ledger(ledger: SyncLedger) -> SyncJournal:
    """与账本同目录的意图日志。"""
    return SyncJournal(journal_path_for(ledger.path))


def recovery_status(ledger: SyncLedger | None = None,
                    journal: Any = None) -> dict[str, Any]:
    """只读 WPS 恢复查询：不联网、不写云端、不写日志/账本。

    日志不可读时返回 ``ok=False`` 与明确的 ``error_code``，
    **绝不**返回"没有未完成操作"的空摘要。
    """
    if ledger is None:
        try:
            ledger = SyncLedger()
        except WpsCloudError as exc:
            return _failure("wps_recovery_ledger_unreadable", "fix_journal",
                            f"{type(exc).__name__}: {exc}")
    if journal is None:
        try:
            journal = journal_for_ledger(ledger)
        except Exception as exc:  # noqa: BLE001
            return _failure("wps_recovery_journal_unreadable", "fix_journal",
                            f"{type(exc).__name__}: {exc}")
    try:
        pending = journal.pending_operations()
        guarded = journal.guarded_operations()
    except Exception as exc:  # noqa: BLE001
        return _failure("wps_recovery_journal_unreadable", "fix_journal",
                        f"{type(exc).__name__}: {exc}")

    operations: list[dict[str, Any]] = []
    for op_id, op in sorted(pending.items(), key=lambda item: str(item[0])):
        operations.append(_safe_operation(op_id, op, pending=True))
    for op_id, op in sorted(guarded.items(), key=lambda item: str(item[0])):
        if op_id in pending:
            continue
        operations.append(_safe_operation(op_id, op, pending=False))

    counts: dict[str, int] = {}
    for op in operations:
        for sheet in op.get("sheets") or []:
            status = str(sheet.get("display_status") or "uncertain")
            counts[status] = counts.get(status, 0) + 1
    pending_count = sum(1 for op in operations if op.get("pending"))
    next_action = ("manual_reconcile" if pending_count
                   else "none" if not operations else "manual_reconcile")
    return {
        "ok": True,
        "contract_version": 1,
        "source": "local_journal",
        "read_only": True,
        "queried_cloud": False,
        "counts": counts,
        "pending_count": pending_count,
        "guarded_count": sum(1 for op in operations if not op.get("pending")),
        "operations": operations,
        "pending_operations": [op for op in operations if op.get("pending")],
        "next_action": next_action,
        "journal_path": str(getattr(journal, "path", "") or ""),
    }


def recovery_status_error(code: str, detail: str = "") -> dict[str, Any]:
    """本地状态不可读时的恢复查询结果（**绝不**伪装成"没有未完成操作"）。"""
    return _failure(code, "fix_journal", detail)


def _safe_operation(operation_id: str, op: Mapping[str, Any], *,
                    pending: bool) -> dict[str, Any]:
    sheets_raw = op.get("sheets") or {}
    sheets = [_safe_sheet(record, str(key))
              for key, record in sheets_raw.items()
              if isinstance(record, Mapping)]
    return {
        "operation_id": str(operation_id),
        "operation_ref": str(operation_id),
        "status": str(op.get("status") or "uncertain"),
        "next_action": str(op.get("next_action") or ""),
        "created_at": str(op.get("created_at") or ""),
        "updated_at": str(op.get("updated_at") or ""),
        "target_date": str(op.get("target_date") or ""),
        "pending": bool(pending),
        "retired_guarded": bool(op.get("retired_guarded", False)),
        "retire_note": str(op.get("retire_note") or ""),
        "sheets": sheets,
    }


def _failure(code: str, next_action: str, detail: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        "contract_version": 1,
        "source": "local_journal",
        "read_only": True,
        "queried_cloud": False,
        "error_code": str(code),
        "reason": str(code) + (f"：{detail}" if detail else ""),
        "next_action": str(next_action),
        "counts": {},
        "operations": [],
        "pending_operations": [],
    }


# ----------------------------------------------------------------------
# 人工处置（永不写云端）
# ----------------------------------------------------------------------

def resolve_pending_operation(
        operation_id: str,
        decision: str,
        *,
        confirm: str = "",
        note: str = "",
        confirm_structure_checked: bool = False,
        ledger: SyncLedger | None = None,
        journal: Any = None,
        cli: Any = None,
        lock_timeout: float = 0.0) -> dict[str, Any]:
    """人工核对后的处置入口；**永不写云端**，只写本地日志审计。

    与上传共用同一把**操作级锁**（``<ledger>.oplock``）：恢复的"读状态 → 核对 →
    记账 → 保存 journal"整段都不允许和另一个上传/恢复交织。冲突立即返回，不排队。

    锁内会**重载权威账本与日志**：调用方传进来的对象可能已经过时（另一个进程刚
    改过），处置必须基于磁盘上的最新状态，否则会写回一份旧快照。

    请求约束（与服务端契约一致）：

    * ``confirm`` 必须与 ``decision`` 完全相同；
    * ``note`` 至少 4 个字符（写进审计）；
    * ``retire_guarded`` 需要 ``confirm_structure_checked=True``；
    * ``cloud_verified`` / ``cloud_untouched`` 必须由**实际只读云端核对**
      证明才算数，证明不了就保持阻断；
    * 重复处置幂等：已经 ``retired_guarded`` 的任务再次提交返回 ``already_retired``。
    """
    operation_key = str(operation_id or "").strip()
    lock = _operation_lock(ledger)
    if lock is not None:
        try:
            lock.acquire()
        except Exception as exc:  # noqa: BLE001 - 拿不到独占就不许改状态
            return _resolve_failure(
                "operation_locked",
                f"另一个上传/恢复操作正在进行（{type(exc).__name__}: {exc}）；"
                f"为避免用旧状态覆盖它的结果，本次处置被拒绝",
                "等当前操作结束后重试", status="blocked",
                operation_ref=operation_key)
    try:
        fresh_ledger, fresh_journal = _reload_authoritative(ledger, journal)
        return _resolve_pending_operation_locked(
            operation_id, decision, confirm=confirm, note=note,
            confirm_structure_checked=confirm_structure_checked,
            ledger=fresh_ledger, journal=fresh_journal, cli=cli)
    finally:
        if lock is not None:
            lock.release()


def _operation_lock(ledger: SyncLedger | None) -> Any:
    """恢复处置用的操作级锁；没有账本路径时返回 ``None``（无锁）。"""
    path = getattr(ledger, "path", None) if ledger is not None else None
    if not path:
        return None
    return FileLock(operation_lock_path_for(path), timeout=0.0)


def _reload_authoritative(ledger: SyncLedger | None,
                          journal: Any) -> tuple[SyncLedger | None, Any]:
    """在锁内重载账本与日志；失败时保留调用方传入的对象（后续会如实报错）。"""
    path = getattr(ledger, "path", None) if ledger is not None else None
    if not path:
        return ledger, journal
    try:
        fresh = SyncLedger(path)
        return fresh, journal_for_ledger(fresh)
    except Exception:  # noqa: BLE001 - 读不出来就让原对象去报错，别吞掉
        return ledger, journal


def _resolve_pending_operation_locked(
        operation_id: str,
        decision: str,
        *,
        confirm: str = "",
        note: str = "",
        confirm_structure_checked: bool = False,
        ledger: SyncLedger | None = None,
        journal: Any = None,
        cli: Any = None) -> dict[str, Any]:
    """``resolve_pending_operation`` 的实现（调用方已持有操作级锁）。"""
    raw_decision = str(decision or "").strip().lower()
    normalized = "retire_guarded" if raw_decision in RETIRE_ALIASES else raw_decision
    confirm_text = str(confirm or "").strip()
    note_text = str(note or "").strip()
    operation_key = str(operation_id or "").strip()

    if normalized not in RESOLVE_DECISIONS:
        return _resolve_failure("decision_invalid", "处置动作不合法",
                                "从 wps_recovery_status 的 allowed_actions 里选",
                                operation_ref=operation_key)
    if confirm_text != normalized:
        return _resolve_failure(
            "confirm_mismatch", "确认文本必须与处置动作完全一致",
            f"把 confirm 原样填成 {normalized}", operation_ref=operation_key)
    if len(note_text) < RESOLVE_MIN_NOTE:
        return _resolve_failure(
            "note_too_short", f"人工备注至少 {RESOLVE_MIN_NOTE} 个字符",
            "填写核对说明后重试", operation_ref=operation_key)
    if not operation_key:
        return _resolve_failure("invalid_operation_id", "缺少 operation_id",
                                "先用 wps_recovery_status 取出 operation_id")

    if ledger is None:
        try:
            ledger = SyncLedger()
        except WpsCloudError as exc:
            return _resolve_failure("ledger_unreadable", f"账本不可用：{exc}",
                                    "先修复本地账本", operation_ref=operation_key)
    if journal is None:
        try:
            journal = journal_for_ledger(ledger)
        except Exception as exc:  # noqa: BLE001
            return _resolve_failure("journal_unreadable", f"意图日志不可用：{exc}",
                                    "先修复本地日志", operation_ref=operation_key)

    op = journal.get_operation(operation_key)
    if not isinstance(op, dict):
        return _resolve_failure("operation_not_found", "意图日志里没有这个操作",
                                "重新查询 wps_recovery_status",
                                operation_ref=operation_key)
    if not isinstance(op.get("sheets"), dict):
        return _resolve_failure("operation_invalid", "操作记录结构非法",
                                "先修复本地日志", operation_ref=operation_key)

    if normalized == "retire_guarded" and not confirm_structure_checked:
        return _resolve_failure(
            "manual_confirmation_required",
            "退出（保留防重复闸门）需要确认「已核对云端表结构」",
            "勾选结构核对后重试", status="rejected",
            operation_ref=operation_key)

    sheets: Mapping[str, Any] = op["sheets"]
    # 幂等判据只看"还有没有需要退出的子表"，不看 operation 级标记：
    # 上一个进程可能只写到了子表级（崩溃/部分写），再点一次仍然是"没有可退的"。
    already_retired = bool(sheets) and all(
        not isinstance(record, Mapping)
        or record.get("retired_guarded")
        or str(record.get("status") or "") in _TERMINAL_STATUSES
        for record in sheets.values())

    results: list[dict[str, Any]] = []
    if normalized == "retire_guarded":
        if already_retired:
            return _resolve_success("already_retired", operation_key, [],
                                    note=note_text, status="retired_guarded")
        now = _dt.datetime.now().isoformat(timespec="microseconds")
        for sheet_key, record in sheets.items():
            if not isinstance(record, dict):
                continue
            status = str(record.get("status") or "")
            if status in _TERMINAL_STATUSES:
                continue
            changed = journal.set_sheet_status(
                operation_key, sheet_key, "retired_guarded",
                reason=note_text or record.get("reason", ""),
                next_action="manual_reconcile",
                problems=record.get("problems") or [],
                retired_guarded=True, retired_at=now, retire_note=note_text,
                prior_status=status or "unknown",
                cloud_checked=bool(record.get("cloud_checked", False)),
                evidence=str(record.get("evidence") or "local_journal"))
            if not changed:
                continue
            results.append({"sheet": record.get("sheet", sheet_key),
                            "status": "retired_guarded", "reason": "manual_retire"})
        op = journal.get_operation(operation_key) or op
        op["retired_guarded"] = True
        op["retired_at"] = now
        op["retire_note"] = note_text
        op["retire_decision"] = "retire_guarded"
        op["status"] = "retired_guarded"
        op["next_action"] = "manual_reconcile"
        op["updated_at"] = now
        saved, detail = _save_journal(journal)
        if not saved:
            return _resolve_failure("journal_save_failed",
                                    f"日志落盘失败：{detail}", "fix_journal",
                                    status="blocked", operation_ref=operation_key)
        return _resolve_success("retired_with_guard", operation_key, results,
                                note=note_text, status="retired_guarded")

    # keep / cloud_verified / cloud_untouched：逐表处理
    for sheet_key, record in sheets.items():
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "")
        if status in _TERMINAL_STATUSES:
            continue
        if normalized == "keep":
            _apply_keep(journal, operation_key, sheet_key, record, note_text)
            results.append({"sheet": record.get("sheet", sheet_key),
                            "status": "uncertain", "reason": "user_keep"})
            continue
        classified = (classify_journal_sheet(cli, record)
                      if cli is not None else None)
        if normalized == "cloud_verified":
            if classified is None or classified.get("state") != "verified":
                results.append({
                    "sheet": record.get("sheet", sheet_key), "status": "uncertain",
                    "reason": ("cloud_verify_failed" if classified else "cli_required"),
                    "problems": (classified or {}).get("problems", [])})
                continue
            ok, reason = _merge_ledger(ledger, record)
            if not ok:
                journal.set_sheet_status(
                    operation_key, sheet_key, "ledger_pending", reason=reason,
                    next_action="recover_journal", problems=[reason],
                    cloud_checked=True, evidence="resolve+cloud_read")
                results.append({"sheet": record.get("sheet", sheet_key),
                                "status": "ledger_pending", "reason": reason})
                continue
            journal.set_sheet_status(
                operation_key, sheet_key, "verified", reason=note_text,
                next_action="none", problems=[], retired_guarded=False,
                retired_at="", retire_note="", prior_status="",
                cloud_checked=True, evidence="resolve+cloud_read")
            results.append({"sheet": record.get("sheet", sheet_key),
                            "status": "verified", "reason": ""})
        else:  # cloud_untouched
            manual = str(record.get("manual_required") or "")
            can_confirm = bool(
                classified is not None and classified.get("state") == "not_started"
                and (not manual or confirm_structure_checked))
            if not can_confirm:
                results.append({
                    "sheet": record.get("sheet", sheet_key), "status": "uncertain",
                    "reason": ("cloud_not_untouched" if classified else "cli_required"),
                    "problems": (classified or {}).get("problems", [])})
                continue
            journal.set_sheet_status(
                operation_key, sheet_key, "not_started",
                reason=note_text or "人工核对确认完全未执行",
                next_action="repreview", problems=[], retired_guarded=False,
                retired_at="", retire_note="", prior_status="",
                cloud_checked=True, evidence="resolve+cloud_read")
            results.append({"sheet": record.get("sheet", sheet_key),
                            "status": "not_started", "reason": "manual_confirm"})

    saved, detail = _save_journal(journal)
    if not saved:
        return _resolve_failure("journal_save_failed", f"日志落盘失败：{detail}",
                                "fix_journal", status="blocked",
                                operation_ref=operation_key)
    if normalized == "keep":
        return _resolve_success("kept", operation_key, results, note=note_text,
                                status="uncertain",
                                next_action="manual_reconcile")
    resolved = not any(item.get("status") in ("uncertain", "ledger_pending")
                       for item in results)
    return _resolve_success(
        "resolved" if resolved else "uncertain", operation_key, results,
        note=note_text, status="resolved" if resolved else "uncertain",
        next_action="repreview" if resolved else "manual_reconcile")


def _apply_keep(journal: Any, operation_key: str, sheet_key: str,
                record: Mapping[str, Any], note_text: str) -> None:
    """``keep``：保持阻断（已退出的旧任务只更新审计备注，不退回全局待处理）。"""
    retired = bool(record.get("retired_guarded")) or (
        str(record.get("status") or "") == "retired_guarded")
    journal.set_sheet_status(
        operation_key, sheet_key, "retired_guarded" if retired else "uncertain",
        reason=note_text or record.get("reason", ""),
        next_action="manual_reconcile",
        problems=record.get("problems") or [],
        retired_guarded=True if retired else False,
        retired_at=(record.get("retired_at")
                    or _dt.datetime.now().isoformat(timespec="microseconds"))
        if retired else "",
        retire_note=(note_text or record.get("retire_note", "")) if retired else "",
        cloud_checked=bool(record.get("cloud_checked", False)),
        evidence=str(record.get("evidence") or "local_journal"))


def _save_journal(journal: Any) -> tuple[bool, str]:
    try:
        journal.save()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def _resolve_failure(code: str, reason: str, next_action: str, *,
                     status: str = "rejected",
                     operation_ref: str = "",
                     extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "status": str(status),
        "code": str(code),
        "reason": str(reason or code),
        "next_action": str(next_action or ""),
        "contract_version": 1,
        "read_only": False,
        "cloud_write": False,
        "operation_ref": str(operation_ref or ""),
        "changed": False,
        "operations": [],
        "allowed_decisions": list(RESOLVE_DECISIONS),
    }
    if extra:
        payload.update(extra)
    return payload


def _resolve_success(reason_code: str, operation_ref: str,
                     results: Sequence[Mapping[str, Any]], *, note: str,
                     status: str, next_action: str = "") -> dict[str, Any]:
    changed = any(item.get("status") in ("verified", "not_started", "retired_guarded")
                  for item in results)
    return {
        "ok": reason_code in ("resolved", "retired_with_guard", "already_retired",
                              "kept"),
        "status": str(status),
        "code": "",
        "reason": "",
        "reason_code": str(reason_code),
        "next_action": str(next_action),
        "contract_version": 1,
        "read_only": False,
        "cloud_write": False,
        "operation_ref": str(operation_ref),
        "changed": bool(changed),
        "note": str(note),
        "resolved_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "operations": [dict(item) for item in results],
    }


__all__ = [
    "RESOLVE_DECISIONS",
    "RESOLVE_MIN_NOTE",
    "RETIRE_ALIASES",
    "classify_journal_sheet",
    "journal_for_ledger",
    "recovery_status",
    "recovery_status_error",
    "resolve_pending_operation",
    "target_ref",
]
