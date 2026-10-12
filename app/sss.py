"""闪时送（sss）平台批量下单自动化（接口模式）。

本模块与管理后台订单处理（:mod:`app.automation`）方向相反：从独立的
《闪时送.xlsx》读取订单（午餐/晚餐两表），再在闪时送平台逐单创建预约单。

默认使用纯接口模式：直接请求闪时送 HTTP 接口完成登录与下单；用户只需在应用内
输入图形验证码，不再弹出浏览器。备用浏览器表单模式会启动 Playwright，自动填写
账号密码后由用户在浏览器中输入图形验证码；下单则按原 ``闪时送.py`` 脚本的
「创建订单」弹窗流程逐单操作（预约单 → 门店 → 常用地址 → 送达时间 →
姓名/电话/名称/门牌号 → 确定），结果分类与对账语义和接口分支完全一致。

干跑模式：配置 ``sss_dry_run = true`` 时只组装并打印下单报文，不真实提交。
凭据与路径由 GUI 通过 :class:`AppConfig` 传入，不写入源码。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from threading import Lock, local
from typing import Any, Callable, NamedTuple
from urllib.parse import urlencode

try:
    from .api_client import (ApiError, SssApiClient, SssTransportError,
                             auth_error_message, is_auth_expired_payload,
                             is_internal_error_payload)
    from .automation import (
        BrowserNotFoundError,
        LocatorError,
        _emit,
        _find_by_candidates,
        _launch_browser,
        _locator_failure,
        _save_failure_snapshot,
    )
    from .locators import SSS_LOCATORS, load_sss_locators
    from .sss_import import ImportRefused, prepare_day_orders
    from .sss_url import SssUrlConfigError, canonical_sss_origin
except ImportError:  # pragma: no cover - allows ``python app/sss.py``
    from api_client import (ApiError, SssApiClient, SssTransportError,
                            auth_error_message, is_auth_expired_payload,
                            is_internal_error_payload)
    from automation import (
        BrowserNotFoundError,
        LocatorError,
        _emit,
        _find_by_candidates,
        _launch_browser,
        _locator_failure,
        _save_failure_snapshot,
    )
    from locators import SSS_LOCATORS, load_sss_locators
    from sss_import import ImportRefused, prepare_day_orders
    from sss_url import SssUrlConfigError, canonical_sss_origin

DEFAULT_SHEETS = ("午餐", "晚餐")
LUNCH_TIME = "11:00:00"
DINNER_TIME = "17:00:00"
DEFAULT_SSS_URL = "https://sssplusnew.zhuopaikeji.com/takeout"

# 下单相关接口（2026-09-05 实测抓包确认，详见 .zcode/sss_api_recon.md）。
_CREATE_ORDER_PATH = "/consumer/order/one-touch-send/create-order-from-client"
_ORDER_LIST_PATH = "/consumer/order/one-touch-send/list"
_STORE_LIST_PATH = "/consumer/customer/store/queryStoreAddresses?pageNo=1&pageSize=40"
_FREQUENT_ADDR_PATH = "/consumer/customer/customerAddress/queryFrequentAddressByCustomer"

# 余额查询接口（2026-09-09 实测：result={totalAmount, freezeAmount}）。
_ACCOUNT_PATH = "/consumer/account/get-login-user-account"

# 读表：连续空行达到该数量才判定结束，避免中间偶发空行截断大单。
_BLANK_ROWS_TO_STOP = 3
# 日志节流：每成功这么多单才打一条汇总，避免事件队列被刷爆。
_PROGRESS_EVERY_N = 10
# 干跑时只打印前 N 单完整报文，其余只计数。
_DRY_RUN_PREVIEW_N = 3
# 对账只读轮询：服务端列表延迟时多查几次，绝不在轮询期间重发 POST。
_RECONCILE_POLL_ATTEMPTS = 3
_RECONCILE_POLL_INTERVAL_S = 0.5
# 服务端预筛返回「窗口内一条都没有」时的快速重查间隔。实测 2026-09-13：批次
# 刚创建完立刻做预筛会查到 0 条，几分钟后同一窗口能查到全部订单（列表写后
# 可见性延迟）。「窗口真的为空」与「刚写入还没读到」在响应上无法区分，
# 因此先短暂重查一次，仍为空才退回无过滤全量扫描。
_PREFILTER_ZERO_RETRY_DELAY_S = 2.0
# 订单创建时间与本地批次起点的最大时钟偏差；用于排除其他设备更早创建的相似订单。
_BATCH_CLOCK_SKEW_S = 120.0
# 浏览器表单模式（原 闪时送.py 弹窗流程）的等待节奏。
_UI_CREATE_BTN_TIMEOUT_MS = 30_000      # 打开弹窗：点「创建订单」（含首屏加载）
_UI_STEP_TIMEOUT_MS = 10_000            # 弹窗内常规步骤（下拉/地址/输入框）
_UI_FINAL_CONFIRM_TIMEOUT_MS = 15_000   # 最终确定按钮
_UI_MODAL_CLOSE_TIMEOUT_S = 90.0        # 点最终确定后等弹窗遮罩收场的总时限
_UI_MODAL_POLL_MS = 5_000               # 遮罩轮询单次等待
_UI_RECOVER_TIMEOUT_MS = 50_000         # 弹窗收场后等「创建订单」恢复可点
# 最终确定的重试节奏（2026-10-12 真实账号 102 单实测值，见
# docs/闪时送弹窗重试与移除未决阻断-实施计划-2026-10-12.md §2）。
# 平台在建单节奏过快或内部偶发时会对「最终确定」快速驳回
# （antd 顶部浅色提示「操作失败，java.lang.IndexOutOfBoundsException: Index: 0,
# Size: 0」，无按钮），表现是弹窗不关闭；**原表单数据不丢失，再点一次即成功**。
# 83 单提交里 81 单首点被驳回，重试救回 77 单，102 单最终 0 重复。
_UI_FINAL_CONFIRM_DELAY_S = 0.5         # 全部字段填完 → 首次点击前的静默（用户要求）
_UI_FINAL_CONFIRM_ATTEMPTS = 3          # 驳回后最多点击轮数（实测值）
_UI_REJECT_SETTLE_WINDOW_S = 5.0        # 单轮点击后等收场的观察窗（实测值）
_UI_REJECT_RETRY_PAUSE_MS = 600         # 驳回后到下一轮点击的间隔（实测值）
_UI_MASK_POLL_S = 0.3                   # 遮罩可见性轮询间隔（实测值）
# 创建订单接口是非幂等 POST，且平台在并发请求下曾间歇性返回
# ``IndexOutOfBoundsException: Index: 0, Size: 0``（2026-09 实测，未定根因）。
# 当时为此把提交固定成 1 路串行排查；2026-10-08 恢复为配置值（默认 4 路），
# 依赖的是"至少一次提交 + 站内对账确认"这条既有兜底：并发只影响并发度，
# 不改变"POST 不自动重发、以对账结果为准"的语义。平台若再次出现与并发相关的
# 间歇错误，把 ``sss_max_workers`` 配回 1 即可回到串行，不需要改代码。
_SSS_CREATE_MAX_WORKERS_CEILING = 20  # 与 config._SSS_MAX_WORKERS_CEILING 保持一致
# 配置对象缺 ``sss_max_workers`` 字段（或取值无法解析）时的保守回退：串行。
# 正常路径上 ``AppConfig`` 始终带该字段（默认 4 路），因此这条只保护
# 传了残缺 config 的调用方。
_FALLBACK_SSS_CREATE_WORKERS = 1
# 提交节流（2026-10-09 按服务端 v3.6.18 / commit 03413f6 移植）：平台对同一账号
# 的建单受理节奏实测约 1 单 / 2.1 秒，超速的 POST 会在约 0.3-1 秒内被快速驳回
# （HTTP 200 + code=500 + ``java.lang.IndexOutOfBoundsException: Index: 0, Size: 0``，
# 且每个请求都消耗一个平台订单序列号）。4 路滚动补位下被驳回的车道会立刻补发
# 下一单，实际发送节奏可达约 1.37 POST/秒（约 3 倍超速），于是每轮约 2/3 被驳回。
# 这里令相邻两次 POST 的**起点**至少间隔 ``sss_submit_min_interval_s``（出厂
# 2.5 秒，0 = 关闭），并发路数不变；出现快速驳回特征时自动放宽间隔自保。
# 节流只影响发送节奏，不改变任何结果分类、记录与重发语义。
# 注意：2.1 秒是现场观察到的受理节奏，不是已证实的平台硬限速（见
# docs/闪时送周期失败与序号跳号排查-2026-10-09.md §5.3），2.5 秒是保守试探值。
_SUBMIT_FAST_REJECT_MAX_S = 1.5          # 「快速驳回」的耗时上限（秒）
_SUBMIT_INTERVAL_PENALTY_FACTOR = 1.5    # 每次自动放宽的倍数
_SUBMIT_INTERVAL_PENALTY_MAX_S = 10.0    # 自动放宽的上限（秒）
_DEFAULT_SSS_SUBMIT_MIN_INTERVAL_S = 2.5 # 出厂默认提交最小间隔（秒）
_SSS_SUBMIT_MIN_INTERVAL_CEILING_S = 60.0
# 对账页大小。实测 2026-09-10：100 条/页每页约 2.0s；改成 1000 条/页反而要
# 17.9s，所以宁可多翻几页也不要放大单页体积。
_LIST_PAGE_SIZE = 100
# 服务端预筛的时间窗安全边界：订单创建时间可能早于/晚于预约送达日，
# 因此窗口在目标日基础上前后各放宽这么多天。放宽只会多取几条记录，
# 真正的判定仍然由本地按送达日/状态过滤负责。
_SERVER_PREFILTER_MARGIN_DAYS = 1
# 服务端预筛开关：默认开启；设 YIKOU_SSS_SERVER_PREFILTER=0 可一键退回全量扫描。
_SSS_SERVER_PREFILTER = os.environ.get(
    "YIKOU_SSS_SERVER_PREFILTER", "").strip().lower() not in ("0", "false", "no", "off")
# 订单列表接口的 time 型参数只接受 epoch 毫秒（2026-09-10 实测：传字符串会被
# Spring 以 BindException 拒绝）。状态参数是单数 ``status``；``statusList`` 无效。
_SERVER_PREFILTER_STATUS = 2
# 平台抓包报文没有客户端幂等字段。留空时明确采用“至少一次提交 + 对账确认”；
# 若平台后续支持，可在配置/调用方指定字段名后启用稳定 UUID（见 build_order_payload）。
_CLIENT_IDEMPOTENCY_FIELD = ""

# 同一时刻只允许一个闪时送下单任务，避免两个 worker 并发提交相似订单。
_SSS_RUN_LOCK = Lock()

# 一键发货订单非活跃 status（2026-09-09/10 实测只见过 2=待发单在途、
# 3=已发送；其余状态码未经实测，一律按活跃处理——对账多算比漏算安全，
# 漏算会导致重复提交）。
_INACTIVE_ORDER_STATUS = frozenset()

_DOOR_RE = re.compile(r"\s+")
_PHONE_RE = re.compile(r"^[0-9]{11}$")

# 下单失败 message 里出现这些关键词即判定为余额不足（服务端动态文案，
# 前端 chunk 里无固定文案，只能按关键词识别；英文兜底不区分大小写）。
_BALANCE_KEYWORDS = ("余额", "充值", "欠费", "冻结", "钱包",
                     "balance", "insufficient", "recharge")

_SSS_FETCH_JS = """
async ({method, path, body}) => {
  let token = null;
  try {
    const t = JSON.parse(localStorage.getItem('tokenObj') || 'null');
    if (t && t.expirationTime > Date.now()) token = t.token;
  } catch (e) {}
  const headers = {'Content-Type': 'application/json'};
  if (token) headers['token'] = token;
  const r = await fetch(path, {method, headers,
      body: body === null || body === undefined ? undefined : JSON.stringify(body)});
  return JSON.stringify({http: r.status, text: await r.text()});
}
"""


def _clean(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


# 耗时埋点：默认关闭，设 YIKOU_SSS_TRACE=1 才输出每页列表 / 每次下单的真实耗时。
# 2026-09-10 实测背景：站内 2200+ 单、列表每页 100 条约 2s、一次对账要扫 23 页；
# 下单 POST 的耗时此前完全没有记录，无法区分是客户端还是服务端慢。
_SSS_TRACE_ENABLED = os.environ.get("YIKOU_SSS_TRACE", "").strip().lower() not in (
    "", "0", "false", "no", "off")


def _trace(message: str) -> None:
    """把埋点写到标准输出（GUI 启动时仍落在终端），不影响业务日志。"""
    if not _SSS_TRACE_ENABLED:
        return
    stamp = time.strftime("%H:%M:%S")
    print(f"[sss-trace {stamp}] {message}", file=sys.stderr, flush=True)


def load_sss_orders(excel_path: str | Path, sheets=DEFAULT_SHEETS) -> dict[str, list[dict[str, Any]]]:
    """读取《闪时送.xlsx》的订单（A=姓名 B=门牌号 C=电话 D=送达时间）。

    每张工作表从第 3 行开始，连续 ``_BLANK_ROWS_TO_STOP`` 个 A/B/C 全空
    的行才终止（兼容中间偶发空行）。使用 ``read_only`` + ``iter_rows``
    批量取值，避免逐格 ``ws[f"A{n}"]`` 的开销。返回
    ``{工作表名: [订单字典, ...]}``，每个订单含 ``row`` 与四列原始值。
    """
    from openpyxl import load_workbook

    wb = load_workbook(excel_path, data_only=True, read_only=True)
    result: dict[str, list[dict[str, Any]]] = {}
    try:
        for sheet_name in sheets:
            if sheet_name not in wb.sheetnames:
                continue
            ws = wb[sheet_name]
            orders: list[dict[str, Any]] = []
            blank_streak = 0
            for row_num, row in enumerate(
                ws.iter_rows(min_row=3, max_col=4, values_only=True), start=3
            ):
                name = _clean(row[0]) if len(row) > 0 else None
                door = _clean(row[1]) if len(row) > 1 else None
                phone = _clean(row[2]) if len(row) > 2 else None
                delivery_time = row[3] if len(row) > 3 else None
                if not any([name, door, phone]):
                    blank_streak += 1
                    if blank_streak >= _BLANK_ROWS_TO_STOP:
                        break
                    continue
                blank_streak = 0
                orders.append({
                    "row": row_num,
                    "name": name,
                    "door": door,
                    "phone": phone,
                    "delivery_time": delivery_time,
                })
            result[sheet_name] = orders
        return result
    finally:
        wb.close()


def _normalise_phone(value: Any) -> str:
    """把 Excel 电话字段规范成 11 位 ASCII 数字，拒绝含糊格式。"""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        # openpyxl 会把没有文本格式的整数字段读成 float；仅接受精确整数，
        # 防止 138... .5 一类值被悄悄截断后发送到平台。
        if not value.is_integer():
            return ""
        text = str(int(value))
    else:
        text = unicodedata.normalize("NFKC", str(value)).strip()
    return _DOOR_RE.sub("", text)


def _validate_sss_orders(orders_by_sheet: dict[str, list[dict[str, Any]]]) -> None:
    """校验并规范所有订单，任何一行异常都阻止整批继续。"""
    invalid: list[str] = []
    for sheet_name, orders in orders_by_sheet.items():
        for order in orders:
            name = unicodedata.normalize("NFKC", str(order.get("name") or "")).strip()
            door = unicodedata.normalize("NFKC", str(order.get("door") or "")).strip()
            phone = _normalise_phone(order.get("phone"))
            missing = []
            if not name:
                missing.append("姓名")
            if not door:
                missing.append("门牌")
            if not _PHONE_RE.fullmatch(phone):
                missing.append("11 位电话")
            if missing:
                invalid.append(f"{sheet_name}第 {order.get('row', '?')} 行（{'、'.join(missing)}）")
                continue
            order["name"] = name
            order["door"] = door
            order["phone"] = phone
    if invalid:
        raise ValueError("闪时送 Excel 存在无效订单：" + "；".join(invalid))


def compute_delivery_time(is_dinner: bool, now: _dt.datetime | None = None) -> str:
    """按原脚本规则计算送达时间：午餐 11:00 / 晚餐 17:00，16 点后顺延次日。"""
    now = now or _dt.datetime.now()
    target_date = now
    hour = now.hour
    if 20 <= hour < 24 or 16 <= hour < 20:  # 原脚本的等价写法：16~23 点顺延次日
        target_date = now + _dt.timedelta(days=1)
    time_str = DINNER_TIME if is_dinner else LUNCH_TIME
    return target_date.strftime("%Y-%m-%d ") + time_str


def expected_delivery_date(now: _dt.datetime | None = None) -> _dt.date:
    """本次下单会给平台报的送达日期。

    午餐与晚餐使用同一条顺延规则，因此只有时间不同、日期相同；云端名单的
    「识别日期」必须与它一致，否则拒绝下单（见 ``sss_import.check_target_day``）。
    """
    return _dt.date.fromisoformat(compute_delivery_time(False, now)[:10])


def _resolve(locators: dict[str, Any] | None, name: str) -> dict[str, Any]:
    """Return a step dict, falling back to the built-in 闪时送 default."""
    return (locators or {}).get(name) or SSS_LOCATORS.get(name) or {}


def _substitute_tokens(step: dict[str, Any], **tokens: str) -> dict[str, Any]:
    """Deep-copy a step and replace ``{token}`` placeholders in candidate strings."""
    copied = json.loads(json.dumps(step))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if isinstance(value, str):
                    for token, replacement in tokens.items():
                        value = value.replace("{" + token + "}", replacement)
                    node[key] = value
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(copied)
    return copied


def _pick(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """按优先级从记录里取第一个非空字段。"""
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _unwrap_scalar(value: Any) -> Any:
    """把列表/元组字段摊平成首个标量。

    闪时送订单列表把电话返回成数组（如 ``["18545726939"]``），直接
    ``str()`` 会得到 ``"['18545726939']"``，与下单报文的单值永远不相等。
    """
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                continue
            if item not in (None, ""):
                return item
        return None
    return value


def _pick_scalar(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """同 ``_pick``，但把数组字段取首元素，适配列表接口的数组化字段。"""
    for key in keys:
        value = _unwrap_scalar(record.get(key))
        if value not in (None, ""):
            return value
    return None


def _result_records(payload: dict[str, Any]) -> list:
    """从 {result: {records: [...]}} 或 {result: [...]} 里取记录列表。"""
    result = payload.get("result")
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        records = result.get("records") or result.get("list")
        if isinstance(records, list):
            return records
    return []


def _match_record(records: Any, keyword: str, what: str,
                  fields: tuple[str, ...] = ()) -> dict[str, Any]:
    """按关键词匹配记录；先定字段精确匹配，找不到再回退全文。

    ``fields`` 为优先匹配的字段名（如门店的 name、地址的 contactName）。
    定字段匹配逐字段做子串判断，避免对每条记录做 ``json.dumps`` 全文
    序列化；回退全文只用于兼容未知字段结构。
    """
    if not isinstance(records, list) or not records:
        raise LookupError(f"{what}列表为空或格式异常")
    if keyword:
        for record in records:
            if not isinstance(record, dict):
                continue
            for field in fields:
                value = record.get(field)
                if value not in (None, "") and keyword in str(value):
                    return record
        for record in records:
            if keyword in json.dumps(record, ensure_ascii=False):
                return record
    if len(records) == 1:
        single = records[0]
        if isinstance(single, dict):
            return single
        raise LookupError(f"{what}列表格式异常")
    raise LookupError(
        f"{what}列表里没有匹配「{keyword}」的记录，共 {len(records)} 条："
        + json.dumps(records, ensure_ascii=False)[:600])


def _sss_api(page: Any, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any]]:
    """在登录页会话内调闪时送接口（token 头自动取自 localStorage.tokenObj）。"""
    raw = page.evaluate(_SSS_FETCH_JS, {"method": method, "path": path, "body": body})
    envelope = json.loads(raw)
    try:
        payload = json.loads(envelope.get("text") or "{}")
    except ValueError:
        payload = {}
    return int(envelope.get("http") or 0), payload


def _ensure_logged_in(page: Any, account: str, password: str, stop_event: Any,
                      callback: Callable[[str], Any] | None) -> None:
    """确保已登录：已登录直接返回；否则自动填写并等用户输验证码后自动点登录。

    验证码输错时页面会刷新出新验证码，循环等待重输；登录成功的标志是
    「创建订单」按钮出现（登录后进入的是订单页）。
    """
    if page.locator("text=创建订单").count():
        return
    page.wait_for_selector("text=账户密码登录", state="visible", timeout=30000)
    page.get_by_text("账户密码登录").click()
    page.wait_for_selector("input#account", state="visible", timeout=15000)
    page.fill("input#account", account)
    page.fill("input#password", password)
    _emit(callback, ">>> 请在闪时送窗口输入图形验证码（输完自动点登录；"
                    "窗口已最小化时请先点任务栏还原）<<<")

    deadline = time.time() + 300
    while time.time() < deadline:
        if stop_event.is_set():
            raise RuntimeError("已停止")
        code_val = page.evaluate(
            "() => (document.querySelector('input#code')||{}).value || ''")
        if len(code_val.strip()) >= 4:
            page.locator("button", has_text="登录").first.click()
            page.wait_for_timeout(2500)
            if page.locator("text=创建订单").count():
                _emit(callback, "登录成功")
                return
            # 验证码错误：清空输入框等待重输（否则残留旧值会触发误重试）
            try:
                page.fill("input#code", "")
            except Exception:
                pass
            _emit(callback, "验证码不正确或登录未成功，请按窗口里的新验证码重新输入")
        page.wait_for_timeout(400)
    raise TimeoutError("登录超时：未完成验证码输入")


def _minimize_window(browser: Any, callback: Callable[[str], Any] | None) -> None:
    """Best-effort 最小化浏览器窗口（CDP），失败不阻断流程。"""
    try:
        session = browser.new_browser_cdp_session()
        window_id = session.send("Browser.getWindowForTarget")["windowId"]
        session.send("Browser.setWindowBounds",
                     {"windowId": window_id, "bounds": {"windowState": "minimized"}})
        _emit(callback, "浏览器已最小化（任务栏可见，需要输验证码时再还原）")
    except Exception:
        pass


# ===================== 浏览器表单模式（原 闪时送.py 弹窗流程） =====================
#
# 浏览器模式（api_mode=False）的下单按原 ``闪时送.py`` 脚本驱动「创建订单」
# 弹窗，不再在页面会话里 fetch 直调建单接口。登录仍由 :func:`_ensure_logged_in`
# 自动完成，随后每一单执行：
#
#   创建订单 → 等弹窗（即时单）→ 订单类型选「预约单」→ 分单方式选门店
#   （默认「一口轻食」）→ 常用地址 → 地址选项（默认「嗯哼」）→ 地址确定
#   → 填送达时间 → 时间确定 → 姓名/电话/名称/门牌号 → 最终确定 → 等弹窗收场
#
# 结果分类必须遵守与接口分支相同的契约（见 run_sss_job 中 submit_browser 注释）：
#   * 最终确定点击**之前**的任何失败 —— 普通异常（这一单确定没有创建，
#     未决日志按“未落单”关闭，允许人工重试）；
#   * 最终确定点击后无法确认弹窗收场 —— ``_SubmissionUncertain``（结果未知，
#     由站内只读对账定论，绝不自动重发）；
#   * 「创建订单」按钮消失 —— ``_AuthExpired``（登录态丢失，触发统一重登）；
#   * 弹窗正常收场 —— 返回 ``{"success": True}``（仍以站内对账为最终确认）。

_JS_CLICK_EVENTS = """
(el) => {
  const opts = {bubbles: true, cancelable: true, view: window};
  el.dispatchEvent(new MouseEvent('mousedown', opts));
  el.dispatchEvent(new MouseEvent('mouseup', opts));
  el.dispatchEvent(new MouseEvent('click', opts));
}
"""


def _ui_click(page: Any, step: dict[str, Any], timeout_ms: int, desc: str,
              *, pick: str = "first") -> Any:
    """按候选链定位并点击；常规点击被遮挡时回退 JS 派发完整鼠标事件。

    ``pick="last"`` 取最后一个匹配（antd 弹窗按打开顺序追加在 body 末尾，
    因此叠在「创建订单」弹窗之上的地址弹窗的按钮是最后一个匹配 —— 原脚本
    用绝对 xpath 选 div[8] 也是同一个道理；窄化在定位引擎内完成，先 ``.first``
    再 ``.last`` 的链式覆盖不会生效）。定位不到时保存现场截图并抛普通异常
    （调用上下文里即“确定未创建”）。
    """
    locator, _index = _find_by_candidates(page, step, timeout_ms, pick=pick)
    if locator is None:
        raise _locator_failure(page, desc)
    try:
        locator.click(timeout=timeout_ms)
    except Exception:  # noqa: BLE001 - 遮挡/时序问题，回退 JS 事件模拟
        handle = locator.first.element_handle(timeout=1000)
        if handle is None:
            raise _locator_failure(page, desc) from None
        page.evaluate(_JS_CLICK_EVENTS, handle)
    return locator


def _ui_fill(page: Any, step: dict[str, Any], value: str, timeout_ms: int,
             desc: str) -> Any:
    """按候选链定位输入框并清空填入（fill 自带清空语义，等价原脚本“全选+删除”）。"""
    locator, _index = _find_by_candidates(page, step, timeout_ms)
    if locator is None:
        raise _locator_failure(page, desc)
    locator.fill(str(value), timeout=timeout_ms)
    return locator


def _ui_present(page: Any, step: dict[str, Any], timeout_ms: int, desc: str) -> Any:
    """按候选链等待元素出现（不点击）。"""
    locator, _index = _find_by_candidates(page, step, timeout_ms)
    if locator is None:
        raise _locator_failure(page, desc)
    return locator


def _ui_wait_modal_closed(page: Any, locators: dict[str, Any],
                          callback: Callable[[str], Any] | None) -> None:
    """等待「创建订单」弹窗彻底收场（原脚本：遮罩消失 + 创建订单恢复可点）。

    只能在最终确定点击**之后**调用：此刻订单是否已创建无法从页面断言，
    任何一步等待超时都按“结果未知”抛 ``_SubmissionUncertain``，交给站内
    只读对账定论，绝不在这里判“失败”（误判失败会导致下一次重复下单）。
    """
    deadline = time.monotonic() + _UI_MODAL_CLOSE_TIMEOUT_S
    while True:
        try:
            if time.monotonic() >= deadline:
                raise _SubmissionUncertain(
                    f"最终确定已点击，但 {_UI_MODAL_CLOSE_TIMEOUT_S:g} 秒内弹窗遮罩未收场，"
                    "无法确认订单是否已创建")
            page.wait_for_selector(".ant-modal-mask", state="hidden",
                                   timeout=_UI_MODAL_POLL_MS)
            break
        except _SubmissionUncertain:
            raise
        except Exception:  # noqa: BLE001 - 遮罩还在/页面短暂忙，轮询到总时限为止
            time.sleep(0.3)
            continue
    _emit(callback, "确认弹窗已收场，等待页面恢复…")
    recover_step = _resolve(locators, "创建订单")
    # 恢复检查必须走收窄定位（``_find_by_candidates`` 内部取 ``.first`` 并轮询）：
    # 2026-10-12 实测，页面同时存在导航按钮与弹窗标题残留时 ``get_by_text`` 多匹配，
    # Playwright strict mode 直接抛 ``Error``（不是超时），把每一单成功提交都误报成
    # 「创建订单按钮未恢复」→ 全部被标成"结果未知"。加收窄即修复。
    if recover_step:
        if _find_by_candidates(page, recover_step, _UI_RECOVER_TIMEOUT_MS)[0] is None:
            raise _SubmissionUncertain(
                f"弹窗收场后 {_UI_RECOVER_TIMEOUT_MS / 1000:g} 秒内「创建订单」"
                "按钮未恢复，无法确认订单状态")
    time.sleep(0.5)


def _ui_wait_mask_hidden(page: Any, window_s: float) -> bool:
    """轮询等待弹窗遮罩消失，返回是否在观察窗内收场。

    实测用 ``.ant-modal-mask:visible`` 的 ``count() == 0`` 判定收场：antd 弹窗
    入场/退场期间可能同时存在多个遮罩，``wait_for_selector(state="hidden")``
    在多遮罩下会误判，而 ``count()`` 直接数"还有几个可见遮罩"。
    """
    deadline = time.monotonic() + max(0.0, window_s)
    while True:
        try:
            if not page.locator(".ant-modal-mask:visible").count():
                return True
        except Exception:  # noqa: BLE001 - 页面短暂忙就继续轮询
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(_UI_MASK_POLL_S)


def _snapshot_rejection_once(page: Any,
                             callback: Callable[[str], Any] | None) -> None:
    """首次平台驳回时抓一份现场（截图/HTML/网址）；纯诊断，失败即忽略。

    用于回答实施计划 §2.4 的遗留问题：驳回报错是 page 级 antd message（无按钮），
    因此"点掉报错"实际点到的是弹窗自己的「确定」——有现场证据后即可确认动作语义，
    必要时再按证据微调点击目标。
    """
    try:
        log_dir = _save_failure_snapshot(page, "最终确定被平台驳回")
        _emit(callback, f"已保存驳回现场截图与页面 HTML：{log_dir}")
    except Exception:  # noqa: BLE001 - 诊断失败绝不能影响重试
        pass


def _store_option_step(locators: dict[str, Any], store_name: str) -> dict[str, Any]:
    """解析门店选项步骤；旧版覆盖文件里门店文字写死时按同构候选动态构造。

    表单里选的门店必须与报文指纹里的 ``storeId``（按 ``store_name`` 解析）
    指向同一家门店，否则站内对账会把刚下的单当成“别人的订单”。
    """
    resolved = _substitute_tokens(_resolve(locators, "一口轻食选项"),
                                  store=store_name, label=store_name)
    if store_name != "一口轻食" and all(
            store_name not in json.dumps(candidate, ensure_ascii=False)
            for candidate in resolved.get("candidates", [])):
        resolved = {"candidates": [
            {"css": ".ant-select-dropdown .ant-select-item-option", "has_text": store_name},
            {"text": store_name},
        ]}
    return resolved


def _ui_dismiss_modal(page: Any) -> None:
    """表单中途失败后 best-effort 关掉残留弹窗，避免污染下一单的表单状态。"""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:  # noqa: BLE001 - 收尾动作失败不影响原异常上抛
        pass


def _ui_create_order(page: Any, payload: dict[str, Any], locators: dict[str, Any],
                     *, store_name: str, address_label: str,
                     callback: Callable[[str], Any] | None) -> dict[str, Any]:
    """按原 ``闪时送.py`` 的弹窗流程创建一笔预约单（浏览器表单模式的提交器）。

    姓名/电话/门牌/送达时间/商品名一律从 ``payload``（与接口分支同一套
    ``build_order_payload`` 报文）读取，保证“表单里填什么”与“指纹/对账
    期望什么”完全一致；门店与常用地址在表单里按 ``store_name`` 与
    ``address_label``（常用地址标签，默认「嗯哼」）选择，浏览器分支的报文
    地址也必须来自同一条常用地址记录（见 run_sss_job 浏览器分支的地址
    准备注释）。

    返回 ``{"success": True}``；异常分类契约见本节开头的模块注释。
    """
    address = payload.get("receiveAddress")
    address = address if isinstance(address, dict) else {}
    goods_detail = payload.get("goodsDetail")
    goods_name = ""
    if isinstance(goods_detail, list) and goods_detail and isinstance(goods_detail[0], dict):
        goods_name = str(goods_detail[0].get("goodsName") or "")
    name = str(payload.get("receiveName") or "")
    phone = str(payload.get("receivePhone") or "")
    door = str(address.get("doorNum") or "")
    expected_time = str(payload.get("expectedDeliveryTime") or "")

    def step(key: str, **tokens: str) -> dict[str, Any]:
        return _substitute_tokens(_resolve(locators, key), **tokens)

    # 0. 登录态守卫：「创建订单」按钮消失 = 已被登出
    if not page.locator("text=创建订单").count():
        raise _AuthExpired("登录态已失效（「创建订单」按钮未找到）")

    def open_and_fill_form() -> None:
        """打开弹窗并把表单填到“只差最终确定”；任何失败都发生在提交之前。"""
        # 1. 打开「创建订单」弹窗并等它出现
        _ui_click(page, step("创建订单"), _UI_CREATE_BTN_TIMEOUT_MS, "点击创建订单")
        _ui_present(page, step("订单弹窗"), _UI_STEP_TIMEOUT_MS, "创建订单弹窗未出现")

        # 2. 订单类型：即时单 → 预约单（第二候选兼容上一单残留的“已选预约单”）
        _ui_click(page, step("订单类型下拉"), _UI_STEP_TIMEOUT_MS, "打开订单类型下拉")
        _ui_click(page, step("预约单选项"), _UI_STEP_TIMEOUT_MS, "选择预约单")

        # 3. 分单方式：自动分单 → 门店（默认「一口轻食」）
        _ui_click(page, step("分单下拉", store=store_name), _UI_STEP_TIMEOUT_MS,
                  "打开分单下拉")
        _ui_click(page, _store_option_step(locators, store_name),
                  _UI_STEP_TIMEOUT_MS, f"选择门店「{store_name}」")

        # 4. 常用地址：打开 → 选地址 → 确定（确定按钮常被遮挡，交给 JS 回退）
        _ui_click(page, step("常用地址"), _UI_STEP_TIMEOUT_MS, "打开常用地址")
        _ui_click(page, step("地址选项", label=address_label), _UI_STEP_TIMEOUT_MS,
                  f"选择常用地址「{address_label}」")
        _ui_click(page, step("地址确定"), _UI_STEP_TIMEOUT_MS, "确认常用地址",
                  pick="last")

        # 5. 送达时间：点开选择器 → 全选删除 → 逐字键入 → 确定
        delivery_input = _ui_present(page, step("送达时间输入"), _UI_STEP_TIMEOUT_MS,
                                     "定位送达时间输入框")
        delivery_input.click(timeout=_UI_STEP_TIMEOUT_MS)
        try:
            delivery_input.press("ControlOrMeta+a", timeout=_UI_STEP_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - 旧内核不认 ControlOrMeta 时退回 Control
            delivery_input.press("Control+a", timeout=_UI_STEP_TIMEOUT_MS)
        delivery_input.press("Delete", timeout=_UI_STEP_TIMEOUT_MS)
        delivery_input.type(expected_time, timeout=_UI_STEP_TIMEOUT_MS)
        _ui_click(page, step("时间确定"), _UI_STEP_TIMEOUT_MS, "确认送达时间")

        # 6. 收件信息（fill 自带清空，等价原脚本“全选+删除再输入”）
        _ui_fill(page, step("顾客姓名"), name, _UI_STEP_TIMEOUT_MS, "填顾客姓名")
        _ui_fill(page, step("顾客电话"), phone, _UI_STEP_TIMEOUT_MS, "填顾客电话")
        _ui_fill(page, step("商品名称"), goods_name, _UI_STEP_TIMEOUT_MS, "填商品名称")
        _ui_fill(page, step("门牌号"), door, _UI_STEP_TIMEOUT_MS, "填门牌号")

    try:
        open_and_fill_form()
    except Exception:
        # 中途失败：弹窗还开着（可能残留半填数据），best-effort 关掉再上抛，
        # 否则下一单会在脏表单上继续操作。
        _ui_dismiss_modal(page)
        raise

    # 7. 全部字段填完后先静默 _UI_FINAL_CONFIRM_DELAY_S 再点（用户要求；
    #    首点驳回疑似与"填完立刻点"的平台竞态有关）。
    page.wait_for_timeout(int(_UI_FINAL_CONFIRM_DELAY_S * 1000))

    # 8. 最终确定 + 平台驳回重试（≤ _UI_FINAL_CONFIRM_ATTEMPTS 轮）
    # 从这一步开始结果才可能未知：点是点了，但"订单是否创建"无法从页面断言。
    # 平台驳回的形态是**弹窗不关闭**（顶部一条无按钮的 antd message），原表单
    # 数据不丢，再点一次即成功；因此按实测节奏重试，最多 N 轮后交给收尾对账。
    final_step = step("最终确定")
    reject_step = {"candidates": [
        {"css": ".ant-modal-footer .ant-btn-primary:visible"},
    ]}
    for attempt in range(_UI_FINAL_CONFIRM_ATTEMPTS):
        _ui_click(page, final_step, _UI_FINAL_CONFIRM_TIMEOUT_MS, "点击最终确定")
        if _ui_wait_mask_hidden(page, _UI_REJECT_SETTLE_WINDOW_S):
            if attempt:
                _emit(callback,
                      f"第 {attempt + 1} 次点击「最终确定」后弹窗收场"
                      "（平台驳回重试生效）")
            break
        _emit(callback, f"平台驳回（第 {attempt + 1} 次），点掉报错后原表单重试…")
        if attempt == 0:
            # 首次驳回留一份现场（截图 + HTML），用来回答"点掉报错到底点到了
            # 谁"：若报错是 page 级 message，此处点到的是弹窗自己的「确定」，
            # 动作语义就是"再点一次"（与实测一致）。纯诊断、失败不影响重试。
            _snapshot_rejection_once(page, callback)
        try:
            # 实测版本点的是脚部主按钮的最后一个（.last）：先照抄实测点击序列，
            # 语义以首次实跑的现场快照为准（实施计划 §2.4 备注）。
            _ui_click(page, reject_step, _UI_STEP_TIMEOUT_MS, "点掉平台驳回报错",
                      pick="last")
        except Exception:  # noqa: BLE001 - 点不到就交给收尾等待判定
            break
        page.wait_for_timeout(_UI_REJECT_RETRY_PAUSE_MS)
    else:
        _emit(callback,
              f"重试 {_UI_FINAL_CONFIRM_ATTEMPTS} 次仍未收场，交给站内对账判定")

    # 9. 等弹窗收场（恢复检查已改为收窄版）；失败即结果未知
    _ui_wait_modal_closed(page, locators, callback)
    return {"success": True}


def build_order_payload(order: dict[str, Any], is_dinner: bool, store_id: int,
                        address: dict[str, Any], goods_name: str,
                        now: _dt.datetime | None = None,
                        expected_time: str | None = None,
                        *, client_request_id: str = "",
                        idempotency_field: str = "") -> dict[str, Any]:
    """组装 create-order-from-client 的请求体（字段为 2026-09-05 抓包确认）。

    ``expected_time`` 传入时直接复用（批量下单午餐/晚餐各算一次即可，
    避免每单调 ``datetime.now()`` 导致午夜跨天批次日期不一致）。
    抓包确认的接口目前没有客户端幂等字段；只有调用方明确指定
    ``idempotency_field`` 时才附加稳定 ``client_request_id``，避免向未知
    schema 塞入字段导致服务端拒绝。
    """
    payload = {
        "expectedDeliveryTime": expected_time or compute_delivery_time(is_dinner, now),
        "goodsDetail": [{"goodsName": goods_name, "goodsNum": 1}],
        "orderType": 2,  # 预约单
        "receiveName": str(order.get("name") or ""),
        "receivePhone": str(order.get("phone") or ""),
        "storeId": store_id,
        "receiveAddress": {
            "lnt": address["lnt"],
            "lat": address["lat"],
            "areaCode": address["areaCode"],
            "addressDetail": address["addressDetail"],
            "doorNum": str(order.get("door") or ""),
        },
    }
    if client_request_id and idempotency_field:
        payload[idempotency_field] = client_request_id
    return payload


def _address_from_record(record: dict[str, Any]) -> dict[str, Any]:
    """从常用地址记录提取下单需要的坐标与地址字段。

    站点前端的映射（chunk 反解）：记录的 ``markLnglat`` 子对象携带
    ``longitude/latitude/adcode/address``，顶层字段仅作兜底。
    """
    mark = record.get("markLnglat") if isinstance(record.get("markLnglat"), dict) else {}

    def pick(src: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            value = src.get(key)
            if value not in (None, ""):
                return value
        return None

    lnt = pick(mark, ("longitude", "lng", "lnt", "lon")) or _pick(record, ("longitude", "lnt", "lng"))
    lat = pick(mark, ("latitude", "lat")) or _pick(record, ("latitude", "lat"))
    area = pick(mark, ("adcode", "areaCode")) or _pick(record, ("areaCode", "code"))
    detail = pick(mark, ("address", "addressDetail")) or _pick(record, ("position", "address", "addressDetail"))
    if lnt in (None, "") or lat in (None, ""):
        raise LookupError("常用地址记录缺少经纬度字段："
                          + json.dumps(record, ensure_ascii=False)[:400])
    return {
        "lnt": float(lnt), "lat": float(lat),
        "areaCode": str(area or ""), "addressDetail": str(detail or ""),
    }


def _fixed_address_from_config(config: Any) -> dict[str, Any]:
    """从配置读取固定地址，供“跳过常用地址匹配”的固定地址模式使用。"""
    lnt = getattr(config, "sss_fixed_lnt", None)
    lat = getattr(config, "sss_fixed_lat", None)
    area_code = str(getattr(config, "sss_fixed_area_code", "") or "")
    detail = str(getattr(config, "sss_fixed_address_detail", "") or "")
    if lnt in (None, "") or lat in (None, ""):
        raise LookupError("固定地址配置缺少经纬度字段："
                          + json.dumps({"lnt": lnt, "lat": lat}, ensure_ascii=False))
    return {
        "lnt": float(lnt), "lat": float(lat),
        "areaCode": area_code, "addressDetail": detail,
    }


def _resolve_store_id(store_payload: dict[str, Any], store_name: str) -> int:
    """从门店列表载荷里解析出门店 id（定字段优先，全文回退）。"""
    store_record = _match_record(
        _result_records(store_payload), store_name, "门店",
        fields=("name", "storeName", "shopName", "title"),
    )
    store_id = _pick(store_record, ("id", "storeId", "storeNo"))
    if store_id in (None, ""):
        raise LookupError("门店记录缺少 id："
                          + json.dumps(store_record, ensure_ascii=False)[:300])
    return int(store_id)


def _resolve_address(addr_payload: dict[str, Any], common_address: str) -> dict[str, Any]:
    """从常用地址列表载荷里解析出下单用坐标与地址字段。"""
    address_record = _match_record(
        _result_records(addr_payload), common_address, "常用地址",
        fields=("contactName", "name", "position", "address", "addressDetail"),
    )
    return _address_from_record(address_record)


def _cached_store_id(config: Any, store_name: str) -> int | None:
    """门店名与缓存一致时直接复用 id，跳过一次门店列表查询。"""
    cached_name = str(getattr(config, "sss_store_name_cached", "") or "")
    cached_id = getattr(config, "sss_store_id", None)
    if cached_id not in (None, "") and cached_name == store_name:
        try:
            return int(cached_id)
        except (TypeError, ValueError):
            return None
    return None


def _save_store_id(config: Any, store_name: str, store_id: int,
                   store_cache_callback: Callable[[str, int], None] | None = None) -> None:
    """记住门店 id 供下次复用；写盘失败不阻断主流程。"""
    try:
        config.sss_store_id = int(store_id)
        config.sss_store_name_cached = store_name
        if hasattr(config, "save"):
            config.save()
    except Exception:
        pass
    if store_cache_callback is not None:
        try:
            store_cache_callback(store_name, int(store_id))
        except Exception:
            # 缓存只是性能优化；桥接层回写失败不应影响当前已确认的门店。
            pass


def _prepare_store_and_address(fetch_json: Callable[[str], dict[str, Any]],
                               config: Any, store_name: str,
                               common_address: str, use_fixed_address: bool,
                               callback: Callable[[str], Any] | None,
                               parallel: bool = True,
                               store_cache_callback: Callable[[str, int], None] | None = None,
                               ) -> tuple[int, dict[str, Any]]:
    """准备下单所需的门店 id 与地址（接口/浏览器分支共用）。

    ``fetch_json`` 是 ``client.get_json`` 或浏览器版 ``_sss_api`` 的 GET
    封装。门店 id 命中缓存则跳过门店查询；门店与地址查询并行发起。
    ``parallel=False`` 时串行查询：Playwright 的 ``page.evaluate`` 非
    线程安全，浏览器分支必须串行。
    """
    store_id = _cached_store_id(config, store_name)
    need_store = store_id is None
    need_addr = not use_fixed_address

    store_payload: dict[str, Any] | None = None
    addr_payload: dict[str, Any] | None = None
    if need_store or need_addr:
        jobs: list[tuple[str, str]] = []
        if need_store:
            jobs.append(("store", _STORE_LIST_PATH))
        if need_addr:
            jobs.append(("addr", _FREQUENT_ADDR_PATH))
        if parallel and len(jobs) > 1:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {kind: executor.submit(fetch_json, path)
                           for kind, path in jobs}
                if "store" in futures:
                    store_payload = futures["store"].result()
                if "addr" in futures:
                    addr_payload = futures["addr"].result()
        else:
            for kind, path in jobs:
                if kind == "store":
                    store_payload = fetch_json(path)
                else:
                    addr_payload = fetch_json(path)

    if need_store:
        assert store_payload is not None
        store_id = _resolve_store_id(store_payload, store_name)
        assert store_id is not None
        _save_store_id(config, store_name, store_id, store_cache_callback)
        _emit(callback, f"门店「{store_name}」就绪（id={store_id}）")
    else:
        assert store_id is not None
        _emit(callback, f"门店「{store_name}」命中缓存（id={store_id}），跳过查询")

    if use_fixed_address:
        address = _fixed_address_from_config(config)
        _emit(callback,
              f"使用固定地址：{address['addressDetail']} @ {address['lnt']},{address['lat']}")
    else:
        assert addr_payload is not None
        address = _resolve_address(addr_payload, common_address)
        _emit(callback,
              f"门店「{store_name}」与常用地址「{common_address}」就绪"
              f"（{address['addressDetail']} @ {address['lnt']},{address['lat']}）")
    return store_id, address


def _stable_client_request_id(account: str, sheet: str, row: Any,
                              fingerprint: "OrderFingerprint") -> str:
    """同一账号/表/行/订单内容的稳定 UUID；人工重试时复用同一个值。

    注意：抓包确认的平台接口没有幂等字段，本 ID 默认不发送，仅用于本地
    日志/诊断与未来平台支持幂等字段时的稳定键。
    """
    material = "|".join(("yikou-sss", str(account or ""), str(sheet), str(row),
                         "|".join(fingerprint)))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, material))


def _collect_tasks(orders_by_sheet: dict[str, list[dict[str, Any]]],
                   store_id: int, address: dict[str, Any],
                   goods_name: str, *,
                   account: str = "",
                   batch_id: str = "",
                   idempotency_field: str = "",
                   now: _dt.datetime | None = None) -> list[dict[str, Any]]:
    """把午餐/晚餐两表展平成待下单任务；送达时间每表只算一次。

    每条任务带完整 ``OrderFingerprint``（账号/门店/商品/地址/坐标/类型等）
    以及稳定 ``client_request_id``，避免仅凭姓名+电话+门牌+时间误判。

    ``now`` 由调用方传入时复用同一时刻：日期闸门（云端识别日期 vs 送达日期）
    与报文里的 ``expectedDeliveryTime`` 必须基于同一个瞬间计算，否则在
    16:00 / 午夜这样的边界上会出现两套日期。
    """
    now = now or _dt.datetime.now()
    tasks: list[dict[str, Any]] = []
    for sheet_name in DEFAULT_SHEETS:
        orders = orders_by_sheet.get(sheet_name, [])
        if not orders:
            continue
        is_dinner = sheet_name == "晚餐"
        expected_time = compute_delivery_time(is_dinner, now)
        for order in orders:
            identifier = f"第 {order['row']} 行 {order.get('name') or '未填写'}"
            fingerprint = _payload_fingerprint(
                build_order_payload(order, is_dinner, store_id, address, goods_name,
                                    expected_time=expected_time),
                account=account,
            )
            client_request_id = _stable_client_request_id(account, sheet_name,
                                                          order.get("row", ""), fingerprint)
            payload = build_order_payload(
                order, is_dinner, store_id, address, goods_name,
                expected_time=expected_time,
                client_request_id=client_request_id,
                idempotency_field=idempotency_field,
            )
            tasks.append({
                "sheet": sheet_name,
                "identifier": identifier,
                "payload": payload,
                "fingerprint": fingerprint,
                "account": account,
                "batch_id": batch_id,
                "client_request_id": client_request_id,
            })
    return tasks


def _run_dry_run(tasks: list[dict[str, Any]],
                 callback: Callable[[str], Any] | None) -> dict[str, Any]:
    """干跑：只组装打印报文，不真实提交；只预览前几单全文。"""
    _emit(callback, f"【干跑模式】只组装报文，不真实提交，共 {len(tasks)} 单")
    for index, task in enumerate(tasks):
        if index < _DRY_RUN_PREVIEW_N:
            _emit(callback,
                  f"【干跑】{task['identifier']} 报文："
                  f"{json.dumps(task['payload'], ensure_ascii=False)}")
    if len(tasks) > _DRY_RUN_PREVIEW_N:
        _emit(callback, f"【干跑】…其余 {len(tasks) - _DRY_RUN_PREVIEW_N} 单报文已省略")
    _emit(callback, f"闪时送干跑完成：已组装 {len(tasks)}/{len(tasks)}，未创建真实订单")
    return {
        "status": "dry_run",
        "processed": len(tasks),
        "created": 0,
        "previewed": len(tasks),
        "submitted": 0,
        "reconciled": False,
        "uncertain": False,
    }


def _balance_precheck(tasks: list[dict[str, Any]], total: float | None,
                     unit_price: float, callback: Callable[[str], Any] | None
                     ) -> tuple[str, float]:
    """阻止余额未知或不足的真实批次，且保证尚未发送任何 POST。"""
    estimate = round(max(0.0, unit_price) * len(tasks), 2)
    if total is None:
        _emit(callback, "余额未知，为避免产生无法完成的订单，本批不会提交")
        return "balance_unknown", estimate
    if unit_price > 0 and total < estimate:
        shortage = round(estimate - total, 2)
        _emit(callback, f"余额不足：当前可用 {total:.2f}，本批预计 {estimate:.2f}，"
                        f"还差 {shortage:.2f}，本批不会提交")
        return "insufficient_balance", estimate
    return "ok", estimate


def _preflight_tasks(tasks: list[dict[str, Any]],
                     fetch_json: Callable[[str], dict[str, Any]],
                     callback: Callable[[str], Any] | None
                     ) -> tuple[str, _Reconciliation | None]:
    """只读检查订单列表；预检失败时绝不进入 POST 阶段。"""
    reconciliation = _safe_reconcile(tasks, fetch_json, callback, "预检站内对账", attempts=1)
    if reconciliation is None:
        return "preflight_uncertain", None
    if reconciliation.duplicate_count:
        _emit(callback, "预检发现站内重复订单，停止且不会提交")
        return "duplicate_detected", reconciliation
    if reconciliation.confirmed:
        _emit(callback, f"预检发现已有 {len(reconciliation.confirmed)} 单匹配，"
                        "这些订单不会重复提交")
    return "preflight_ok", reconciliation


def _check_success(resp: dict[str, Any]) -> None:
    """下单响应成功则静默返回，否则抛错（message 优先）。

    三种情况必须分开，因为它们对"要不要重发"的结论完全不同：

    * ``success`` 为真 → 成功（仍要等站内对账确认）；
    * ``success`` 显式为假（含 ``0`` / ``"false"``）→ **服务端明确拒绝**，
      可以安全地关闭未决记录；
    * 响应里**根本没有** ``success`` 字段 → 这不是一次可确认的响应（可能是网关
      返回的其它 JSON、平台改了协议），一律按"结果未知"处理，绝不能当成拒绝 ——
      当成拒绝就会关闭记录，下一次运行就能重复下单。
    """
    if resp.get("success"):
        return
    if "success" not in resp:
        raise _SubmissionUncertain(
            "下单响应缺少 success 字段，无法确认是否已创建："
            + json.dumps(resp, ensure_ascii=False)[:200])
    message = str(resp.get("message") or "")
    lowered = message.lower()
    if any(kw.lower() in lowered or (kw in message) for kw in _BALANCE_KEYWORDS):
        raise _BalanceDepleted(message or json.dumps(resp, ensure_ascii=False)[:200])
    raise LookupError(message or json.dumps(resp, ensure_ascii=False)[:200])


class _BalanceDepleted(LookupError):
    """余额不足：中断整批的标记异常（不重登、不补提、不重试）。"""


class _AuthExpired(RuntimeError):
    """worker 遇到 401 时交由主线程统一重登的标记异常。"""


class _SubmissionUncertain(RuntimeError):
    """POST 未得到可确认结果，必须先查站，不能直接重发。"""


@dataclass
class _SubmitResult:
    succeeded: set[str] = field(default_factory=set)
    failures: list[tuple[str, str]] = field(default_factory=list)
    uncertain: list[tuple[str, str]] = field(default_factory=list)
    auth_error: str = ""
    balance_error: str = ""
    stopped: bool = False


@dataclass
class _Reconciliation:
    confirmed: set[str]
    missing: list[dict[str, Any]]
    duplicate_count: int
    matched_count: int


def _is_auth_expired(exc: BaseException) -> bool:
    """判断是否为登录态过期，过期才值得打断整批去重登。

    闪时送实际使用 HTTP 200 + code=10000 + “token失效，请重新登陆”，
    因此不能只看 401。
    """
    text = str(exc)
    lowered = text.lower()
    keywords = (
        "401", "code=10000", "code: 10000", "code:10000",
        "token失效", "token 失效", "token invalid", "invalid token",
        "登录态失效", "登录已失效", "登录过期", "登录已过期",
        "请重新登陆", "请重新登录", "unauthorized", "login expired",
    )
    return any(keyword in text or keyword in lowered for keyword in keywords)


def _with_auth_relogin(operation: Callable[[], Any], relogin: Callable[[], None],
                       callback: Callable[[str], Any] | None, label: str) -> Any:
    """执行只读/准备操作；确认登录态失效时重登一次并重试。"""
    try:
        return operation()
    except Exception as exc:
        if not _is_auth_expired(exc):
            raise
        _emit(callback, f"{label}时登录态失效，正在重新登录…")
        relogin()
        return operation()


def _post_one(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """单次下单 POST；任何非确认响应均改为待对账状态。

    ``ApiError`` 之外**任何**异常（连接池坏了、类型错误、被取消）同样意味着
    "没拿到可确认响应"，一律按未知处理：只有明确的 401/登录失效才允许打断整批去重登。
    """
    try:
        return client.post_json(_CREATE_ORDER_PATH, payload)
    except ApiError as exc:
        if _is_auth_expired(exc):
            raise _AuthExpired(str(exc)) from exc
        raise _SubmissionUncertain(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 非 ApiError 的意外同样无法确认结果
        raise _SubmissionUncertain(
            f"{type(exc).__name__}: {exc}") from exc


class OrderFingerprint(NamedTuple):
    """订单身份指纹：仅凭姓名/电话/门牌/时间不再足够。"""

    receive_name: str = ""
    receive_phone: str = ""
    door_num: str = ""
    expected_delivery_time: str = ""
    account: str = ""
    store_id: str = ""
    goods_name: str = ""
    goods_num: str = ""
    address_detail: str = ""
    area_code: str = ""
    lnt: str = ""
    lat: str = ""
    order_type: str = ""

    def as_dict(self) -> dict[str, str]:
        """把结果对象转成普通 dict（供日志/回报使用）。"""
        return self._asdict()


def _normalise_text(value: Any, *, compact: bool = False) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return _DOOR_RE.sub("", text) if compact else text


def _normalise_number(value: Any) -> str:
    """把门店/商品数量/坐标规范成稳定字符串，避免 1 与 1.0 误判不同。"""
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _normalise_text(value)
    if not (number == number and number not in (float("inf"), float("-inf"))):  # NaN/Inf
        return _normalise_text(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.7f}".rstrip("0").rstrip(".")


def _normalise_int_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return _normalise_number(value)


def _normalise_delivery_time(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) or re.fullmatch(r"\d{10,13}", str(value).strip()):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            parsed = _dt.datetime.fromtimestamp(number, tz=_dt.timezone(_dt.timedelta(hours=8)))
        except (OverflowError, OSError, ValueError):
            return str(value)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    text = unicodedata.normalize("NFKC", str(value or "")).strip().replace("T", " ")
    if not text:
        return ""
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return text
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _normalise_order_type(value: Any) -> str:
    if value in (None, ""):
        return ""
    numeric = _normalise_int_text(value)
    if numeric.isdigit():
        return numeric
    text = _normalise_text(value)
    return {"预约单": "2", "即时单": "1", "外卖单": "1", "自取单": "3"}.get(text, text)


def _payload_goods(payload: dict[str, Any]) -> tuple[str, str]:
    goods = payload.get("goodsDetail")
    if isinstance(goods, list) and goods and isinstance(goods[0], dict):
        item = goods[0]
        return (_normalise_text(item.get("goodsName")),
                _normalise_int_text(item.get("goodsNum")))
    return "", ""


def _record_goods(record: dict[str, Any]) -> tuple[str, str]:
    for key in ("goodsDetail", "goods", "generalGoods", "products", "productList"):
        items = record.get(key)
        if not isinstance(items, list) or not items:
            continue
        first = items[0]
        if not isinstance(first, dict):
            continue
        name = _pick(first, ("goodsName", "name", "title", "productName", "goods_title"))
        num = _pick(first, ("goodsNum", "num", "count", "quantity", "goods_num"))
        if name not in (None, "") or num not in (None, ""):
            return _normalise_text(name), _normalise_int_text(num)
    # 兼容把商品名/数量拍平在订单顶层字段的响应。
    return (_normalise_text(_pick(record, ("goodsName", "goods_name", "productName"))),
            _normalise_int_text(_pick(record, ("goodsNum", "goods_num", "goodsCount"))))


def _record_address(record: dict[str, Any]) -> dict[str, Any]:
    address = record.get("receiveAddress")
    if not isinstance(address, dict):
        address = record.get("address") if isinstance(record.get("address"), dict) else {}
    return address


_FLAT_ADDRESS_KEYS = (
    "recipientAddress", "receiverAddress", "address", "addressDetail",
    "detailedAddress", "deliveryAddress", "position",
)


def _record_address_text(record: dict[str, Any]) -> str:
    """取拍平成一整串的收货地址（订单列表把地址拍平成 ``recipientAddress``）。

    新结构（2026-09 实测）列表记录没有 ``receiveAddress`` 子对象，只有
    ``recipientAddress`` 字符串，门牌号也被拼进这一串，因此这里原样返回，
    由对账阶段与下单报文的 ``addressDetail + doorNum`` 组合后比较。
    """
    if _record_address(record):
        return ""
    for key in _FLAT_ADDRESS_KEYS:
        value = _unwrap_scalar(record.get(key))
        if isinstance(value, str) and value.strip():
            return _normalise_text(value)
    return ""


def _record_region_code(address: dict[str, Any]) -> str:
    value = _pick(address, ("areaCode", "adcode", "area_code", "regionCode", "code"))
    if value not in (None, ""):
        return _normalise_text(value)
    region = address.get("regionId")
    if isinstance(region, list) and region:
        return _normalise_text(region[-1])
    return ""


def _payload_fingerprint(payload: dict[str, Any], *, account: str = "") -> OrderFingerprint:
    """从下单请求体生成完整订单指纹。"""
    address = payload.get("receiveAddress")
    address = address if isinstance(address, dict) else {}
    goods_name, goods_num = _payload_goods(payload)
    return OrderFingerprint(
        receive_name=_normalise_text(payload.get("receiveName")),
        receive_phone=_normalise_text(payload.get("receivePhone"), compact=True),
        door_num=_normalise_text(address.get("doorNum"), compact=True),
        expected_delivery_time=_normalise_delivery_time(payload.get("expectedDeliveryTime")),
        account=_normalise_text(account, compact=True),
        store_id=_normalise_int_text(payload.get("storeId")),
        goods_name=goods_name,
        goods_num=goods_num,
        address_detail=_normalise_text(address.get("addressDetail")),
        area_code=_normalise_text(address.get("areaCode")),
        lnt=_normalise_number(address.get("lnt")),
        lat=_normalise_number(address.get("lat")),
        order_type=_normalise_int_text(payload.get("orderType")),
    )


def _legacy_fingerprint(value: tuple[str, str, str, str]) -> OrderFingerprint:
    return OrderFingerprint(
        receive_name=value[0], receive_phone=value[1], door_num=value[2],
        expected_delivery_time=value[3],
    )


def _task_fingerprint(task: dict[str, Any]) -> OrderFingerprint:
    value = task.get("fingerprint")
    if isinstance(value, OrderFingerprint):
        return value
    if isinstance(value, tuple) and len(value) == 4:
        return _legacy_fingerprint(value)  # type: ignore[arg-type]
    return _payload_fingerprint(task["payload"], account=str(task.get("account") or ""))


def _order_record_fingerprint(record: dict[str, Any], *, account: str = "") -> OrderFingerprint:
    """从站内列表记录提取完整指纹；缺失字段留空，由对账阶段判定。

    列表接口（2026-09 实测）返回的是概要结构：姓名/电话/地址为
    ``recipientName``/``recipientPhone``(数组)/``recipientAddress``(整串)，
    且通常不带 ``storeId``/``goodsDetail``/坐标。缺失的字段留空，由
    ``_reconcile_tasks`` 区分“字段未返回”与“值相冲突”。
    """
    address = _record_address(record)
    flat_address = _record_address_text(record)
    user = record.get("user") if isinstance(record.get("user"), dict) else {}
    store = record.get("store") if isinstance(record.get("store"), dict) else {}
    goods_name, goods_num = _record_goods(record)
    return OrderFingerprint(
        receive_name=_normalise_text(_pick_scalar(record, (
            "receiveName", "receiverName", "recipientName", "consigneeName",
            "contactName", "contacts", "recipient", "consignee", "receiver", "name")) or
            _pick(address, ("contact", "contactName", "name"))),
        receive_phone=_normalise_text(_pick_scalar(record, (
            "receivePhone", "receiverPhone", "recipientPhone", "consigneePhone",
            "contactPhone", "recipientMobile", "receiverMobile", "mobilePhone",
            "phone", "mobile")) or
            _pick_scalar(address, ("mobile", "phone", "tel")), compact=True),
        door_num=_normalise_text(_pick_scalar(address, (
            "doorNum", "houseNum", "door", "description")) or
            _pick_scalar(record, ("recipientDoorNum", "receiverDoorNum",
                                  "doorNum", "houseNum")), compact=True),
        expected_delivery_time=_normalise_delivery_time(_pick_scalar(record, (
            "expectedDeliveryTime", "appointmentTime", "deliveryTime", "expected_time"))),
        account=_normalise_text(_pick_scalar(record, (
            "account", "sssAccount", "customerMobile", "loginMobile")) or
            _pick_scalar(user, ("mobile", "phone", "account")) or account, compact=True),
        store_id=_normalise_int_text(_pick_scalar(record, ("storeId", "storeID", "store_id")) or
                                     _pick_scalar(store, ("id", "storeId"))),
        goods_name=goods_name,
        goods_num=goods_num,
        address_detail=_normalise_text(_pick(address, (
            "addressDetail", "address_detail", "address", "position", "description")) or
            _pick(record, ("addressDetail", "address_detail", "position")) or
            flat_address),
        area_code=_record_region_code(address) or _record_region_code(record),
        lnt=_normalise_number(_pick_scalar(address, ("lnt", "lng", "longitude", "lon")) or
                              _pick_scalar(record, ("lnt", "lng", "longitude", "lon"))),
        lat=_normalise_number(_pick_scalar(address, ("lat", "latitude")) or
                              _pick_scalar(record, ("lat", "latitude"))),
        order_type=_normalise_order_type(_pick_scalar(record, ("orderType", "order_type")) or
                                         _pick_scalar(record, ("orderTypeFormat",))),
    )


def _record_core(fingerprint: OrderFingerprint) -> tuple[str, str, str]:
    """可用来判定“是否本批订单”的最小身份：姓名 + 电话 + 预约时间。"""
    return (fingerprint.receive_name, fingerprint.receive_phone,
            fingerprint.expected_delivery_time)


def _address_combo(fingerprint: OrderFingerprint) -> str:
    """把地址折成单一串：``addressDetail + doorNum``。

    列表接口把两者拼成一整串（``recipientAddress``），下单报文则分开存放，
    统一折叠后即可比较；返回空串表示该侧未提供地址。
    """
    return (_normalise_text(fingerprint.address_detail, compact=True)
            + _normalise_text(fingerprint.door_num, compact=True))


def _addresses_compatible(record_address: str, task_address: str) -> bool:
    """地址是否指向同一处：折叠后相等，或一方包含另一方。

    列表接口的地址是整串拼接（可能带省市区前缀、空格或“号”等尾缀），而任务
    地址由 ``addressDetail`` 与门牌拼成，两边很难逐字相等；用包含关系兼容
    这些格式差异，避免把同一订单误判成“其他订单”而重复提交。
    """
    if record_address == task_address:
        return True
    return record_address in task_address or task_address in record_address


def _fingerprint_compatibility(record: OrderFingerprint,
                               task: OrderFingerprint) -> tuple[bool, bool]:
    """比较站内记录与某条任务指纹，返回 ``(是否冲突, 是否因缺字段无法判定)``。

    列表接口只返回概要字段，因此缺失的字段一律忽略（视为“未提供”），不能
    当成“不同”；只有站内明确给出、且与任务不一致的字段才算冲突。地址是
    判断归属的关键，记录缺少地址时标记为无法判定，由调用方 fail-closed。
    """
    record_address = _address_combo(record)
    task_address = _address_combo(task)
    if task_address and not record_address:
        return False, True
    if record_address and task_address and not _addresses_compatible(
            record_address, task_address):
        return True, False
    for name in ("area_code", "lnt", "lat", "store_id", "goods_name",
                 "goods_num", "order_type", "account"):
        on_site = getattr(record, name)
        expected = getattr(task, name)
        if not on_site:
            continue
        if expected and on_site != expected:
            return True, False
    return False, False


def _list_records(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    """兼容闪时送列表响应的 ``result``/``data`` 两层分页结构。"""
    for container in (payload.get("result"), payload.get("data"), payload):
        if isinstance(container, list):
            return ([item for item in container if isinstance(item, dict)], None)
        if not isinstance(container, dict):
            continue
        records = container.get("records")
        if records is None:
            records = container.get("list")
        if isinstance(records, list):
            try:
                total = int(container.get("total")) if container.get("total") is not None else None
            except (TypeError, ValueError):
                total = None
            return ([item for item in records if isinstance(item, dict)], total)
        # 服务端对不支持的过滤参数返回 success:true + 无 records 的空壳
        # （2026-09-10 实测：带 startTime/endTime/statusList 即如此）。调用方
        # 已改为无过滤参数 + 本地过滤；此处保留 fail-closed，但报错必须点名
        # 结构，避免与“真的零订单”混淆。
        if isinstance(container, dict) and container:
            raise LookupError(
                "订单列表响应缺少 records/list（服务端可能不支持本次查询参数"
                "或结构已变化），为避免重复下单已停止")
    raise LookupError("订单列表响应缺少 records/list")


def _build_list_prefilter(tasks: list[dict[str, Any]],
                          *,
                          now: _dt.datetime | None = None) -> dict[str, Any]:
    """按目标送达日构造「服务端预筛」查询参数（epoch 毫秒时间窗）。

    2026-09-10 实测纠正了此前「服务端不支持时间过滤」的结论：服务端字段名是
    ``startTime``/``endTime``，**必须是 epoch 毫秒**（传字符串会被 Spring 以
    ``BindException`` 拒绝），而 ``statusList`` 才是真的无效。实测同一账号
    一次对账由 23 页/46s 降到 1 页/1.7s，且过滤后的记录跑原有对账逻辑结论
    完全一致。

    窗口在目标日基础上前后各放宽 ``_SERVER_PREFILTER_MARGIN_DAYS`` 天：订单
    创建时间可能早于或晚于预约送达日，放宽只是多取几条，真正的判定依然由
    本地按送达日/状态过滤负责。

    **刻意不带 ``status`` 参数**：2026-09-11 实测送达日 09-10 的订单全部是
    ``status=3``（已发单），而目标日 09-11 的订单是 ``status=2``。也就是说
    同一批订单在派发后会从 2 变成 3，硬编码 ``status=2`` 会把这批订单直接
    筛没、误判成「缺失」。``statusList`` 又无效、``status`` 是单值，无法
    一次查多个状态，因此只按时间窗预筛。
    """
    days: list[_dt.date] = []
    for task in tasks:
        fingerprint = task.get("fingerprint") if isinstance(task, dict) else None
        raw = str(getattr(fingerprint, "expected_delivery_time", "") or "")[:10]
        try:
            days.append(_dt.datetime.strptime(raw, "%Y-%m-%d").date())
        except ValueError:
            continue
    if not days:
        return {}

    margin = _dt.timedelta(days=max(0, _SERVER_PREFILTER_MARGIN_DAYS))
    timezone = (now or _dt.datetime.now()).astimezone().tzinfo or _dt.timezone.utc
    start = _dt.datetime.combine(min(days) - margin, _dt.time.min, tzinfo=timezone)
    end = _dt.datetime.combine(max(days) + margin, _dt.time.max, tzinfo=timezone)
    return {
        "startTime": int(start.timestamp() * 1000),
        "endTime": int(end.timestamp() * 1000),
    }


def _list_pending_orders(fetch_json: Callable[[str], dict[str, Any]],
                         tasks: list[dict[str, Any]],
                         *,
                         prefilter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """分页拉取订单列表并在本地按预约日期/状态过滤，不发任何写请求。

    ``prefilter`` 非空时只作为服务端粗筛条件（缩小翻页范围）；无论服务端返回
    什么，送达日与活跃状态的判定一律由本地过滤负责，判定标准不因预筛改变。
    """
    wanted_days = {_task_fingerprint(task).expected_delivery_time[:10] for task in tasks}
    records: list[dict[str, Any]] = []
    page_no = 1
    page_size = _LIST_PAGE_SIZE
    sweep_started = time.perf_counter()
    page_count = 0
    prefilter_active = bool(prefilter)
    while page_no <= 100:
        query = urlencode({
            "pageNo": page_no,
            "pageSize": page_size,
            "sortType": 1,
            "sort": 1,
            **(prefilter or {}),
        })
        page_started = time.perf_counter()
        payload = fetch_json(f"{_ORDER_LIST_PATH}?{query}")
        page_seconds = time.perf_counter() - page_started
        page_count += 1
        if payload.get("success") is False:
            raise LookupError(str(payload.get("message") or "订单列表查询失败"))
        try:
            page_records, total = _list_records(payload)
        except LookupError:
            # 服务端对某些查询参数返回 success:true 的「空壳」（无 records）。
            # 埋点里连同完整查询串与原始报文一起记录，便于定位是哪个参数触发。
            _trace(f"list page {page_no} 结构异常，查询串={query} "
                   f"原始报文={json.dumps(payload, ensure_ascii=False)[:400]}")
            raise
        _trace(f"list page {page_no}: {page_seconds:.2f}s 返回 {len(page_records)} 条 total={total}")
        if not page_records:
            break
        for record in page_records:
            if _record_active_for_days(record, wanted_days):
                records.append(record)
        if len(page_records) < page_size:
            break
        if total is not None and page_no * page_size >= total:
            break
        page_no += 1
    else:
        raise LookupError("订单列表分页超过 100 页，拒绝继续下单")
    elapsed = time.perf_counter() - sweep_started
    _LAST_LIST_STATS.clear()
    _LAST_LIST_STATS.update({
        "pages": page_count,
        "elapsed_s": round(elapsed, 2),
        "matched": len(records),
        "prefilter": prefilter_active,
        "per_page_s": round(elapsed / page_count, 3) if page_count else 0.0,
        "throughput_per_s": round(len(records) / elapsed, 2) if elapsed > 0 else 0.0,
    })
    _trace(f"list sweep 结束：{page_count} 页 {elapsed:.1f}s，"
           f"命中目标日 {len(records)} 条，预筛={'开' if prefilter_active else '关'}")
    return records


#: 最近一次列表扫描的吞吐统计（界面/日志用；只读展示，不参与任何判定）。
_LAST_LIST_STATS: dict[str, Any] = {}


def _record_active_for_days(record: dict[str, Any], wanted_days: set[str]) -> bool:
    """本地过滤：预约送达日在目标日期内且状态为活跃（未取消/未退款）。"""
    raw_expected = _pick(record, ("expectedDeliveryTime", "expected_delivery_time",
                                  "appointmentTime", "appointment_time"))
    day = _normalise_delivery_time(raw_expected)[:10]
    if day not in wanted_days:
        return False
    status = _pick(record, ("status", "orderStatus", "state"))
    try:
        code = int(str(status).strip())
    except (TypeError, ValueError):
        # 状态不可读时按“不排除”处理，避免字段缺失导致整批误判缺失。
        return True
    # 取消/退款/忽略类状态不计入在途；其余一律视为活跃。
    return code not in _INACTIVE_ORDER_STATUS


def _record_created_timestamp(record: dict[str, Any]) -> float | None:
    """解析站内订单创建时间，用于排除本批次之前由其他设备创建的相似订单。"""
    value = _pick(record, (
        "created_at", "createdAt", "createTime", "createdTime", "gmtCreate",
        "orderTime", "submitTime", "created"))
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 10_000_000_000 else number
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if re.fullmatch(r"\d{10,13}", text):
        number = int(text)
        return number / 1000.0 if number > 10_000_000_000 else float(number)
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    return parsed.timestamp()


def _reconcile_tasks(tasks: list[dict[str, Any]],
                     fetch_json: Callable[[str], dict[str, Any]],
                     *, created_after: float | None = None,
                     created_before: float | None = None,
                     prefilter: dict[str, Any] | None = None) -> _Reconciliation:
    """按订单身份对账，兼容列表接口的概要结构。

    匹配以「姓名 + 电话 + 预约时间」为核心；站内明确给出、却与任务不一致的
    字段（门店/商品/坐标等）判定为他人订单而忽略；站内未返回的字段视为
    “未提供”而不参与比较。地址是归属的关键：若记录疑似本批订单却缺少地址，
    则无法安全判定，直接 fail-closed，绝不猜测。

    ``created_after``/``created_before`` 用于收尾对账时只接受本批次时间窗口
    内创建的订单；站内记录缺少创建时间时无法证明归属，按“不排除”处理。

    ``prefilter`` 只是服务端粗筛（参见 ``_build_list_prefilter``），不改变
    任何本地判定标准。
    """
    expected = Counter(_task_fingerprint(task) for task in tasks)
    waiting: dict[OrderFingerprint, list[dict[str, Any]]] = {}
    by_core: dict[tuple[str, str, str], list[OrderFingerprint]] = {}
    for task in tasks:
        fingerprint = _task_fingerprint(task)
        waiting.setdefault(fingerprint, []).append(task)
    for fingerprint in expected:
        by_core.setdefault(_record_core(fingerprint), []).append(fingerprint)
    expected_accounts = {fingerprint.account for fingerprint in expected if fingerprint.account}
    fallback_account = next(iter(expected_accounts)) if len(expected_accounts) == 1 else ""

    actual = Counter()
    for record in _list_pending_orders(fetch_json, tasks, prefilter=prefilter):
        created_at = _record_created_timestamp(record)
        if created_at is not None:
            if created_after is not None and created_at < created_after:
                continue
            if created_before is not None and created_at > created_before:
                continue
        fingerprint = _order_record_fingerprint(record, account=fallback_account)
        core = _record_core(fingerprint)
        if not all(core):
            # 连姓名/电话/预约时间都读不出，无法排除是本批订单，必须 fail-closed。
            raise LookupError("订单列表记录缺少姓名、电话或预约送达时间，无法安全对账："
                              + json.dumps(record, ensure_ascii=False)[:300])
        candidates = by_core.get(core, [])
        matched: OrderFingerprint | None = None
        undecidable = False
        for candidate in candidates:
            conflict, unknown = _fingerprint_compatibility(fingerprint, candidate)
            if unknown:
                undecidable = True
                continue
            if not conflict:
                matched = candidate
                break
        if matched is not None:
            actual[matched] += 1
        elif undecidable:
            raise LookupError("订单列表记录疑似本批订单但缺少地址等字段，无法安全对账："
                              + json.dumps(record, ensure_ascii=False)[:300])
        # 否则：与同人同时间的任务在已知字段上冲突，判定为其他订单，忽略。

    confirmed: set[str] = set()
    missing: list[dict[str, Any]] = []
    duplicate_count = 0
    for fingerprint, grouped_tasks in waiting.items():
        on_site = actual[fingerprint]
        confirmed.update(task["identifier"] for task in grouped_tasks[:min(len(grouped_tasks), on_site)])
        missing.extend(grouped_tasks[min(len(grouped_tasks), on_site):])
        duplicate_count += max(0, on_site - len(grouped_tasks))
    return _Reconciliation(confirmed, missing, duplicate_count, sum(actual.values()))


def _emit_reconciliation(callback: Callable[[str], Any] | None, label: str,
                         reconciliation: _Reconciliation, total: int) -> None:
    _emit(callback, f"{label}：站内匹配 {reconciliation.matched_count}/{total} 单，"
                    f"缺失 {len(reconciliation.missing)} 单，"
                    f"重复 {reconciliation.duplicate_count} 单")


def resolve_create_workers(config: Any) -> int:
    """本批创建订单的并发路数：取配置值并夹到 ``[1, _SSS_CREATE_MAX_WORKERS_CEILING]``。

    1 = 串行（平台限流或担心并发时的回退值）。
    """
    try:
        workers = int(getattr(config, "sss_max_workers",
                              _FALLBACK_SSS_CREATE_WORKERS))
    except (TypeError, ValueError):
        workers = _FALLBACK_SSS_CREATE_WORKERS
    return max(1, min(_SSS_CREATE_MAX_WORKERS_CEILING, workers))


def resolve_submit_min_interval_s(config: Any) -> float:
    """本批提交的最小间隔（秒）：取配置值并夹到 ``[0, 60]``；0 = 关闭节流。

    拿不到配置（缺字段或取值不可解析）时按出厂默认（2.5 秒）保守节流——宁可
    按节奏慢发，也不要全速连发触发平台的快速驳回（内部异常，且每个请求都会
    消耗一个平台订单序列号）。
    """
    try:
        interval = float(getattr(config, "sss_submit_min_interval_s",
                                 _DEFAULT_SSS_SUBMIT_MIN_INTERVAL_S))
    except (TypeError, ValueError):
        interval = _DEFAULT_SSS_SUBMIT_MIN_INTERVAL_S
    if interval != interval:  # NaN
        interval = _DEFAULT_SSS_SUBMIT_MIN_INTERVAL_S
    return max(0.0, min(_SSS_SUBMIT_MIN_INTERVAL_CEILING_S, interval))


def _emit_round_summary(callback: Callable[[str], Any] | None,
                        latencies: list[float],
                        started_at: float,
                        min_interval_s: float | None = None) -> None:
    """本轮提交的实测汇总：并发到底有没有用，全看这一行。

    吞吐 = 单数 / 墙钟，每单均时 = 服务端建单耗时。启用提交节流时补一句当前
    生效的最小间隔，便于与平台受理节奏（现场观察约 1 单 / 2.1 秒）对照。
    """
    if not latencies:
        return
    wall = max(0.0, time.perf_counter() - started_at)
    average = sum(latencies) / len(latencies)
    throughput = (len(latencies) / wall) if wall > 0 else 0.0
    message = (f"本轮提交 {len(latencies)} 单，耗时 {wall:.1f} 秒，"
               f"每单平均 {average:.1f} 秒（吞吐 {throughput:.2f} 单/秒）")
    try:
        pacing = float(min_interval_s) if min_interval_s else 0.0
    except (TypeError, ValueError):
        pacing = 0.0
    if pacing > 0:
        message += f"；提交最小间隔 {pacing:g} 秒"
    _emit(callback, message)


def _is_rate_reject_response(response: Any) -> bool:
    """平台「受理超速」特征的快速驳回：内部异常且 message 含 ``IndexOutOfBounds``。

    2026-10-09 现场（见 docs/闪时送周期失败与序号跳号排查-2026-10-09.md）：超过
    平台受理节奏的建单请求在约 0.3-1 秒内返回
    ``java.lang.IndexOutOfBoundsException: Index: 0, Size: 0``，且仍会消耗一个
    平台订单序列号。该判定只用于提交节奏的自适应放宽（``_SubmitPacer.penalize``），
    不参与任何结果分类、记录或重发语义。
    """
    if not isinstance(response, dict) or not is_internal_error_payload(response):
        return False
    message = str(response.get("message") or response.get("msg") or "")
    return "IndexOutOfBounds" in message


class _SubmitPacer:
    """提交节流：令相邻两次 POST 的起点至少间隔 ``interval`` 秒。

    4 路滚动补位下，被快速驳回的车道能在 0.36 秒内补发下一单，实际发送节奏
    会被推到远超平台受理节奏的水平。这里在 POST 之前统一排队，让发送起点按
    最小间隔错开；出现平台的快速驳回特征时自动放宽间隔（``penalize``）。
    节流只影响节奏，不改变任何结果分类与重发语义。
    """

    def __init__(self, interval_s: float) -> None:
        self._lock = Lock()
        self._interval = max(0.0, float(interval_s or 0.0))
        self._ceiling = max(_SUBMIT_INTERVAL_PENALTY_MAX_S, self._interval)
        self._next_allowed = 0.0  # time.perf_counter 时间基准

    def wait(self, stop_event: Any = None,
             halted: Callable[[], bool] | None = None) -> bool:
        """阻塞到可以发送下一次 POST；返回 False = 等待期间停止/中止（未发送）。

        节流关闭（间隔为 0）时不等待也不中止，行为与未引入节流前完全一致；
        等待路径上出现停止、401 或余额不足时返回 False，调用方应把该任务记为
        「未发送」（它确实没有发出）。
        """
        if self._interval <= 0.0:
            return True

        def aborted() -> bool:
            if halted is not None and halted():
                return True
            return stop_event is not None and bool(stop_event.is_set())

        while True:
            if aborted():
                return False
            with self._lock:
                now = time.perf_counter()
                if now >= self._next_allowed:
                    self._next_allowed = now + self._interval
                    return True
                delay = min(self._next_allowed - now, 0.25)
            time.sleep(delay)

    def penalize(self) -> float | None:
        """平台快速驳回后放宽间隔；返回放宽后的值，未启用/已到上限时返回 None。"""
        with self._lock:
            if self._interval <= 0.0 or self._interval >= self._ceiling:
                return None
            self._interval = min(self._ceiling,
                                 self._interval * _SUBMIT_INTERVAL_PENALTY_FACTOR)
            return self._interval

    @property
    def interval(self) -> float:
        """当前生效的最小间隔（秒）。"""
        with self._lock:
            return self._interval


def _submit_tasks_concurrent(tasks: list[dict[str, Any]],
                             submit: Callable[[dict[str, Any]], dict[str, Any]],
                             stop_event: Any,
                             callback: Callable[[str], Any] | None,
                             max_workers: int = 4,
                             on_stop: Callable[[], None] | None = None,
                             min_interval_s: float = 0.0) -> _SubmitResult:
    """有界并发提交，停止、401、余额不足后不再派发新任务。同一轮最多保留
    ``max_workers`` 个在途 POST。请求超时或断链只记录为 ``uncertain``，由调用方
    查询订单列表确认，绝不在此处自动重发。

    ``max_workers == 1`` 时**在本函数的调用线程内串行执行**，不经过执行器线程：
    Playwright 同步 API 把 greenlet 绑定在创建它的线程上，浏览器表单分支的
    ``submit`` 一旦被丢进 ``ThreadPoolExecutor`` 工作线程就会每单立刻抛
    ``Cannot switch to a different thread``（2026-10-11 实测，102 单全部瞬间
    失败、每单平均 0.0 秒）。串行本来就轮不到流水线提速，内联执行语义完全
    等价，还省掉线程切换。

    ``min_interval_s`` > 0 时启用全局提交节流（见 `_SubmitPacer`）：相邻两次
    POST 的**起点**至少间隔该值，贴近平台对同一账号的建单受理节奏；出现平台
    快速驳回特征时自动放宽间隔。0 = 关闭节流（行为与引入节流前完全一致）。
    结束后输出一行本轮汇总（见 `_emit_round_summary`）。
    """
    result = _SubmitResult()
    workers = max(1, int(max_workers))
    try:
        pacing = max(0.0, float(min_interval_s or 0.0))
    except (TypeError, ValueError):
        pacing = 0.0
    pacer = _SubmitPacer(pacing)
    latencies: list[float] = []
    round_started = time.perf_counter()

    def halted() -> bool:
        return bool(stop_event.is_set() or result.auth_error or result.balance_error)

    def run_one(task: dict[str, Any]) -> tuple[str, str, str]:
        if stop_event.is_set():
            return task["identifier"], "stopped", ""
        if not pacer.wait(stop_event, halted):
            # 节流等待期间收到停止/中止：请求确实没有发出，按「未发送」处理
            # （与在派发前被停止拦下同义，不引入任何新的重发行为）。
            return task["identifier"], "stopped", ""
        interval_at_send = pacer.interval
        started = time.perf_counter()  # 计时只量 HTTP 本身，不含节流等待
        response: Any = None
        try:
            response = submit(task["payload"])
            _check_success(response)
        except _AuthExpired as exc:
            state, detail = "auth", str(exc)
        except _BalanceDepleted as exc:
            state, detail = "balance", str(exc)
        except (_SubmissionUncertain, SssTransportError) as exc:
            state, detail = "uncertain", str(exc)
        except Exception as exc:
            state, detail = "failure", str(exc)
        else:
            state, detail = "success", ""
        elapsed = time.perf_counter() - started
        # 平台「受理超速」的快速驳回：自动放宽提交节奏（自保）。桌面版把
        # ``success=false`` 的平台拒绝归类为显式失败（可人工重试），与把平台内部
        # 异常归为 uncertain 的服务端不同；因此这里按「响应特征 + 快速返回」判定，
        # 两种分类都接受，只放宽节奏，不改变分类与重发语义。
        if (state in ("uncertain", "failure")
                and elapsed <= _SUBMIT_FAST_REJECT_MAX_S
                and _is_rate_reject_response(response)):
            widened = pacer.penalize()
            if widened is not None:
                _emit(callback, "平台快速驳回（疑似建单受理超速）；"
                                f"提交最小间隔自动放宽到 {widened:g} 秒"
                                "（结果分类与重发语义不变）")
        latencies.append(elapsed)
        _trace(f"POST {task['identifier']}: {elapsed:.2f}s -> {state}"
               f"；min_interval_s={interval_at_send:.3f}")
        return task["identifier"], state, detail

    def classify(identifier: str, state: str, detail: str) -> None:
        if state == "success":
            result.succeeded.add(identifier)
            if len(result.succeeded) % _PROGRESS_EVERY_N == 0:
                _emit(callback, f"本轮收到成功响应 {len(result.succeeded)}/{len(tasks)} 单")
        elif state == "failure":
            result.failures.append((identifier, detail))
            # 立即透出：收尾对账失败时这些明细是唯一的现场证据
            # （2026-10-11 的跨线程崩溃就因为对账失败而整批详情丢失）。
            _emit(callback, f"提交失败：{identifier}：{detail}")
        elif state == "uncertain":
            result.uncertain.append((identifier, detail))
            _emit(callback, f"提交结果未知：{identifier}：{detail}")
        elif state == "auth" and not result.auth_error:
            result.auth_error = detail
        elif state == "balance" and not result.balance_error:
            result.balance_error = detail

    if workers == 1:
        # 内联串行：submit 在调用线程内执行（Playwright 同步 API 的线程约束，
        # 见 docstring）。停止/中止/节流语义与并发路径逐字一致。
        for task in tasks:
            if halted():
                break
            identifier, state, detail = run_one(task)
            classify(identifier, state, detail)
        if stop_event.is_set() and on_stop is not None:
            on_stop()
        result.stopped = bool(result.stopped or stop_event.is_set())
        _emit_round_summary(callback, latencies, round_started, pacer.interval)
        return result

    iterator = iter(tasks)
    in_flight: dict[Any, dict[str, Any]] = {}
    stop_closed = False

    with ThreadPoolExecutor(max_workers=workers) as executor:
        exhausted = False
        while in_flight or not exhausted:
            while not exhausted and not halted() and len(in_flight) < workers:
                try:
                    task = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                in_flight[executor.submit(run_one, task)] = task
            if stop_event.is_set() and not stop_closed:
                stop_closed = True
                if on_stop is not None:
                    on_stop()
            if not in_flight:
                if halted():
                    result.stopped = bool(stop_event.is_set())
                    break
                continue
            done, _ = wait(in_flight, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                task = in_flight.pop(future)
                try:
                    identifier, state, detail = future.result()
                except Exception as exc:  # pragma: no cover - run_one already contains errors
                    identifier, state, detail = task["identifier"], "uncertain", str(exc)
                classify(identifier, state, detail)
        result.stopped = bool(result.stopped or stop_event.is_set())
    _emit_round_summary(callback, latencies, round_started, pacer.interval)
    return result


def query_balance(fetch_json: Callable[[str], dict[str, Any]]) -> tuple[float | None, float | None]:
    """查询账户余额，返回 ``(可用余额, 冻结金额)``。

    登录态失效必须向上抛出 ``_AuthExpired``，不能静默返回未知余额；普通
    接口异常仍按旧策略返回 ``(None, None)`` 以便继续展示日志。
    """
    try:
        payload = fetch_json(_ACCOUNT_PATH)
    except Exception as exc:
        if _is_auth_expired(exc):
            raise _AuthExpired(str(exc)) from exc
        return None, None
    if is_auth_expired_payload(payload):
        raise _AuthExpired(auth_error_message(payload))
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return None, None

    def _num(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return (_num(result.get("totalAmount")), _num(result.get("freezeAmount")))


def _format_balance(total: float | None, frozen: float | None) -> str:
    """余额日志一行：查不到时如实说明，不编造数字。"""
    if total is None:
        return "账户余额查询失败（接口异常），本批将停止提交"
    if frozen is None:
        return f"账户可用余额：{total}"
    return f"账户可用余额：{total}（冻结 {frozen}）"


def _make_api_submitter(client: SssApiClient) -> tuple[
        Callable[[dict[str, Any]], dict[str, Any]], Callable[[], None]]:
    """返回每线程独立 Session 的提交函数，以及幂等的关闭回调。"""
    per_thread = local()
    clients: list[SssApiClient] = []
    lock = Lock()
    closed = False

    def submit(payload: dict[str, Any]) -> dict[str, Any]:
        worker = getattr(per_thread, "client", None)
        if worker is None:
            worker = client.fork()
            per_thread.client = worker
            with lock:
                clients.append(worker)
        return _post_one(worker, payload)

    def close() -> None:
        nonlocal closed
        with lock:
            if closed:
                return
            closed = True
            to_close = list(clients)
        for worker in to_close:
            worker.close()

    return submit, close


def _safe_reconcile(tasks: list[dict[str, Any]],
                    fetch_json: Callable[[str], dict[str, Any]],
                    callback: Callable[[str], Any] | None,
                    label: str,
                    *,
                    created_after: float | None = None,
                    attempts: int = 1,
                    prefilter: dict[str, Any] | None = None,
                    zero_retry_delay: float | None = None) -> _Reconciliation | None:
    """只读对账；列表延迟时有限轮询，绝不因“暂时查不到”而重发 POST。

    ``prefilter`` 非空时先用服务端粗筛对账；只要粗筛没有得出「全部匹配」，
    就再用无过滤的全量扫描复核一次，以全量结论为准。这样即使服务端将来
    忽略或改变时间窗语义，最坏结果只是多扫一遍，绝不会因为预筛把订单
    筛没而误判「缺失」。

    ``zero_retry_delay`` 为秒数时：预筛窗口返回「一条都没有」会先等这么久
    再重查一次，用于覆盖「刚写入、列表还读不到」的情况。只应在**提交后**的
    对账里启用——提交前站内本就没有本批订单，重查纯属浪费。
    """
    if prefilter is None and _SSS_SERVER_PREFILTER:
        prefilter = _build_list_prefilter(tasks)
    if prefilter:
        _trace(f"{label} 服务端预筛窗口 startTime={prefilter.get('startTime')} "
               f"endTime={prefilter.get('endTime')}")
    attempts = max(1, int(attempts))
    last: _Reconciliation | None = None
    last_error = ""
    zero_retry_done = False
    for attempt in range(attempts):
        try:
            reconciliation = _reconcile_tasks(tasks, fetch_json, created_after=created_after,
                                              prefilter=prefilter)
        except Exception as exc:
            last_error = str(exc)
            if attempt + 1 < attempts:
                _emit(callback, f"{label}第 {attempt + 1} 次查询失败：{exc}；"
                                f"{_RECONCILE_POLL_INTERVAL_S:g}s 后只读复查")
                time.sleep(_RECONCILE_POLL_INTERVAL_S)
                continue
            reconciliation = None
        # 预筛窗口一条都没查到、而目标批次非空：很可能是「刚写入还没被列表读到」。
        # 先做一次短暂重查，把这种情况和「窗口真的为空」区分开；这次重查不占用
        # attempts 配额，因此后面仍会正常进入全量兜底。
        if (zero_retry_delay is not None and reconciliation is not None and prefilter
                and not zero_retry_done and reconciliation.matched_count == 0
                and len(reconciliation.missing) == len(tasks)):
            zero_retry_done = True
            _emit(callback, f"{label}：服务端预筛窗口内 0 条，"
                            f"{zero_retry_delay:g}s 后重查一次"
                            f"（可能是刚写入尚未可见）")
            time.sleep(zero_retry_delay)
            try:
                reconciliation = _reconcile_tasks(
                    tasks, fetch_json, created_after=created_after, prefilter=prefilter)
            except Exception as exc:
                last_error = str(exc)
                reconciliation = None
        if reconciliation is not None:
            last_error = ""
            _emit_reconciliation(callback, label, reconciliation, len(tasks))
            _emit_list_stats(callback)
            last = reconciliation
            if not reconciliation.missing:
                return reconciliation
            if attempt + 1 < attempts:
                _emit(callback, f"{label}暂缺 {len(reconciliation.missing)} 单，"
                                f"{_RECONCILE_POLL_INTERVAL_S:g}s 后只读复查（不重发 POST）")
                time.sleep(_RECONCILE_POLL_INTERVAL_S)
                continue
        # 轮询用尽仍有缺失或查询报错：若这次用了服务端预筛，先用无过滤的全量
        # 扫描确认不是预筛窗口/参数把站内订单挡住，再以全量结论为准。
        if not prefilter:
            break
        why = (f"缺少 {len(reconciliation.missing)} 单" if reconciliation is not None
               else f"查询失败（{last_error}）")
        # 只补一次只读自检：判定服务端不再接受时间窗时 fail-closed，
        # 直接改走无过滤全量扫描（绝不把"读不到"当成"站内没有"）。
        if _verify_list_window(fetch_json, callback) == "unsupported":
            _emit(callback, f"{label}：时间窗失效，本次以无过滤全量扫描结论为准")
        _emit(callback, f"{label}：服务端预筛{why}，正在用无过滤全量扫描复核")
        try:
            full = _reconcile_tasks(tasks, fetch_json, created_after=created_after)
        except Exception as exc:
            _emit(callback, f"{label}全量复核失败：{exc}；为避免重复下单，后续不会自动提交")
            return None
        _emit_reconciliation(callback, label, full, len(tasks))
        if reconciliation is not None and len(full.confirmed) != len(reconciliation.confirmed):
            _emit(callback, f"{label}：全量复核确认为 {len(full.confirmed)}/{len(tasks)} 单，"
                            f"以全量结果为准")
        return full
    if last is None:
        _emit(callback, f"{label}失败：{last_error}；为避免重复下单，后续不会自动提交")
    return last


#: 时间窗自检结论的进程内缓存：{"verdict": str, "at": 单调秒}。
_WINDOW_CHECK_CACHE: dict[str, Any] = {"verdict": "", "at": 0.0}
_WINDOW_CHECK_TTL_S = 600.0


def _list_probe(fetch_json: Callable[[str], dict[str, Any]],
                params: dict[str, Any]) -> list[dict[str, Any]]:
    """时间窗自检用的一次性只读查询：固定 pageSize=1，返回首页记录。"""
    query = urlencode({"pageNo": 1, "pageSize": 1, "sortType": 1, "sort": 1, **params})
    payload = fetch_json(f"{_ORDER_LIST_PATH}?{query}")
    if payload.get("success") is False:
        raise LookupError(str(payload.get("message") or "订单列表查询失败"))
    records, _total = _list_records(payload)
    return records


def _verify_list_window(fetch_json: Callable[[str], dict[str, Any]],
                        callback: Callable[[str], Any] | None = None) -> str:
    """只读自检：确认服务端真的接受 ``startTime``/``endTime`` 时间窗过滤。

    为什么需要它：整个对账判定都建立在「带时间窗的查询返回 0 条 = 窗口内确实
    没有订单」之上。服务端对**空结果**与**不接受的查询参数**返回的是同一种
    ``success:true`` 空壳。一旦服务端改了时间窗语义，对账就会把「读不到」误判成
    「站内没有」，提交前据此放行会重复下单。

    做法（最多两次 pageSize=1 的只读请求，结论按进程缓存十分钟）：

    1. 探针 A：不带任何过滤查 1 条（列表按创建时间倒序，即最新的一单；自检不
       依赖排序 —— 无论拿到哪一条，它自己的创建时间必然落在下面那个窗口里）；
    2. 探针 B：用这单的**创建时间 ±1 天**做时间窗再查 1 条 —— 该窗口按构造必然
       包含探针 A 那单；
    3. 探针 B 有记录 → ``ok``；探针 A 也没有记录 → ``ok``（账号本来就没有订单）；
       探针 B 空而探针 A 有记录 → ``unsupported``；读不出创建时间或请求异常 →
       ``inconclusive``。

    只返回结论、不抛异常：``unsupported`` 由调用方 fail-closed（改走无过滤全量
    扫描），``inconclusive`` 绝不新增停机条件（读不到 ≠ 站内没有）。
    """
    cached = str(_WINDOW_CHECK_CACHE.get("verdict") or "")
    cached_at = float(_WINDOW_CHECK_CACHE.get("at") or 0.0)
    if cached and (time.monotonic() - cached_at) < _WINDOW_CHECK_TTL_S:
        return cached

    verdict = "inconclusive"
    reason = "查询异常或读不出创建时间"
    try:
        newest = _list_probe(fetch_json, {})
        if not newest:
            verdict, reason = "ok", "账号暂无订单，空窗口合理"
        else:
            created = _record_created_timestamp(newest[0])
            if created is None:
                reason = "最新订单读不出创建时间"
            else:
                timezone = _dt.datetime.now().astimezone().tzinfo or _dt.timezone.utc
                moment = _dt.datetime.fromtimestamp(created, tz=timezone)
                window = {
                    "startTime": int((moment - _dt.timedelta(days=1)).timestamp() * 1000),
                    "endTime": int((moment + _dt.timedelta(days=1)).timestamp() * 1000),
                }
                if _list_probe(fetch_json, window):
                    verdict, reason = "ok", "带时间窗的查询能查到账号最新订单"
                else:
                    verdict, reason = "unsupported", "账号有订单，但带时间窗的查询返回 0 条"
    except Exception as exc:  # noqa: BLE001 - 自检读不到只能如实说读不到
        _trace(f"时间窗自检查询失败（按 inconclusive 处理）：{exc}")

    _WINDOW_CHECK_CACHE["verdict"] = verdict
    _WINDOW_CHECK_CACHE["at"] = time.monotonic()
    if verdict == "ok":
        _emit(callback, f"时间窗自检：服务端接受 startTime/endTime（{reason}）")
    elif verdict == "unsupported":
        _emit(callback, f"时间窗自检：{reason} —— 服务端可能不再接受 startTime/endTime，"
                        f"将改用无过滤全量扫描")
    else:
        _emit(callback, f"时间窗自检未能完成（{reason}），按既有逻辑继续")
    return verdict


def _emit_list_stats(callback: Callable[[str], Any] | None) -> None:
    """把最近一次列表扫描的耗时/吞吐写进日志（用户据此判断对账是不是变慢了）。"""
    stats = dict(_LAST_LIST_STATS)
    if not stats:
        return
    _emit(callback,
          f"列表扫描：{stats.get('pages', 0)} 页 / {stats.get('elapsed_s', 0)} 秒，"
          f"命中 {stats.get('matched', 0)} 条"
          + (f"，平均每页 {stats.get('per_page_s')} 秒" if stats.get("pages") else "")
          + f"（预筛{'开' if stats.get('prefilter') else '关'}）")


def _merge_reconciliation(preconfirmed: set[str], current: _Reconciliation) -> _Reconciliation:
    return _Reconciliation(
        confirmed=set(preconfirmed) | set(current.confirmed),
        missing=list(current.missing),
        duplicate_count=current.duplicate_count,
        matched_count=len(preconfirmed) + current.matched_count,
    )


def _run_reconciled_submission(
        tasks: list[dict[str, Any]],
        submit_factory: Callable[[], tuple[Callable[[dict[str, Any]], dict[str, Any]], Callable[[], None]]],
        fetch_json: Callable[[str], dict[str, Any]],
        stop_event: Any,
        callback: Callable[[str], Any] | None,
        decision_callback: Callable[[str, str], str] | None,
        max_workers: int,
        relogin: Callable[[], None] | None = None,
        submit_min_interval_s: float = 0.0,
        ) -> tuple[_Reconciliation | None, bool]:
    """执行"先对账、提交、再对账"的至少一次语义。

    返回 ``(最终对账结果, 是否完成了最终对账)``。平台接口没有客户端幂等
    键，因此这里不承诺 exactly-once：只保证非幂等 POST 不自动重试，提交后
    通过列表对账确认；对账延迟时只读轮询，绝不立即重复 POST。

    ``submit_min_interval_s`` > 0 时，每轮提交的相邻 POST 起点至少间隔该值
    （见 `_SubmitPacer`；每轮新建一个节流器，0 = 关闭）。节流只影响发送节奏，
    不改变分类、记录与重发语义。
    """
    batch_started_at = time.time()
    batch_window_start = batch_started_at - _BATCH_CLOCK_SKEW_S

    initial = _safe_reconcile(tasks, fetch_json, callback, "下单前站内对账", attempts=1)
    if initial is None:
        stop_event.set()
        return None, False
    if initial.duplicate_count:
        _emit(callback, "站内记录数超过 Excel 目标，已停止且不会自动取消或补发")
        stop_event.set()
        return initial, True
    if not initial.missing:
        _emit(callback, "Excel 订单均已在站内确认，本次不发送任何下单请求")
        return initial, True

    all_errors: dict[str, str] = {}
    balance_error = ""

    def reconcile(round_tasks: list[dict[str, Any]], label: str, **options: Any
                  ) -> "_Reconciliation | None":
        """只读对账；"查不到"（missing）与"查失败"（返回 None）都如实返回。"""
        return _safe_reconcile(round_tasks, fetch_json, callback, label, **options)

    def submit_round(round_tasks: list[dict[str, Any]], workers: int) -> _SubmitResult:
        submit, close = submit_factory()
        try:
            return _submit_tasks_concurrent(round_tasks, submit, stop_event,
                                            callback, workers, on_stop=close,
                                            min_interval_s=submit_min_interval_s)
        finally:
            close()

    preconfirmed = set(initial.confirmed)
    current = list(initial.missing)
    first = submit_round(current, max_workers)
    all_errors.update(first.failures)
    all_errors.update(first.uncertain)
    balance_error = first.balance_error
    if first.uncertain:
        _emit(callback, f"{len(first.uncertain)} 单请求未获确认，正在查站，不会直接重发")
    if first.balance_error:
        _emit(callback, f"余额不足，已停止后续下单：{first.balance_error}")
        stop_event.set()

    # 401 只能统一重登一次；重登后先对账，再仅提交确认仍缺失的任务。
    if first.auth_error and not stop_event.is_set() and relogin is not None:
        _emit(callback, "登录态过期，正在重新登录并对账…")
        relogin()
        after_login = reconcile(
            current, "重登后站内对账",
            created_after=batch_window_start, attempts=_RECONCILE_POLL_ATTEMPTS)
        if after_login is None:
            stop_event.set()
            return None, False
        if after_login.duplicate_count:
            _emit(callback, "重登后发现站内重复，已停止且不会自动取消或补发")
            stop_event.set()
            return _merge_reconciliation(preconfirmed, after_login), True
        # 重登后对账确认的订单也要计入最终 confirmed，否则 created 数会少算。
        preconfirmed.update(after_login.confirmed)
        current = list(after_login.missing)
        if current:
            second = submit_round(current, max_workers)
            all_errors.update(second.failures)
            all_errors.update(second.uncertain)
            balance_error = balance_error or second.balance_error
            if second.balance_error:
                _emit(callback, f"余额不足，已停止后续下单：{second.balance_error}")
                stop_event.set()
            if second.auth_error:
                _emit(callback, "重新登录后仍遇到 401，停止自动提交并以站内对账为准")
                all_errors.update({task["identifier"]: "重新登录后仍遇到 401"
                                   for task in current})
                stop_event.set()
    elif first.auth_error and not stop_event.is_set():
        _emit(callback, "登录态过期但当前模式无法重登，停止自动提交并以站内对账为准")
        stop_event.set()

    poll_attempts = _RECONCILE_POLL_ATTEMPTS if not (stop_event.is_set() or balance_error) else 1
    final_current = reconcile(
        current, "收尾站内对账",
        created_after=batch_window_start, attempts=poll_attempts,
        zero_retry_delay=_PREFILTER_ZERO_RETRY_DELAY_S)
    if final_current is None:
        stop_event.set()
        return None, False
    final = _merge_reconciliation(preconfirmed, final_current)
    if final.duplicate_count:
        _emit(callback, "收尾对账发现站内重复，已停止且不会自动取消")
        stop_event.set()
        return final, True
    if stop_event.is_set() or balance_error:
        return final, True

    # 对账后仍缺失的显式失败项才允许用户手动触发一次串行重试。
    for task in list(final.missing):
        identifier = task["identifier"]
        error = all_errors.get(identifier, "站内未查询到对应订单")
        _emit(callback, f"订单未确认：{identifier}：{error}")
        if decision_callback is None:
            continue
        decision = decision_callback(identifier, error).lower()
        if decision == "stop":
            stop_event.set()
            break
        if decision != "retry":
            continue
        before_retry = reconcile(
            current, "重试前站内对账",
            created_after=batch_window_start, attempts=_RECONCILE_POLL_ATTEMPTS,
            zero_retry_delay=_PREFILTER_ZERO_RETRY_DELAY_S)
        if before_retry is None:
            stop_event.set()
            return None, False
        if before_retry.duplicate_count:
            _emit(callback, "重试前发现站内重复，已停止且不会自动取消")
            stop_event.set()
            return _merge_reconciliation(preconfirmed, before_retry), True
        if identifier not in {item["identifier"] for item in before_retry.missing}:
            _emit(callback, f"{identifier} 已在站内确认，跳过重试")
            final = _merge_reconciliation(preconfirmed, before_retry)
            continue
        _emit(callback, f"重试 {identifier}（已完成重试前对账）")
        retry = submit_round([task], 1)
        if retry.balance_error:
            _emit(callback, f"余额不足，已停止后续下单：{retry.balance_error}")
            stop_event.set()
        if retry.auth_error:
            _emit(callback, "重试遇到 401，不再自动重登")
            stop_event.set()
        final_current = reconcile(
            current, "重试后站内对账",
            created_after=batch_window_start,
            attempts=1 if stop_event.is_set() else _RECONCILE_POLL_ATTEMPTS,
            zero_retry_delay=_PREFILTER_ZERO_RETRY_DELAY_S)
        if final_current is None:
            stop_event.set()
            return None, False
        if final_current.duplicate_count:
            _emit(callback, "重试后发现站内重复，已停止且不会自动取消")
            stop_event.set()
            return _merge_reconciliation(preconfirmed, final_current), True
        final = _merge_reconciliation(preconfirmed, final_current)
        if stop_event.is_set():
            break
    return final, True


def _final_status_fields(*, processed: int, created: int, stopped: bool,
                         partial: bool, reconciled: bool,
                         idempotency_field: str = "") -> dict[str, Any]:
    """收尾状态：把"订单结果"与"是否完成站内对账"分开表达。

    ``reconciled`` 为假（收尾对账失败）时结果按未知处理：订单是否创建无法从
    平台得到确定回答，绝不能显示成一次干净的失败。
    """
    return {
        "processed": processed,
        "created": created,
        "stopped": stopped,
        "partial": partial,
        "reconciled": reconciled,
        "uncertain": bool(not reconciled),
        "semantics": ("idempotency-key+reconciliation"
                      if idempotency_field else "at-least-once+reconciliation"),
    }


def _emit_sss_completion(callback: Callable[[str], Any] | None, *, processed: int,
                         created: int, stopped: bool, partial: bool,
                         reconciled: bool, job_start: float,
                         idempotency_field: str) -> dict[str, Any]:
    """收尾播报与结果字段（纯接口/浏览器两条分支共用，避免话术漂移）。"""
    if stopped:
        _emit(callback,
              f"闪时送任务已停止：{'已完成站内对账' if reconciled else '站内对账失败'}，"
              f"确认 {created}/{processed}")
    elif partial:
        _emit(callback,
              f"闪时送任务部分完成：确认 {created}/{processed}，"
              f"{processed - created} 单未完成")
    else:
        _emit(callback,
              f"闪时送下单完成：确认 {created}/{processed}，"
              f"总耗时 {(time.perf_counter() - job_start):.1f} 秒")
    return _final_status_fields(
        processed=processed, created=created, stopped=stopped, partial=partial,
        reconciled=reconciled, idempotency_field=idempotency_field)


def _exclusive_sss_job(func: Callable[..., Any]) -> Callable[..., Any]:
    """同一进程内拒绝两个闪时送下单任务并发提交。"""
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not _SSS_RUN_LOCK.acquire(blocking=False):
            raise RuntimeError("已有闪时送下单任务正在运行，拒绝并发执行")
        try:
            return func(*args, **kwargs)
        finally:
            _SSS_RUN_LOCK.release()
    return wrapper


@_exclusive_sss_job
def run_sss_job(config: Any, stop_event: Any,
                progress_callback: Callable[[str], Any] | None = None,
                password: str | None = None,
                decision_callback: Callable[[str, str], str] | None = None,
                locators: dict[str, Any] | None = None,
                captcha_callback: Callable[[bytes], str] | None = None,
                store_cache_callback: Callable[[str, int], None] | None = None) -> dict[str, Any]:
    """按配置的名单来源读取当天订单，并通过接口批量创建预约单。

    名单来源（``config.sss_order_source``）：

    - ``wps``（默认）：下单前从 WPS 云端的「东湖中餐 / 东湖晚餐」读取当天
      （运行时刻 20:00 之后识别次日，其余时刻识别运行日；见
      ``sss_import.ordering_target_date``）标 ``1`` 的人，地址是「大西 / 小」的不下单；
      名单直接用内存数据下单，同时写一份到《闪时送.xlsx》留档。云端读不到、
      数据不完整或「识别日期 ≠ 本次送达日期」时**一律拒绝下单**（不登录、不提交）。
    - ``excel``：旧行为，读《闪时送.xlsx》（人工准备名单时的兜底）。

    ``decision_callback(identifier, error)`` 返回 ``retry``/``skip``/``stop``，
    用于单个订单创建失败时的交互决策（与订单处理的 order_decision 一致）。
    ``config.sss_dry_run`` 为真时只组装并打印报文，不真实提交（且跳过
    登录与门店/地址查询，无需验证码）。
    ``captcha_callback`` 在纯接口模式接收验证码 PNG 字节，返回用户输入的验证码。

    提交路径分两种（``config.api_mode``）：纯接口模式按 ``sss_max_workers``
    并发 POST 建单接口；浏览器表单模式（默认关闭）启动 Playwright，登录后按
    原 ``闪时送.py`` 脚本的「创建订单」弹窗流程逐单操作，固定串行。两条路径
    都会做提交前后的站内订单列表对账；状态不确定时绝不直接重复提交。
    """
    load_start = time.perf_counter()
    now = _dt.datetime.now()
    source = str(getattr(config, "sss_order_source", "wps") or "wps").strip().lower()
    if source != "excel":
        source = "wps"
    configured_excel = getattr(config, "sss_excel_path", None)
    excel_path = Path(configured_excel) if configured_excel else None
    import_summary: dict[str, Any] | None = None

    if source == "wps":
        # 云端当天名单：读云端 → 地址过滤 → 校验 → 留档；任何问题都在这里
        # 抛 ImportRefused，此时还没有登录、没有提交任何订单。
        day = prepare_day_orders(config, now=now,
                                 delivery_date=expected_delivery_date(now),
                                 log=progress_callback)
        orders_by_sheet = day.orders_by_sheet
        import_summary = day.as_summary()
    else:
        if excel_path is None:
            raise FileNotFoundError("尚未选择闪时送 Excel 文件")
        if not excel_path.is_file():
            raise FileNotFoundError(f"Excel 文件不存在: {excel_path}")
        if excel_path.suffix.lower() not in {".xlsx", ".xlsm"}:
            raise ValueError("仅支持 .xlsx 和 .xlsm Excel 文件")
        orders_by_sheet = load_sss_orders(excel_path)

    def _result(payload: dict[str, Any]) -> dict[str, Any]:
        """给结果补上名单来源与云端导入摘要（界面/日志用）。"""
        payload["source"] = source
        if import_summary is not None:
            payload["import"] = import_summary
        return payload

    # ---- 平台身份与网址校验（在任何登录/POST 之前） ----
    account = str(getattr(config, "sss_account", "") or "")
    _validate_sss_orders(orders_by_sheet)
    total = sum(len(orders) for orders in orders_by_sheet.values())
    if total == 0:
        if source == "wps":
            _emit(progress_callback,
                  "当天云端名单为空（没有当天日期列，或标 1 的人都是「大西/小」），本次不下单")
        else:
            _emit(progress_callback, "闪时送 Excel 中没有任何订单")
        return _result({"processed": 0, "created": 0, "status": "no_orders"})
    _emit(progress_callback,
          f"读取订单表耗时 {(time.perf_counter() - load_start):.1f} 秒，共 {total} 单")

    # ---- 网址闸门：在**任何登录/POST 之前**严格校验配置的闪时送网址 ----
    timeout_ms = int(getattr(config, "element_timeout_ms", 8000))
    try:
        read_timeout_s = float(getattr(config, "sss_read_timeout_s", 20.0))
    except (TypeError, ValueError):
        read_timeout_s = 20.0
    read_timeout_s = max(1.0, min(120.0, read_timeout_s))
    url = str(getattr(config, "sss_url", "") or "").strip() or DEFAULT_SSS_URL
    try:
        canonical_sss_origin(url)
    except SssUrlConfigError as exc:
        _emit(progress_callback, "错误：" + str(exc))
        return _result({"status": "url_config_error", "processed": 0, "created": 0,
                        "submitted": 0, "previewed": 0, "stopped": True,
                        "partial": False, "reconciled": False, "uncertain": False,
                        "reason": str(exc)})
    if password is None:
        password = ""
    dry_run = bool(getattr(config, "sss_dry_run", False))
    store_name = str(getattr(config, "sss_store_name", "") or "一口轻食")
    common_address = str(getattr(config, "sss_common_address", "") or "")
    use_fixed_address = bool(getattr(config, "sss_use_fixed_address", False))
    goods_name = str(getattr(config, "sss_product_name", "") or "轻食")
    api_mode = bool(getattr(config, "api_mode", True))
    max_workers = resolve_create_workers(config)
    submit_min_interval_s = resolve_submit_min_interval_s(config)
    batch_id = uuid.uuid4().hex[:12]
    idempotency_field = str(getattr(config, "sss_idempotency_field", "") or _CLIENT_IDEMPOTENCY_FIELD).strip()

    if dry_run:
        store_id = _cached_store_id(config, store_name) or 0
        if store_id:
            _emit(progress_callback, f"门店「{store_name}」命中缓存（id={store_id}）")
        if use_fixed_address:
            address = _fixed_address_from_config(config)
        else:
            _emit(progress_callback, "干跑模式：跳过常用地址查询，用占位地址组装报文")
            address = {"lnt": 0.0, "lat": 0.0, "areaCode": "",
                       "addressDetail": common_address or "干跑占位"}
        tasks = _collect_tasks(
            orders_by_sheet, store_id, address, goods_name,
            account=str(getattr(config, "sss_account", "") or ""),
            batch_id=batch_id, idempotency_field=idempotency_field, now=now)
        return _result(_run_dry_run(tasks, progress_callback))

    if locators is None:
        locators = load_sss_locators()
    account = str(getattr(config, "sss_account", "") or "")
    if not account:
        raise ValueError("尚未填写闪时送账号")

    if api_mode:
        job_start = time.perf_counter()
        _emit(progress_callback, "正在获取闪时送验证码…")
        client = SssApiClient(url, account, password or "", timeout=(5.0, read_timeout_s),
                              pool_size=max_workers)
        try:
            captcha = client.fetch_captcha()
            if captcha_callback is None:
                raise RuntimeError("纯接口模式需要验证码输入回调，当前界面未提供 captcha 弹窗")
            code = captcha_callback(captcha)
            _emit(progress_callback, "正在登录闪时送…")
            client.login(code)

            def relogin() -> None:
                _emit(progress_callback, "登录态过期，重新获取验证码并登录…")
                img = client.fetch_captcha()
                if captcha_callback is None:
                    raise RuntimeError("纯接口模式需要验证码输入回调")
                client.login(captcha_callback(img))
                _emit(progress_callback, "重新登录成功")

            def prepare_session_data() -> tuple[int, dict[str, Any], tuple[float | None, float | None]]:
                _emit(progress_callback, "登录成功，读取门店与常用地址…")
                prep_start = time.perf_counter()
                # 接口模式也串行：requests.Session 不保证线程安全，门店/地址两个
                # GET 并发共用同一 Session 会出现偶发失败。
                store_id, address = _prepare_store_and_address(
                    client.get_json, config, store_name, common_address,
                    use_fixed_address, progress_callback, parallel=False,
                    store_cache_callback=store_cache_callback)
                _emit(progress_callback,
                      f"门店与地址准备耗时 {(time.perf_counter() - prep_start):.1f} 秒")
                balance = query_balance(client.get_json)
                return store_id, address, balance

            store_id, address, (balance_total, balance_frozen) = _with_auth_relogin(
                prepare_session_data, relogin, progress_callback, "读取门店/地址/余额")
            _emit(progress_callback, _format_balance(balance_total, balance_frozen))

            tasks = _collect_tasks(
                orders_by_sheet, store_id, address, goods_name,
                account=account, batch_id=batch_id, idempotency_field=idempotency_field,
                now=now)
            if idempotency_field:
                _emit(progress_callback, f"已启用客户端幂等字段「{idempotency_field}」")
            else:
                _emit(progress_callback, "平台接口未探测到客户端幂等字段：采用“至少一次提交 + 对账确认”语义，不承诺 exactly-once")
            try:
                unit_price = float(getattr(config, "sss_unit_price", 0) or 0)
            except (TypeError, ValueError):
                unit_price = 0
            if unit_price > 0:
                estimate = round(unit_price * len(tasks), 2)
                _emit(progress_callback,
                      f"本批 {len(tasks)} 单预计送完结算约 {estimate} 元"
                      + (f"，当前可用 {balance_total}"
                         if balance_total is not None else "，当前余额未知"))

            balance_status, estimate = _balance_precheck(
                tasks, balance_total, unit_price, progress_callback)
            if balance_status != "ok":
                return _result({
                    "status": balance_status,
                    "processed": len(tasks), "created": 0, "submitted": 0,
                    "previewed": 0, "stopped": True, "partial": False,
                    "reconciled": False, "uncertain": balance_status == "balance_unknown",
                    "estimate": estimate,
                    "semantics": "pre-submit-balance-guard",
                })

            if bool(getattr(config, "sss_preflight", False)):
                preflight_status, preflight = _preflight_tasks(
                    tasks, client.get_json, progress_callback)
                return _result({
                    "status": preflight_status,
                    "processed": len(tasks),
                    "created": len(preflight.confirmed) if preflight else 0,
                    "submitted": 0, "previewed": 0, "stopped": preflight_status != "preflight_ok",
                    "partial": False,
                    "reconciled": preflight is not None,
                    "uncertain": preflight is None,
                    "balance_total": balance_total,
                    "estimate": estimate,
                    "semantics": "preflight-only",
                })

            _emit(progress_callback,
                  f"开始下单：共 {len(tasks)} 单，创建订单 {max_workers} 路"
                  f"{'串行' if max_workers == 1 else '并发'}提交，"
                  f"读取超时 {read_timeout_s:g}s"
                  + (f"，提交最小间隔 {submit_min_interval_s:g}s"
                     if submit_min_interval_s > 0 else ""))
            submit_start = time.perf_counter()
            final, reconciled = _run_reconciled_submission(
                tasks,
                lambda: _make_api_submitter(client),
                client.get_json,
                stop_event,
                progress_callback,
                decision_callback,
                max_workers,
                relogin=relogin,
                submit_min_interval_s=submit_min_interval_s,
            )
            created = len(final.confirmed) if final is not None else 0
            processed = len(tasks)
            _emit(progress_callback,
                  f"下单与对账耗时 {(time.perf_counter() - submit_start):.1f} 秒，"
                  f"确认 {created}/{processed}")
            try:
                end_total, end_frozen = query_balance(client.get_json)
            except _AuthExpired:
                _emit(progress_callback, "余额查询时登录态失效，正在重新登录…")
                relogin()
                try:
                    end_total, end_frozen = query_balance(client.get_json)
                except _AuthExpired as exc:
                    _emit(progress_callback, f"注意：重新登录后余额查询仍失败：{exc}")
                    end_total, end_frozen = None, None
            _emit(progress_callback, "结束" + _format_balance(end_total, end_frozen))
            stopped = bool(stop_event.is_set())
            partial = bool(reconciled and final is not None and final.missing and not stopped)
            result: dict[str, Any] = _emit_sss_completion(
                progress_callback, processed=processed, created=created,
                stopped=stopped, partial=partial, reconciled=reconciled,
                job_start=job_start, idempotency_field=idempotency_field)
            if balance_total is not None:
                result["balance_total"] = balance_total
            if end_total is not None:
                result["balance_end"] = end_total
            return _result(result)
        finally:
            client.close()

    # 浏览器表单模式的前置配置检查：弹窗里选的就是「常用地址」，报文指纹必须
    # 与之指向同一处；没有配置常用地址标签就无法保证一致性，直接拒绝（未登录）。
    if not common_address:
        _emit(progress_callback,
              "错误：浏览器表单模式需要配置常用地址标签（界面上的「常用地址」，"
              "默认「嗯哼」），表单按它选择收货地址")
        return _result({
            "status": "config_error", "processed": total, "created": 0,
            "submitted": 0, "previewed": 0, "stopped": True, "partial": False,
            "reconciled": False, "uncertain": False,
            "reason": "browser_ui_mode_requires_common_address",
            "semantics": "pre-submit-config-guard",
        })

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright，请先安装 requirements.txt") from exc

    with sync_playwright() as playwright:
        browser = _launch_browser(
            playwright,
            bool(getattr(config, "headless", False)),
        )
        page = browser.new_page()
        try:
            job_start = time.perf_counter()
            _emit(progress_callback, "正在打开闪时送…")
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            _ensure_logged_in(page, account, password, stop_event, progress_callback)
            _minimize_window(browser, progress_callback)

            def relogin_browser() -> None:
                _emit(progress_callback, "登录态过期，重新登录…")
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                _ensure_logged_in(page, account, password, stop_event,
                                  progress_callback)
                _minimize_window(browser, progress_callback)

            def fetch_json(path: str) -> dict[str, Any]:
                _, payload = _sss_api(page, "GET", path)
                if is_auth_expired_payload(payload):
                    raise _AuthExpired(auth_error_message(payload))
                return payload

            def prepare_browser_session_data() -> tuple[int, dict[str, Any]]:
                # 表单流程在弹窗里选择的就是「常用地址」，报文指纹必须与之一致；
                # 固定地址坐标只有纯接口模式消费（表单里无处填写坐标）。
                if use_fixed_address:
                    _emit(progress_callback,
                          f"浏览器表单模式：报文地址改用常用地址「{common_address}」"
                          "（固定地址配置仅在纯接口模式生效）")
                _emit(progress_callback, "读取门店与常用地址…")
                prep_start = time.perf_counter()
                store_id, address = _prepare_store_and_address(
                    fetch_json, config, store_name, common_address,
                    False,  # 表单选的是常用地址，指纹必须与之一致
                    progress_callback, parallel=False,
                    store_cache_callback=store_cache_callback)
                _emit(progress_callback,
                      f"门店与地址准备耗时 {(time.perf_counter() - prep_start):.1f} 秒")
                return store_id, address

            def prepare_browser_session_data_with_balance() -> tuple[
                    int, dict[str, Any], tuple[float | None, float | None]]:
                store_id, address = prepare_browser_session_data()
                return store_id, address, query_balance(fetch_json)

            store_id, address, (balance_total, balance_frozen) = _with_auth_relogin(
                prepare_browser_session_data_with_balance, relogin_browser,
                progress_callback, "读取门店/地址")

            tasks = _collect_tasks(
                orders_by_sheet, store_id, address, goods_name,
                account=account, batch_id=batch_id, idempotency_field=idempotency_field,
                now=now)
            if idempotency_field:
                _emit(progress_callback, f"已启用客户端幂等字段「{idempotency_field}」")
            else:
                _emit(progress_callback, "平台接口未探测到客户端幂等字段：采用“至少一次提交 + 对账确认”语义，不承诺 exactly-once")
            try:
                unit_price = float(getattr(config, "sss_unit_price", 0) or 0)
            except (TypeError, ValueError):
                unit_price = 0
            balance_status, estimate = _balance_precheck(
                tasks, balance_total, unit_price, progress_callback)
            if balance_status != "ok":
                return _result({
                    "status": balance_status,
                    "processed": len(tasks), "created": 0, "submitted": 0,
                    "previewed": 0, "stopped": True, "partial": False,
                    "reconciled": False, "uncertain": balance_status == "balance_unknown",
                    "estimate": estimate,
                    "semantics": "pre-submit-balance-guard",
                })
            if bool(getattr(config, "sss_preflight", False)):
                preflight_status, preflight = _preflight_tasks(
                    tasks, fetch_json, progress_callback)
                return _result({
                    "status": preflight_status,
                    "processed": len(tasks),
                    "created": len(preflight.confirmed) if preflight else 0,
                    "submitted": 0, "previewed": 0, "stopped": preflight_status != "preflight_ok",
                    "partial": False,
                    "reconciled": preflight is not None,
                    "uncertain": preflight is None,
                    "balance_total": balance_total,
                    "estimate": estimate,
                    "semantics": "preflight-only",
                })
            _emit(progress_callback,
                  f"开始下单：共 {len(tasks)} 单，浏览器表单模式（创建订单弹窗）串行提交"
                  + (f"，提交最小间隔 {submit_min_interval_s:g}s"
                     if submit_min_interval_s > 0 else ""))
            submit_start = time.perf_counter()

            def submit_browser(payload: dict[str, Any]) -> dict[str, Any]:
                """浏览器表单模式的单笔下单（原 ``闪时送.py`` 弹窗流程）。

                异常归类契约与接口分支完全一致（见 ``_submit_tasks_concurrent``）：

                * 最终确定点击**之前**的任何失败（按钮/输入框定位不到、送达时间
                  确定失败等）→ 普通异常按 ``failure`` 处理 —— 此刻确定没有创建
                  订单，可安全人工重试；
                * 最终确定点击后无法确认弹窗收场 → ``_SubmissionUncertain``，
                  结果未知，由站内只读对账定论，绝不自动重发；
                * 「创建订单」按钮消失 → ``_AuthExpired``，触发统一重登。

                成功返回 ``{"success": True}``；订单最终以站内对账确认为准。
                """
                return _ui_create_order(
                    page, payload, locators,
                    store_name=store_name, address_label=common_address,
                    callback=progress_callback)

            final, reconciled = _run_reconciled_submission(
                tasks,
                lambda: (submit_browser, lambda: None),
                fetch_json,
                stop_event,
                progress_callback,
                decision_callback,
                # 浏览器分支只有单个 page，Playwright 同步 API 非线程安全，
                # 因此这里固定串行；并发只作用于纯接口分支。
                max_workers=1,
                relogin=relogin_browser,
                submit_min_interval_s=submit_min_interval_s,
            )
            created = len(final.confirmed) if final is not None else 0
            processed = len(tasks)
            stopped = bool(stop_event.is_set())
            partial = bool(reconciled and final is not None and final.missing and not stopped)
            _emit(progress_callback,
                  f"下单与对账耗时 {(time.perf_counter() - submit_start):.1f} 秒，"
                  f"确认 {created}/{processed}")
        finally:
            browser.close()

    return _result(_emit_sss_completion(
        progress_callback, processed=processed, created=created, stopped=stopped,
        partial=partial, reconciled=reconciled, job_start=job_start,
        idempotency_field=idempotency_field))


__all__ = ["run_sss_job", "load_sss_orders", "compute_delivery_time",
           "expected_delivery_date", "build_order_payload", "SSS_LOCATORS",
           "ImportRefused", "BrowserNotFoundError", "LocatorError"]
