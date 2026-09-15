"""WPS 云文档同步：把本地排单表的内容增量写入云端排单表。

设计要点（均来自 2026-09 的真机验证，详见 design/WPS-CLOUD-SYNC-PLAN.md）：

- 通过金山官方 CLI ``kdocs-cli`` 访问云文档，**单元格级读写**，不做整表覆盖，
  因此不会破坏协作者维护的公式、自定义排序、字体与列宽。
- 云端表头不是固定列号（6 张表的目标日期列分别在第 6/88/113/87/110/84 列），
  因此一律**按内容定位**：日期列只比对「月.日」，忽略星期文字（协作者写错过星期）。
- 按【名字 + 电话】双重匹配客户；已存在则累加总餐次，不存在则追加新行。
- 用本地账本记录"每人上次已同步的餐次"，使重复运行零副作用、加餐只补差额。
- 任何失败都只抛异常/写日志，绝不阻塞调用方的本地排单任务。

依赖：仅标准库 + openpyxl（读取本地 xlsx）+ kdocs-cli 可执行文件。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

CLI_NAME = "kdocs-cli"
CLI_NAME_WIN = "kdocs-cli.exe"

# 云端表里可能出现的工作列表头（不同表写法不同，统一按名字找列）
HEADER_NAME = ("名字", "姓名")
HEADER_ADDRESS = ("地址",)
HEADER_PHONE = ("电话",)
HEADER_TYPE = ("类型",)
HEADER_KIND = ("餐种",)
HEADER_TOTAL = ("总餐次", "总餐数")
HEADER_SERVED = ("已出餐", "出餐")
HEADER_LEFT = ("剩余餐", "剩余")
HEADER_REMARK = ("备注",)

# 目标日期格与通讯记号写入的值
CELL_MARK = "1"
# 数据从第 3 行开始（第 1 行标题、第 2 行表头）
FIRST_DATA_ROW = 3
HEADER_ROW = 2
TITLE_ROW = 1
# 通讯记号默认偏移：备注列右边第 3 列。
# （备注+2 实测被协作者的「9.14 周一」日期标记占用，兜底位置必须让开）
MARKER_OFFSET = 3
# 通讯记号的合法数字（周几：周日=1 … 周六=7）。用于在表头行里认出记号位。
MARKER_VALUES = range(1, 8)
# 结构列（除姓名/地址/电话/日期以外的固定列）。
# **日期列只允许出现在「电话列」与「第一个结构列」之间** —— 备注右侧是协作者
# 写日期标记的区域（实测 6 张表都在备注+2），绝不能当成日期列：
# 否则新行的「已出餐」公式会把它统计进去，甚至把目标日期的 1 写进协作者的格子。
STRUCT_COLUMN_KEYS = ("type", "kind", "total", "served", "left", "remark")
# 排序辅助列的列号上限：真实排单表最宽也就 200 多列，异常宽说明 used range 有问题，
# 此时宁可不排序，也不要对几万列的区域发排序请求。
MAX_SORT_COL = 1000
# 排序键零填充宽度：接口可能把数字按文本比较（"10" < "2"），补零后字典序即数值序。
SORT_KEY_WIDTH = 4
# 同一个地址组内：已有行在前（+0），本次新增行在后（+1），空行垫底（+2）。
SORT_FLAG_EXISTING = 0
SORT_FLAG_NEW = 1
SORT_FLAG_BLANK = 2
# 本地排单路线名 -> 云端地址组的写法（协作者习惯，实测 2026-09-12 目标表）。
# 命中别名时：插入到云端组的末尾，且地址格按云端写法落表。
ADDRESS_ALIASES = {"小西": "小"}
# 云端单次读取的行/列上限
MAX_SCAN_ROW = 400
MAX_SCAN_COL = 200
# 接口单次读取的格数上限（实测 5 万）。扫描区域必须按"行×列"控制，
# 否则会撞 `range 选区过大（N 行 × M 列 = X 格）`。留 10% 余量。
MAX_READ_CELLS = 45000

# 单次 update-range-data 允许的最大单元格数（实测上限 100，留余量）。
WRITE_BATCH_CELLS = 80
# 字号 -> twip（1 磅 = 20 twip），接口的 font.dyHeight 用 twip。
FONT_SIZE_TO_TWIP = 20
# 颜色常量（ARGB 整数，接口用整数传色）
FILL_LUXURY = 0xFFFFC000        # 豪华餐整行底色：金黄（与"总餐次"列同色）
FILL_GOLD = 0xFFFFC000          # 总餐次/已出餐/剩余餐 的既有底色
# 接口读回来的对齐是字符串枚举，写回去要整数（alcH/alcV）。
_ALIGN_H = {"haGeneral": 0, "haLeft": 1, "haCenter": 2, "haRight": 3,
            "haFill": 4, "haJustify": 5, "haCenterContinuous": 6}
_ALIGN_V = {"vaTop": 0, "vaCenter": 1, "vaBottom": 2, "vaJustify": 3}

# 金山接口的限流错误码：当日额度用尽 / 短时频繁触发（均次日 08:00 恢复）。
# 金山接口的限流错误码：当日额度用尽 / 短时频繁触发（均次日 08:00 恢复）。
RATE_LIMIT_CODES = {429001, 429002}

# ``build_plan`` 的并发度：每张子表 3 次只读往返，6 张表串行最多 18 次。
# 并发只改变往返的重叠方式，**调用次数与参数完全不变**，不额外消耗每日额度。
#
# 为什么是 2 而不是 6：429002「短时间频繁触发」不在 ``_TRANSIENT_HINTS`` 里，
# ``_run`` 不会重试它，突发触发会让该子表直接报错。取 2 只把瞬时速率翻倍，
# 与 ``bridge.WPS_COPY_CHECK_WORKERS`` 保持同一口径。
WPS_PLAN_WORKERS = 2

DATE_RE = re.compile(r"^\s*(\d{1,2})\s*[.．]\s*(\d{1,2})\s*(?:周|星期|礼拜)?\s*([一二三四五六日天])?")


class WpsCloudError(RuntimeError):
    """云文档同步过程中的可预期错误（调用方据此提示用户）。"""


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------

@dataclass
class CloudOrder:
    """本地排单表里的一行订单。"""

    sheet: str
    name: str
    address: str
    phone: str
    meal_type: str          # 中餐 / 晚餐
    meal_kind: str          # 经济 / 豪华
    meals: int              # 「餐次」列
    row: int = 0            # 本地行号，便于报错定位


@dataclass
class Change:
    """一条待写入云端的变更。"""

    kind: str               # existing / new
    name: str
    phone: str
    row: int                # 云端行号（排序后要写入的行号）
    delta: int              # 总餐次差值（want - before），0 表示已一致
    target_col: int         # 目标日期列（1-based）
    total_before: int = 0
    total_after: int = 0
    target_ok: bool = False  # 目标日期格是否已经是 1
    # 新客户追加行需要一并写入的字段（来自本地排单表）
    address: str = ""
    meal_type: str = ""     # 类型：中餐 / 晚餐
    meal_kind: str = ""     # 餐种：经济 / 豪华
    detail: str = ""
    # 补全标记：云端这些格子目前是空的，本次要补上（防止上次中断留下半成品行）
    fill_type: bool = False
    fill_kind: bool = False
    fill_formula: bool = False
    # 新客户**排序前**所在的物理行号：排序前要写的东西（姓名/地址/电话/类型/餐种、
    # 底色）必须写在这里，否则会覆盖掉别人（排序后行号在那时还属于其他人）。
    # 放在最后，避免影响按位置构造 Change 的老代码。
    insert_row: int = 0

    @property
    def needs_write(self) -> bool:
        return ((not self.target_ok) or self.total_after != self.total_before
                or self.fill_type or self.fill_kind or self.fill_formula)


@dataclass
class InsertBlock:
    """一批要插入云端的新客户行。

    2026-09-15 起流程改为：**所有新客户合成一块，统一插到第 3 行与第 4 行之间**，
    再按列B 的地址顺序把整张表重排（见 ``SheetPlan.sort_*``）。
    ``position`` 是「插到这一行之前」（固定 = 4）；``append_only`` 表示表里本来
    就没有数据行，直接写第 4 行起即可，不需要调用插入行接口。
    """

    position: int
    count: int
    first_row: int
    address: str = ""
    append_only: bool = False


@dataclass
class SheetPlan:
    """单张表的写入计划。"""

    sheet: str
    file_id: str
    drive_id: str = ""
    target_date: _dt.date | None = None
    target_col: int = 0
    target_header: str = ""
    weekday_number: int = 0
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    append_row: int = 0
    columns: dict[str, int] = field(default_factory=dict)
    # 供"学格式"参考的已有数据行号（1-based）
    format_rows: list[int] = field(default_factory=list)
    # 参考行的逐列底色 {1-based 列: "#AARRGGBB"}（build_plan 里一次性读好并缓存）
    format_fills: dict[int, str] = field(default_factory=dict)
    # 新客户插入块（统一一块：插到第 4 行之前）；老客户行号已计入排序结果
    insert_blocks: list[InsertBlock] = field(default_factory=list)
    # 协作者通讯记号列（1-based）；0 = 没找到
    marker_col: int = 0
    date_cols: list[int] = field(default_factory=list)
    # ---- 排序（新增客户时按列B 的地址顺序重排整张表）----
    # 本次是否真的执行了「插到第 4 行 + 整表重排」
    sort_enabled: bool = False
    # 排序键辅助列（1-based）；0 = 本次不排序
    sort_key_col: int = 0
    # 排序区域，形如 ``A3:GS142``
    sort_range: str = ""
    # 每个数据行的排序键：{(排序前不能用的) 行号: 键值}
    row_keys: dict[int, int] = field(default_factory=dict)
    # 预测的排序后行号：{(姓名, 电话): 行号}
    final_rows: dict[tuple[str, str], int] = field(default_factory=dict)
    # 实际排序结果与预测不一致（真机 sort_range 行为异常）
    sort_mismatch: bool = False
    # 排序后数据区的最后一行（1-based）
    last_data_row: int = 0
    # 不在地址清单里的地址（预览里提示"会排到表尾"）
    unknown_addresses: list[str] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        return bool(self.changes)


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------

def target_date_for(now: _dt.datetime | None = None, *,
                    start_hour: int = 20, end_hour: int = 10) -> _dt.date:
    """按"晚上跑算次日"的规则算目标日期。

    窗口为 [start_hour, 24) ∪ [0, end_hour)：落在窗口内则 +1 天。
    """
    now = now or _dt.datetime.now()
    if now.hour >= start_hour or now.hour < end_hour:
        return (now + _dt.timedelta(days=1)).date()
    return now.date()


def weekday_number(day: _dt.date) -> int:
    """通讯记号数字：周日=1、周一=2 … 周六=7。"""
    return 1 if day.weekday() == 6 else day.weekday() + 2


def parse_date_header(text: Any) -> tuple[int, int] | None:
    """从表头文字解析 (月, 日)；只认月.日，忽略星期。"""
    if text is None:
        return None
    m = DATE_RE.match(str(text))
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return month, day


def person_key(name: Any, phone: Any) -> tuple[str, str]:
    """客户匹配键：名字 + 电话（都归一化：去空白、电话去小数点与非数字尾巴）。"""
    n = str(name or "").strip()
    p = str(phone or "").strip()
    if p.endswith(".0"):
        p = p[:-2]
    p = re.sub(r"\D", "", p)
    return n, p


def normalize_phone(value: Any) -> str:
    return person_key("", value)[1]


def _address_key(text: Any) -> str:
    """地址组匹配键：去空白 + 忽略大小写（云端「b2」和本地「B2」是同一组）。"""
    return re.sub(r"\s+", "", str(text or "")).casefold()


def canonical_address(raw: Any, order: Sequence[str] = ()) -> str:
    """落表时用的地址写法：先过别名（本地「小西」= 云端「小」），
    再按清单里的标准写法统一（本地写「B5」→ 落「b5」；匹配忽略大小写与空格）。"""
    text = str(raw or "").strip()
    text = ADDRESS_ALIASES.get(text, text)
    key = _address_key(text)
    for item in order:
        if _address_key(item) == key:
            return str(item).strip()
    return text


_NATURAL_CHUNK = re.compile(r"(\d+)")

def natural_key(text: Any) -> tuple:
    """自然序排序键：「医2号」排在「医10号」之前（数字按数值比，不按字典序）。

    返回可比较的元组：文本块用 (0, 文本)，数字块用 (1, 数值)。
    """
    chunks: list[tuple[int, Any]] = []
    for piece in _NATURAL_CHUNK.split(str(text or "")):
        if not piece:
            continue
        if piece.isdigit():
            chunks.append((1, int(piece)))
        else:
            chunks.append((0, piece.casefold()))
    return tuple(chunks)


def build_address_ranks(order: Sequence[str],
                        addresses: Iterable[str]) -> tuple[dict[str, int], int]:
    """算出「归一化地址 -> 名次」，名次越小越靠前。

    ``order`` 非空：按清单顺序排名，清单里没有的一律 ``len(order)``（排表尾）；
    ``order`` 为空：对出现过的地址按**自然序**升序排名（医学院用这种）。
    返回 ``(名次表, 表尾名次)``。
    """
    normalized = [_address_key(addr) for addr in order if str(addr).strip()]
    if normalized:
        ranks = {addr: idx for idx, addr in enumerate(normalized)}
        return ranks, len(normalized)
    distinct = sorted({_address_key(a) for a in addresses if _address_key(a)}, key=natural_key)
    return {addr: idx for idx, addr in enumerate(distinct)}, len(distinct)


def sort_key_value(rank: int, flag: int) -> int:
    """复合排序键：地址名次为主，同组内已有行（0）在前、新增行（1）在后。"""
    return rank * 10 + flag


def format_sort_key(value: int) -> str:
    """零填充成定宽字符串 —— 接口若按文本排序，字典序也必须等于数值序。"""
    return f"{value:0{SORT_KEY_WIDTH}d}"


def sort_key_column(*, sheet_col_to: int, extra_cols: Iterable[int] = ()) -> int:
    """选排序辅助列：一定落在该表**所有内容列右侧**。

    排序范围必须覆盖所有有内容的列，否则右侧那些列不会跟着行一起移动，
    行与列就错位了。放在最后使用列的右边一格，天然满足这个条件。
    """
    last = max([int(sheet_col_to or 0), 1, *[int(c or 0) for c in extra_cols]])
    return last + 1


def date_region(columns: Mapping[str, int],
                *, fallback_hi: int = MAX_SCAN_COL) -> tuple[int, int]:
    """日期列允许出现的区间 ``(lo, hi)``（1-based 闭区间）。

    规则：从**电话列的右一列**开始，到**第一个结构列（类型/餐种/总餐次/已出餐/
    剩余餐/备注）的左一列**结束。备注右侧是协作者写「9.14 周一」标记的区域，
    把它排除掉才不会把标记列当成日期列。
    """
    lo = int(columns.get("phone") or 3) + 1
    struct = [int(columns[key]) for key in STRUCT_COLUMN_KEYS if columns.get(key)]
    hi = (min(struct) - 1) if struct else int(fallback_hi)
    if hi < lo:
        hi = lo - 1
    return lo, hi


def date_headers(header: Mapping[int, str], lo: int, hi: int) -> list[tuple[int, str]]:
    """表头里落在日期区间内的日期样式格，返回 ``[(1-based 列, 原文)]``。"""
    found: list[tuple[int, str]] = []
    for col in sorted(header):
        column = int(col) + 1
        if lo <= column <= hi and parse_date_header(header[col]):
            found.append((column, str(header[col])))
    return found


def find_marker_column(header: Mapping[int, str], remark_col: int) -> int:
    """定位协作者通讯记号列（返回 1-based 列号，找不到用兜底位置）。

    优先：备注列右侧第一个内容为 1~7 整数的格子 —— 那是协作者**正在用**的
    记号位（实测它会随协作方式变化，不能写死偏移）。
    兜底：备注列右边第 3 列（备注+2 已被协作者的日期标记占用）。
    **护栏**：找到的格子若本身是「9.14 周一」这类日期样式，说明那是协作者的
    标记列，宁可不用也不覆盖 —— 此时返回兜底位置。
    """
    for col in sorted(header):
        if col + 1 <= remark_col:
            continue
        text = str(header[col]).strip()
        if parse_date_header(text):
            continue
        if text.isdigit() and int(text) in MARKER_VALUES:
            return col + 1
    return remark_col + MARKER_OFFSET


# ----------------------------------------------------------------------
# 本地排单表读取
# ----------------------------------------------------------------------

LOCAL_SHEETS = ("东湖中餐", "衣锦中餐", "医学院中餐",
                "东湖晚餐", "衣锦晚餐", "医学院晚餐")
# 本地子表列（1-based），与 app/excel_templates.py 的排单模板一致
LOCAL_COL = {
    "order": 1, "name": 2, "address": 3, "phone": 4,
    "type": 12, "kind": 13, "meals": 14,
}


def read_local_orders(excel_path: str | os.PathLike[str], *,
                      sheets: Iterable[str] = LOCAL_SHEETS,
                      log: Callable[[str], Any] | None = None) -> dict[str, list[CloudOrder]]:
    """读取本地排单工作簿，返回 {子表名: [CloudOrder, ...]}。

    只读取，绝不修改本地文件。
    """
    from openpyxl import load_workbook

    path = Path(excel_path)
    if not path.is_file():
        raise WpsCloudError(f"本地排单表不存在：{path}")

    result: dict[str, list[CloudOrder]] = {}
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in sheets:
            if sheet not in wb.sheetnames:
                if log:
                    log(f"[云同步] 本地表缺少子表「{sheet}」，跳过")
                continue
            ws = wb[sheet]
            orders: list[CloudOrder] = []
            # read_only 模式下 max_row 可能是 None，直接按行迭代最稳。
            for row_idx, cells in enumerate(
                    ws.iter_rows(min_row=FIRST_DATA_ROW, max_col=LOCAL_COL["meals"],
                                 values_only=True), start=FIRST_DATA_ROW):
                name = cells[LOCAL_COL["name"] - 1]
                if name is None or str(name).strip() == "":
                    continue
                meals_raw = cells[LOCAL_COL["meals"] - 1]
                try:
                    meals = int(float(meals_raw)) if meals_raw not in (None, "") else 0
                except (TypeError, ValueError):
                    meals = 0
                orders.append(CloudOrder(
                    sheet=sheet,
                    name=str(name).strip(),
                    address=str(cells[LOCAL_COL["address"] - 1] or "").strip(),
                    phone=normalize_phone(cells[LOCAL_COL["phone"] - 1]),
                    meal_type=str(cells[LOCAL_COL["type"] - 1] or "").strip(),
                    meal_kind=str(cells[LOCAL_COL["kind"] - 1] or "").strip(),
                    meals=meals,
                    row=row_idx,
                ))
            result[sheet] = orders
    finally:
        wb.close()
    return result


# ----------------------------------------------------------------------
# 账本
# ----------------------------------------------------------------------

def default_state_path() -> Path:
    try:
        from .config import user_data_dir
    except ImportError:  # pragma: no cover - 直接执行模块时
        from config import user_data_dir
    return user_data_dir() / "wps_sync_state.json"


class SyncLedger:
    """记录"每人在某目标日期上，上一次已同步的餐次数"。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path else default_state_path()
        self.data: dict[str, Any] = {"version": 1, "batches": {}}
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(payload, dict) and isinstance(payload.get("batches"), dict):
            self.data = payload

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.data, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp",
                                   dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return self.path

    # ---- 批次 ----

    def _batch(self, date_key: str, file_id: str) -> dict[str, Any]:
        batches = self.data.setdefault("batches", {})
        day = batches.setdefault(date_key, {})
        return day.setdefault(file_id, {"synced_at": "", "people": {}})

    def synced_meals(self, date_key: str, file_id: str,
                     name: str, phone: str) -> int | None:
        people = self._batch(date_key, file_id)["people"]
        entry = people.get(f"{name}\u0000{phone}")
        if entry is None:
            return None
        try:
            return int(entry.get("meals", 0))
        except (TypeError, ValueError):
            return None

    def record(self, date_key: str, file_id: str,
               entries: Mapping[str, int]) -> None:
        batch = self._batch(date_key, file_id)
        batch["synced_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        people = batch["people"]
        for key, meals in entries.items():
            people[key] = {"meals": int(meals),
                           "at": _dt.datetime.now().isoformat(timespec="seconds")}

    def batch_summary(self, date_key: str, file_id: str) -> dict[str, Any] | None:
        batch = self.data.get("batches", {}).get(date_key, {}).get(file_id)
        if not batch:
            return None
        return {"synced_at": batch.get("synced_at", ""),
                "people": len(batch.get("people", {}))}


# ----------------------------------------------------------------------
# kdocs-cli 调用
# ----------------------------------------------------------------------

def find_cli(explicit: str | os.PathLike[str] | None = None) -> str:
    """按优先级查找 kdocs-cli：显式配置 → 打包内置 → 仓库 vendor → 程序同目录 → PATH。"""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    names = [CLI_NAME_WIN, CLI_NAME] if os.name == "nt" else [CLI_NAME]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        for name in names:
            candidates.append(Path(bundle) / name)
    # 源码运行：仓库内的 vendor/kdocs-cli/
    repo_vendor = Path(__file__).resolve().parent.parent / "vendor" / "kdocs-cli"
    for name in names:
        candidates.append(repo_vendor / name)
    exe_dir = Path(sys.executable).parent
    for name in names:
        candidates.append(exe_dir / name)
    for name in names:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    raise WpsCloudError(
        "找不到 kdocs-cli 组件。请确认程序完整安装，或在「云文档同步」里手动指定路径。")


def effective_tables(config: Any) -> dict[str, dict[str, str]]:
    """返回当前实际生效的云端目标表，并拒绝过期或越权目标。

    测试模式一律写测试副本（且副本 ID 不能是正式表、也不能是已废弃的试验田）；
    正式模式只允许写正式表 ID。「闪时送下单」的云端名单读取同样走这里，
    这样测试模式下读的也是副本，不会拿正式表的数据做实验。
    """
    production = {sheet: conf.get("file_id", "") for sheet, conf in
                  (getattr(config, "wps_production_tables", None) or {}).items()}
    legacy_test_ids = {
        "H8vzKoTJVrMP7mA9QG591xqS9W8Bg57iG",
        "qFBgqf13GxM7vPUbTSJmxxsrgopD4DnpA",
        "p9P2p2NFfxMZjLXGZ1fyxxrFTBf9s81Kn",
        "RBLtXB8x3rMcQhCp6zp11xGBN7Wey2xCD",
        "afnJ5h5Di1M3U9VwX3rvxx9jpTn8EUw9o",
        "rxYTF8Juk9MBbhkbfjE9Bx1dQ3vGeZ3zr",
    }
    if bool(getattr(config, "wps_test_mode", False)):
        test = {sheet: str(fid).strip() for sheet, fid in
                (getattr(config, "wps_test_tables", None) or {}).items()
                if str(fid).strip()}
        if not test:
            raise WpsCloudError("测试模式未配置新的测试副本，拒绝写入；请先从正式表创建副本")
        # 注意：必须比对正式表的 **file_id 值**，而不是 dict 的键（键是子表名）。
        stale = {fid for fid in test.values() if fid in set(production.values())}
        if stale:
            raise WpsCloudError("测试副本配置包含正式表 ID，拒绝写入")
        if set(test.values()) & legacy_test_ids:
            raise WpsCloudError("测试副本配置包含已过期试验田 ID，拒绝写入")
        return {sheet: {"file_id": fid} for sheet, fid in test.items()}
    active = {sheet: dict(conf) for sheet, conf in
              (getattr(config, "wps_tables", None) or {}).items()}
    active_ids = {conf.get("file_id", "") for conf in active.values()}
    if active_ids - set(production.values()):
        raise WpsCloudError("正式模式目标包含非正式表 ID，拒绝写入")
    return active


class KdocsCli:
    """kdocs-cli 的最小封装。"""

    def __init__(self, cli_path: str | os.PathLike[str] | None = None,
                 *, timeout: int = 300, token: str | None = None) -> None:
        self.path = find_cli(cli_path)
        self.timeout = timeout
        self.token = token or os.environ.get("KINGSOFT_DOCS_TOKEN")

    # ---- 底层 ----

    def _run(self, *args: str, params: Mapping[str, Any] | None = None,
             retries: int = 2) -> dict[str, Any]:
        """调用 kdocs-cli 并解析 JSON。

        网络抖动（TLS handshake timeout / connection reset）会重试 ``retries`` 次 ——
        实测写入过程中偶发 TLS 超时，一次失败就让整张表判定失败代价太大。
        接口业务错误（code != 0）不重试。
        """
        last_error: WpsCloudError | None = None
        for attempt in range(retries + 1):
            try:
                return self._run_once(*args, params=params)
            except WpsCloudError as exc:
                if not _is_transient(exc):
                    raise
                last_error = exc
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        assert last_error is not None
        raise last_error

    def _run_once(self, *args: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        cmd = [self.path, *args]
        if self.token:
            cmd += ["--token", self.token]
        tmp: str | None = None
        if params is not None:
            # 关键：参数走临时文件，避免命令行长度上限（约 128 KiB）
            fd, tmp = tempfile.mkstemp(prefix="kdocs-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(params, fh, ensure_ascii=False)
            cmd += ["--file", tmp]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout)
        except FileNotFoundError as exc:
            raise WpsCloudError(f"无法执行 kdocs-cli：{exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise WpsCloudError(f"kdocs-cli 超时（{self.timeout}s）：{' '.join(args)}") from exc
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        raw = (proc.stdout or "").strip()
        payload: dict[str, Any] | None = None
        if raw.startswith("{"):
            try:
                payload, _ = json.JSONDecoder().raw_decode(raw)
            except json.JSONDecodeError:
                payload = None
        if payload is None:
            hint = (proc.stderr or raw or "").strip()[:300]
            raise WpsCloudError(f"kdocs-cli 无有效输出（exit {proc.returncode}）：{hint}")
        # 注意：CLI 在接口报错时退出码仍可能是 0，必须看 code 字段
        code = payload.get("code")
        if code in RATE_LIMIT_CODES:
            # 429001 = 当日总量用尽；429002 = 短时间频繁触发，均次日 08:00 恢复。
            # 接口返回的 reset_at 时区口径不稳定（实测与提示文案差 8 小时），
            # 因此只显示"还有多久"，不显示具体时点，避免误导。
            detail = payload.get("data") or {}
            when = ""
            reset_at = detail.get("reset_at")
            if isinstance(reset_at, (int, float)) and reset_at > 0:
                remain = reset_at - _dt.datetime.now().timestamp()
                if remain > 0:
                    hours, minutes = divmod(int(remain // 60), 60)
                    when = f"，约 {hours} 小时 {minutes} 分钟后恢复"
            elif detail.get("retry_after"):
                when = f"，约 {int(detail['retry_after']) // 60} 分钟后可再试"
            raise WpsCloudError(
                "今日云文档调用额度已用尽（金山接口限流）"
                f"{when}。这不是程序故障：读表、写表、搜索都会受限，"
                "本地排单任务不受影响。")
        if code not in (0, None):
            raise WpsCloudError(
                f"云文档接口返回 code={code}：{payload.get('message') or payload.get('msg')}")
        data = payload.get("data", payload)
        return data if isinstance(data, dict) else {"data": data}
    # ---- 认证 ----

    def authenticated(self) -> bool:
        try:
            proc = subprocess.run([self.path, "auth", "status"],
                                  capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return False
        try:
            return bool(json.loads(proc.stdout.strip()).get("authenticated"))
        except (json.JSONDecodeError, AttributeError):
            return False

    def login_argv(self) -> list[str]:
        """返回可交给调用方在终端/新窗口里执行的授权命令。"""
        return [self.path, "auth", "login"]

    # ---- 表格读写 ----

    def sheets_info(self, file_id: str) -> list[dict[str, Any]]:
        data = self._run("sheet", "get-sheets-info", params={"file_id": file_id})
        detail = data.get("detail") or {}
        return detail.get("sheetsInfo") or []

    def read_grid(self, file_id: str, worksheet_id: int,
                  row_from: int, row_to: int,
                  col_from: int, col_to: int,
                  *, with_format: bool = False) -> dict[tuple[int, int], str]:
        """读取矩形区域，返回 {(0-based 行, 0-based 列): cellText}。

        接口返回里没有 ``detail`` 说明这张表读不了（例如是二进制 xlsx 而非在线
        表格），此时抛错而不是返回空结果 —— 否则调用方会把"读不了"误判成"表是空的"。
        """
        data = self._run("sheet", "get-range-data", params={
            "file_id": file_id, "worksheet_id": worksheet_id,
            "range": {"rowFrom": row_from, "rowTo": row_to,
                      "colFrom": col_from, "colTo": col_to}})
        if not isinstance(data, dict) or not isinstance(data.get("detail"), dict):
            raise WpsCloudError(
                f"表格内容读取失败（file_id={file_id}）：{str(data)[:200]}")
        cells = data["detail"].get("rangeData") or []
        grid: dict[tuple[int, int], str] = {}
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            text = cell.get("cellText")
            if text in (None, ""):
                continue
            if with_format:
                key = (int(cell.get("originRow", 0)), int(cell.get("originCol", 0)))
                grid[key] = {"text": str(text),
                             "fill": (cell.get("cell_background_color")
                                      or cell.get("fill") or "")}
                continue
            grid[(int(cell.get("originRow", 0)), int(cell.get("originCol", 0)))] = str(text)
        return grid

    def read_row(self, file_id: str, worksheet_id: int, row: int,
                 col_from: int = 0, col_to: int = MAX_SCAN_COL - 1) -> dict[int, str]:
        """读一整行，返回 {0-based 列号: 文本}（表头解析用，避免坐标元组混淆）。

        ``row`` 为 **1-based** 行号（与 Excel 一致）。
        """
        grid = self.read_grid(file_id, worksheet_id, row - 1, row - 1, col_from, col_to)
        return {col: text for (_r, col), text in grid.items()}

    def find_column(self, file_id: str, worksheet_id: int, names: Sequence[str],
                    row: int = HEADER_ROW) -> int | None:
        """按表头文字找列，返回 **1-based** 列号；找不到返回 None。"""
        return _find_column(self.read_row(file_id, worksheet_id, row), names)

    def write_cells(self, file_id: str, worksheet_id: int,
                    cells: Sequence[Mapping[str, Any]]) -> None:
        """写入多个单元格，自动按接口上限分批。

        cells 每项：{"row": 1-based 行, "col": 1-based 列, "value": str}

        实测：``update-range-data`` 单次 ``rangeData`` 最多 **100** 项，超出返回
        ``400001 rangeData length N exceeds limit 100``。排单表追加新客户时
        单元格数很容易过百（东湖中餐一次 25 人 ≈ 175 格），因此这里必须分批。
        """
        if not cells:
            return
        pending = list(cells)
        for start in range(0, len(pending), WRITE_BATCH_CELLS):
            batch = pending[start:start + WRITE_BATCH_CELLS]
            range_data = [{
                "opType": "formula",
                "rowFrom": int(c["row"]) - 1, "rowTo": int(c["row"]) - 1,
                "colFrom": int(c["col"]) - 1, "colTo": int(c["col"]) - 1,
                "formula": str(c["value"]),
            } for c in batch]
            self._run("sheet", "update-range-data", params={
                "file_id": file_id, "worksheet_id": worksheet_id,
                "rangeData": range_data})

    def read_formulas(self, file_id: str, worksheet_id: int,
                      row_from: int, row_to: int,
                      col_from: int, col_to: int) -> dict[tuple[int, int], str]:
        """读取指定区域的公式本体（而不是计算后的显示值）。"""
        data = self._run("sheet", "get-range-data", params={
            "file_id": file_id, "worksheet_id": worksheet_id,
            "range": {"rowFrom": row_from, "rowTo": row_to,
                      "colFrom": col_from, "colTo": col_to}})
        cells = (data.get("detail") or {}).get("rangeData") or {}
        return {(int(c.get("originRow", 0)), int(c.get("originCol", 0))): str(c["fmlaText"])
                for c in cells if isinstance(c, dict) and c.get("fmlaText")}

    # ---- 格式 ----

    def insert_rows(self, file_id: str, worksheet_id: int, *,
                    row: int, count: int) -> None:
        """在 1-based 行号 ``row`` 之前插入 ``count`` 个空行。

        新行占据 row..row+count-1，原有内容（含公式、底色）整体下移。
        接口参数是 0-based 闭区间：row_from = row_to = row - 1 + count - 1。
        """
        if count <= 0:
            return
        self._run("sheet", "insert-rows-cols", params={
            "file_id": file_id, "worksheet_id": worksheet_id, "type": "row",
            "row_from": row - 1, "row_to": row - 1 + count - 1})

    def delete_rows(self, file_id: str, worksheet_id: int, *,
                    row: int, count: int) -> None:
        """删除 1-based 行号 ``row`` 起的 ``count`` 行（插入失败时的回滚手段）。"""
        if count <= 0:
            return
        self._run("sheet", "delete-range-data", params={
            "file_id": file_id, "worksheet_id": worksheet_id,
            "range_data": [{
                "col_from": 0, "col_to": 16383,
                "row_from": row - 1, "row_to": row - 1 + count - 1}],
            "shift_type": "shift_up"})

    def delete_columns(self, file_id: str, worksheet_id: int, *,
                       column: int, rows: int) -> None:
        """删除一整列（排序辅助列的收尾清理）。``column`` 为 1-based 列号。

        辅助列一定在该表所有内容列的右侧，所以左移删除不会动到任何数据。
        """
        self._run("sheet", "delete-range-data", params={
            "file_id": file_id, "worksheet_id": worksheet_id,
            "range_data": [{
                "col_from": column - 1, "col_to": column - 1,
                "row_from": 0, "row_to": max(0, rows - 1)}],
            "shift_type": "shift_left"})

    def write_format_ops(self, file_id: str, worksheet_id: int,
                         ops: Sequence[Mapping[str, Any]]) -> None:
        """批量写格式操作（opType=format），按接口单次上限自动分批。"""
        if not ops:
            return
        pending = [dict(op) for op in ops]
        for start in range(0, len(pending), WRITE_BATCH_CELLS):
            self._run("sheet", "update-range-data", params={
                "file_id": file_id, "worksheet_id": worksheet_id,
                "rangeData": pending[start:start + WRITE_BATCH_CELLS]})

    def read_cell_format(self, file_id: str, worksheet_id: int,
                         row: int, col: int) -> dict[str, Any] | None:
        """读取单个单元格的格式（1-based 行列）；空单元格返回 None。

        只用于"学一行参考格式"。注意：**只有带内容的格才会被接口返回**，
        所以调用方要挑一个确实有值的格子。
        """
        data = self._run("sheet", "get-range-data", params={
            "file_id": file_id, "worksheet_id": worksheet_id,
            "range": {"rowFrom": row - 1, "rowTo": row - 1,
                      "colFrom": col - 1, "colTo": col - 1}})
        detail = data.get("detail") if isinstance(data, dict) else None
        cells = (detail or {}).get("rangeData") or []
        for cell in cells:
            if isinstance(cell, dict) and cell.get("cellText") not in (None, ""):
                return cell
        return None

    def sort_range(self, file_id: str, worksheet_id: int, *, range_ref: str,
                   key: str, order: str = "asc", header: bool = True,
                   key2: str | None = None, order2: str | None = None) -> None:
        """原地排序（供后续功能使用）。``range_ref`` 形如 ``A3:L42``。"""
        params: dict[str, Any] = {
            "file_id": file_id, "worksheet_id": worksheet_id,
            "range": range_ref, "key": key, "order": order, "header": header}
        if key2:
            params["key2"] = key2
        if order2:
            params["order2"] = order2
        self._run("sheet", "range-sort", params=params)

    def list_files(self, drive_id: str, parent_id: str = "0",
                   page_size: int = 200) -> list[dict[str, Any]]:
        data = self._run("drive", "list-files", params={
            "drive_id": drive_id, "parent_id": parent_id, "page_size": page_size})
        return data.get("data", {}).get("items") or data.get("items") or []


# ----------------------------------------------------------------------
# 云端表解析与计划
# ----------------------------------------------------------------------

def _find_column(header: Mapping[int, str], names: Sequence[str]) -> int | None:
    """在表头里按名字找列，返回 **1-based** 列号（与写入接口保持一致）。

    注意：``header`` 的键来自 ``read_grid``，是 0-based 列号。
    """
    for col, text in header.items():
        if str(text).strip() in names:
            return int(col) + 1
    return None


def find_target_column(header: Mapping[int, str],
                       target: _dt.date,
                       *, col_from: int = 1, col_to: int = MAX_SCAN_COL
                       ) -> tuple[int, str] | None:
    """在表头里找目标日期的列（只比对月.日），**限定在日期区间内**。

    必须限定区间：协作者的标记格里也写着「9.14 周一」，若不设限，在当天还没有
    真实日期列时会命中标记列，把 1 写进协作者的格子。
    """
    for col in sorted(header):
        column = int(col) + 1
        if not (col_from <= column <= col_to):
            continue
        parsed = parse_date_header(header[col])
        if parsed and parsed == (target.month, target.day):
            return col, str(header[col])
    return None


def column_name(column: int) -> str:
    result = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        result = chr(65 + remainder) + result
    return result


def formula_cells_for_new_rows(plan: SheetPlan, rows: Sequence[int]) -> list[dict[str, Any]]:
    """为新增行生成已出餐/剩余餐公式，日期列按表头动态识别。"""
    served = plan.columns.get("served") or 0
    left = plan.columns.get("left") or 0
    total = plan.columns.get("total") or 0
    date_columns = plan.date_cols
    if not rows or not date_columns or not served or not left or not total:
        return []
    cells = []
    for row in rows:
        date_range = f"{column_name(min(date_columns))}{row}:{column_name(max(date_columns))}{row}"
        cells.extend((
            {"row": row, "col": served, "value": f"=SUM({date_range})"},
            {"row": row, "col": left,
             "value": f"={column_name(total)}{row}-{column_name(served)}{row}"},
        ))
    return cells


def _build_sheet_plan(cli: KdocsCli, *, sheet: str, orders: Sequence[CloudOrder],
                      conf: Mapping[str, str], target: _dt.date,
                      run_date: _dt.date | None,
                      order_map: Mapping[str, Sequence[str]],
                      sort_enabled: bool) -> SheetPlan:
    """为**单个**子表生成写入计划（只读云端，不写任何东西）。

    本函数是把 ``build_plan`` 的循环体**原样搬出来**的，除下列两点外没有任何改动：

    * 原先靠闭包读取的 ``sheet``/``orders``/``conf``/``target``/``run_date``/
      ``order_map``/``sort_enabled`` 改为显式入参；
    * 原先 ``plans.append(plan)`` 之后 ``continue``（末尾那处是落到循环底部），
      现在统一 ``return plan``。

    只读写自己的局部变量与入参，不碰任何共享可变状态，因此不同子表之间完全独立，
    可以安全地在工作线程里并发执行。
    """
    plan = SheetPlan(sheet=sheet, file_id=conf["file_id"],
                     drive_id=conf.get("drive_id", ""),
                     target_date=target,
                     weekday_number=weekday_number(run_date or target))
    infos = cli.sheets_info(plan.file_id)
    if not infos:
        plan.warnings.append("云端文件不可读或不是在线表格")
        return plan
    worksheet_id = int(infos[0].get("sheetId") or 1)
    # 真实的「最后使用列」（1-based）——排序辅助列要放在它右边，不能用被
    # MAX_SCAN_COL 截断过的读取范围。
    real_col_to = int(infos[0].get("colTo") or 0) + 1
    row_to, col_to = scan_bounds(infos[0])
    grid = cli.read_grid(plan.file_id, worksheet_id, 0, row_to, 0, col_to)
    plan.append_row = max(plan.append_row, 0)

    header = {col: text for (row, col), text in grid.items() if row == HEADER_ROW - 1}

    def col_or(names: Sequence[str], fallback: int) -> int:
        # 注意：_find_column 返回 1-based 列号，A 列 = 1；
        # 不能用 `or` 兜底（1 为真但 0 才是假，容易把 A 列误判）。
        value = _find_column(header, names)
        return fallback if value is None else value

    plan.columns = {
        "name": col_or(HEADER_NAME, 1),
        "address": col_or(HEADER_ADDRESS, 2),
        "phone": col_or(HEADER_PHONE, 3),
        "type": col_or(HEADER_TYPE, 0),
        "kind": col_or(HEADER_KIND, 0),
        "total": col_or(HEADER_TOTAL, 0),
        "served": col_or(HEADER_SERVED, 0),
        "left": col_or(HEADER_LEFT, 0),
        "remark": col_or(HEADER_REMARK, 0),
    }
    # 日期列区间：电话右侧 ~ 第一个结构列左侧。
    # 区间外（备注右侧）的日期样式格子是协作者的标记，只提示、不参与统计。
    date_lo, date_hi = date_region(plan.columns)
    stray_dates = [(int(col) + 1, str(text)) for col, text in header.items()
                   if int(col) + 1 > date_hi and parse_date_header(text)]
    if stray_dates:
        shown = "、".join(f"{column_name(c)}{HEADER_ROW}「{t}」"
                         for c, t in stray_dates[:2])
        plan.warnings.append(
            f"已忽略日期区间外的日期样式格 {shown}（判定为协作者的标记列，不参与统计）")
    found = find_target_column(header, target, col_from=date_lo, col_to=date_hi)
    if not found:
        note = ("（注意：备注右侧那些『9.14 周一』样式的格子是协作者的标记列，"
                "不会当成日期列）" if stray_dates else "")
        plan.warnings.append(
            f"云端表里没有 {target.month}.{target.day} 这一列，"
            f"请确认协作者是否已加好当天的列{note}")
        return plan
    plan.target_col, plan.target_header = found[0] + 1, found[1]
    plan.date_cols = [column for column, _text in date_headers(header, date_lo, date_hi)]
    if not plan.date_cols:
        plan.warnings.append("日期区间里一个日期列都没读到，已拒绝写入")
        return plan

    # 结构性校验：表头必须能读出姓名、电话、类型、餐种、总餐次。
    # 缺任何一项通常意味着表头被人改乱了（例如某列表头被覆盖成了日期），
    # 此时宁可拒绝写入，也不要往一张看不懂的表里写数字。
    # 已核对 2026-09 的 6 张正式表，这些表头都存在。
    header_texts = {str(v).strip() for v in header.values()}
    missing_header = [name for name, keys in (
        ("姓名", HEADER_NAME), ("电话", HEADER_PHONE),
        ("类型", HEADER_TYPE), ("餐种", HEADER_KIND),
        ("总餐次", HEADER_TOTAL))
        if not (header_texts & set(keys))]
    if missing_header:
        plan.warnings.append(
            f"云端表头异常，缺少 {'、'.join(missing_header)} 列，已拒绝写入"
            f"（请人工核对云端表结构）")
        return plan

    # 客户索引 + 追加行
    people: dict[tuple[str, str], int] = {}
    last_used = FIRST_DATA_ROW - 1
    for (row, col), text in grid.items():
        if row < FIRST_DATA_ROW - 1 or col != plan.columns["name"] - 1:
            continue
        last_used = max(last_used, row + 1)
        phone = grid.get((row, plan.columns["phone"] - 1), "")
        key = person_key(text, phone)
        if key[0]:
            people.setdefault(key, row + 1)
    plan.append_row = last_used + 1

    # 数据行（有姓名的行）与逐行地址 —— 排序要用。
    # 只认「有姓名」的行：表尾若有合计/说明之类的非人员行，不参与排序。
    addr_col = plan.columns.get("address") or 0
    data_rows: list[int] = []                       # 1-based，升序
    address_of: dict[int, str] = {}                 # 行号 -> 地址原文
    for (row, col), text in grid.items():
        if row < FIRST_DATA_ROW - 1 or col != plan.columns["name"] - 1:
            continue
        if not str(text).strip():
            continue
        data_rows.append(row + 1)
    data_rows = sorted(set(data_rows))
    if addr_col:
        for row in data_rows:
            address_of[row] = str(grid.get((row - 1, addr_col - 1), "") or "").strip()

    # 通讯记号列：优先认协作者正在用的 1~7 数字格，找不到用备注+3 兜底。
    if plan.columns.get("remark"):
        plan.marker_col = find_marker_column(header, plan.columns["remark"])

    # 学格式：这类排单表的底色**不统一**（实测东湖中餐 73 行白底、24 行绿底），
    # 不能简单取第一行或最后一行 —— 否则整批新行会被涂成某个偶然行的颜色。
    #
    # 做法：**一次**批量读"姓名列"（带格式），统计多数派底色；再取多数派里
    # 最靠近表尾的一行当样板，把它的逐列底色缓存到 plan.format_fills。
    # 只花 1~2 次接口调用（逐行读格式会消耗几十次，曾把当日额度打满）。
    name_col = plan.columns.get("name") or 1
    remark_col = plan.columns.get("remark") or name_col
    try:
        fmt_grid = cli.read_grid(plan.file_id, worksheet_id,
                                 FIRST_DATA_ROW - 1, row_to, name_col - 1, remark_col - 1,
                                 with_format=True)
    except WpsCloudError:
        fmt_grid = {}
    name_cells: dict[int, dict[str, str]] = {}
    for (r, c), payload in (fmt_grid or {}).items():
        if not isinstance(payload, dict):
            continue
        if c == name_col - 1 and str(payload.get("text", "")).strip():
            name_cells[r + 1] = payload
    candidates = sorted(name_cells)
    plan.format_rows = list(reversed(candidates))
    if candidates:
        from collections import Counter
        counts = Counter(str(name_cells[r].get("fill") or "") for r in candidates)
        dominant = counts.most_common(1)[0][0] if counts else ""
        preferred = [r for r in candidates
                     if str(name_cells[r].get("fill") or "") == dominant]
        if preferred:
            plan.format_rows = list(reversed(preferred))
        sample = plan.format_rows[0]
        fills: dict[int, str] = {}
        for (r, c), payload in (fmt_grid or {}).items():
            if r + 1 == sample and isinstance(payload, dict) and payload.get("fill"):
                fills[c + 1] = str(payload["fill"])
        plan.format_fills = fills

    total_col = plan.columns["total"]
    existing_changes: list[Change] = []
    new_orders: list[CloudOrder] = []
    for order in orders:
        key = person_key(order.name, order.phone)
        if not key[0]:
            continue
        row = people.get(key)
        if row:
            before = _as_int(grid.get((row - 1, total_col - 1))) if total_col else 0
            already = str(grid.get((row - 1, plan.target_col - 1), "")).strip() == CELL_MARK
            # 总餐次 = 本地「餐次」的绝对值（不是累加）。
            # 本地表就是网站当前的完整状态，云端总餐次应当与之一致；
            # 写成绝对值天然幂等：重复运行不会翻倍。
            want = order.meals
            change = Change(kind="existing", name=order.name, phone=order.phone,
                            row=row, delta=want - before, target_col=plan.target_col,
                            total_before=before, total_after=want, target_ok=already,
                            address=str(order.address or "").strip(),
                            meal_type=order.meal_type, meal_kind=order.meal_kind)
            # 半成品行自愈：上次中断可能留下缺「类型/餐种/公式」的行，本次补齐。
            type_col = plan.columns.get("type") or 0
            kind_col = plan.columns.get("kind") or 0
            served_col = plan.columns.get("served") or 0
            left_col = plan.columns.get("left") or 0
            change.fill_type = bool(
                type_col and order.meal_type
                and not str(grid.get((row - 1, type_col - 1), "")).strip())
            change.fill_kind = bool(
                kind_col and order.meal_kind
                and not str(grid.get((row - 1, kind_col - 1), "")).strip())
            change.fill_formula = bool(
                served_col and left_col and plan.date_cols
                and not str(grid.get((row - 1, served_col - 1), "")).strip()
                and not str(grid.get((row - 1, left_col - 1), "")).strip())
            if want == before:
                change.detail = "总餐次已一致"
            elif want < before:
                change.detail = (f"本地 {want} 餐 < 云端 {before} 餐，"
                                 "按本地值覆盖（如非预期请人工核对）")
                plan.warnings.append(f"{order.name}：{change.detail}")
            existing_changes.append(change)
        else:
            new_orders.append(order)

    # ---- 新增客户：统一插到第 3 行与第 4 行之间，再按列B 顺序重排整张表 ----
    order_list = [str(item).strip() for item in (order_map.get(sheet) or [])]
    written: list[tuple[CloudOrder, str]] = [
        (order, canonical_address(order.address, order_list)) for order in new_orders]

    ranks, tail_rank = build_address_ranks(
        order_list, [*address_of.values(), *(addr for _o, addr in written)])
    unknown = sorted({addr for addr in
                      [*address_of.values(), *(a for _o, a in written)]
                      if _address_key(addr) and _address_key(addr) not in ranks})
    plan.unknown_addresses = unknown[:5]
    for addr in unknown[:5]:
        plan.warnings.append(f"地址「{addr}」不在排序清单里，将排到表格最后面")
    if len(unknown) > 5:
        plan.warnings.append(f"…另有 {len(unknown) - 5} 个清单外地址，同样排到表尾")

    new_count = len(written)
    # 有老数据时插到第 4 行之前（第 3 行是第一行数据，不动它）；
    # 空表直接写第 3 行起，不留空行。
    first_insert_row = (FIRST_DATA_ROW + 1) if data_rows else FIRST_DATA_ROW
    plan.insert_blocks = []
    if new_count:
        plan.insert_blocks.append(InsertBlock(
            position=first_insert_row, count=new_count, first_row=first_insert_row,
            address="", append_only=not data_rows))

    # 排序辅助列：放在该表所有内容列右侧，排序范围才能覆盖全部列（否则会错位）。
    # 表里本来没有数据行时无需排序（没什么可排的）。
    sort_on = bool(sort_enabled and new_count and data_rows)
    helper_col = 0
    if sort_on:
        helper_col = sort_key_column(
            sheet_col_to=real_col_to,
            extra_cols=[plan.marker_col, plan.columns.get("remark") or 0])
        if helper_col > MAX_SORT_COL:
            plan.warnings.append(
                f"表格宽度异常（最后使用列 {real_col_to}，辅助列 {helper_col}），本次跳过排序")
            sort_on = False

    # 预测排序结果（稳定排序：等键保持源顺序，与云端 range-sort 的承诺一致）。
    # 排序前的物理顺序 = 新行（第 4 行起）+ 已有行（原有先后）。
    # 注意：插到"第 4 行之前"时**第 3 行不动**，第 4 行及以下才整体下移。
    def pre_insert_row(row: int) -> int:
        return row if row < first_insert_row else row + new_count

    entries: list[tuple[int, int]] = []
    if sort_on:
        for idx, (_order, addr) in enumerate(written):
            rank = ranks.get(_address_key(addr), tail_rank)
            entries.append((sort_key_value(rank, SORT_FLAG_NEW), first_insert_row + idx))
        # 表中间的空行（没有姓名的行，通常是分组之间的空行）：跟着**上一行**的
        # 地址组走、排在那组最后 —— 否则空行会被排序甩到整张表的末尾。
        name_rows = set(data_rows)
        scan_last = data_rows[-1] if data_rows else FIRST_DATA_ROW - 1
        first_rank = (ranks.get(_address_key(address_of.get(data_rows[0], "")), tail_rank)
                      if data_rows else tail_rank)
        carried = first_rank
        for row in range(FIRST_DATA_ROW, scan_last + 1):
            if row in name_rows:
                carried = ranks.get(_address_key(address_of.get(row, "")), tail_rank)
            flag = SORT_FLAG_EXISTING if row in name_rows else SORT_FLAG_BLANK
            entries.append((sort_key_value(carried, flag), pre_insert_row(row)))
        ordered = sorted(entries, key=lambda item: item[0])
        plan.row_keys = {pre_row: key for key, pre_row in entries}
        final_row_of: dict[int, int] = {
            pre_row: FIRST_DATA_ROW + idx for idx, (_key, pre_row) in enumerate(ordered)}
        plan.last_data_row = FIRST_DATA_ROW + len(ordered) - 1
        plan.sort_key_col = helper_col
        plan.sort_range = (f"A{FIRST_DATA_ROW}:"
                           f"{column_name(helper_col)}{plan.last_data_row}")
    else:
        # 不排序：新行留在第 4 行起，第 4 行及以下的已有行整体下移 new_count 行。
        final_row_of = {pre_insert_row(row): pre_insert_row(row) for row in data_rows}
        for idx in range(new_count):
            final_row_of[first_insert_row + idx] = first_insert_row + idx
        plan.last_data_row = FIRST_DATA_ROW + len(data_rows) + new_count - 1

    for change in existing_changes:
        change.row = final_row_of.get(pre_insert_row(change.row), change.row)
        plan.final_rows[person_key(change.name, change.phone)] = change.row

    for idx, (order, addr) in enumerate(written):
        pre_row = first_insert_row + idx
        row = final_row_of.get(pre_row, pre_row)
        if sort_on:
            where = f"排到第 {row} 行（地址「{addr}」）"
        elif new_count:
            where = f"插到第 {row} 行（本次未排序）"
        else:
            where = f"追加到第 {row} 行"
        change = Change(kind="new", name=order.name, phone=order.phone,
                        row=row, delta=order.meals,
                        target_col=plan.target_col,
                        insert_row=pre_row,
                        total_before=0, total_after=order.meals,
                        target_ok=False,
                        address=addr, meal_type=order.meal_type,
                        meal_kind=order.meal_kind,
                        detail=where, fill_formula=True)
        plan.changes.append(change)
        plan.final_rows[person_key(order.name, order.phone)] = row
    plan.changes = existing_changes + plan.changes
    if sort_on:
        plan.sort_enabled = True
    return plan


def build_plan(cli: KdocsCli, *, local_orders: Mapping[str, Sequence[CloudOrder]],
               tables: Mapping[str, Mapping[str, str]],
               target: _dt.date,
               ledger: SyncLedger | None,
               marker_enabled: bool = True,
               run_date: _dt.date | None = None,
               address_order: Mapping[str, Sequence[str]] | None = None,
               sort_enabled: bool = True,
               log: Callable[[str], Any] | None = None) -> list[SheetPlan]:
    """只读云端，生成写入计划（不写任何东西）。

    ``run_date``：运行日（通讯记号写的是**运行日**的周几，不是目标日期 ——
    实测目标表：周四晚跑记号 5、周五晚跑记号 6）。缺省退回目标日期。

    ``address_order``：``{子表名: [地址, ...]}``，列B 的规定顺序；空列表表示
    按地址自然升序。``sort_enabled``：新增客户时是否重排整张表。

    每个子表要 3 次云端往返，6 张子表串行最多 18 次。这里**按子表分批并发**，
    ``Executor.map`` 保证 ``plans`` 仍严格按 ``local_orders`` 的顺序产出。
    """
    order_map = address_order or {}
    # 先滤掉配置里没有 file_id 的子表：这一步不产生任何云端调用，串行版本对它们
    # 也是一个请求都不发，因此挪到并发之前不改变任何可观测行为。
    items: list[tuple[str, Sequence[CloudOrder], Mapping[str, str]]] = []
    for sheet, orders in local_orders.items():
        conf = tables.get(sheet)
        if not conf or not conf.get("file_id"):
            continue
        items.append((sheet, orders, conf))

    def _one(item: tuple[str, Sequence[CloudOrder], Mapping[str, str]]) -> SheetPlan:
        sheet, orders, conf = item
        return _build_sheet_plan(cli, sheet=sheet, orders=orders, conf=conf,
                                 target=target, run_date=run_date,
                                 order_map=order_map, sort_enabled=sort_enabled)

    if len(items) <= 1:
        # 没有可重叠的往返，直接顺序执行，省掉线程池开销。
        return [_one(item) for item in items]

    # 分批提交而不是一次提交全部：``_build_sheet_plan`` 的云端异常会向上抛，
    # 一次提交全部会在出错时把已经发出去的调用全部作废（``cancel()`` 只能取消
    # 尚未开始的任务）；分批后最多只多耗 workers-1 次，贴近串行「出错即停」。
    workers = max(1, min(WPS_PLAN_WORKERS, len(items)))
    plans: list[SheetPlan] = []
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="wps-build-plan") as pool:
        for start in range(0, len(items), workers):
            batch = items[start:start + workers]
            plans.extend(pool.map(_one, batch))
    return plans


def _as_int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def summarize_plan(plans: Iterable[SheetPlan]) -> dict[str, int]:
    """汇总计划：用于界面展示与日志。"""
    update = new = unchanged = warn = 0
    for plan in plans:
        for change in plan.changes:
            if change.kind == "new":
                new += 1
            elif change.delta == 0 and change.target_ok:
                unchanged += 1
            else:
                update += 1
            if change.delta < 0:
                warn += 1
    return {"to_update": update, "to_append": new,
            "unchanged": unchanged, "warned": warn}


def format_plan(plans: Iterable[SheetPlan]) -> str:
    """把计划渲染成人类可读的多行文本（给预览用）。"""
    lines: list[str] = []
    for plan in plans:
        head = f"【{plan.sheet}】目标日期 {plan.target_date} "
        if plan.target_col:
            head += f"→ 第 {plan.target_col} 列（{plan.target_header}）"
        lines.append(head)
        for warning in plan.warnings:
            lines.append(f"    ⚠ {warning}")
        new_count = sum(1 for c in plan.changes if c.kind == "new")
        if new_count:
            if plan.sort_enabled:
                lines.append(
                    f"    本次新增 {new_count} 人：先插到第 {FIRST_DATA_ROW + 1} 行起，"
                    f"再按地址顺序重排整张表"
                    f"（第 {FIRST_DATA_ROW}~{plan.last_data_row} 行）")
            else:
                lines.append(
                    f"    本次新增 {new_count} 人：插到第 {FIRST_DATA_ROW + 1} 行起"
                    f"（已关闭排序，新行会留在表格最上面）")
        for change in plan.changes:
            if change.kind == "new":
                lines.append(f"    + 新增 {change.name}（{change.phone}）"
                             f"总餐次 {change.total_after}、日期格 {CELL_MARK}"
                             f" → {change.detail}")
            elif change.delta == 0 and change.target_ok:
                lines.append(f"    ≈ {change.name}：已完成（总餐次 {change.total_before}，日期格已填）")
            else:
                need_cell = "" if change.target_ok else f"，日期格 {CELL_MARK}"
                lines.append(f"    · {change.name}（{change.phone}）"
                             f"总餐次 {change.total_before}→{change.total_after}{need_cell}")
                if change.detail:
                    lines.append(f"        {change.detail}")
        if not plan.changes:
            lines.append("    （无变更）")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 执行
# ----------------------------------------------------------------------

def _rollback_inserts(cli: KdocsCli, plan: SheetPlan, worksheet_id: int,
                      inserted: Sequence[tuple[int, int]],
                      emit: Callable[[str], Any]) -> None:
    """插入成功但后续写入失败时，把插出来的行删掉，避免云端留下烂尾空行。

    从位置最靠后的块开始删（前面的删除不会影响后面的行号）。
    """
    if not inserted:
        return
    for first_row, count in sorted(inserted, reverse=True):
        try:
            cli.delete_rows(plan.file_id, worksheet_id, row=first_row, count=count)
        except WpsCloudError as exc:
            emit(f"[云同步] {plan.sheet}：回滚插入失败（第 {first_row} 行起 "
                 f"{count} 行可能残留空行，请人工检查）：{exc}")
            return
    emit(f"[云同步] {plan.sheet}：已回滚 {len(inserted)} 处插入，云端恢复原状")


def read_person_rows(cli: KdocsCli, plan: SheetPlan, worksheet_id: int, *,
                     last_row: int) -> dict[tuple[str, str], int]:
    """回读「姓名+电话」列，返回 ``{(姓名, 电话): 行号}``（1-based）。

    整表排序后人行号全变了，必须靠这个重新定位到每个人的新行号。
    只读 3 列，一次调用。
    """
    name_col = plan.columns.get("name") or 1
    phone_col = plan.columns.get("phone") or max(name_col + 1, 3)
    lo, hi = min(name_col, phone_col), max(name_col, phone_col)
    row_from = FIRST_DATA_ROW if last_row >= FIRST_DATA_ROW else FIRST_DATA_ROW
    grid = cli.read_grid(plan.file_id, worksheet_id,
                         row_from - 1, max(last_row, row_from) - 1, lo - 1, hi - 1)
    index: dict[tuple[str, str], int] = {}
    for (row, col), text in grid.items():
        if col != name_col - 1 or not str(text).strip():
            continue
        phone = grid.get((row, phone_col - 1), "")
        index.setdefault(person_key(text, phone), row + 1)
    return index


def apply_plan(cli: KdocsCli, plans: Iterable[SheetPlan], *,
               ledger: SyncLedger | None = None,
               marker_enabled: bool = True,
               log: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """按计划写入云端；写入后回读校验；成功才更新账本。

    单张表的执行顺序（2026-09-15 起）：
      1. 新客户统一插到第 3 行与第 4 行之间；
      2. 写新行的姓名/地址/电话/类型/餐种（与行号无关，排序后跟着行走）；
      3. 上新行底色（经济餐照抄模板、豪华餐整行金黄）；
      4. 写排序辅助列 → range-sort 按地址顺序重排整表 → 删掉辅助列；
      5. 回读列A~C，按（姓名,电话）重新定位每个人的**排序后行号**；
      6. 写日期格/总餐次/已出餐剩余餐公式/通讯记号；
      7. 回读校验 + 记账本。

    回滚红线：**只有排序之前**的失败才回滚（把插进去的行删掉）；排序一旦成功，
    新行已散落到各地址组里，此时删行会删错人 —— 只告警，让用户重传（重复执行安全）。
    """
    emit = log or (lambda _msg, _level="INFO": None)
    result: dict[str, Any] = {"sheets": [], "written": 0, "failed": 0}
    for plan in plans:
        if not plan.target_col:
            result["sheets"].append({"sheet": plan.sheet, "status": "skipped",
                                     "reason": "未找到目标日期列"})
            continue
        infos = cli.sheets_info(plan.file_id)
        if not infos:
            result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                     "reason": "云端文件不可读"})
            result["failed"] += 1
            continue
        worksheet_id = int(infos[0].get("sheetId") or 1)

        # 1) 插入新客户行：统一一块，插到第 3 行与第 4 行之间。
        #    表里本来没有数据行时不插（写单元格会自动把表扩出来）。
        inserted: list[tuple[int, int]] = []          # (first_row, count)，回滚用
        new_changes = [c for c in plan.changes if c.kind == "new"]
        block = plan.insert_blocks[0] if plan.insert_blocks else None
        if block and not block.append_only:
            try:
                cli.insert_rows(plan.file_id, worksheet_id,
                                row=block.first_row, count=block.count)
                inserted.append((block.first_row, block.count))
                emit(f"[云同步] {plan.sheet}：已在第 {block.first_row} 行前插入 "
                     f"{block.count} 行（新客户）")
            except WpsCloudError as exc:
                emit(f"[云同步] {plan.sheet} 插入新行失败：{exc}")
                result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                         "reason": f"插入新行失败：{exc}"})
                result["failed"] += 1
                continue

        # 2) 排序前先写"与行号无关"的整行信息：排序后这些值会跟着行走。
        #    必须写在 insert_row（排序前的物理行），不能写 change.row（那是排序后
        #    的目标行号，此刻还属于别人）。
        base_cells: list[dict[str, Any]] = []
        for change in new_changes:
            at = change.insert_row or change.row
            base_cells.append({"row": at, "col": plan.columns["name"],
                               "value": change.name})
            if plan.columns.get("address"):
                base_cells.append({"row": at, "col": plan.columns["address"],
                                   "value": change.address})
            base_cells.append({"row": at, "col": plan.columns["phone"],
                               "value": change.phone})
            if plan.columns.get("type") and change.meal_type:
                base_cells.append({"row": at, "col": plan.columns["type"],
                                   "value": change.meal_type})
            if plan.columns.get("kind") and change.meal_kind:
                base_cells.append({"row": at, "col": plan.columns["kind"],
                                   "value": change.meal_kind})
        if base_cells:
            try:
                cli.write_cells(plan.file_id, worksheet_id, base_cells)
            except WpsCloudError as exc:
                _rollback_inserts(cli, plan, worksheet_id, inserted, emit)
                emit(f"[云同步] {plan.sheet} 新客户行写入失败：{exc}")
                result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                         "reason": str(exc)})
                result["failed"] += 1
                continue

        # 3) 上底色：新客户行跟着原数据行的字体/对齐；经济餐照抄模板底色（黄带位置
        #    与老行一致）；**豪华餐整行金黄**。所有操作合并后分批，25 个新行只花
        #    1~2 次调用。格式失败不影响数据写入结论。
        if new_changes:
            col_lo = plan.columns.get("name") or 1
            col_hi = plan.columns.get("remark") or (col_lo + 12)
            try:
                spec = learn_row_format(cli, plan.file_id, worksheet_id,
                                        col_from=col_lo, col_to=col_hi,
                                        rows=[r for r in plan.format_rows if r],
                                        cached_fills=plan.format_fills)
                if spec:
                    econ = [c.insert_row or c.row for c in new_changes
                            if str(c.meal_kind).strip() != "豪华"]
                    lux = [c.insert_row or c.row for c in new_changes
                           if str(c.meal_kind).strip() == "豪华"]
                    ops = build_format_ops(
                        spec, econ_rows=econ, lux_rows=lux,
                        col_from=col_lo, col_to=col_hi,
                        # A~「餐种」整段刷底色（含日期与类型之间可能夹着的空列）
                        plain_to=(plan.columns.get("kind")
                                  or plan.columns.get("type") or 0),
                        # 金黄带按列身份固定：总餐次 / 已出餐 / 剩余餐
                        band_cols=tuple(c for c in (plan.columns.get("total"),
                                                    plan.columns.get("served"),
                                                    plan.columns.get("left")) if c))
                    cli.write_format_ops(plan.file_id, worksheet_id, ops)
                    emit(f"[云同步] {plan.sheet}：{len(new_changes)} 个新客户已套用"
                         f"表格原有格式（字体/对齐、经济餐照抄模板底色"
                         + ("、豪华餐整行金黄" if lux else "") + "）")
                else:
                    emit(f"[云同步] {plan.sheet}：读不到参考行格式，跳过格式设置")
            except WpsCloudError as exc:
                emit(f"[云同步] {plan.sheet}：格式设置失败（数据已写入）：{exc}")

        # 4) 排序：写排序键（辅助列，在该表所有内容列右侧）→ 云端原地排序 → 删辅助列。
        if plan.sort_key_col and plan.row_keys:
            key_cells = [{"row": row, "col": plan.sort_key_col,
                          "value": format_sort_key(key)}
                         for row, key in sorted(plan.row_keys.items())]
            try:
                cli.write_cells(plan.file_id, worksheet_id, key_cells)
                cli.sort_range(plan.file_id, worksheet_id, range_ref=plan.sort_range,
                               key=column_name(plan.sort_key_col), order="asc",
                               header=False)
            except WpsCloudError as exc:
                # 排序还没生效（行还在原位）→ 插进去的新行可以整块删掉
                _rollback_inserts(cli, plan, worksheet_id, inserted, emit)
                emit(f"[云同步] {plan.sheet} 按地址排序失败：{exc}")
                result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                         "reason": f"按地址排序失败：{exc}"})
                result["failed"] += 1
                continue
            emit(f"[云同步] {plan.sheet}：已按地址顺序重排第 {FIRST_DATA_ROW}~"
                 f"{plan.last_data_row} 行")
            try:
                cli.delete_columns(plan.file_id, worksheet_id,
                                   column=plan.sort_key_col, rows=plan.last_data_row)
            except WpsCloudError as exc:
                emit(f"[云同步] {plan.sheet}：排序辅助列（第 {plan.sort_key_col} 列）"
                     f"删除失败，表右侧可能残留一排排序键：{exc}", "WARN")
                try:
                    cli.write_cells(plan.file_id, worksheet_id, [
                        {"row": row, "col": plan.sort_key_col, "value": ""}
                        for row in sorted(plan.row_keys)])
                except WpsCloudError:
                    pass

        # 5) 重定位：整表重排后人行号全变了，必须回读（姓名,电话）重新定位。
        #    定位失败就停手（不猜行号），此时只写了新行信息与底色，重传一次即可。
        if plan.sort_enabled:
            try:
                actual_rows = read_person_rows(cli, plan, worksheet_id,
                                               last_row=plan.last_data_row)
            except WpsCloudError as exc:
                emit(f"[云同步] {plan.sheet} 排序后无法重新定位人员行号，已停止写入"
                     f"（请重新上传，重复执行安全）：{exc}", "ERROR")
                result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                         "reason": f"排序后回读失败：{exc}"})
                result["failed"] += 1
                continue
            missing = [c for c in plan.changes
                       if person_key(c.name, c.phone) not in actual_rows]
            if missing:
                names = "、".join(c.name for c in missing[:5])
                emit(f"[云同步] {plan.sheet} 排序后定位不到这些人：{names}，已停止写入",
                     "ERROR")
                result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                         "reason": f"排序后定位不到：{names}"})
                result["failed"] += 1
                continue
            drift = 0
            for change in plan.changes:
                key = person_key(change.name, change.phone)
                row = actual_rows[key]
                if plan.final_rows.get(key, row) != row:
                    drift += 1
                    if drift <= 3:
                        emit(f"[云同步] {plan.sheet}：{change.name} 实际排在第 {row} 行，"
                             f"与预测不符（按实际行号写入）", "WARN")
                change.row = row
            if drift:
                plan.sort_mismatch = True
                emit(f"[云同步] {plan.sheet}：{drift} 人的实际行号与预测不同"
                     f"（已按实际行号写入，不影响数据正确性）", "WARN")

        # 6) 其余写入：日期格 / 总餐次 / 公式 / 通讯记号
        cells: list[dict[str, Any]] = []
        pending: list[Change] = []
        for change in plan.changes:
            if not change.needs_write:
                continue          # 已经一致：一个字都不写
            pending.append(change)
            if change.total_after != change.total_before and plan.columns.get("total"):
                cells.append({"row": change.row, "col": plan.columns["total"],
                              "value": change.total_after})
            if change.kind == "existing":
                if change.fill_type and plan.columns.get("type"):
                    cells.append({"row": change.row, "col": plan.columns["type"],
                                  "value": change.meal_type})
                if change.fill_kind and plan.columns.get("kind"):
                    cells.append({"row": change.row, "col": plan.columns["kind"],
                                  "value": change.meal_kind})
            if not change.target_ok:
                cells.append({"row": change.row, "col": change.target_col, "value": CELL_MARK})
        formula_cells = formula_cells_for_new_rows(
            plan, [c.row for c in pending if c.kind == "new"])
        # 半成品行自愈：老客户缺公式的也补上（同一套公式）
        formula_cells.extend(formula_cells_for_new_rows(
            plan, [c.row for c in pending if c.kind == "existing" and c.fill_formula]))
        cells.extend(formula_cells)
        if marker_enabled and plan.marker_col:
            cells.append({"row": HEADER_ROW, "col": plan.marker_col,
                          "value": str(plan.weekday_number)})
        if not cells:
            # 没有任何待写内容（本次也没新客户，否则一定有格子要写）
            result["sheets"].append({"sheet": plan.sheet, "status": "noop"})
            continue
        try:
            cli.write_cells(plan.file_id, worksheet_id, cells)
        except WpsCloudError as exc:
            note = "（表已按地址重排，请重新上传；重复执行是安全的）" if plan.sort_enabled else ""
            emit(f"[云同步] {plan.sheet} 写入失败：{exc}{note}", "ERROR")
            result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                     "reason": str(exc)})
            result["failed"] += 1
            continue

        # 回读校验：逐格核对，而不是只抽查首尾行。
        # （曾出现过"只抽查末尾行、恰好该行原本就是 1"导致的假阳性。）
        rows_written = sorted({int(c["row"]) for c in cells if int(c["row"]) >= FIRST_DATA_ROW})
        if rows_written:
            lo, hi = rows_written[0] - 1, rows_written[-1] - 1
            verify_cols = {plan.target_col}
            if plan.columns.get("total"):
                verify_cols.add(plan.columns["total"])
            if plan.columns.get("name"):
                verify_cols.add(plan.columns["name"])
            if formula_cells:
                for formula_col in (plan.columns.get("served"), plan.columns.get("left")):
                    if formula_col:
                        verify_cols.add(formula_col)
            c_lo, c_hi = min(verify_cols) - 1, max(verify_cols) - 1
            try:
                back = cli.read_grid(plan.file_id, worksheet_id, lo, hi, c_lo, c_hi)
            except WpsCloudError as exc:
                emit(f"[云同步] {plan.sheet} 写入已完成，但回读校验失败（网络/接口问题）：{exc}",
                     "WARN")
                result["sheets"].append({"sheet": plan.sheet, "status": "verify_unreadable",
                                         "reason": f"写入完成但回读失败：{exc}"})
                if ledger is not None and pending:
                    # 数据已写入，账本仍要记，避免下次重复写入
                    ledger.record(plan.target_date.isoformat(), plan.file_id,
                                  {f"{c.name}\u0000{c.phone}": c.total_after for c in pending})
                result["written"] += 1
                continue
            problems: list[str] = []
            formula_back = {}
            if formula_cells:
                formula_back = cli.read_formulas(
                    plan.file_id, worksheet_id, lo, hi, c_lo, c_hi)
            name_col = plan.columns.get("name") or 0
            for change in pending:
                if name_col:
                    got_name = str(back.get((change.row - 1, name_col - 1), "")).strip()
                    if got_name != str(change.name).strip():
                        problems.append(f"第 {change.row} 行应为「{change.name}」，"
                                        f"实际「{got_name}」")
                got_mark = str(back.get((change.row - 1, plan.target_col - 1), "")).strip()
                if got_mark != CELL_MARK:
                    problems.append(f"{change.name}: 目标列应为 {CELL_MARK}，实际 {got_mark!r}")
                if plan.columns.get("total"):
                    got_total = _as_int(back.get((change.row - 1, plan.columns["total"] - 1)))
                    if got_total != change.total_after:
                        problems.append(
                            f"{change.name}: 总餐次应为 {change.total_after}，实际 {got_total}")
                if change.kind == "new" and plan.columns.get("served") and plan.columns.get("left"):
                    expected_served = f"=SUM({column_name(min(plan.date_cols))}{change.row}:{column_name(max(plan.date_cols))}{change.row})"
                    expected_left = f"={column_name(plan.columns['total'])}{change.row}-{column_name(plan.columns['served'])}{change.row}"
                    if formula_back.get((change.row - 1, plan.columns["served"] - 1)) != expected_served:
                        problems.append(f"{change.name}: 已出餐公式未写入或不正确")
                    if formula_back.get((change.row - 1, plan.columns["left"] - 1)) != expected_left:
                        problems.append(f"{change.name}: 剩余餐公式未写入或不正确")
            if problems:
                for item in problems[:10]:
                    emit(f"[云同步] {plan.sheet} 校验不一致：{item}")
                result["sheets"].append({"sheet": plan.sheet, "status": "verify_failed",
                                         "problems": problems[:20]})
                result["failed"] += 1
                continue
        if ledger is not None and pending:
            # 账本仅作留痕/审计：记录本次写入后每人的总餐次与目标日期格状态
            entries = {f"{c.name}\u0000{c.phone}": c.total_after for c in pending}
            ledger.record(plan.target_date.isoformat(), plan.file_id, entries)
        result["sheets"].append({"sheet": plan.sheet, "status": "ok",
                                 "cells": len(cells), "people": len(pending),
                                 "sorted": bool(plan.sort_enabled),
                                 "sort_mismatch": bool(plan.sort_mismatch)})
        result["written"] += 1
    if ledger is not None:
        try:
            ledger.save()
        except OSError as exc:
            emit(f"[云同步] 账本保存失败：{exc}")
    return result


