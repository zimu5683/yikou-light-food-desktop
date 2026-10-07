"""WPS 预览/上传的**两种口径**统计：计划归计划、执行归执行。

禁止用"计划更新 100 行"代表"实际已经成功写入 100 行"——两者只有在全部成功
时数字才恰好相同，而失败/未知时它们完全不同。因此：

* :func:`planned_summary` 标注 ``kind="plan"``：计划**要改**多少行；
* :func:`execution_summary` 标注 ``kind="execution"``：执行**实际证明**了多少行。

执行口径的行数只认 ``apply_plan`` 逐格回读校验通过的表（``people`` 计数）；
拿不到整数、表状态未知、或整轮结果不确定时一律上报 ``null``（未知），
绝不用计划数或成功表数顶替。
"""
from __future__ import annotations

from typing import Any, Iterable

#: 执行口径的逐表状态桶。
EXEC_SHEET_KEYS = ("verified", "noop", "failed", "uncertain", "skipped",
                   "blocked", "other")


def _as_int(value: Any, default: int = 0) -> int:
    """宽松整数：布尔、``None``、非数字文本都回退默认值，绝不抛异常。"""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def planned_summary(plans: Any, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    """计划口径：明确标注 ``kind="plan"``，避免被当成执行结果。

    ``stats`` 可由调用方传入已算好的 :func:`app.wps_cloud.summarize_plan` 结果
    （避免重复计算，也让调用方可以替换实现）。
    """
    if stats is None:
        from .wps_cloud import summarize_plan
        try:
            stats = summarize_plan(plans)
        except Exception:  # noqa: BLE001 - 摘要失败不能影响写入结果
            stats = {}
    planned: dict[str, Any] = {str(key): value for key, value in (stats or {}).items()}
    planned["kind"] = "plan"
    planned["rows"] = {
        "to_update": _as_int(planned.get("to_update")),
        "to_append": _as_int(planned.get("to_append")),
        "unchanged": _as_int(planned.get("unchanged")),
        "skipped": _as_int(planned.get("skipped")),
        "warned": _as_int(planned.get("warned")),
        "blocked": _as_int(planned.get("blocked")),
    }
    planned["note"] = "计划要改动的行数，不是执行结果；执行结果见 execution_summary.rows"
    return planned


def execution_summary(*, status: str,
                      sheets: Iterable[dict[str, Any]] = (),
                      written: int = 0, failed: int = 0,
                      uncertain: bool | None = None,
                      malformed: bool = False,
                      next_action: str = "",
                      planned: dict[str, Any] | None = None,
                      proven_no_write: bool = False,
                      executed: bool = True,
                      counts_source: str = "") -> dict[str, Any]:
    """执行口径：逐表状态计数 + 可证明/未知的行数。

    统计口径（固定，不随调用方变化）：

    * ``sheets.*`` = **表数**：``verified``（ok）、``noop``、``failed``
      （failed/stale_batch/blocked）、``uncertain``、``skipped``、``blocked``、
      ``other``（未知/畸形状态）。
    * ``rows.verified`` = 逐表 ``people`` 求和；任何一张 ok 表拿不到整数计数
      就整体为 ``None``（未知）。noop 表按 0 计入（执行器证明没有写）。
    * ``rows.failed`` = 0（执行器对 failed/stale_batch 表保证零写入）或
      ``None``（表结构畸形/整轮异常，无法证明）。
    * ``rows.uncertain`` = 0（不存在不确定表）或 ``None``（未知）。
    * ``rows.skipped`` = 0（skipped 表在任何写入之前被跳过）或 ``None``。
    * ``rows.planned`` = 计划要改的行数，单独标注为计划口径。
    * ``rows_unknown`` = 上面任意一个为 ``None``。
    * ``proven_no_write`` = 这次调用是否**已被证明**没有发生任何云端写入
      （例如在消费预览令牌之前就拒绝）。
    """
    sheet_items = [item for item in sheets if isinstance(item, dict)]
    sheet_statuses = [str(item.get("status") or "") for item in sheet_items]
    counts = {key: 0 for key in EXEC_SHEET_KEYS}
    item_uncertain = False
    for item, sheet_status in zip(sheet_items, sheet_statuses):
        if sheet_status in ("ok", "verified"):
            counts["verified"] += 1
        elif sheet_status == "noop":
            counts["noop"] += 1
        elif sheet_status in ("failed", "verify_failed", "verify_unreadable",
                              "stale_batch"):
            counts["failed"] += 1
            if bool(item.get("uncertain")):
                item_uncertain = True
        elif sheet_status == "uncertain":
            counts["uncertain"] += 1
        elif sheet_status == "skipped":
            counts["skipped"] += 1
        elif sheet_status == "blocked":
            counts["blocked"] += 1
        else:
            counts["other"] += 1
    if item_uncertain:
        counts["uncertain"] = max(counts["uncertain"], 1)

    rows_verified: int | None = 0
    rows_verified_known = not malformed
    seen_sheets: list[str] = []
    for item in sheet_items:
        key = str(item.get("sheet") or "")
        if key in seen_sheets:
            # 同一张表被重复上报会重复计数：一律按未知处理，宁可说"不知道"。
            rows_verified_known = False
            break
        seen_sheets.append(key)
    if rows_verified_known:
        for item, sheet_status in zip(sheet_items, sheet_statuses):
            if sheet_status not in ("ok", "verified"):
                continue
            people = item.get("people")
            if isinstance(people, bool) or not isinstance(people, int) or people < 0:
                rows_verified_known = False
                break
            rows_verified += people
    if rows_verified_known and not proven_no_write:
        aggregate = str(status or "").strip().lower()
        if uncertain is not False or counts["uncertain"] or counts["other"]:
            rows_verified_known = False
        elif executed and not sheet_statuses:
            rows_verified_known = False
        elif aggregate in ("failed", "error", "blocked", "rejected") and counts["verified"]:
            rows_verified_known = False
    if not rows_verified_known:
        rows_verified = None

    rows_failed: int | None = None if malformed else 0
    uncertain_known = bool(
        uncertain is False and not counts["uncertain"] and not counts["other"]
        and not malformed)
    rows_uncertain: int | None = 0 if (uncertain_known or proven_no_write) else None
    rows_skipped: int | None = None if malformed else 0
    if not executed and not proven_no_write:
        # 执行过程的异常路径（例如 apply_plan 抛错）：可能已写入一部分，
        # 四个行数全部按"未知"上报，不得给出任何 0 的假象。
        rows_verified = rows_failed = rows_uncertain = rows_skipped = None
    planned_rows = (planned or {}).get("rows") or {}
    rows_planned = (_as_int(planned_rows.get("to_update"))
                    + _as_int(planned_rows.get("to_append")))
    rows_unknown = any(value is None for value in (
        rows_verified, rows_failed, rows_uncertain, rows_skipped))

    return {
        "kind": "execution",
        "contract_version": 1,
        "status": str(status or ""),
        "executed": bool(executed),
        "counts_source": (str(counts_source)
                          or ("apply_plan" if executed else "rejected_before_write")),
        "sheets": {"total": len(sheet_statuses), **counts},
        "rows": {
            "verified": rows_verified,
            "failed": rows_failed,
            "uncertain": rows_uncertain,
            "skipped": rows_skipped,
            "planned": rows_planned,
        },
        "rows_unknown": bool(rows_unknown),
        "proven_no_write": bool(proven_no_write),
        "written_sheets": _as_int(written),
        "failed_sheets": _as_int(failed),
        "note": ("rows.verified 只统计 apply_plan 逐格回读校验通过的行；"
                 "无法证明时为 null（未知），不能用计划数或成功表数顶替"),
        "next_action": str(next_action or ""),
    }


__all__ = ["EXEC_SHEET_KEYS", "execution_summary", "planned_summary"]
