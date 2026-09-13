#!/usr/bin/env python3
"""只读诊断：为什么预筛窗口漏掉了刚下的批次（最新订单的时间戳对照）。

触发场景（2026-09-11 22:53 真实批次）：批次 75 单、目标送达日 09-12，
生产预筛窗口 09-11 00:00 ~ 09-13 23:59（epoch 毫秒）却返回 0 条，兜底全量
扫描才发现 75 单。

本脚本取「最新若干页」订单，逐条打印 orderTime / expectedDeliveryTime /
appointmentTime 的原始值，再逐一测试候选时间窗，直接判定服务端
``startTime``/``endTime`` 到底过滤的是哪个字段、按什么时区，以及正确窗口
应该怎么取。只做只读 GET。
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
UTC = dt.timezone.utc


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


def show_ms(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{value!r}"
    if number > 10_000_000_000:
        number /= 1000.0
    return dt.datetime.fromtimestamp(number, CST).strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # pragma: no cover
        pass
    parser = argparse.ArgumentParser(description="预筛窗口漏单原因只读诊断")
    parser.add_argument("--captcha-file-mode", action="store_true")
    parser.add_argument("--captcha-image", default="/tmp/sss_captcha_live.png")
    parser.add_argument("--captcha-code-file", default="/tmp/sss_code.txt")
    parser.add_argument("--captcha-wait", type=float, default=300.0)
    parser.add_argument("--pages", type=int, default=2, help="取最新多少页订单")
    parser.add_argument("--out", default="/tmp/sss_probe/newest_report.txt")
    args = parser.parse_args()

    cfg = load_config()
    url = str(cfg.get("sss_url") or S.DEFAULT_SSS_URL)
    account = str(cfg.get("sss_account") or "")
    excel = str(cfg.get("sss_excel_path") or "")
    timeout_s = float(cfg.get("sss_read_timeout_s") or 20.0)

    lines: list[str] = []
    log = lines.append
    log(f"# 预筛窗口漏单原因只读诊断  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"本机时区={time.strftime('%Z%z')}")

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

    # 1) 最新订单的时间戳原始值
    log(f"## 1. 最新 {args.pages} 页订单的时间戳对照（按列表顺序，最新在前）")
    newest: list[dict] = []
    for page in range(1, max(1, args.pages) + 1):
        payload = client.get_json(f"{S._ORDER_LIST_PATH}?" + urlencode(
            {"pageNo": page, "pageSize": 100, "sortType": 1, "sort": 1}))
        records, total = S._list_records(payload)
        if page == 1:
            log(f"- 列表 total={total}")
        if not records:
            break
        newest.extend(records)
    for record in newest[:12]:
        log(f"- id={S._pick(record, ('id',))} status={S._pick(record, ('status',))} "
            f"orderTime={show_ms(S._pick(record, ('orderTime',)))} "
            f"expectedDeliveryTime={show_ms(S._pick(record, ('expectedDeliveryTime',)))} "
            f"appointmentTime={show_ms(S._pick(record, ('appointmentTime',)))} "
            f"sendTime={show_ms(S._pick(record, ('sendTime',)))} "
            f"name={S._pick(record, ('recipientName',))}")

    # 2) 本地 Excel 这一批的目标日 + 生产预筛窗口
    log("")
    log("## 2. 本批目标日与生产预筛窗口")
    tasks = S._collect_tasks(
        S.load_sss_orders(excel), int(cfg.get("sss_store_id") or 0),
        {"lnt": cfg.get("sss_fixed_lnt"), "lat": cfg.get("sss_fixed_lat"),
         "areaCode": cfg.get("sss_fixed_area_code"),
         "addressDetail": cfg.get("sss_fixed_address_detail")},
        str(cfg.get("sss_product_name") or "轻食"), account=account,
        batch_id="probe", idempotency_field="")
    days = sorted({t["fingerprint"].expected_delivery_time[:10] for t in tasks})
    prefilter = S._build_list_prefilter(tasks)
    start = dt.datetime.fromtimestamp(prefilter["startTime"] / 1000, CST)
    end = dt.datetime.fromtimestamp(prefilter["endTime"] / 1000, CST)
    log(f"- 本批 {len(tasks)} 单，目标送达日={days}")
    log(f"- 生产预筛：{prefilter}")
    log(f"- 即 {start} ~ {end}（CST）")

    # 3) 候选窗口逐一测试
    log("")
    log("## 3. 候选时间窗实测（同一会话）")
    today = dt.datetime.now(CST).date()

    def window(first: dt.date, last: dt.date, tz) -> tuple[int, int]:
        return (int(dt.datetime.combine(first, dt.time(0, 0, 0), tzinfo=tz).timestamp() * 1000),
                int(dt.datetime.combine(last, dt.time(23, 59, 59), tzinfo=tz).timestamp() * 1000))

    candidates: list[tuple[str, dt.date, dt.date, object]] = [
        ("生产预筛（目标日±1，CST）", dt.date.fromisoformat(days[0]) - dt.timedelta(days=1),
         dt.date.fromisoformat(days[-1]) + dt.timedelta(days=1), CST),
        ("目标日±1，按 UTC 解释", dt.date.fromisoformat(days[0]) - dt.timedelta(days=1),
         dt.date.fromisoformat(days[-1]) + dt.timedelta(days=1), UTC),
        ("仅目标日，CST", dt.date.fromisoformat(days[0]), dt.date.fromisoformat(days[-1]), CST),
        ("今天~目标日+1，CST", today,
         dt.date.fromisoformat(days[-1]) + dt.timedelta(days=1), CST),
        ("今天往前 3 天 ~ 今天+3 天，CST", today - dt.timedelta(days=3),
         today + dt.timedelta(days=3), CST),
        ("90 天宽窗，CST", today - dt.timedelta(days=90), today + dt.timedelta(days=90), CST),
    ]
    for label, first, last, tz in candidates:
        start_ms, end_ms = window(first, last, tz)
        query = {"pageNo": 1, "pageSize": 100, "sortType": 1, "sort": 1,
                 "startTime": start_ms, "endTime": end_ms}
        started = time.perf_counter()
        payload = client.get_json(f"{S._ORDER_LIST_PATH}?{urlencode(query)}")
        elapsed = time.perf_counter() - started
        try:
            records, total = S._list_records(payload)
        except Exception as exc:
            log(f"- {label:28s} {first} ~ {last} {elapsed:5.2f}s ★{str(exc)[:40]}")
            log(f"    原始报文：{json.dumps(payload, ensure_ascii=False)[:260]}")
            continue
        created = {}
        delivered = {}
        for record in records:
            created[show_ms(S._pick(record, ("orderTime",)))[:10]] = \
                created.get(show_ms(S._pick(record, ("orderTime",)))[:10], 0) + 1
            delivered[show_ms(S._pick(record, ("expectedDeliveryTime",)))[:10]] = \
                delivered.get(show_ms(S._pick(record, ("expectedDeliveryTime",)))[:10], 0) + 1
        log(f"- {label:28s} {first} ~ {last} {elapsed:5.2f}s total={total} 首页={len(records)}")
        log(f"    创建日分布={dict(sorted(created.items())[-4:])} 送达日分布={dict(sorted(delivered.items())[-4:])}")

    # 4) 结论
    log("")
    log("## 4. 判定")
    if newest:
        newest_created = show_ms(S._pick(newest[0], ("orderTime",)))
        newest_delivery = show_ms(S._pick(newest[0], ("expectedDeliveryTime",)))
        log(f"- 最新一单：创建 {newest_created}，预约送达 {newest_delivery}")
        log(f"- 生产预筛窗口：{start} ~ {end}")
        inside = start <= dt.datetime.strptime(newest_created, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=CST) <= end
        log(f"- 最新一单是否落在生产预筛窗口内（按 orderTime 判断）：{inside}")
        if not inside:
            log("- → 服务端 startTime/endTime 过滤的很可能不是 orderTime，"
                "或时区/字段语义与假设不符，需要按下面的实测分布重新取窗口")

    client.close()
    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"\n（报告已写入 {args.out}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
