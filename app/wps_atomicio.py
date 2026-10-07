"""原子写盘与跨进程文件锁（WPS 账本 / journal 共用的小基础设施）。

设计取舍：

* 写临时文件 → ``fsync`` 文件 → ``os.replace`` → 尽力 ``fsync`` 父目录；
* 跨进程锁按平台选择原语：POSIX 用 ``fcntl.lockf``，Windows 用 ``msvcrt.locking``。
  桌面端必须三平台可用，因此**不能**直接照搬只跑 Linux/Android 的参考实现；
  两个平台都拿不到锁原语时明确抛错，不做“假装上锁”的降级；
* 锁文件与数据文件分离，数据锁路径由 :func:`lock_path_for` 统一计算，
  journal 与 ledger 对同一账本映射到同一个锁文件。
"""
from __future__ import annotations

import errno
import os
import tempfile
import threading
import time
from pathlib import Path

try:  # POSIX：Linux / macOS
    import fcntl
except ImportError:  # pragma: no cover - Windows 上不存在
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX 上不存在
    msvcrt = None  # type: ignore[assignment]


class LockTimeout(TimeoutError):
    """在给定超时时间内没有取得跨进程锁。"""


class AtomicWriteError(OSError):
    """原子写盘失败（调用方必须失败关闭）。"""


_LOCK_STATE = threading.Lock()
_HELD_LOCKS: dict[tuple[int, str, int], "_LockRecord"] = {}
_THREAD_LOCKS: dict[str, threading.Lock] = {}


class _LockRecord:
    """同一 ``(进程, 锁文件, 线程)`` 的共享持锁记录。

    **资源（fd 与线程锁）挂在这里，而不是挂在 FileLock 实例上** —— 这是关键：
    同一个线程里两个实例嵌套取同一把锁时，"谁先释放"是不确定的；如果 fd 属于
    先释放的那个实例，先释放就会把 OS 锁和线程锁一起丢掉（或者反过来永远不释放）。
    把资源放进共享记录后，只有"把计数减到 0"的那一次才真正解锁，与释放顺序无关。
    """

    __slots__ = ("count", "fd", "thread_lock")

    def __init__(self, count: int, fd: int, thread_lock: threading.Lock) -> None:
        self.count = count
        self.fd = fd
        self.thread_lock = thread_lock

# 目录 fsync 在这些 errno 下属于「平台不支持」，不算失败。
_DIR_FSYNC_UNSUPPORTED = (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF)
try:  # Windows 上打开目录会直接失败；EACCES 同样按不支持处理
    _DIR_FSYNC_UNSUPPORTED = (*_DIR_FSYNC_UNSUPPORTED, errno.EACCES)
except Exception:  # pragma: no cover - errno 常量缺失时的兜底
    pass


def _thread_lock_for(key: str) -> threading.Lock:
    """按**归一化后的锁身份**取进程内线程锁（同一路径的别名共用一把）。"""
    with _LOCK_STATE:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


def normalise_lock_identity(path: str | os.PathLike[str]) -> str:
    """把锁路径归一成稳定的进程内身份。

    为什么必须归一：POSIX 的文件锁是**基于 inode** 且**同进程可重入**的，
    所以同一进程内真正互斥靠的是这份进程内记账。如果 ``a.lock``、
    ``./a.lock``、``dir/../a.lock`` 各算一个身份，同一进程就会同时"持有"
    两把互不相识的锁，释放顺序一乱就会泄漏（见 _LockRecord 的说明）。

    ``realpath`` 会把符号链接与相对路径折叠到同一串；Windows 上再用
    ``normcase`` 折叠大小写（macOS 默认大小写不敏感但 ''normcase'' 是恒等，
    这种极端情况由 OS 层 inode 语义兜底）。
    """
    text = os.path.expanduser(str(path))
    resolved = os.path.realpath(os.path.abspath(text))
    return os.path.normcase(resolved)


