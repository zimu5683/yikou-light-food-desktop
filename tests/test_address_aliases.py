from __future__ import annotations

import json

import pytest

from app.address_aliases import load_aliases, write_pending


def test_load_aliases_creates_empty_file(tmp_path):
    path = tmp_path / "address_aliases.json"
    assert load_aliases(path) == {}
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_load_aliases_strips_keys_and_values(tmp_path):
    path = tmp_path / "address_aliases.json"
    path.write_text(json.dumps({" 地址 ": " A5 "}, ensure_ascii=False), encoding="utf-8")
    assert load_aliases(path) == {"地址": "A5"}


@pytest.mark.parametrize("payload,message", [
    ("{broken", "地址别名文件无效"),
    ("[]", "根节点必须是对象"),
    (json.dumps({"": "A5"}), "空键或非字符串值"),
    (json.dumps({"地址": 5}), "空键或非字符串值"),
])
def test_load_aliases_rejects_invalid_content(tmp_path, payload, message):
    path = tmp_path / "address_aliases.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_aliases(path)


def test_write_pending_atomically_replaces_previous_report(tmp_path):
    path = tmp_path / "pending_addresses.json"
    path.write_text("old", encoding="utf-8")
    write_pending([{"raw_address": "地址"}], target_date="2026-09-07", path=path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["target_date"] == "2026-09-07"
    assert payload["items"] == [{"raw_address": "地址"}]
