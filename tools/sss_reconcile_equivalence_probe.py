#!/usr/bin/env python3
"""验证「服务端过滤预筛」与「当前全量扫描」的对账结果是否等价。

这是给优化方案做前置验证：只证明过滤有效还不够，必须证明**用过滤后的
记录跑现有 `_reconcile_tasks`，结论与全量扫描完全一致**（confirmed /
missing / duplicate 三项逐单相同），否则优化会带来重复下单风险。

同时统计：目标批次订单在站内列表里的分布（前几页能否取全），用来估算
过滤方案能把 23 页降到几页。

只读 GET。
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


def make_replay(pages: list[dict]):
    """把已抓到的分页响应做成 fetch_json，交给真实 _reconcile_tasks 复用。"""
    state = {"index": 0}

    def fetch(path: str) -> dict:
        index = state["index"]
        state["index"] = index + 1
        if index >= len(pages):
            return {"success": True, "result": {"records": [], "total": len(pages) * 100}}
        return pages[index]

    return fetch, state


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # pragma: no cover
        pass
    parser = argparse.ArgumentParser(description="过滤预筛 vs 全量扫描 对账等价性验证")
    parser.add_argument("--captcha-file-mode", action="store_true")
    parser.add_argument("--captcha-image", default="/tmp/sss_captcha_live.png")
    parser.add_argument("--captcha-code-file", default="/tmp/sss_code.txt")
    parser.add_argument("--captcha-wait", type=float, default=300.0)
    parser.add_argument("--pages", type=int, default=10, help="全量策略最多抓多少页（默认 10）")
    parser.add_argument("--out", default="/tmp/sss_probe/equiv_report.txt")
    args = parser.parse_args()

    cfg = load_config()
    url = str(cfg.get("sss_url") or S.DEFAULT_SSS_URL)
    account = str(cfg.get("sss_account") or "")
    excel = str(cfg.get("sss_excel_path") or "")
    timeout_s = float(cfg.get("sss_read_timeout_s") or 20.0)
    idempotency_field = str(cfg.get("sss_idempotency_field") or "")

    lines: list[str] = []
    log = lines.append
    log(f"# 过滤预筛 vs 全量扫描 对账等价性验证  {time.strftime('%Y-%m-%d %H:%M:%S')}")

    orders = S.load_sss_orders(excel)
    tasks = S._collect_tasks(
        orders, int(cfg.get("sss_store_id") or 0),
        {"lnt": cfg.get("sss_fixed_lnt"), "lat": cfg.get("sss_fixed_lat"),
         "areaCode": cfg.get("sss_fixed_area_code"),
         "addressDetail": cfg.get("sss_fixed_address_detail")},
        str(cfg.get("sss_product_name") or "轻食"), account=account,
        batch_id="probe", idempotency_field=idempotency_field)
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

    def fetch_page(query: dict) -> dict:
        return client.get_json(f"{S._ORDER_LIST_PATH}?{urlencode(query)}")

    # 策略 A：当前实现——无过滤，逐页扫描
    log("## 策略 A：当前实现（无过滤，逐页扫全量）")
    strategy_a: list[dict] = []
    full_records: list[dict] = []
    page_no = 1
    started = time.perf_counter()
    while page_no <= max(1, args.pages):
        payload = fetch_page({"pageNo": page_no, "pageSize": 100, "sortType": 1, "sort": 1})
        records, total = S._list_records(payload)
        strategy_a.append(payload)
        full_records.extend(records)
        wanted = [r for r in records if S._record_active_for_days(r, set(wanted_days))]
        log(f"- page {page_no}: 返回 {len(records)} 条，其中目标日 {len(wanted)} 条，total={total}")
        if not records or len(records) < 100:
            break
        page_no += 1
    scan_s = time.perf_counter() - started
    log(f"- 策略 A 抓了 {len(strategy_a)} 页，耗时 {scan_s:.1f}s，累积记录 {len(full_records)} 条")

    # 策略 B：服务端过滤预筛——直接调用生产代码的 _build_list_prefilter，
    # 保证验证的就是将来真正上线的那套参数（含 ±1 天安全边界）。
    log("")
    log("## 策略 B：服务端过滤预筛（生产函数 _build_list_prefilter + status 不带）")
    prefilter = S._build_list_prefilter(tasks)
    log(f"- 生产预筛参数：{prefilter}")
    strategy_b: list[dict] = []
    b_records: list[dict] = []
    b_started = time.perf_counter()
    b_page = 1
    while b_page <= 40:
        payload = fetch_page({"pageNo": b_page, "pageSize": S._LIST_PAGE_SIZE,
                              "sortType": 1, "sort": 1, **prefilter})
        records, total = S._list_records(payload)
        strategy_b.append(payload)
        b_records.extend(records)
        log(f"- page {b_page}: 返回 {len(records)} 条，total={total}")
        if not records or len(records) < 100:
            break
        b_page += 1
    b_s = time.perf_counter() - b_started
    log(f"- 策略 B 抓了 {len(strategy_b)} 页，耗时 {b_s:.1f}s，返回记录 {len(b_records)} 条")

    # 用真实 _reconcile_tasks 对两份记录分别对账，比较结论
    log("")
    log("## 等价性：真实 _reconcile_tasks 对两份记录的结论")
    replay_a, state_a = make_replay(strategy_a)
    recon_a = S._reconcile_tasks(tasks, replay_a)
    replay_b, state_b = make_replay(strategy_b)
    recon_b = S._reconcile_tasks(tasks, replay_b)

    for name, recon, state in (("A 全量扫描", recon_a, state_a), ("B 过滤预筛", recon_b, state_b)):
        log(f"- {name}：匹配 {recon.matched_count}/{len(tasks)}，缺失 {len(recon.missing)}，"
            f"重复 {recon.duplicate_count}；实际取页 {state['index']} 次")

    same = (recon_a.confirmed == recon_b.confirmed
            and {t["identifier"] for t in recon_a.missing} == {t["identifier"] for t in recon_b.missing}
            and recon_a.duplicate_count == recon_b.duplicate_count)
    log("")
    log(f"结论：两份记录的对账结果{'完全一致 ✅' if same else '不一致 ❌（过滤方案不可用）'}")
    if not same:
        only_a = recon_a.confirmed - recon_b.confirmed
        only_b = recon_b.confirmed - recon_a.confirmed
        log(f"  A 独有 {len(only_a)} 项，B 独有 {len(only_b)} 项")
        log(f"  A 缺失：{[t['identifier'] for t in recon_a.missing][:10]}")
        log(f"  B 缺失：{[t['identifier'] for t in recon_b.missing][:10]}")
    log(f"耗时对比：A（{len(strategy_a)} 页）{scan_s:.1f}s  vs  B（{len(strategy_b)} 页）{b_s:.1f}s")

    client.close()
    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"\n（报告已写入 {args.out}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
