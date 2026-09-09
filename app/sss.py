"""闪时送（sss）平台批量下单自动化（接口模式）。

本模块与管理后台订单处理（:mod:`app.automation`）方向相反：从独立的
《闪时送.xlsx》读取订单（午餐/晚餐两表），再在闪时送平台逐单创建预约单。

默认使用纯接口模式：直接请求闪时送 HTTP 接口完成登录与下单；用户只需在应用内
输入图形验证码，不再弹出浏览器。备用浏览器模式仍会启动 Playwright，自动填写
账号密码后由用户在浏览器中输入图形验证码。

干跑模式：配置 ``sss_dry_run = true`` 时只组装并打印下单报文，不真实提交。
凭据与路径由 GUI 通过 :class:`AppConfig` 传入，不写入源码。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
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
                             auth_error_message, is_auth_expired_payload)
    from .automation import (
        BrowserNotFoundError,
        LocatorError,
        _emit,
        _launch_browser,
    )
    from .locators import SSS_LOCATORS, load_sss_locators
except ImportError:  # pragma: no cover - allows ``python app/sss.py``
    from api_client import (ApiError, SssApiClient, SssTransportError,
                            auth_error_message, is_auth_expired_payload)
    from automation import (
        BrowserNotFoundError,
        LocatorError,
        _emit,
        _launch_browser,
    )
    from locators import SSS_LOCATORS, load_sss_locators

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
# 订单创建时间与本地批次起点的最大时钟偏差；用于排除其他设备更早创建的相似订单。
_BATCH_CLOCK_SKEW_S = 120.0
# 平台抓包报文没有客户端幂等字段。留空时明确采用“至少一次提交 + 对账确认”；
# 若平台后续支持，可在配置/调用方指定字段名后启用稳定 UUID（见 build_order_payload）。
_CLIENT_IDEMPOTENCY_FIELD = ""

# 同一时刻只允许一个闪时送下单任务，避免两个 worker 并发提交相似订单。
_SSS_RUN_LOCK = Lock()

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
                   idempotency_field: str = "") -> list[dict[str, Any]]:
    """把午餐/晚餐两表展平成待下单任务；送达时间每表只算一次。

    每条任务带完整 ``OrderFingerprint``（账号/门店/商品/地址/坐标/类型等）
    以及稳定 ``client_request_id``，避免仅凭姓名+电话+门牌+时间误判。
    """
    now = _dt.datetime.now()
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
                 callback: Callable[[str], Any] | None) -> dict[str, int]:
    """干跑：只组装打印报文，不真实提交；只预览前几单全文。"""
    _emit(callback, f"【干跑模式】只组装报文，不真实提交，共 {len(tasks)} 单")
    for index, task in enumerate(tasks):
        if index < _DRY_RUN_PREVIEW_N:
            _emit(callback,
                  f"【干跑】{task['identifier']} 报文："
                  f"{json.dumps(task['payload'], ensure_ascii=False)}")
    if len(tasks) > _DRY_RUN_PREVIEW_N:
        _emit(callback, f"【干跑】…其余 {len(tasks) - _DRY_RUN_PREVIEW_N} 单报文已省略")
    _emit(callback, f"闪时送下单完成（干跑）：成功 {len(tasks)}/{len(tasks)}")
    return {"processed": len(tasks), "created": len(tasks)}


def _check_success(resp: dict[str, Any]) -> None:
    """下单响应成功则静默返回，否则抛错（message 优先）。"""
    if resp.get("success"):
        return
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
    """单次下单 POST；任何非确认响应均改为待对账状态。"""
    try:
        return client.post_json(_CREATE_ORDER_PATH, payload)
    except ApiError as exc:
        if _is_auth_expired(exc):
            raise _AuthExpired(str(exc)) from exc
        raise _SubmissionUncertain(str(exc)) from exc


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
    """从站内列表记录提取完整指纹；缺失字段留空，由对账阶段 fail-closed。"""
    address = _record_address(record)
    user = record.get("user") if isinstance(record.get("user"), dict) else {}
    store = record.get("store") if isinstance(record.get("store"), dict) else {}
    goods_name, goods_num = _record_goods(record)
    return OrderFingerprint(
        receive_name=_normalise_text(_pick(record, (
            "receiveName", "receiverName", "contactName", "contacts", "name")) or
            _pick(address, ("contact", "contactName", "name"))),
        receive_phone=_normalise_text(_pick(record, (
            "receivePhone", "receiverPhone", "contactPhone", "phone", "mobile")) or
            _pick(address, ("mobile", "phone", "tel")), compact=True),
        door_num=_normalise_text(_pick(address, (
            "doorNum", "houseNum", "door", "description")) or
            _pick(record, ("doorNum", "houseNum")), compact=True),
        expected_delivery_time=_normalise_delivery_time(_pick(record, (
            "expectedDeliveryTime", "appointmentTime", "deliveryTime", "expected_time"))),
        account=_normalise_text(_pick(record, (
            "account", "sssAccount", "customerMobile", "loginMobile")) or
            _pick(user, ("mobile", "phone", "account")) or account, compact=True),
        store_id=_normalise_int_text(_pick(record, ("storeId", "storeID", "store_id")) or
                                     _pick(store, ("id", "storeId"))),
        goods_name=goods_name,
        goods_num=goods_num,
        address_detail=_normalise_text(_pick(address, (
            "addressDetail", "address_detail", "address", "position", "description")) or
            _pick(record, ("addressDetail", "address_detail", "position", "address"))),
        area_code=_record_region_code(address) or _record_region_code(record),
        lnt=_normalise_number(_pick(address, ("lnt", "lng", "longitude", "lon")) or
                              _pick(record, ("lnt", "lng", "longitude", "lon"))),
        lat=_normalise_number(_pick(address, ("lat", "latitude")) or
                              _pick(record, ("lat", "latitude"))),
        order_type=_normalise_order_type(_pick(record, ("orderType", "order_type")) or
                                         _pick(record, ("orderTypeFormat",))),
    )


def _record_fingerprint_or_raise(record: dict[str, Any], *, account: str = "") -> OrderFingerprint:
    """只接受能确定收货核心字段的站内记录，避免未知结构被误判为缺失。

    列表接口本身按当前登录账号隔离；若记录没有暴露账号字段，用本次任务的
    账号兜底，避免把“字段缺失”误判成“不同账号订单”。
    """
    fingerprint = _order_record_fingerprint(record, account=account)
    if all((fingerprint.receive_name, fingerprint.receive_phone,
            fingerprint.door_num, fingerprint.expected_delivery_time)):
        return fingerprint
    raise LookupError("订单列表记录缺少姓名、电话、门牌或预约送达时间，无法安全对账："
                      + json.dumps(record, ensure_ascii=False)[:300])


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
    raise LookupError("订单列表响应缺少 records/list")


def _delivery_day_bounds(delivery_time: str) -> tuple[int, int]:
    try:
        day = _dt.date.fromisoformat(delivery_time[:10])
    except ValueError as exc:
        raise LookupError(f"预约送达时间格式异常：{delivery_time}") from exc
    china_tz = _dt.timezone(_dt.timedelta(hours=8))
    start = _dt.datetime.combine(day, _dt.time.min, tzinfo=china_tz)
    end = start + _dt.timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _list_pending_orders(fetch_json: Callable[[str], dict[str, Any]],
                         tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按预约日期分页查询所有活跃状态订单，不发任何写请求。"""
    # 同一天的午餐/晚餐共用一次列表查询，避免把同一页记录重复计入对账。
    dates = sorted({_task_fingerprint(task).expected_delivery_time[:10] for task in tasks})
    records: list[dict[str, Any]] = []
    for delivery_time in dates:
        start_time, end_time = _delivery_day_bounds(delivery_time)
        page_no = 1
        page_size = 100
        while page_no <= 100:
            query = urlencode({
                "pageNo": page_no,
                "pageSize": page_size,
                "sortType": 1,
                "sort": 1,
                "startTime": start_time,
                "endTime": end_time,
                "statusList": [0, 1, 2],
            }, doseq=True)
            payload = fetch_json(f"{_ORDER_LIST_PATH}?{query}")
            if payload.get("success") is False:
                raise LookupError(str(payload.get("message") or "订单列表查询失败"))
            page_records, total = _list_records(payload)
            records.extend(page_records)
            if not page_records or len(page_records) < page_size:
                break
            if total is not None and len(page_records) + (page_no - 1) * page_size >= total:
                break
            page_no += 1
        else:
            raise LookupError("订单列表分页超过 100 页，拒绝继续下单")
    return records


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
                     created_before: float | None = None) -> _Reconciliation:
    """按完整 ``OrderFingerprint`` 多重集合对账。

    ``created_after``/``created_before`` 用于收尾对账时只接受本批次时间窗口
    内创建的订单；站内记录缺少创建时间时无法证明归属，按“不排除”处理并由
    完整指纹兜底，避免因列表 schema 缺失字段导致整批误判缺失。
    """
    expected = Counter(_task_fingerprint(task) for task in tasks)
    waiting: dict[OrderFingerprint, list[dict[str, Any]]] = {}
    for task in tasks:
        waiting.setdefault(_task_fingerprint(task), []).append(task)
    expected_accounts = {fingerprint.account for fingerprint in expected if fingerprint.account}
    fallback_account = next(iter(expected_accounts)) if len(expected_accounts) == 1 else ""

    actual = Counter()
    for record in _list_pending_orders(fetch_json, tasks):
        created_at = _record_created_timestamp(record)
        if created_at is not None:
            if created_after is not None and created_at < created_after:
                continue
            if created_before is not None and created_at > created_before:
                continue
        fingerprint = _record_fingerprint_or_raise(record, account=fallback_account)
        if fingerprint in expected:
            actual[fingerprint] += 1

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


