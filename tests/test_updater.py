import io

from app.updater import check_for_update, compare_versions


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
