from app.design_system import (IMAGE_CACHE_LIMIT, STATUS_STYLES, TOKENS, calculate_scrollregion,
                               form_layout_mode, layout_mode, render_rounded_corner_tiles,
                               render_rounded_image, split_pane_sizes, split_ratio_for_width)


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


def test_form_layout_switches_before_controls_become_cramped():
    assert form_layout_mode(429) == "compact"
    assert form_layout_mode(430) == "wide"


def test_rounded_corner_tiles_are_small_and_dpi_specific():
    small = render_rounded_corner_tiles(12, TOKENS.surface, outline=TOKENS.border_soft, dpi_scale=1.0)
    large = render_rounded_corner_tiles(12, TOKENS.surface, outline=TOKENS.border_soft, dpi_scale=1.5)
    assert set(small) == {"top_left", "top_right", "bottom_left", "bottom_right"}
    assert small["top_left"].width < 64
    assert large["top_left"].size != small["top_left"].size


def test_render_cache_is_bounded():
    import app.design_system as design_system

    for width in range(1, IMAGE_CACHE_LIMIT + 20):
        render_rounded_image(width, 40, 20, TOKENS.surface, dpi_scale=1.0 + width / 1000)
    assert len(design_system._IMAGE_CACHE) <= IMAGE_CACHE_LIMIT


def test_scrollregion_starts_at_origin_and_includes_padding():
    assert calculate_scrollregion(300, 200, 420, 600, 24) == (0, 0, 468, 648)


def test_status_styles_cover_lifecycle_states():
    assert set(("ready", "running", "stopping", "success", "error", "updating")) <= set(STATUS_STYLES)
    assert STATUS_STYLES["running"][0] == "处理中"
    assert STATUS_STYLES["error"][1] == TOKENS.error_tint


def test_split_ratio_preserves_minimum_pane_widths():
    assert split_ratio_for_width(0.38, 1000) == 380 / (1000 - 8)
    assert split_ratio_for_width(0.30, 900) >= 380 / (900 - 8)
    assert split_ratio_for_width(0.55, 900) <= 1 - 500 / (900 - 8)


def test_split_pane_sizes_are_continuous_and_sum_to_container_width():
    left, divider, right = split_pane_sizes(1400, 0.38)
    assert (left, divider, right) == (529, 8, 863)
    assert left + divider + right == 1400
    left2, divider2, right2 = split_pane_sizes(1600, 0.38)
    assert divider2 == 8
    assert left2 + divider2 + right2 == 1600
    assert abs(left2 / (1600 - 8) - 0.38) < 0.001
