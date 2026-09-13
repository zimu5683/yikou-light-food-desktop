#!/usr/bin/env python3
"""构建期：把 Playwright Chromium 抓取到发行目录，并精简体积。

程序运行时不探测系统 Edge/Chrome，也不下载浏览器；发行包里与可执行文件同级
的 ``browser/`` 目录就是自动化唯一使用的浏览器。这个脚本在构建阶段做四件事：

1. 按 requirements.txt 锁定的 Playwright 版本解析 Chromium revision；
2. 优先复用本机 Playwright 缓存，缺失时才用官方驱动下载（``--no-shell``，
   只要完整 Chromium，不要 headless shell）；
3. 删掉自动化用不到的内容（多余语言包、WidevineCdm）以压缩发行体积；
4. 写入 ``browser.json`` 版本标记，运行时据此提示版本错配。

用法::

    python scripts/fetch_browser.py                    # 输出到 vendor/browser
    python scripts/fetch_browser.py --output dist/browser
    python scripts/fetch_browser.py --keep-all         # 不精简，保留原样
    python scripts/fetch_browser.py --force            # 忽略已有输出，重新抓取
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from importlib.metadata import version as _package_version
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT / "vendor" / "browser"
MANIFEST_NAME = "browser.json"

# 精简时保留的语言包。Chromium 找不到匹配语言时回落到 en-US，因此这两个是
# 底线；en-GB 一起留下，避免英文环境下的拼写回落造成界面异常。
KEPT_LOCALE_PAKS = {"zh-CN.pak", "en-US.pak", "en-GB.pak"}
KEPT_LPROJ_DIRS = {"zh_CN.lproj", "en.lproj", "en_GB.lproj", "Base.lproj"}
# 自动化不播放受 DRM 保护的内容，这个组件在 Linux 上约 21MB。
DROPPED_DIR_NAMES = {"WidevineCdm"}


def _driver() -> tuple[str, str]:
    """返回 Playwright 的 ``(node, cli.js)`` 路径。"""
    from playwright._impl._driver import compute_driver_executable

    driver, cli = compute_driver_executable()
    return str(driver), str(cli)


def _browsers_json() -> dict:
    """读取 Playwright 自带的浏览器清单（revision / 版本号的事实来源）。"""
    import playwright

    path = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _chromium_entry() -> dict:
    for item in _browsers_json().get("browsers", []):
        if item.get("name") == "chromium":
            return item
    raise RuntimeError("Playwright 浏览器清单里没有 chromium 条目")


def _default_cache_dir() -> Path:
    """本机 Playwright 的默认浏览器缓存目录（用于复用，避免重复下载）。"""
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return root / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "ms-playwright"


def _download_chromium(staging: Path) -> None:
    """用官方驱动把完整 Chromium 下载到 staging 目录。"""
    driver, cli = _driver()
    from playwright._impl._driver import get_driver_env

    env = {**get_driver_env(), "PLAYWRIGHT_BROWSERS_PATH": str(staging)}
    print(f"[fetch_browser] 下载 Chromium 到 {staging} ...")
    result = subprocess.run(
        [driver, cli, "install", "--no-shell", "chromium"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode != 0:
        tail = (result.stdout or "").strip()[-800:]
        raise RuntimeError(f"Playwright Chromium 下载失败（退出码 {result.returncode}）：{tail}")


def _stage_chromium(staging: Path) -> Path:
    """确保 staging 里有一份 ``chromium-<revision>``，返回其路径。"""
    source = _default_cache_dir() / f"chromium-{_chromium_entry()['revision']}"
    target = staging / source.name
    if target.is_dir():
        return target
    staging.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        print(f"[fetch_browser] 复用本机缓存 {source}")
        shutil.copytree(source, target, symlinks=True)
        return target
    _download_chromium(staging)
    if not target.is_dir():
        raise RuntimeError(f"下载完成但未找到 {target}")
    return target


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _trim_locales(directory: Path, root: Path) -> list[str]:
    """只保留白名单语言包；命名不符合预期时整个目录原样保留。"""
    paks = sorted(directory.glob("*.pak"))
    if not any(pak.name in KEPT_LOCALE_PAKS for pak in paks):
        return []
    removed: list[str] = []
    for pak in paks:
        if pak.name in KEPT_LOCALE_PAKS:
            continue
        removed.append(f"{pak.relative_to(root)}（{pak.stat().st_size / 1024:.0f} KB）")
        pak.unlink(missing_ok=True)
    return removed


def _trim(root: Path) -> list[str]:
    """删除运行时用不到的载荷，返回被删项的说明列表。"""
    removed: list[str] = []
    # 由深到浅遍历，保证先处理内层目录再处理外层。
    for directory in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not directory.is_dir():
            continue
        if directory.name in DROPPED_DIR_NAMES:
            removed.append(f"{directory.relative_to(root)}（{_dir_size(directory) / 1_048_576:.1f} MB）")
            shutil.rmtree(directory, ignore_errors=True)
        elif directory.name == "locales":
            removed.extend(_trim_locales(directory, root))
        elif directory.suffix == ".lproj" and directory.name not in KEPT_LPROJ_DIRS:
            removed.append(f"{directory.relative_to(root)}")
            shutil.rmtree(directory, ignore_errors=True)
    return removed


def _manifest(entry: dict) -> dict:
    return {
        "playwright": _package_version("playwright"),
        "revision": str(entry.get("revision", "")),
        "browser_version": str(entry.get("browserVersion", "")),
        "platform": f"{sys.platform}-{platform.machine()}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取并精简内置 Chromium")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"输出目录（默认 {DEFAULT_OUTPUT}）")
    parser.add_argument("--keep-all", action="store_true", help="不做精简，保留 Chromium 原样")
    parser.add_argument("--force", action="store_true", help="忽略已有输出，重新抓取")
    args = parser.parse_args()

    output: Path = args.output.resolve()
    if output.exists():
        if not args.force:
            print(f"[fetch_browser] {output} 已存在；如需重建请加 --force")
            return 0
        shutil.rmtree(output)

    entry = _chromium_entry()
    staging = output.parent / f".{output.name}-staging"
    shutil.rmtree(staging, ignore_errors=True)
    before = after = 0
    try:
        browser = _stage_chromium(staging)
        before = _dir_size(browser)
        if not args.keep_all:
            for item in _trim(browser):
                print(f"[fetch_browser] 已删除 {item}")
        after = _dir_size(browser)
        output.mkdir(parents=True, exist_ok=True)
        shutil.move(str(browser), str(output / browser.name))
        (output / MANIFEST_NAME).write_text(
            json.dumps(_manifest(entry), ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    executable = _locate(output)
    if executable is None:
        print(f"[fetch_browser] 错误：{output} 里没找到 Chromium 可执行文件", file=sys.stderr)
        return 1
    saved = (before - after) / 1_048_576
    print(f"[fetch_browser] Chromium {entry.get('browserVersion')} → {executable}")
    print(f"[fetch_browser] 精简后 {after / 1_048_576:.0f} MB（省下 {saved:.0f} MB）")
    return 0


def _locate(root: Path) -> Path | None:
    """在输出目录里定位 Chromium 可执行文件（与运行时解析规则保持一致）。"""
    sys.path.insert(0, str(PROJECT))
    from app.automation import find_bundled_browser

    previous = os.environ.get("YIKOU_BROWSER_DIR")
    os.environ["YIKOU_BROWSER_DIR"] = str(root)
    try:
        return find_bundled_browser()
    finally:
        if previous is None:
            os.environ.pop("YIKOU_BROWSER_DIR", None)
        else:
            os.environ["YIKOU_BROWSER_DIR"] = previous


if __name__ == "__main__":
    raise SystemExit(main())
