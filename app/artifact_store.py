"""上下文自适应压缩：把本地诊断产物的占用稳定控制在预算之内。

程序会在用户配置目录里累积两类「只增不减」的诊断产物：

* ``logs/``：页面定位失败时保存的证据三元组（``.png`` 截图 / ``.html`` 页面
  快照 / ``.txt`` 当前网址），单次可达数 MB；
* ``update.log``：更新助手用 ``>>`` 追加的人类可读日志。

它们对排查问题很有用，但长期使用会无限增长，最终拖慢启动、占满磁盘，也让
「把现场交给别人看」变得不现实。本模块实现「预算 + 逼近触发 + 自适应压缩」：

1. 量出受管产物的总占用；
2. **只有总占用逼近阈值**（默认 100 MiB 的 ``trigger_ratio``）才动手，避免每次
   启动都做无用功；一旦触发就压到 ``target_ratio`` 以下，留出滞回余量；
3. 按「信息损失从小到大」执行：轮转超大日志 → 把最旧的证据三元组**无损**打包
   成 ``.tar.gz`` → 淘汰超出保留数量的最旧归档。

安全边界（任何一条被破坏都视为缺陷）：

* 最近 ``fresh_keep`` 组证据**永不压缩**，保证刚失败的那几次现场仍可直接打开；
* 归档先写 ``.tmp``、重新打开校验成员数、``os.replace`` 落位，**最后**才删原文件；
  任何一步失败都不会丢数据；
* 不跟随符号链接，不触碰受管路径之外的任何文件；
* 所有失败路径只写日志，绝不向上抛给排单/下单主流程。

用法::

    from app.artifact_store import enforce_budget
    report = enforce_budget()          # 默认整理用户配置目录
    print(report.summary())
"""
from __future__ import annotations

import datetime as _dt
import gzip
import io
import logging
import os
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import user_data_dir

logger = logging.getLogger(__name__)

MIB = 1024 * 1024

#: 「上下文容量」预算：受管诊断产物的总占用上限（100 MiB）。
DEFAULT_BUDGET_BYTES = 100 * MIB
#: 逼近系数：占用达到预算的该比例即视为「逼近阈值」，触发压缩。
DEFAULT_TRIGGER_RATIO = 0.8
#: 压缩目标：整理后希望降到预算的该比例以下（滞回，避免启动时反复触发）。
DEFAULT_TARGET_RATIO = 0.6
#: 最近 N 组证据永不压缩。
DEFAULT_FRESH_KEEP = 5
#: 归档目录内保留的归档数量上限。
DEFAULT_ARCHIVE_KEEP = 24
#: 单个日志文件超过该大小就轮转。
DEFAULT_LOG_FILE_BYTES = 8 * MIB
#: 单次 ``enforce_budget`` 最多整理的轮数（估计偏差时的兜底）。
DEFAULT_MAX_PASSES = 4

#: 归档子目录名（位于 ``logs/`` 内）。
ARCHIVE_DIRNAME = "archive"
#: 受管的滚动日志文件名（相对用户配置目录）。
MANAGED_LOGS = ("update.log",)

#: 证据文件名形如 ``20260915_221400_123456_定位失败.png``。
#: 同一时间戳前缀的三个文件属于同一次失败现场，必须整组归档或整组保留。
_SNAPSHOT_STAMP = re.compile(r"^(\d{8}_\d{6}_\d{6})_")

#: 归档后体积的保守估计系数：PNG 已是压缩格式几乎不再变小，HTML/文本压缩比很高。
#: 估计偏保守只会让我们多压几组（无损、无害）；真正的数据丢弃由预算线把关，
#: 因此这里不需要为了「算得准」而冒险。多轮循环会用实测值纠正偏差。
_ESTIMATED_RATIO = {
    ".png": 0.98, ".jpg": 0.98, ".jpeg": 0.98, ".webp": 0.98, ".gif": 0.98,
    ".html": 0.25, ".htm": 0.25,
    ".json": 0.30,
    ".txt": 0.60,
}
_DEFAULT_ESTIMATED_RATIO = 0.60


@dataclass(frozen=True)
class CompactionPolicy:
    """压缩策略。默认值对应用户要求的「逼近 100 MiB 阈值自动触发」。"""

    budget_bytes: int = DEFAULT_BUDGET_BYTES
    trigger_ratio: float = DEFAULT_TRIGGER_RATIO
    target_ratio: float = DEFAULT_TARGET_RATIO
    fresh_keep: int = DEFAULT_FRESH_KEEP
    archive_keep: int = DEFAULT_ARCHIVE_KEEP
    log_file_bytes: int = DEFAULT_LOG_FILE_BYTES

    @property
    def trigger_bytes(self) -> int:
        """达到该占用即触发压缩。"""
        return int(self.budget_bytes * self.trigger_ratio)

    @property
    def target_bytes(self) -> int:
        """一次整理后希望降到该占用以下。"""
        return int(self.budget_bytes * self.target_ratio)


