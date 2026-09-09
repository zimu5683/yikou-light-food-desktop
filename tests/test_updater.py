import base64
import hashlib
import io
import json
import os
import shlex
import shutil
import tarfile
import urllib.error
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import updater
from app.updater import ReleaseInfo, UpdateError, apply_pending_update, check_for_update, compare_versions, download_and_install

_linux_only = pytest.mark.skipif(
    os.name != "posix",
    reason="Linux 更新流程测试会把全局 os.name 补丁成 posix，"
           "在 Windows 上会产生跨平台路径残留并击穿 pytest，仅在有真实 posix 语义的系统运行",
)



def test_compare_versions_handles_release_and_prerelease():
    assert compare_versions("v1.2.0", "1.1.9") > 0
    assert compare_versions("1.2.0", "1.2.0-rc.1") > 0
    assert compare_versions("1.2.0-rc.2", "1.2.0-rc.10") < 0


def _manifest_opener(manifest: dict, monkeypatch, *, private_key: Ed25519PrivateKey | None = None):
    """生成带 Ed25519 签名的 latest.json/.sig opener，并临时替换内置公钥。"""
    key = private_key or Ed25519PrivateKey.generate()
    public_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = hashlib.sha256(public_raw).hexdigest()[:16]
    monkeypatch.setattr(updater, "UPDATE_MANIFEST_PUBLIC_KEY",
                        base64.b64encode(public_raw).decode("ascii"))
    monkeypatch.setattr(updater, "UPDATE_MANIFEST_KEY_ID", key_id)
    manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    signature = key.sign(manifest_bytes)
    sig_bytes = json.dumps({
        "algorithm": "ed25519",
        "key_id": key_id,
        "signature": base64.b64encode(signature).decode("ascii"),
    }).encode("utf-8")

    def opener(request, timeout):
        assert timeout == 2 or timeout == 5.0
        return io.BytesIO(sig_bytes if request.full_url.endswith(".sig") else manifest_bytes)

    return opener


def test_check_for_update_verifies_signed_manifest(monkeypatch):
    opener = _manifest_opener({
        "schema_version": 1,
        "version": "v1.3.0",
        "name": "Feature update",
        "body": "- New order log",
        "url": "https://github.com/zimu5683/yikou-light-food-desktop/releases/tag/v1.3.0",
        "assets": [],
    }, monkeypatch)

    release = check_for_update("1.2.0", timeout=2, opener=opener)
    assert release is not None
    assert release.version == "1.3.0"
    assert "New order log" in release.body
    assert release.manifest_sha256
    assert release.manifest_key_id


def test_check_for_update_returns_none_when_current_is_latest(monkeypatch):
    opener = _manifest_opener({"schema_version": 1, "version": "v1.2.0",
                               "name": "Current", "body": ""}, monkeypatch)
    assert check_for_update("1.2.0", opener=opener) is None


def test_check_for_update_rejects_bad_signature(monkeypatch):
    opener = _manifest_opener({"schema_version": 1, "version": "v1.3.0",
                               "name": "Bad", "body": ""}, monkeypatch)
    # 篡改签名文件内容，验证失败必须拒绝，不能回退到 GitHub API。
    original = opener

    def tampered(request, timeout):
        response = original(request, timeout)
        if request.full_url.endswith(".sig"):
            return io.BytesIO(b'{"algorithm":"ed25519","signature":"AAAA"}')
        return response

    with pytest.raises(UpdateError, match="签名验证失败"):
        check_for_update("1.2.0", opener=tampered)


def test_check_for_update_rejects_downgrade(monkeypatch):
    opener = _manifest_opener({"schema_version": 1, "version": "v1.0.0",
                               "name": "Old", "body": ""}, monkeypatch)
    with pytest.raises(UpdateError, match="拒绝降级"):
        check_for_update("2.0.0", opener=opener)


def test_decode_manifest_rejects_non_semver():
    with pytest.raises(UpdateError, match="SemVer"):
        updater._decode_manifest({"schema_version": 1, "version": "latest"})


def test_release_executable_asset_is_selected():
    from app.updater import _decode_release

    release = _decode_release({
        "tag_name": "v1.3.0",
        "assets": [{"name": "notes.txt"}, {"name": "yikou-light-food.exe", "browser_download_url": "https://github.com/example.exe"}],
    })
    assert release.executable_asset["name"] == "yikou-light-food.exe"


