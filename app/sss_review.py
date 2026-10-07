"""闪时送只读核对与人工处置：把"未决记录"变成可核对的证据。

三条入口（对应界面上的三个按钮）：

* :func:`start_review`：**只读核对**。登录、查订单列表、查目标时间窗、保存
  核对快照；**严禁**创建订单 POST 或自动补发。可靠匹配到的未决记录会被
  标记为本地 ``resolved``（因此本地文件会变，这一点必须在界面上说清楚）。
* :func:`pending_views`：列出未决记录（脱敏），供人工判断。
* :func:`resolve_records`：人工处置。``station_present`` 确认站内已有订单、
  ``station_absent`` 确认站内没有订单（解除阻断、允许重跑）、``keep`` 保持阻断。

安全约束（都来自"重复下单会真的多送一份饭"这一后果）：

* 只有**新鲜的、未过期的、且日志指纹未变化**的核对快照才能支撑
  ``station_absent``；快照过期或日志被人改过就必须重新核对。
* 快照里所选记录必须**全部**是 ``station_missing``；有一条读不出来
  （``scan_failed``）就不能解除阻断 —— "查询失败"绝不是"站内没有订单"。
* 处置动作只写本地日志/审计，**永不写云端**、不创建订单。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import tempfile
from urllib.parse import urlencode
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

try:
    from .sss_journal import (UncertainJournalError, batch_key,
                              guarded_close_records, journal_fingerprint,
                              load_journal, mask_contact, pending_records,
                              pending_record_views,
                              resolve_records as _close_resolved)
except ImportError:  # pragma: no cover - 直接执行模块时
    from sss_journal import (UncertainJournalError, batch_key,
                             guarded_close_records, journal_fingerprint,
                             load_journal, mask_contact, pending_records,
                             pending_record_views,
                             resolve_records as _close_resolved)

#: 核对证据有效期（秒）。超过就必须重新核对，不能拿旧证据解除阻断。
REVIEW_TTL_SECONDS = 600.0
#: 人工备注最短长度：强制写下"凭什么判定站内没有这一单"。
RESOLVE_MIN_NOTE = 4

#: 核对分类
STATION_CONFIRMED = "station_confirmed"
STATION_MISSING = "station_missing"
STATION_FOUND_OTHER_DAY = "station_found_other_day"
SCAN_FAILED = "scan_failed"

DECISIONS = ("station_present", "station_absent", "keep")


def review_snapshot_path(journal_path: str | os.PathLike[str]) -> Path:
    """核对快照路径（与日志同目录）。"""
    return Path(str(journal_path) + ".review.json")


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def load_snapshot(journal_path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """读取核对快照；不存在返回 ``None``，损坏抛 :class:`UncertainJournalError`。"""
    target = review_snapshot_path(journal_path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise UncertainJournalError(f"核对快照不可读：{target}（{exc}）") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UncertainJournalError(f"核对快照 JSON 损坏：{target}（{exc}）") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise UncertainJournalError(f"核对快照结构非法：{target}")
    return payload


def _fingerprint_from_record(record: Mapping[str, Any]) -> Any:
    """把日志里存的指纹还原成 :class:`OrderFingerprint`（对账判定要用它）。"""
    try:
        from .sss import OrderFingerprint
    except ImportError:  # pragma: no cover
        from sss import OrderFingerprint
    stored = record.get("fingerprint")
    stored = stored if isinstance(stored, Mapping) else {}
    kwargs = {field: str(stored.get(field) or "")
              for field in OrderFingerprint._fields}
    return OrderFingerprint(**kwargs)


def _review_task(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "identifier": str(record.get("journal_id") or ""),
        "payload": {},
        "fingerprint": _fingerprint_from_record(record),
        "account": str(record.get("account") or ""),
    }


def _scope_records(records: Iterable[Mapping[str, Any]], origin: str) -> list[dict[str, Any]]:
    """只返回明确属于当前平台 origin 的活跃记录。"""
    origin_text = str(origin or "").strip()
    result: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if not origin_text or str(record.get("platform") or "").strip() == origin_text:
            result.append(record)
    return result


#: 只读核对的最大翻页数：超过就说明列表异常（重复页/漏页），按扫描失败处理。
_REVIEW_MAX_PAGES = 60


def _scan_for_review(fetch_json: Callable[[str], dict[str, Any]],
                     tasks: Sequence[Mapping[str, Any]], *,
                     emit: Callable[[str], Any],
                     now: _dt.datetime | None = None
                     ) -> tuple[list[dict[str, Any]], str]:
    """只读扫描站内订单，返回 ``(记录列表, 失败原因)``。

    与"提交前对账"相比，核对的判据更严格：它要支撑"站内确实没有这一单"这个结论，
    因此**不使用时间窗**（``tasks``/``now`` 只保留形参兼容，不参与查询）：

    * 时间窗的正确性依赖服务端语义（窗口真被接受吗？创建时间与送达时间的关系？），
      而这些都无法在本地证明；一旦窗口把订单挡住，"窗口内 0 条"就会被误读成
      "站内没有" —— 那是解除阻断的直接依据，不能建立在未验证的假设上；
    * 全量扫描的代价只是慢（这是只有存在未决记录时才会执行的人工操作），
      换来的是"没读到 = 确实没有"这个可以直接引用的结论；
    * 分页必须有完整性证明：重复页、超过翻页上限、中途读失败、响应结构异常
      都返回失败原因，调用方按 ``scan_failed`` 处理。

    ``tasks`` 有意保留但**不用于过滤**，只用于日志里说明在核对多少条未决记录。
    """
    try:
        from .sss import _ORDER_LIST_PATH, _list_records
    except ImportError:  # pragma: no cover
        from sss import _ORDER_LIST_PATH, _list_records

    del tasks, now
    records: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    page_size = 100
    page_no = 1
    while page_no <= _REVIEW_MAX_PAGES:
        query = urlencode({"pageNo": page_no, "pageSize": page_size,
                           "sortType": 1, "sort": 1})
        try:
            payload = fetch_json(f"{_ORDER_LIST_PATH}?{query}")
        except Exception as exc:  # noqa: BLE001 - 读失败必须显式报告
            return records, f"第 {page_no} 页读取失败：{type(exc).__name__}: {exc}"
        if payload.get("success") is False:
            return records, f"第 {page_no} 页返回失败：{payload.get('message') or ''}"
        try:
            page_records, total = _list_records(payload)
        except Exception as exc:  # noqa: BLE001 - 结构异常也算读不到
            return records, f"第 {page_no} 页响应结构异常：{exc}"
        if not page_records:
            break
        signature = json.dumps(page_records[0], ensure_ascii=False, sort_keys=True,
                               default=str)[:200]
        if signature in seen_pages:
            # 服务端重复返回同一页：继续翻页只会拿到垃圾，按扫描失败处理。
            return records, f"第 {page_no} 页与之前某页重复，分页不可信"
        seen_pages.add(signature)
        records.extend(page_records)
        if len(page_records) < page_size:
            break
        if total is not None and page_no * page_size >= int(total):
            break
        page_no += 1
    else:
        return records, f"翻页超过 {_REVIEW_MAX_PAGES} 页，分页不可信"
    emit(f"只读核对：站内共扫描到 {len(records)} 条记录（无过滤全量）")
    return records, ""


def _classify_against_scan(scanned: Sequence[Mapping[str, Any]],
                           fingerprint: Any, *, account: str = ""
                           ) -> tuple[str, str]:
    """把一条未决记录与"已扫描到的站内记录"比对，给出分类。

    只允许在**明确证明没有**时返回 ``station_missing``：

    * 找到同人 + 同送达时间且字段不冲突的订单 → ``station_confirmed``；
    * 找到同人（同姓名 + 同电话）但时间/日期不同 → ``station_found_other_day``；
    * 同人同时刻的候选记录字段不足或冲突（无法证明是不是这一单）→ 也算
      "找到相似但无法确认"，**不是** missing；
    * 否则 → ``station_missing``（扫描完整性由调用方另行保证）。
    """
    try:
        from .sss import (_fingerprint_compatibility, _order_record_fingerprint,
                          _record_core)
    except ImportError:  # pragma: no cover
        from sss import (_fingerprint_compatibility, _order_record_fingerprint,
                         _record_core)

    core = _record_core(fingerprint)
    identity = (fingerprint.receive_name, fingerprint.receive_phone)
    ambiguous: str = ""
    other_day: str = ""
    for raw in scanned:
        if not isinstance(raw, Mapping):
            continue
        record_fp = _order_record_fingerprint(dict(raw), account=account)
        record_core = _record_core(record_fp)
        same_identity = ((record_fp.receive_name, record_fp.receive_phone)
                         == identity)
        if not same_identity and record_core != core:
            continue
        if record_core == core and all(core):
            conflict, unknown = _fingerprint_compatibility(record_fp, fingerprint)
            if unknown:
                ambiguous = ambiguous or "找到同人同时间的订单，但字段不全，无法确认是否同一单"
                continue
            if not conflict:
                return STATION_CONFIRMED, "站内已找到对应订单"
            ambiguous = ambiguous or "找到同人同时间的订单，但地址等字段不一致"
            continue
        if same_identity:
            when = record_fp.expected_delivery_time or "未提供送达时间"
            other_day = other_day or f"站内存在同一客户但送达时间为 {when} 的订单"
    if ambiguous:
        return STATION_FOUND_OTHER_DAY, ambiguous
    if other_day:
        return STATION_FOUND_OTHER_DAY, other_day
    return STATION_MISSING, "完整扫描确认站内没有对应订单"


def start_review(journal_path: str | os.PathLike[str], *,
                 delivery_date: Any, account: Any,
                 fetch_json: Callable[[str], dict[str, Any]],
                 log: Callable[[str], Any] | None = None,
                 now: _dt.datetime | None = None,
                 ttl_seconds: float = REVIEW_TTL_SECONDS,
                 origin: str = "") -> dict[str, Any]:
    """只读核对：查站内订单、给每条未决记录分类、保存核对快照。

    返回的 ``results`` 每条都是 ``station_confirmed`` / ``station_missing`` /
    ``station_found_other_day`` / ``scan_failed`` 之一。

    **这不是"本地文件完全不变"的入口**：可靠匹配到的记录会被标记为
    ``resolved``（与服务端行为一致，并保留审计痕迹）；仍然缺失或无法确认的记录
    只生成证据，不自动解除重发阻断。
    """
    emit = log or (lambda _message: None)
    journal = Path(journal_path)
    key = batch_key(delivery_date, "", account)
    payload = load_journal(journal)
    records = _scope_records(pending_records(payload.get("records", []), key), origin)
    snapshot: dict[str, Any] = {
        "journal_path": str(journal),
        "batch_key": key,
        "delivery_date": str(delivery_date or ""),
        "account": mask_contact(account),
        # 作用域指纹：账号不以明文落盘，但仍能证明"快照与当前账号+平台一致"。
        "scope_fingerprint": _scope_fingerprint(key, origin),
        "platform_origin": str(origin or ""),
        "created_at": (now or _dt.datetime.now()).isoformat(timespec="seconds"),
        "ttl_seconds": int(round(max(1.0, float(ttl_seconds)))),
        "expires_at": ((now or _dt.datetime.now())
                       + _dt.timedelta(seconds=max(1.0, float(ttl_seconds)))
                       ).isoformat(timespec="seconds"),
        "journal_fingerprint": journal_fingerprint(journal),
        "queried_cloud": True,
        "read_only": True,
        "cloud_write": False,
        "results": [],
    }
    if not records:
        emit("没有需要核对的未决记录")
        snapshot["results"] = []
        _atomic_write(review_snapshot_path(journal), snapshot)
        return {"ok": True, **snapshot, "confirmed": 0, "missing": 0,
                "other_day": 0, "scan_failed": 0}

    tasks = [_review_task(record) for record in records]
    emit(f"只读核对：站内查询 {len(tasks)} 条未决记录对应的订单…")
    confirmed_ids: set[str] = set()
    results: list[dict[str, Any]] = []

    scanned, failure = _scan_for_review(fetch_json, tasks, emit=emit, now=now)
    for record, task in zip(records, tasks):
        identifier = task["identifier"]
        fingerprint = task["fingerprint"]
        if failure:
            # 扫描不完整（分页异常/重复页/读失败）：一律"无法判定"，
            # 绝不能因为"没读到"而说"站内没有这一单"。
            classification, reason = SCAN_FAILED, f"读取失败，无法判定：{failure}"
        else:
            classification, reason = _classify_against_scan(
                scanned, fingerprint, account=str(record.get("account") or ""))
            if classification == STATION_CONFIRMED:
                confirmed_ids.add(identifier)
        results.append({
            "journal_id": identifier,
            "classification": classification,
            "reason": reason,
            "name": fingerprint.receive_name,
            "phone": mask_contact(fingerprint.receive_phone),
            "delivery_time": fingerprint.expected_delivery_time,
            "error": str(record.get("error") or "")[:200],
        })

    if confirmed_ids:
        # 与服务端一致：可靠匹配到的记录在核对过程中就标记为本地已确认。
        _close_resolved(journal, key, sorted(confirmed_ids),
                        note="站内只读对账确认")
        emit(f"站内已确认 {len(confirmed_ids)} 条，已标记为本地已确认")
    snapshot["results"] = results
    snapshot["journal_fingerprint"] = journal_fingerprint(journal)
    _atomic_write(review_snapshot_path(journal), snapshot)

    counts: dict[str, int] = {}
    for item in results:
        counts[item["classification"]] = counts.get(item["classification"], 0) + 1
    return {"ok": True, **snapshot, "counts": counts,
            "confirmed": counts.get(STATION_CONFIRMED, 0),
            "missing": counts.get(STATION_MISSING, 0),
            "other_day": counts.get(STATION_FOUND_OTHER_DAY, 0),
            "scan_failed": counts.get(SCAN_FAILED, 0)}


def pending_views(journal_path: str | os.PathLike[str], *,
                  delivery_date: Any = "", account: Any = "",
                  origin: str = "") -> dict[str, Any]:
    """未决记录列表（脱敏），**分组**返回。日志损坏时抛错，绝不返回"没有未决"。

    为什么必须分组：只看"当前日期 + 当前账号 + 当前平台"会把仍然造成阻断的记录
    藏起来 —— 换成别的账号/网址之后，那些记录既看不到、也没法核对，用户会以为
    "没有未决"，然后要么白等、要么拿当前平台的证据去处置它们（后者是明确禁止的）。

    三组（都脱敏、都不改任何文件）：

    * ``current``：当前作用域里仍活跃的记录 —— 只有这些能在当前账号/平台处置；
    * ``other_scope``：**同一天**但账号或平台不同的活跃记录（它们也在阻断今天的
      运行），必须切回原账号/原网址才能核对与处置；
    * ``history``：其它日期的活跃记录 + 最近已结束（resolved/discarded）的记录，
      只作审计可见性，不影响当前批次。
    """
    key = batch_key(delivery_date, "", account) if delivery_date else None
    all_records = [record for record in load_journal(journal_path).get("records", [])
                   if isinstance(record, dict)]
    views = pending_record_views(journal_path, key)
    by_id = {str(item.get("journal_id") or ""): item for item in views["records"]}

    def _view(record: Mapping[str, Any]) -> dict[str, Any]:
        item = by_id.get(str(record.get("journal_id") or ""))
        if item is not None:
            return item
        # 已被过滤掉的（跨范围/历史）记录也要有脱敏视图，才能被用户发现。
        fingerprint = (record.get("fingerprint")
                       if isinstance(record.get("fingerprint"), dict) else {})
        return {
            "journal_id": str(record.get("journal_id") or ""),
            "identifier": str(record.get("identifier") or ""),
            "sheet": str(record.get("sheet") or ""),
            "batch_id": str(record.get("batch_id") or ""),
            "delivery_date": str(record.get("delivery_date") or ""),
            "status": str(record.get("status") or "unresolved"),
            "error": str(record.get("error") or "")[:300],
            "created_at": str(record.get("created_at") or ""),
            "name": str(fingerprint.get("receive_name") or ""),
            "phone": mask_contact(fingerprint.get("receive_phone")),
            "delivery_time": str(fingerprint.get("expected_delivery_time") or ""),
            "door_num": str(fingerprint.get("door_num") or ""),
            "address": str(fingerprint.get("address_detail") or ""),
            "goods_name": str(fingerprint.get("goods_name") or ""),
            "account": mask_contact(record.get("account")),
            "platform": str(record.get("platform") or ""),
            "source": str(record.get("source") or ""),
        }

    current_ids = {str(item.get("journal_id") or "") for item in views["records"]}
    if origin:
        allowed = {str(record.get("journal_id") or "")
                   for record in _scope_records(
                       pending_records(all_records, key), origin)}
        current_ids &= allowed
    same_day_ids: set[str] = set()
    date_text = str(delivery_date or "")
    for record in pending_records(all_records, None):
        record_date = str(record.get("delivery_date") or "")
        if not record_date:
            record_date = str(record.get("batch_key") or "").split("|")[0]
        if date_text and record_date == date_text:
            same_day_ids.add(str(record.get("journal_id") or ""))
    active_ids = {str(record.get("journal_id") or "")
                  for record in pending_records(all_records, None)}

    current = [item for item in views["records"]
               if str(item.get("journal_id") or "") in current_ids]
    other_scope = [_view(record) for record in all_records
                   if str(record.get("journal_id") or "") in same_day_ids
                   and str(record.get("journal_id") or "") not in current_ids]
    history = [_view(record) for record in all_records
               if str(record.get("journal_id") or "") not in same_day_ids
               or str(record.get("journal_id") or "") not in active_ids]
    groups = {
        "current": current,
        "other_scope": other_scope,
        "history": history[-20:],      # 历史只给最近若干条，避免列表无限增长
    }
    return {
        "ok": True,
        "read_only": True,
        "journal_unreadable": False,
        "records": current,             # 兼容旧字段：当前作用域可处置的那些
        "groups": groups,
        "group_counts": {name: len(items) for name, items in groups.items()},
        "counts": views["counts"],
        "scope": {"delivery_date": date_text, "account": mask_contact(account),
                  "platform_origin": str(origin or "")},
        "journal_path": views["path"],
        "fingerprint": views["fingerprint"],
    }


def _scope_fingerprint(key: Any, origin: Any) -> str:
    """核对作用域指纹：批次键（送达日 + 规范化账号）+ 平台 origin 的不可逆摘要。

    快照必须绑定"当时核对的是哪个账号、哪个平台"：换账号或换网址之后，旧证据
    对新作用域**无效**（那边的订单根本没查过），必须重新核对。
    """
    material = f"{str(key or '')}\u0000{str(origin or '')}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _guard_result(problem: tuple[str, str] | None) -> tuple[bool, str, str]:
    """把 ``_scope_guard`` 的返回值转成 ``guarded_close_records`` 的 guard 形态。"""
    if problem is None:
        return True, "", ""
    return False, problem[0], problem[1]


def _scope_guard(payload: Mapping[str, Any], *, key: str, origin: str,
                 ids: Sequence[str]) -> tuple[str, str] | None:
    """锁内作用域与状态校验：返回 ``(错误码, 说明)``，通过时返回 ``None``。

    作用域 = 送达日 + 规范化账号 + 平台 origin；三者任一不符都不允许在这里处置
    （否则可能解除别处的阻断）。已经结束的记录单独给 ``record_not_pending``，
    让用户知道"这条不需要再处置"，而不是含糊地报作用域不符。
    """
    all_records = [record for record in payload.get("records", [])
                   if isinstance(record, dict)]
    known_ids = {str(record.get("journal_id") or "") for record in all_records}
    unknown = [item for item in ids if item not in known_ids]
    if unknown:
        return ("record_not_found",
                f"有 {len(unknown)} 条所选记录在未决日志里不存在")
    closed_ids = {str(record.get("journal_id") or "") for record in all_records
                  if str(record.get("status") or "") in ("resolved", "discarded")}
    scoped_ids = {str(record.get("journal_id") or "")
                  for record in _scope_records(pending_records(all_records, key),
                                               origin)}
    out_of_scope = [item for item in ids
                    if item not in scoped_ids and item not in closed_ids]
    if out_of_scope:
        return ("cross_scope_record",
                "所选记录不在当前作用域内（送达日、账号或平台地址与核对时不同）；"
                "在当前账号/网址下核对过它们也没有任何意义")
    already_closed = [item for item in ids if item not in scoped_ids]
    if already_closed:
        return ("record_not_pending",
                f"有 {len(already_closed)} 条所选记录已经不在待处理状态"
                f"（已被核对确认或已处置），无需再处置")
    return None


def fingerprint_of_records(payload: Mapping[str, Any]) -> str:
    """对内存里的 journal payload 计算指纹（与 :func:`journal_fingerprint` 同算法）。

    锁内校验必须用**同一份**即将被修改的数据算指纹：如果再去读一次文件，读到的
    可能已经是别人改过的版本，校验就落空了。
    """
    records = [record for record in payload.get("records", [])
               if isinstance(record, dict)]
    blob = json.dumps(records, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _fresh_snapshot(journal_path: Path, snapshot: Mapping[str, Any],
                    *, key: Any = None, origin: Any = None,
                    payload: Mapping[str, Any] | None = None) -> str:
    """校验快照新鲜度与日志指纹；不满足返回拒绝原因（空 = 通过）。"""
    created = str(snapshot.get("created_at") or "")
    ttl = float(snapshot.get("ttl_seconds") or REVIEW_TTL_SECONDS)
    try:
        created_at = _dt.datetime.fromisoformat(created)
    except ValueError:
        return "核对快照时间无法解析，请重新核对"
    age = (_dt.datetime.now() - created_at).total_seconds()
    if age > ttl:
        return (f"核对证据已过期（{int(age)} 秒前，有效期 {int(ttl)} 秒），"
                f"请重新做「只读核对」")
    if key is not None or origin is not None:
        # 账号或平台换过之后，旧证据对新作用域无效：那份证据查的是另一边的订单。
        recorded = str(snapshot.get("scope_fingerprint") or "")
        if recorded:
            if recorded != _scope_fingerprint(key, origin):
                return ("核对证据是在另一个账号或平台地址下取得的，"
                        "对当前账号/网址无效，请重新做「只读核对」")
        elif str(snapshot.get("platform_origin") or "") != str(origin or ""):
            # 兼容没有 scope_fingerprint 的旧快照：退回 origin 比对。
            return ("核对证据是在另一个平台地址下取得的，"
                    "对当前网址无效，请重新做「只读核对」")
    try:
        current = (fingerprint_of_records(payload) if payload is not None
                   else journal_fingerprint(journal_path))
    except UncertainJournalError as exc:
        return f"未决日志不可读：{exc}"
    if current != str(snapshot.get("journal_fingerprint") or ""):
        return "未决记录在核对之后发生了变化，请重新做「只读核对」"
    return ""


def resolve_records(journal_path: str | os.PathLike[str], *,
                    delivery_date: Any, account: Any,
                    decision: str, record_ids: Iterable[str],
                    confirm: str = "", note: str = "",
                    actor: str = "本机用户", origin: str = "") -> dict[str, Any]:
    """人工处置未决记录；**永不写云端、不创建订单**。

    ``decision``：

    * ``station_present``：站内已有这些订单 → 标记本地已确认（解除阻断）；
    * ``station_absent``：人工确认站内没有 → 解除阻断，**下一次主动运行才可能
      真的发单**（确认文案必须说明这个后果）；
    * ``keep``：保持阻断，只记录备注（等价于 no-op）。

    ``station_absent`` 必须同时满足：有针对这些记录的新鲜核对快照、日志指纹未变、
    所选记录全部是 ``station_missing``、``confirm`` 与 ``decision`` 一致、
    备注至少 4 个字符。
    """
    journal = Path(journal_path)
    key = batch_key(delivery_date, "", account)
    decision_text = str(decision or "").strip().lower()
    confirm_text = str(confirm or "").strip()
    note_text = str(note or "").strip()
    ids = sorted({str(item).strip() for item in record_ids if str(item or "").strip()})

    if decision_text not in DECISIONS:
        return _failure("decision_invalid", "处置动作不合法",
                        "从 station_present / station_absent / keep 里选")
    if not ids:
        return _failure("record_ids_required", "必须显式选择要处置的未决记录",
                        "先用「只读核对」列出记录并勾选")
    if confirm_text != decision_text:
        return _failure("confirm_mismatch", "确认文本必须与处置动作完全一致",
                        f"把 confirm 原样填成 {decision_text}")
    if decision_text != "keep" and len(note_text) < RESOLVE_MIN_NOTE:
        return _failure("note_too_short", f"人工备注至少 {RESOLVE_MIN_NOTE} 个字符",
                        "写下你凭什么这样判定后再提交")

    # 作用域与状态校验**放到数据锁内**（见 _state_guard）：先在锁外读一遍
    # 再在锁内写，中间那个窗口足以让新增/被改动的记录被旧证据"顺带解除"。
    if decision_text == "keep":
        # keep 是 no-op（与服务端一致）：不改日志，只返回审计结果。
        return {
            "ok": True, "status": "kept", "code": "", "reason_code": "kept",
            "reason": "已保持阻断（未改动任何记录）",
            "cloud_write": False, "changed": False, "note": note_text,
            "operations": [], "record_ids": ids,
        }

    if decision_text == "station_present":
        closed, code, message = guarded_close_records(
            journal, key, ids, status="resolved",
            reason=note_text or "人工确认站内已有订单",
            note=note_text, actor=actor,
            guard=lambda payload: _guard_result(_scope_guard(
                payload, key=key, origin=origin, ids=ids)))
        if code:
            return _failure(code, message, "刷新未决记录后重新选择")
        return {
            "ok": True, "status": "resolved", "code": "",
            "reason_code": "station_present", "changed": bool(closed),
            "reason": f"已确认 {closed} 条站内订单并解除阻断",
            "cloud_write": False, "note": note_text, "record_ids": ids,
            "operations": [{"journal_id": item, "status": "resolved"} for item in ids],
        }

    # station_absent：解除阻断前必须先有新鲜、可信的核对证据。
    # 证据校验与状态变更在**同一把数据锁内**完成（R04）。
    try:
        snapshot = load_snapshot(journal)
    except UncertainJournalError as exc:
        return _failure("review_snapshot_unreadable", str(exc), "重新做只读核对")
    if snapshot is None:
        return _failure("review_required", "还没有只读核对结果，不能解除阻断",
                        "先点「只读核对」，确认站内确实没有这些订单")

    def _absent_guard(payload: dict[str, Any]) -> tuple[bool, str, str]:
        """锁内证据校验：新鲜度 → 作用域 → 覆盖关系 → 分类全部为 missing。"""
        scope_problem = _scope_guard(payload, key=key, origin=origin, ids=ids)
        if scope_problem:
            return False, scope_problem[0], scope_problem[1]
        stale_reason = _fresh_snapshot(journal, snapshot, key=key, origin=origin,
                                      payload=payload)
        if stale_reason:
            return False, "review_stale", stale_reason
        by_id = {str(item.get("journal_id") or ""): item
                 for item in snapshot.get("results") or [] if isinstance(item, dict)}
        selected = [by_id.get(item) for item in ids]
        if any(item is None for item in selected):
            return (False, "record_not_in_review",
                    "所选记录不在这次核对结果里（记录可能在核对之后新增）")
        bad = [item for item in selected
               if str(item.get("classification")) != STATION_MISSING]
        if bad:
            kinds = sorted({str(item.get("classification")) for item in bad})
            return (False, "station_not_absent",
                    "所选记录里有的不是「站内确认没有」（" + "、".join(kinds) + "）："
                    "查询失败或找到相似订单都不能当成没下单")
        return True, "", ""

    closed, code, message = guarded_close_records(
        journal, key, ids, status="discarded",
        reason="人工确认站内没有对应订单，解除阻断",
        note=note_text, actor=actor, guard=_absent_guard)
    if code:
        return _failure(code, message, "重新核对，或改用 station_present / keep")
    return {
        "ok": True, "status": "resolved", "code": "",
        "reason_code": "station_absent", "changed": bool(closed),
        "reason": (f"已人工确认站内没有这 {closed} 条订单并解除阻断；"
                   f"本次处置没有发送任何下单请求，下一次点「开始下单」才会真正下单"),
        "cloud_write": False, "note": note_text, "record_ids": ids,
        "operations": [{"journal_id": item, "status": "discarded"} for item in ids],
    }


def _failure(code: str, reason: str, next_action: str) -> dict[str, Any]:
    return {
        "ok": False, "status": "rejected", "code": str(code),
        "reason": str(reason or code), "reason_code": "", "next_action": str(next_action),
        "cloud_write": False, "changed": False, "operations": [], "record_ids": [],
    }


__all__ = [
    "DECISIONS",
    "RESOLVE_MIN_NOTE",
    "REVIEW_TTL_SECONDS",
    "SCAN_FAILED",
    "STATION_CONFIRMED",
    "STATION_FOUND_OTHER_DAY",
    "STATION_MISSING",
    "load_snapshot",
    "pending_views",
    "resolve_records",
    "review_snapshot_path",
    "start_review",
]
