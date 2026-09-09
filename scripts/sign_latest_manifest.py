"""Sign ``latest.json`` with the Ed25519 release key.

The updater only trusts a manifest whose detached signature verifies against
the public key embedded in ``app/updater.py``.  The private key must be supplied
via ``UPDATE_SIGNING_KEY`` (PEM text) or ``--key-file``; it must never be
committed to the repository.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _load_private_key(value: str) -> Ed25519PrivateKey:
    text = value.strip()
    path = Path(text)
    if "\n" not in text and path.is_file():
        data = path.read_bytes()
    else:
        data = text.encode("utf-8")
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("签名私钥必须是 Ed25519 PKCS#8 PEM")
    return key


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="latest.json")
    parser.add_argument("--output", default="latest.json.sig")
    parser.add_argument("--key-file", default="")
    args = parser.parse_args()

    key_value = args.key_file or os.environ.get("UPDATE_SIGNING_KEY", "")
    if not key_value:
        print("缺少 UPDATE_SIGNING_KEY（或 --key-file），拒绝发布未签名清单", file=sys.stderr)
        return 2
    try:
        private_key = _load_private_key(key_value)
    except Exception as exc:  # noqa: BLE001 - CLI 需要清晰报错
        print(f"无法加载 Ed25519 签名私钥：{exc}", file=sys.stderr)
        return 2

    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        print(f"找不到清单文件：{manifest_path}", file=sys.stderr)
        return 2
    manifest = manifest_path.read_bytes()
    signature = private_key.sign(manifest)
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    payload = {
        "algorithm": "ed25519",
        "key_id": hashlib.sha256(public_raw).hexdigest()[:16],
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    Path(args.output).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已签名 {manifest_path} -> {args.output}（key_id={payload['key_id']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