def _submit_tasks_concurrent(tasks: list[dict[str, Any]],
                             submit: Callable[[dict[str, Any]], dict[str, Any]],
                             stop_event: Any,
                             callback: Callable[[str], Any] | None,
                             max_workers: int = 4,
                             on_stop: Callable[[], None] | None = None) -> _SubmitResult:
    """有界并发提交，停止、401、余额不足后不再派发新任务。

    同一轮最多保留 ``max_workers`` 个在途 POST。请求超时或断链只记录为
    ``uncertain``，由调用方查询订单列表确认，绝不在此处自动重发。
    """
    result = _SubmitResult()
    workers = max(1, int(max_workers))
    iterator = iter(tasks)
    in_flight: dict[Any, dict[str, Any]] = {}
    stop_closed = False

    def halted() -> bool:
        return bool(stop_event.is_set() or result.auth_error or result.balance_error)

    def run_one(task: dict[str, Any]) -> tuple[str, str, str]:
        if stop_event.is_set():
            return task["identifier"], "stopped", ""
        try:
            _check_success(submit(task["payload"]))
        except _AuthExpired as exc:
            return task["identifier"], "auth", str(exc)
        except _BalanceDepleted as exc:
            return task["identifier"], "balance", str(exc)
        except (_SubmissionUncertain, SssTransportError) as exc:
            return task["identifier"], "uncertain", str(exc)
        except Exception as exc:
            return task["identifier"], "failure", str(exc)
        return task["identifier"], "success", ""

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
                if state == "success":
                    result.succeeded.add(identifier)
                    if len(result.succeeded) % _PROGRESS_EVERY_N == 0:
                        _emit(callback, f"本轮收到成功响应 {len(result.succeeded)}/{len(tasks)} 单")
                elif state == "failure":
                    result.failures.append((identifier, detail))
                elif state == "uncertain":
                    result.uncertain.append((identifier, detail))
                elif state == "auth" and not result.auth_error:
                    result.auth_error = detail
                elif state == "balance" and not result.balance_error:
                    result.balance_error = detail
        result.stopped = bool(result.stopped or stop_event.is_set())
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
        return "账户余额查询失败（接口异常），继续下单"
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
                    attempts: int = 1) -> _Reconciliation | None:
    """只读对账；列表延迟时有限轮询，绝不因“暂时查不到”而重发 POST。"""
    attempts = max(1, int(attempts))
    last: _Reconciliation | None = None
    for attempt in range(attempts):
        try:
            reconciliation = _reconcile_tasks(tasks, fetch_json, created_after=created_after)
        except Exception as exc:
            if attempt + 1 < attempts:
                _emit(callback, f"{label}第 {attempt + 1} 次查询失败：{exc}；"
                                f"{_RECONCILE_POLL_INTERVAL_S:g}s 后只读复查")
                time.sleep(_RECONCILE_POLL_INTERVAL_S)
                continue
            _emit(callback, f"{label}失败：{exc}；为避免重复下单，后续不会自动提交")
            return None
        _emit_reconciliation(callback, label, reconciliation, len(tasks))
        last = reconciliation
        if not reconciliation.missing:
            return reconciliation
        if attempt + 1 < attempts:
            _emit(callback, f"{label}暂缺 {len(reconciliation.missing)} 单，"
                            f"{_RECONCILE_POLL_INTERVAL_S:g}s 后只读复查（不重发 POST）")
            time.sleep(_RECONCILE_POLL_INTERVAL_S)
    return last


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
        ) -> tuple[_Reconciliation | None, bool]:
    """执行“先对账、提交、再对账”的至少一次语义。

    返回 ``(最终对账结果, 是否完成了最终对账)``。平台接口没有客户端幂等
    键，因此这里不承诺 exactly-once：只保证非幂等 POST 不自动重试，提交后
    通过列表对账确认；对账延迟时只读轮询，绝不立即重复 POST。
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

    def submit_round(round_tasks: list[dict[str, Any]], workers: int) -> _SubmitResult:
        submit, close = submit_factory()
        try:
            return _submit_tasks_concurrent(round_tasks, submit, stop_event,
                                            callback, workers, on_stop=close)
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
        after_login = _safe_reconcile(
            current, fetch_json, callback, "重登后站内对账",
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
    final_current = _safe_reconcile(
        current, fetch_json, callback, "收尾站内对账",
        created_after=batch_window_start, attempts=poll_attempts)
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
        before_retry = _safe_reconcile(
            current, fetch_json, callback, "重试前站内对账",
            created_after=batch_window_start, attempts=_RECONCILE_POLL_ATTEMPTS)
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
        final_current = _safe_reconcile(
            current, fetch_json, callback, "重试后站内对账",
            created_after=batch_window_start,
            attempts=1 if stop_event.is_set() else _RECONCILE_POLL_ATTEMPTS)
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
    """读取《闪时送.xlsx》并通过接口批量创建预约单。

    ``decision_callback(identifier, error)`` 返回 ``retry``/``skip``/``stop``，
    用于单个订单创建失败时的交互决策（与订单处理的 order_decision 一致）。
    ``config.sss_dry_run`` 为真时只组装并打印报文，不真实提交（且跳过
    登录与门店/地址查询，无需验证码）。
    ``captcha_callback`` 在纯接口模式接收验证码 PNG 字节，返回用户输入的验证码。
    下单阶段并发提交（``config.sss_max_workers``，默认 4），提交前后均会
    查询站内订单列表；状态不确定时绝不直接重复 POST。
    """
    load_start = time.perf_counter()
    configured_excel = getattr(config, "sss_excel_path", None)
    if not configured_excel:
        raise FileNotFoundError("尚未选择闪时送 Excel 文件")
    excel_path = Path(configured_excel)
    if not excel_path.is_file():
        raise FileNotFoundError(f"Excel 文件不存在: {excel_path}")
    if excel_path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError("仅支持 .xlsx 和 .xlsm Excel 文件")
    if password is None:
        password = ""
    dry_run = bool(getattr(config, "sss_dry_run", False))
    store_name = str(getattr(config, "sss_store_name", "") or "一口轻食")
    common_address = str(getattr(config, "sss_common_address", "") or "")
    use_fixed_address = bool(getattr(config, "sss_use_fixed_address", False))
    goods_name = str(getattr(config, "sss_product_name", "") or "轻食")
    api_mode = bool(getattr(config, "api_mode", True))
    try:
        max_workers = int(getattr(config, "sss_max_workers", 4))
    except (TypeError, ValueError):
        max_workers = 4
    max_workers = max(1, min(20, max_workers))
    batch_id = uuid.uuid4().hex[:12]
    idempotency_field = str(getattr(config, "sss_idempotency_field", "") or _CLIENT_IDEMPOTENCY_FIELD).strip()

    orders_by_sheet = load_sss_orders(excel_path)
    _validate_sss_orders(orders_by_sheet)
    total = sum(len(orders) for orders in orders_by_sheet.values())
    if total == 0:
        _emit(progress_callback, "闪时送 Excel 中没有任何订单")
        return {"processed": 0, "created": 0}
    _emit(progress_callback,
          f"读取订单表耗时 {(time.perf_counter() - load_start):.1f} 秒，共 {total} 单")

    # 干跑短路：组装报文即返回，不取验证码、不登录、不查门店地址。
    timeout_ms = int(getattr(config, "element_timeout_ms", 8000))
    try:
        read_timeout_s = float(getattr(config, "sss_read_timeout_s", 20.0))
    except (TypeError, ValueError):
        read_timeout_s = 20.0
    read_timeout_s = max(1.0, min(120.0, read_timeout_s))
    url = str(getattr(config, "sss_url", "") or "").strip() or DEFAULT_SSS_URL
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
            batch_id=batch_id, idempotency_field=idempotency_field)
        return _run_dry_run(tasks, progress_callback)

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
                account=account, batch_id=batch_id, idempotency_field=idempotency_field)
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
                      + (f"，当前可用 {balance_total}，结算时请留意充值"
                         if balance_total is not None and balance_total < estimate else ""))

            _emit(progress_callback,
                  f"开始下单：共 {len(tasks)} 单，并发 {max_workers} 路，读取超时 {read_timeout_s:g}s")
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
                    _emit(progress_callback, f"重新登录后余额查询仍失败：{exc}", "WARN")
                    end_total, end_frozen = None, None
            _emit(progress_callback, "结束" + _format_balance(end_total, end_frozen))
            stopped = bool(stop_event.is_set())
            partial = bool(reconciled and final is not None and final.missing and not stopped)
            if stopped:
                _emit(progress_callback,
                      f"闪时送任务已停止：{'已完成站内对账' if reconciled else '站内对账失败'}，"
                      f"确认 {created}/{processed}")
            elif partial:
                _emit(progress_callback,
                      f"闪时送任务部分完成：确认 {created}/{processed}，"
                      f"{processed - created} 单未完成")
            else:
                _emit(progress_callback,
                      f"闪时送下单完成：确认 {created}/{processed}，"
                      f"总耗时 {(time.perf_counter() - job_start):.1f} 秒")
            result: dict[str, Any] = {
                "processed": processed,
                "created": created,
                "stopped": stopped,
                "partial": partial,
                "reconciled": reconciled,
                "uncertain": not reconciled,
                "semantics": ("idempotency-key+reconciliation"
                              if idempotency_field else "at-least-once+reconciliation"),
            }
            if balance_total is not None:
                result["balance_total"] = balance_total
            if end_total is not None:
                result["balance_end"] = end_total
            return result
        finally:
            client.close()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright，请先安装 requirements.txt") from exc

    with sync_playwright() as playwright:
        browser = _launch_browser(
            playwright,
            getattr(config, "browser_mode", "auto"),
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
                _emit(progress_callback, "读取门店与常用地址…")
                prep_start = time.perf_counter()
                store_id, address = _prepare_store_and_address(
                    fetch_json, config, store_name, common_address,
                    use_fixed_address, progress_callback, parallel=False,
                    store_cache_callback=store_cache_callback)
                _emit(progress_callback,
                      f"门店与地址准备耗时 {(time.perf_counter() - prep_start):.1f} 秒")
                return store_id, address

            store_id, address = _with_auth_relogin(
                prepare_browser_session_data, relogin_browser,
                progress_callback, "读取门店/地址")

            tasks = _collect_tasks(
                orders_by_sheet, store_id, address, goods_name,
                account=account, batch_id=batch_id, idempotency_field=idempotency_field)
            if idempotency_field:
                _emit(progress_callback, f"已启用客户端幂等字段「{idempotency_field}」")
            else:
                _emit(progress_callback, "平台接口未探测到客户端幂等字段：采用“至少一次提交 + 对账确认”语义，不承诺 exactly-once")
            _emit(progress_callback,
                  f"开始下单：共 {len(tasks)} 单，浏览器模式固定串行")
            submit_start = time.perf_counter()

            def submit_browser(payload: dict[str, Any]) -> dict[str, Any]:
                http, resp = _submit_order(page, payload)
                if http == 401 or is_auth_expired_payload(resp):
                    raise _AuthExpired(auth_error_message(resp, fallback="浏览器会话登录态失效"))
                return resp

            final, reconciled = _run_reconciled_submission(
                tasks,
                lambda: (submit_browser, lambda: None),
                fetch_json,
                stop_event,
                progress_callback,
                decision_callback,
                max_workers=1,
                relogin=relogin_browser,
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

    if stopped:
        _emit(progress_callback,
              f"闪时送任务已停止：{'已完成站内对账' if reconciled else '站内对账失败'}，"
              f"确认 {created}/{processed}")
    elif partial:
        _emit(progress_callback,
              f"闪时送任务部分完成：确认 {created}/{processed}，"
              f"{processed - created} 单未完成")
    else:
        _emit(progress_callback,
              f"闪时送下单完成：确认 {created}/{processed}，"
              f"总耗时 {(time.perf_counter() - job_start):.1f} 秒")
    return {"processed": processed, "created": created,
            "stopped": stopped, "partial": partial, "reconciled": reconciled,
            "uncertain": not reconciled,
            "semantics": ("idempotency-key+reconciliation"
                          if idempotency_field else "at-least-once+reconciliation")}


def _submit_order(page: Any, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    http, resp = _sss_api(page, "POST", _CREATE_ORDER_PATH, payload)
    return http, resp


__all__ = ["run_sss_job", "load_sss_orders", "compute_delivery_time",
           "build_order_payload", "SSS_LOCATORS",
           "BrowserNotFoundError", "LocatorError"]