@pytest.mark.skipif(os.name != "nt", reason="automatic installer is Windows-only")
def test_download_reports_progress_and_supports_unicode_paths(tmp_path, monkeypatch):
    payload = b"MZ" + b"x" * 1_000_000
    checksum = hashlib.sha256(payload).hexdigest().encode("ascii") + b"  yikou-light-food.exe\n"

    class Response(io.BytesIO):
        headers = {"Content-Length": str(len(payload))}

    release = ReleaseInfo(
        tag_name="v1.3.2",
        name="v1.3.2",
        body="",
        html_url="",
        assets=(
            {
                "name": "yikou-light-food.exe",
                "browser_download_url": "https://github.com/zimu5683/yikou-light-food-desktop/releases/download/v1.3.2/yikou-light-food.exe",
            },
            {
                "name": "yikou-light-food.exe.sha256",
                "browser_download_url": "https://github.com/zimu5683/yikou-light-food-desktop/releases/download/v1.3.2/yikou-light-food.exe.sha256",
            },
        ),
    )
    target = tmp_path / "一口轻食.exe"
    progress = []
    launched = []
    monkeypatch.setattr("app.updater.subprocess.Popen", lambda args, **kwargs: launched.append((args, kwargs)))

    download_and_install(
        release,
        current_executable=target,
        opener=lambda request, timeout: Response(checksum if request.full_url.endswith(".sha256") else payload),
        progress_callback=lambda downloaded, total: progress.append((downloaded, total)),
    )

    assert progress[0] == (0, len(payload))
    assert progress[-1] == (len(payload), len(payload))
    helper_args = launched[0][0]
    assert helper_args[1] == "--apply-update"
    assert helper_args[3] == str(target.resolve())
    helper = Path(helper_args[0])
    assert helper.is_file()
    helper.unlink()
    target.with_name(f".{target.stem}.update-{os.getpid()}.tmp").unlink()


def test_pending_update_atomically_replaces_and_restarts(tmp_path, monkeypatch):
    source = tmp_path / "download.tmp"
    target = tmp_path / "一口轻食.exe"
    source.write_bytes(b"new executable")
    target.write_bytes(b"old executable")
    launched = []
    monkeypatch.setattr("app.updater._schedule_helper_cleanup", lambda _helper: None)

    apply_pending_update(source, target, launcher=lambda args, **kwargs: launched.append((args, kwargs)))

    assert target.read_bytes() == b"new executable"
    assert not source.exists()
    assert launched[0][0] == [str(target.resolve())]


def test_source_mode_cannot_replace_python_executable(monkeypatch):
    release = ReleaseInfo("v1.4.0", "v1.4.0", "", "", ())
    monkeypatch.setattr("app.updater.os.name", "nt")
    monkeypatch.delattr("app.updater.sys.frozen", raising=False)
    with pytest.raises(UpdateError, match="源码运行模式"):
        download_and_install(release)


def test_only_canonical_executable_asset_is_selected():
    release = ReleaseInfo(
        "v1.4.0", "v1.4.0", "", "",
        ({"name": "helper.exe", "browser_download_url": "https://github.com/helper.exe"},),
    )
    assert release.executable_asset is None


def test_select_platform_assets_filters_linux_archives():
    from app.updater import select_platform_assets

    assets = (
        {"name": "yikou-light-food.exe"},
        {"name": "yikou-light-food-macos.zip"},
        {"name": "yikou-light-food-linux-x64.tar.gz"},
        {"name": "yikou-light-food-linux-x64.tar.gz.sha256"},
        {"name": "latest.json"},
    )
    selected = select_platform_assets(assets, platform="linux", architecture="x64")
    assert [item["name"] for item in selected] == ["yikou-light-food-linux-x64.tar.gz"]


def test_release_linux_assets_are_selected():
    from app.updater import _decode_release

    release = _decode_release({
        "tag_name": "v2.1.0",
        "assets": [
            {"name": "yikou-light-food-linux-x64.tar.gz", "browser_download_url": "https://github.com/example/yikou-light-food-linux-x64.tar.gz"},
            {"name": "yikou-light-food-linux-x64.tar.gz.sha256", "browser_download_url": "https://github.com/example/yikou-light-food-linux-x64.tar.gz.sha256"},
        ],
    })
    assert release.linux_asset["name"] == "yikou-light-food-linux-x64.tar.gz"
    assert release.linux_checksum_asset["name"] == "yikou-light-food-linux-x64.tar.gz.sha256"


