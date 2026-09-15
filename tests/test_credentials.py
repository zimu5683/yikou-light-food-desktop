"""``app.credentials`` 的回归锁（改动前 9 个公开函数**一个都没被测过**）。

这个模块守的是一条 README 明确写下的安全属性：

> 闪时送密码使用独立凭据名 `yikou-light-food-sss`，与管理后台账号密码**互不覆盖**。

一旦这条被破坏（例如 ``set_sss_password`` 误用了 ``SERVICE_NAME``），用户用**同一个
手机号**登录两个平台时，后存的密码会**悄悄覆盖**先存的 —— 表现是「密码明明没改，
却登录失败」，极难排查。所以「同名不同服务」必须钉死。

另外 ``keyring`` 是**可选依赖**（没装就每次手输），因此所有分支都必须优雅降级：
backend 缺失或抛异常时返回 ``None``/``False``，**绝不向上抛**。

测试一律 monkeypatch ``_backend``，**绝不触碰真实系统密钥链**。
"""
from __future__ import annotations

import pytest

from app import credentials as cred


class FakeKeyring:
    """内存版 keyring，记录每次调用。"""

    def __init__(self, *, raise_on: tuple[str, ...] = ()) -> None:
        self.data: dict[tuple[str, str], str] = {}
        self.calls: list[tuple] = []
        self.raise_on = raise_on

    def _record(self, *call):
        self.calls.append(call)
        if call[0] in self.raise_on:
            raise RuntimeError("模拟密钥链故障")

    def get_password(self, service: str, username: str):
        self._record("get", service, username)
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str):
        self._record("set", service, username, password)
        self.data[(service, username)] = password

    def delete_password(self, service: str, username: str):
        self._record("del", service, username)
        self.data.pop((service, username), None)


