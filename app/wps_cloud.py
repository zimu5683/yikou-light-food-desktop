"""WPS 云文档同步：把本地排单表的内容增量写入云端排单表。

设计要点（均来自 2026-09 的真机验证，详见 design/WPS-CLOUD-SYNC-PLAN.md）：

- 通过金山官方 CLI ``kdocs-cli`` 访问云文档，**单元格级读写**，不做整表覆盖，
  因此不会破坏协作者维护的公式、自定义排序、字体与列宽。
- 云端表头不是固定列号（6 张表的目标日期列分别在第 6/88/113/87/110/84 列），
  因此一律**按内容定位**：日期列只比对「月.日」，忽略星期文字（协作者写错过星期）。
- 按【名字 + 电话】匹配客户，**同一个人本地有几行、云端就写几行**（一人一天两单
  各占一行、各标当天 1）；匹配与幂等都按槽位（第几行）记录。
- 总餐次是**增量累加**：``云端现值 + (本地本批餐次 − 账本里本批已同步的本地餐次)``，
  重复上传增量为 0，一个格子都不写。
- 目标日期格里协作者写下的非 ``1`` 非空值（例如 ``0`` = 当天不送）**只读不写**。
- 上传前核对本地表的批次星期标记与目标日期；明确矛盾时整表拒绝，零次云端写请求。
- 用本地账本记录"每人/每槽位上次已同步的本地餐次"，使重复运行零副作用。
- 任何失败都只抛异常/写日志，绝不阻塞调用方的本地排单任务。

依赖：仅标准库 + openpyxl（读取本地 xlsx）+ kdocs-cli 可执行文件。
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
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

try:
    from .wps_atomicio import (FileLock, atomic_write_text, lock_path_for,
                                operation_lock_path_for)
except ImportError:  # pragma: no cover - 直接执行模块时
    from wps_atomicio import (FileLock, atomic_write_text, lock_path_for,
                              operation_lock_path_for)

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
#: 计划构建时账本文件尚不存在；执行锁内若文件出现/摘要变化即为 stale。
LEDGER_SNAPSHOT_ABSENT = "__wps_ledger_absent__"
# 排序辅助列的列号上限：真实排单表最宽也就 200 多列，异常宽说明 used range 有问题，
# 此时宁可不排序，也不要对几万列的区域发排序请求。
MAX_SORT_COL = 1000
# 排序前的“右侧边界探测”宽度（列数）。辅助列放在「已扫描到的最后一个有内容的列」
# 右边，这一带必须为空，否则说明有内容落在 MAX_SCAN_COL 之外 —— 那样排序区覆盖不到它，
# 排序后行与列就会错位，必须拒绝排序而不是硬排。
SORT_PROBE_WIDTH = 40
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

# 周一~周日（本地排单表的第 5~11 列标记的就是这个顺序）。
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class WpsCloudError(RuntimeError):
    """云文档同步过程中的可预期错误（调用方据此提示用户）。"""


class LedgerCorruptError(WpsCloudError):
    """同步账本损坏或不可读。

    必须让调用方**拒绝上传**，而不是当成空账本 —— 后者会把同一批餐再加一遍。
    """


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------

@dataclass
class CloudOrder:
    """本地排单表里的**一行**订单。

    ``meals`` 是这一行的「餐次」（**本次要加的餐**，不是云端总餐次的绝对值）：
    本地排单表每次「订单处理」都会清空六张子表重写，所以它只代表这一批的餐。

    同一个人在同一子表里可能有多行（一晚两单、一单按份数拆成两行）—— 读取层
    **不合并**；由 :func:`_group_rows_per_person` 按「一行一个槽位」分组，
    云端同样写两行、各标当天 1（用户 2026-09-18 明确）。
    """

    sheet: str
    name: str
    address: str
    phone: str
    meal_type: str          # 中餐 / 晚餐
    meal_kind: str          # 经济 / 豪华
    meals: int              # 「餐次」列
    row: int = 0            # 本地行号，便于报错定位
    # ---- 以下字段在末尾追加，避免影响按位置构造 CloudOrder 的老代码 ----
    order_no: str = ""                    # 本地「订单」列（W 编号）
    rows: tuple[int, ...] = ()            # 该行占用的本地行号（读取层恒为单元素）
    weekday_marks: tuple[str, ...] = ()   # 本地行的「周一~周日」标记（批次日期核对用）

    @property
    def local_rows(self) -> tuple[int, ...]:
        """这一行订单对应的本地行号（老数据可能只有 ``row``）。"""
        return self.rows or ((self.row,) if self.row else ())


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
    # ---- 累加语义：总餐次 = 云端现值 + 本次增量 ----
    # 本槽位对应的本地「餐次」
    local_meals: int = 0
    # 账本里「本批该槽位已同步的本地餐次」；None = 本批首次同步这一槽位
    ledger_prev: int | None = None
    # 目标日期格里协作者已经写下的值（非空且不是 1，例如 0 = 当天不送）。
    # 非空时程序**只读不写**这一格 —— 那是协作者的明确决定，不是"还没写"。
    target_occupied: str = ""
    # 这个人在本地表里占用的行号（预览里写"本地第 19 行"，便于人工核对）
    local_rows: tuple[int, ...] = ()
    # 槽位（1-based）：本地第 i 行 ↔ 云端这个人的第 i 行。
    # 同一个人一天下了两单（或一单两份）时会有 slot=2 的变更，两行都标当天 1，
    # 这样「闪时送下单」按"日期格 == 1"出两单、这天真的送两餐。
    slot: int = 1
    # 排序前这一槽位所在的物理行号；排序后用它写排序身份标记。
    pre_row: int = 0
    # build_plan 看到的这个人排序前已有的云端行号（1-based，升序）；
    # 新增槽位为空元组。恢复核对用它判断"原值槽位"和"本次新建槽位"。
    cloud_before_rows: tuple[int, ...] = ()

    @property
    def target_blocked(self) -> bool:
        """目标日期格已被协作者占用（写了 0 等），本次不能覆盖。"""
        return bool(str(self.target_occupied).strip())

    @property
    def needs_write(self) -> bool:
        """这条变更是否真的需要写云端（目标格已是目标状态且无需补格式时为 ``False``）。"""
        return (((not self.target_ok) and not self.target_blocked)
                or self.total_after != self.total_before
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
    # 排序区实际覆盖到的最后一列（1-based）；写入前由 ``probe_sort_area`` 探测确认
    sort_probe_col: int = 0
    # 每个数据行的排序键：{(排序前不能用的) 行号: 键值}
    row_keys: dict[int, int] = field(default_factory=dict)
    # 预测的排序后行号：{(姓名, 电话): (第 1 行, 第 2 行, ...)}，按槽位顺序
    final_rows: dict[tuple[str, str], tuple[int, ...]] = field(default_factory=dict)
    # 实际排序结果与预测不一致（真机 sort_range 行为异常）
    sort_mismatch: bool = False
    # 排序后数据区的最后一行（1-based）
    last_data_row: int = 0
    # 不在地址清单里的地址（数据排查用；按设计排到表尾，不算风险）
    unknown_addresses: list[str] = field(default_factory=list)
    # 整张表被拒绝写入的原因（非空 = 本次一个格子都不写）。
    # 目前只有一个来源：本地排单表的批次日期与目标日期不符（见 _batch_date_check）。
    blocked_reason: str = ""
    # 本批（目标日期 + 文件）已同步过的摘要，形如「29 人（2026-09-17T21:27:06）」；
    # 空 = 本批还没同步过（首次上传）
    previous_batch: str = ""
    # build_plan 读到的人员行基线（排序/插入前）：[(1-based 行, 姓名, 规范化电话), ...]，
    # 只用于恢复时证明"整张表完全没动过"，不参与业务写入。
    baseline_name_rows: tuple[tuple[int, str, str], ...] = ()
    # build_plan 构建该计划时所用 ledger 的磁盘快照摘要（``_loaded_digest``）。
    # apply_plan 在锁内必须拿它与最新磁盘账本比较，不能用执行时新加载的 ledger 冒充。
    ledger_digest: str | None = None

    @property
    def applied(self) -> bool:
        """本次计划是否包含任何变更。"""
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
    """把手机号规范化成「11 位 ASCII 数字」形式（与 :func:`person_key` 口径一致）。"""
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


def effective_last_col(reported: int, *,
                       scan_width: int = MAX_SCAN_COL) -> int:
    """把 ``sheetsInfo.colTo`` 换算成可用的「最后使用列」（1-based）。

    为什么不能无条件相信它：那是 WPS 记的 used range 右下角，一旦表格被"整列/到最右列"
    的操作碰过（本程序自己就会用 ``col_to: 16383`` 删行），它就会停在网格最右列 ——
    实测东湖中餐返回 16383，而真实内容只到第 22 列。据此算出的辅助列是 16385，
    超过 ``MAX_SORT_COL``，排序会被静默跳过（这正是"东湖中餐排不了序"的根因）。

    规则：报出来的宽度**没超过我们真正读过的范围**时照用（它更保守，辅助列能离内容
    更远、不会碰到内容右侧的边框/底色）；超过读取范围（``MAX_SCAN_COL``）说明它是
    "整列污染"的产物，此时退回读取宽度，边界由 :func:`probe_sort_area` 兜底。
    """
    reported = max(1, int(reported or 0))
    return reported if reported <= max(1, int(scan_width)) else max(1, int(scan_width))


def content_last_col(grid: Mapping[tuple[int, int], Any], fallback: int) -> int:
    """已读到的内容里**最后一个有内容的列**（1-based）；没有内容时用 ``fallback``。"""
    last = 0
    for row, col in grid:
        try:
            column = int(col) + 1
        except (TypeError, ValueError):
            continue
        if column > last:
            last = column
    return max(int(fallback or 0), last, 1)


def probe_sort_area(cli: "KdocsCli", plan: SheetPlan, worksheet_id: int, *,
                    col_from: int, row_to: int,
                    width: int = SORT_PROBE_WIDTH) -> tuple[bool, str]:
    """确认「辅助列右边的这一带」是空的 —— 排序区必须覆盖到表里所有内容。

    ``col_from`` 必须从 **辅助列 + 1** 开始：辅助列自己就装着本次要写的排序键，
    把它一起读进来会误判成"右侧还有内容"。``content_last_col`` 只能看见
    ``MAX_SCAN_COL`` 以内读到的内容；若表在更右边还有东西（异常 used range 通常
    就是这个原因），排序区就覆盖不到那些列，排序后行会与它们错位。这里往右探一条
    窄带：发现任何内容就返回 ``False``，调用方拒绝排序。

    只读、只发一次请求；``read_grid`` 的键是 0-based 坐标，因此定位不依赖接口顺序。
    """
    col_to = min(MAX_SORT_COL, int(plan.sort_key_col) + max(0, int(width) - 1),
                 16383)
    if col_to <= int(col_from):
        return True, ""
    try:
        grid = cli.read_grid(plan.file_id, worksheet_id, FIRST_DATA_ROW - 1,
                             int(row_to), int(col_from) - 1, col_to)
    except WpsCloudError:
        # 探测本身失败不该阻断排序：按"没探到"处理（旧行为），只留一条提示。
        return True, ""
    if not grid:
        return True, ""
    row, col = min(grid, key=lambda key: (int(key[1]), int(key[0])))
    return False, (f"{column_name(int(col) + 1)}{int(row) + 1}「"
                   f"{str(grid[(row, col)])[:12]}」")


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
    # 周一~周日 七列：记录的是**这一批要送的那天**是周几，
    # 云同步用它核对"本地表是不是这次目标日期的批次"（见 _batch_date_check）。
    "weekdays": (5, 6, 7, 8, 9, 10, 11),
    "type": 12, "kind": 13, "meals": 14,
}


def _weekday_marks(cells: Iterable[Any]) -> tuple[str, ...]:
    """从一行的「周一~周日」七格里读出标记（只为真值格；``0``/空格都不算）。"""
    values = list(cells)
    marks: list[str] = []
    for name, raw in zip(WEEKDAYS, values):
        text = str(raw if raw is not None else "").strip()
        if text and text != "0":
            marks.append(name)
    return tuple(marks)


def read_local_orders(excel_path: str | os.PathLike[str], *,
                      sheets: Iterable[str] = LOCAL_SHEETS,
                      log: Callable[[str], Any] | None = None) -> dict[str, list[CloudOrder]]:
    """读取本地排单工作簿，返回 ``{子表名: [CloudOrder, ...]}``（**一行一条**）。

    同一个人的多行**在这里保持原样**（不合并不去重）：由 :func:`build_plan`
    按「一行一个槽位」处理（云端也写多行）。

    只读取，绝不修改本地文件。

    实现上先整份读进内存再解析（见 :func:`read_local_orders_from_bytes`）：这样
    "算指纹的字节"与"解析出来的内容"必然是同一份，不会出现半新半旧的混合快照。
    """
    path = Path(excel_path)
    if not path.is_file():
        raise WpsCloudError(f"本地排单表不存在：{path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise WpsCloudError(f"本地排单表不可读：{path}（{exc}）") from exc
    return read_local_orders_from_bytes(data, sheets=sheets, log=log)


def read_local_orders_from_bytes(data: bytes, *,
                                 sheets: Iterable[str] = LOCAL_SHEETS,
                                 log: Callable[[str], Any] | None = None
                                 ) -> dict[str, list[CloudOrder]]:
    """从**内存字节**解析本地排单工作簿（不碰文件系统）。

    单独抽出来的原因：预览令牌要绑定"本地文件的 SHA-256"，上传时也要重算并比对；
    如果哈希的是一次读、解析的是另一次读，中间被替换文件就会产出一份**半新半旧**
    的计划 —— 哈希校验通过，写入内容却来自另一个版本。
    """
    from io import BytesIO

    from openpyxl import load_workbook

    buffer = BytesIO(data)
    result: dict[str, list[CloudOrder]] = {}
    wb = load_workbook(buffer, read_only=True, data_only=True)
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
                    order_no=str(cells[LOCAL_COL["order"] - 1] or "").strip(),
                    rows=(row_idx,),
                    weekday_marks=_weekday_marks(
                        cells[col - 1] for col in LOCAL_COL["weekdays"]),
                ))
            result[sheet] = orders
    finally:
        wb.close()
    return result


# ----------------------------------------------------------------------
# 账本
# ----------------------------------------------------------------------

def default_state_path() -> Path:
    """同步账本的默认路径：用户配置目录下的 ``wps_sync_state.json``。"""
    try:
        from .config import user_data_dir
    except ImportError:  # pragma: no cover - 直接执行模块时
        from config import user_data_dir
    return user_data_dir() / "wps_sync_state.json"


def _entry_key(name: str, phone: str) -> str:
    """账本里一个人的键：``姓名\\u0000电话``（与账本文件里的历史格式一致）。"""
    return f"{name}\u0000{phone}"


def _to_int(value: Any) -> int | None:
    """尽力转 int；转不了返回 ``None``（坏数据当"没有记录"，不抛异常）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _require_entry_number(entry: Mapping[str, Any], key: str) -> None:
    if key in entry and _to_int(entry[key]) is None:
        raise LedgerCorruptError("账本条目数字字段非法")


