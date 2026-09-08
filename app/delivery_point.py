"""Delivery-address → pickup-point normalization for 东湖农林 & 医学院 orders.

平台后台保存的收货地址是「校区固定前缀 + 用户自由填写的楼栋/房间/备注」的
拼接，写法非常杂乱：混入人名、手机号、房间号、街道名、繁体、重复拼写等。
排单时人工只关心「放到哪个取餐点」。

输出口径（2026-09-08 与店主确认）：
- 东湖农林：宿舍楼输出字母楼码 ``A5``/``B5``/``C12``/``D1``；两个校门沿用现网
  ``大西``/``小西``；命名点输出文字（学三/学14/教2/图书馆/国重楼…）。
- 医学院：宿舍楼输出 ``医{N}号``（如 医5号/医6号）。
- 楼与校门并存时放宿舍楼下，取楼码不取校门（prefer_gate_when_both=False）。

规则层负责剥前缀、抽楼码/校门/命名点，能自动覆盖绝大多数订单；无法自动
判定的写法返回 medium/unknown 置信度，由上层人工确认一次并写入别名表，
之后同款写法全自动。所有函数均为纯函数，测试无需网络。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# 校区判定关键词（与 processing.ADDRESS_SHEET_MAP 保持同源语义）
CAMPUS_YIXUE_KEYWORDS = ("医学院", "杭州医学院", "medical college")
CAMPUS_DONGHU_KEYWORDS = ("东湖", "农林", "农大")

# 东湖校区字母宿舍区。E 区属衣锦联建；其余字母当异常。
DORM_ZONES = ("A", "B", "C", "D")
# 字母楼码统一大写（与现有排单一致）；若个别区习惯记小写可改为 False。
DORM_UPPER = True

# 医学院楼号命名模板：医{N}号。
YIXUE_BUILDING_TMPL = "医{n}号"

# 校门别名 → 标准点位（沿用现网排单短名）。
GATE_POINT = {
    "西南1门": "小西",   # 后台地址库把西南门存成"西南1门"
    "西南门": "小西",
    "小西门": "小西",
    "大西门": "大西",
    "西门": "大西",      # 单独"西门"更靠近大西，命中即 medium 复核
}

# 词面即可作取餐点名的命名建筑/别名 → 标准点名。
NAMED_POINTS = {
    "图书馆": "图书馆",
    "国重楼": "国重楼",
    "碳汇楼": "碳汇楼",
    "行政楼": "行政楼",
    "教2": "教2",
    "教二": "教2",
    "学14": "学14",
    "学三": "学三",
    "学3": "学三",
    "校门口": "校门口",
    "食堂": "食堂",
}

CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_CN_TABLE = str.maketrans("农東學區號間醫務體農", "农东学区号间医务体农")

_RE_MSG = re.compile(
    r"浙江农林大学|杭州医学院|浙农林大?|医学院|农林大?|Hangzhou Medical College[^ ]*"
    r"|Lin'an Campus|Lin'an District"
)
_RE_PLACE = re.compile(
    r"[\u4e00-\u9fff]{1,3}街道|武肃街\s*\d+\s*号|颐康街\s*\d+\s*号|浙江省|杭州市|临安区|锦北|锦城|锦南"
)
_RE_CAMPUS_SUFFIX = re.compile(r"(?:东湖|临安)校区|东湖校|临安校")
_RE_GATE = re.compile(r"(西南1门|西南门|小西门|大西门|西门)")
_RE_BLDG = re.compile(r"(?<![A-Za-z0-9])([A-Za-z])(\d{1,2})(?![A-Za-z0-9])")
_RE_NUM_BLDG = re.compile(
    r"(?<![A-Za-z0-9一二三四五六七八九十])([一二三四五六七八九十]+|\d{1,2})"
    r"\s*号?\s*(?:宿舍|寝室|栋|号楼|学生宿舍)"
)
_RE_DORM_DASH = re.compile(r"(宿舍|寝室)(?:楼)?\s*(\d{1,2})\s*[-—]\s*\d{2,3}")
_RE_CABINET = re.compile(r"柜|架空层|楼下|外卖柜|美団|美团")

_NAMED_HITS = {kw: pt for kw, pt in NAMED_POINTS.items()}

# 店主已逐条确认过的特殊写法 → 标准点名（随模块内置，也可在调用处覆盖）。
DEFAULT_ALIASES = {
    "浙江省杭州市临安区浙江农林大学(东湖校区)行政楼 325 室（放学三外卖柜）": "学三",
}


def _cn_to_int(text: str) -> Optional[int]:
    if not text:
        return None
    if all(ch in CN_DIGITS for ch in text):
        if len(text) == 1:
            return CN_DIGITS[text]
        if "十" in text:
            head, _, tail = text.partition("十")
            tens = CN_DIGITS[head] if head else 1
            ones = CN_DIGITS[tail] if tail else 0
            return tens * 10 + ones
    return None


def _clean(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_CN_TABLE)
    text = re.sub(r"[()（）]", " ", text)
    text = _RE_MSG.sub(" ", text)
    text = _RE_PLACE.sub(" ", text)
    text = _RE_CAMPUS_SUFFIX.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def detect_campus(address: str) -> str:
    """按文本识别校区：东湖农林 / 医学院 / 衣锦联建 / 未知。"""
    text = unicodedata.normalize("NFKC", str(address or "")).translate(_CN_TABLE).lower()
    if "联建" in text or "衣锦" in text:
        return "衣锦联建"
    if any(k in text for k in CAMPUS_YIXUE_KEYWORDS):
        return "医学院"
    if any(k in text for k in CAMPUS_DONGHU_KEYWORDS):
        return "东湖农林"
    return "未知"


def _extract_candidates(body: str, campus: str) -> tuple[dict[str, int], bool, bool]:
    """抽候选点名。柜/楼下仅作投递属性，不进候选。

    返回 (候选名→次数, 是否带柜提示, 是否命中泛指"学院楼")。
    """
    cands: dict[str, int] = {}

    def add(kw: str) -> None:
        cands[kw] = cands.get(kw, 0) + 1

    # 字母楼栋码（东湖宿舍区 A/B/C/D）
    for m in _RE_BLDG.finditer(body):
        zone, no = m.group(1).upper() if DORM_UPPER else m.group(1), m.group(2)
        if zone.upper() in DORM_ZONES:
            add(f"{zone.upper() if DORM_UPPER else zone}{no}")
    # 数字楼：医学院宿舍用「医N号」；东湖出现"学院楼 N 号"之类不带字母则仍按楼号。
    for m in _RE_NUM_BLDG.finditer(body):
        raw = m.group(1)
        num = _cn_to_int(raw) if not raw.isdigit() else int(raw)
        if num is None or not (0 < num <= 40):
            continue
        if campus == "医学院":
            add(YIXUE_BUILDING_TMPL.format(n=num))
        else:
            add(f"{num}号楼")
    # 命名点（词面即点名）
    for kw, pt in _NAMED_HITS.items():
        if kw in body:
            add(pt)
    # 校门
    gate = _RE_GATE.search(body)
    if gate:
        add(GATE_POINT[gate.group(1)])
    has_cabinet = bool(_RE_CABINET.search(body))
    named_college = "学院楼" in body and "学院楼" not in cands
    # 回退：宿舍 5-112 这类连字符房号（医学院）
    if not cands:
        m = _RE_DORM_DASH.search(body)
        if m:
            num = int(m.group(2))
            add(YIXUE_BUILDING_TMPL.format(n=num) if campus == "医学院" else f"{num}号楼")
    return cands, has_cabinet, named_college


def _pick_primary(cands: dict[str, int], prefer_gate_when_both: bool) -> tuple[str, str]:
    """在多个候选中选主点位。返回 (点名, 决策依据)。

    楼码与校门并存时：默认放宿舍楼下取楼码（prefer_gate_when_both=False，
    与店主确认口径）；置 True 则取校门。其余按出现次数取多数，平手取首现。
    """
    ordered = sorted(
        ((idx, k, v) for idx, (k, v) in enumerate(cands.items())),
        key=lambda kv: (-kv[2], kv[0]),
    )
    if len(ordered) == 1:
        return ordered[0][1], "唯一候选"
    gates = {k for k in cands if k in GATE_POINT.values()}
    bldgs = [k for k in cands if re.fullmatch(r"[A-D]\d{1,2}|\d{1,2}号楼|医\d+号", k)]
    if gates and bldgs:
        if prefer_gate_when_both:
            return next(iter(gates)), "门与楼共存，默认取门"
        return bldgs[0], "门与楼共存，按口径放宿舍楼下取楼码"
    if ordered[0][2] > ordered[1][2]:
        return ordered[0][1], "取出现次数最多的候选"
    return ordered[0][1], "多个候选并列/疑为误填，待人工确认"


def normalize_delivery_point(
    address: str,
    *,
    aliases: Optional[dict[str, str]] = None,
    prefer_gate_when_both: bool = False,
) -> dict:
    """把完整收货地址归一化为取餐点。

    返回 dict：
      campus      校区分类
      point       取餐点名（无则 ""）
      confidence  high | medium | unknown
      reason      规则命中依据 / 待确认原因
      raw_address 输入原文
      candidates  命中的候选点名及次数（便于 UI 展示供选择）
      cabinet     是否为「楼下/柜」投递（仅展示用，不参与点位判断）

    ``aliases`` 为「原始地址字符串 → 点名」覆盖表，命中即返回，用于存放
    人工确认过的特殊写法（如行政楼放学三外卖柜 → 学三）。传入的 aliases
    会覆盖同键内置别名（DEFAULT_ALIASES）。
    """
    raw = str(address or "").strip()
    alias_map = dict(DEFAULT_ALIASES)
    if aliases:
        alias_map.update(aliases)
    if raw in alias_map:
        return {
            "campus": detect_campus(raw),
            "point": alias_map[raw],
            "confidence": "high",
            "reason": "别名表命中",
            "raw_address": raw,
            "candidates": {alias_map[raw]: 1},
            "cabinet": False,
        }
    campus = detect_campus(raw)
    body = _clean(raw)
    cands, has_cabinet, named_college = _extract_candidates(body, campus)

    if not cands and body in {"大西", "小西", "校门口", "外卖柜"}:
        return {
            "campus": campus, "point": body, "confidence": "high",
            "reason": "已是短名直接采用", "raw_address": raw,
            "candidates": {body: 1}, "cabinet": False,
        }

    if not cands:
        if "学院楼" in body:
            point, reason = "学院楼", "学院楼未带具体楼号，待确认"
        elif has_cabinet and re.search(r"[A-D]区", body):
            m = re.search(r"([A-D])区", body)
            point, reason = f"{m.group(1).upper() if DORM_UPPER else m.group(1)}区", "仅有区提示，待确认"
        else:
            point, reason = "", "未识别出任何取餐点"
        return {
            "campus": campus, "point": point, "confidence": "unknown",
            "reason": reason, "raw_address": raw,
            "candidates": dict(cands), "cabinet": has_cabinet,
        }
    if len(cands) == 1:
        point = next(iter(cands))
        # A bare "西门" is ambiguous in the real data; explicit 大/小西门
        # and 西南门 remain high-confidence aliases.
        has_explicit_gate = bool(re.search(r"大西门|小西门|西南1?门", body))
        if (point == "大西" and not has_explicit_gate
                and re.search(r"(?<![大小])西门", body)):
            return {
                "campus": campus, "point": point, "confidence": "medium",
                "reason": "泛称西门，待人工确认", "raw_address": raw,
                "candidates": dict(cands), "cabinet": has_cabinet,
            }
        return {
            "campus": campus, "point": point, "confidence": "high",
            "reason": "唯一候选", "raw_address": raw,
            "candidates": dict(cands), "cabinet": has_cabinet,
        }
    point, reason = _pick_primary(cands, prefer_gate_when_both)
    # 楼码与校门并存时按既定口径取楼码，规则确定，无需人工复核。
    if reason.startswith("门与楼共存"):
        confidence = "high"
    else:
        confidence = "medium"
    return {
        "campus": campus, "point": point, "confidence": confidence,
        "reason": reason, "raw_address": raw,
        "candidates": dict(cands), "cabinet": has_cabinet,
    }