@pytest.fixture
def keyring(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(cred, "_backend", lambda: fake)
    return fake


# ----------------------------------------------------------------------
# 服务名：两种凭据必须落在不同的命名空间
# ----------------------------------------------------------------------
def test_service_names_are_distinct():
    assert cred.SERVICE_NAME == "yikou-light-food"
    assert cred.SSS_SERVICE_NAME == "yikou-light-food-sss"
    assert cred.SERVICE_NAME != cred.SSS_SERVICE_NAME


def test_admin_functions_use_the_default_service(keyring):
    cred.set_password("13800000000", "ADMIN")
    assert keyring.calls == [("set", "yikou-light-food", "13800000000", "ADMIN")]
    assert cred.get_password("13800000000") == "ADMIN"
    cred.delete_password("13800000000")
    assert keyring.calls[-1] == ("del", "yikou-light-food", "13800000000")


def test_sss_functions_use_the_sss_service(keyring):
    cred.set_sss_password("13800000000", "SSS")
    assert keyring.calls == [("set", "yikou-light-food-sss", "13800000000", "SSS")]
    assert cred.get_sss_password("13800000000") == "SSS"
    cred.delete_sss_password("13800000000")
    assert keyring.calls[-1] == ("del", "yikou-light-food-sss", "13800000000")


def test_same_username_keeps_two_passwords_without_overwriting(keyring):
    """**核心安全属性**：同一个手机号在两个平台各存一把密码，互不覆盖。

    如果 ``set_sss_password`` 误用 ``SERVICE_NAME``，下面第一句断言就会失败。
    """
    cred.set_password("13800000000", "ADMIN")
    cred.set_sss_password("13800000000", "SSS")

    assert cred.get_password("13800000000") == "ADMIN"
    assert cred.get_sss_password("13800000000") == "SSS"
    assert len(keyring.data) == 2, "两把密码必须落在不同的键上"


def test_deleting_one_credential_leaves_the_other_untouched(keyring):
    cred.set_password("u", "ADMIN")
    cred.set_sss_password("u", "SSS")

    cred.delete_sss_password("u")

    assert cred.get_sss_password("u") is None
    assert cred.get_password("u") == "ADMIN", "删闪时送密码不能连带删掉管理后台密码"


def test_explicit_service_argument_overrides_the_default(keyring):
    cred.set_password("u", "PW", service="自定义")
    assert keyring.calls == [("set", "自定义", "u", "PW")]
    assert cred.get_password("u", service="自定义") == "PW"


# ----------------------------------------------------------------------
# 空账号：不该白跑一趟密钥链
# ----------------------------------------------------------------------
def test_empty_username_never_touches_the_backend(keyring):
    assert cred.get_password("") is None
    assert cred.set_password("", "PW") is False
    assert cred.delete_password("") is False
    assert cred.get_sss_password("") is None
    assert cred.set_sss_password("", "PW") is False
    assert cred.delete_sss_password("") is False
    assert keyring.calls == [], "空账号不该调用密钥链"


def test_whitespace_only_username_does_reach_the_backend(keyring):
    """记录实际行为：判断条件是 ``not username``，因此**纯空白会被当成有效账号**。

    这不是缺陷（界面不会传空白账号），但把它写下来，避免后人误以为这里有
    「去空白」的保护。
    """
    cred.set_password("   ", "PW")
    assert keyring.calls == [("set", "yikou-light-food", "   ", "PW")]


# ----------------------------------------------------------------------
# keyring 缺失 / 故障：必须优雅降级，绝不向上抛
# ----------------------------------------------------------------------
def test_missing_backend_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(cred, "_backend", lambda: None)
    assert cred.get_password("u") is None
    assert cred.set_password("u", "PW") is False
    assert cred.delete_password("u") is False
    assert cred.get_sss_password("u") is None
    assert cred.set_sss_password("u", "PW") is False


def test_backend_read_failure_is_swallowed(monkeypatch):
    """密钥链读取故障（锁屏、无 D-Bus、权限不足…）要返回 None，不能抛。"""
    monkeypatch.setattr(cred, "_backend", lambda: FakeKeyring(raise_on=("get",)))
    assert cred.get_password("u") is None
    assert cred.get_sss_password("u") is None


def test_backend_write_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(cred, "_backend", lambda: FakeKeyring(raise_on=("set",)))
    assert cred.set_password("u", "PW") is False
    assert cred.set_sss_password("u", "PW") is False


def test_backend_delete_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(cred, "_backend", lambda: FakeKeyring(raise_on=("del",)))
    assert cred.delete_password("u") is False
    assert cred.delete_sss_password("u") is False


def test_backend_factory_guards_the_import(monkeypatch):
    """``_backend()`` 把 import 包在 try 里：keyring 缺失/导入失败都返回 None。

    这是 ``_backend`` **自身**的保证（不是调用方包的 try），所以单独验一下 ——
    否则「keyring 没装就每次手输」这条 README 承诺就落空了。
    """
    import builtins

    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "keyring":
            raise ImportError("模拟未安装 keyring")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    assert cred._backend() is None

    # 后端缺失时所有入口都优雅降级
    assert cred.get_password("u") is None
    assert cred.set_password("u", "PW") is False


# ----------------------------------------------------------------------
# 交互式兜底与兼容别名
# ----------------------------------------------------------------------
def test_prompt_password_uses_getpass_with_username_in_the_prompt(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(cred.getpass, "getpass",
                        lambda prompt="": (seen.append(prompt), "TYPED")[1])

    assert cred.prompt_password("13800000000") == "TYPED"
    assert seen == ["Password for 13800000000: "], "提示语以 \": \" 结尾（含尾随空格）"


def test_prompt_password_without_username(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(cred.getpass, "getpass",
                        lambda prompt="": (seen.append(prompt), "TYPED")[1])

    assert cred.prompt_password() == "TYPED"
    assert seen == ["Password: "], "提示语以 \": \" 结尾（含尾随空格）"


def test_compatibility_aliases_forward_to_the_same_functions(keyring):
    """Tkinter 层用过的旧名字要等价，且同样支持自定义 service。"""
    assert cred.save_password("u", "PW") is True
    assert keyring.calls[-1] == ("set", "yikou-light-food", "u", "PW")
    assert cred.load_password("u") == "PW"

    cred.save_password("u", "PW2", service=cred.SSS_SERVICE_NAME)
    assert keyring.calls[-1] == ("set", "yikou-light-food-sss", "u", "PW2")
    assert cred.load_password("u", service=cred.SSS_SERVICE_NAME) == "PW2"