# 传输层瞬时错误的特征词：这些值得重试（业务错误不重试）。
_TRANSIENT_HINTS = (
    "TLS handshake timeout", "connection reset", "connection refused",
    "i/o timeout", "EOF", "broken pipe", "no such host",
    "temporarily unavailable", "timeout awaiting response",
)


def _is_transient(exc: BaseException) -> bool:
    text = str(exc)
    return any(hint.lower() in text.lower() for hint in _TRANSIENT_HINTS)


def scan_bounds(info: Mapping[str, Any] | None, *,
                max_col: int = MAX_SCAN_COL,
                max_row: int = MAX_SCAN_ROW,
                budget: int = MAX_READ_CELLS) -> tuple[int, int]:
    """按接口单次上限，算出安全的读取范围 ``(row_to, col_to)``（0-based 闭区间）。

    先按需取列宽，再据此压缩行数 —— 宽表（排单表有 100+ 个日期列）必须少读行。
    """
    sheet = (info or {}) if isinstance(info, Mapping) else {}
    col_to = min(int(sheet.get("colTo") or 0), max_col)
    row_to = min(int(sheet.get("rowTo") or 0), max_row)
    cols = col_to + 1
    if cols > 0:
        row_to = min(row_to, max(0, budget // cols - 1))
    return row_to, col_to


def _argb_to_int(hexcolor: str) -> int:
    """"#FF92D050" -> 4287811664（接口用 ARGB 整数传色）。"""
    text = str(hexcolor).strip().lstrip("#")
    try:
        return int(text, 16) & 0xFFFFFFFF
    except ValueError:
        return 0xFFFFFFFF


def learn_row_format(cli: "KdocsCli", file_id: str, worksheet_id: int, *,
                     col_from: int, col_to: int,
                     rows: Sequence[int],
                     cached_fills: Mapping[int, str] | None = None) -> dict[str, Any]:
    """从已有数据行里"学"一行模板：字体 + 对齐 + **每列底色**。

    按列照抄底色（而不是"读回目标行原底色"）的原因：接口不返回空单元格，
    目标行的空列读不到颜色，照抄会让那些列变成无填充（看起来像被涂黑）。
    模板行（如东湖中餐第 99 行）的颜色分布是「A~J 白 / K~M 黄 / N 白」，
    照抄后新行的黄带位置与老行完全一致。

    ``rows`` 里第一个能读到内容的行会被采用；全都读不到返回空 dict，
    调用方应跳过格式写入（宁可不设，也不要写错）。
    """
    for row in rows:
        grid = cli.read_grid(file_id, worksheet_id, row - 1, row - 1, col_from - 1, col_to - 1)
        cells = {col + 1: text for (_r, col), text in grid.items()}
        if not cells:
            continue
        # 逐列底色：优先用 build_plan 预先读好的缓存（省接口调用）；
        # 没有缓存时才逐列读 —— 宽表逐列读会消耗大量调用次数。
        fills: dict[int, str] = dict(cached_fills or {})
        if not fills:
            for col in range(col_from, col_to + 1):
                cell = cli.read_cell_format(file_id, worksheet_id, row, col)
                if cell and cell.get("cell_background_color"):
                    fills[col] = str(cell["cell_background_color"])
        spec_cell = cli.read_cell_format(file_id, worksheet_id, row, col_from)
        if not spec_cell:
            continue
        fonts = spec_cell.get("fonts") or {}
        align = spec_cell.get("alignment") or {}
        return {
            "font_name": fonts.get("font_east_asia") or fonts.get("name") or "",
            "font_size": int(fonts.get("size") or 10),
            "font_color": (_argb_to_int(fonts["color"]) if fonts.get("color") else None),
            "alcH": _ALIGN_H.get(str(align.get("horizontal") or ""), 2),
            "alcV": _ALIGN_V.get(str(align.get("vertical") or ""), 1),
            "fill_default": _argb_to_int(fills.get(col_from, "#FFFFFFFF")),
            "fills": fills,
            "sample_row": row,
        }
    return {}


def build_format_ops(spec: Mapping[str, Any], *, econ_rows: Sequence[int],
                     lux_rows: Sequence[int],
                     col_from: int, col_to: int,
                     plain_to: int = 0,
                     band_cols: Sequence[int] = ()) -> list[dict[str, Any]]:
    """把"新客户行 × 模板格式"压成尽量少的格式操作。

    - 经济行：**第 A 列 ~ 「餐种」列整段刷成模板底色**（用户 2026-09-15 要求）。
      原因：日期列与「类型」之间有时夹着一列**空列**，接口不返回空单元格、读不到
      它的底色，逐列照抄就会漏掉它 —— 那一格于是保留插入时从上一行继承的颜色，
      看起来就是"有一格没涂到"。整段刷过去就不会再有漏网的列。
    - 金黄色的三列（总餐次/已出餐/剩余餐）按**列的身份**固定涂金，不依赖模板行
      那几格有没有内容（实测有客户行的总餐次是空的，模板选到它就学不到金色）。
    - 豪华行：**整行金黄**（名字到备注整段，用户 2026-09-12 确认）；
    - 相邻同色列再合并成区间。

    这样 25 个新行只要 ~1 次接口调用（逐行设格式要 25 次，曾把当日额度打满）。
    """
    base_xf: dict[str, Any] = {"alcH": spec.get("alcH", 2), "alcV": spec.get("alcV", 1)}
    if spec.get("font_name"):
        font: dict[str, Any] = {
            "name": spec["font_name"],
            "dyHeight": int(spec.get("font_size", 10)) * FONT_SIZE_TO_TWIP,
        }
        if spec.get("font_color") is not None:
            font["color"] = {"type": 2, "value": int(spec["font_color"])}
        base_xf["font"] = font

    def _xf(color: int) -> dict[str, Any]:
        xf = dict(base_xf)
        xf["fill"] = {"type": 1, "back": {"type": 2, "value": color},
                      "fore": {"type": 255, "value": 0, "tint": 0}}
        return xf

    def _runs(values: Sequence[int]) -> list[tuple[int, int]]:
        runs: list[tuple[int, int]] = []
        for value in sorted(set(values)):
            if runs and value == runs[-1][1] + 1:
                runs[-1] = (runs[-1][0], value)
            else:
                runs.append((value, value))
        return runs

    fills: Mapping[int, str] = spec.get("fills") or {}
    # 注意：fills 里是 "#AARRGGBB" 字符串，而 fill_default 已经是整数 —— 别再转一次，
    # 否则 str(4294967295) 会被当十六进制解析成 0x94967295（离线仿真抓到的真实教训）。
    if fills.get(col_from):
        base_color = _argb_to_int(str(fills[col_from]))
    else:
        base_color = int(spec.get("fill_default") or 0xFFFFFFFF) & 0xFFFFFFFF
    # 「餐种」列及其左边整段用模板底色；它右边只有金黄三列是特殊的。
    plain_end = plain_to if col_from <= plain_to <= col_to else col_from - 1
    band = {int(c) for c in band_cols if col_from <= int(c) <= col_to}

    def color_of(column: int) -> int:
        if column <= plain_end:
            return base_color
        if column in band:
            return FILL_GOLD
        learned = fills.get(column)
        return _argb_to_int(learned) if learned else base_color

    ops: list[dict[str, Any]] = []
    for r0, r1 in _runs(econ_rows):
        col = col_from
        while col <= col_to:
            color = color_of(col)
            col_end = col
            while col_end + 1 <= col_to and color_of(col_end + 1) == color:
                col_end += 1
            ops.append({"opType": "format",
                        "rowFrom": r0 - 1, "rowTo": r1 - 1,
                        "colFrom": col - 1, "colTo": col_end - 1,
                        "xf": _xf(color)})
            col = col_end + 1
    for r0, r1 in _runs(lux_rows):
        ops.append({"opType": "format",
                    "rowFrom": r0 - 1, "rowTo": r1 - 1,
                    "colFrom": col_from - 1, "colTo": col_to - 1,
                    "xf": _xf(FILL_LUXURY)})
    return ops
