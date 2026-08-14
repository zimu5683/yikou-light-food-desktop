"""Password storage using the operating-system keychain when available.

`keyring` is optional: a missing backend simply means the user is prompted on
each run instead of writing a password to the repository or JSON config.
"""
from __future__ import annotations

import getpass
from typing import Optional

SERVICE_NAME = "yikou-light-food"
# 闪时送（sss）平台使用独立的服务名，避免与管理后台同名账号的密码互相覆盖。
SSS_SERVICE_NAME = "yikou-light-food-sss"


def _backend():
    try:
        import keyring  # type: ignore
        return keyring
    except Exception:
        return None


def get_password(username: str, service: str = SERVICE_NAME) -> Optional[str]:
    backend = _backend()
    if backend is None or not username:
        return None
    try:
        return backend.get_password(service, username)
    except Exception:
        return None


def set_password(username: str, password: str, service: str = SERVICE_NAME) -> bool:
    backend = _backend()
    if backend is None or not username:
        return False
    try:
        backend.set_password(service, username, password)
        return True
    except Exception:
        return False


def delete_password(username: str, service: str = SERVICE_NAME) -> bool:
    backend = _backend()
    if backend is None or not username:
        return False
    try:
        backend.delete_password(service, username)
        return True
    except Exception:
        return False


def prompt_password(username: str = "") -> str:
    """Get a password interactively without echoing it (CLI fallback)."""
    return getpass.getpass(f"Password{f' for {username}' if username else ''}: ")


def get_sss_password(username: str) -> Optional[str]:
    """Read the 闪时送 password from the OS keychain."""
    return get_password(username, SSS_SERVICE_NAME)


def set_sss_password(username: str, password: str) -> bool:
    """Persist the 闪时送 password to the OS keychain."""
    return set_password(username, password, SSS_SERVICE_NAME)


def delete_sss_password(username: str) -> bool:
    """Remove the 闪时送 password from the OS keychain."""
    return delete_password(username, SSS_SERVICE_NAME)


# Compatibility aliases used by the Tkinter layer.
def load_password(username: str, service: str = SERVICE_NAME) -> Optional[str]:
    return get_password(username, service)


def save_password(username: str, password: str, service: str = SERVICE_NAME) -> bool:
    return set_password(username, password, service)