def lock_path_for(write_path: str | os.PathLike[str]) -> Path:
    """同一账本的 ledger/journal 共用一把数据锁。

    ``state.json`` 与 ``state.json.journal`` 都映射到 ``state.json.lock``。
    """
    path = Path(write_path)
    name = str(path)
    if name.endswith(".journal"):
        name = name[: -len(".journal")]
    return Path(name + ".lock")


def operation_lock_path_for(ledger_path: str | os.PathLike[str]) -> Path:
    """整次 apply_plan 操作的跨进程锁；与逐文件数据锁分离，避免嵌套死锁。"""
    return Path(str(ledger_path) + ".oplock")


def batch_lock_path_for(state_path: str | os.PathLike[str]) -> Path:
    """整次闪时送下单批次的跨进程锁。

    **刻意与数据锁分开**（``<state>.lock`` 是逐次读写的短锁）：一个批次会长时间
    持有它，其间还会写日志、做只读核对 —— 如果两者是同一个文件，就只能靠"同线程
    重入"勉强不锁死；而重入语义一旦被改动（或换线程写日志）就会变成死锁。

    明确的锁顺序（全局统一，只允许从前往后取）：

        ``<state>.batch.lock``（批次，长）
          → ``<state>.lock``（数据，短）

    WPS 侧同理：``<ledger>.oplock``（操作，长）→ ``<ledger>.lock``（数据，短）。
    """
    return Path(str(state_path) + ".batch.lock")


def _fsync_parent_dir(parent: Path) -> None:
    """尽力 fsync 父目录；不支持目录 fsync 的平台直接返回。

    Windows 不允许把目录当文件打开（``os.open`` 会抛 ``PermissionError``），
    这里按「平台不支持」处理而不是让写入失败 —— 数据文件本身已经 flush 并原子替换。
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(str(parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno in _DIR_FSYNC_UNSUPPORTED:
            return
        raise
    finally:
        os.close(fd)


def atomic_write_text(path: str | os.PathLike[str], text: str, *,
                      encoding: str = "utf-8") -> Path:
    """临时文件 + fsync + 原子替换 + 父目录 fsync；任何一步失败都不留下半文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                    dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        _fsync_parent_dir(target.parent)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def lock_primitive_available() -> bool:
    """当前平台是否有可用的跨进程锁原语。"""
    return fcntl is not None or msvcrt is not None