@_linux_only
def test_linux_source_mode_cannot_auto_install(monkeypatch):
    # 源码运行模式下 sys.executable 是 python 解释器，替换它会损坏 Python 安装；
    # 此时仅提示前往 Release 页面手动下载。
    monkeypatch.setattr("app.updater.sys.platform", "linux")
    monkeypatch.setattr("app.updater.os.name", "posix")
    monkeypatch.delattr("app.updater.sys.frozen", raising=False)
    release = ReleaseInfo(
        "v2.1.0", "v2.1.0", "", "",
        ({"name": "yikou-light-food-linux-x64.tar.gz", "browser_download_url": "https://github.com/example/yikou-light-food-linux-x64.tar.gz"},),
    )
    with pytest.raises(UpdateError, match="源码运行模式"):
        download_and_install(release)


def _build_linux_archive(tmp_path: Path, *, payload: bytes = b"#!/bin/sh\necho new\n") -> tuple[bytes, str]:
    """构造一个假的 Linux 更新包（tar.gz 内含单个可执行文件），返回内容与哈希。"""
    source = tmp_path / "yikou-light-food"
    source.write_bytes(payload)
    source.chmod(0o755)
    archive_path = tmp_path / "archive.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(source, arcname="yikou-light-food")
    data = archive_path.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def _linux_release(archive_sha: str | None) -> ReleaseInfo:
    asset: dict = {
        "name": "yikou-light-food-linux-x64.tar.gz",
        "browser_download_url": "https://github.com/zimu5683/yikou-light-food-desktop/releases/download/v2.2.0/yikou-light-food-linux-x64.tar.gz",
    }
    if archive_sha is not None:
        asset["sha256"] = archive_sha
    return ReleaseInfo("v2.2.0", "v2.2.0", "", "", (asset,))