def validate_ledger_payload(payload: Any) -> dict[str, Any]:
    """校验账本整体结构；任何损坏都抛 :class:`LedgerCorruptError`。

    迁移兼容只接受"旧字段名 + 新校验规则"：旧账本可能只有 ``meals``，
    可能没有 ``version``；但 **必须** 有 ``batches`` 映射，且每个人条目的
    已知数字字段可解析。这样截断 JSON、半截写入、字段类型被改坏的账本会
    失败关闭，而不是被当成空账本把同一批餐再加一遍。
    """
    if not isinstance(payload, dict):
        raise LedgerCorruptError("账本根节点不是 JSON 对象")
    batches = payload.get("batches")
    if not isinstance(batches, dict):
        raise LedgerCorruptError("账本缺少合法的 batches 映射")
    version = payload.get("version", 1)
    if not isinstance(version, int):
        raise LedgerCorruptError("账本 version 不是整数")
    for _day_key, day in batches.items():
        if not isinstance(day, dict):
            raise LedgerCorruptError("账本日期层结构非法")
        for _file_key, batch in day.items():
            if not isinstance(batch, dict):
                raise LedgerCorruptError("账本批次层结构非法")
            people = batch.get("people")
            if not isinstance(people, dict):
                raise LedgerCorruptError("账本 people 层结构非法")
            for person_key, entry in people.items():
                if not isinstance(person_key, str) or not isinstance(entry, dict):
                    raise LedgerCorruptError("账本人员条目结构非法")
                for numeric_key in ("local", "meals", "total"):
                    _require_entry_number(entry, numeric_key)
                slots = entry.get("slots")
                has_anchor = any(key in entry for key in ("local", "meals", "slots"))
                if not has_anchor:
                    raise LedgerCorruptError("账本人员条目缺少幂等锚点")
                if slots is not None:
                    if not isinstance(slots, (list, tuple)):
                        raise LedgerCorruptError("账本 slots 结构非法")
                    if not slots or any(_to_int(value) is None for value in slots):
                        raise LedgerCorruptError("账本 slots 值非法")
    return payload


def _entry_merge_key(entry: Mapping[str, Any]) -> tuple[str, int]:
    stamp = str(entry.get("at") or "")
    slots = entry.get("slots")
    if isinstance(slots, (list, tuple)):
        weight = sum(_to_int(item) or 0 for item in slots)
    else:
        weight = _to_int(entry.get("local")) or 0
    return stamp, weight


