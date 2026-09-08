"""Persistent user-maintained aliases and pending address reports."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .config import user_data_dir
except ImportError:  # pragma: no cover - direct module execution compatibility
    from config import user_data_dir


def aliases_path() -> Path:
    return user_data_dir() / "address_aliases.json"


def pending_path() -> Path:
    return user_data_dir() / "pending_addresses.json"


def load_aliases(path: Path | None = None) -> dict[str, str]:
    """Load the editable alias map, creating an empty file on first use."""
    target = path or aliases_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        _atomic_write(target, {})
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"地址别名文件无效：{target}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"地址别名文件根节点必须是对象：{target}")
    result: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip():
            raise ValueError(f"地址别名文件包含空键或非字符串值：{target}")
        result[key.strip()] = value.strip()
    return result


def write_pending(items: list[dict[str, Any]], *, target_date: Any,
                  path: Path | None = None) -> Path:
    """Atomically replace the current run's pending-address report."""
    target = path or pending_path()
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "target_date": str(target_date),
        "items": items,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, payload)
    return target


def _atomic_write(target: Path, payload: Any) -> None:
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


__all__ = ["aliases_path", "pending_path", "load_aliases", "write_pending"]