def _linux_install_env(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """伪造 frozen 模式与已安装位置，返回 (安装目录, 可执行文件路径)。"""
    install_dir = tmp_path / "app"
    install_dir.mkdir()
    target = install_dir / "yikou-light-food"
    target.write_bytes(b"old binary")
    monkeypatch.setattr("app.updater.sys.platform", "linux")
    monkeypatch.setattr("app.updater.os.name", "posix")
    monkeypatch.setattr("app.updater.sys.frozen", True, raising=False)
    monkeypatch.setattr("app.updater.sys.executable", str(target))
    return install_dir, target


@_linux_only
def test_linux_download_and_install_replaces_binary_and_relaunches(tmp_path, monkeypatch):
    # 打包版在 Linux 上应自动下载、校验、解压，并调度退出后的原子替换与重启。
    archive_bytes, archive_sha = _build_linux_archive(tmp_path)
    install_dir, target = _linux_install_env(tmp_path, monkeypatch)
    monkeypatch.setattr("app.updater._stream_download",
                        lambda url, destination, **kwargs: destination.write_bytes(archive_bytes))
    monkeypatch.setattr("app.updater.tempfile.mkdtemp", lambda prefix: str(tmp_path / "work"))
    (tmp_path / "work").mkdir()
    launched = []
    monkeypatch.setattr("app.updater.subprocess.Popen", lambda args, **kwargs: launched.append((args, kwargs)))

    result = download_and_install(_linux_release(archive_sha))

    assert result == target.resolve()
    staged_dir = install_dir / f".yikou-light-food.update-{os.getpid()}"
    staged_binary = staged_dir / "yikou-light-food"
    assert staged_binary.is_file()
    assert os.access(staged_binary, os.X_OK)
    # Popen 被替换后 shell 不会真的运行，workdir 应留给替换脚本清理。
    assert (tmp_path / "work").is_dir()
    assert launched, "应调度替换脚本"
    args, kwargs = launched[0]
    assert args[:2] == ["/bin/sh", "-c"]
    script = args[2]
    assert f"kill -0 {os.getpid()}" in script
    assert f"mv -f {shlex.quote(str(staged_binary))} {shlex.quote(str(target.resolve()))}" in script
    assert "YIKOU_UPDATE_HEALTH_FILE" in script
    assert "kill $new_pid" in script
    assert kwargs["start_new_session"] is True
    shutil.rmtree(staged_dir)


@_linux_only
def test_linux_rejects_untrusted_archive_url(tmp_path, monkeypatch):
    _linux_install_env(tmp_path, monkeypatch)
    release = ReleaseInfo(
        "v2.2.0", "v2.2.0", "", "",
        ({"name": "yikou-light-food-linux-x64.tar.gz",
          "browser_download_url": "https://evil.example/yikou-light-food-linux-x64.tar.gz"},),
    )
    with pytest.raises(UpdateError, match="trusted Linux archive"):
        download_and_install(release)


@_linux_only
def test_linux_rejects_checksum_mismatch(tmp_path, monkeypatch):
    # 哈希不一致必须拒绝安装，且不留下 staging / 临时文件。
    archive_bytes, _ = _build_linux_archive(tmp_path)
    install_dir, _target = _linux_install_env(tmp_path, monkeypatch)
    monkeypatch.setattr("app.updater._stream_download",
                        lambda url, destination, **kwargs: destination.write_bytes(archive_bytes))
    monkeypatch.setattr("app.updater.tempfile.mkdtemp", lambda prefix: str(tmp_path / "work"))
    (tmp_path / "work").mkdir()

    with pytest.raises(UpdateError, match="SHA-256 verification"):
        download_and_install(_linux_release("0" * 64))

    assert not (tmp_path / "work").exists()
    assert not list(install_dir.glob(".yikou-light-food.update-*"))


@_linux_only
def test_linux_rejects_malicious_archive_member(tmp_path, monkeypatch):
    # 归档内出现 .. 上跳或链接条目时必须拒绝解压。
    evil_archive = tmp_path / "evil.tar.gz"
    with tarfile.open(evil_archive, "w:gz") as tar:
        info = tarfile.TarInfo("../evil.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    archive_bytes = evil_archive.read_bytes()
    install_dir, _target = _linux_install_env(tmp_path, monkeypatch)
    monkeypatch.setattr("app.updater._stream_download",
                        lambda url, destination, **kwargs: destination.write_bytes(archive_bytes))
    monkeypatch.setattr("app.updater.tempfile.mkdtemp", lambda prefix: str(tmp_path / "work"))
    (tmp_path / "work").mkdir()

    with pytest.raises(UpdateError, match="非法路径条目"):
        download_and_install(_linux_release(hashlib.sha256(archive_bytes).hexdigest()))

    assert not list(install_dir.glob(".yikou-light-food.update-*"))


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="root 用户不受目录权限限制")
@_linux_only
def test_linux_requires_writable_install_dir(tmp_path, monkeypatch):
    _linux_install_env(tmp_path, monkeypatch)
    (tmp_path / "app").chmod(0o555)
    try:
        with pytest.raises(UpdateError, match="安装目录不可写"):
            download_and_install(_linux_release(hashlib.sha256(b"x").hexdigest()))
    finally:
        (tmp_path / "app").chmod(0o755)


def _linux_patch_release(tmp_path: Path, patch_bytes: bytes, *, from_sha: str, target_sha: str,
                         patch_sha: str | None = None) -> ReleaseInfo:
    patch_name = "yikou-light-food-linux-x64-v2.1.1-v2.2.0.patch"
    patch_path = tmp_path / patch_name
    patch_path.write_bytes(patch_bytes)
    return ReleaseInfo(
        "v2.2.0", "v2.2.0", "", "",
        ({"name": "yikou-light-food-linux-x64.tar.gz",
          "browser_download_url": "https://github.com/zimu5683/yikou-light-food-desktop/releases/download/v2.2.0/yikou-light-food-linux-x64.tar.gz"},),
        ({
            "name": patch_name,
            "url": f"https://github.com/zimu5683/yikou-light-food-desktop/releases/download/v2.2.0/{patch_name}",
            "sha256": patch_sha or hashlib.sha256(patch_bytes).hexdigest(),
            "from_sha256": from_sha,
            "target_sha256": target_sha,
        },),
    )


@_linux_only
def test_linux_patch_update_rebuilds_and_relaunches(tmp_path, monkeypatch):
    # 本地二进制命中补丁基线时走差分更新：下载补丁 → 还原新版 → 校验 → 调度替换。
    old_binary = b"\x7fELF" + b"old" * 4096
    new_binary = b"\x7fELF" + b"new" * 4096
    install_dir, target = _linux_install_env(tmp_path, monkeypatch)
    target.write_bytes(old_binary)
    patch_bytes = b"FAKE-PATCH-BYTES"
    release = _linux_patch_release(tmp_path, patch_bytes,
                                   from_sha=hashlib.sha256(old_binary).hexdigest(),
                                   target_sha=hashlib.sha256(new_binary).hexdigest())
    monkeypatch.setattr("app.updater.tempfile.mkdtemp", lambda prefix: str(tmp_path / "work"))
    (tmp_path / "work").mkdir()
    monkeypatch.setattr("app.updater.bspatch.apply_file",
                        lambda old, patch, new: Path(new).write_bytes(new_binary))
    launched = []
    monkeypatch.setattr("app.updater.subprocess.Popen", lambda args, **kwargs: launched.append((args, kwargs)))

    result = download_and_install(release, opener=lambda request, timeout: io.BytesIO(patch_bytes))

    assert result == target.resolve()
    staged_binary = install_dir / f".yikou-light-food.update-{os.getpid()}" / "yikou-light-food"
    assert staged_binary.read_bytes() == new_binary
    assert os.access(staged_binary, os.X_OK)
    args, kwargs = launched[0]
    script = args[2]
    assert f"mv -f {shlex.quote(str(staged_binary))}" in script
    assert kwargs["start_new_session"] is True
    shutil.rmtree(install_dir / f".yikou-light-food.update-{os.getpid()}")


@_linux_only
def test_linux_patch_update_with_real_bsdiff(tmp_path, monkeypatch):
    # 有 bsdiff4 时做一次真实差分：生成补丁 → 应用 → 校验还原结果逐字节一致。
    bsdiff4 = pytest.importorskip("bsdiff4")
    old_binary = (b"\x7fELF" + b"payload-" * 2048) + b"\nold-tail"
    new_binary = (b"\x7fELF" + b"payload-" * 2048) + b"\nnew-tail-changed"
    old_path = tmp_path / "old-binary"
    old_path.write_bytes(old_binary)
    # 生成补丁需要新旧两个文件；先写出新文件再 diff。
    (tmp_path / "new-src").write_bytes(new_binary)
    patch_path = tmp_path / "real.patch"
    bsdiff4.file_diff(str(old_path), str(tmp_path / "new-src"), str(patch_path))
    patch_bytes = patch_path.read_bytes()

    install_dir, target = _linux_install_env(tmp_path, monkeypatch)
    target.write_bytes(old_binary)
    release = _linux_patch_release(tmp_path, patch_bytes,
                                   from_sha=hashlib.sha256(old_binary).hexdigest(),
                                   target_sha=hashlib.sha256(new_binary).hexdigest())
    monkeypatch.setattr("app.updater.tempfile.mkdtemp", lambda prefix: str(tmp_path / "work"))
    (tmp_path / "work").mkdir()
    launched = []
    monkeypatch.setattr("app.updater.subprocess.Popen", lambda args, **kwargs: launched.append((args, kwargs)))

    download_and_install(release, opener=lambda request, timeout: io.BytesIO(patch_bytes))

    staged_binary = install_dir / f".yikou-light-food.update-{os.getpid()}" / "yikou-light-food"
    assert staged_binary.read_bytes() == new_binary
    assert launched


@_linux_only
def test_linux_patch_sha_mismatch_is_rejected(tmp_path, monkeypatch):
    # 补丁哈希不符必须报错并清理，不允许静默回退（与 Windows 行为一致）。
    old_binary = b"\x7fELF" + b"old" * 4096
    install_dir, target = _linux_install_env(tmp_path, monkeypatch)
    target.write_bytes(old_binary)
    patch_bytes = b"FAKE-PATCH-BYTES"
    release = _linux_patch_release(tmp_path, patch_bytes,
                                   from_sha=hashlib.sha256(old_binary).hexdigest(),
                                   target_sha=hashlib.sha256(b"\x7fELFnew"),
                                   patch_sha="0" * 64)
    monkeypatch.setattr("app.updater.tempfile.mkdtemp", lambda prefix: str(tmp_path / "work"))
    (tmp_path / "work").mkdir()

    with pytest.raises(UpdateError, match="补丁 SHA-256 校验失败"):
        download_and_install(release, opener=lambda request, timeout: io.BytesIO(patch_bytes))

    assert not list(install_dir.glob(".yikou-light-food.update-*"))
    assert not (tmp_path / "work").exists()


@_linux_only
def test_linux_patch_miss_falls_back_to_full_download(tmp_path, monkeypatch):
    # 本地二进制不命中任何补丁基线时，回退为全量 tar.gz 下载。
    archive_bytes, archive_sha = _build_linux_archive(tmp_path)
    install_dir, target = _linux_install_env(tmp_path, monkeypatch)
    patch_bytes = b"FAKE-PATCH-BYTES"
    release = _linux_patch_release(tmp_path, patch_bytes,
                                   from_sha="a" * 64,  # 与本地二进制不符
                                   target_sha="b" * 64)
    release = ReleaseInfo(release.tag_name, release.name, release.body, release.html_url,
                          ({"name": "yikou-light-food-linux-x64.tar.gz",
                            "browser_download_url": "https://github.com/zimu5683/yikou-light-food-desktop/releases/download/v2.2.0/yikou-light-food-linux-x64.tar.gz",
                            "sha256": archive_sha},),
                          release.patches)
    served = []

    def fake_stream_download(url, destination, **kwargs):
        served.append(str(url))
        destination.write_bytes(archive_bytes)

    monkeypatch.setattr("app.updater._stream_download", fake_stream_download)
    monkeypatch.setattr("app.updater.tempfile.mkdtemp", lambda prefix: str(tmp_path / "work"))
    (tmp_path / "work").mkdir()
    launched = []
    monkeypatch.setattr("app.updater.subprocess.Popen", lambda args, **kwargs: launched.append((args, kwargs)))

    download_and_install(release)

    assert served == ["https://github.com/zimu5683/yikou-light-food-desktop/releases/download/v2.2.0/yikou-light-food-linux-x64.tar.gz"]
    staged_binary = install_dir / f".yikou-light-food.update-{os.getpid()}" / "yikou-light-food"
    assert staged_binary.is_file()
    assert launched


@pytest.mark.skipif(os.name != "nt", reason="automatic installer is Windows-only")
def test_checksum_falls_back_to_embedded_manifest_hash(tmp_path, monkeypatch):
    # GitHub 直连不可达、.sha256 校验文件拉不到时，使用 latest.json 内嵌哈希。
    payload = b"MZ" + b"y" * 1_000_000
    digest = hashlib.sha256(payload).hexdigest()

    def opener(request, timeout):
        if request.full_url.endswith(".sha256"):
            raise urllib.error.URLError("github unreachable")
        return io.BytesIO(payload)

    release = ReleaseInfo(
        tag_name="v1.3.3",
        name="v1.3.3",
        body="",
        html_url="",
        assets=(
            {
                "name": "yikou-light-food.exe",
                "browser_download_url": "https://github.com/zimu5683/yikou-light-food-desktop/releases/download/v1.3.3/yikou-light-food.exe",
                "sha256": digest,
            },
            {
                "name": "yikou-light-food.exe.sha256",
                "browser_download_url": "https://github.com/zimu5683/yikou-light-food-desktop/releases/download/v1.3.3/yikou-light-food.exe.sha256",
            },
        ),
    )
    target = tmp_path / "一口轻食.exe"
    launched = []
    monkeypatch.setattr("app.updater.subprocess.Popen", lambda args, **kwargs: launched.append(args))

    download_and_install(release, current_executable=target, opener=opener)

    assert launched[0][1] == "--apply-update"
    Path(launched[0][0]).unlink()
    target.with_name(f".{target.stem}.update-{os.getpid()}.tmp").unlink()


@pytest.mark.skipif(os.name != "nt", reason="automatic installer is Windows-only")
def test_update_without_checksum_source_is_rejected(tmp_path):
    # 既没有可信校验地址也没有内嵌哈希时，必须拒绝安装。
    payload = b"MZ" + b"y" * 1_000_000
    release = ReleaseInfo(
        tag_name="v1.3.3",
        name="v1.3.3",
        body="",
        html_url="",
        assets=(
            {
                "name": "yikou-light-food.exe",
                "browser_download_url": "https://github.com/zimu5683/yikou-light-food-desktop/releases/download/v1.3.3/yikou-light-food.exe",
            },
        ),
    )
    target = tmp_path / "app.exe"

    def opener(request, timeout):
        return io.BytesIO(payload)

    with pytest.raises(UpdateError, match="trusted SHA-256 checksum"):
        download_and_install(release, current_executable=target, opener=opener)
    assert not target.with_name(f".app.update-{os.getpid()}.tmp").exists()


@_linux_only
def test_linux_rejects_extra_file_in_archive(tmp_path, monkeypatch):
    evil_archive = tmp_path / "extra.tar.gz"
    with tarfile.open(evil_archive, "w:gz") as tar:
        binary = tmp_path / "yikou-light-food"
        binary.write_bytes(b"\x7fELF-payload")
        tar.add(binary, arcname="yikou-light-food")
        helper = tmp_path / "helper"
        helper.write_bytes(b"#!/bin/sh\n")
        tar.add(helper, arcname="helper")
    archive_bytes = evil_archive.read_bytes()
    install_dir, _target = _linux_install_env(tmp_path, monkeypatch)
    monkeypatch.setattr("app.updater._stream_download",
                        lambda url, destination, **kwargs: destination.write_bytes(archive_bytes))
    monkeypatch.setattr("app.updater.tempfile.mkdtemp", lambda prefix: str(tmp_path / "work"))
    (tmp_path / "work").mkdir()

    with pytest.raises(UpdateError, match="预期外文件"):
        download_and_install(_linux_release(hashlib.sha256(archive_bytes).hexdigest()))

    assert not list(install_dir.glob(".yikou-light-food.update-*"))


def test_asset_platform_and_architecture_mismatch_is_rejected():
    from app.updater import _asset_matches_platform

    assert _asset_matches_platform({"platform": "linux", "architecture": "x64"},
                                   "linux", "x64") is True
    assert _asset_matches_platform({"platform": "windows"}, "linux", "x64") is False
    assert _asset_matches_platform({"architecture": "arm64"}, "linux", "x64") is False
    assert _asset_matches_platform({"architecture": "amd64"}, "linux", "x64") is True


def test_check_for_update_rejects_current_below_minimum(monkeypatch):
    opener = _manifest_opener({
        "schema_version": 1,
        "version": "v1.6.0",
        "name": "New",
        "body": "",
        "minimum_supported_version": "2.0.0",
    }, monkeypatch)
    with pytest.raises(UpdateError, match="最低支持版本"):
        check_for_update("1.5.0", opener=opener)


def test_windows_authenticode_requires_configured_publisher(monkeypatch):
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.os, "name", "nt")
    monkeypatch.setattr(updater, "WINDOWS_AUTHENTICODE_PUBLISHER", "")
    with pytest.raises(UpdateError, match="未配置 Windows Authenticode 发布者"):
        updater._verify_windows_authenticode(Path("app.exe"))


