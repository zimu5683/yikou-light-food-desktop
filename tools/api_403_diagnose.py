#!/usr/bin/env python3
"""诊断管理后台纯接口模式在 Windows 上返回 403 的原因。

只做只读探测，不写任何业务数据。会对比「走系统代理解析」与「强制直连」
两种情况的响应，从而把 403 定位到代理层还是 WAF/网络层。

用法::

    # 只看代理与连通性，不登录
    python tools/api_403_diagnose.py

    # 完整复现登录 403（密码只用于本次请求，不会打印）
    python tools/api_403_diagnose.py --username 13800000000 --password '***'

    # 指定后台地址
    python tools/api_403_diagnose.py --url https://m.icall.me/admin/#/login
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import requests  # noqa: E402
import urllib.request  # noqa: E402

from app.api_client import _browser_headers, origin_from_url  # noqa: E402


def _show_proxy_configuration() -> dict:
    print("=" * 68)
    print("1) 代理配置")
    print("=" * 68)
    env_proxies = urllib.request.getproxies()
    print(f"urllib.request.getproxies()      : {env_proxies or '（空）'}")
    print("  说明：Windows 上该函数会读取注册表 Internet Settings，")
    print("        Linux 上只读 http_proxy/https_proxy 环境变量。")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "no_proxy"):
        value = __import__("os").environ.get(name)
        if value:
            print(f"  环境变量 {name} = {value}")
    return env_proxies


def _probe(url: str, *, trust_env: bool, timeout: float = 15.0) -> tuple[int | None, str]:
    """对目标发一次 GET，返回 (状态码, 摘要)。"""
    session = requests.Session()
    session.headers.update(_browser_headers(url, admin=True))
    session.trust_env = trust_env
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        session.close()
    body = (resp.text or "").strip().replace("\n", " ")[:160]
    return resp.status_code, body


def _compare(origin: str) -> None:
    print()
    print("=" * 68)
    print("2) 同一请求：走系统代理 vs 强制直连")
    print("=" * 68)
    for label, trust_env in (("trust_env=True （当前程序行为）", True),
                             ("trust_env=False（强制直连）", False)):
        status, body = _probe(origin + "/admin/", trust_env=trust_env)
        print(f"\n[{label}]")
        print(f"  HTTP 状态 : {status if status is not None else '请求失败'}")
        print(f"  响应摘要  : {body[:120] or '（空）'}")
    print()
    print("判读：")
    print("  · 代理行 403/失败、直连行 200 → 403 由 Windows 系统代理造成。")
    print("  · 两行都是 403                → 与代理无关，是 WAF/出口 IP/请求特征问题。")


def _login(origin: str, username: str, password: str, *, trust_env: bool) -> None:
    label = "走系统代理" if trust_env else "强制直连"
    session = requests.Session()
    session.headers.update(_browser_headers(origin, admin=True))
    session.trust_env = trust_env
    print()
    print("=" * 68)
    print(f"3) 复现登录 POST /channel/login（{label}）")
    print("=" * 68)
    print(f"  实际使用的 User-Agent: {session.headers.get('User-Agent')}")
    print(f"  实际使用的 Origin    : {session.headers.get('Origin')}")
    print(f"  实际使用的 Referer   : {session.headers.get('Referer')}")
    try:
        resp = session.post(
            origin + "/channel/login",
            json={"username": username, "password": password, "remember": False},
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"  请求失败：{type(exc).__name__}: {exc}")
        return
    finally:
        session.close()
    print(f"  HTTP 状态 : {resp.status_code}")
    for key in ("Server", "Date", "Content-Type", "Set-Cookie", "X-Cache", "Via"):
        if key in resp.headers:
            print(f"  响应头 {key}: {resp.headers[key][:120]}")
    print(f"  响应摘要  : {(resp.text or '').strip().replace(chr(10), ' ')[:200] or '（空）'}")
    if resp.status_code == 403:
        print()
        print("  → 403 已复现。若上面第 2 步直连正常，请把 session.trust_env 设为 False。")


def main() -> int:
    parser = argparse.ArgumentParser(description="诊断纯接口模式 403")
    parser.add_argument("--url", default="https://m.icall.me/admin/#/login",
                        help="管理后台地址（用于提取 origin）")
    parser.add_argument("--username", default="", help="后台账号（可选）")
    parser.add_argument("--password", default="", help="后台密码（可选，不会被打印）")
    args = parser.parse_args()

    try:
        origin = origin_from_url(args.url)
    except ValueError as exc:
        print(f"无法解析地址：{exc}", file=sys.stderr)
        return 2
    print(f"目标 origin: {origin}")
    print(f"Python     : {sys.version.split()[0]}  平台: {sys.platform}")

    _show_proxy_configuration()
    _compare(origin)
    if args.username and args.password:
        _login(origin, args.username, args.password, trust_env=True)
        _login(origin, args.username, args.password, trust_env=False)
    else:
        print()
        print("（未提供账号密码，跳过登录复现。加 --username/--password 可完整复现。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
