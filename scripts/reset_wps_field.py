"""把「试验田」重置回基线存档（开发/调试用）。

背景（见 design/WPS-CLOUD-SYNC-PLAN.md）：
- **试验田** = 程序写入目标（`DEFAULT_WPS_TABLES`），可以随便试错；
- **验收标准** = 正式表（`DEFAULT_WPS_PRODUCTION_TABLES`）；
- **基线存档** = `基准-<子表名>.xlsx`（**只读**，永不改动）。

重置做法：从存档**复制出新的**试验田副本（存档本身不动，可无限次重置），
并把新 file_id 写回 `app/config.py`。

用法：
    python scripts/reset_wps_field.py            # 只预览（默认）
    python scripts/reset_wps_field.py --apply    # 真正执行
    python scripts/reset_wps_field.py --apply --rebuild   # 顺带重新打包
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import AppConfig  # noqa: E402
from app.wps_cloud import KdocsCli, WpsCloudError  # noqa: E402

ARCHIVE_FILE = ROOT / "design" / "WPS基线存档.json"
CONFIG_PY = ROOT / "app" / "config.py"
DEVICE = "757726038"
STAMP = datetime.datetime.now().strftime("%m%d-%H%M")


def call(cli: KdocsCli, *args: str) -> dict:
    proc = subprocess.run([cli.path, *args], capture_output=True, text=True, timeout=180)
    raw = proc.stdout.strip()
    if not raw.startswith("{"):
        raise WpsCloudError(f"kdocs-cli 无有效输出：{(proc.stderr or raw)[:200]}")
    payload = json.JSONDecoder().raw_decode(raw)[0]
    if payload.get("code") not in (0, None):
        raise WpsCloudError(f"接口返回 code={payload.get('code')}：{payload.get('message')}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="把试验田重置回基线存档")
    parser.add_argument("--apply", action="store_true", help="真正执行（默认只预览）")
    parser.add_argument("--rebuild", action="store_true", help="执行后重新打包")
    parser.add_argument("--sheets", help="只重置指定子表，逗号分隔（默认全部）")
    args = parser.parse_args()

    if not ARCHIVE_FILE.is_file():
        raise SystemExit(f"找不到基线存档清单：{ARCHIVE_FILE}（先建存档再重置）")
    archive: dict[str, str] = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
    cfg = AppConfig()
    cli = KdocsCli()
    targets = list(cfg.wps_tables)
    if args.sheets:
        wanted = {s.strip() for s in args.sheets.split(",") if s.strip()}
        targets = [s for s in targets if s in wanted]

    print("重置计划（试验田 -> 基线存档）：")
    print(f"{'子表':<10}{'当前试验田':>16}{'基线存档':>16}")
    for sheet in targets:
        cur = cfg.wps_tables.get(sheet, {}).get("file_id", "")
        arc = archive.get(sheet, "(缺)")
        print(f"{sheet:<10}{cur[:14] + '…':>16}{str(arc)[:14] + '…':>16}")
    if not args.apply:
        print("\n（预览模式；加 --apply 才会真正执行）")
        return 0

    new_ids: dict[str, str] = {}
    for sheet in targets:
        arc = archive.get(sheet)
        if not arc:
            print(f"  ⚠ {sheet} 没有存档，跳过")
            continue
        resp = call(cli, "drive", "copy-file", json.dumps(
            {"file_id": arc, "dst_drive_id": DEVICE, "dst_parent_id": "0"}))
        fid = (resp.get("data") or {}).get("data", {}).get("id")
        if not fid:
            print(f"  ⚠ {sheet} 复制失败")
            continue
        call(cli, "drive", "rename-file", json.dumps(
            {"file_id": fid, "dst_name": f"试验田-{sheet}-{STAMP}.xlsx"}))
        new_ids[sheet] = fid
        print(f"  ✅ {sheet:<8} 新试验田 {fid}")
        time.sleep(1.2)

    if not new_ids:
        raise SystemExit("没有成功创建任何新试验田，未修改配置")

    text = CONFIG_PY.read_text(encoding="utf-8")
    match = re.search(r"DEFAULT_WPS_TABLES: Dict\[str, Dict\[str, str\]\] = \{.*?\n\}",
                      text, re.S)
    block = match.group(0)
    for sheet, fid in new_ids.items():
        block = re.sub(rf'("{sheet}": \{{"file_id": ")[^"]+("}})', rf'\g<1>{fid}\g<2>', block)
    CONFIG_PY.write_text(text[:match.start()] + block + text[match.end():], encoding="utf-8")
    print(f"\n已更新 {CONFIG_PY}")

    # 用户配置里若存了 wps_tables，会覆盖代码里的新默认值（踩过这个坑：
    # 重置后程序仍写旧副本）。这里一并清掉，让它跟随代码默认值。
    import json as _json
    user_cfg = Path.home() / ".config" / "yikou-light-food" / "config.json"
    if user_cfg.is_file():
        try:
            data = _json.loads(user_cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and ("wps_tables" in data
                                       or "wps_production_tables" in data):
            data.pop("wps_tables", None)
            data.pop("wps_production_tables", None)
            backup = user_cfg.with_name(user_cfg.name + ".bak_reset")
            backup.write_text(user_cfg.read_text(encoding="utf-8"), encoding="utf-8")
            user_cfg.write_text(_json.dumps(data, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            print(f"已清除用户配置里过期的 wps_tables（原文件备份为 {backup.name}）")

    if args.rebuild:
        print("重新打包中（约 1 分钟）…")
        subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
             "yikou-light-food.spec"],
            cwd=ROOT, check=True,
            env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": "0"},
            stdout=subprocess.DEVNULL)
        print("打包完成：dist/yikou-light-food（记得替换到运行目录）")
    else:
        print("提示：打包版程序需要重新构建（或加 --rebuild）才会用上新试验田。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