def test_windows_authenticode_accepts_matching_publisher(monkeypatch):
    class Completed:
        returncode = 0
        stdout = "CN=Acme Software, O=Acme"
        stderr = ""

    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.os, "name", "nt")
    monkeypatch.setattr(updater, "WINDOWS_AUTHENTICODE_PUBLISHER", "CN=Acme Software")
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: Completed())

    updater._verify_windows_authenticode(Path("app.exe"))


def test_macos_codesign_requires_matching_team_id(monkeypatch):
    class Completed:
        def __init__(self, code=0, out="", err=""):
            self.returncode = code
            self.stdout = out
            self.stderr = err

    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    monkeypatch.setattr(updater, "MACOS_TEAM_ID", "TEAMID123")

    def fake_run(command, **kwargs):
        if command[0].endswith("codesign") and "-dv" in command:
            return Completed(err="TeamIdentifier=TEAMID123\n")
        return Completed()

    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    updater._verify_macos_bundle(Path("/tmp/App.app"))


def test_apply_pending_update_rolls_back_when_restart_fails(tmp_path, monkeypatch):
    source = tmp_path / "download.tmp"
    target = tmp_path / "app.exe"
    source.write_bytes(b"new executable")
    target.write_bytes(b"old executable")
    monkeypatch.setattr("app.updater._schedule_helper_cleanup", lambda _helper: None)

    def failing_launcher(*args, **kwargs):
        raise OSError("cannot exec")

    with pytest.raises(UpdateError, match="could not restart"):
        apply_pending_update(source, target, launcher=failing_launcher)

    assert target.read_bytes() == b"old executable"
    assert target.with_name("app.exe.bak").read_bytes() == b"old executable"


