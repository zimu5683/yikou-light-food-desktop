"""bspatch 解码器的格式级测试。

不依赖 bsdiff4：测试里按 BSDIFF40 格式手工构造补丁字节，验证
``app.bspatch`` 对魔数、还原长度、diff 叠加、extra 拷贝、old 指针
越界补零与负偏移的处理符合 bsdiff4 语义。
"""
from __future__ import annotations

import bz2

import pytest

from app import bspatch


def _encode_int64(value: int) -> bytes:
    """按 bsdiff4 自定义编码写出有符号 int64（符号位在最高字节 bit7）。"""
    body = bytearray(abs(value).to_bytes(8, "little"))
    if value < 0:
        body[7] |= 0x80
    return bytes(body)


def _make_patch(control: list[tuple[int, int, int]], diff: bytes, extra: bytes,
                new_size: int) -> bytes:
    """按 BSDIFF40 头 + 三个 bz2 块的结构拼出补丁。"""
    control_bytes = b"".join(
        _encode_int64(x) + _encode_int64(y) + _encode_int64(z) for x, y, z in control
    )
    control_bz2 = bz2.compress(control_bytes)
    diff_bz2 = bz2.compress(diff)
    extra_bz2 = bz2.compress(extra)
    return (
        b"BSDIFF40"
        + _encode_int64(len(control_bz2))
        + _encode_int64(len(diff_bz2))
        + _encode_int64(new_size)
        + control_bz2
        + diff_bz2
        + extra_bz2
    )


def test_apply_zero_diff_shortcut_returns_old_bytes():
    old = bytes(range(10))
    patch = _make_patch([(10, 0, 0)], b"\x00" * 10, b"", new_size=10)
    assert bspatch.apply(old, patch) == old


def test_apply_combines_diff_overlap_extra_and_out_of_range_padding():
    old = b"abcdef"
    # 前 6 字节 diff 全零 → 沿用 old；随后 old 指针越过文件末尾，
    # 越界部分按 0 补齐再叠加 diff；最后 3 字节从 extra 原样拷贝。
    diff = b"\x00\x00\x00\x00\x00\x00\x01\x02"
    patch = _make_patch([(6, 0, 4), (2, 3, 0)], diff, b"XYZ", new_size=11)
    assert bspatch.apply(old, patch) == b"abcdef\x01\x02XYZ"


def test_apply_rewinds_old_pointer_with_negative_offset():
    old = b"abcdef"
    # z=-3 把 old 指针拨回开头，下一条指令重新叠加 old[0:3]。
    diff = b"\x00\x00\x00\x01\x01\x00"
    patch = _make_patch([(3, 0, -3), (3, 0, 0)], diff, b"", new_size=6)
    assert bspatch.apply(old, patch) == b"abcbcc"


def test_apply_copies_extra_block_without_diff():
    patch = _make_patch([(0, 5, 0)], b"", b"hello", new_size=5)
    assert bspatch.apply(b"", patch) == b"hello"


def test_apply_rejects_bad_magic():
    with pytest.raises(ValueError):
        bspatch.apply(b"anything", b"NOTBSDIFF12345678")


def test_apply_rejects_length_mismatch():
    old = b"abcdef"
    # 实际还原 6 字节，但头声明的 len_new 是 5。
    patch = _make_patch([(6, 0, 0)], b"\x00" * 6, b"", new_size=5)
    with pytest.raises(ValueError, match="长度不符"):
        bspatch.apply(old, patch)


def test_apply_file_writes_result(tmp_path):
    old_path = tmp_path / "old.bin"
    patch_path = tmp_path / "patch.bin"
    new_path = tmp_path / "new.bin"
    old_path.write_bytes(b"abcdef")
    patch_path.write_bytes(_make_patch([(6, 0, 0)], b"\x00" * 6, b"", new_size=6))

    bspatch.apply_file(old_path, patch_path, new_path)

    assert new_path.read_bytes() == b"abcdef"
