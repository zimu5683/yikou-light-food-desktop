"""Application configuration persistence (non-sensitive values only)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Dict, Optional


APP_NAME = "yikou-light-food"
MIN_SPLIT_RATIO = 0.30
MAX_SPLIT_RATIO = 0.55
_CONFIG_SAVE_LOCK = threading.RLock()


def clamp_split_ratio(value: object) -> float:
    """Keep the user-controlled pane ratio inside a usable range."""
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        ratio = 0.38
    if ratio != ratio:  # NaN
        ratio = 0.38
    return max(MIN_SPLIT_RATIO, min(MAX_SPLIT_RATIO, ratio))


def user_data_dir() -> Path:
    """Return a per-user writable directory, independent of the repository."""
    if os.name == "nt":
        root = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return Path(root) / APP_NAME


@dataclass(init=False)
class AppConfig:
    target_url: str = "https://m.icall.me/admin/#/login"
    phone_number: str = ""
    excel_path: Path | None = None
    browser_mode: str = "auto"  # auto, msedge, chromium
    headless: bool = False
    # 默认使用纯接口模式（不启动浏览器）；False 时退回 Playwright 浏览器模式。
    api_mode: bool = True
    max_page_search: int = 20
    element_timeout_ms: int = 8000
    network_idle_timeout_ms: int = 5000
    # The order table is rendered asynchronously after navigation/back.
    order_search_timeout_ms: int = 8000
    retry_wait_ms: int = 1000
    order_search_attempts: int = 3
    # Empty means "today". The GUI validates the YYYY-MM-DD form before a run.
    order_date: str = ""
    # 待处理订单数；None 表示「留空 = 处理全部」（与表单语义一致，随配置持久化）。
    order_count: int | None = None
    split_ratio: float = 0.38
    # 闪时送（sss）下单任务的独立配置，与管理后台订单处理互不影响。
    sss_url: str = "https://sssplusnew.zhuopaikeji.com/takeout"
    sss_account: str = ""
    sss_excel_path: Path | None = None
    sss_product_name: str = "轻食"
    sss_common_address: str = "嗯哼"
    sss_store_name: str = "一口轻食"
    # 固定地址模式：跳过常用地址匹配，直接用以下坐标/地区/详细地址下单。
    sss_use_fixed_address: bool = True
    sss_fixed_lnt: float = 119.728224
    sss_fixed_lat: float = 30.256632
    sss_fixed_area_code: str = "330110"
    sss_fixed_address_detail: str = "浙江农林大学东湖校区"
    # 干跑模式：只组装并打印下单报文，不真实提交（用于验证流程）。
    sss_dry_run: bool = True
    # 门店 id 缓存：按门店名命中后跳过门店列表查询（门店几乎不变）。
    sss_store_id: int | None = None
    sss_store_name_cached: str = ""
    # 批量下单并发 worker 数（1 = 串行，用于服务端限流时回退）。
    sss_max_workers: int = 4
    # 闪时送 API 读取超时（秒）；与浏览器元素超时独立，慢响应不会误判失败。
    sss_read_timeout_s: float = 20.0
    # 闪时送单均价（元）：>0 时只提示预计送完结算费用，不拦截下单。
    sss_unit_price: float = 1.9
    # 可选：平台若支持客户端幂等字段，填字段名（如 clientRequestId）后启用稳定 UUID。
    # 默认空 = 抓包确认的平台 schema 没有该字段，采用至少一次提交+对账确认。
    sss_idempotency_field: str = ""
    config_path: Optional[str] = None

    def __init__(self, target_url: str = "https://m.icall.me/admin/#/login", phone_number: str = "",
                 excel_path: str | os.PathLike[str] = "", browser_mode: str = "auto", headless: bool = False, api_mode: bool = True,
                 max_page_search: int = 20, element_timeout_ms: int = 8000,
                 network_idle_timeout_ms: int = 5000, order_search_timeout_ms: int = 8000,
                 retry_wait_ms: int = 1000, order_search_attempts: int = 3,
                 order_date: str = "",
                 order_count: int | None = None,
                 split_ratio: float = 0.38,
                 sss_url: str = "https://sssplusnew.zhuopaikeji.com/takeout",
                 sss_account: str = "",
                 sss_excel_path: str | os.PathLike[str] = "",
                 sss_product_name: str = "轻食",
                 sss_common_address: str = "嗯哼",
                 sss_store_name: str = "一口轻食",
                 sss_use_fixed_address: bool = True,
                 sss_fixed_lnt: float = 119.728224,
                 sss_fixed_lat: float = 30.256632,
                 sss_fixed_area_code: str = "330110",
                 sss_fixed_address_detail: str = "浙江农林大学东湖校区",
                 sss_dry_run: bool = True,
                 sss_store_id: int | None = None,
                 sss_store_name_cached: str = "",
                 sss_max_workers: int = 4,
                 sss_read_timeout_s: float = 20.0,
                 sss_unit_price: float = 1.9,
                 sss_idempotency_field: str = "",
                 config_path: Optional[str] = None,
                 *, url: Optional[str] = None, phone: Optional[str] = None,
                 browser: Optional[str] = None) -> None:
        # url/phone/browser are compatibility aliases used by the GUI.
        self.target_url = url if url is not None else target_url
        self.phone_number = phone if phone is not None else phone_number
        # ``Path("")`` resolves to the current directory and used to pass the
        # GUI's existence check on a fresh install.  ``None`` is an unambiguous
        # representation of "no workbook selected".
        self.excel_path = Path(excel_path) if excel_path else None
        self.browser_mode = browser if browser is not None else browser_mode
        self.headless = headless
        self.api_mode = bool(api_mode)
        self.max_page_search = max_page_search
        self.element_timeout_ms = element_timeout_ms
        self.network_idle_timeout_ms = network_idle_timeout_ms
        self.order_search_timeout_ms = order_search_timeout_ms
        self.retry_wait_ms = retry_wait_ms
        self.order_search_attempts = order_search_attempts
        self.order_date = str(order_date or "").strip()
        self.order_count = order_count
        self.split_ratio = clamp_split_ratio(split_ratio)
        self.sss_url = sss_url
        self.sss_account = sss_account
        self.sss_excel_path = Path(sss_excel_path) if sss_excel_path else None
        self.sss_product_name = sss_product_name or "轻食"
        self.sss_common_address = sss_common_address or "嗯哼"
        self.sss_store_name = sss_store_name or "一口轻食"
        self.sss_use_fixed_address = bool(sss_use_fixed_address)
        self.sss_fixed_lnt = float(sss_fixed_lnt)
        self.sss_fixed_lat = float(sss_fixed_lat)
        self.sss_fixed_area_code = sss_fixed_area_code or "330110"
        self.sss_fixed_address_detail = sss_fixed_address_detail or "浙江农林大学东湖校区"
        self.sss_dry_run = bool(sss_dry_run)
        self.sss_store_id = int(sss_store_id) if sss_store_id not in (None, "") else None
        self.sss_store_name_cached = str(sss_store_name_cached or "")
        try:
            workers = int(sss_max_workers)
        except (TypeError, ValueError):
            workers = 4
        self.sss_max_workers = max(1, min(20, workers))
        try:
            read_timeout = float(sss_read_timeout_s)
        except (TypeError, ValueError):
            read_timeout = 20.0
        self.sss_read_timeout_s = max(1.0, min(120.0, read_timeout))
        try:
            self.sss_unit_price = max(0.0, float(sss_unit_price))
        except (TypeError, ValueError):
            self.sss_unit_price = 1.9
        self.sss_idempotency_field = str(sss_idempotency_field or "").strip()
        self.config_path = config_path
        # Snapshot used by ``save`` to distinguish "this instance never touched
        # the field" from "another thread/process wrote a newer value".
        self._baseline: Dict[str, Any] = self._to_payload()
        # Last on-disk payload observed by this instance.  A fresh instance
        # that never loaded a file (``_last_seen_disk is None``) must not merge
        # stale disk values: it is intentionally writing a complete config.
        self._last_seen_disk: Dict[str, Any] | None = None

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
            config = cls(**values, config_path=str(target))
            config._last_seen_disk = dict(payload)
            return config
        except (OSError, ValueError, TypeError):
            # A malformed config must not prevent the application starting,
            # but the GUI saves defaults over it on the next run, so keep a
            # copy the user can still inspect or restore.
            try:
                target.replace(target.with_name(target.name + ".bak"))
            except OSError:
                pass
            return cls(config_path=str(target))

    def _to_payload(self) -> Dict[str, Any]:
        """Return the JSON-serialisable representation of this config."""
        payload: Dict[str, Any] = asdict(self)
        if isinstance(payload.get("excel_path"), Path):
            payload["excel_path"] = str(payload["excel_path"])
        if isinstance(payload.get("sss_excel_path"), Path):
            payload["sss_excel_path"] = str(payload["sss_excel_path"])
        payload.pop("config_path", None)
        return payload

    @staticmethod
    def _read_disk_payload(target: Path) -> Dict[str, Any] | None:
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Best-effort fsync of the parent directory after os.replace.

        Windows does not allow opening a directory as a file; failures are
        intentionally ignored because the data file itself has already been
        flushed and atomically replaced.
        """
        if os.name == "nt":
            return
        try:
            fd = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def save(self, path: Optional[os.PathLike[str] | str] = None) -> Path:
        """Atomically persist config, merging fields untouched by this instance.

        The process may have multiple writers (UI debounce save, worker store
        cache update).  A field whose current value is still equal to the
        value loaded/constructed for this instance is considered untouched, so
        the on-disk value is kept.  Fields changed in memory are written.
        The write itself uses a same-directory temporary file, fsync and
        ``os.replace`` to avoid truncated JSON after a crash.
        """
        target = Path(path or self.config_path or self.default_path())
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self._to_payload()

        with _CONFIG_SAVE_LOCK:
            disk = self._read_disk_payload(target)
            baseline = getattr(self, "_baseline", None)
            last_seen = getattr(self, "_last_seen_disk", None)
            if (isinstance(disk, dict) and isinstance(baseline, dict)
                    and isinstance(last_seen, dict) and disk != last_seen):
                # 另一个实例/线程在本实例上次读盘后写过：对“本实例未改”的
                # 字段采用磁盘值，避免防抖保存把 worker 刚写入的门店缓存覆盖。
                for key, current in list(payload.items()):
                    if key in disk and baseline.get(key, current) == current:
                        payload[key] = disk[key]

            text = json.dumps(payload, ensure_ascii=False, indent=2)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
            )
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                if target.exists():
                    try:
                        shutil.copy2(target, target.with_name(target.name + ".bak"))
                    except OSError:
                        pass
                os.replace(temp_path, target)
                self._fsync_directory(target.parent)
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise

            self._baseline = dict(payload)
            self._last_seen_disk = dict(payload)
            self.config_path = str(target)
            return target


def load_config(path: Optional[os.PathLike[str] | str] = None) -> AppConfig:
    return AppConfig.load(path)


def save_config(config: AppConfig, path: Optional[os.PathLike[str] | str] = None) -> Path:
    return config.save(path)