def test_check_for_update_rejects_replaced_manifest(monkeypatch):
    opener = _manifest_opener({"schema_version": 1, "version": "v1.3.0",
                               "name": "Good", "body": ""}, monkeypatch)

    def replaced(request, timeout):
        if request.full_url.endswith(".sig"):
            return opener(request, timeout)
        return io.BytesIO(b'{"schema_version":1,"version":"v9.9.9","name":"Evil","body":""}')

    with pytest.raises(UpdateError, match="签名验证失败"):
        check_for_update("1.2.0", opener=replaced)


def test_strict_semver_accepts_build_metadata_and_ignores_it_for_order():
    from app.updater import is_valid_semver

    assert is_valid_semver("1.2.3")
    assert is_valid_semver("v1.2.3-rc.1+build.9")
    assert not is_valid_semver("1.2")
    assert not is_valid_semver("latest")
    assert compare_versions("1.2.3+build.2", "1.2.3+build.1") == 0


def test_embedded_signed_hash_takes_priority_over_checksum_file():
    embedded = "a" * 64

    def opener(request, timeout):
        raise AssertionError("已有签名清单内嵌哈希时不应再信任独立 .sha256 文件")

    result = updater._resolve_asset_hash(
        {"name": "x", "sha256": embedded},
        "https://github.com/example/x.sha256",
        opener=opener,
        timeout=5,
    )
    assert result == embedded


