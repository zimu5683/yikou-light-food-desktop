#!/usr/bin/env python3
"""只读诊断：订单列表时间窗在什么条件下会返回「空壳」（success:true 但无 records）。

背景：2026-09-11 22:13 的真实批次里，生产预筛窗口
（09-11 00:00 ~ 09-13 23:59:59，CST）被服务端判为非法，`_list_records` 抛
「响应缺少 records/list」，兜底逻辑随即改用无过滤全量扫描（结论正确，但慢）。

而 2026-09-11 08:31 用 09-10 ~ 09-12 的窗口实测却是正常的（2 页 134 条）。
本脚本用**同一时刻、同一会话**逐项对照不同时间窗，找出真正的触发条件，
并把失败响应的原始报文打印出来（这是 `_list_records` 报错时看不到的信息）。

只做只读 GET。
"""
from __future__ import annotations

import argparse
import datetime as dt
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
CST = dt.timezone(dt.timedelta(hours=8))


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
    raise SystemExit("等待验证码超时")


def day_ms(day: dt.date, hour: int, minute: int, second: int) -> int:
    return int(dt.datetime.combine(day, dt.time(hour, minute, second),
                                   tzinfo=CST).timestamp() * 1000)


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # pragma: no cover
        pass
    parser = argparse.ArgumentParser(description="时间窗空壳条件只读诊断")
    parser.add_argument("--captcha-file-mode", action="store_true")
    parser.add_argument("--captcha-image", default="/tmp/sss_captcha_live.png")
    parser.add_argument("--captcha-code-file", default="/tmp/sss_code.txt")
    parser.add_argument("--captcha-wait", type=float, default=300.0)
    parser.add_argument("--out", default="/tmp/sss_probe/window_report.txt")
    args = parser.parse_args()

    cfg = load_config()
    url = str(cfg.get("sss_url") or S.DEFAULT_SSS_URL)
    account = str(cfg.get("sss_account") or "")
    excel = str(cfg.get("sss_excel_path") or "")
    timeout_s = float(cfg.get("sss_read_timeout_s") or 20.0)

    lines: list[str] = []
    log = lines.append
    log(f"# 时间窗空壳条件只读诊断  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"本机时区={time.strftime('%Z%z')}，CST 当日={dt.datetime.now(CST).date()}")

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

    today = dt.datetime.now(CST).date()

    def call(start: int, end: int, *, page_size: int = 100, page_no: int = 1) -> dict:
        query = {"pageNo": page_no, "pageSize": page_size, "sortType": 1, "sort": 1,
                 "startTime": start, "endTime": end}
        path = f"{S._ORDER_LIST_PATH}?{urlencode(query)}"
        started = time.perf_counter()
        payload = client.get_json(path)
        elapsed = time.perf_counter() - started
        try:
            records, total = S._list_records(payload)
            verdict = f"ok total={total} 首页={len(records)}"
        except Exception as exc:
            verdict = f"★空壳/异常：{str(exc)[:60]}"
        log(f"- [{start} ~ {end}] {elapsed:5.2f}s  {verdict}")
        if "★" in verdict:
            log(f"    原始报文：{json.dumps(payload, ensure_ascii=False)[:400]}")
        return payload

    log("## A. 对照：昨天能用的窗口 vs 本次失败的窗口")
    cases: list[tuple[str, dt.date, dt.date]] = [
        ("本次真实失败窗口", today, today + dt.timedelta(days=2)),
        ("上次成功窗口(同日)", today - dt.timedelta(days=1), today + dt.timedelta(days=1)),
        ("目标日往前 1 天", today - dt.timedelta(days=1), today),
        ("目标日当天", today, today),
        ("目标日往后 1 天", today, today + dt.timedelta(days=1)),
        ("目标日往后 2 天", today, today + dt.timedelta(days=2)),
        ("目标日往后 3 天", today, today + dt.timedelta(days=3)),
        ("目标日往后 7 天", today, today + dt.timedelta(days=7)),
        ("目标日往后 14 天", today, today + dt.timedelta(days=14)),
        ("今天往前推 30 天的单日", today - dt.timedelta(days=30), today - dt.timedelta(days=30)),
    ]
    for label, first, last in cases:
        log(f"\n### {label}：{first} ~ {last}")
        call(day_ms(first, 0, 0, 0), day_ms(last, 23, 59, 59))

    log("")
    log("## B. 其他可能触发空壳的变形（对照用）")
    log("\n### 窗口相同但 endTime 取末毫秒 / 少 1 毫秒")
    call(day_ms(today - dt.timedelta(days=1), 0, 0, 0),
         day_ms(today + dt.timedelta(days=1), 23, 59, 59) - 1)
    log("\n### 只带 startTime 不带 endTime")
    query = {"pageNo": 1, "pageSize": 100, "sortType": 1, "sort": 1,
             "startTime": day_ms(today - dt.timedelta(days=1), 0, 0, 0)}
    payload = client.get_json(f"{S._ORDER_LIST_PATH}?{urlencode(query)}")
    log(f"    原始报文：{json.dumps(payload, ensure_ascii=False)[:300]}")
    log("\n### 只带 endTime 不带 startTime")
    query = {"pageNo": 1, "pageSize": 100, "sortType": 1, "sort": 1,
             "endTime": day_ms(today + dt.timedelta(days=1), 23, 59, 59)}
    payload = client.get_json(f"{S._ORDER_LIST_PATH}?{urlencode(query)}")
    log(f"    原始报文：{json.dumps(payload, ensure_ascii=False)[:300]}")
    log("\n### 带 status=2（历史上同样出现过空壳）")
    query = {"pageNo": 1, "pageSize": 100, "sortType": 1, "sort": 1, "status": 2,
             "startTime": day_ms(today - dt.timedelta(days=1), 0, 0, 0),
             "endTime": day_ms(today + dt.timedelta(days=1), 23, 59, 59)}
    payload = client.get_json(f"{S._ORDER_LIST_PATH}?{urlencode(query)}")
    try:
        records, total = S._list_records(payload)
        log(f"    ok total={total} 首页={len(records)}")
    except Exception as exc:
        log(f"    ★空壳/异常：{str(exc)[:60]}")
        log(f"    原始报文：{json.dumps(payload, ensure_ascii=False)[:400]}")

    log("")
    log("## C. 生产函数在同一时刻的取值（对照）")
    try:
        orders = S.load_sss_orders(excel)
        tasks = S._collect_tasks(
            orders, int(cfg.get("sss_store_id") or 0),
            {"lnt": cfg.get("sss_fixed_lnt"), "lat": cfg.get("sss_fixed_lat"),
             "areaCode": cfg.get("sss_fixed_area_code"),
             "addressDetail": cfg.get("sss_fixed_address_detail")},
            str(cfg.get("sss_product_name") or "轻食"), account=account,
            batch_id="probe", idempotency_field="")
        prefilter = S._build_list_prefilter(tasks)
        log(f"- 生产预筛参数：{prefilter}")
        start = dt.datetime.fromtimestamp(prefilter["startTime"] / 1000, CST)
        end = dt.datetime.fromtimestamp(prefilter["endTime"] / 1000, CST)
        log(f"- 对应窗口：{start} ~ {end}")
        call(prefilter["startTime"], prefilter["endTime"])
    except Exception as exc:
        log(f"- 生产预筛构造失败：{type(exc).__name__}: {exc}")

    log("")
    log("## D. 稳定性压测：同一预筛查询连打 N 次，看空壳是否偶发")
    try:
        orders = S.load_sss_orders(excel)
        tasks = S._collect_tasks(
            orders, int(cfg.get("sss_store_id") or 0),
            {"lnt": cfg.get("sss_fixed_lnt"), "lat": cfg.get("sss_fixed_lat"),
             "areaCode": cfg.get("sss_fixed_area_code"),
             "addressDetail": cfg.get("sss_fixed_address_detail")},
            str(cfg.get("sss_product_name") or "轻食"), account=account,
            batch_id="probe", idempotency_field="")
        prefilter = S._build_list_prefilter(tasks)
    except Exception as exc:
        log(f"- 生产预筛构造失败：{type(exc).__name__}: {exc}")
        prefilter = None

    if prefilter:
        rounds = 25
        failures = 0
        seconds: list[float] = []
        for index in range(rounds):
            query = {"pageNo": 1, "pageSize": 100, "sortType": 1, "sort": 1, **prefilter}
            path = f"{S._ORDER_LIST_PATH}?{urlencode(query)}"
            # 用原始 resp 抓取，才能在校验失败时看到「到底收到了什么」：
            # 是结构差异、还是响应体被截断（JSON 解析失败）。
            started = time.perf_counter()
            resp = client.session.get(client.origin + path,
                                      headers={"token": client.token}, timeout=client.timeout)
            elapsed = time.perf_counter() - started
            raw = resp.content
            seconds.append(elapsed)
            parse_error = ""
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                payload = None
                parse_error = f"{type(exc).__name__}: {exc}"
            if payload is None:
                failures += 1
                log(f"- #{index + 1} {elapsed:5.2f}s ★JSON 解析失败 bytes={len(raw)} "
                    f"enc={resp.headers.get('Content-Encoding')} {parse_error}")
                log(f"    前 200 字节：{raw[:200]!r}")
                continue
            try:
                records, total = S._list_records(payload)
                log(f"- #{index + 1} {elapsed:5.2f}s ok total={total} 首页={len(records)} "
                    f"bytes={len(raw)}")
            except Exception as exc:
                failures += 1
                log(f"- #{index + 1} {elapsed:5.2f}s ★空壳 bytes={len(raw)} "
                    f"enc={resp.headers.get('Content-Encoding')} err={str(exc)[:50]}")
                log(f"    原始报文：{json.dumps(payload, ensure_ascii=False)[:400]}")
        if seconds:
            log(f"- 共 {rounds} 次：失败 {failures} 次（{failures / rounds * 100:.0f}%）；"
                f"耗时 min {min(seconds):.2f}s / 中位 {sorted(seconds)[len(seconds) // 2]:.2f}s / "
                f"max {max(seconds):.2f}s")

    client.close()
    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"\n（报告已写入 {args.out}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