class FileLock:
    """轻量跨进程锁（POSIX ``fcntl`` / Windows ``msvcrt`` 双实现）。

    重入规则（明确、可验证）：

    * **同一实例**重复 ``acquire()`` 允许，必须配对释放同样次数；
    * **同线程、同一把锁**的另一个实例也可以嵌套获取（计数共享）；
    * **另一个线程/进程**必须等待，直到这一侧的计数归零；
    * ``release()`` 与获取次数**配对**才真正解锁；多余的 ``release()`` 是 no-op
      （幂等，不会去减别人的计数）；
    * 释放顺序不影响正确性：无论哪个实例先释放，只有把共享计数减到 0 的那一次
      才真正解锁并把 fd/线程锁还回去；
    * 取锁过程中**任何**异常（``OSError``、没有锁原语导致的 ``RuntimeError``、
      超时）都会先把已经拿到的资源还回去，不会泄漏 fd 或线程锁。
    """

    def __init__(self, path: str | os.PathLike[str], *,
                 shared: bool = False, timeout: float = 30.0,
                 poll: float = 0.05) -> None:
        self.path = Path(path)
        self.identity = normalise_lock_identity(path)
        self.shared = bool(shared)
        self.timeout = max(0.0, float(timeout))
        self.poll = max(0.001, float(poll))
        self._key: tuple[int, str, int] | None = None
        self._holds = 0

    # ---- 底层原语 ----
    def _open(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)

    def _try_lock(self, fd: int) -> bool:
        """尝试取锁；已被别人持有返回 ``False``，其余 OSError 向上抛。"""
        if fcntl is not None:
            flags = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
            try:
                fcntl.lockf(fd, flags | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return False
                raise
            return True
        if msvcrt is not None:  # pragma: no cover - 需要 Windows 才能跑到
            # msvcrt.locking 锁的是「从当前文件位置起 N 字节」；先定位到 0 再锁 1 字节。
            # 它**没有共享锁**语义，因此 shared=True 也退化成独占锁（只影响并发读，
            # 本项目没有多进程并发读同一账本的需求）。
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    return False
                raise
            return True
        raise RuntimeError("当前平台没有可用的跨进程文件锁原语")

    def _unlock(self, fd: int) -> None:
        if fcntl is not None:
            try:
                fcntl.lockf(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            return
        if msvcrt is not None:  # pragma: no cover - 需要 Windows 才能跑到
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass

    # ---- 取锁 ----
    def acquire(self) -> "FileLock":
        """取锁；超时抛 :class:`LockTimeout`，锁原语错误抛 :class:`AtomicWriteError`。

        没有可用锁原语的平台抛 ``RuntimeError``（**不做"假装上锁"的降级**）。
        任何异常路径都不会留下 fd 或线程锁。
        """
        key = (os.getpid(), self.identity, threading.get_ident())
        with _LOCK_STATE:
            record = _HELD_LOCKS.get(key)
            if record is not None:
                # 本线程已经拿着这把锁（自己或另一个实例）：只加计数。
                record.count += 1
                self._key = key
                self._holds += 1
                return self

        thread_lock = _thread_lock_for(self.identity)
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if not thread_lock.acquire(timeout=remaining):
                raise LockTimeout(f"等待跨进程锁超时：{self.path}")
            fd: int | None = None
            try:
                fd = self._open()
                locked = self._try_lock(fd)
            except BaseException as exc:
                # 任何异常（含"没有锁原语"的 RuntimeError）都必须还回资源。
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                thread_lock.release()
                if isinstance(exc, OSError):
                    raise AtomicWriteError(
                        f"无法获取跨进程锁 {self.path}：{exc}") from exc
                raise
            if locked:
                with _LOCK_STATE:
                    _HELD_LOCKS[key] = _LockRecord(count=1, fd=fd,
                                                   thread_lock=thread_lock)
                self._key = key
                self._holds += 1
                return self
            try:
                os.close(fd)
            finally:
                thread_lock.release()
            if time.monotonic() >= deadline:
                raise LockTimeout(f"等待跨进程锁超时：{self.path}")
            time.sleep(self.poll)

    # ---- 释放 ----
    def release(self) -> None:
        """释放一次持有；只有把共享计数减到 0 时才真正解锁（幂等）。"""
        key = self._key
        if key is None or self._holds <= 0:
            return
        self._holds -= 1
        if self._holds == 0:
            self._key = None
        fd: int | None = None
        thread_lock: threading.Lock | None = None
        with _LOCK_STATE:
            record = _HELD_LOCKS.get(key)
            if record is None:
                # 已经被更强壮的清理路径回收（例如进程退出后的重放）：幂等返回。
                return
            record.count -= 1
            if record.count > 0:
                return
            _HELD_LOCKS.pop(key, None)
            fd, thread_lock = record.fd, record.thread_lock
            record.fd = None
            record.thread_lock = None
        if fd is not None:
            try:
                self._unlock(fd)
            finally:
                try:
                    os.close(fd)
                except OSError:  # pragma: no cover - 关闭失败不值当炸掉调用方
                    pass
        if thread_lock is not None:
            try:
                thread_lock.release()
            except RuntimeError:  # pragma: no cover - 已被回收时按幂等处理
                pass

    @property
    def held(self) -> bool:
        """当前实例是否仍持有（供诊断与测试断言）。"""
        return self._holds > 0 and self._key is not None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


__all__ = [
    "AtomicWriteError",
    "batch_lock_path_for",
    "normalise_lock_identity",
    "FileLock",
    "LockTimeout",
    "atomic_write_text",
    "lock_path_for",
    "lock_primitive_available",
    "operation_lock_path_for",
]