def test_resolve_asset_hash_falls_back_to_checksum_file_for_old_release():
    expected = b"b" * 64 + b"  file.exe\n"

    def opener(request, timeout):
        return io.BytesIO(expected)

    result = updater._resolve_asset_hash(
        {"name": "x"},
        "https://github.com/example/x.sha256",
        opener=opener,
        timeout=5,
    )
    assert result == "b" * 64


def test_same_version_with_different_content_is_not_auto_overwritten(monkeypatch):
    opener = _manifest_opener({
        "schema_version": 1,
        "version": "v1.2.0",
        "name": "Repacked same version",
        "body": "different hash",
        "assets": [{
            "name": "yikou-light-food.exe",
            "url": "https://github.com/example/yikou-light-food.exe",
            "size": 1,
            "sha256": "f" * 64,
        }],
    }, monkeypatch)
    assert check_for_update("1.2.0", opener=opener) is None


def test_platform_metadata_can_be_required_by_new_manifest():
    release = updater._decode_manifest({
        "schema_version": 1,
        "requires_platform_metadata": True,
        "version": "v1.0.0",
        "assets": [],
    })
    assert release.require_platform_fields is True
    assert updater._asset_matches_platform(
        {}, "linux", "x64", require_declared=True) is False
    assert updater._asset_matches_platform(
        {"platform": "linux", "architecture": "x64"},
        "linux", "x64", require_declared=True) is True
    assert updater._asset_matches_platform(
        {"platform": "macos", "architecture": "universal"},
        "macos", "arm64", require_declared=True) is True


