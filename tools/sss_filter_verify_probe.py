#!/usr/bin/env python3
"""闪时送列表过滤「可用形式」与 POST 并发吞吐只读验证（诊断用）。

第一轮侦察（tools/sss_list_filter_probe.py）实测结论：
  - 服务端字段名是 ``startTime`` / ``endTime``（Long，epoch 毫秒），
    传字符串会被 Spring 直接 BindException 拒绝；
  - ``statusList`` 无效，``status``（单数，int）有效（status=2 → 66 单）；
  - ``appointmentTime`` 起止（毫秒）有效（今天 → 69 单）。
所以当年“服务端不支持过滤”的判断来自参数类型/命名不匹配，而非服务端无此能力。

本脚本验证：
  A. ``status`` + ``startTime/endTime`` 组合，以及 epoch 毫秒的时区语义；
  B. ``pageSize`` 放大是否线性变慢（决定能否一页拉完）；
  C. 并发 POST 的吞吐（用故意非法报文，服务端只会校验失败，不会建单）。

只做只读 GET + 故意非法 POST，不会创建任何订单。
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
    value = dt.datetime.strptime(day, "%Y-%m-%d").replace(
        hour=hour, minute=minute, second=second, tzinfo=tz)
    return int(value.timestamp() * 1000)


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # pragma: no cover
        pass
    parser = argparse.ArgumentParser(description="闪时送过滤形式 + POST 并发验证")
    parser.add_argument("--captcha-file-mode", action="store_true")
    parser.add_argument("--captcha-image", default="/tmp/sss_captcha_live.png")
    parser.add_argument("--captcha-code-file", default="/tmp/sss_code.txt")
    parser.add_argument("--captcha-wait", type=float, default=300.0)
    parser.add_argument("--out", default="/tmp/sss_probe/filter2_report.txt")
    parser.add_argument("--skip-post", action="store_true", help="跳过并发 POST 测试")
    parser.add_argument("--days", default="2026-09-10,2026-09-11",
                        help="本次批次的预约送达日（按 Excel 语义，可跨天）")
    args = parser.parse_args()

    cfg = load_config()
    url = str(cfg.get("sss_url") or S.DEFAULT_SSS_URL)
    account = str(cfg.get("sss_account") or "")
    timeout_s = float(cfg.get("sss_read_timeout_s") or 20.0)
    days = [d.strip() for d in args.days.split(",") if d.strip()]

    lines: list[str] = []
    log = lines.append
    log(f"# 闪时送过滤形式 + POST 并发验证  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"批次预约送达日：{days}")

    from app.credentials import get_sss_password
    password = get_sss_password(account) or os.environ.get("YIKOU_SSS_PASSWORD", "")
    if not password:
        import getpass
        password = getpass.getpass(f"请输入 {account} 的闪时送登录密码：")

    client = SssApiClient(url, account, password, timeout=(5.0, timeout_s), pool_size=8)
    png = client.fetch_captcha()
    code = (captcha_from_file(png, args.captcha_image, args.captcha_code_file, args.captcha_wait)
            if args.captcha_file_mode else input("请输入验证码：").strip())
    client.login(code)
    log("登录成功\n")

    def call(query: dict) -> tuple[dict, float, int]:
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

    def show(label: str, query: dict, expect_days: set[str] | None = None) -> dict:
        payload, elapsed, size = call(query)
        if payload.get("success") is False:
            log(f"- {label:46s} {elapsed:5.2f}s 失败：{str(payload.get('message'))[:120]}")
            return {"total": None, "days": {}}
        try:
            records, total = S._list_records(payload)
        except Exception as exc:
            log(f"- {label:46s} {elapsed:5.2f}s 结构异常：{exc}")
            return {"total": None, "days": {}}
        day_count: dict[str, int] = {}
        for record in records:
            day = S._normalise_delivery_time(
                S._pick(record, ("expectedDeliveryTime", "expected_delivery_time",
                                 "appointmentTime", "appointment_time")))[:10]
            day_count[day] = day_count.get(day, 0) + 1
        statuses: dict[str, int] = {}
        for record in records:
            status = str(S._pick(record, ("status", "orderStatus", "state")))
            statuses[status] = statuses.get(status, 0) + 1
        flag = ""
        if expect_days is not None:
            flag = "  ← 覆盖全部目标日" if expect_days <= set(day_count) else ""
        log(f"- {label:46s} {elapsed:5.2f}s {size // 1024:4d}KB total={total} 首页={len(records)} "
            f"状态分布={dict(sorted(statuses.items())[:6])} 日={dict(sorted(day_count.items())[:4])}{flag}")
        return {"total": total, "days": day_count, "records": len(records)}

    base = {"pageNo": 1, "pageSize": 100, "sortType": 1, "sort": 1}

    log("## A. 组合过滤（epoch 毫秒 + status）")
    for day in days:
        for tz, tz_name in ((CST, "CST+8"), (dt.timezone.utc, "UTC")):
            start = epoch_ms(day, 0, 0, 0, tz)
            end = epoch_ms(day, 23, 59, 59, tz)
            query = dict(base, status=2, startTime=start, endTime=end)
            show(f"status=2 & {day} 00:00-23:59 ({tz_name}, startTime/endTime)", query, {day})
    # 用 appointmentTime 交叉验证：同一批订单在两种字段下的落点是否一致
    for day in days:
        start = epoch_ms(day, 0, 0, 0, CST)
        end = epoch_ms(day, 23, 59, 59, CST)
        show(f"status=2 & {day} (appointmentTime 起止, CST+8)",
             dict(base, status=2, appointmentStartTime=start, appointmentEndTime=end), {day})
    # 一个请求覆盖两天：按 startTime 窗口 从第一天 00:00 到最后一天 23:59
    if len(days) > 1:
        start = epoch_ms(days[0], 0, 0, 0, CST)
        end = epoch_ms(days[-1], 23, 59, 59, CST)
        show("status=2 & 全部目标日 (startTime 窗口, CST+8)",
             dict(base, status=2, startTime=start, endTime=end), set(days))

    log("")
    log("## B. pageSize 放大成本（能否一页拉完）")
    for size in (100, 300, 1000, 3000):
        show(f"status=2 & 目标日窗口 & pageSize={size}",
             dict(base, pageSize=size, status=2,
                  startTime=epoch_ms(days[0], 0, 0, 0, CST),
                  endTime=epoch_ms(days[-1], 23, 59, 59, CST)), set(days))

    if not args.skip_post:
        log("")
        log("## C. create-order POST 并发吞吐（故意非法报文，不会建单）")
        for workers in (1, 4, 8):
            def one(index: int) -> float:
                body = {"clientRequestId": f"timing-probe-invalid-{index}",
                        "noSuchField": "probe"}
                started = time.perf_counter()
                worker = client.fork()
                try:
                    worker.post_json(POST_PATH, body)
                except Exception:
                    pass
                finally:
                    worker.close()
                return time.perf_counter() - started

            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                samples = list(pool.map(one, range(workers * 2)))
            wall = time.perf_counter() - started
            log(f"- 并发 {workers} 路 × {workers * 2} 次：单次 min {min(samples):.2f}s / "
                f"中位 {statistics.median(samples):.2f}s / max {max(samples):.2f}s；"
                f"总墙钟 {wall:.2f}s → 吞吐 {len(samples) / wall:.2f} 次/秒")

    client.close()
    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"\n（报告已写入 {args.out}）")
    return 0


POST_PATH = S._CREATE_ORDER_PATH

if __name__ == "__main__":
    raise SystemExit(main())
