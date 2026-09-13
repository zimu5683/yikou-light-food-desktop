#!/usr/bin/env python3
"""闪时送下单耗时只读探针（诊断用，不修改任何业务代码）。

目的：用真实网络数据定位「开始下单」到「下单前站内对账」之间的 49 秒，
以及提交阶段 ~2.3 秒/单 的耗时到底花在哪里。

只做读操作：
  1. 抓取验证码（GET），弹窗让你输入；登录（POST），复用 app/api_client.SssApiClient；
  2. 计时调用 /consumer/account/get-login-user-account（余额，只读）；
  3. 计时分页调用 /consumer/order/one-touch-send/list（对账用的列表接口），
     记录每页 RTT / 返回条数 / total / keep-alive 复用情况；
  4. 用本地 Excel 的 66 单做一次离线对账，看 _list_pending_orders 的真实成本。

不会调用 create-order-from-client，不会创建任何订单。

用法（在仓库根目录）：
    .venv/bin/python tools/sss_timing_probe.py --pages 5
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
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


def load_config() -> dict:
    if CONFIG_PATH.is_file():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def ask_captcha(png: bytes) -> str:
    """弹出 Tk 窗口显示验证码图片并读取用户输入。"""
    import tkinter as tk

    root = tk.Tk()
    root.title("闪时送验证码（只读探针）")
    root.attributes("-topmost", True)
    photo = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
    tk.Label(root, text="请输入图中验证码后回车：", font=("sans", 12)).pack(padx=12, pady=(12, 4))
    tk.Label(root, image=photo).pack(padx=12, pady=4)
    entry = tk.Entry(root, font=("mono", 16), justify="center", width=12)
    entry.pack(padx=12, pady=8)
    entry.focus_force()
    result: dict[str, str] = {}

    def submit(_event=None) -> None:
        result["code"] = entry.get().strip()
        root.destroy()

    entry.bind("<Return>", submit)
    tk.Button(root, text="确定", command=submit).pack(pady=(0, 12))
    root.mainloop()
    code = result.get("code", "")
    if not code:
        raise SystemExit("未输入验证码，已取消")
    return code


def captcha_from_file(png: bytes, image_path: str, code_path: str,
                      wait_s: float = 180.0) -> str:
    """把验证码写到 ``image_path``，等待外部把识别结果写进 ``code_path``。

    会话绑定：验证码在同一进程、同一 HTTPS 会话里签发与校验，所以取图之后
    必须由本进程继续登录，不能另起进程。
    """
    image_file = Path(image_path)
    code_file = Path(code_path)
    image_file.write_bytes(png)
    code_file.unlink(missing_ok=True)
    deadline = time.monotonic() + wait_s
    print(f"[probe] 验证码已写入 {image_file}，等待 {code_file} …", flush=True)
    while time.monotonic() < deadline:
        if code_file.is_file():
            code = code_file.read_text(encoding="utf-8").strip()
            code_file.unlink(missing_ok=True)
            if code:
                return code
        time.sleep(0.2)
    raise SystemExit(f"等待验证码超时（{wait_s:g}s）：{code_file}")


def timed_get(client: SssApiClient, url: str) -> tuple[dict, dict]:
    """GET 并返回 (payload, 计时)。同时记录该请求是否复用了已建立的连接。

    urllib3 2.x 的客户端响应不再暴露 ``.timings``，因此这里读取连接池里
    实际使用的连接对象：``_connection`` 非空表示本次是 keep-alive 复用
    （没有重新 TCP/TLS 握手），为空表示新建了连接。
    """
    started = time.perf_counter()
    resp = client.session.get(client.origin + url,
                              headers=({"token": client.token} if client.token else {}),
                              timeout=client.timeout)
    elapsed = time.perf_counter() - started
    payload = resp.json()
    raw = resp.raw
    pool = getattr(raw, "_pool", None)
    connection = getattr(raw, "_connection", None)
    info = {
        "seconds": round(elapsed, 3),
        "http": resp.status_code,
        "bytes": len(resp.content),
        "reused_conn": bool(getattr(pool, "_pool", None)) and connection is not None,
        "server": resp.headers.get("Server", ""),
        "content_encoding": resp.headers.get("Content-Encoding", "-"),
    }
    return payload, info


def summarise(values: list[float]) -> str:
    if not values:
        return "n/a"
    if len(values) == 1:
        return f"{values[0]:.3f}s"
    return (f"min {min(values):.3f}s / 中位 {statistics.median(values):.3f}s / "
            f"max {max(values):.3f}s / 合计 {sum(values):.3f}s")


def record_shape(record: dict) -> dict:
    """只返回字段名与类型，避免把用户隐私写进报告。"""
    out: dict[str, str] = {}
    for key, value in record.items():
        if isinstance(value, dict):
            out[key] = "{" + ",".join(list(value)[:12]) + "}"
        elif isinstance(value, list):
            out[key] = f"list[{len(value)}]"
        else:
            out[key] = type(value).__name__
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # pragma: no cover
        pass
    parser = argparse.ArgumentParser(description="闪时送只读耗时探针")
    parser.add_argument("--pages", type=int, default=5, help="最多抓取多少页订单列表（默认 5）")
    parser.add_argument("--page-size", type=int, default=100, help="每页条数（默认 100，与业务一致）")
    parser.add_argument("--excel", default="", help="订单 Excel 路径，默认读配置")
    parser.add_argument("--out", default="", help="把报告写到该文件（默认打印到标准输出）")
    parser.add_argument("--captcha-image", default="/tmp/sss_captcha_live.png",
                        help="非交互模式：验证码 PNG 落盘路径")
    parser.add_argument("--captcha-code-file", default="/tmp/sss_code.txt",
                        help="非交互模式：从该文件读取验证码（读完即删）")
    parser.add_argument("--captcha-file-mode", action="store_true",
                        help="不弹窗，改为写图 + 等文件的方式获取验证码")
    parser.add_argument("--captcha-wait", type=float, default=180.0,
                        help="非交互模式下等待验证码的最长秒数")
    args = parser.parse_args()

    cfg = load_config()
    url = str(cfg.get("sss_url") or S.DEFAULT_SSS_URL)
    account = str(cfg.get("sss_account") or "")
    excel = args.excel or str(cfg.get("sss_excel_path") or "")
    timeout_s = float(cfg.get("sss_read_timeout_s") or 20.0)

    lines: list[str] = []
    log = lines.append

    log(f"# 闪时送只读耗时探针  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"url={url} account={account[:3]}****{account[-4:] if len(account) > 7 else ''} "
        f"read_timeout={timeout_s:g}s page_size={args.page_size}")

    password = ""
    try:
        from app.credentials import get_sss_password
        password = get_sss_password(account) or ""
        log("已从系统钥匙串读取闪时送密码" if password else "系统钥匙串中没有闪时送密码，将提示输入")
    except Exception as exc:  # pragma: no cover
        log(f"（读取已保存密码失败：{exc}）")
    if not password:
        if args.captcha_file_mode:
            print("[probe] 系统钥匙串里没有密码：请用环境变量 YIKOU_SSS_PASSWORD 提供",
                  file=sys.stderr)
            password = os.environ.get("YIKOU_SSS_PASSWORD", "")
            if not password:
                raise SystemExit("缺少闪时送密码（钥匙串为空且未设置 YIKOU_SSS_PASSWORD）")
        else:
            import getpass
            password = getpass.getpass(f"请输入 {account} 的闪时送登录密码：")

    client = SssApiClient(url, account, password, timeout=(5.0, timeout_s), pool_size=4)
    t0 = time.perf_counter()
    captcha = client.fetch_captcha()
    t_captcha = time.perf_counter() - t0
    log(f"验证码 GET 耗时 {t_captcha:.3f}s（{len(captcha)} 字节）")

    code = (captcha_from_file(captcha, args.captcha_image, args.captcha_code_file,
                              args.captcha_wait)
            if args.captcha_file_mode else ask_captcha(captcha))
    t0 = time.perf_counter()
    client.login(code)
    log(f"登录 POST 耗时 {time.perf_counter() - t0:.3f}s")

    # 1) 余额接口（只读）
    payload, info = timed_get(client, S._ACCOUNT_PATH)
    log(f"余额接口 {S._ACCOUNT_PATH} -> {info['seconds']}s http={info['http']} "
        f"bytes={info['bytes']} reused={info['reused_conn']}")

    # 2) 列表接口逐页计时（业务里 _list_pending_orders 就是逐个这么拉）
    log("")
    log(f"## 订单列表分页计时（{S._ORDER_LIST_PATH}）")
    per_page: list[float] = []
    total_records: int | None = None
    all_days: dict[str, int] = {}
    first_shape: dict | None = None
    page = 1
    raw_text = ""
    while page <= max(1, args.pages):
        query = urlencode({"pageNo": page, "pageSize": args.page_size,
                           "sortType": 1, "sort": 1})
        path = f"{S._ORDER_LIST_PATH}?{query}"
        raw_text = path
        payload, info = timed_get(client, path)
        if payload.get("success") is False:
            log(f"page {page}: 接口返回失败 {payload.get('message')}")
            break
        try:
            records, total = S._list_records(payload)
        except Exception as exc:
            log(f"page {page}: 解析失败 {exc}；payload 顶层键={list(payload)[:10]}")
            break
        per_page.append(info["seconds"])
        if page == 1:
            total_records = total
            if records:
                first_shape = record_shape(records[0])
        for record in records:
            day = S._normalise_delivery_time(
                S._pick(record, ("expectedDeliveryTime", "expected_delivery_time",
                                 "appointmentTime", "appointment_time")))[:10]
            all_days[day] = all_days.get(day, 0) + 1
        log(f"page {page}: {info['seconds']}s http={info['http']} bytes={info['bytes']} "
            f"records={len(records)} total={total} reused_conn={info['reused_conn']} "
            f"server={info['server'] or '-'} enc={info['content_encoding']}")
        if not records or len(records) < args.page_size:
            break
        if total is not None and page * args.page_size >= total:
            break
        page += 1

    log("")
    log(f"逐页耗时：{summarise(per_page)}  (n={len(per_page)})")
    if total_records is not None:
        pages_needed = -(-int(total_records) // args.page_size)
        log(f"站内 total={total_records} → 若每页 {args.page_size} 条，对账需要 {pages_needed} 页")
        log(f"按实测中位每页耗时估算，单次对账的列表成本 ≈ "
            f"{statistics.median(per_page) * pages_needed:.1f}s" if per_page else "n/a")
    log(f"站内订单的预约送达日分布（前 10）：{dict(list(sorted(all_days.items()))[:10])}")
    if first_shape:
        log(f"列表记录字段结构：{json.dumps(first_shape, ensure_ascii=False)}")
    log(f"列表请求样例路径：{raw_text}")

    # 3) 本地离线对账：把已抓到的页喂给 _reconcile_tasks，验证对账本身是否吃 CPU
    if excel:
        try:
            orders = S.load_sss_orders(excel)
            total_orders = sum(len(v) for v in orders.values())
            tasks = S._collect_tasks(
                orders, int(cfg.get("sss_store_id") or 0),
                {"lnt": cfg.get("sss_fixed_lnt"), "lat": cfg.get("sss_fixed_lat"),
                 "areaCode": cfg.get("sss_fixed_area_code"),
                 "addressDetail": cfg.get("sss_fixed_address_detail")},
                str(cfg.get("sss_product_name") or "轻食"), account=account,
                batch_id="probe", idempotency_field="")
            started = time.perf_counter()
            fingerprints = [S._task_fingerprint(task) for task in tasks]
            build_s = time.perf_counter() - started
            log("")
            log(f"本地任务组装：{len(tasks)}/{total_orders} 单，指纹计算 {build_s * 1000:.1f} ms "
                f"（纯 CPU，可忽略）")
        except Exception as exc:
            log(f"本地离线对账准备失败：{exc}")

    client.close()
    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"\n（报告已写入 {args.out}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
