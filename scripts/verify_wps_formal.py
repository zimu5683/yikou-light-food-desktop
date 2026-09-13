"""Read formal WPS tables and create isolated copies for safe verification."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import DEFAULT_WPS_PRODUCTION_TABLES  # noqa: E402
from app.wps_cloud import KdocsCli, WpsCloudError, scan_bounds  # noqa: E402

DEVICE = "757726038"


def cli_call(cli: KdocsCli, service: str, action: str, params: dict) -> dict:
    proc = subprocess.run([cli.path, service, action, json.dumps(params, ensure_ascii=False)],
                          capture_output=True, text=True, timeout=180, check=False)
    raw = proc.stdout.strip()
    try:
        payload = json.JSONDecoder().raw_decode(raw)[0]
    except (ValueError, TypeError) as exc:
        raise WpsCloudError(f"kdocs-cli 输出无效：{(proc.stderr or raw)[:300]}") from exc
    if payload.get("code") not in (0, None):
        raise WpsCloudError(f"接口返回 code={payload.get('code')}：{payload.get('message')}")
    return payload


def snapshot(cli: KdocsCli, output: Path) -> None:
    result = {"captured_at": dt.datetime.now().isoformat(timespec="seconds"), "tables": {}}
    for sheet, conf in DEFAULT_WPS_PRODUCTION_TABLES.items():
        file_id = conf["file_id"]
        infos = cli.sheets_info(file_id)
        if not infos:
            raise WpsCloudError(f"正式表不可读：{sheet}")
        info = infos[0]
        row_to, col_to = scan_bounds(info)
        grid = cli.read_grid(file_id, int(info.get("sheetId") or 1), 0, row_to, 0, col_to)
        headers = {str(col + 1): value for (row, col), value in grid.items() if row == 1}
        people = sum(1 for (row, col), value in grid.items()
                     if row >= 2 and col == 0 and str(value).strip())
        result["tables"][sheet] = {
            "file_id": file_id,
            "worksheet": info.get("sheetName", ""),
            "row_to": row_to,
            "col_to": col_to,
            "people": people,
            "headers": headers,
        }
        print(f"只读正式表：{sheet} file_id={file_id} rows={people} target={headers.get('9')}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"正式表快照：{output}")


def copy_table(cli: KdocsCli, sheet: str, output_name: str) -> str:
    source = DEFAULT_WPS_PRODUCTION_TABLES[sheet]["file_id"]
    response = cli_call(cli, "drive", "copy-file", {
        "drive_id": DEVICE, "file_id": source,
        "dst_drive_id": DEVICE, "dst_parent_id": "0"})
    data = response.get("data") or {}
    file_id = (data.get("data") or {}).get("id") or data.get("id")
    if not file_id:
        raise WpsCloudError(f"复制正式表失败：{sheet}")
    cli_call(cli, "drive", "rename-file", {"file_id": file_id, "dst_name": output_name})
    print(f"测试副本：{sheet} source={source} copy={file_id} name={output_name}")
    return str(file_id)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path,
                        default=ROOT / "design" / "WPS正式表快照-20260913.json")
    parser.add_argument("--copy-sheet", default="东湖中餐")
    parser.add_argument("--copy-name", default="测试副本-东湖中餐-20260913-正式表验证.xlsx")
    parser.add_argument("--no-copy", action="store_true")
    args = parser.parse_args()
    cli = KdocsCli()
    if not cli.authenticated():
        raise SystemExit("kdocs-cli 未授权")
    snapshot(cli, args.snapshot)
    if not args.no_copy:
        copy_table(cli, args.copy_sheet, args.copy_name)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WpsCloudError as exc:
        print(f"验证失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
