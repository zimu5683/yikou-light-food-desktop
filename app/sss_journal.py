"""闪时送未决订单的**权威日志**：只要下单 POST 已发出但结果未知，就必须留痕。

这是桌面端最重要的一条安全边界。平台接口没有客户端幂等键，所以"至少一次提交 +
站内对账"是唯一可行语义；一旦客户端拿不到确定响应（超时、断线、进程被杀），
**重复提交就会真的多下一单**（客户多收一份饭）。因此：

* 每个 POST 在**发出之前**先把意图写成 ``inflight``；
* 拿不到确定响应就更新为 ``unresolved``（含明确 success 的也要等到站内对账确认）；
* 同一批次（``送达日|规范化账号``）只要还有 ``inflight``/``unresolved``，
  再次运行就**不许自动发 POST**（换名单来源、重启程序、再点一次"开始下单"
  都不能绕过），必须先做只读核对或人工处置；
* 日志不可读/损坏时**失败关闭**：绝不把它当成"没有未决记录"；
* 日志路径是**固定的机器级位置**，不由网址/账号/名单来源/Excel 路径推导 ——
  否则改一下配置就能换到另一个文件、看不到原来的未决记录。

键的设计（与参考实现一致）：``批次键 = 送达日|规范化账号``。**名单来源不参与**
—— 切换 ``wps``/``excel`` 不能解除同一批次的未决记录。平台 origin 单独记录，
用于识别"同一批次换了个平台地址"这种跨作用域情况：那种记录不会被当成当前批次，
但**也不会被静默丢弃**，而是继续阻断并要求人工处置。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterable, Mapping

try:
    from .sss_url import SssUrlConfigError, canonical_sss_origin
    from .wps_atomicio import FileLock, batch_lock_path_for, lock_path_for
except ImportError:  # pragma: no cover - 直接执行模块时
    from sss_url import SssUrlConfigError, canonical_sss_origin
    from wps_atomicio import FileLock, lock_path_for

DEFAULT_SSS_URL = "https://sssplusnew.zhuopaikeji.com/complete/takeout"

#: 未决记录状态：``inflight`` 已发出待确认、``unresolved`` 结果未知、
#: ``resolved`` 站内只读对账确认、``discarded`` 有证据证明未落单。
ACTIVE_STATUSES = frozenset({"inflight", "unresolved"})
JOURNAL_VERSION = 1
_SUPPORTED_VERSIONS = frozenset({1})

_MASK_PHONE_RE = re.compile(r"^(\d{3})\d{4}(\d{4})$")
_ACCOUNT_WHITESPACE_RE = re.compile(r"\s+")

_JOURNAL_LOCKS: dict[str, Lock] = {}
_JOURNAL_LOCKS_GUARD = Lock()


class UncertainJournalError(RuntimeError):
    """未决日志不可用/损坏，或平台身份无法规范化；调用方必须失败关闭。"""


# ----------------------------------------------------------------------
# 规范化：账号 / 批次键 / 平台 origin
# ----------------------------------------------------------------------

def normalise_account(value: Any) -> str:
    """规范化账号写法，只合并业务上明确等价的差异。

    规则刻意保持保守：

    - NFKC 统一全角数字/字符；
    - 删除所有空白（含全角空格），例如 ``187 5818 7837`` → ``18758187837``；
    - 不 lowercase、不做 int 转换、不丢前导零、不删其它标点，避免把不同账号合并。
    """
    if value is None:
        return ""
    return _ACCOUNT_WHITESPACE_RE.sub("", unicodedata.normalize("NFKC", str(value)))


def batch_key(delivery_date: Any, source: Any = "", account: Any = "") -> str:
    """批次阻断键：``送达日|规范化账号``。

    ``source`` 只是可变配置（excel/wps），**不能**作为绕过阻断的条件，因此刻意
    不进入键；保留形参只为兼容旧调用签名（旧版会传三个参数）。
    """
    return "|".join((str(delivery_date or ""), normalise_account(account)))


def legacy_batch_key(delivery_date: Any, source: Any, account: Any) -> str:
    """旧版三段键，仅供兼容读取历史日志。"""
    return "|".join((str(delivery_date or ""), str(source or ""), str(account or "")))


def _record_matches_batch(record: Mapping[str, Any], key: str) -> bool:
    """判断日志记录是否属于当前批次，兼容历史的三段键与旧账号写法。"""
    raw_batch = str(record.get("batch_key") or "")
    key_text = str(key or "")
    if raw_batch and raw_batch == key_text:
        return True
    parts = key_text.split("|")
    if len(parts) == 2:
        date, account = parts[0], normalise_account(parts[1])
    elif len(parts) == 3:  # 调用方仍传旧键
        date, account = parts[0], normalise_account(parts[2])
    else:
        return False
    if not date or not account:
        return False
    stored_date = str(record.get("delivery_date") or "")
    stored_account = normalise_account(record.get("account"))
    if stored_date and stored_account and stored_date == date and stored_account == account:
        return True
    old_parts = raw_batch.split("|")
    return (len(old_parts) == 3 and old_parts[0] == date
            and normalise_account(old_parts[2]) == account)


def platform_origin(config: Any = None, *, url: str | None = None) -> str:
    """实际 API 请求目标的规范 origin；非规范网址直接失败关闭。

    先按 :mod:`.sss_url` 严格校验，再取规范 origin。这样"尾点域名、默认端口写法、
    大小写"等等价写法会归一到**同一个** origin，不会凭空多出一个作用域；
    而非法写法（缺协议、非 ASCII 域名等）直接拒绝，绝不为它另建一个 scope ——
    那正是"换个网址写法就绕开未决记录"的入口。

    ``url`` 可显式传入**已冻结**的配置字符串（任务启动时读一次），
    这样执行期间 ``config.sss_url`` 被改写也不会改变本次运行的作用域。
    """
    if url is None:
        raw = (str(getattr(config, "sss_url", "") or "").strip()
               if config is not None else "")
    else:
        raw = str(url).strip()
    if not raw:
        raw = DEFAULT_SSS_URL
    try:
        return canonical_sss_origin(raw)
    except SssUrlConfigError as exc:
        raise UncertainJournalError(
            f"{exc}；已拒绝读写未决状态（不会发送任何闪时送请求）") from exc


def scope_key(config: Any = None, *, origin: str | None = None,
              account: Any = None) -> str:
    """权威安全状态身份：平台 origin + 规范化账号。

    刻意**不包含** ``sss_excel_path`` / ``sss_order_source`` / 日志路径，
    这样换名单来源、换本地文件都不会改变同一平台/账号的未确认状态。
    """
    if origin is None:
        origin_text = platform_origin(config)
    else:
        origin_text = str(origin).strip()
        if not origin_text:
            raise UncertainJournalError(
                "缺少已冻结的平台 origin，无法计算权威状态身份；已阻断提交")
        try:
            canonical_sss_origin(origin_text)
        except SssUrlConfigError as exc:
            raise UncertainJournalError(str(exc)) from exc
    account_value = account if account is not None else (
        getattr(config, "sss_account", "") if config is not None else "")
    return f"{origin_text}|{normalise_account(account_value)}"


# ----------------------------------------------------------------------
# 路径与锁
# ----------------------------------------------------------------------

def default_uncertain_path(config: Any = None) -> Path:
    """权威未决日志路径：显式配置 → 环境变量 → 用户数据目录。

    **刻意不从网址/账号/名单来源/Excel 路径推导**：那些都是用户可改的配置，
    一旦参与文件名，改一下配置就能"换一个日志文件"、看不到原来的未决记录。
    """
    explicit = ""
    if config is not None:
        explicit = str(getattr(config, "sss_uncertain_path", "") or "").strip()
    if not explicit:
        explicit = os.environ.get("YIKOU_SSS_UNCERTAIN_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    try:
        from .config import user_data_dir
    except ImportError:  # pragma: no cover - 直接执行模块时
        from config import user_data_dir
    return user_data_dir() / "sss_uncertain.json"


def journal_lock_path(path: str | os.PathLike[str]) -> Path:
    """**数据锁**（短）：保护单次日志读改写。"""
    return lock_path_for(path)


def batch_lock_path(path: str | os.PathLike[str]) -> Path:
    """**批次锁**（长）：同一时刻只允许一个下单批次提交。

    与数据锁分开的理由与锁顺序见 :func:`app.wps_atomicio.batch_lock_path_for`：
    批次锁会被持有整个提交过程（其间还要短时写日志），两者共用一个文件就只能靠
    同线程重入避免死锁 —— 那是隐含假设，不是设计。
    """
    return batch_lock_path_for(path)


def _journal_lock(target: Path) -> Lock:
    """进程内按路径的线程锁（跨进程由 :class:`FileLock` 负责）。"""
    with _JOURNAL_LOCKS_GUARD:
        return _JOURNAL_LOCKS.setdefault(str(target), Lock())


def batch_submission_lock(path: str | os.PathLike[str], key: str, *,
                          timeout: float = 0.0) -> FileLock:
    """取"当前批次"的跨进程排他锁；拿不到就抛 :class:`UncertainJournalError`。

    两个窗口/两个进程同时点"开始下单"会产生**同一批订单的两次 POST**，
    这是重复下单最直接的来源，因此这里默认不等待（``timeout=0``）。
    """
    lock = FileLock(batch_lock_path(path), timeout=max(0.0, float(timeout)))
    try:
        lock.acquire()
    except TimeoutError as exc:
        raise UncertainJournalError(
            "已有闪时送下单批次正在运行（另一个窗口/进程），拒绝并发提交") from exc
    return lock


# ----------------------------------------------------------------------
# 读写
# ----------------------------------------------------------------------

def _fsync_parent_dir(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    """临时文件 + fsync + 原子替换；任何一步失败都不留下半文件。"""
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
        _fsync_parent_dir(path.parent)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _validate_journal_payload(payload: Any, target: Path) -> dict[str, Any]:
    """校验日志结构；任何损坏都抛 :class:`UncertainJournalError`（失败关闭）。"""
    if not isinstance(payload, dict):
        raise UncertainJournalError(f"未决日志根节点不是 JSON 对象：{target}")
    version = payload.get("version", JOURNAL_VERSION)
    if not isinstance(version, int) or version not in _SUPPORTED_VERSIONS:
        raise UncertainJournalError(f"未决日志版本不受支持：{target}")
    records = payload.get("records")
    if not isinstance(records, list):
        raise UncertainJournalError(f"未决日志缺少 records 列表：{target}")
    for record in records:
        if not isinstance(record, dict):
            raise UncertainJournalError(f"未决日志记录结构非法：{target}")
        if not str(record.get("journal_id") or ""):
            raise UncertainJournalError(f"未决日志记录缺少 journal_id：{target}")
        status = str(record.get("status") or "unresolved")
        if status not in ACTIVE_STATUSES | {"resolved", "discarded"}:
            raise UncertainJournalError(
                f"未决日志记录状态不受支持（{status}）：{target}")
    payload.setdefault("version", JOURNAL_VERSION)
    return payload


def load_journal(path: str | os.PathLike[str]) -> dict[str, Any]:
    """读取日志；文件不存在返回空日志，损坏/不可读抛错（绝不静默当空）。"""
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": JOURNAL_VERSION, "records": []}
    except (OSError, UnicodeDecodeError) as exc:
        raise UncertainJournalError(f"未决日志不可读：{target}（{exc}）") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UncertainJournalError(f"未决日志 JSON 损坏：{target}（{exc}）") from exc
    return _validate_journal_payload(payload, target)


def _is_active(record: Mapping[str, Any]) -> bool:
    return str(record.get("status") or "unresolved") in ACTIVE_STATUSES


def pending_records(records: Iterable[Mapping[str, Any]],
                    key: str | None = None) -> list[dict[str, Any]]:
    """筛选仍活跃的记录；``key`` 非空时用兼容匹配（忽略 source 变化）。"""
    pending: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or not _is_active(record):
            continue
        if key is not None and not _record_matches_batch(record, key):
            continue
        pending.append(record)
    return pending


def _journal_id(entry: Mapping[str, Any]) -> str:
    """记录的唯一 id：优先用稳定请求 id，否则用"批次+行位+指纹"派生。"""
    explicit = str(entry.get("journal_id") or "").strip()
    if explicit:
        return explicit
    material = "|".join(str(part or "") for part in (
        entry.get("batch_id"), entry.get("sheet"), entry.get("identifier"),
        entry.get("client_request_id"),
        json.dumps(entry.get("fingerprint") or {}, sort_keys=True, default=str),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def append_records(path: str | os.PathLike[str], key: str,
                   entries: Iterable[Mapping[str, Any]],
                   *, meta: Mapping[str, Any] | None = None,
                   now: _dt.datetime | None = None) -> int:
    """把本轮"已发送未知"的任务写入日志；失败必须由调用方停止 POST。"""
    target = Path(path)
    with _journal_lock(target):
        with FileLock(journal_lock_path(target), timeout=30.0):
            payload = load_journal(target)
            info = dict(meta or {})
            timestamp = (now or _dt.datetime.now()).isoformat(timespec="seconds")
            deduped: dict[str, dict[str, Any]] = {}
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                record = {
                    "journal_id": _journal_id(entry),
                    "identifier": str(entry.get("identifier") or ""),
                    "client_request_id": str(entry.get("client_request_id") or ""),
                    "sheet": str(entry.get("sheet") or ""),
                    "batch_id": str(entry.get("batch_id") or ""),
                    "batch_key": key,
                    "delivery_date": str(info.get("delivery_date") or ""),
                    "account": normalise_account(
                        info.get("account") or entry.get("account") or ""),
                    "platform": str(info.get("platform") or entry.get("platform") or ""),
                    "source": str(info.get("source") or ""),
                    "fingerprint": dict(entry.get("fingerprint") or {}),
                    "error": str(entry.get("error") or ""),
                    "status": str(entry.get("status") or "unresolved"),
                    "created_at": timestamp,
                    "batch_started_at": info.get("batch_started_at"),
                }
                deduped[record["journal_id"]] = record
            new_records = list(deduped.values())
            new_ids = {record["journal_id"] for record in new_records}
            # 只替换待写 entry 同 journal_id 的旧活跃记录；别的未决任务
            # （另一个轮次/另一个 worker）必须保留，否则跨运行阻断会漏单。
            kept: list[dict[str, Any]] = []
            for record in payload.get("records", []):
                if not isinstance(record, dict):
                    continue
                if (_record_matches_batch(record, key) and _is_active(record)
                        and str(record.get("journal_id") or "") in new_ids):
                    continue
                kept.append(record)
            payload["version"] = JOURNAL_VERSION
            payload["records"] = kept + new_records
            _atomic_write(target, payload)
            return len(new_records)


def guarded_close_records(path: str | os.PathLike[str], key: str,
                          identifiers: Iterable[str], *, status: str,
                          reason: str, note: str = "", actor: str = "",
                          guard: Callable[[dict[str, Any]], tuple[bool, str, str]]
                          | None = None) -> tuple[int, str, str]:
    """**在数据锁内**校验再关闭记录；返回 ``(关闭数量, 错误码, 错误说明)``。

    为什么必须有这个入口：人工处置的判据（核对快照是否新鲜、日志指纹是否变化、
    所选记录是否仍是"站内确认没有"）如果先读、再写，中间就有一个会被其它进程/
    窗口插进来的窗口 —— 那个窗口里新增或改动过的记录可能被一份旧证据"顺带解除"。

    因此：读、校验、写全部放在**同一把数据锁**内，``guard`` 收到的就是即将被
    修改的那份数据（``payload``）；``guard`` 返回 ``(False, code, message)`` 时
    一个字都不写。
    """
    target = Path(path)
    wanted = {str(identifier) for identifier in identifiers}
    if not wanted:
        return 0, "", ""
    with _journal_lock(target):
        with FileLock(journal_lock_path(target), timeout=30.0):
            payload = load_journal(target)
            if guard is not None:
                ok, code, message = guard(payload)
                if not ok:
                    return 0, str(code), str(message)
            closed = 0
            timestamp = _dt.datetime.now().isoformat(timespec="seconds")
            for record in payload.get("records", []):
                if not isinstance(record, dict):
                    continue
                if not _record_matches_batch(record, key) or not _is_active(record):
                    continue
                record_id = str(record.get("journal_id") or "")
                task_id = str(record.get("identifier") or "")
                if record_id in wanted or task_id in wanted:
                    record["status"] = status
                    record[f"{status}_at"] = timestamp
                    record[f"{status}_reason"] = str(reason or "")
                    if note:
                        record[f"{status}_note"] = str(note)
                    if actor:
                        record[f"{status}_by"] = str(actor)
                    closed += 1
            if closed:
                _atomic_write(target, payload)
            return closed, "", ""


def _close_records(path: str | os.PathLike[str], key: str,
                   identifiers: Iterable[str], *, status: str,
                   reason: str, note: str = "", actor: str = "") -> int:
    """把活跃记录标记为终态（``resolved`` / ``discarded``）；保留审计痕迹。"""
    closed, _code, _message = guarded_close_records(
        path, key, identifiers, status=status, reason=reason, note=note, actor=actor)
    return closed


def resolve_records(path: str | os.PathLike[str], key: str,
                    identifiers: Iterable[str], *, note: str = "",
                    actor: str = "") -> int:
    """只读对账确认站点已有订单后，把记录标记为 ``resolved``。"""
    return _close_records(path, key, identifiers, status="resolved",
                          reason=(str(note or "").strip() or "站内只读对账确认"),
                          note=note, actor=actor)


def discard_records(path: str | os.PathLike[str], key: str,
                    identifiers: Iterable[str],
                    reason: str = "明确未发送/明确失败，有充分证据无需重试",
                    *, note: str = "", actor: str = "") -> int:
    """有充分证据证明 POST 未落单时关闭记录，避免误阻断后续运行。

    只用于明确 401/余额不足/显式 ``success=false``/从未派发等"未发送"证据。
    """
    return _close_records(path, key, identifiers, status="discarded",
                          reason=reason, note=note, actor=actor)


# ----------------------------------------------------------------------
# 阻断判定
# ----------------------------------------------------------------------

def blocking_state(path: str | os.PathLike[str], *, delivery_date: Any,
                   account: Any, origin: str) -> dict[str, Any]:
    """开始下单前的只读闸门：返回是否阻断、原因与相关记录。

    三类命中：

    * ``unresolved_batch``：同批次（送达日 + 规范化账号）且同平台地址仍有未决记录；
    * ``cross_scope``：**同一天**但账号或平台地址不同的活跃记录 —— 不能当成当前
      批次，但也不能静默丢弃（换个账号/网址不能把旧问题藏起来）；
    * 其它日期的活跃记录只计入 ``other_pending``（不阻断今天，但要在界面上可见）。

    日志不可读时抛 :class:`UncertainJournalError`（调用方必须停止）。
    """
    key = batch_key(delivery_date, "", account)
    target = Path(path)
    payload = load_journal(target)
    records = [record for record in payload.get("records", [])
               if isinstance(record, dict)]
    same_scope: list[dict[str, Any]] = []
    cross_scope: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    origin_text = str(origin or "")
    date_text = str(delivery_date or "")
    for record in records:
        if not _is_active(record):
            continue
        record_platform = str(record.get("platform") or "")
        platform_matches = (not record_platform or not origin_text
                            or record_platform == origin_text)
        if _record_matches_batch(record, key) and platform_matches:
            same_scope.append(record)
            continue
        record_date = str(record.get("delivery_date") or "")
        if not record_date:
            record_date = str(record.get("batch_key") or "").split("|")[0]
        if date_text and record_date == date_text:
            cross_scope.append(record)
        else:
            other.append(record)

    blocked = bool(same_scope or cross_scope)
    reason = ""
    code = ""
    if same_scope:
        code = "unresolved_batch"
        reason = (f"同一批次（{date_text}）还有 {len(same_scope)} 条未确认的下单记录："
                  f"上一次提交的结果未知，重复提交会真的多下单。"
                  f"请先做「只读核对」或人工处置未决记录。")
    elif cross_scope:
        code = "cross_scope_unresolved"
        reason = (f"{date_text} 这一天下还有 {len(cross_scope)} 条未确认记录"
                  f"（账号或平台地址与当前不同）。它们不属于当前批次，"
                  f"但同一批客户可能已经在那边下过单 —— 请先核对"
                  f"（换账号/换网址不能解除阻断）。")
    return {
        "blocked": blocked,
        "code": code,
        "reason": reason,
        "batch_key": key,
        "same_scope": same_scope,
        "cross_scope": cross_scope,
        "other_pending": other,
        "journal_path": str(target),
    }


def journal_fingerprint(path: str | os.PathLike[str]) -> str:
    """当前日志内容的稳定指纹（只读），用作"核对快照 → 解除"之间的 CAS 锚点。

    任何记录的新增或状态变化都会改变指纹，因此"先只读核对、后解除"之间文件被
    改过就一定对不上，必须重新核对。
    """
    payload = load_journal(Path(path))
    records = [record for record in payload.get("records", [])
               if isinstance(record, dict)]
    blob = json.dumps(records, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------
# 脱敏投影（给界面/日志）
# ----------------------------------------------------------------------

def mask_contact(value: Any) -> str:
    """手机号/账号脱敏：只保留前 3 后 4，非 11 位数字按首尾保留。"""
    text = normalise_account(value)
    if not text:
        return ""
    match = _MASK_PHONE_RE.match(text)
    if match:
        return f"{match.group(1)}****{match.group(2)}"
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * max(0, len(text) - 6)}{text[-4:]}"


def pending_record_views(path: str | os.PathLike[str],
                         key: str | None = None) -> dict[str, Any]:
    """只读列出未决记录（脱敏投影）；**绝不改文件**。

    ``key`` 非空时只返回该批次键下的活跃记录（只有它们会阻断本次运行）。
    读失败抛 :class:`UncertainJournalError`：调用方绝不能把它当成"没有未决记录"。
    """
    target = Path(path)
    payload = load_journal(target)
    records = [record for record in payload.get("records", [])
               if isinstance(record, dict)]
    counts = {"active": 0, "inflight": 0, "unresolved": 0,
              "resolved": 0, "discarded": 0}
    views: list[dict[str, Any]] = []
    for record in records:
        status = str(record.get("status") or "unresolved")
        if status in counts:
            counts[status] += 1
        if not _is_active(record):
            continue
        if key is not None and not _record_matches_batch(record, key):
            continue
        counts["active"] += 1
        fingerprint = (record.get("fingerprint")
                       if isinstance(record.get("fingerprint"), dict) else {})
        views.append({
            "journal_id": str(record.get("journal_id") or ""),
            "identifier": str(record.get("identifier") or ""),
            "sheet": str(record.get("sheet") or ""),
            "batch_id": str(record.get("batch_id") or ""),
            "delivery_date": str(record.get("delivery_date") or ""),
            "status": status,
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
        })
    return {"records": views, "counts": counts, "path": str(target),
            "fingerprint": journal_fingerprint(target)}


def journal_tasks(records: Iterable[Mapping[str, Any]]
                  ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """把未决记录还原成对账用的任务与 ``{journal_id: record}`` 映射。

    正式运行的收尾对账与"手动只读核对"共用这一份还原规则，
    避免两条路径各自解释 fingerprint 而给出不同结论。
    """
    tasks: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        journal_id = str(record.get("journal_id")
                         or record.get("identifier") or "").strip()
        if not journal_id:
            continue
        fingerprint_data = (record.get("fingerprint")
                            if isinstance(record.get("fingerprint"), dict) else {})
        tasks.append({
            "identifier": journal_id,
            "payload": {},
            "fingerprint": dict(fingerprint_data),
            "account": normalise_account(record.get("account")),
        })
        by_id[journal_id] = dict(record)
    return tasks, by_id


__all__ = [
    "ACTIVE_STATUSES",
    "DEFAULT_SSS_URL",
    "JOURNAL_VERSION",
    "UncertainJournalError",
    "append_records",
    "batch_key",
    "batch_lock_path",
    "batch_submission_lock",
    "blocking_state",
    "guarded_close_records",
    "default_uncertain_path",
    "discard_records",
    "journal_fingerprint",
    "journal_lock_path",
    "journal_tasks",
    "legacy_batch_key",
    "load_journal",
    "mask_contact",
    "normalise_account",
    "pending_record_views",
    "pending_records",
    "platform_origin",
    "resolve_records",
    "scope_key",
]