DEFAULT_POLICY = CompactionPolicy()


@dataclass(frozen=True)
class CompactionAction:
    """一条待执行的整理动作。``plan_compaction`` 只生成、不落盘。"""

    kind: str  # archive_snapshot | prune_archive | rotate_log
    label: str
    sources: tuple[Path, ...] = ()
    destination: Path | None = None
    reclaim_bytes: int = 0


@dataclass
class CompactionReport:
    """一次整理的完整结果，可直接打印给用户或写进日志。"""

    root: Path
    policy: CompactionPolicy
    before_bytes: int
    after_bytes: int
    triggered: bool = False
    dry_run: bool = False
    actions: list[CompactionAction] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def saved_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)

    @property
    def over_budget(self) -> bool:
        return self.after_bytes > self.policy.budget_bytes

    def summary(self) -> str:
        """单行中文摘要，供日志与 ``--compact-artifacts`` 输出。"""
        budget = _human(self.policy.budget_bytes)
        if not self.triggered:
            return (f"上下文占用 {_human(self.before_bytes)} / {budget}，"
                    f"未逼近阈值（{self.policy.trigger_ratio:.0%}），无需压缩")
        if not self.actions and not self.applied and not self.failed:
            # 触发了但确实无事可做：证据都在 fresh_keep 保护期内，或归档槽位已满。
            if self.over_budget:
                return (f"上下文占用 {_human(self.before_bytes)} / {budget}，"
                        f"已超过预算，但已无可压缩内容"
                        f"（证据组在保护期内或归档槽位已满）")
            return (f"上下文占用 {_human(self.before_bytes)} / {budget}，"
                    f"略高于触发线（{self.policy.trigger_ratio:.0%}）"
                    f"但已无可压缩内容，无需处理")
        prefix = "预演" if self.dry_run else "已压缩"
        text = (f"{prefix}：{_human(self.before_bytes)} → {_human(self.after_bytes)}"
                f"（释放 {_human(self.saved_bytes)}，预算 {budget}）")
        if self.applied:
            text += f"，执行 {len(self.applied)} 项"
        if self.failed:
            text += f"，失败 {len(self.failed)} 项"
        return text


