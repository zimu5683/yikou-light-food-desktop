"""``app.artifact_store`` 的预算/压缩/安全边界测试。"""
from __future__ import annotations

import os
import tarfile
from pathlib import Path

import pytest

from app.artifact_store import (DEFAULT_POLICY, CompactionPolicy,
                                apply_compaction, archive_files,
                                directory_footprint, enforce_budget,
                                managed_footprint, managed_paths,
                                plan_compaction, snapshot_groups)

# 测试用的小预算：触发线 80 KiB、目标 60 KiB。
# 证据组约 20 KiB（其中 PNG 不可压、HTML/TXT 可压），因此 fresh_keep=2 的硬下限
# 约 40 KiB < 目标 60 KiB —— 预算在「只压缩」的前提下是可达的。
BUDGET = 80 * 1024
SMALL = CompactionPolicy(
    budget_bytes=BUDGET, trigger_ratio=0.8, target_ratio=0.6,
    fresh_keep=2, archive_keep=20, log_file_bytes=64 * 1024,
)
# PNG 不可压：用随机字节模拟真实截图。
_PNG_BYTES = 3 * 1024
# HTML/TXT 可压：用高度重复的文本模拟真实页面快照。
_HTML_BYTES = 16 * 1024
_TXT_BYTES = 1 * 1024
GROUP_BYTES = _PNG_BYTES + _HTML_BYTES + _TXT_BYTES


