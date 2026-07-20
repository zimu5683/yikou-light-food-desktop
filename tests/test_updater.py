import io
import base64
import hashlib
import os

import pytest

from app.updater import ReleaseInfo, UpdateError, check_for_update, compare_versions, download_and_install


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
    encoded = launched[0][0][-1]
    script = base64.b64decode(encoded).decode("utf-16le")
    assert str(target.resolve()) in script
    target.with_name(f".{target.stem}.update-{os.getpid()}.tmp").unlink()


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
