"""delivery_point 地址归一化单元测试。

覆盖 2026-04~06 真实订单里出现的代表性写法，防止规则回归。
"""
from __future__ import annotations

import pytest

from app.delivery_point import (
    detect_campus,
    normalize_delivery_point as ndp,
)

# (原文, 期望点名, 期望置信度, 期望校区)
CASES = [
    # 东湖字母楼：各种后缀/杂讯
    ("浙江省杭州市临安区浙江农林大学(东湖校区) A5 506", "A5", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) A5宿舍楼", "A5", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) A6 603", "A6", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) A6寝室楼", "A6", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) B5号楼", "B5", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) b5", "B5", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) a5 518", "A5", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) B11宿舍楼", "B11", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) C5号寝室楼", "C5", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) d2宿舍", "D2", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) D2学生公寓", "D2", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) D1楼下美团外卖柜", "D1", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) b1外卖柜", "B1", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) A6宿舍楼下外卖柜", "A6", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) B9号楼 GUUU+13705820763+B9寝室楼", "B9", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江農林大學(東湖校區) D2宿舍楼下", "D2", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学东湖校区B2号楼 b1-2外卖柜", "B2", "medium", "东湖农林"),
    # 楼码与校门并存 → 按口径放宿舍楼下取楼码
    ("浙江省杭州市临安区浙江农林大学(东湖校区) C7栋502(大西门)", "C7", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) 小西门A6", "A6", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) A5 605（小西门）", "A5", "high", "东湖农林"),
    # 校门短名
    ("浙江省杭州市临安区浙江农林大学(东湖校区) 大西门外卖柜", "大西", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区)-西南1门 小西门外卖柜", "小西", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) 大西门", "大西", "high", "东湖农林"),
    # 命名点位
    ("浙江省杭州市临安区浙江农林大学(东湖校区)图书馆 外卖柜", "图书馆", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) 国重楼下", "国重楼", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) 学三外卖柜", "学三", "high", "东湖农林"),
    # 已确认别名：行政楼放学三外卖柜 = 学三
    ("浙江省杭州市临安区浙江农林大学(东湖校区)行政楼 325 室（放学三外卖柜）", "学三", "high", "东湖农林"),
    # 医学院：数字楼 → 医N号
    ("浙江省杭州市临安区杭州医学院(临安校区) 5号楼", "医5号", "high", "医学院"),
    ("浙江省杭州市临安区杭州医学院(临安校区) 女寝五号楼", "医5号", "high", "医学院"),
    ("浙江省杭州市临安区杭州医学院(临安校区) 六号楼", "医6号", "high", "医学院"),
    ("浙江省杭州市临安区杭州医学院(临安校区) 5号楼214", "医5号", "high", "医学院"),
    ("浙江省杭州市临安区杭州医学院(临安校区)宿舍楼 5-112", "医5号", "high", "医学院"),
    ("浙江省杭州市临安区Hangzhou Medical College (Lin 'an Campus) Lin'an District 4号寝室楼", "医4号", "high", "医学院"),
    # 已在排单表里的简短地址：保持原样点
    ("A7", "A7", "high", "未知"),
    ("大西", "大西", "high", "未知"),
]


@pytest.mark.parametrize("address,point,confidence,campus", CASES)
def test_normalize(address, point, confidence, campus):
    res = ndp(address)
    assert res["point"] == point, f"{address}: point={res['point']} cands={res['candidates']}"
    assert res["confidence"] == confidence, f"{address}: conf={res['confidence']} reason={res['reason']}"
    assert res["campus"] == campus, f"{address}: campus={res['campus']}"


def test_campus_detection():
    assert detect_campus("浙江省杭州市临安区浙江农林大学(东湖校区) A5") == "东湖农林"
    assert detect_campus("浙江省杭州市临安区杭州医学院 5号楼") == "医学院"
    assert detect_campus("浙江省杭州市临安区浙江农林大学联建公寓 E2") == "衣锦联建"
    assert detect_campus("校门口") == "未知"


def test_unknown_returns_empty_point():
    res = ndp("浙江省杭州市临安区杭州医学院(临安校区) 杭州医学院(临安校区)")
    assert res["confidence"] == "unknown"
    assert res["point"] == ""


def test_alias_override():
    res = ndp("浙江省杭州市临安区浙江农林大学(东湖校区) C'6321", aliases={"浙江省杭州市临安区浙江农林大学(东湖校区) C'6321": "C6"})
    assert res["point"] == "C6"
    assert res["confidence"] == "high"
    assert res["reason"] == "别名表命中"


def test_bare_west_gate_requires_confirmation():
    res = ndp("浙江农林大学东湖校区西门")
    assert res["point"] == "大西"
    assert res["confidence"] == "medium"
    assert "待人工确认" in res["reason"]


def test_explicit_west_gate_wins_over_duplicated_generic_prefix():
    res = ndp("浙江农林大学东湖校区-西门 大西门外卖柜")
    assert res["point"] == "大西"
    assert res["confidence"] == "high"
