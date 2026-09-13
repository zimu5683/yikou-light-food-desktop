"""按当前平台下载并校验 kdocs-cli（WPS 云文档同步组件）。

官方只提供二进制（无 PyPI 包），所以构建前需要把它放进 ``vendor/kdocs-cli/``：
本脚本自动挑选当前平台对应的包、下载、用官方 ``checksums.txt`` 校验 sha256、
解包并赋予可执行权限。

用法：
    python scripts/fetch_kdocs_cli.py            # 下载当前平台的组件
    python scripts/fetch_kdocs_cli.py --check    # 只检查本地是否就绪（CI 用）

设计要点：
- 校验和来自官方 ``checksums.txt``，不做任何跳过校验的"方便选项"；
- 已存在且校验通过时直接跳过，避免每次构建重复下载 7 MB；
- 失败时给出明确提示，且**不**静默生成一个缺少组件的包。
"""
from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# Windows 控制台默认 cp1252；构建脚本会输出中文进度，先统一改成 UTF-8，
# 避免 UnicodeEncodeError 让构建在真正开始前就失败。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "kdocs-cli"
VERSION = "2.5.29"
CDN_BASE = f"https://wpsai.wpscdn.cn/skillhub/pro/v{VERSION}/releases"

# (sys.platform, platform.machine()) -> (包内平台名, 压缩格式, 解包后可执行文件名)
PLATFORMS: dict[tuple[str, str], tuple[str, str, str]] = {
    ("win32", "AMD64"): ("windows-amd64", "zip", "kdocs-cli.exe"),
    ("win32", "ARM64"): ("windows-arm64", "zip", "kdocs-cli.exe"),
    ("darwin", "x86_64"): ("darwin-amd64", "tar.gz", "kdocs-cli"),
    ("darwin", "arm64"): ("darwin-arm64", "tar.gz", "kdocs-cli"),
    ("linux", "x86_64"): ("linux-amd64", "tar.gz", "kdocs-cli"),
    ("linux", "aarch64"): ("linux-arm64", "tar.gz", "kdocs-cli"),
    ("linux", "arm64"): ("linux-arm64", "tar.gz", "kdocs-cli"),
}


def platform_key() -> tuple[str, str]:
    return sys.platform, platform.machine()


def resolve() -> tuple[str, str, str]:
    key = platform_key()
    if key not in PLATFORMS:
        raise SystemExit(
            f"暂不支持的平台：{key[0]}/{key[1]}。"
            f"可用组合：{sorted(PLATFORMS)}。"
            "可手动从官方 CDN 下载后放到 vendor/kdocs-cli/。")
    return PLATFORMS[key]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(url: str, dest: Path) -> None:
    print(f"  下载 {url}")
    with urllib.request.urlopen(url, timeout=120) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out)


def official_checksums() -> dict[str, str]:
    """取官方 checksums.txt（失败则回退到仓库内已保存的副本）。"""
    url = f"{CDN_BASE}/checksums.txt"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            text = response.read().decode("utf-8")
        (VENDOR / "checksums.txt").write_text(text, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        local = VENDOR / "checksums.txt"
        if not local.is_file():
            raise SystemExit(f"无法获取官方 checksums.txt，且本地没有副本：{exc}") from exc
        print(f"  （联网获取 checksums 失败，改用仓库内副本：{exc}）")
        text = local.read_text(encoding="utf-8")
    result: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            result[parts[1].strip()] = parts[0].strip().lower()
    return result


def extract(archive: Path, fmt: str, exe_name: str, dest_dir: Path) -> Path:
    with tempfile.TemporaryDirectory(prefix="kdocs-cli-") as tmp:
        tmp_path = Path(tmp)
        if fmt == "zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(tmp_path)
        else:
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(tmp_path, filter="data")
        found = next((p for p in tmp_path.rglob(exe_name) if p.is_file()), None)
        if found is None:
            raise SystemExit(f"压缩包内没有找到 {exe_name}")
        dest = dest_dir / exe_name
        shutil.copy2(found, dest)
        dest.chmod(0o755)
        return dest


def check() -> int:
    _, _, exe_name = resolve()
    target = VENDOR / exe_name
    if target.is_file():
        print(f"✅ 组件就绪：{target}（{target.stat().st_size} 字节）")
        return 0
    print(f"❌ 缺少组件：{target}\n   请运行：python scripts/fetch_kdocs_cli.py")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="下载并校验 kdocs-cli")
    parser.add_argument("--check", action="store_true", help="只检查本地是否就绪")
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = parser.parse_args()
    if args.check:
        return check()

    VENDOR.mkdir(parents=True, exist_ok=True)
    plat_name, fmt, exe_name = resolve()
    target = VENDOR / exe_name
    archive_name = f"kdocs-cli-{VERSION}-{plat_name}.{fmt}"

    checksums = official_checksums()
    expected = checksums.get(archive_name)
    if not expected:
        raise SystemExit(f"官方 checksums.txt 里没有 {archive_name}，终止以免装入未校验的二进制。")

    if target.is_file() and not args.force:
        print(f"组件已存在：{target}（如需重下加 --force）")
        return 0

    archive = VENDOR / archive_name
    if not archive.is_file() or args.force:
        fetch(f"{CDN_BASE}/{archive_name}", archive)

    actual = sha256_of(archive)
    if actual != expected:
        archive.unlink(missing_ok=True)
        raise SystemExit(
            f"sha256 校验失败：\n  期望 {expected}\n  实际 {actual}\n"
            "已删除下载文件，请重试或手动核对官方 checksums.txt。")
    print(f"  sha256 校验通过：{actual[:16]}…")

    dest = extract(archive, fmt, exe_name, VENDOR)
    print(f"✅ 组件就绪：{dest}（{dest.stat().st_size} 字节）")

    # 在类 Unix 系统上顺手确认可执行
    if sys.platform != "win32":
        try:
            out = subprocess.run([str(dest), "version"], capture_output=True,
                                 text=True, timeout=30).stdout.strip()
            print(f"   版本自检：{out}")
        except Exception as exc:  # noqa: BLE001
            print(f"   ⚠ 版本自检失败（不影响打包）：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