def test_apply_pending_update_waits_for_health_marker(tmp_path, monkeypatch):
    source = tmp_path / "download.tmp"
    target = tmp_path / "app.exe"
    source.write_bytes(b"new executable")
    target.write_bytes(b"old executable")
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr("app.updater._schedule_helper_cleanup", lambda _helper: None)
    launched = []

    def launcher(args, **kwargs):
        launched.append((args, kwargs))
        env = kwargs.get("env") or {}
        Path(env["YIKOU_UPDATE_HEALTH_FILE"]).write_text(
            json.dumps({"token": env["YIKOU_UPDATE_HEALTH_TOKEN"]}), encoding="utf-8")

    apply_pending_update(source, target, launcher=launcher, health_timeout=1)

    assert target.read_bytes() == b"new executable"
    assert len(launched) == 1
    assert not list(tmp_path.glob(".yikou-update-health-*.json"))


def test_apply_pending_update_rolls_back_when_health_marker_missing(tmp_path, monkeypatch):
    source = tmp_path / "download.tmp"
    target = tmp_path / "app.exe"
    source.write_bytes(b"new executable")
    target.write_bytes(b"old executable")
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr("app.updater._schedule_helper_cleanup", lambda _helper: None)
    launched = []

    def launcher(args, **kwargs):
        launched.append(args)

    with pytest.raises(UpdateError, match="健康检查失败"):
        apply_pending_update(source, target, launcher=launcher, health_timeout=0.01)

    assert target.read_bytes() == b"old executable"
    assert len(launched) == 2
    assert launched[1] == [str(target.resolve())]


def test_manifest_generator_platform_metadata_helper():
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "generate_latest_manifest.py"
    spec = importlib.util.spec_from_file_location("generate_latest_manifest_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module._asset_platform("yikou-light-food.exe") == ("windows", "x64")
    assert module._asset_platform("yikou-light-food-macos.zip") == ("macos", "universal")
    assert module._asset_platform("yikou-light-food-linux-arm64.tar.gz") == ("linux", "arm64")


def test_update_trust_loader_allows_packaged_config_or_env_override(monkeypatch):
    monkeypatch.setenv("YIKOU_WINDOWS_AUTHENTICODE_PUBLISHER", "CN=Packaged Publisher")
    monkeypatch.setenv("YIKOU_MACOS_TEAM_ID", "PACKAGEDTEAM")
    windows, macos = updater._load_update_trust()
    assert windows == "CN=Packaged Publisher"
    assert macos == "PACKAGEDTEAM"