def _merge_people_into(base: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    for person_key, entry in incoming.items():
        if not isinstance(entry, Mapping):
            continue
        other = base.get(person_key)
        if (not isinstance(other, Mapping)
                or _entry_merge_key(entry) >= _entry_merge_key(other)):
            base[person_key] = entry


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SyncLedger:
    """记录"每人在某目标日期上，上一次已同步的**本地餐次**"。

    **这不只是留痕**：总餐次改成"云端现值 + 本次增量"后，账本里记的"本批已同步
    的本地餐次"就是幂等锚点：

        本次增量 = 本地本批餐次 − 账本里的本批本地餐次

    同一批重复上传时增量为 0（一个格子都不写）；本地表里加了新的餐，只补差额。
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path else default_state_path()
        self.data: dict[str, Any] = {"version": 1, "batches": {}}
        self._loaded_digest: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._loaded_digest = None
            return
        except (OSError, UnicodeDecodeError) as exc:
            raise LedgerCorruptError("账本文件不可读") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LedgerCorruptError("账本 JSON 损坏") from exc
        self.data = validate_ledger_payload(payload)
        self._loaded_digest = _digest_text(raw)

    @property
    def journal_path(self) -> Path:
        """与账本同目录的写入意图日志路径（测试可用临时账本路径注入）。"""
        return Path(str(self.path) + ".journal")

    def _merged_data(self, disk_data: Mapping[str, Any]) -> dict[str, Any]:
        """把本内存批次合并进磁盘最新快照；不同人/不同批次不互相覆盖。

        同一人条目按 ``(at 时间戳, 槽位总量)`` 取新；时间戳相同时保留总量较大的
        一方。这样两个进程各自 ``record()+save()`` 不会用旧快照覆盖对方。
        """
        merged = copy.deepcopy(dict(disk_data))
        merged.setdefault("version", 1)
        batches = merged.setdefault("batches", {})
        for day_key, day in (self.data.get("batches") or {}).items():
            if not isinstance(day, Mapping):
                continue
            target_day = batches.setdefault(day_key, {})
            for file_key, batch in day.items():
                if not isinstance(batch, Mapping):
                    continue
                target_batch = target_day.setdefault(
                    file_key, {"synced_at": "", "people": {}})
                target_batch["synced_at"] = max(
                    str(target_batch.get("synced_at") or ""),
                    str(batch.get("synced_at") or ""))
                people = target_batch.setdefault("people", {})
                _merge_people_into(people, batch.get("people") or {})
        return merged

    def _save_unlocked(self) -> Path:
        """已持有数据锁时的原子落盘；由 :meth:`save` 与 :meth:`merge_entries` 调用。"""
        text = json.dumps(self.data, ensure_ascii=False, indent=2)
        path = atomic_write_text(self.path, text)
        self._loaded_digest = _digest_text(text)
        return path

    def save(self) -> Path:
        """持跨进程数据锁，合并磁盘最新快照后原子落盘。

        journal/ledger 共用 ``<ledger>.lock``；即使两个进程都从旧快照
        ``record()+save()``，后写者也会把先写者的不同批次/人员合并而不是覆盖。
        """
        with FileLock(lock_path_for(self.path), timeout=30.0):
            fresh = SyncLedger(self.path)
            merged = self._merged_data(fresh.data)
            fresh.data = merged
            fresh._save_unlocked()
            self.data = merged
            self._loaded_digest = fresh._loaded_digest
        return self.path

    def merge_entries(self, date_key: str, file_id: str,
                      entries: Mapping[str, Any]) -> Path:
        """跨进程安全的账本合并：持锁、重读磁盘、追加本次条目、原子落盘。

        用于"两个进程各自给同一账本追加不同批次/人员"的场景，避免后写者
        用旧内存快照覆盖先写者。
        """
        with FileLock(lock_path_for(self.path), timeout=30.0):
            fresh = SyncLedger(self.path)
            fresh.record(date_key, file_id, entries)
            fresh._save_unlocked()
            self.data = fresh.data
            self._loaded_digest = fresh._loaded_digest
        return self.path

    # ---- 批次 ----

    def _batch(self, date_key: str, file_id: str) -> dict[str, Any]:
        batches = self.data.setdefault("batches", {})
        day = batches.setdefault(date_key, {})
        return day.setdefault(file_id, {"synced_at": "", "people": {}})

    def _person_entry(self, date_key: str, file_id: str,
                      name: str, phone: str) -> Mapping[str, Any] | None:
        """**只读**取一个人的账本条目；任何一层缺失都返回 ``None``。

        刻意不走 :meth:`_batch`：那个会 ``setdefault`` 造出空批次，而
        ``build_plan`` 会**按子表并发**查账本，预览也要求"绝不改状态"。
        """
        batches = self.data.get("batches")
        if not isinstance(batches, Mapping):
            return None
        day = batches.get(date_key)
        if not isinstance(day, Mapping):
            return None
        batch = day.get(file_id)
        if not isinstance(batch, Mapping):
            return None
        people = batch.get("people")
        if not isinstance(people, Mapping):
            return None
        entry = people.get(_entry_key(name, phone))
        return entry if isinstance(entry, Mapping) else None

    def synced_local(self, date_key: str, file_id: str,
                     name: str, phone: str) -> int | None:
        """查"某人本批上次已同步的**本地餐次**"；没有记录返回 ``None``。

        旧版账本（绝对值时代）里只有 ``meals``：那个值就是当时的本地「餐次」
        （旧代码写入的总餐次 = 本地值），因此可以直接当"已同步本地餐次"用 ——
        这保证从旧版本升级后，**同一批不会被重复加一次**。
        """
        entry = self._person_entry(date_key, file_id, name, phone)
        if entry is None:
            return None
        for key in ("local", "meals"):
            if key in entry:
                return _to_int(entry[key])
        return None

    def synced_slots(self, date_key: str, file_id: str,
                     name: str, phone: str) -> list[int] | None:
        """查"某人本批**每个槽位**已同步的本地餐次"；没有记录返回 ``None``。

        槽位 = 本地表的第几行 = 云端这个人的第几行。一个人一天下两单时本地两行、
        云端两行，所以幂等也要按行记：本地第 2 行对应账本第 2 个槽位。

        旧版账本（一人一行时代）只有 ``local``/``meals``：整体当成第 1 个槽位。
        """
        entry = self._person_entry(date_key, file_id, name, phone)
        if entry is None:
            return None
        slots = entry.get("slots")
        if isinstance(slots, (list, tuple)):
            return [int(value) for value in slots]
        for key in ("local", "meals"):
            if key in entry:
                value = _to_int(entry[key])
                return None if value is None else [value]
        return None

    def synced_total(self, date_key: str, file_id: str,
                     name: str, phone: str) -> int | None:
        """查"上次写入后的云端总餐次"（仅审计/排查用；旧版账本没有这个字段）。"""
        entry = self._person_entry(date_key, file_id, name, phone)
        if entry is None or "total" not in entry:
            return None
        return _to_int(entry["total"])

    def synced_meals(self, date_key: str, file_id: str,
                     name: str, phone: str) -> int | None:
        """历史名字，等价于 :meth:`synced_local`（保留给已有调用与测试）。"""
        return self.synced_local(date_key, file_id, name, phone)

    def record(self, date_key: str, file_id: str,
               entries: Mapping[str, int | Mapping[str, int]]) -> None:
        """把本次写入后的状态记进账本（键为 ``姓名\\u0000电话``），并刷新批次时间。

        值可以是 ``{"local": 本地餐次合计, "slots": [每行餐次], "total": 槽位总餐次之和}``，
        也可以是单个整数（= 本地餐次合计，兼容旧调用）。``slots`` 是幂等锚点：
        本地第 i 行对应第 i 个槽位，重复上传时逐个槽位算增量。
        """
        batch = self._batch(date_key, file_id)
        stamp = _dt.datetime.now().isoformat(timespec="microseconds")
        batch["synced_at"] = stamp
        people = batch["people"]
        for key, value in entries.items():
            if isinstance(value, Mapping):
                slots = value.get("slots")
                if isinstance(slots, (list, tuple)):
                    clean = [_to_int(item) or 0 for item in slots]
                    payload: dict[str, Any] = {"local": sum(clean), "slots": clean,
                                               "at": stamp}
                else:
                    payload = {"local": _to_int(value.get("local")) or 0, "at": stamp}
                total = _to_int(value.get("total"))
                if total is not None:
                    payload["total"] = total
            else:
                payload = {"local": _to_int(value) or 0, "at": stamp}
            people[key] = payload

    def batch_summary(self, date_key: str, file_id: str) -> dict[str, Any] | None:
        """某天某表的批次摘要 ``{synced_at, people}``；没有批次返回 ``None``。"""
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
        """kdocs-cli 是否已授权（跑 ``auth status``）；命令缺失或超时按未授权处理。"""
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
        """读取在线表格的子表信息列表（``sheetsInfo``）；读不到返回空列表。"""
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
        """列出云盘目录下的文件；接口两种返回形态都兼容，取不到时返回空列表。"""
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
    """把 1-based 列号转成 Excel 列名（1 → ``A``，27 → ``AA``）。"""
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


def _batch_date_check(orders: Sequence[CloudOrder],
                      target: _dt.date) -> tuple[str, str]:
    """核对本地表的批次日期，返回 ``(拒绝原因, 警告)``（两者最多一个非空）。

    本地排单表每一行都标着「这批要送的那天是周几」（订单处理写入的是运行日+1
    的周几；实测 2026-09-18 那批全是「周六」，目标日期 9.19 正是周六）。

    为什么必须核对：总餐次改成"云端 + 本次增量"之后，拿**别的日期**的本地表去
    上传会把同一批餐重复加一遍。所以出现**明确矛盾**时整张表拒绝写入：

    * 有星期标记的行**一行都不标目标周几** → 返回拒绝原因（零云端写请求）；
    * 部分行不符 → 只警告（可能是个别行来自别的批次）；
    * 一行标记都没有（人工做的表）→ 只警告，照常写入。
    """
    expected = WEEKDAYS[target.weekday()]
    marked = [order for order in orders if order.weekday_marks]
    if not marked:
        if not orders:
            return "", ""
        return "", (f"本地表里没有星期标记，无法核对批次日期"
                    f"（目标日期 {target.isoformat()} 是{expected}），已照常写入")
    mismatched = [order for order in marked if expected not in order.weekday_marks]
    if not mismatched:
        return "", ""
    seen: list[str] = []
    for order in mismatched:
        for mark in order.weekday_marks:
            if mark not in seen:
                seen.append(mark)
    shown = "、".join(seen[:3])
    if len(mismatched) == len(marked):
        reason = (f"本地排单表的星期标记是「{shown}」，与本次目标日期 "
                  f"{target.isoformat()}（{expected}）不符：这张本地表是**别的日期**的批次，"
                  f"上传会把同一批餐重复加到云端。已拒绝写入本表 —— "
                  f"请先在「订单处理」里重跑当天订单，再点上传。")
        return reason, reason
    names = "、".join(order.name for order in mismatched[:3])
    return "", (f"本地表里 {len(mismatched)} 行的星期标记不是「{expected}」"
                f"（{names}…），这些行可能来自别的批次，已按目标日期写入，请核对")


def _group_rows_per_person(orders: Sequence[CloudOrder],
                           ) -> tuple[list[list[CloudOrder]], list[str]]:
    """把本地同一子表的行按【姓名 + 电话】分组，**一行一组内一个槽位**。

    用户 2026-09-18 明确：同一个人本地有几行，**云端就写几行**，每行各标当天 1 ——

    * 一晚下了两单（实测「雷丝淇」W25 + W5，各 1 餐）；
    * 一单两份被订单处理按份数写成两行（实测「柟」W33 两行）。

    两种都算"这一天吃两餐"：两行 = 两个槽位，各自的餐次记在各自那一行上，
    日期格都写 1。这样「闪时送下单」（按"日期格 == 1"出单）会真的送两餐。

    槽位顺序 = 组内顺序 = 本地表的行顺序；云端同一个人的多行按**表内从上到下**对应。
    """
    groups: dict[tuple[str, str], list[CloudOrder]] = {}
    result: list[list[CloudOrder]] = []
    for order in orders:
        key = person_key(order.name, order.phone)
        if not key[0]:
            result.append([order])
            continue
        group = groups.get(key)
        if group is None:
            groups[key] = [order]
            result.append(groups[key])
        else:
            group.append(order)
    warnings: list[str] = []
    for group in result:
        if len(group) < 2:
            continue
        first = group[0]
        rows = "、".join(
            str(row) for order in group for row in order.local_rows)
        total = sum(order.meals for order in group)
        warnings.append(
            f"「{first.name}」（{first.phone}）在本地表里有 {len(group)} 行"
            f"（第 {rows} 行）：云端写 {len(group)} 行、各标当天 1，这天共 {total} 餐")
        for order in group[1:]:
            for label, mine, theirs in (("地址", first.address, order.address),
                                        ("类型", first.meal_type, order.meal_type),
                                        ("餐种", first.meal_kind, order.meal_kind)):
                if mine and theirs and mine != theirs:
                    warnings.append(
                        f"「{first.name}」多行的{label}不一致（「{mine}」/「{theirs}」），"
                        f"每一行各按自己的值写入，请核对")
                    break
    return result, warnings


def _build_sheet_plan(cli: KdocsCli, *, sheet: str, orders: Sequence[CloudOrder],
                      conf: Mapping[str, str], target: _dt.date,
                      run_date: _dt.date | None,
                      order_map: Mapping[str, Sequence[str]],
                      sort_enabled: bool,
                      ledger: SyncLedger | None = None) -> SheetPlan:
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
    plan.ledger_digest = None
    if ledger is not None and getattr(ledger, "path", None):
        plan.ledger_digest = (getattr(ledger, "_loaded_digest", None)
                              or LEDGER_SNAPSHOT_ABSENT)
    # 批次日期核对放在**任何云端调用之前**：被拒绝的子表一个请求都不发（省额度）。
    block_reason, batch_warning = _batch_date_check(orders, target)
    if batch_warning:
        plan.warnings.append(batch_warning)
    if block_reason:
        plan.blocked_reason = block_reason
        return plan
    if ledger is not None:
        previous = ledger.batch_summary(target.isoformat(), plan.file_id)
        if previous:
            plan.previous_batch = (f"{previous['people']} 人"
                                   f"（{previous['synced_at'] or '时间未知'}）")
    infos = cli.sheets_info(plan.file_id)
    if not infos:
        plan.warnings.append("云端文件不可读或不是在线表格")
        return plan
    worksheet_id = int(infos[0].get("sheetId") or 1)
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

    # 客户索引 + 追加行。
    # 一个人可能有**多行**（多买的那几餐各自一行）：按表内从上到下的顺序记下来，
    # 本地第 i 行就写在他的第 i 行上（见 _group_rows_per_person）。
    people: dict[tuple[str, str], list[int]] = {}
    last_used = FIRST_DATA_ROW - 1
    for (row, col), text in grid.items():
        if row < FIRST_DATA_ROW - 1 or col != plan.columns["name"] - 1:
            continue
        last_used = max(last_used, row + 1)
        phone = grid.get((row, plan.columns["phone"] - 1), "")
        key = person_key(text, phone)
        if key[0]:
            people.setdefault(key, []).append(row + 1)
    for rows in people.values():
        rows.sort()
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
    # 恢复核对基线：只记“这一批云端有哪些人、在第几行”，用于证明“完全没执行”。
    phone_col = plan.columns.get("phone") or 0
    plan.baseline_name_rows = tuple(
        (row,
         str(grid.get((row - 1, plan.columns["name"] - 1), "") or "").strip(),
         person_key("", grid.get((row - 1, phone_col - 1), "") if phone_col else "")[1])
        for row in data_rows
    )

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
    type_col = plan.columns.get("type") or 0
    kind_col = plan.columns.get("kind") or 0
    served_col = plan.columns.get("served") or 0
    left_col = plan.columns.get("left") or 0
    date_key = target.isoformat()
    existing_changes: list[Change] = []
    # 需要**新建**的云端行：(本地行, 槽位, 人键, 该人是否完全不在云端)
    new_rows: list[tuple[CloudOrder, int, tuple[str, str], bool]] = []
    groups, group_warnings = _group_rows_per_person(orders)
    plan.warnings.extend(group_warnings)
    for group in groups:
        first_order = group[0]
        key = person_key(first_order.name, first_order.phone)
        if not key[0]:
            continue
        cloud_rows = people.get(key, [])
        is_new_person = not cloud_rows
        prev_slots = (ledger.synced_slots(date_key, plan.file_id, *key)
                      if ledger is not None else None)
        if is_new_person and any(name == first_order.name for name, _phone in people):
            plan.warnings.append(
                f"{first_order.name}：云端已有同名的人（电话不同），本次会新增 "
                f"{len(group)} 行，请核对是不是同一个人（重复行会让总餐次被加两次）")
        for slot, order in enumerate(group, start=1):
            local_rows = order.local_rows
            local_note = "、".join(str(row) for row in local_rows) or "?"
            if order.meals <= 0:
                # 本地「餐次」为空/0：既不能加餐，也不能写日期格 1（那会白送一餐）。
                plan.warnings.append(
                    f"{order.name}（{order.phone}）：本地第 {local_note} 行的「餐次」是空的"
                    f"（按 0 计），已跳过这一行：不写日期格、不动总餐次")
                continue
            # 账本按**槽位**记"本批已同步的本地餐次"：本地第 i 行对应账本第 i 个槽位。
            prev = (prev_slots[slot - 1]
                    if prev_slots is not None and slot - 1 < len(prev_slots) else None)
            added = order.meals - prev if prev is not None else order.meals
            if added < 0:
                plan.warnings.append(
                    f"{order.name}（第 {slot} 行）：本地 {order.meals} 餐少于账本里本批"
                    f"已同步的 {prev} 餐（可能退单），本次不动云端总餐次，请人工核对")
                added = 0
            if slot <= len(cloud_rows):
                # 云端已有这一行：总餐次 = **现值 + 本次增量**（累加，不是绝对值）
                row = cloud_rows[slot - 1]
                before = _as_int(grid.get((row - 1, total_col - 1))) if total_col else 0
                raw_mark = str(grid.get((row - 1, plan.target_col - 1), "") or "").strip()
                already = raw_mark == CELL_MARK
                # 协作者写的其它值（例如 0 = 当天不送）是他的明确决定，只读不写。
                occupied = "" if (already or not raw_mark) else raw_mark
                want = before + added
                change = Change(kind="existing", name=order.name, phone=order.phone,
                                row=row, delta=want - before, target_col=plan.target_col,
                                total_before=before, total_after=want, target_ok=already,
                                address=str(order.address or "").strip(),
                                meal_type=order.meal_type, meal_kind=order.meal_kind,
                                local_meals=order.meals, ledger_prev=prev,
                                target_occupied=occupied, local_rows=local_rows,
                                slot=slot, pre_row=row,
                                cloud_before_rows=tuple(cloud_rows))
                # 半成品行自愈：上次中断可能留下缺「类型/餐种/公式」的行，本次补齐。
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
                if occupied:
                    plan.warnings.append(
                        f"{order.name}：{plan.target_header or '目标日期'}那格协作者已经写了"
                        f"「{occupied}」（表示当天不送），本次不改这一格；总餐次仍按付款累加。"
                        f"若这天确实要送，请先在云端把那格改回 1")
                existing_changes.append(change)
                continue
            # 云端这个人还没有这一"行"（新客户，或本地又多下了一单）→ 新建一行
            if prev is not None:
                plan.warnings.append(
                    f"{order.name}（第 {slot} 行）：账本说这一行本批已同步 {prev} 餐，"
                    f"但云端没有这一行（可能被协作者删了），本次按本地 {order.meals} 餐重建")
            new_rows.append((order, slot, key, is_new_person))

    # ---- 新增客户：统一插到第 3 行与第 4 行之间，再按列B 顺序重排整张表 ----
    order_list = [str(item).strip() for item in (order_map.get(sheet) or [])]
    written: list[tuple[CloudOrder, str, int, tuple[str, str]]] = [
        (order, canonical_address(order.address, order_list), slot, key)
        for order, slot, key, _is_new in new_rows]

    ranks, tail_rank = build_address_ranks(
        order_list, [*address_of.values(), *(addr for _o, addr, _s, _k in written)])
    unknown = sorted({addr for addr in
                      [*address_of.values(), *(a for _o, a, _s, _k in written)]
                      if _address_key(addr) and _address_key(addr) not in ranks})
    # 清单外地址按设计排到表尾：**不再逐条报成风险**（每一行实际落到第几行
    # 在 changes 里已经能看到）。这里保留前 5 个作为结构化数据，只供排查用。
    plan.unknown_addresses = unknown[:5]

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
        # 宽度必须以**接口报的 used range** 为准，不能只看读到的内容：内容右侧的
        # 边框/底色即使没有文字也属于"表格内容"，辅助列插在那里会把它们推走。
        reported_last = max(1, int(infos[0].get("colTo") or 0) + 1)
        real_last_col = max(effective_last_col(reported_last),
                            content_last_col(grid, col_to + 1))
        helper_col = sort_key_column(
            sheet_col_to=real_last_col,
            extra_cols=[plan.marker_col, plan.columns.get("remark") or 0])
        if helper_col > MAX_SORT_COL:
            # used range 报得离谱（整列污染）或表真的过宽：无法保证排序区覆盖全部列，
            # 宁可不排序，也不要把行与列排错位。
            plan.warnings.append(
                f"表格宽度异常（接口报最后使用列 {reported_last}，辅助列 {helper_col}），"
                "本次跳过排序：新客户会留在表格最上面")
            sort_on = False
    elif new_count and not sort_enabled:
        plan.warnings.append(
            "已按设置关闭排序：新客户留在表格最上面，不会按地址归位")
    elif new_count and not data_rows:
        plan.warnings.append("表里还没有数据行，本次只写入新客户，无需排序")

    # 预测排序结果（稳定排序：等键保持源顺序，与云端 range-sort 的承诺一致）。
    # 排序前的物理顺序 = 新行（第 4 行起）+ 已有行（原有先后）。
    # 注意：插到"第 4 行之前"时**第 3 行不动**，第 4 行及以下才整体下移。
    def pre_insert_row(row: int) -> int:
        return row if row < first_insert_row else row + new_count

    entries: list[tuple[int, int]] = []
    if sort_on:
        for idx, (_order, addr, _slot, _key) in enumerate(written):
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

    # 预测行号按 (人, 槽位) 记：一个人多行时第 i 行 = 他的第 i 个槽位。
    final_rows_by_key: dict[tuple[str, str], dict[int, int]] = {}
    for change in existing_changes:
        old_pre_row = change.pre_row or change.row
        change.pre_row = pre_insert_row(old_pre_row)
        change.row = final_row_of.get(change.pre_row, change.row)
        final_rows_by_key.setdefault(
            person_key(change.name, change.phone), {})[change.slot] = change.row

    for idx, (order, addr, slot, key) in enumerate(written):
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
                        detail=where, fill_formula=True,
                        local_meals=order.meals,
                        ledger_prev=None,
                        local_rows=order.local_rows,
                        slot=slot, pre_row=pre_row)
        plan.changes.append(change)
        final_rows_by_key.setdefault(key, {})[slot] = row
    plan.final_rows = {key: tuple(rows[i] for i in sorted(rows))
                       for key, rows in final_rows_by_key.items()}
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
                                 order_map=order_map, sort_enabled=sort_enabled,
                                 ledger=ledger)

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
    """汇总计划：用于界面展示与日志。

    * ``to_update``：本次要更新的**老行**（总餐次变了，或要补类型/餐种/公式）
    * ``to_append``：本次要**新增的行**（新客户每人一行；同一天多下的单各占一行）
    * ``unchanged``：本次一个字都不用写的行
    * ``warned``：差额为负（本地比账本少，可能退单）的槽位数
    * ``skipped``：日期格被协作者写了别的值（例如 0 = 当天不送）而没动的行数
    * ``blocked``：整表被拒绝（批次日期不符）的子表数

    计数单位是**行**不是人：一天下两单的客户会有两行（两餐各一行）。
    """
    update = new = unchanged = warn = skipped = blocked = 0
    for plan in plans:
        if plan.blocked_reason:
            blocked += 1
        for change in plan.changes:
            if change.kind == "new":
                new += 1
            elif change.needs_write:
                update += 1
            else:
                unchanged += 1
            if change.target_blocked:
                skipped += 1
            if change.delta < 0:
                warn += 1
    return {"to_update": update, "to_append": new,
            "unchanged": unchanged, "warned": warn, "skipped": skipped,
            "blocked": blocked}


def _local_rows_text(change: Change) -> str:
    """预览里的本地出处，例如「本地第 19 行共 6 餐」。"""
    rows = "、".join(str(row) for row in change.local_rows) or "?"
    return f"本地第 {rows} 行共 {change.local_meals} 餐"


def _change_lines(change: Change) -> list[str]:
    """把一条变更渲染成 1~2 行预览文字（用户靠它决定"要不要真的上传"）。"""
    if change.kind == "new":
        # 同一个人的第 2、3…行是"这天多加的餐"，写清第几行，用户核对方便。
        slot_note = f"第 {change.slot} 行：" if change.slot > 1 else ""
        return [f"    + 新增 {change.name}（{change.phone}）{slot_note}"
                f"总餐次 {change.total_after}（{_local_rows_text(change)}）"
                f"、日期格 {CELL_MARK} → {change.detail}"]
    if not change.needs_write:
        lines = [f"    ≈ {change.name}：本次不需要改动（总餐次 {change.total_after}）"]
    else:
        added = change.total_after - change.total_before
        if change.ledger_prev is None:
            why = f"本批首次同步 +{added}" if added else "总餐次已一致"
        elif added:
            why = f"本批本地 {change.ledger_prev}→{change.local_meals} 餐，+{added}"
        else:
            why = f"本批本地 {change.local_meals} 餐已同步过，+0"
        cell = "" if (change.target_ok or change.target_blocked) else f"，日期格 {CELL_MARK}"
        slot_note = f"第 {change.slot} 行 " if change.slot > 1 else ""
        lines = [f"    · {change.name}（{change.phone}）{slot_note}"
                 f"总餐次 {change.total_before}→{change.total_after}"
                 f"（{why}；{_local_rows_text(change)}）{cell}"]
    if change.target_blocked:
        lines.append(f"        ⛔ 日期格协作者已写「{change.target_occupied}」"
                     f"（当天不送），本次不改这一格；要送请先在云端改回 1")
    return lines


def format_plan(plans: Iterable[SheetPlan]) -> str:
    """把计划渲染成人类可读的多行文本（给预览用）。"""
    lines: list[str] = []
    for plan in plans:
        head = f"【{plan.sheet}】目标日期 {plan.target_date} "
        if plan.target_col:
            head += f"→ 第 {plan.target_col} 列（{plan.target_header}）"
        lines.append(head)
        if plan.blocked_reason:
            lines.append("    ⛔ 本次未写入这张表（一个格子都没动）")
            lines.append(f"    {plan.blocked_reason}")
            for warning in plan.warnings:
                if warning != plan.blocked_reason:
                    lines.append(f"    ⚠ {warning}")
            continue
        if plan.previous_batch:
            lines.append(f"    本批已同步过 {plan.previous_batch}：重复上传只补差额")
        for warning in plan.warnings:
            lines.append(f"    ⚠ {warning}")
        new_count = sum(1 for c in plan.changes if c.kind == "new")
        if new_count:
            if plan.sort_enabled:
                lines.append(
                    f"    本次新增 {new_count} 行：先插到第 {FIRST_DATA_ROW + 1} 行起，"
                    f"再按地址顺序重排整张表"
                    f"（第 {FIRST_DATA_ROW}~{plan.last_data_row} 行）")
            else:
                lines.append(
                    f"    本次新增 {new_count} 行：插到第 {FIRST_DATA_ROW + 1} 行起"
                    f"（已关闭排序，新行会留在表格最上面）")
        for change in plan.changes:
            lines.extend(_change_lines(change))
        if not plan.changes:
            lines.append("    （无变更）")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 执行
# ----------------------------------------------------------------------

def _verify_inserted_block(cli: KdocsCli, plan: SheetPlan, worksheet_id: int, *,
                           first_row: int, count: int,
                           new_names: set[str]) -> str:
    """只读证明"这一段行确实只装着本次新增的人"。返回问题描述（空 = 通过）。

    为什么必须先证明再删：删除行是按**行号区间**执行的。如果协作者在我们插入之后
    又在同一区域插了行/重排过整表，那个区间里可能已经混入别人的数据 —— 直接删就
    会删掉协作者的行。这里只读比对两件事：

    1. 区间内的非空姓名必须全部属于本次新增的人（允许空行、允许我们还没写完）；
    2. 紧邻区间上下的行里不能出现本次新增的人（那说明表被重排过，行号已经不可信）。
    """
    name_col = plan.columns.get("name") or 0
    if not name_col or not count:
        return "缺少姓名列信息，无法证明待删除行的身份"
    try:
        grid = cli.read_grid(plan.file_id, worksheet_id,
                             first_row - 1, first_row - 1 + count - 1,
                             name_col - 1, name_col - 1)
        probe_from = max(FIRST_DATA_ROW, first_row - 2)
        probe_to = first_row + count  # 含区间下方一行
        around = cli.read_grid(plan.file_id, worksheet_id,
                               probe_from - 1, probe_to - 1,
                               name_col - 1, name_col - 1)
    except WpsCloudError as exc:
        return f"回滚前的只读核对失败：{exc}"

    inside = {str(text).strip() for (_row, _col), text in grid.items()
              if str(text).strip()}
    strangers = sorted(name for name in inside if name not in new_names)
    if strangers:
        return f"待删除区间里出现了非本次新增的人（{('、'.join(strangers[:3]))}）"

    outside = {str(text).strip() for (row, _col), text in around.items()
               if str(text).strip()
               and not (first_row <= row + 1 <= first_row + count - 1)}
    moved = sorted(name for name in outside if name in new_names)
    if moved:
        return (f"本次新增的人已经出现在区间之外（{('、'.join(moved[:3]))}），"
                f"说明表被重排过，行号不再可信")
    return ""


def _rollback_inserts(cli: KdocsCli, plan: SheetPlan, worksheet_id: int,
                      inserted: Sequence[tuple[int, int]],
                      emit: Callable[[str], Any],
                      *, new_names: set[str] | None = None
                      ) -> tuple[bool, int]:
    """插入成功但后续写入失败时，把插出来的行删掉，避免云端留下烂尾空行。

    从位置最靠后的块开始删（前面的删除不会影响后面的行号）。

    返回 ``(是否成功, 实际删除的行数)``：

    * 成功且删除行数为 0 → 从头到尾没写过（可以报"未写入"）；
    * 成功且删除行数 > 0 → **写过后完整回滚**。这**不等于**"从未写入"，
      报告里必须区分（见 :func:`_finalize_apply_result`）；
    * 失败 → 云端可能残留内容，必须按"结果不确定"处置。
    """
    if not inserted:
        return True, 0
    names = set(new_names or ())
    deleted = 0
    for first_row, count in sorted(inserted, reverse=True):
        if names:
            problem = _verify_inserted_block(cli, plan, worksheet_id,
                                            first_row=first_row, count=count,
                                            new_names=names)
            if problem:
                emit(f"[云同步] {plan.sheet}：{problem}；为避免删掉协作者的行，"
                     f"本次不回滚，请人工核对（重复上传前先确认云端状态）", "ERROR")
                return False, deleted
        try:
            cli.delete_rows(plan.file_id, worksheet_id, row=first_row, count=count)
        except WpsCloudError as exc:
            emit(f"[云同步] {plan.sheet}：回滚插入失败（第 {first_row} 行起 "
                 f"{count} 行可能残留空行，请人工检查）：{exc}")
            return False, deleted
        deleted += count
    emit(f"[云同步] {plan.sheet}：已回滚 {len(inserted)} 处插入（{deleted} 行），"
         f"云端恢复原状")
    return True, deleted


def read_person_rows(cli: KdocsCli, plan: SheetPlan, worksheet_id: int, *,
                     last_row: int) -> dict[tuple[str, str], list[int]]:
    """回读「姓名+电话」列，返回 ``{(姓名, 电话): [行号, ...]}``（1-based，升序）。

    整表排序后人行号全变了，必须靠这个重新定位到每个人的新行号。
    一个人可能有多行（多槽位），因此值是**行号列表**，按表内从上到下排列。
    只读 2 列，一次调用。
    """
    name_col = plan.columns.get("name") or 1
    phone_col = plan.columns.get("phone") or max(name_col + 1, 3)
    lo, hi = min(name_col, phone_col), max(name_col, phone_col)
    row_from = FIRST_DATA_ROW if last_row >= FIRST_DATA_ROW else FIRST_DATA_ROW
    grid = cli.read_grid(plan.file_id, worksheet_id,
                         row_from - 1, max(last_row, row_from) - 1, lo - 1, hi - 1)
    index: dict[tuple[str, str], list[int]] = {}
    for (row, col), text in grid.items():
        if col != name_col - 1 or not str(text).strip():
            continue
        phone = grid.get((row, phone_col - 1), "")
        index.setdefault(person_key(text, phone), []).append(row + 1)
    for rows in index.values():
        rows.sort()
    return index


def _change_key(change: Change) -> str:
    return f"{change.name}\u0000{change.phone}"


def _ledger_entries(changes: Sequence[Change], *,
                    only_keys: set[str] | None = None,
                    ledger: SyncLedger | None = None,
                    date_key: str = "", file_id: str = "") -> dict[str, dict[str, Any]]:
    """账本条目：每个受影响的人输出**与本地槽位等长**的 slots。

    绝不能只把本次 pending 的槽位写进去：若"第 1 槽已同步、第 2 槽新增"时把旧槽位
    截断，下一次上传会把第 1 槽的餐再全额加一遍。未被本次待写的槽位沿用旧锚点；
    本地槽位是权威顺序。
    """
    grouped: dict[str, list[Change]] = {}
    for change in changes:
        key = _change_key(change)
        if only_keys is not None and key not in only_keys:
            continue
        grouped.setdefault(key, []).append(change)
    entries: dict[str, dict[str, Any]] = {}
    for key, items in grouped.items():
        name, _separator, phone = key.partition("\u0000")
        old_slots = (ledger.synced_slots(date_key, file_id, name, phone)
                     if ledger is not None else None)
        by_slot = {int(change.slot): change for change in items}
        max_slot = max([0, *by_slot, *range(len(old_slots or []))])
        if max_slot <= 0:
            continue
        slots: list[int] = []
        for position in range(1, max_slot + 1):
            change = by_slot.get(position)
            if change is not None:
                slots.append(int(change.local_meals))
            elif old_slots is not None and position <= len(old_slots):
                slots.append(int(old_slots[position - 1]))
            else:
                slots.append(0)
        total = sum(int(change.total_after) for change in by_slot.values())
        entries[key] = {"slots": slots, "local": sum(slots), "total": total}
    return entries


def _target_before(change: Change) -> str:
    """计划构建时目标日期格的原值（写前基线；``1`` 表示本来就有）。"""
    if change.target_ok:
        return CELL_MARK
    if change.target_occupied:
        return str(change.target_occupied)
    return ""


def _target_expected(change: Change) -> str:
    """写入后目标日期格应有的值：协作者占用的格子保持原值。"""
    if change.target_occupied:
        return str(change.target_occupied)
    return CELL_MARK


def _intent_for_change(plan: SheetPlan, change: Change, index: int) -> dict[str, Any]:
    """一条变更的写前意图（journal 用：恢复时靠它比对云端现状）。"""
    columns = plan.columns or {}
    formula_needed = bool(
        (change.kind == "new" or change.fill_formula)
        and columns.get("served") and columns.get("left")
        and columns.get("total") and plan.date_cols)
    return {
        "kind": change.kind,
        "name": change.name,
        "phone": change.phone,
        "phone_key": person_key(change.name, change.phone)[1],
        "slot": int(change.slot),
        "total_before": int(change.total_before),
        "total_after": int(change.total_after),
        "target_before": _target_before(change),
        "target_expected": _target_expected(change),
        "target_occupied": str(change.target_occupied or ""),
        "address": str(change.address or ""),
        "meal_type": str(change.meal_type or ""),
        "meal_kind": str(change.meal_kind or ""),
        "fill_type": bool(change.fill_type),
        "fill_kind": bool(change.fill_kind),
        "fill_formula": bool(change.fill_formula),
        "formula_needed": formula_needed,
        "needs_write": bool(change.needs_write),
        "local_meals": int(change.local_meals),
        "cloud_before_count": len(change.cloud_before_rows or ()),
        "row_hint": int(change.row or 0),
        "pre_row": int(change.pre_row or change.insert_row or 0),
        "sort_token": f"t{index:04d}",
    }


def _sheet_intent(plan: SheetPlan, ledger: SyncLedger | None) -> dict[str, Any]:
    """一张表的写前意图记录（期望值只来自计划，不猜云端）。"""
    pending_seed = [change for change in plan.changes if change.needs_write]
    affected = {_change_key(change) for change in pending_seed}
    date_key = plan.target_date.isoformat() if plan.target_date else ""
    entries = _ledger_entries(plan.changes, only_keys=affected, ledger=ledger,
                              date_key=date_key, file_id=plan.file_id)
    return {
        "sheet": plan.sheet,
        "file_id": plan.file_id,
        "target_date": date_key,
        "target_col": int(plan.target_col or 0),
        "target_header": str(plan.target_header or ""),
        "weekday_number": int(plan.weekday_number or 0),
        "marker_col": int(plan.marker_col or 0),
        "columns": {str(key): int(value or 0)
                    for key, value in (plan.columns or {}).items()},
        "date_cols": [int(col) for col in plan.date_cols],
        "sort_enabled": bool(plan.sort_enabled and plan.sort_key_col and plan.row_keys),
        "sort_key_col": int(plan.sort_key_col or 0),
        "sort_range": str(plan.sort_range or ""),
        "last_data_row": int(plan.last_data_row or 0),
        "append_row": int(plan.append_row or 0),
        "baseline_name_rows": [[int(row), str(name), str(phone)]
                               for row, name, phone in plan.baseline_name_rows],
        "intents": [_intent_for_change(plan, change, index)
                    for index, change in enumerate(plan.changes)],
        "ledger_entries": entries,
        # 审计来源：本记录只代表本地意图；回读/恢复成功后会更新。
        "cloud_checked": False,
        "evidence": "local_journal",
    }


def _ledger_disk_digest(ledger: SyncLedger | None) -> str | None:
    """重新读磁盘账本并返回它的摘要；读不到（损坏）返回 ``None``。

    文件**尚不存在**时返回 :data:`LEDGER_SNAPSHOT_ABSENT`（而不是 ``None``）：
    否则"预览时还没有账本、执行时也还没有"会被误判成"账本被人改过"，
    首次上传就永远被自己的 stale 检查挡住。
    """
    if ledger is None or not getattr(ledger, "path", None):
        return None
    try:
        fresh = SyncLedger(ledger.path)
    except WpsCloudError:
        return None
    return getattr(fresh, "_loaded_digest", None) or LEDGER_SNAPSHOT_ABSENT


def _operation_guard(ledger: SyncLedger | None):
    """整次 apply_plan 的跨进程排他锁；没有账本路径时退化为无锁上下文。"""
    if ledger is None or not getattr(ledger, "path", None):
        return _NullContext()
    return FileLock(operation_lock_path_for(ledger.path), timeout=30.0)


def _classify_after_failure(cli: KdocsCli, journal: Any, operation_id: str,
                            plan: SheetPlan, *, rollback_ok: bool,
                            rolled_back_rows: int = 0
                            ) -> tuple[str, dict[str, Any]]:
    """回滚之后的只读核对：靠意图日志逐格证明"云端有没有被写进去"。

    产生 ``failed_no_write`` 的条件**不是**"我们删回了插入行"，而是"只读云端
    逐格比对后确认与写前基线一致"。证明不了就按 ``uncertain`` 处置 —— 未知的
    写入结果绝不能变成"可以自动重传"。

    返回 ``(journal 状态, 附加字段)``，状态取值 ``failed_no_write`` /
    ``verified`` / ``uncertain``。

    ``rolled_back_rows`` > 0 表示"写进去过、但已经完整删回基线"：云端状态确实
    回到了写前，但**这不是"从未写入"**，报告层据此不声明 ``proven_no_write``。
    """
    base: dict[str, Any] = {"cloud_checked": True,
                            "evidence": "executor_cloud_readback"}
    if rolled_back_rows:
        base["rolled_back"] = True
        base["rolled_back_rows"] = int(rolled_back_rows)
    if journal is None or not operation_id:
        return ("failed_no_write" if rollback_ok else "uncertain"), base
    try:
        from .wps_recovery import classify_journal_sheet
        record = ((journal.get_operation(operation_id) or {}).get("sheets") or {}
                  ).get(plan.sheet)
        classified = classify_journal_sheet(cli, record or {})
    except Exception as exc:  # noqa: BLE001 - 恢复核对失败必须不确定
        base["problems"] = [f"recovery_error:{type(exc).__name__}:{exc}"]
        return "uncertain", base
    state = str(classified.get("state") or "uncertain")
    if state == "verified":
        return "verified", base
    if rollback_ok and state == "not_started":
        return "failed_no_write", base
    problems = list(classified.get("problems") or [])
    if not rollback_ok:
        problems.append("插入行回滚删除失败：云端可能残留空行，严禁自动重试")
        base["manual_required"] = "rollback_delete_failed"
    base["problems"] = problems[:20]
    return "uncertain", base


def _advance_batch_digest(ledger: SyncLedger | None,
                           expected: str | None) -> str | None:
    """本批成功记账后推进"本批应有的账本版本"。

    推进依据是**磁盘上的实际内容**（而不是"我们以为写了什么"）：如果另一个进程
    恰好也在改账本，这里读到的就是合并后的版本，下一次闸门比较自然以它为准。
    """
    if expected is None or ledger is None:
        return expected
    return _ledger_disk_digest(ledger) or expected


def _commit_ledger(ledger: SyncLedger, plan: SheetPlan,
                   entries: Mapping[str, Any]) -> tuple[bool, str]:
    """把本次写入的槽位锚点落进账本；失败回滚内存快照并如实返回原因。

    跨进程安全：走 :meth:`SyncLedger.merge_entries`（持锁重读磁盘再合并），
    避免另一个进程/窗口的账本更新被这次写入覆盖。
    """
    if not entries:
        return True, ""
    snapshot = copy.deepcopy(ledger.data)
    snapshot_digest = getattr(ledger, "_loaded_digest", None)
    date_key = plan.target_date.isoformat() if plan.target_date else ""
    try:
        ledger.merge_entries(date_key, plan.file_id, entries)
    except BaseException as exc:  # noqa: BLE001 - 磁盘写失败要如实报告
        ledger.data = snapshot
        if hasattr(ledger, "_loaded_digest"):
            ledger._loaded_digest = snapshot_digest
        if not isinstance(exc, Exception):
            raise
        return False, f"ledger_save_failed:{type(exc).__name__}:{exc}"
    return True, ""


class _NullContext:
    """无账本场景的空上下文管理器（避免分支里出现 ``nullcontext`` 依赖）。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def apply_plan(cli: KdocsCli, plans: Iterable[SheetPlan], *,
               ledger: SyncLedger | None = None,
               marker_enabled: bool = True,
               log: Callable[[str], Any] | None = None,
               journal: Any = None,
               operation_id: str = "") -> dict[str, Any]:
    """按计划写入云端；写入后回读校验；成功才更新账本。

    单张表的执行顺序：
      1. 新客户统一插到第 3 行与第 4 行之间；
      2. 写新行的姓名/地址/电话/类型/餐种（与行号无关，排序后跟着行走）；
      3. 上新行底色（经济餐照抄模板、豪华餐整行金黄）；
      4. 写排序辅助列 → range-sort 按地址顺序重排整表 → 删掉辅助列；
      5. 回读列A~C，按（姓名,电话）重新定位每个人的**排序后行号**；
      6. 写日期格/总餐次/已出餐剩余餐公式/通讯记号；
      7. 回读校验 + 记账本。

    回滚红线：**只有排序之前**的失败才回滚（把插进去的行删掉）；排序一旦成功，
    新行已散落到各地址组里，此时删行会删错人 —— 只告警，让用户重传（重复执行安全）。

    ``journal`` 传入 :class:`app.wps_journal.SyncJournal` 时：整批在被写入之前
    先把意图落盘（``planned``），逐表推进到 ``writing`` / ``verified`` /
    ``failed_no_write`` / ``uncertain``，使崩溃或部分失败后能只读对账并人工处置。
    同时：同日期 + 同云表若还有未解除的防重复闸门（``retire_guarded``），
    本表直接拒绝写入；计划构建时所用的账本快照若已变化，同样拒绝写入。
    """
    emit = log or (lambda _msg, _level="INFO": None)
    plan_list = list(plans)
    with _operation_guard(ledger):
        return _apply_plan_locked(cli, plan_list, ledger=ledger,
                                  marker_enabled=marker_enabled, emit=emit,
                                  journal=journal, operation_id=operation_id)


def _guard_block_reason(plan: SheetPlan, journal: Any,
                        ledger: SyncLedger | None,
                        operation_id: str = "",
                        expected_digest: str | None = None) -> str:
    """写入前的三道只读闸门。返回拒绝原因（空 = 放行）。

    1. **未处置的同目标写入**（``planned``/``writing``/``ledger_pending``/
       ``uncertain``）：上一次写这张表的结果没能确认。这是重复累加的最后一道防线 ——
       如果那次其实写成功了，这一次再写就会把同一批餐加两遍；
    2. **仍保留的防重复闸门**（``retired_guarded``）：已退出待处理队列，
       但同日期 + 同表的重放仍然被阻断，只有实际云端核对才能解除；
    3. **账本快照过期**：预览之后账本被别的动作改过，本次写入的增量基准不可信。

    ``operation_id`` 是本次正在执行的 operation，必须排除 —— 否则它自己刚建出来的
    ``planned`` 记录会把本批挡在门外（自锁）。
    """
    target_key = plan.target_date.isoformat() if plan.target_date else ""
    if journal is not None and target_key:
        blockers = journal.blocking_operations(
            target_key, plan.file_id, exclude_operation_id=operation_id)
        if blockers:
            pending_ids = [op_id for op_id, op in blockers.items()
                           if not (bool(op.get("retired_guarded"))
                                   or str(op.get("status") or "") == "retired_guarded")]
            if pending_ids:
                return ("这张表还有未处置的同目标写入（结果未知："
                        + "、".join(sorted(pending_ids))
                        + "）。如果上一次其实写成功了，本次再写会把餐次重复累加 —— "
                        "请先在「云同步恢复」里对旧任务做只读核对并处置后再上传。")
            return ("这张表还有未解除的防重复闸门（同日期同表的上一次写入结果未知）。"
                    "请先在「云同步恢复」里对旧任务做只读核对并处置后再上传。")
    if journal is not None and ledger is not None and expected_digest is not None:
        # 只比"计划构建时的快照"是错的：本批第一张表成功后**自己**改了账本，
        # 后续表就会被自己的合法记账判成过期（六张表的真实配置下只有第一张能写）。
        # 因此比较对象是"本批当前应有的版本"（expected_digest），它随本批每张表
        # 成功记账而推进；任何**外部**变化仍然会被这一比较挡住。
        current = _ledger_disk_digest(ledger)
        if current != expected_digest:
            return ("本地同步账本在本次上传期间被别的动作改过（另有上传/恢复动作）。"
                    "为避免把同一批餐重复加到云端，本次拒绝写入这张表，请重新预览。")
    return ""


def _apply_plan_locked(cli: KdocsCli, plans: Sequence[SheetPlan], *,
                       ledger: SyncLedger | None,
                       marker_enabled: bool,
                       emit: Callable[..., Any],
                       journal: Any = None,
                       operation_id: str = "") -> dict[str, Any]:
    """``apply_plan`` 的实际实现（调用方已持有跨进程操作锁）。"""
    result: dict[str, Any] = {"sheets": [], "written": 0, "failed": 0}
    if journal is not None and not operation_id:
        from .wps_journal import new_operation_id
        operation_id = new_operation_id()
    if journal is not None:
        result["operation_id"] = operation_id
        result["journal_path"] = str(getattr(journal, "path", "") or "")
        # 意图必须在**任何云端写入之前**落盘：崩溃后才知道"本来要写什么"。
        journal.create_operation(
            operation_id,
            {plan.sheet: _sheet_intent(plan, ledger) for plan in plans},
            target_date=(plans[0].target_date.isoformat()
                         if plans and plans[0].target_date else ""))
        try:
            journal.save()
        except Exception as exc:  # noqa: BLE001 - 意图不可持久化就不能写云端
            emit(f"[云同步] 意图日志保存失败（{exc}）：已停止，一个字都不会写云端",
                 "ERROR")
            result["sheets"] = [{"sheet": plan.sheet, "status": "blocked",
                                 "reason": f"意图日志不可写：{exc}"} for plan in plans]
            result["failed"] = len(result["sheets"])
            result["journal_error"] = str(exc)
            return result
    # 本批合法的账本版本：起始 = 计划构建时看到的快照（整批必须一致），
    # 之后每张表成功记账都会推进它。外部写入不会被计入，因此仍会被闸门挡住。
    batch_digest = next((plan.ledger_digest for plan in plans
                         if getattr(plan, "ledger_digest", None) is not None), None)
    if batch_digest is not None:
        mismatched = [plan.sheet for plan in plans
                      if getattr(plan, "ledger_digest", None) not in (None, batch_digest)]
        if mismatched:
            emit("[云同步] 本批各表的账本基线不一致（" + "、".join(mismatched)
                 + "）：拒绝整批写入，请重新预览", "ERROR")
            for plan in plans:
                result["sheets"].append({
                    "sheet": plan.sheet, "status": "stale_batch",
                    "reason": "本批各表绑定的账本基线不一致（可能来自两次不同的预览）",
                    "cloud_writes": 0, "ledger_updates": 0})
                result["failed"] += 1
            return _finalize_apply_result(result)

    for plan in plans:

        def mark(status: str, **fields: Any) -> None:
            """把某张表的状态写进意图日志；日志不可写只告警，不改变云端结论。"""
            if journal is None:
                return
            try:
                journal.set_sheet_status(operation_id, plan.sheet, status, **fields)
                journal.save()
            except Exception as exc:  # noqa: BLE001
                emit(f"[云同步] {plan.sheet}：意图日志更新失败（{exc}）", "WARN")

        if plan.blocked_reason:
            # 批次日期闸门：**一个云端请求都不发**（连 sheets_info 都不调用），
            # 写请求 = 0、账本更新 = 0、新行插入 = 0。
            emit(f"[云同步] {plan.sheet}：整表拒绝写入 —— {plan.blocked_reason}", "ERROR")
            mark("not_started", reason=plan.blocked_reason)
            result["sheets"].append({"sheet": plan.sheet, "status": "blocked",
                                     "reason": plan.blocked_reason,
                                     "cloud_writes": 0, "ledger_updates": 0})
            result["blocked"] = int(result.get("blocked", 0)) + 1
            continue
        guard_reason = _guard_block_reason(plan, journal, ledger,
                                           operation_id=operation_id,
                                           expected_digest=batch_digest)
        if guard_reason:
            emit(f"[云同步] {plan.sheet}：拒绝写入 —— {guard_reason}", "ERROR")
            mark("not_started", reason=guard_reason)
            result["sheets"].append({"sheet": plan.sheet, "status": "stale_batch",
                                     "reason": guard_reason,
                                     "cloud_writes": 0, "ledger_updates": 0})
            result["failed"] += 1
            continue
        if not plan.target_col:
            mark("not_started", reason="未找到目标日期列")
            result["sheets"].append({"sheet": plan.sheet, "status": "skipped",
                                     "reason": "未找到目标日期列",
                                     "cloud_writes": 0, "ledger_updates": 0})
            continue
        infos = cli.sheets_info(plan.file_id)
        if not infos:
            mark("failed_no_write", reason="云端文件不可读")
            result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                     "reason": "云端文件不可读", "cloud_writes": 0,
                                     "ledger_updates": 0})
            result["failed"] += 1
            continue
        worksheet_id = int(infos[0].get("sheetId") or 1)
        if any(change.needs_write for change in plan.changes):
            # 第一次真正写入之前先把状态推进到 writing：崩溃后能看出"这张表动过"。
            # worksheet_id 也必须记住：恢复时的只读核对要靠它定位子表。
            mark("writing", worksheet_id=worksheet_id)

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
                # 服务端可能**已经插入成功**而客户端只拿到了超时/断连。直接报
                # "未写入"会在下一次上传时重复插行，所以这里一定先只读核对：
                # 只有证明云端仍与写前基线一致，才允许说失败且零写入。
                emit(f"[云同步] {plan.sheet} 插入新行失败：{exc}；正在只读核对云端")
                status, fields = _classify_after_failure(
                    cli, journal, operation_id, plan, rollback_ok=True)
                if status == "failed_no_write" and _looks_like_transport_error(exc):
                    # 超时/断连/网关错误：服务端**可能已经插过行**（例如插入了空行），
                    # 本地读回也看不见这种痕迹 —— 不能声明"未写入"。
                    status = "uncertain"
                    fields["manual_required"] = "insert_may_have_landed"
                    fields["problems"] = list(fields.get("problems") or []) + [
                        "插入请求报错发生在传输层：服务端可能已经插入过空行，"
                        "本地读回无法证明；请人工核对表结构后再决定是否重传"]
                if status == "verified":
                    mark("verified", reason=f"插入回报异常但云端期望值已完整：{exc}",
                         **fields)
                    result["sheets"].append({"sheet": plan.sheet, "status": "ok",
                                             "people": 0, "warning": "插入异常但云端已完整",
                                             "ledger_updates": 0})
                    result["written"] += 1
                    continue
                mark(status, reason=f"插入新行失败：{exc}", **fields)
                result["sheets"].append({
                    "sheet": plan.sheet, "status": "failed",
                    "reason": f"插入新行失败：{exc}",
                    "uncertain": status != "failed_no_write",
                    "cloud_writes": (0 if status == "failed_no_write" else None),
                    "ledger_updates": 0})
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
                rolled_back, deleted = _rollback_inserts(
                    cli, plan, worksheet_id, inserted, emit,
                    new_names={c.name for c in new_changes})
                emit(f"[云同步] {plan.sheet} 新客户行写入失败：{exc}")
                status, fields = _classify_after_failure(
                    cli, journal, operation_id, plan, rollback_ok=rolled_back,
                    rolled_back_rows=deleted)
                if status == "verified":
                    mark("verified", reason=str(exc), **fields)
                    result["sheets"].append({"sheet": plan.sheet, "status": "ok",
                                             "people": 0, "cloud_writes": None,
                                             "warning": "写入异常但云端期望值已完整"})
                    result["written"] += 1
                    continue
                mark(status, reason=str(exc), rollback_ok=rolled_back,
                     **fields)
                result["sheets"].append({
                    "sheet": plan.sheet, "status": "failed", "reason": str(exc),
                    "uncertain": status != "failed_no_write",
                    # 写过后完整回滚 ≠ 从未写入：不能声明零写入（§R10）。
                    "rolled_back": deleted > 0,
                    "cloud_writes": (0 if (status == "failed_no_write" and not deleted)
                                     else None),
                    "ledger_updates": 0})
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
            # 4a) 排序区必须覆盖表里**所有**内容列：辅助列右边若还有内容，说明它在
            #     读取范围（MAX_SCAN_COL）之外，排序会让行与那些列错位。此时拒绝排序并
            #     回滚插入 —— 宁可不排序，也不能把表排坏。
            safe, found_at = probe_sort_area(
                cli, plan, worksheet_id, col_from=plan.sort_key_col + 1,
                row_to=max(plan.last_data_row, FIRST_DATA_ROW))
            if not safe:
                rolled_back, deleted = _rollback_inserts(
                    cli, plan, worksheet_id, inserted, emit,
                    new_names={c.name for c in new_changes})
                reason = (f"第 {plan.sort_key_col} 列右侧（{found_at}）还有内容，"
                          "排序区覆盖不到它，已放弃排序以免行与列错位")
                emit(f"[云同步] {plan.sheet}：{reason}；新客户行已回滚，"
                     "请先清理该列内容后重新上传", "ERROR")
                status, fields = _classify_after_failure(
                    cli, journal, operation_id, plan, rollback_ok=rolled_back,
                    rolled_back_rows=deleted)
                mark("uncertain" if status == "verified" else status,
                     reason=reason, rollback_ok=rolled_back,
                     **fields)
                result["sheets"].append({
                    "sheet": plan.sheet, "status": "failed", "reason": reason,
                    "uncertain": status != "failed_no_write",
                    "rolled_back": deleted > 0})
                result["failed"] += 1
                continue
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
                rolled_back, deleted = _rollback_inserts(
                    cli, plan, worksheet_id, inserted, emit,
                    new_names={c.name for c in new_changes})
                emit(f"[云同步] {plan.sheet} 按地址排序失败：{exc}")
                status, fields = _classify_after_failure(
                    cli, journal, operation_id, plan, rollback_ok=rolled_back,
                    rolled_back_rows=deleted)
                reason = f"按地址排序失败：{exc}"
                mark("uncertain" if status == "verified" else status,
                     reason=reason, rollback_ok=rolled_back,
                     **fields)
                result["sheets"].append({
                    "sheet": plan.sheet, "status": "failed", "reason": reason,
                    "uncertain": status != "failed_no_write",
                    "rolled_back": deleted > 0})
                result["failed"] += 1
                continue
            emit(f"[云同步] {plan.sheet}：已按地址顺序重排第 {FIRST_DATA_ROW}~"
                 f"{plan.last_data_row} 行")
            # 4b) 排序后复查一次：确实错位了就不要继续往这些行写数据。
            safe, found_at = probe_sort_area(
                cli, plan, worksheet_id, col_from=plan.sort_key_col + 1,
                row_to=max(plan.last_data_row, FIRST_DATA_ROW))
            if not safe:
                emit(f"[云同步] {plan.sheet}：排序后第 {plan.sort_key_col} 列右侧出现内容"
                     f"（{found_at}），说明排序区未覆盖全部列，已停止写入后续数据。"
                     "表已重排，请重新上传（重复执行安全）", "ERROR")
                reason = f"排序区未覆盖全部列（右侧有内容：{found_at}）"
                mark("uncertain", reason=reason)
                result["sheets"].append({
                    "sheet": plan.sheet, "status": "failed",
                    "reason": reason, "uncertain": True})
                result["failed"] += 1
                continue
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
                mark("uncertain", reason=f"排序后回读失败：{exc}")
                result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                         "reason": f"排序后回读失败：{exc}",
                                         "uncertain": True})
                result["failed"] += 1
                continue
            missing = [c for c in plan.changes
                       if person_key(c.name, c.phone) not in actual_rows]
            if missing:
                names = "、".join(c.name for c in missing[:5])
                emit(f"[云同步] {plan.sheet} 排序后定位不到这些人：{names}，已停止写入",
                     "ERROR")
                mark("uncertain", reason=f"排序后定位不到：{names}")
                result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                         "reason": f"排序后定位不到：{names}"})
                result["failed"] += 1
                continue
            drift = 0
            for change in plan.changes:
                key = person_key(change.name, change.phone)
                rows = actual_rows[key]
                # 一个人多行（多槽位）：第 i 个槽位对应他第 i 行，不能取同一个行号。
                position = max(1, int(change.slot or 1)) - 1
                row = rows[position] if position < len(rows) else rows[-1]
                predicted = plan.final_rows.get(key)
                if predicted and position < len(predicted) and predicted[position] != row:
                    drift += 1
                    if drift <= 3:
                        emit(f"[云同步] {plan.sheet}：{change.name} 第 {change.slot} 行"
                             f"实际排在第 {row} 行，与预测不符（按实际行号写入）", "WARN")
                change.row = row
            if drift:
                plan.sort_mismatch = True
                emit(f"[云同步] {plan.sheet}：{drift} 行的实际行号与预测不同"
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
            if not change.target_ok and not change.target_blocked:
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
            mark("verified", reason="noop")
            result["sheets"].append({"sheet": plan.sheet, "status": "noop",
                                     "people": 0, "cloud_writes": 0,
                                     "ledger_updates": 0})
            continue
        try:
            cli.write_cells(plan.file_id, worksheet_id, cells)
        except WpsCloudError as exc:
            note = "（表已按地址重排，请重新上传；重复执行是安全的）" if plan.sort_enabled else ""
            emit(f"[云同步] {plan.sheet} 写入失败：{exc}{note}", "ERROR")
            mark("uncertain", reason=str(exc))
            result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                     "reason": str(exc), "uncertain": True})
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
                mark("uncertain", reason=f"写入完成但回读失败：{exc}")
                result["sheets"].append({"sheet": plan.sheet, "status": "verify_unreadable",
                                         "reason": f"写入完成但回读失败：{exc}",
                                         "uncertain": True})
                if ledger is not None and pending:
                    # 数据已写入，账本仍要记，避免下次重复写入
                    ok, detail = _commit_ledger(
                        ledger, plan, _ledger_entries(
                            plan.changes, only_keys={_change_key(c) for c in pending},
                            ledger=ledger, date_key=plan.target_date.isoformat(),
                            file_id=plan.file_id))
                    if not ok:
                        mark("ledger_pending", reason=detail)
                    batch_digest = _advance_batch_digest(ledger, batch_digest)
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
                expected_mark = (change.target_occupied if change.target_blocked
                                 else CELL_MARK)
                if got_mark != expected_mark:
                    problems.append(f"{change.name}: 目标列应为 {expected_mark!r}，"
                                    f"实际 {got_mark!r}")
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
                mark("uncertain", reason="写入后回读校验未通过", problems=problems[:20])
                result["sheets"].append({"sheet": plan.sheet, "status": "verify_failed",
                                         "problems": problems[:20], "uncertain": True})
                result["failed"] += 1
                continue
        ledger_error = ""
        if ledger is not None and pending:
            # 账本记录本次写入后每个人的**每槽位本地餐次**（幂等锚点）与总餐次。
            ok, detail = _commit_ledger(
                ledger, plan, _ledger_entries(
                    plan.changes, only_keys={_change_key(c) for c in pending},
                    ledger=ledger, date_key=plan.target_date.isoformat(),
                    file_id=plan.file_id))
            if not ok:
                ledger_error = detail
                emit(f"[云同步] {plan.sheet}：数据已写入但账本没记上（{detail}），"
                     f"下一次上传可能重复加餐，请不要直接重传", "ERROR")
            batch_digest = _advance_batch_digest(ledger, batch_digest)
        sort_skipped = ""
        if new_changes and not plan.sort_enabled:
            # 具体原因（设置里关了排序 / 表格结构检查没过）由 plan.warnings 给出，
            # 这里只保证"确实没排序"这件事在日志里有一句醒目的话。
            sort_skipped = "本次未按地址重排整表：新客户留在表格最上面，原因见上方警告"
        mark("ledger_pending" if ledger_error else "verified",
             reason=ledger_error, cloud_checked=True,
             evidence="executor_cloud_readback")
        result["sheets"].append({"sheet": plan.sheet, "status": "ok",
                                 "cells": len(cells), "people": len(pending),
                                 "sorted": bool(plan.sort_enabled),
                                 "sort_skipped": sort_skipped,
                                 "sort_mismatch": bool(plan.sort_mismatch),
                                 "ledger_error": ledger_error,
                                 "ledger_updates": len(pending)})
        if ledger_error:
            result["failed"] += 1
            result.setdefault("ledger_errors", []).append(
                {"sheet": plan.sheet, "reason": ledger_error})
        result["written"] += 1
    if ledger is not None:
        try:
            ledger.save()
        except OSError as exc:
            emit(f"[云同步] 账本保存失败：{exc}")
    return _finalize_apply_result(result)


