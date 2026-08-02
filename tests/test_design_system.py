from app.design_system import STATUS_STYLES, TOKENS, layout_mode


def test_design_tokens_follow_project_design_doc():
    assert TOKENS.canvas == "#f2f0eb"
    assert TOKENS.primary == "#006241"
    assert TOKENS.accent == "#00754a"
    assert TOKENS.house == "#1e3932"
    assert TOKENS.radius_card == 12
    assert TOKENS.radius_button == 50


def test_layout_switches_at_desktop_split_breakpoint():
    assert layout_mode(979) == "stacked"
    assert layout_mode(980) == "split"
    assert layout_mode(1400) == "split"


def test_status_styles_cover_lifecycle_states():
    assert set(("ready", "running", "stopping", "success", "error", "updating")) <= set(STATUS_STYLES)
    assert STATUS_STYLES["running"][0] == "处理中"
    assert STATUS_STYLES["error"][1] == TOKENS.error_tint
