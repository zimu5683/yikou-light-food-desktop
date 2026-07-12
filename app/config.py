"""Application configuration persistence (non-sensitive values only)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Dict, Optional


APP_NAME = "yikou-light-food"


def user_data_dir() -> Path:
    """Return a per-user writable directory, independent of the repository."""
    if os.name == "nt":
        root = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    elif os.sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return Path(root) / APP_NAME


@dataclass(init=False)
class AppConfig:
    target_url: str = "https://m.icall.me/admin/#/login"
    phone_number: str = ""
    excel_path: str = ""
    browser_mode: str = "auto"  # auto, msedge, chromium
    headless: bool = False
    max_page_search: int = 20
    element_timeout_ms: int = 8000
    network_idle_timeout_ms: int = 5000
    # The order table is rendered asynchronously after navigation/back.
    order_search_timeout_ms: int = 8000
    retry_wait_ms: int = 1000
    order_search_attempts: int = 3
    config_path: Optional[str] = None

    def __init__(self, target_url: str = "https://m.icall.me/admin/#/login", phone_number: str = "",
                 excel_path: str | os.PathLike[str] = "", browser_mode: str = "auto", headless: bool = False,
                 max_page_search: int = 20, element_timeout_ms: int = 8000,
                 network_idle_timeout_ms: int = 5000, order_search_timeout_ms: int = 8000,
                 retry_wait_ms: int = 1000, order_search_attempts: int = 3,
                 config_path: Optional[str] = None,
                 *, url: Optional[str] = None, phone: Optional[str] = None,
                 browser: Optional[str] = None) -> None:
        # url/phone/browser are compatibility aliases used by the GUI.
        self.target_url = url if url is not None else target_url
        self.phone_number = phone if phone is not None else phone_number
        self.excel_path = Path(excel_path) if excel_path else Path("")
        self.browser_mode = browser if browser is not None else browser_mode
        self.headless = headless
        self.max_page_search = max_page_search
        self.element_timeout_ms = element_timeout_ms
        self.network_idle_timeout_ms = network_idle_timeout_ms
        self.order_search_timeout_ms = order_search_timeout_ms
        self.retry_wait_ms = retry_wait_ms
        self.order_search_attempts = order_search_attempts
        self.config_path = config_path

    @property
    def url(self) -> str:
        return self.target_url

    @url.setter
    def url(self, value: str) -> None:
        self.target_url = value

    @property
    def phone(self) -> str:
        return self.phone_number

    @phone.setter
    def phone(self, value: str) -> None:
        self.phone_number = value

    @property
    def browser(self) -> str:
        return self.browser_mode

    @browser.setter
    def browser(self, value: str) -> None:
        self.browser_mode = value

    @classmethod
    def default_path(cls) -> Path:
        return user_data_dir() / "config.json"

    @classmethod
    def load(cls, path: Optional[os.PathLike[str] | str] = None) -> "AppConfig":
        target = Path(path) if path else cls.default_path()
        if not target.exists():
            return cls(config_path=str(target))
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            valid = {f.name for f in fields(cls)}
            values = {k: v for k, v in payload.items() if k in valid and k != "config_path"}
            return cls(**values, config_path=str(target))
        except (OSError, ValueError, TypeError):
            # A malformed config should not prevent the application starting.
            return cls(config_path=str(target))

    def save(self, path: Optional[os.PathLike[str] | str] = None) -> Path:
        target = Path(path or self.config_path or self.default_path())
        target.parent.mkdir(parents=True, exist_ok=True)
        payload: Dict[str, Any] = asdict(self)
        if isinstance(payload.get("excel_path"), Path):
            payload["excel_path"] = str(payload["excel_path"])
        payload.pop("config_path", None)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.config_path = str(target)
        return target


def load_config(path: Optional[os.PathLike[str] | str] = None) -> AppConfig:
    return AppConfig.load(path)


def save_config(config: AppConfig, path: Optional[os.PathLike[str] | str] = None) -> Path:
    return config.save(path)


def load_config(path: Optional[os.PathLike[str] | str] = None) -> AppConfig:
    return AppConfig.load(path)


def save_config(config: AppConfig, path: Optional[os.PathLike[str] | str] = None) -> Path:
    return config.save(path)