def _fill(pattern: bytes, size: int) -> bytes:
    """把 ``pattern`` 重复/裁剪成恰好 ``size`` 字节。"""
    return (pattern * (size // len(pattern) + 1))[:size]


def _snapshot(root: Path, stamp: str, *, rng: bytes = b"\xa5") -> Path:
    """写一组证据文件（.png/.html/.txt），返回 html 路径。"""
    logs, _ = managed_paths(root)
    logs.mkdir(parents=True, exist_ok=True)
    # PNG 模拟成近似不可压：伪随机但可由 rng 复现。
    (logs / f"{stamp}_定位失败.png").write_bytes(_fill(rng, _PNG_BYTES))
    (logs / f"{stamp}_定位失败.html").write_bytes(
        _fill(b"<div class='row'>x</div>", _HTML_BYTES))
    (logs / f"{stamp}_定位失败.txt").write_bytes(
        _fill(b"https://m.icall.me/admin/#/order", _TXT_BYTES))
    return logs / f"{stamp}_定位失败.html"


def _many(root: Path, count: int) -> list[str]:
    stamps = [f"20260901_{index:02d}0000_000000" for index in range(count)]
    for index, stamp in enumerate(stamps):
        _snapshot(root, stamp, rng=bytes([index % 251 + 1]))
    return stamps


# ----------------------------------------------------------------------
# 度量
# ----------------------------------------------------------------------
def test_directory_footprint_counts_nested_and_ignores_missing(tmp_path):
    assert directory_footprint(tmp_path / "nope") == 0

    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (tmp_path / "a" / "one.bin").write_bytes(b"x" * 10)
    (nested / "two.bin").write_bytes(b"y" * 25)
    assert directory_footprint(tmp_path) == 35


def test_directory_footprint_handles_single_file_and_symlink(tmp_path):
    target = tmp_path / "file.bin"
    target.write_bytes(b"z" * 7)
    assert directory_footprint(target) == 7

    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - 平台不支持
        return
    # 符号链接既不计入自身，也不把目标重复算一遍。
    assert directory_footprint(tmp_path) == 7


def test_directory_footprint_does_not_follow_directory_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"q" * 5000)
    root = tmp_path / "root"
    root.mkdir()
    (root / "small.bin").write_bytes(b"q" * 5)
    try:
        (root / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover
        return
    assert directory_footprint(root) == 5


# ----------------------------------------------------------------------
# 分组
# ----------------------------------------------------------------------
def test_snapshot_groups_bundles_by_stamp_and_sorts_oldest_first(tmp_path):
    for stamp in ("20260901_020000_000000", "20260901_010000_000000"):
        _snapshot(tmp_path, stamp)
    groups = snapshot_groups(managed_paths(tmp_path)[0])
    assert [group.stamp for group in groups] == [
        "20260901_010000_000000", "20260901_020000_000000",
    ]
    assert len(groups[0].files) == 3
    assert groups[0].size == GROUP_BYTES


def test_snapshot_groups_ignores_unstamped_files_and_archives(tmp_path):
    logs, archive_dir = managed_paths(tmp_path)
    _snapshot(tmp_path, "20260901_010000_000000")
    (logs / "说明.txt").write_text("手工放置的文件", encoding="utf-8")
    archive_dir.mkdir(parents=True)
    (archive_dir / "20260901_000000_000000_snapshot.tar.gz").write_bytes(b"old")

    groups = snapshot_groups(logs)
    assert [group.stamp for group in groups] == ["20260901_010000_000000"]
    assert archive_files(archive_dir) == [
        archive_dir / "20260901_000000_000000_snapshot.tar.gz"
    ]


# ----------------------------------------------------------------------
# 规划
# ----------------------------------------------------------------------
def test_plan_is_empty_below_trigger_threshold(tmp_path):
    _snapshot(tmp_path, "20260901_010000_000000")
    assert directory_footprint(tmp_path) <= SMALL.trigger_bytes
    assert plan_compaction(tmp_path, SMALL) == []


def test_plan_prefers_lossless_archiving_over_discarding(tmp_path):
    stamps = _many(tmp_path, 8)
    assert directory_footprint(tmp_path) > SMALL.trigger_bytes

    actions = plan_compaction(tmp_path, SMALL)

    assert actions, "超过触发线必须给出计划"
    kinds = [action.kind for action in actions]
    # 宽松预算下只应使用无损档位，绝不能出现兜底清理。
    assert "discard_snapshot" not in kinds
    assert kinds[0] == "archive_snapshot"

    archived_at = kinds.index("archive_snapshot")
    assert all(kind == "archive_snapshot" for kind in kinds[archived_at:])

    archived = {action.sources[0].name.split("_定位失败")[0]
                for action in actions if action.kind == "archive_snapshot"}
    # 最新的 fresh_keep=2 组必须原样保留。
    assert stamps[-1] not in archived
    assert stamps[-2] not in archived
    # 归档由新到旧：候选里最新的那组排在最前面。
    assert actions[0].sources[0].name.startswith(stamps[-3])


def test_enforce_budget_reaches_budget_when_compression_allows(tmp_path):
    """预算足够时，整理后的占用必须落到预算以内（目标线是滞回余量，不是硬承诺）。"""
    _many(tmp_path, 8)
    assert directory_footprint(tmp_path) > SMALL.trigger_bytes

    report = enforce_budget(tmp_path, SMALL)

    assert report.triggered
    assert report.after_bytes <= SMALL.budget_bytes
    assert not report.over_budget


def test_compression_alone_never_destroys_evidence_below_budget(tmp_path):
    """只要压缩就能压进预算，就不允许出现任何有损动作。"""
    _many(tmp_path, 8)
    report = enforce_budget(tmp_path, SMALL)

    assert report.after_bytes <= SMALL.budget_bytes
    assert not [a for a in report.actions if a.kind in {"prune_archive", "discard_snapshot"}]
    # 证据组一个都没少：5 个明文（fresh_keep）+ 归档里的组数 == 原始组数。
    logs, archive_dir = managed_paths(tmp_path)
    plain = {p.name.split("_定位失败")[0] for p in logs.iterdir() if p.is_file()}
    stored = {a.name.split("_snapshot")[0] for a in archive_files(archive_dir)}
    assert len(plain | stored) == 8


def test_plan_never_touches_fresh_groups_even_when_over_target(tmp_path):
    _many(tmp_path, 8)
    # 把目标压到远低于 fresh_keep 硬下限的水平。
    strict = CompactionPolicy(budget_bytes=BUDGET, trigger_ratio=0.8, target_ratio=0.01,
                              fresh_keep=2, archive_keep=1, log_file_bytes=64 * 1024)
    actions = plan_compaction(tmp_path, strict)

    protected = ("20260901_060000_000000", "20260901_070000_000000")
    touched = [source for action in actions for source in action.sources]
    assert not any(path.name.startswith(protected) for path in touched)


def test_enforce_budget_never_exceeds_budget_when_reachable(tmp_path):
    """即使目标线因 fresh_keep 下限不可达，也绝不能突破预算本身。"""
    _many(tmp_path, 8)
    fresh_floor = 2 * GROUP_BYTES
    strict = CompactionPolicy(
        budget_bytes=4 * GROUP_BYTES, trigger_ratio=0.8, target_ratio=0.01,
        fresh_keep=2, archive_keep=20, log_file_bytes=64 * 1024,
    )
    report = enforce_budget(tmp_path, strict)

    assert report.after_bytes <= strict.budget_bytes
    assert not report.over_budget
    assert report.after_bytes >= fresh_floor - 1  # fresh_keep 仍是硬下限


def test_plan_falls_back_to_discard_when_compression_cannot_reach_target(tmp_path):
    _many(tmp_path, 8)
    strict = CompactionPolicy(budget_bytes=BUDGET, trigger_ratio=0.8, target_ratio=0.01,
                              fresh_keep=2, archive_keep=0, log_file_bytes=64 * 1024)
    actions = plan_compaction(tmp_path, strict)
    # archive_keep=0 → 没有归档槽位，只能走兜底清理。
    assert any(action.kind == "discard_snapshot" for action in actions)


def test_discard_always_targets_the_oldest_evidence(tmp_path):
    """归档槽位不足时，被清理的必须是最旧的现场，最新的优先保住。"""
    stamps = _many(tmp_path, 10)
    strict = CompactionPolicy(budget_bytes=BUDGET, trigger_ratio=0.8, target_ratio=0.01,
                              fresh_keep=1, archive_keep=3, log_file_bytes=64 * 1024)
    actions = plan_compaction(tmp_path, strict)

    discarded = [group for action in actions if action.kind == "discard_snapshot"
                 for group in [action.sources[0].name.split("_定位失败")[0]]]
    archived = [group for action in actions if action.kind == "archive_snapshot"
                for group in [action.sources[0].name.split("_定位失败")[0]]]

    assert discarded, "槽位不足时必须清理"
    # 每一个被清理的现场都比每一个被归档的现场更旧。
    assert max(discarded) < min(archived), (discarded, archived)
    # 最新的现场永远不在清理名单里。
    assert stamps[-1] not in discarded


def test_retention_prefers_newest_groups_end_to_end(tmp_path):
    stamps = _many(tmp_path, 10)
    strict = CompactionPolicy(budget_bytes=BUDGET, trigger_ratio=0.8, target_ratio=0.01,
                              fresh_keep=1, archive_keep=3, log_file_bytes=64 * 1024)
    enforce_budget(tmp_path, strict)

    logs, archive_dir = managed_paths(tmp_path)
    alive = {path.name.split("_定位失败")[0] for path in logs.iterdir() if path.is_file()}
    for archive in archive_files(archive_dir):
        alive.add(archive.name.split("_snapshot")[0])

    # 最新的现场一定还在（明文或归档里）。
    assert stamps[-1] in alive
    # 被丢掉的只能是较旧的那一端。
    gone = [stamp for stamp in stamps if stamp not in alive]
    assert gone, "预算极紧时应当有现场被清理"
    assert max(gone) < max(alive)


def test_plan_rotates_oversized_log(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "update.log").write_bytes(b"L" * 40_000)
    policy = CompactionPolicy(budget_bytes=1024, trigger_ratio=0.8, target_ratio=0.6,
                              fresh_keep=2, archive_keep=3, log_file_bytes=1024)
    actions = plan_compaction(tmp_path, policy)
    rotate = [a for a in actions if a.kind == "rotate_log"]
    assert len(rotate) == 1
    assert rotate[0].sources == (tmp_path / "update.log",)
    assert rotate[0].destination.name.endswith("_update.log.gz")


def test_plan_prunes_archives_beyond_keep_without_any_snapshots(tmp_path):
    _, archive_dir = managed_paths(tmp_path)
    archive_dir.mkdir(parents=True)
    for index in range(6):
        (archive_dir / f"2026090{index}_010000_000000_snapshot.tar.gz").write_bytes(b"a" * 20000)
    policy = CompactionPolicy(budget_bytes=40_000, trigger_ratio=0.8, target_ratio=0.6,
                              fresh_keep=2, archive_keep=2, log_file_bytes=64 * 1024)
    actions = plan_compaction(tmp_path, policy)
    prune = [a for a in actions if a.kind == "prune_archive"]
    assert prune, "超预算时应淘汰最旧归档"
    assert prune[0].destination.name.startswith("20260900")


# ----------------------------------------------------------------------
# 执行：无损、可恢复、幂等
# ----------------------------------------------------------------------
def test_enforce_budget_is_lossless_and_respects_budget(tmp_path):
    stamps = _many(tmp_path, 8)
    payload = {stamp: (managed_paths(tmp_path)[0] / f"{stamp}_定位失败.html").read_bytes()
               for stamp in stamps}

    report = enforce_budget(tmp_path, SMALL)

    assert report.triggered
    assert report.after_bytes <= SMALL.budget_bytes
    assert report.saved_bytes > 0
    assert not report.failed
    assert not report.over_budget

    logs, archive_dir = managed_paths(tmp_path)
    # 最新 fresh_keep 组仍是明文，可直接打开。
    for stamp in stamps[-2:]:
        assert (logs / f"{stamp}_定位失败.html").is_file()

    # 被归档的证据能从 .tar.gz 里按原字节还原 —— 无损。
    restored = {}
    for archive in archive_files(archive_dir):
        with tarfile.open(archive, mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.isfile():
                    restored[member.name] = tar.extractfile(member).read()
    for stamp, original in payload.items():
        name = f"{stamp}_定位失败.html"
        if not (logs / name).exists():
            assert restored[name] == original, f"{name} 归档后内容不一致"


def test_enforce_budget_is_idempotent_and_quiet_when_below_trigger(tmp_path):
    _many(tmp_path, 8)
    first = enforce_budget(tmp_path, SMALL)
    assert first.triggered and first.applied

    second = enforce_budget(tmp_path, SMALL)
    assert not second.triggered
    assert second.applied == []
    assert second.after_bytes == directory_footprint(tmp_path)


def test_enforce_budget_on_untouched_directory_reports_quiet(tmp_path):
    _snapshot(tmp_path, "20260901_010000_000000")
    report = enforce_budget(tmp_path, SMALL)
    assert not report.triggered
    assert "未逼近阈值" in report.summary()


def test_enforce_budget_dry_run_changes_nothing(tmp_path):
    _many(tmp_path, 8)
    before = directory_footprint(tmp_path)

    report = enforce_budget(tmp_path, SMALL, dry_run=True)

    assert report.dry_run and report.triggered
    assert report.actions, "预演也应给出计划"
    assert report.after_bytes < before
    assert directory_footprint(tmp_path) == before
    assert archive_files(managed_paths(tmp_path)[1]) == []


def test_enforce_budget_rotates_large_log_without_losing_lines(tmp_path):
    log = tmp_path / "update.log"
    log.write_text("旧版本更新记录：已下载并校验\n" * 4000, encoding="utf-8")
    original = log.read_text(encoding="utf-8")
    policy = CompactionPolicy(budget_bytes=64 * 1024, trigger_ratio=0.8, target_ratio=0.6,
                              fresh_keep=2, archive_keep=5, log_file_bytes=8 * 1024)

    report = enforce_budget(tmp_path, policy)

    assert report.triggered
    assert any(action.kind == "rotate_log" for action in report.actions)
    assert log.read_text(encoding="utf-8") == ""
    _, archive_dir = managed_paths(tmp_path)
    archives = archive_files(archive_dir)
    assert len(archives) == 1
    with tarfile.open(archives[0], mode="r:gz") as tar:
        member = next(m for m in tar.getmembers() if m.isfile())
        assert tar.extractfile(member).read().decode("utf-8") == original


def test_apply_compaction_isolates_single_failure(tmp_path, monkeypatch):
    from app import artifact_store

    logs, _ = managed_paths(tmp_path)
    logs.mkdir(parents=True)
    good = logs / "20260901_010000_000000_定位失败.txt"
    good.write_bytes(b"ok")
    doomed = logs / "20260901_020000_000000_定位失败.txt"
    doomed.write_bytes(b"boom")

    policy = CompactionPolicy(budget_bytes=1, trigger_ratio=1.0, target_ratio=1.0,
                              fresh_keep=0, archive_keep=5, log_file_bytes=1)
    actions = plan_compaction(tmp_path, policy)
    assert len(actions) == 2

    real_write = artifact_store._write_archive

    def flaky(sources, destination):
        if any(source == doomed for source in sources):
            raise OSError("模拟磁盘错误")
        return real_write(sources, destination)

    monkeypatch.setattr(artifact_store, "_write_archive", flaky)
    applied, failed = apply_compaction(actions)

    assert len(failed) == 1 and "模拟磁盘错误" in failed[0]
    assert len(applied) == 1
    # 失败的那组原文件必须原样保留，成功的才能被删。
    assert doomed.read_bytes() == b"boom"
    assert not good.exists()


def test_compaction_leaves_unmanaged_files_alone(tmp_path):
    keep = tmp_path / "config.json"
    keep.write_text('{"secret": true}', encoding="utf-8")
    webview = tmp_path / "webview" / "Local Storage"
    webview.mkdir(parents=True)
    (webview / "settings.localstorage").write_bytes(b"theme=dark")

    _many(tmp_path, 8)
    enforce_budget(tmp_path, SMALL)

    assert keep.read_text(encoding="utf-8") == '{"secret": true}'
    assert (webview / "settings.localstorage").read_bytes() == b"theme=dark"


def test_report_summary_mentions_savings(tmp_path):
    _many(tmp_path, 8)
    summary = enforce_budget(tmp_path, SMALL).summary()
    assert "已压缩" in summary and "预算" in summary and "释放" in summary


def test_report_summary_mentions_dry_run(tmp_path):
    _many(tmp_path, 8)
    assert "预演" in enforce_budget(tmp_path, SMALL, dry_run=True).summary()


def test_managed_footprint_ignores_unmanaged_files(tmp_path):
    """预算是「受管产物」的预算，webview 缓存等不该算进来。"""
    root = tmp_path / "cfg"
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "20260901_010000_000000_定位失败.html").write_bytes(b"h" * 100)
    (root / "update.log").write_bytes(b"l" * 50)
    # 管不了的大文件：不该计入预算。
    (root / "webview").mkdir()
    (root / "webview" / "cache.bin").write_bytes(b"c" * 5_000_000)
    (root / "config.json").write_bytes(b"k" * 1000)

    assert directory_footprint(root) > 5_000_000
    assert managed_footprint(root) == 150


def test_huge_unmanaged_files_never_trigger_compaction(tmp_path):
    """只有 webview 缓存超标时，绝不能因此删掉证据。"""
    root = tmp_path / "cfg"
    (root / "logs").mkdir(parents=True)
    for stamp in ("20260901_010000_000000", "20260901_020000_000000"):
        (root / "logs" / f"{stamp}_定位失败.html").write_bytes(b"evidence" * 10)
    (root / "webview").mkdir()
    (root / "webview" / "cache.bin").write_bytes(b"c" * 20_000_000)

    report = enforce_budget(root, SMALL)

    assert not report.triggered
    assert report.applied == []
    # 两条证据原封不动。
    assert len(list((root / "logs").glob("*_定位失败.html"))) == 2


def test_report_and_plan_use_managed_footprint(tmp_path):
    root = tmp_path / "cfg"
    (root / "logs").mkdir(parents=True)
    _snapshot(root, "20260901_010000_000000")
    before = managed_footprint(root)
    report = enforce_budget(root, SMALL)
    assert report.before_bytes == before
    assert report.after_bytes == managed_footprint(root)


def test_report_summary_is_honest_when_nothing_is_compressible(tmp_path):
    """触发线之上但已无可压缩内容时，不能谎报「已释放」字节。"""
    _, archive_dir = managed_paths(tmp_path)
    archive_dir.mkdir(parents=True)
    # 归档槽位已满 + 没有明文证据 → 计划为空。
    for index in range(3):
        (archive_dir / f"2026090{index}_010000_000000_snapshot.tar.gz").write_bytes(b"a" * 300)
    policy = CompactionPolicy(budget_bytes=1000, trigger_ratio=0.8, target_ratio=0.6,
                              fresh_keep=2, archive_keep=3, log_file_bytes=64 * 1024)

    report = enforce_budget(tmp_path, policy)

    assert report.triggered and not report.applied and not report.actions
    assert report.saved_bytes == 0
    summary = report.summary()
    assert "已压缩" not in summary and "释放" not in summary
    assert "无需处理" in summary


def test_default_policy_matches_stated_100mib_threshold():
    assert DEFAULT_POLICY.budget_bytes == 100 * 1024 * 1024
    assert DEFAULT_POLICY.trigger_bytes == 80 * 1024 * 1024
    assert DEFAULT_POLICY.target_bytes == 60 * 1024 * 1024


def test_enforce_budget_defaults_to_user_data_dir(tmp_path, monkeypatch):
    from app import artifact_store

    monkeypatch.setattr(artifact_store, "user_data_dir", lambda: tmp_path)
    report = enforce_budget()
    assert report.root == tmp_path
    assert not report.triggered  # 空目录远低于预算


def test_apply_compaction_rejects_unknown_kind():
    from app.artifact_store import CompactionAction

    applied, failed = apply_compaction([CompactionAction(kind="explode", label="未知")])
    assert applied == []
    assert failed and "未知整理动作" in failed[0]


def test_archive_is_actually_smaller_for_compressible_text(tmp_path):
    logs, _ = managed_paths(tmp_path)
    logs.mkdir(parents=True)
    raw = b"<html><body><div class='order'>x</div></body></html>" * 2000
    (logs / "20260901_010000_000000_定位失败.html").write_bytes(raw)

    policy = CompactionPolicy(budget_bytes=1024, trigger_ratio=0.8, target_ratio=0.6,
                              fresh_keep=0, archive_keep=5, log_file_bytes=64 * 1024)
    report = enforce_budget(tmp_path, policy)

    assert report.triggered and not report.failed
    _, archive_dir = managed_paths(tmp_path)
    archives = archive_files(archive_dir)
    assert len(archives) == 1
    assert os.path.getsize(archives[0]) < len(raw) // 10


def test_broken_source_file_never_produces_a_partial_archive(tmp_path, monkeypatch):
    """归档中途源文件消失时，必须整体失败而不是留下半截归档。"""
    logs, archive_dir = managed_paths(tmp_path)
    logs.mkdir(parents=True)
    stamps = ["20260901_010000_000000", "20260901_020000_000000"]
    for stamp in stamps:
        (logs / f"{stamp}_定位失败.html").write_bytes(b"<p>evidence</p>" * 50)

    actions = plan_compaction(tmp_path, CompactionPolicy(
        budget_bytes=1, trigger_ratio=1.0, target_ratio=1.0,
        fresh_keep=0, archive_keep=5,
    ))

    # 模拟第二个文件在打包过程中被外部删除。
    real_gettarinfo = tarfile.TarFile.gettarinfo

    def vanishing(self, name, arcname=None):
        info = real_gettarinfo(self, name, arcname)
        Path(name).unlink(missing_ok=True)
        return info

    monkeypatch.setattr(tarfile.TarFile, "gettarinfo", vanishing)
    applied, failed = apply_compaction(actions)
    monkeypatch.undo()

    assert failed, "成员消失时必须报失败"
    assert applied == []
    assert archive_files(archive_dir) == []
    # 临时文件必须被清理干净。
    assert [p.name for p in archive_dir.iterdir()] == []


def test_enforce_budget_stops_when_no_progress_is_possible(tmp_path, monkeypatch):
    """文件被占用导致压缩无收益时必须停止循环，不能空转。"""
    from app import artifact_store

    _many(tmp_path, 8)

    def noop_apply(actions):
        return [], [f"{action.label} —— 模拟失败" for action in actions]

    monkeypatch.setattr(artifact_store, "apply_compaction", noop_apply)
    report = enforce_budget(tmp_path, SMALL, max_passes=3)

    assert report.triggered
    assert report.after_bytes == directory_footprint(tmp_path)
    assert report.saved_bytes == 0
    assert report.failed


@pytest.mark.parametrize("count", [3, 5, 12])
def test_enforce_budget_terminates_for_various_sizes(tmp_path, count):
    _many(tmp_path, count)
    report = enforce_budget(tmp_path, SMALL)
    assert report.after_bytes <= max(SMALL.budget_bytes, directory_footprint(tmp_path))
    assert report.after_bytes == directory_footprint(tmp_path)
