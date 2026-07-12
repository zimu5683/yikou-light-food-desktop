"""Password storage using the operating-system keychain when available.

`keyring` is optional: a missing backend simply means the user is prompted on
each run instead of writing a password to the repository or JSON config.
"""
from __future__ import annotations

import getpass
from typing import Optional

SERVICE_NAME = "yikou-light-food"


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


load_password = get_password
save_password = set_password


# Compatibility aliases used by the Tkinter layer.
def load_password(username: str, service: str = SERVICE_NAME) -> Optional[str]:
    return get_password(username, service)


def save_password(username: str, password: str, service: str = SERVICE_NAME) -> bool:
    return set_password(username, password, service)