#: 逐表状态里"可以证明一个格子都没写"的那些（上界口径见 _finalize_apply_result）。
_NO_WRITE_STATUSES = frozenset({"blocked", "stale_batch", "skipped", "noop"})


def _finalize_apply_result(result: dict[str, Any]) -> dict[str, Any]:
    """补上整批口径的计数，并判定这次是否**已被证明**没有发生任何云端写入。

    ``proven_no_write`` 只在"每一张表都落在零写入终态"时为真：

    * ``blocked``（批次日期闸门/防重复闸门拒绝，一个请求都没发）；
    * ``stale_batch``（同上）；
    * ``skipped``（未找到目标日期列）；
    * ``noop``（回读确认无需改动）；
    * ``failed`` 但显式带 ``cloud_writes == 0``（回滚 + 只读核对证明未写入）；
    * ``failed`` 且带 ``uncertain`` 的**不算** —— 未知写入结果绝不能被说成零写入。

    逐表 ``cloud_writes`` / ``ledger_updates`` 是调用方明确给出的证据；缺省一律
    按"未知"处理（不猜、不美化）。
    """
    sheets = [item for item in (result.get("sheets") or []) if isinstance(item, dict)]
    proven = bool(sheets)
    ledger_updates = 0
    ledger_known = True
    for item in sheets:
        status = str(item.get("status") or "")
        if status in _NO_WRITE_STATUSES:
            continue
        if status == "failed" and item.get("cloud_writes") == 0 and not item.get("uncertain"):
            continue
        if status == "failed" and item.get("rolled_back"):
            # 写进去过、只是又删回了基线：这是"已恢复基线"，**不是**"从未写入"，
            # 因此这张表不能计入"可证明零写入"（上面的 continue 分支才是）。
            proven = False
            continue
        proven = False
    for item in sheets:
        value = item.get("ledger_updates")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            ledger_known = False
            break
        ledger_updates += value
    # 有写入（``written``>0）就没有"零写入"可言；完全没有表被处理时也无法证明。
    if result.get("written"):
        proven = False
    if not sheets:
        proven = False
    result["proven_no_write"] = bool(proven)
    result["cloud_writes_unknown"] = any(
        item.get("cloud_writes") is None for item in sheets)
    if proven:
        result.setdefault("cloud_writes", 0)
        result["ledger_updates"] = ledger_updates if ledger_known else None
    else:
        result.setdefault("cloud_writes", None)
        if "ledger_updates" not in result:
            result["ledger_updates"] = ledger_updates if ledger_known else None
    return result


# 传输层瞬时错误的特征词：这些值得重试（业务错误不重试）。
_TRANSIENT_HINTS = (
    "TLS handshake timeout", "connection reset", "connection refused",
    "i/o timeout", "EOF", "broken pipe", "no such host",
    "temporarily unavailable", "timeout awaiting response",
)


#: 中文/接口文案里的传输型线索：这些错误下"请求可能已经到达服务端"。
_TRANSPORT_HINTS = (
    "超时", "timeout", "timed out", "connection reset", "connection refused",
    "eof", "broken pipe", "i/o timeout", "no such host", "temporarily unavailable",
    "502", "503", "504", "bad gateway", "service unavailable", "无有效输出",
)


def _looks_like_transport_error(text: Any) -> bool:
    """粗判"这个错误可能发生在服务端已经收到请求之后"。

    用途只有一个：**插入行**报错时不能直接说"没写入"。超时/断连/网关错误下
    服务端可能已经插过行，必须按"写入结果未知"处置（保留闸门、等人工核对）；
    而"表头异常""没有权限"这类业务性拒绝则可以判定没有写入。
    """
    lowered = str(text or "").lower()
    return any(hint in lowered for hint in _TRANSPORT_HINTS)


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
