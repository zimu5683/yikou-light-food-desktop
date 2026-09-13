#!/usr/bin/env python3
"""前置实验：目标日订单的 status 分布，决定预筛要不要带 ``status`` 参数。

背景：`_INACTIVE_ORDER_STATUS` 当前是空集，即现有代码把所有状态都当「活跃」。
如果服务端预筛硬编码 ``status=2``，而站内又存在 status≠2 但仍应参与匹配的在途
订单，就会把订单筛没、误判成「缺失」。本脚本只读扫描，统计：

  1. 目标日窗口内（无 status 过滤）各状态码的数量；
  2. 这些日期上「非 2」状态的订单长什么样（字段结构，便于判断是否属于在途）；
  3. 随机抽一页历史订单的全局状态分布，作为参照。

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


def day_ms(day: str, hour: int, minute: int, second: int) -> int:
    return int(dt.datetime.strptime(day, "%Y-%m-%d").replace(
        hour=hour, minute=minute, second=second, tzinfo=CST).timestamp() * 1000)


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # pragma: no cover
        pass
    parser = argparse.ArgumentParser(description="目标日订单 status 分布只读实验")
    parser.add_argument("--captcha-file-mode", action="store_true")
    parser.add_argument("--captcha-image", default="/tmp/sss_captcha_live.png")
    parser.add_argument("--captcha-code-file", default="/tmp/sss_code.txt")
    parser.add_argument("--captcha-wait", type=float, default=300.0)
    parser.add_argument("--history-pages", type=int, default=6,
                        help="额外抽样多少页历史订单看全局状态分布")
    parser.add_argument("--out", default="/tmp/sss_probe/status_report.txt")
    args = parser.parse_args()

    cfg = load_config()
    url = str(cfg.get("sss_url") or S.DEFAULT_SSS_URL)
    account = str(cfg.get("sss_account") or "")
    excel = str(cfg.get("sss_excel_path") or "")
    timeout_s = float(cfg.get("sss_read_timeout_s") or 20.0)

    lines: list[str] = []
    log = lines.append
    log(f"# 目标日订单 status 分布只读实验  {time.strftime('%Y-%m-%d %H:%M:%S')}")

    orders = S.load_sss_orders(excel)
    tasks = S._collect_tasks(
        orders, int(cfg.get("sss_store_id") or 0),
        {"lnt": cfg.get("sss_fixed_lnt"), "lat": cfg.get("sss_fixed_lat"),
         "areaCode": cfg.get("sss_fixed_area_code"),
         "addressDetail": cfg.get("sss_fixed_address_detail")},
        str(cfg.get("sss_product_name") or "轻食"), account=account,
        batch_id="probe", idempotency_field="")
    wanted_days = sorted({S._task_fingerprint(t).expected_delivery_time[:10] for t in tasks})
    log(f"批次：{len(tasks)} 单，预约送达日={wanted_days}")

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

    def fetch(query: dict) -> tuple[list[dict], int | None]:
        payload = client.get_json(f"{S._ORDER_LIST_PATH}?{urlencode(query)}")
        if payload.get("success") is False:
            raise LookupError(str(payload.get("message"))[:160])
        return S._list_records(payload)

    # 1) 目标日窗口（不带 status 过滤）→ 各状态码分布；再与带 status=2 的结果对比
    first = dt.datetime.strptime(wanted_days[0], "%Y-%m-%d") - dt.timedelta(days=1)
    last = dt.datetime.strptime(wanted_days[-1], "%Y-%m-%d") + dt.timedelta(days=1)
    window = {"startTime": day_ms(first.strftime("%Y-%m-%d"), 0, 0, 0),
              "endTime": day_ms(last.strftime("%Y-%m-%d"), 23, 59, 59)}

    log("## 1. 窗口内（无 status 过滤）逐页记录，按「送达日 × status」统计")
    status_by_day: dict[str, dict[str, int]] = {}
    no_status_records: list[dict] = []
    page = 1
    while page <= 10:
        records, total = fetch(dict(pageNo=page, pageSize=100, sortType=1, sort=1, **window))
        if page == 1:
            log(f"- 窗口 total={total}")
        if not records:
            break
        no_status_records.extend(records)
        for record in records:
            day = S._normalise_delivery_time(
                S._pick(record, ("expectedDeliveryTime", "appointmentTime")))[:10]
            status = str(S._pick(record, ("status", "orderStatus", "state")))
            bucket = status_by_day.setdefault(day, {})
            bucket[status] = bucket.get(status, 0) + 1
        if len(records) < 100:
            break
        page += 1
    for day in sorted(status_by_day):
        mark = " ★目标日" if day in wanted_days else ""
        log(f"- 送达日 {day}: status 分布={dict(sorted(status_by_day[day].items()))}{mark}")

    target_non2 = [
        r for r in no_status_records
        if S._normalise_delivery_time(
            S._pick(r, ("expectedDeliveryTime", "appointmentTime")))[:10] in wanted_days
        and str(S._pick(r, ("status", "orderStatus", "state"))) != "2"
    ]
    log(f"- 目标日内 status≠2 的记录数：{len(target_non2)}")
    for record in target_non2[:5]:
        log(f"    status={S._pick(record, ('status',))} "
            f"customerOrderStatus={S._pick(record, ('customerOrderStatus',))} "
            f"pickGoodsStatus={S._pick(record, ('pickGoodsStatus',))} "
            f"izNeglected={S._pick(record, ('izNeglected',))} "
            f"orderTime={S._pick(record, ('orderTime',))}")

    log("")
    log("## 2. 同一窗口带 status=2 的对比")
    with_status: list[dict] = []
    page = 1
    while page <= 10:
        records, total = fetch(dict(pageNo=page, pageSize=100, sortType=1, sort=1,
                                    status=2, **window))
        if page == 1:
            log(f"- 带 status=2 的 total={total}")
        if not records:
            break
        with_status.extend(records)
        if len(records) < 100:
            break
        page += 1
    by_day_no_filter = {d: sum(v.values()) for d, v in status_by_day.items()}
    log(f"- 无过滤窗口记录 {len(no_status_records)} 条；带 status=2 记录 {len(with_status)} 条")
    log(f"- 无过滤各送达日合计={dict(sorted(by_day_no_filter.items()))}")

    log("")
    log(f"## 3. 历史抽样（前 {args.history_pages} 页无过滤）的全局状态分布")
    history_status: dict[str, int] = {}
    for page in range(1, max(1, args.history_pages) + 1):
        records, _total = fetch(dict(pageNo=page, pageSize=100, sortType=1, sort=1))
        if not records:
            break
        for record in records:
            status = str(S._pick(record, ("status", "orderStatus", "state")))
            history_status[status] = history_status.get(status, 0) + 1
    log(f"- 历史样本状态分布={dict(sorted(history_status.items()))}")

    log("")
    log("## 结论")
    log(f"- 目标日出现过的状态码：{sorted({s for d in wanted_days for s in status_by_day.get(d, {})})}")
    log(f"- 目标日内 status=2 记录数：{sum(status_by_day.get(d, {}).get('2', 0) for d in wanted_days)}"
        f" / 目标日总记录数：{sum(by_day_no_filter.get(d, 0) for d in wanted_days)}")
    if target_non2:
        log("- 存在 status≠2 的目标日订单 → 预筛**不要**硬编码 status，仅用时间窗")
    else:
        log("- 目标日订单全部 status=2 → 预筛带 status=2 安全（与非 2 状态即非活跃一致）")

    client.close()
    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"\n（报告已写入 {args.out}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
