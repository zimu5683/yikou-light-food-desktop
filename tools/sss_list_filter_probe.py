#!/usr/bin/env python3
"""闪时送订单列表「查询参数能力」只读侦察（诊断用，绝不写数据）。

背景：对账用 ``GET /consumer/order/one-touch-send/list`` 拉全量历史
（实测站内 2222 单 → 每页 100 条 → 23 页 → 每页约 2.0 秒 ≈ 46 秒/次）。
如果服务端支持按时间/状态过滤，对账成本可以降到个位数秒。本脚本只做
只读 GET，逐组参数比较 ``total`` 与首页记录数，找出真正生效的参数。

用法（仓库根目录）：
    .venv/bin/python tools/sss_list_filter_probe.py --captcha-file-mode
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import sss as S  # noqa: E402
from app.api_client import SssApiClient  # noqa: E402

CONFIG_PATH = Path.home() / ".config" / "yikou-light-food" / "config.json"
POST_PATH = S._CREATE_ORDER_PATH


def load_config() -> dict:
    if CONFIG_PATH.is_file():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def captcha_from_file(png: bytes, image_path: str, code_path: str,
                      wait_s: float = 300.0) -> str:
    image_file = Path(image_path)
    code_file = Path(code_path)
    image_file.write_bytes(png)
    code_file.unlink(missing_ok=True)
    print(f"[probe] 验证码已写入 {image_file}，等待 {code_file} …", flush=True)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if code_file.is_file():
            code = code_file.read_text(encoding="utf-8").strip()
            code_file.unlink(missing_ok=True)
            if code:
                return code
        time.sleep(0.2)
    raise SystemExit(f"等待验证码超时（{wait_s:g}s）")


def get(client: SssApiClient, query: dict) -> tuple[dict, float, int]:
    path = f"{S._ORDER_LIST_PATH}?{urlencode(query)}"
    started = time.perf_counter()
    resp = client.session.get(client.origin + path,
                              headers={"token": client.token}, timeout=client.timeout)
    elapsed = time.perf_counter() - started
    try:
        payload = resp.json()
    except ValueError:
        payload = {"_non_json": resp.text[:200]}
    return payload, elapsed, len(resp.content)


def describe(payload: dict) -> tuple[str, int | None, int, list[str]]:
    """返回 (结论, total, 记录数, 备注)。"""
    if payload.get("success") is False:
        return (f"接口 failure：{payload.get('message')!r}", None, 0, [])
    try:
        records, total = S._list_records(payload)
    except Exception as exc:
        return (f"结构异常：{exc}；顶层键={list(payload)[:8]}", None, 0, [])
    days = {}
    for record in records:
        day = S._normalise_delivery_time(
            S._pick(record, ("expectedDeliveryTime", "expected_delivery_time",
                             "appointmentTime", "appointment_time")))[:10]
        days[day] = days.get(day, 0) + 1
    top = [f"{k}:{v}" for k, v in sorted(days.items())[:3]]
    return ("ok", total, len(records), top)


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # pragma: no cover
        pass
    parser = argparse.ArgumentParser(description="闪时送列表查询参数只读侦察")
    parser.add_argument("--captcha-file-mode", action="store_true")
    parser.add_argument("--captcha-image", default="/tmp/sss_captcha_live.png")
    parser.add_argument("--captcha-code-file", default="/tmp/sss_code.txt")
    parser.add_argument("--captcha-wait", type=float, default=300.0)
    parser.add_argument("--post-check", action="store_true",
                        help="额外测一次「故意非法」的 create-order POST，用于量出服务端处理耗时")
    parser.add_argument("--out", default="/tmp/sss_probe/filter_report.txt")
    args = parser.parse_args()

    cfg = load_config()
    url = str(cfg.get("sss_url") or S.DEFAULT_SSS_URL)
    account = str(cfg.get("sss_account") or "")
    timeout_s = float(cfg.get("sss_read_timeout_s") or 20.0)

    lines: list[str] = []
    log = lines.append
    log(f"# 闪时送列表查询参数只读侦察  {time.strftime('%Y-%m-%d %H:%M:%S')}")

    from app.credentials import get_sss_password
    password = get_sss_password(account) or os.environ.get("YIKOU_SSS_PASSWORD", "")
    if not password:
        import getpass
        password = getpass.getpass(f"请输入 {account} 的闪时送登录密码：")

    client = SssApiClient(url, account, password, timeout=(5.0, timeout_s), pool_size=4)
    png = client.fetch_captcha()
    code = (captcha_from_file(png, args.captcha_image, args.captcha_code_file, args.captcha_wait)
            if args.captcha_file_mode else input("请输入验证码：").strip())
    client.login(code)
    log("登录成功\n")

    today = time.strftime("%Y-%m-%d")
    today_start = f"{today} 00:00:00"
    today_end = f"{today} 23:59:59"
    now_s = int(time.time())
    day_start_ms = int(time.mktime(time.strptime(today, "%Y-%m-%d"))) * 1000
    day_end_ms = day_start_ms + 86_400_000 - 1

    base = {"pageNo": 1, "pageSize": 100, "sortType": 1, "sort": 1}
    cases: list[tuple[str, dict]] = [
        ("基线（当前业务用法）", {}),
        ("startTime/endTime =今天(字符串)", {"startTime": today_start, "endTime": today_end}),
        ("startTime/endTime =今天(epoch秒)", {"startTime": now_s - 86400, "endTime": now_s}),
        ("startTime/endTime =今天(epoch毫秒)", {"startTime": day_start_ms, "endTime": day_end_ms}),
        ("expectedDeliveryTime 起止(字符串)", {"expectedDeliveryStartTime": today_start,
                                       "expectedDeliveryEndTime": today_end}),
        ("expectedDeliveryTime 起止(毫秒)", {"expectedDeliveryStartTime": day_start_ms,
                                      "expectedDeliveryEndTime": day_end_ms}),
        ("deliveryTime 起止(毫秒)", {"deliveryStartTime": day_start_ms,
                                "deliveryEndTime": day_end_ms}),
        ("orderTime 起止(毫秒)", {"orderStartTime": day_start_ms, "orderEndTime": day_end_ms}),
        ("createTime 起止(毫秒)", {"createStartTime": day_start_ms, "createEndTime": day_end_ms}),
        ("appointmentTime 起止(毫秒)", {"appointmentStartTime": day_start_ms,
                                  "appointmentEndTime": day_end_ms}),
        ("status=2", {"status": 2}),
        ("statusList=2,3", {"statusList": "2,3"}),
        ("status=2&statusList=2,3", {"status": 2, "statusList": "2,3"}),
        ("customerOrderStatus=2", {"customerOrderStatus": 2}),
        ("storeId=211053", {"storeId": 211053}),
        ("goodsName=轻食", {"goodsName": "轻食"}),
        ("pageSize=1000", {"pageSize": 1000}),
        ("searchType/queryDate 今天", {"queryDate": today, "searchType": 1}),
    ]

    results = []
    for label, extra in cases:
        query = dict(base)
        query.update(extra)
        payload, elapsed, size = get(client, query)
        verdict, total, count, days = describe(payload)
        results.append((label, verdict, total, count, elapsed, size, days))
        log(f"- {label:34s} {elapsed:5.2f}s {size // 1024:4d}KB  total={total} 首页={count}  {verdict}"
            + (f"  日期({','.join(days)})" if days else ""))
        time.sleep(0.2)

    log("")
    baseline = next((r for r in results if r[0].startswith("基线")), None)
    if baseline and baseline[3]:
        for label, verdict, total, count, elapsed, size, _days in results[1:]:
            if total is not None and baseline[2] is not None and total < baseline[2]:
                log(f"★ 生效参数：{label}  total {baseline[2]} → {total}（首页 {count} 条，{elapsed:.2f}s）")
        log(f"（基线 total={baseline[2]}，首页 {baseline[3]} 条，{baseline[4]:.2f}s）")

    if args.post_check:
        log("")
        log("## create-order POST 服务端耗时（故意非法报文：不会创建订单）")
        probe_body = {"clientRequestId": "timing-probe-invalid",
                      "phone": "", "noSuchField": "probe"}
        started = time.perf_counter()
        try:
            payload = client.post_json(POST_PATH, probe_body)
            elapsed = time.perf_counter() - started
            log(f"POST 耗时 {elapsed:.3f}s 响应={json.dumps(payload, ensure_ascii=False)[:300]}")
        except Exception as exc:
            elapsed = time.perf_counter() - started
            log(f"POST 耗时 {elapsed:.3f}s 异常={type(exc).__name__}: {exc}")

    client.close()
    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"\n（报告已写入 {args.out}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
