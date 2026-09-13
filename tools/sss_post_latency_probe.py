#!/usr/bin/env python3
"""验证「真实报文形状」的 create-order POST 耗时，以及列表过滤的时区语义。

为什么需要它：上一轮用「故意非法」报文测 POST，服务端 0.26 秒就返回校验
失败——那不能代表真实下单（真实报文要走完门店/地址/第三方校验）。本脚本
用真实报文形状、但把 ``storeId`` 改成不存在的门店号，使请求走完解析后必然
在校验阶段失败。**不会创建任何订单**：门店不存在时服务端无法落单。

同时验证 ``startTime/endTime``（epoch 毫秒）窗口是按哪个时区解释的，
以及按预约时间字段过滤是否可靠，为「服务端预过滤」方案提供依据。

用法：
    .venv/bin/python tools/sss_post_latency_probe.py --captcha-file-mode
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import sss as S  # noqa: E402
from app.api_client import SssApiClient  # noqa: E402

CONFIG_PATH = Path.home() / ".config" / "yikou-light-food" / "config.json"
CST = dt.timezone(dt.timedelta(hours=8))
# 目标门店是一口轻食 211053；这里故意换成一个不存在的门店号。
INVALID_STORE_ID = 999_999_999


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


def epoch_ms(day: str, hour: int, minute: int, second: int, tz) -> int:
    return int(dt.datetime.strptime(day, "%Y-%m-%d").replace(
        hour=hour, minute=minute, second=second, tzinfo=tz).timestamp() * 1000)


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # pragma: no cover
        pass
    parser = argparse.ArgumentParser(description="真实报文形状 POST 耗时 + 过滤时区验证")
    parser.add_argument("--captcha-file-mode", action="store_true")
    parser.add_argument("--captcha-image", default="/tmp/sss_captcha_live.png")
    parser.add_argument("--captcha-code-file", default="/tmp/sss_code.txt")
    parser.add_argument("--captcha-wait", type=float, default=300.0)
    parser.add_argument("--out", default="/tmp/sss_probe/post_latency_report.txt")
    parser.add_argument("--rounds", type=int, default=3, help="每种并发档位的请求数")
    parser.add_argument("--workers", default="1,4,8", help="并发档位，逗号分隔")
    args = parser.parse_args()

    cfg = load_config()
    url = str(cfg.get("sss_url") or S.DEFAULT_SSS_URL)
    account = str(cfg.get("sss_account") or "")
    timeout_s = float(cfg.get("sss_read_timeout_s") or 20.0)
    excel = str(cfg.get("sss_excel_path") or "")

    lines: list[str] = []
    log = lines.append
    log(f"# 真实报文形状 POST 耗时 + 过滤时区验证  {time.strftime('%Y-%m-%d %H:%M:%S')}")

    from app.credentials import get_sss_password
    password = get_sss_password(account) or os.environ.get("YIKOU_SSS_PASSWORD", "")
    if not password:
        import getpass
        password = getpass.getpass(f"请输入 {account} 的闪时送登录密码：")

    # 真实报文形状：拿 Excel 第一单的完整 payload，仅把 storeId 换成不存在的门店。
    orders = S.load_sss_orders(excel)
    tasks = S._collect_tasks(
        orders, int(cfg.get("sss_store_id") or 211053),
        {"lnt": cfg.get("sss_fixed_lnt"), "lat": cfg.get("sss_fixed_lat"),
         "areaCode": cfg.get("sss_fixed_area_code"),
         "addressDetail": cfg.get("sss_fixed_address_detail")},
        str(cfg.get("sss_product_name") or "轻食"), account=account,
        batch_id="probe", idempotency_field="")
    template = json.loads(json.dumps(tasks[0]["payload"], ensure_ascii=False))
    probe_body = dict(template, storeId=INVALID_STORE_ID)
    log(f"探针报文：真实报文形状，storeId={INVALID_STORE_ID}（不存在的门店，服务端必然校验失败，不会建单）")
    log(f"真实报文示例字段：{sorted(template)[:12]}\n")

    client = SssApiClient(url, account, password, timeout=(5.0, timeout_s), pool_size=8)
    png = client.fetch_captcha()
    code = (captcha_from_file(png, args.captcha_image, args.captcha_code_file, args.captcha_wait)
            if args.captcha_file_mode else input("请输入验证码：").strip())
    client.login(code)
    log("登录成功\n")

    # 0) 时区语义：同一「日期」在 CST 与 UTC 下窗口不同，看哪个窗口包含目标日订单
    log("## 0. startTime/endTime 窗口的时区语义（status=2）")
    target_day = str(S._task_fingerprint(tasks[0]).expected_delivery_time)[:10]
    order_day = time.strftime("%Y-%m-%d")
    for label, tz in (("CST+8", CST), ("UTC", dt.timezone.utc)):
        for day in (order_day, target_day):
            query = {"pageNo": 1, "pageSize": 100, "sortType": 1, "sort": 1, "status": 2,
                     "startTime": epoch_ms(day, 0, 0, 0, tz),
                     "endTime": epoch_ms(day, 23, 59, 59, tz)}
            path = f"{S._ORDER_LIST_PATH}?{urlencode(query)}"
            started = time.perf_counter()
            payload = client.get_json(path)
            elapsed = time.perf_counter() - started
            try:
                records, total = S._list_records(payload)
            except Exception as exc:
                log(f"- 窗口 {day}（{label}）→ 结构异常：{str(exc)[:60]}")
                continue
            days: dict[str, int] = {}
            for record in records:
                d = S._normalise_delivery_time(
                    S._pick(record, ("expectedDeliveryTime", "appointmentTime")))[:10]
                days[d] = days.get(d, 0) + 1
            log(f"- 窗口 {day} 00:00-23:59（{label}）→ total={total} 送达日分布={dict(sorted(days.items()))}  {elapsed:.2f}s")

    # 1) 真实形状 POST 的耗时（单发 + 并发）
    log("")
    log("## 1. 真实报文形状 POST 耗时（storeId 不存在 → 必然校验失败）")
    def one(_index: int) -> tuple[float, str]:
        worker = client.fork()
        started = time.perf_counter()
        try:
            payload = worker.post_json(S._CREATE_ORDER_PATH, probe_body)
            detail = json.dumps(payload, ensure_ascii=False)[:200]
        except Exception as exc:
            detail = f"{type(exc).__name__}: {str(exc)[:160]}"
        finally:
            worker.close()
        return time.perf_counter() - started, detail

    for workers_text in args.workers.split(","):
        workers = max(1, int(workers_text))
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, range(args.rounds)))
        wall = time.perf_counter() - started
        samples = [r[0] for r in results]
        log(f"- 并发 {workers} 路 × {args.rounds} 次：单次 min {min(samples):.2f}s / "
            f"中位 {statistics.median(samples):.2f}s / max {max(samples):.2f}s；"
            f"总墙钟 {wall:.2f}s → 吞吐 {len(samples) / wall:.2f} 次/秒")
        log(f"  服务端响应示例：{results[0][1]}")

    client.close()
    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"\n（报告已写入 {args.out}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
