"""Application configuration persistence (non-sensitive values only)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional


APP_NAME = "yikou-light-food"
MIN_SPLIT_RATIO = 0.30
MAX_SPLIT_RATIO = 0.55
_CONFIG_SAVE_LOCK = threading.RLock()

# 本地排单子表 -> 云端排单表（WPS 云文档 file_id）。
# 表名映射来自用户确认：本地「衣锦」= 云端「校门口」（云端表标题写的是「衣锦汇总」）。
# 允许修改：协作者重建当月表后 file_id 会变，可在界面上更新。
DEFAULT_WPS_PRODUCTION_TABLES: Dict[str, Dict[str, str]] = {
    "东湖中餐": {"file_id": "fr2FrpFVMrM8poHaSHFK1xAsmSSBMspye"},
    "衣锦中餐": {"file_id": "amqzgcXVMrMWopCdyxkJrxnqzcQ8UbaJG"},
    "医学院中餐": {"file_id": "qvuPzdurK1MyKwL2weiZ1xF8RzdijFdaJ"},
    "东湖晚餐": {"file_id": "noJa93Xq9xM62hfVqj6UrxEmgLjAHmqka"},
    "衣锦晚餐": {"file_id": "mAc5hDw1q1MV33kW8JE7xxnnW1KEdYc1V"},
    "医学院晚餐": {"file_id": "PpnGwh9ERxMRByaRp2EHrxUgCDjP4kZpA"},
}

# 当前生效的默认目标是正式云表。测试时必须显式配置本轮新建的副本。
DEFAULT_WPS_TABLES: Dict[str, Dict[str, str]] = {
    sheet: dict(conf) for sheet, conf in DEFAULT_WPS_PRODUCTION_TABLES.items()
}


# 云端表「列B（地址）」的规定顺序（用户 2026-09-15 口述）。
# 列表里没有的地址一律排到表格最后面；**空列表 = 按地址自然升序**（医学院用这种）。
DEFAULT_ADDRESS_ORDER: Dict[str, list] = {
    "东湖中餐": (["小", "大西"]
               + [f"A{i}" for i in range(1, 7)]
               + [f"b{i}" for i in range(1, 13)]
               + [f"C{i}" for i in range(1, 13)]
               + [f"D{i}" for i in range(1, 13)]),
    "东湖晚餐": (["小", "大西"]
               + [f"A{i}" for i in range(1, 7)]
               + [f"b{i}" for i in range(1, 13)]
               + [f"C{i}" for i in range(1, 13)]
               + [f"D{i}" for i in range(1, 13)]),
    "衣锦中餐": ["外卖柜", "校门口"],
    "衣锦晚餐": ["外卖柜", "校门口"],
    "医学院中餐": [],
    "医学院晚餐": [],
}


def default_wps_production_tables() -> Dict[str, Dict[str, str]]:
    """正式排单表的 file_id（当前暂停使用，仅作切回备份）。"""
    return {sheet: dict(conf) for sheet, conf in DEFAULT_WPS_PRODUCTION_TABLES.items()}


def default_wps_address_order() -> Dict[str, list]:
    """返回地址顺序默认值的副本（避免多个实例共享同一 list）。"""
    return {sheet: list(order) for sheet, order in DEFAULT_ADDRESS_ORDER.items()}


def normalize_wps_address_order(value: Any,
                                base: Optional[Dict[str, Any]] = None
                                ) -> Dict[str, list]:
    """规整「地址排序清单」：{子表名: [地址, ...]}。

    规则与 normalize_wps_tables 一致：以 ``base``（默认=出厂默认）为底，
    只覆盖传入值里出现的子表；**显式传空列表是有效值**（= 该表按地址升序），
    因此不能用"空即忽略"的写法，必须按键判断。
    """
    result = {sheet: list(order) for sheet, order in (base or default_wps_address_order()).items()}
    if not isinstance(value, dict):
        return result
    for sheet, order in value.items():
        if not isinstance(sheet, str) or not sheet.strip():
            continue
        name = sheet.strip()
        if isinstance(order, str):
            items = order.splitlines()
        elif isinstance(order, (list, tuple)):
            items = list(order)
        else:
            continue
        result[name] = [str(item).strip() for item in items if str(item or "").strip()]
    return result


def default_wps_test_tables() -> Dict[str, str]:
    """测试副本映射；默认为空，避免回退到过期副本。"""
    return {}


def normalize_wps_test_tables(value: Any) -> Dict[str, str]:
    """规整显式测试表映射，不从正式表或历史副本继承。"""
    result = default_wps_test_tables()
    if isinstance(value, dict):
        for sheet, file_id in value.items():
            if isinstance(sheet, str) and sheet.strip() and str(file_id or "").strip():
                result[sheet.strip()] = str(file_id).strip()
    return result


def normalize_wps_production_tables(value: Any) -> Dict[str, Dict[str, str]]:
    """规整"正式表备份"映射：以 DEFAULT_WPS_PRODUCTION_TABLES 为底。

    必须用独立的底表 —— 否则会被"当前生效表（测试副本）"覆盖，
    备份就失去意义（曾经踩过这个坑）。
    """
    return normalize_wps_tables(value, base=default_wps_production_tables())


def default_wps_tables() -> Dict[str, Dict[str, str]]:
    """返回默认表映射的副本（避免多个实例共享同一 dict）。"""
    return {sheet: dict(conf) for sheet, conf in DEFAULT_WPS_TABLES.items()}


def normalize_wps_tables(value: Any,
                         base: Optional[Dict[str, Dict[str, str]]] = None
                         ) -> Dict[str, Dict[str, str]]:
    """把用户/磁盘上的表映射规整成 {子表名: {"file_id": str, "drive_id": str}}。

    规则：以 ``base``（默认=当前生效表）为底，用传入值覆盖 file_id（非空才覆盖），
    保证旧配置升级后仍能拿到新增子表的默认值；无法解析的输入直接退回默认值。
    """
    result = {k: dict(v) for k, v in (base or default_wps_tables()).items()}
    if not isinstance(value, dict):
        return result
    for sheet, conf in value.items():
        if not isinstance(sheet, str) or not sheet.strip():
            continue
        entry = result.setdefault(sheet.strip(), {"file_id": ""})
        if isinstance(conf, str):
            if conf.strip():
                entry["file_id"] = conf.strip()
        elif isinstance(conf, dict):
            fid = str(conf.get("file_id") or "").strip()
            if fid:
                entry["file_id"] = fid
            drive = str(conf.get("drive_id") or "").strip()
            if drive:
                entry["drive_id"] = drive
    # 去掉没有 file_id 的条目（未配置的表不参与同步）
    return {sheet: conf for sheet, conf in result.items() if conf.get("file_id")}


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
    # 已弃用：自动化固定使用随包分发的内置 Chromium，不再按此选择系统
    # 浏览器。字段保留只为兼容既有 config.json，不会影响运行行为。
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
    # 名单来源：wps = 每次下单前从 WPS 云端读当天标 1 的人（东湖午餐/东湖晚餐）；
    # excel = 旧行为，读《闪时送.xlsx》。云端模式下标 1 名单仍会写一份到该 Excel 留档。
    sss_order_source: str = "wps"
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
    # 预检模式：登录并检查门店、地址、余额、订单列表，但不提交订单。
    sss_preflight: bool = False
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
    # ---- WPS 云文档同步（见 design/WPS-CLOUD-SYNC-PLAN.md）----
    # 把本地排单表的内容增量写入云端排单表；单元格级写入，不整表覆盖。
    wps_enabled: bool = False
    # 测试模式必须显式配置本轮新建副本；不会自动使用正式表或历史副本。
    wps_test_mode: bool = True
    wps_test_file_id: str = "anUpCyxwE1MZcZc7KKpS1x625UG5Qwtg3"
    wps_test_drive_id: str = "757726038"
    wps_test_tables: Dict[str, Any] = field(default_factory=default_wps_test_tables)
    # 正式排单表 ID 备份（当前暂停使用，切换回去时用得到）。
    wps_production_tables: Dict[str, Any] = field(default_factory=default_wps_production_tables)
    wps_drive_id: str = "757726038"
    # 留空 = 自动查找内置 / 系统 PATH 里的 kdocs-cli。
    wps_cli_path: str = ""
    wps_tables: Dict[str, Any] = field(default_factory=default_wps_tables)
    # 目标日期顺延窗口：[start, 24) ∪ [0, end) 内运行则写入"运行日 + 1 天"。
    wps_target_hour_start: int = 20
    wps_target_hour_end: int = 10
    # 是否写协作者通讯记号（备注列右侧第 2 列的周几数字）；测试模式下一律不写。
    wps_marker_enabled: bool = True
    # 新增客户时是否按「列B 地址顺序」重排整张表（见 DEFAULT_ADDRESS_ORDER）。
    wps_sort_enabled: bool = True
    # {子表名: [地址, ...]}；空列表 = 该表按地址自然升序。
    wps_address_order: Dict[str, Any] = field(default_factory=default_wps_address_order)
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
                 sss_order_source: str = "wps",
                 sss_product_name: str = "轻食",
                 sss_common_address: str = "嗯哼",
                 sss_store_name: str = "一口轻食",
                 sss_use_fixed_address: bool = True,
                 sss_fixed_lnt: float = 119.728224,
                 sss_fixed_lat: float = 30.256632,
                 sss_fixed_area_code: str = "330110",
                 sss_fixed_address_detail: str = "浙江农林大学东湖校区",
                 sss_dry_run: bool = True,
                 sss_preflight: bool = False,
                 sss_store_id: int | None = None,
                 sss_store_name_cached: str = "",
                 sss_max_workers: int = 4,
                 sss_read_timeout_s: float = 20.0,
                 sss_unit_price: float = 1.9,
                 sss_idempotency_field: str = "",
                 wps_enabled: bool = False,
                 wps_test_mode: bool = True,
                 wps_test_file_id: str = "anUpCyxwE1MZcZc7KKpS1x625UG5Qwtg3",
                 wps_test_drive_id: str = "757726038",
                 wps_test_tables: Optional[Dict[str, Any]] = None,
                 wps_production_tables: Optional[Dict[str, Any]] = None,
                 wps_drive_id: str = "757726038",
                 wps_cli_path: str = "",
                 wps_tables: Optional[Dict[str, Any]] = None,
                 wps_target_hour_start: int = 20,
                 wps_target_hour_end: int = 10,
                 wps_marker_enabled: bool = True,
                 wps_sort_enabled: bool = True,
                 wps_address_order: Optional[Dict[str, Any]] = None,
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
        # 只接受 wps / excel 两个取值；其它（含旧配置缺字段）一律按云端模式。
        source = str(sss_order_source or "").strip().lower()
        self.sss_order_source = "excel" if source == "excel" else "wps"
        self.sss_product_name = sss_product_name or "轻食"
        self.sss_common_address = sss_common_address or "嗯哼"
        self.sss_store_name = sss_store_name or "一口轻食"
        self.sss_use_fixed_address = bool(sss_use_fixed_address)
        self.sss_fixed_lnt = float(sss_fixed_lnt)
        self.sss_fixed_lat = float(sss_fixed_lat)
        self.sss_fixed_area_code = sss_fixed_area_code or "330110"
        self.sss_fixed_address_detail = sss_fixed_address_detail or "浙江农林大学东湖校区"
        self.sss_dry_run = bool(sss_dry_run)
        self.sss_preflight = bool(sss_preflight)
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
        # ---- WPS 云文档同步 ----
        self.wps_enabled = bool(wps_enabled)
        self.wps_test_mode = bool(wps_test_mode)
        self.wps_test_file_id = str(wps_test_file_id or "").strip()
        self.wps_test_drive_id = str(wps_test_drive_id or "").strip()
        self.wps_test_tables = normalize_wps_test_tables(wps_test_tables)
        self.wps_production_tables = normalize_wps_production_tables(wps_production_tables)
        self.wps_drive_id = str(wps_drive_id or "").strip()
        self.wps_cli_path = str(wps_cli_path or "").strip()
        self.wps_tables = normalize_wps_tables(wps_tables)
        try:
            start = int(wps_target_hour_start)
        except (TypeError, ValueError):
            start = 20
        try:
            end = int(wps_target_hour_end)
        except (TypeError, ValueError):
            end = 10
        self.wps_target_hour_start = max(0, min(23, start))
        self.wps_target_hour_end = max(0, min(23, end))
        self.wps_marker_enabled = bool(wps_marker_enabled)
        self.wps_sort_enabled = bool(wps_sort_enabled)
        self.wps_address_order = normalize_wps_address_order(wps_address_order)
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