def _human(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GiB"  # pragma: no cover - 循环已在 GiB 收敛


# ----------------------------------------------------------------------
# 度量
# ----------------------------------------------------------------------
def _file_size(path: Path) -> int:
    try:
        return path.stat(follow_symlinks=False).st_size
    except OSError:
        return 0


def directory_footprint(root: Path) -> int:
    """递归统计 ``root`` 的占用字节数。

    不跟随符号链接（避免把外部目录算进来或顺着环跑飞），读取失败的条目按 0 计。
    ``root`` 是文件时直接返回其大小，不存在时返回 0。
    """
    try:
        if root.is_symlink():
            return 0
        if root.is_file():
            return _file_size(root)
        if not root.is_dir():
            return 0
    except OSError:
        return 0

    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def managed_paths(root: Path) -> tuple[Path, Path]:
    """返回受管路径 ``(日志目录, 归档目录)``。"""
    logs = Path(root) / "logs"
    return logs, logs / ARCHIVE_DIRNAME


@dataclass(frozen=True)
class SnapshotGroup:
    """同一次页面定位失败产生的证据文件集合。"""

    stamp: str
    files: tuple[Path, ...]
    size: int


def snapshot_groups(log_dir: Path) -> list[SnapshotGroup]:
    """按时间戳前缀分组，返回**由旧到新**排序的证据组。

    只扫描 ``log_dir`` 的直接子文件，因此 ``logs/archive/`` 里的归档不会被当成
    待压缩的证据。没有时间戳前缀的文件（例如手工放进来的说明）不参与整理。
    """
    try:
        if not log_dir.is_dir():
            return []
        entries = sorted(log_dir.iterdir())
    except OSError:
        return []

    grouped: dict[str, list[Path]] = {}
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
        except OSError:
            continue
        match = _SNAPSHOT_STAMP.match(entry.name)
        if match:
            grouped.setdefault(match.group(1), []).append(entry)

    groups = [
        SnapshotGroup(stamp, tuple(files), sum(_file_size(p) for p in files))
        for stamp, files in grouped.items()
    ]
    return sorted(groups, key=lambda group: group.stamp)


def archive_files(archive_dir: Path) -> list[Path]:
    """返回归档目录内的归档，按文件名（即时间戳）由旧到新排序。"""
    try:
        if not archive_dir.is_dir():
            return []
        items = [
            entry for entry in sorted(archive_dir.iterdir())
            if entry.is_file() and not entry.is_symlink()
            and entry.suffix in {".gz", ".tgz"}
            and not entry.name.endswith(".tmp")
        ]
    except OSError:
        return []
    return items


def _estimated_archive_bytes(paths: tuple[Path, ...]) -> int:
    """估计归档后的字节数（gzip 之后 tar 头本身也基本被压掉，开销很小）。"""
    total = 0.0
    for path in paths:
        ratio = _ESTIMATED_RATIO.get(path.suffix.lower(), _DEFAULT_ESTIMATED_RATIO)
        total += _file_size(path) * ratio
    return int(total) + 32 * len(paths) + 128


def _stamp_for(path: Path) -> str:
    """用文件 mtime 生成归档时间戳，保证计划可复现。"""
    try:
        mtime = path.stat(follow_symlinks=False).st_mtime
    except OSError:
        mtime = 0
    return _dt.datetime.fromtimestamp(mtime).strftime("%Y%m%d_%H%M%S_000000")


# ----------------------------------------------------------------------
# 规划（纯函数：只读文件系统，不写任何东西）
# ----------------------------------------------------------------------
def plan_compaction(
    root: Path,
    policy: CompactionPolicy = DEFAULT_POLICY,
    *,
    current_bytes: int | None = None,
) -> list[CompactionAction]:
    """生成整理动作列表；已低于触发线时返回空列表。

    动作严格按「信息损失从小到大」分五档，并在**预计**已压到 ``target_bytes``
    以下时提前停止，因此不会为了达标而把还能用的现场一并清掉：

    ============  ==========================================  ============  ========
    档位           动作                                          信息损失      驱动线
    ============  ==========================================  ============  ========
    1              轮转超大日志（gzip + 截断）                    无损          目标线
    2              淘汰超出 ``archive_keep`` 的既有归档           有损（最旧）  目标线
    3              把证据组压成 ``.tar.gz``                       无损          目标线
    4              继续淘汰最旧的归档                             有损          预算线
    5              直接清理最旧的候选证据组                       有损（兜底）  预算线
    ============  ==========================================  ============  ========

    **有损档位只在真的突破 ``budget_bytes`` 时才启动**：压缩不要钱，丢数据要命。
    「140 MiB → 92 MiB 且一条现场都没丢」永远优于「→ 60 MiB 但删掉 11 组现场」。
    第 3 档由新到旧归档、第 5 档由旧到新清理，两端合起来保证**越新的现场越先被
    保住**；最近 ``fresh_keep`` 组在任何档位下都不会被触碰，这构成占用的硬下限。
    """
    root = Path(root)
    logs_dir, archive_dir = managed_paths(root)
    footprint = directory_footprint(root) if current_bytes is None else current_bytes

    if footprint <= policy.trigger_bytes:
        return []

    target = policy.target_bytes
    budget = policy.budget_bytes
    projected = footprint
    actions: list[CompactionAction] = []

    def _over_target() -> bool:
        """无损档位的驱动条件：还没拿到目标余量就继续压缩。"""
        return projected > target

    def _over_budget() -> bool:
        """有损档位的驱动条件：只有真的突破预算才允许丢数据。"""
        return projected > budget

    # 1) 轮转超大日志：纯文本，压缩收益最高且完全不丢信息。
    for name in MANAGED_LOGS:
        if not _over_target():
            break
        path = root / name
        size = _file_size(path)
        if size <= policy.log_file_bytes:
            continue
        estimated = _estimated_archive_bytes((path,))
        destination = archive_dir / f"{_stamp_for(path)}_{name}.gz"
        actions.append(CompactionAction(
            kind="rotate_log",
            label=f"轮转日志 {name}（{_human(size)} → {_human(estimated)}）",
            sources=(path,),
            destination=destination,
            reclaim_bytes=max(0, size - estimated),
        ))
        projected -= max(0, size - estimated)

    # 2) 淘汰超出数量上限的既有归档：它们是最旧的数据，先清最划算。
    existing = archive_files(archive_dir)
    keep_archives = max(0, policy.archive_keep)
    pruned_existing = 0
    for archive in existing[:max(0, len(existing) - keep_archives)]:
        if not _over_target():
            break
        size = _file_size(archive)
        actions.append(CompactionAction(
            kind="prune_archive",
            label=f"淘汰超量归档 {archive.name}（{_human(size)}）",
            sources=(archive,),
            destination=archive,
            reclaim_bytes=size,
        ))
        projected -= size
        pruned_existing += 1

    # 3) 归档证据组（无损）。最近 fresh_keep 组永不触碰；归档槽位有限时**优先
    #    保留最新的现场**——排查问题时最近的失败最有价值，而第 5 档只会从最旧的
    #    一端清理，两端策略合起来保证「越新越先被保住」。
    groups = snapshot_groups(logs_dir)
    keep = max(0, policy.fresh_keep)
    protected = {group.stamp for group in groups[len(groups) - keep:]} if keep else set()
    candidates = [group for group in groups if group.stamp not in protected and group.files]

    slots = max(0, keep_archives - (len(existing) - pruned_existing))
    archived: set[str] = set()
    for group in reversed(candidates):  # 由新到旧
        if not _over_target() or slots <= 0:
            break
        estimated = _estimated_archive_bytes(group.files)
        actions.append(CompactionAction(
            kind="archive_snapshot",
            label=(f"归档证据 {group.stamp}（{len(group.files)} 个文件 "
                   f"{_human(group.size)} → {_human(estimated)}）"),
            sources=group.files,
            destination=archive_dir / f"{group.stamp}_snapshot.tar.gz",
            reclaim_bytes=max(0, group.size - estimated),
        ))
        projected -= max(0, group.size - estimated)
        archived.add(group.stamp)
        slots -= 1

    # 4) 仍然超标时继续淘汰最旧的归档（已压缩过一轮，损失相对小）。
    pruned = {action.destination for action in actions if action.kind == "prune_archive"}
    for archive in existing:
        if not _over_budget():
            break
        if archive in pruned:
            continue
        size = _file_size(archive)
        actions.append(CompactionAction(
            kind="prune_archive",
            label=f"淘汰最旧归档 {archive.name}（{_human(size)}）",
            sources=(archive,),
            destination=archive,
            reclaim_bytes=size,
        ))
        projected -= size

    # 5) 兜底：字节预算仍然压不下来时，直接清理最旧的候选证据组。
    #    只有「压缩收益不足 + 归档槽位已满」才会走到这里；最近 fresh_keep 组
    #    依旧受保护，且每清一组必定减少 projected，保证循环收敛。
    for group in candidates:
        if not _over_budget():
            break
        if group.stamp in archived:
            continue
        actions.append(CompactionAction(
            kind="discard_snapshot",
            label=f"清理最旧证据 {group.stamp}（{len(group.files)} 个文件 {_human(group.size)}）",
            sources=group.files,
            reclaim_bytes=group.size,
        ))
        projected -= group.size

    return actions


# ----------------------------------------------------------------------
# 执行
# ----------------------------------------------------------------------
def _verify_archive(path: Path, expected: dict[str, int]) -> None:
    """重新打开归档，逐成员核对名字与解压后大小，防止写出截断的包。"""
    if _file_size(path) <= 0:
        raise OSError("归档校验失败：文件为空")
    with tarfile.open(path, mode="r:gz") as tar:
        actual = {
            member.name: member.size
            for member in tar.getmembers() if member.isfile()
        }
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        wrong = sorted(
            name for name in set(actual) & set(expected)
            if actual[name] != expected[name]
        )
        detail = []
        if missing:
            detail.append(f"缺失 {missing}")
        if extra:
            detail.append(f"多余 {extra}")
        if wrong:
            detail.append(f"大小不符 {wrong}")
        raise OSError("归档校验失败：" + "；".join(detail or ["成员不一致"]))


def _write_archive(sources: tuple[Path, ...], destination: Path) -> int:
    """把 ``sources`` 无损写进 ``destination``，返回写入的字节数。

    先写 ``.tmp``，重新打开校验每个成员的**解压后大小**，再用 ``os.replace``
    原子落位。任何一步失败都会删掉临时文件并抛错，绝不会留下半截归档。
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".tmp")
    expected: dict[str, int] = {
        source.name: _file_size(source) for source in sources
    }
    try:
        with open(temp, "wb") as raw:
            # mtime=0 让同样的输入产出同样的字节，便于测试与去重。
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
                with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                    for source in sources:
                        try:
                            info = tar.gettarinfo(str(source), arcname=source.name)
                        except OSError:
                            continue
                        # 抹掉机器相关信息，归档只保留内容本身。
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        info.mtime = 0
                        # 读到的字节数以磁盘实际内容为准，写入前后一致才放行。
                        with open(source, "rb") as handle:
                            payload = handle.read()
                        info.size = len(payload)
                        expected[source.name] = len(payload)
                        tar.addfile(info, io.BytesIO(payload))
            raw.flush()
            os.fsync(raw.fileno())
        _verify_archive(temp, expected)
        size = _file_size(temp)
        os.replace(temp, destination)
    except BaseException:
        try:
            temp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 清理失败不应掩盖原始异常
            pass
        raise
    return size


def _apply_one(action: CompactionAction) -> None:
    if action.kind == "rotate_log":
        source = action.sources[0]
        destination = action.destination
        assert destination is not None
        _write_archive((source,), destination)
        # 用截断而不是删除：更新助手用 ``>>`` 追加，O_APPEND 会继续写到新末尾，
        # 不会留下稀疏空洞；Windows 上文件被占用时会抛 OSError，由上层记录失败。
        try:
            with open(source, "r+b") as handle:
                handle.truncate(0)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            # 截断失败：归档已生成但原文还在，属于重复计费而非丢数据，可接受。
            logger.debug("日志截断失败，已保留原文：%s", source, exc_info=True)
        return

    if action.kind == "archive_snapshot":
        destination = action.destination
        assert destination is not None
        _write_archive(action.sources, destination)
        # 归档校验通过后才删原文件；逐个删除，某个失败不影响其余。
        for source in action.sources:
            try:
                source.unlink(missing_ok=True)
            except OSError:
                logger.debug("删除已归档证据失败：%s", source, exc_info=True)
        return

    if action.kind == "prune_archive":
        for source in action.sources:
            source.unlink(missing_ok=True)
        return

    if action.kind == "discard_snapshot":
        # 兜底档：字节预算压不下来时直接清理最旧的证据（最近 fresh_keep 组不在
        # 动作里）。属于有损操作，因此显式警告，便于用户追溯少了的现场。
        logger.warning("上下文压缩兜底：清理最旧证据 %s",
                       "、".join(source.name for source in action.sources))
        for source in action.sources:
            source.unlink(missing_ok=True)
        return

    raise ValueError(f"未知整理动作：{action.kind}")


def apply_compaction(actions: list[CompactionAction]) -> tuple[list[str], list[str]]:
    """执行动作列表，返回 ``(已执行标签, 失败描述)``；单项失败不影响其余。"""
    applied: list[str] = []
    failed: list[str] = []
    for action in actions:
        try:
            _apply_one(action)
        except (OSError, ValueError) as exc:
            failed.append(f"{action.label} —— {exc}")
            logger.warning("上下文压缩动作失败：%s（%s）", action.label, exc)
        else:
            applied.append(action.label)
            logger.info("上下文压缩：%s", action.label)
    return applied, failed


def enforce_budget(
    root: os.PathLike[str] | str | None = None,
    policy: CompactionPolicy = DEFAULT_POLICY,
    *,
    max_passes: int = DEFAULT_MAX_PASSES,
    dry_run: bool = False,
) -> CompactionReport:
    """按策略整理 ``root``（默认用户配置目录），返回整理报告。

    估算与实际压缩比会有偏差，因此最多循环 ``max_passes`` 轮；一旦某一轮没有
    实际收益就立即停止，不会空转。``dry_run`` 只规划不动手。
    """
    target_root = Path(root) if root is not None else user_data_dir()
    before = directory_footprint(target_root)
    report = CompactionReport(
        root=target_root, policy=policy,
        before_bytes=before, after_bytes=before,
        dry_run=dry_run,
    )

    if before <= policy.trigger_bytes:
        return report

    report.triggered = True

    if dry_run:
        actions = plan_compaction(target_root, policy, current_bytes=before)
        report.actions = actions
        report.after_bytes = max(
            0, before - sum(action.reclaim_bytes for action in actions)
        )
        return report

    current = before
    for _ in range(max(1, max_passes)):
        actions = plan_compaction(target_root, policy, current_bytes=current)
        if not actions:
            break
        report.actions.extend(actions)
        applied, failed = apply_compaction(actions)
        report.applied.extend(applied)
        report.failed.extend(failed)

        measured = directory_footprint(target_root)
        if measured >= current:
            # 没有任何收益（例如文件被占用），停止以免空转。
            current = measured
            break
        current = measured
        if current <= policy.target_bytes:
            break

    report.after_bytes = current
    if report.applied or report.failed:
        logger.info("上下文自适应压缩：%s", report.summary())
    return report
