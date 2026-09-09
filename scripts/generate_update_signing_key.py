"""Generate an Ed25519 key pair for update-manifest signing.

The private key is written outside the repository by default.  Put it into the
GitHub Actions secret ``UPDATE_SIGNING_KEY`` and paste the printed public key
into ``app/updater.py`` (``UPDATE_MANIFEST_PUBLIC_KEY`` / KEY_ID).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> int:
    parser = argparse.ArgumentParser()
    default = Path.home() / ".config" / "yikou-light-food" / "update-signing-key.pem"
    parser.add_argument("--output", default=str(default))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    target = Path(args.output).expanduser()
    if target.exists() and not args.force:
        print(f"私钥已存在：{target}（如需轮换请加 --force）", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    target.write_bytes(private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    try:
        target.chmod(0o600)
    except OSError:
        pass
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    print(f"private_key={target}")
    print(f"public_b64={base64.b64encode(public_raw).decode('ascii')}")
    print(f"key_id={hashlib.sha256(public_raw).hexdigest()[:16]}")
    print("请把 private_key 内容保存到 GitHub Secret UPDATE_SIGNING_KEY，"
          "并把 public_b64/key_id 更新到 app/updater.py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
