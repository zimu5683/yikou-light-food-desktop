import io
import hashlib
import os
import urllib.error
from pathlib import Path

import pytest

from app.updater import ReleaseInfo, UpdateError, apply_pending_update, check_for_update, compare_versions, download_and_install


def test_compare_versions_handles_release_and_prerelease():
    assert compare_versions("v1.2.0", "1.1.9") > 0
    assert compare_versions("1.2.0", "1.2.0-rc.1") > 0
    assert compare_versions("1.2.0-rc.2", "1.2.0-rc.10") < 0


def test_check_for_update_decodes_release_payload():
    payload = b'{"tag_name":"v1.3.0","name":"Feature update","body":"- New order log","html_url":"https://github.com/zimu5683/yikou-light-food-desktop/releases/tag/v1.3.0","assets":[]}'

    def opener(_request, timeout):
        assert timeout == 2
        return io.BytesIO(payload)

    release = check_for_update("1.2.0", timeout=2, opener=opener)
    assert release is not None
    assert release.version == "1.3.0"
    assert "New order log" in release.body


def test_check_for_update_returns_none_when_current_is_latest():
    payload = b'{"tag_name":"v1.2.0","name":"Current","body":""}'

    def opener(_request, timeout):
        return io.BytesIO(payload)

    assert check_for_update("1.2.0", opener=opener) is None


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
