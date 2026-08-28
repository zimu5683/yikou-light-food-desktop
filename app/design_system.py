"""Reusable Tkinter design primitives for the Yikou Light Food desktop app.

The values in this module are intentionally kept independent of the business
workflow so the visual system can be tested without creating a Tk root window.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
import os
import tkinter as tk
from tkinter import font as tkfont
from collections import OrderedDict
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageTk


@dataclass(frozen=True)
class DesignTokens:
    """The Yikou Light Food adaptation of the project's DESIGN.md tokens."""

    canvas: str = "#f2f0eb"
    ceramic: str = "#edebe9"
    surface: str = "#ffffff"
    surface_muted: str = "#f9f9f9"
    primary: str = "#006241"
    accent: str = "#00754a"
    house: str = "#1e3932"
    uplift: str = "#2b5148"
    green_light: str = "#d4e9e2"
    text: str = "#1f1f1f"
    text_soft: str = "#6b6b6b"
    text_on_dark: str = "#ffffff"
    text_on_dark_soft: str = "#c9d8d2"
    error: str = "#c82014"
    error_tint: str = "#fff1ef"
    warning: str = "#fbbc05"
    warning_tint: str = "#fff8df"
    focus: str = "#00754a"
    border: str = "#d6dbd8"
    border_soft: str = "#e7e7e7"
    search_hit: str = "#d4e9e2"
    shadow: str = "#d9d5cf"
    shadow_soft: str = "#e7e3de"
    radius_card: int = 12
    radius_button: int = 50
    space_1: int = 4
    space_2: int = 8
    space_3: int = 16
    space_4: int = 24
    space_5: int = 32
    space_6: int = 40
    breakpoint_split: int = 980


TOKENS = DesignTokens()

FONT_FAMILY = "Segoe UI"
FONT_FALLBACK = ("Segoe UI", "Helvetica Neue", "Arial", "sans-serif")
FONT_MONO = ("Consolas", 10)


def enable_high_dpi_awareness() -> str:
    """Enable per-monitor DPI awareness before a Tk root is created.

    The calls are deliberately best-effort: older Windows versions expose only
    one of the legacy APIs, while non-Windows platforms simply use Tk's native
    scaling.  The return value is useful for diagnostics and unit tests.
    """
    if os.name != "nt":
        return "unsupported"
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except (AttributeError, OSError, TypeError):
        pass
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE == 2.
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except (AttributeError, OSError, TypeError):
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError, TypeError):
        pass
    return "unavailable"


def apply_tk_scaling(root: tk.Misc) -> float:
    """Set Tk's point scaling from the monitor DPI and return the scale."""
    scale = 1.0
    try:
        dpi = float(root.winfo_fpixels("1i"))
        if dpi > 0:
            scale = max(0.75, min(3.0, dpi / 72.0))
            root.tk.call("tk", "scaling", scale)
    except (tk.TclError, AttributeError, TypeError, ValueError):
        pass
    return scale


IMAGE_CACHE_LIMIT = 128
_IMAGE_CACHE: OrderedDict[tuple[object, ...], Image.Image] = OrderedDict()


def _color_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(part * 2 for part in value)
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def render_rounded_image(width: int, height: int, radius: int, fill: str,
                         *, outline: str = "", shadow: str = "",
                         scale: int = 4, dpi_scale: float = 1.0) -> Image.Image:
    """Render a cached, supersampled rounded surface with antialiased edges."""
    width, height = max(1, int(width)), max(1, int(height))
    scale = max(1, int(scale))
    key = ("surface", width, height, int(radius), fill, outline, shadow, scale, round(float(dpi_scale), 2))
    cached = _IMAGE_CACHE.get(key)
    if cached is not None:
        _IMAGE_CACHE.move_to_end(key)
        return cached.copy()
    size = (width * scale, height * scale)
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    if shadow:
        shadow_layer = Image.new("RGBA", size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow_layer)
        shadow_draw.rounded_rectangle((2 * scale, 3 * scale, (width - 1) * scale, (height - 1) * scale),
                                      radius=max(1, int(radius * scale)), fill=_color_rgb(shadow) + (150,))
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(max(1, scale)))
        image.alpha_composite(shadow_layer)
    draw = ImageDraw.Draw(image)
    box = (0, 0, max(1, width * scale - 1), max(1, height * scale - 1))
    draw.rounded_rectangle(box, radius=max(1, int(radius * scale)), fill=_color_rgb(fill) + (255,),
                           outline=_color_rgb(outline) + (255,) if outline else None,
                           width=max(1, scale) if outline else 1)
    result = image.resize((width, height), Image.Resampling.LANCZOS)
    _IMAGE_CACHE[key] = result.copy()
    _IMAGE_CACHE.move_to_end(key)
    while len(_IMAGE_CACHE) > IMAGE_CACHE_LIMIT:
        _IMAGE_CACHE.popitem(last=False)
    return result


def render_rounded_corner_tiles(radius: int, fill: str, *, outline: str = "",
                                shadow: str = "", dpi_scale: float = 1.0,
                                scale: int = 4) -> dict[str, Image.Image]:
    """Create four small antialiased corner tiles for a scalable surface.

    Large cards should not allocate a full-size supersampled bitmap during a
    window resize.  A canonical 2R tile is enough because all straight
    portions are painted by Tk's fast rectangles.
    """
    radius = max(2, int(round(radius * max(0.75, float(dpi_scale)))))
    scale = max(1, int(scale))
    pad = max(2, int(round(4 * max(0.75, float(dpi_scale)))))
    key = ("corners", radius, fill, outline, shadow, scale, round(float(dpi_scale), 2), pad)
    cached = _IMAGE_CACHE.get(key)
    if cached is not None:
        _IMAGE_CACHE.move_to_end(key)
        # Corner tiles are stored as one horizontal strip to keep the cache
        # value type stable and reduce duplicate image objects.
        strip = cached.copy()
    else:
        extent = radius * 2 + pad * 2
        size = extent * scale
        strip = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
        shadow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow_layer)
        if shadow:
            shadow_draw.rounded_rectangle(
                (pad * scale, pad * scale, (extent - pad) * scale, (extent - pad) * scale),
                radius=radius * scale,
                fill=_color_rgb(shadow) + (150,),
            )
            shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(max(1, scale)))
        surface = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(surface)
        draw.rounded_rectangle(
            (0, 0, (extent - 1) * scale, (extent - 1) * scale),
            radius=radius * scale,
            fill=_color_rgb(fill) + (255,),
            outline=_color_rgb(outline) + (255,) if outline else None,
            width=max(1, scale) if outline else 1,
        )
        combined = shadow_layer.copy()
        combined.alpha_composite(surface)
        # Keep only the useful top-left quadrant and mirror it for the other
        # corners.  Resampling happens once, not once per window size.
        tile = combined.crop((0, 0, (radius + pad) * scale, (radius + pad) * scale))
        tile = tile.resize((radius + pad, radius + pad), Image.Resampling.LANCZOS)
        strip = Image.new("RGBA", ((radius + pad) * 2, (radius + pad) * 2), (0, 0, 0, 0))
        strip.alpha_composite(tile, (0, 0))
        strip.alpha_composite(tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT), (radius + pad, 0))
        strip.alpha_composite(tile.transpose(Image.Transpose.FLIP_TOP_BOTTOM), (0, radius + pad))
        strip.alpha_composite(tile.transpose(Image.Transpose.ROTATE_180), (radius + pad, radius + pad))
        _IMAGE_CACHE[key] = strip.copy()
        _IMAGE_CACHE.move_to_end(key)
        while len(_IMAGE_CACHE) > IMAGE_CACHE_LIMIT:
            _IMAGE_CACHE.popitem(last=False)
    edge = strip.width // 2
    return {
        "top_left": strip.crop((0, 0, edge, edge)),
        "top_right": strip.crop((edge, 0, edge * 2, edge)),
        "bottom_left": strip.crop((0, edge, edge, edge * 2)),
        "bottom_right": strip.crop((edge, edge, edge * 2, edge * 2)),
    }


def render_pill_caps(height: int, fill: str, *, outline: str = "",
                     dpi_scale: float = 1.0, scale: int = 4) -> tuple[Image.Image, Image.Image]:
    """Render non-overlapping antialiased left and right pill end caps.

    Unlike the card corner tiles, a pill has no separate top/bottom corners.
    The cap is exactly half of the button's content height, so the center
    rectangle can stretch to any width without ever stacking two circles.
    """
    height = max(4, int(height))
    scale = max(1, int(scale))
    dpi = max(0.75, float(dpi_scale))
    key = ("pill-caps", height, fill, outline, scale, round(dpi, 2))
    cached = _IMAGE_CACHE.get(key)
    if cached is None:
        supersampled = height * scale
        image = Image.new("RGBA", (supersampled, supersampled), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        # Keep the geometry a true pill at every monitor scale; DPI affects
        # cache identity and antialiasing policy, not the logical radius.
        radius = max(1, supersampled // 2)
        draw.rounded_rectangle(
            (0, 0, supersampled - 1, supersampled - 1),
            radius=radius,
            fill=_color_rgb(fill) + (255,),
            outline=_color_rgb(outline) + (255,) if outline else None,
            width=max(1, scale) if outline else 1,
        )
        image = image.resize((height, height), Image.Resampling.LANCZOS)
        cap_width = max(1, height // 2)
        strip = Image.new("RGBA", (cap_width * 2, height), (0, 0, 0, 0))
        strip.alpha_composite(image.crop((0, 0, cap_width, height)), (0, 0))
        strip.alpha_composite(image.crop((height - cap_width, 0, height, height)),
                              (cap_width, 0))
        cached = strip
        _IMAGE_CACHE[key] = strip.copy()
        _IMAGE_CACHE.move_to_end(key)
        while len(_IMAGE_CACHE) > IMAGE_CACHE_LIMIT:
            _IMAGE_CACHE.popitem(last=False)
    cap_width = cached.width // 2
    return cached.crop((0, 0, cap_width, height)), cached.crop((cap_width, 0, cap_width * 2, height))


def pill_cap_geometry(width: int, height: int, inset: int = 0) -> tuple[int, int, int, int, int]:
    """Return ``x0, y0, x1, y1, cap_width`` for a pill button."""
    width = max(40, int(width))
    height = max(32, int(height))
    inset = max(0, int(inset))
    inner_height = max(4, height - inset * 2)
    cap_width = max(1, inner_height // 2)
    return inset, inset, width - inset, height - inset, cap_width


def _tk_dpi_scale(widget: tk.Misc) -> float:
    try:
        value = float(widget.tk.call("tk", "scaling"))
        return max(0.75, min(3.0, value))
    except (tk.TclError, TypeError, ValueError):
        return 1.0


def _create_surface_parts(canvas: tk.Canvas, *, fill: str, outline: str,
                          shadow: str, dpi_scale: float) -> tuple[dict[str, int], list[ImageTk.PhotoImage]]:
    """Create reusable Canvas items for a scalable rounded surface."""
    tiles = render_rounded_corner_tiles(TOKENS.radius_card, fill, outline=outline,
                                        shadow=shadow, dpi_scale=dpi_scale)
    photos = [ImageTk.PhotoImage(tiles[name]) for name in
              ("top_left", "top_right", "bottom_left", "bottom_right")]
    parts: dict[str, int] = {}
    parts["shadow_h"] = canvas.create_rectangle(0, 0, 0, 0, fill=shadow, outline="", tags="card-shape")
    parts["shadow_v"] = canvas.create_rectangle(0, 0, 0, 0, fill=shadow, outline="", tags="card-shape")
    parts["fill_h"] = canvas.create_rectangle(0, 0, 0, 0, fill=fill, outline="", tags="card-shape")
    parts["fill_v"] = canvas.create_rectangle(0, 0, 0, 0, fill=fill, outline="", tags="card-shape")
    if outline:
        parts["outline_top"] = canvas.create_line(0, 0, 0, 0, fill=outline, width=1, tags="card-shape")
        parts["outline_left"] = canvas.create_line(0, 0, 0, 0, fill=outline, width=1, tags="card-shape")
        parts["outline_bottom"] = canvas.create_line(0, 0, 0, 0, fill=outline, width=1, tags="card-shape")
        parts["outline_right"] = canvas.create_line(0, 0, 0, 0, fill=outline, width=1, tags="card-shape")
    for name, photo in zip(("top_left", "top_right", "bottom_left", "bottom_right"), photos):
        parts[name] = canvas.create_image(0, 0, anchor="nw", image=photo, tags="card-shape")
    canvas.tag_lower("card-shape")
    return parts, photos


def _update_surface_parts(canvas: tk.Canvas, parts: dict[str, int], width: int, height: int,
                          photos: list[ImageTk.PhotoImage], dpi_scale: float) -> None:
    """Move existing surface items instead of rebuilding them per frame."""
    if width < 2 or height < 2 or not photos:
        return
    corner = photos[0].width()
    shadow_offset = max(1, int(round(2 * dpi_scale)))
    canvas.coords(parts["shadow_h"], corner, shadow_offset, max(corner, width - corner), height)
    canvas.coords(parts["shadow_v"], shadow_offset, corner, width, max(corner, height - corner))
    canvas.coords(parts["fill_h"], corner, 0, max(corner, width - corner), max(1, height - shadow_offset))
    canvas.coords(parts["fill_v"], 0, corner, max(1, width - shadow_offset), max(corner, height - corner))
    if "outline_top" in parts:
        canvas.coords(parts["outline_top"], corner, 0, max(corner, width - corner), 0)
        canvas.coords(parts["outline_left"], 0, corner, 0, max(corner, height - corner))
        canvas.coords(parts["outline_bottom"], corner, max(1, height - 1),
                      max(corner, width - corner), max(1, height - 1))
        canvas.coords(parts["outline_right"], max(1, width - 1), corner,
                      max(1, width - 1), max(corner, height - corner))
    positions = ((0, 0), (width - corner, 0), (0, height - corner),
                 (width - corner, height - corner))
    for name, (x, y) in zip(("top_left", "top_right", "bottom_left", "bottom_right"), positions):
        canvas.coords(parts[name], max(0, x), max(0, y))


def split_ratio_for_width(ratio: float, width: int, *, divider_width: int = 8,
                          min_left: int = 380, min_right: int = 500,
                          min_ratio: float = 0.30, max_ratio: float = 0.55) -> float:
    """Clamp a split ratio while preserving usable minimum pane widths."""
    try:
        value = float(ratio)
    except (TypeError, ValueError):
        value = 0.38
    available = max(1, int(width) - max(0, int(divider_width)))
    lower = max(float(min_ratio), min(1.0, min_left / available))
    upper = min(float(max_ratio), max(0.0, 1.0 - min_right / available))
    if lower > upper:
        # At very small sizes the stacked mode should be selected by the app,
        # but keep this helper deterministic for callers and tests.
        return max(float(min_ratio), min(float(max_ratio), value))
    return max(lower, min(upper, value))


def split_pane_sizes(width: int, ratio: float, *, divider_width: int = 8,
                     min_left: int = 380, min_right: int = 500,
                     min_ratio: float = 0.30, max_ratio: float = 0.55) -> tuple[int, int, int]:
    """Return pixel widths for the left pane, divider and right pane."""
    total = max(0, int(width))
    divider = max(0, min(total, int(divider_width)))
    available = max(0, total - divider)
    effective = split_ratio_for_width(
        ratio, total, divider_width=divider, min_left=min_left,
        min_right=min_right, min_ratio=min_ratio, max_ratio=max_ratio,
    )
    left = int(round(available * effective))
    left = max(0, min(available, left))
    if available >= min_left + min_right:
        left = max(min_left, min(available - min_right, left))
    return left, divider, available - left

STATUS_STYLES: dict[str, tuple[str, str]] = {
    "ready": ("就绪", TOKENS.green_light),
    "running": ("处理中", TOKENS.accent),
    "stopping": ("正在停止", TOKENS.warning_tint),
    "success": ("处理完成", TOKENS.green_light),
    "error": ("处理失败", TOKENS.error_tint),
    "updating": ("检查更新", TOKENS.green_light),
}


def layout_mode(width: int) -> str:
    """Return the responsive layout mode for a window width."""

    return "split" if width >= TOKENS.breakpoint_split else "stacked"


def form_layout_mode(width: int) -> str:
    """Return the left-form layout mode for the available content width."""

    return "compact" if int(width) < 430 else "wide"


def calculate_scrollregion(view_width: int, view_height: int, content_width: int,
                           content_height: int, padding: int) -> tuple[int, int, int, int]:
    """Return a scroll region anchored at the canvas origin."""
    view_width = max(1, int(view_width))
    view_height = max(1, int(view_height))
    content_width = max(1, int(content_width))
    content_height = max(1, int(content_height))
    padding = max(0, int(padding))
    return (0, 0, max(view_width, content_width + padding * 2),
            max(view_height, content_height + padding * 2))


class ResponsiveSplitPane(tk.Frame):
    """A resize-friendly two-pane container without sash repositioning.

    Tk's grid geometry manager owns the continuous resize.  The two pane
    columns share a uniform group, so changing the weights updates the saved
    ratio without a second ``sash_place`` pass during a native window
    maximize/restore animation.
    """

    def __init__(self, master: tk.Misc, *, ratio: float = 0.38,
                 min_ratio: float = 0.30, max_ratio: float = 0.55,
                 divider_width: int = 8,
                 on_ratio_committed: Callable[[float], None] | None = None,
                 **kwargs: object) -> None:
        super().__init__(master, bg=kwargs.pop("bg", TOKENS.canvas),
                         highlightthickness=0, bd=0, **kwargs)
        # The parent owns the available size.  Without disabling propagation,
        # the split-mode minimum column requests can prevent a stacked layout
        # from shrinking when the window is restored.
        self.grid_propagate(False)
        self._min_ratio = float(min_ratio)
        self._max_ratio = float(max_ratio)
        self._divider_width = max(4, int(divider_width))
        self._ratio = max(self._min_ratio, min(self._max_ratio, float(ratio)))
        self._mode = "split"
        self._dragging = False
        self._on_ratio_committed = on_ratio_committed

        self.left_host = tk.Frame(self, bg=TOKENS.canvas, bd=0, highlightthickness=0)
        self.right_host = tk.Frame(self, bg=TOKENS.canvas, bd=0, highlightthickness=0)
        self.divider = tk.Frame(self, bg=TOKENS.canvas, bd=0, highlightthickness=0,
                                width=self._divider_width, cursor="sb_h_double_arrow",
                                takefocus=True)
        self.left_host.grid(row=0, column=0, sticky="nsew")
        self.divider.grid(row=0, column=1, sticky="ns")
        self.right_host.grid(row=0, column=2, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self._apply_split_weights()

        self.divider.bind("<ButtonPress-1>", self._on_divider_press, add="+")
        self.divider.bind("<B1-Motion>", self._on_divider_motion, add="+")
        self.divider.bind("<ButtonRelease-1>", self._on_divider_release, add="+")
        self.divider.bind("<Left>", lambda _event: self._nudge(-0.01), add="+")
        self.divider.bind("<Right>", lambda _event: self._nudge(0.01), add="+")
        self.divider.bind("<Up>", lambda _event: self._nudge(-0.01), add="+")
        self.divider.bind("<Down>", lambda _event: self._nudge(0.01), add="+")

    @property
    def ratio(self) -> float:
        return self._ratio

    @property
    def mode(self) -> str:
        return self._mode

    def _apply_split_weights(self) -> None:
        left_weight = max(1, int(round(self._ratio * 1000)))
        right_weight = max(1, 1000 - left_weight)
        self.columnconfigure(0, weight=left_weight, uniform="responsive-split", minsize=380)
        self.columnconfigure(1, weight=0, minsize=self._divider_width)
        self.columnconfigure(2, weight=right_weight, uniform="responsive-split", minsize=500)
        self.rowconfigure(0, weight=1)
        self.rowconfigure(1, weight=0, minsize=0)
        self.rowconfigure(2, weight=0, minsize=0)
        self.left_host.grid_configure(row=0, column=0, columnspan=1, sticky="nsew")
        self.divider.grid_configure(row=0, column=1, columnspan=1, sticky="ns")
        self.right_host.grid_configure(row=0, column=2, columnspan=1, sticky="nsew")
        self.divider.configure(cursor="sb_h_double_arrow")

    def set_mode(self, mode: str) -> None:
        mode = "split" if mode == "split" else "stacked"
        if mode == self._mode:
            return
        self._mode = mode
        if mode == "split":
            self._apply_split_weights()
            return
        for column in range(3):
            self.columnconfigure(column, weight=0, minsize=0)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1, minsize=0)
        self.rowconfigure(1, weight=0, minsize=self._divider_width)
        self.rowconfigure(2, weight=1, minsize=0)
        self.left_host.grid_configure(row=0, column=0, columnspan=3, sticky="nsew")
        self.divider.grid_configure(row=1, column=0, columnspan=3, sticky="ew")
        self.right_host.grid_configure(row=2, column=0, columnspan=3, sticky="nsew")
        self.divider.configure(cursor="sb_v_double_arrow")

    def set_ratio(self, ratio: float) -> float:
        try:
            value = float(ratio)
        except (TypeError, ValueError):
            value = self._ratio
        self._ratio = max(self._min_ratio, min(self._max_ratio, value))
        if self._mode == "split":
            self._apply_split_weights()
        return self._ratio

    def _ratio_from_event(self, event: tk.Event[tk.Misc]) -> float:
        width = max(1, self.winfo_width())
        origin = event.x_root - self.winfo_rootx() - self._divider_width / 2
        return split_ratio_for_width(
            origin / max(1, width - self._divider_width), width,
            divider_width=self._divider_width, min_ratio=self._min_ratio,
            max_ratio=self._max_ratio,
        )

    def _on_divider_press(self, _event: tk.Event[tk.Misc]) -> None:
        if self._mode == "split":
            self._dragging = True
            self.divider.focus_set()

    def _on_divider_motion(self, event: tk.Event[tk.Misc]) -> None:
        if self._dragging and self._mode == "split":
            self.set_ratio(self._ratio_from_event(event))

    def _on_divider_release(self, _event: tk.Event[tk.Misc]) -> None:
        if not self._dragging:
            return
        self._dragging = False
        if self._on_ratio_committed is not None:
            self._on_ratio_committed(self._ratio)

    def _nudge(self, delta: float) -> str:
        if self._mode != "split":
            return "break"
        self.set_ratio(self._ratio + delta)
        if self._on_ratio_committed is not None:
            self._on_ratio_committed(self._ratio)
        return "break"


class RoundedCard(tk.Frame):
    """A rounded white surface with two restrained shadow layers."""

    def __init__(self, master: tk.Misc, *, padding: int = TOKENS.space_4, **kwargs: object) -> None:
        parent_bg = str(kwargs.pop("parent_bg", TOKENS.canvas))
        super().__init__(master, bg=parent_bg, highlightthickness=0, bd=0, **kwargs)
        self._padding = padding
        self._parent_bg = parent_bg
        self.canvas = tk.Canvas(self, bg=parent_bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=TOKENS.surface, bd=0, highlightthickness=0)
        self.body_window = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self._surface_photos: list[ImageTk.PhotoImage] = []
        self._surface_parts: dict[str, int] | None = None
        self._surface_style_key: tuple[float, str, str, str] | None = None
        self._redraw_job: str | None = None
        self._last_surface_size: tuple[int, int, float] | None = None
        self.canvas.bind("<Configure>", self._redraw)

    def _redraw(self, event: tk.Event[tk.Misc]) -> None:
        self._pending_surface_size = (max(1, event.width), max(1, event.height))
        if self._redraw_job is None:
            self._redraw_job = self.after(16, self._redraw_now)

    def _redraw_now(self) -> None:
        self._redraw_job = None
        width, height = getattr(self, "_pending_surface_size", (1, 1))
        scale = _tk_dpi_scale(self.canvas)
        size_key = (width, height, round(scale, 2))
        if size_key == self._last_surface_size:
            return
        self._last_surface_size = size_key
        style_key = (round(scale, 2), TOKENS.surface, TOKENS.border_soft, TOKENS.shadow)
        if self._surface_parts is None or style_key != self._surface_style_key:
            self.canvas.delete("card-shape")
            self._surface_parts, self._surface_photos = _create_surface_parts(
                self.canvas, fill=TOKENS.surface, outline=TOKENS.border_soft,
                shadow=TOKENS.shadow, dpi_scale=scale,
            )
            self._surface_style_key = style_key
        _update_surface_parts(self.canvas, self._surface_parts, width, height,
                              self._surface_photos, scale)
        self.canvas.itemconfigure(self.body_window, width=max(1, width - self._padding * 2 - 4),
                                  height=max(1, height - self._padding * 2 - 5))
        self.canvas.coords(self.body_window, self._padding, self._padding)

    def destroy(self) -> None:
        if self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except tk.TclError:
                pass
            self._redraw_job = None
        super().destroy()


class ScrollableRoundedCard(tk.Frame):
    """A rounded card whose content can be scrolled with mouse or keyboard."""

    def __init__(self, master: tk.Misc, *, padding: int = TOKENS.space_4, **kwargs: object) -> None:
        parent_bg = str(kwargs.pop("parent_bg", TOKENS.canvas))
        super().__init__(master, bg=parent_bg, highlightthickness=0, bd=0, **kwargs)
        self._padding = padding
        self._parent_bg = parent_bg
        self.canvas = tk.Canvas(self, bg=parent_bg, highlightthickness=0, bd=0,
                                yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                      bg=TOKENS.ceramic, troughcolor=TOKENS.surface,
                                      activebackground=TOKENS.accent, relief="flat", bd=0,
                                      width=7)
        self.body = tk.Frame(self.canvas, bg=TOKENS.surface, bd=0, highlightthickness=0)
        self.body_window = self.canvas.create_window(padding, padding, anchor="nw", window=self.body)
        self._surface_photos: list[ImageTk.PhotoImage] = []
        self._surface_parts: dict[str, int] | None = None
        self._surface_style_key: tuple[float, str, str, str] | None = None
        self._redraw_job: str | None = None
        self._last_surface_size: tuple[int, int, float] | None = None
        self._active = False
        self.canvas.bind("<Configure>", self._redraw)
        self.body.bind("<Configure>", self._update_scrollregion)
        self.bind_all("<Enter>", self._on_global_enter, add="+")
        self.bind_all("<Leave>", self._on_global_leave, add="+")
        self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.bind_all("<Button-4>", lambda event: self._scroll_lines(-1, event), add="+")
        self.bind_all("<Button-5>", lambda event: self._scroll_lines(1, event), add="+")
        self.bind_all("<FocusIn>", self._on_focus_in_global, add="+")

    def _contains(self, widget: tk.Misc | None) -> bool:
        while widget is not None:
            if widget is self:
                return True
            widget = widget.master  # type: ignore[assignment]
        return False

    def _on_global_enter(self, event: tk.Event[tk.Misc]) -> None:
        try:
            self._active = self._contains(self.winfo_containing(event.x_root, event.y_root))
        except tk.TclError:
            self._active = False

    def _on_global_leave(self, event: tk.Event[tk.Misc]) -> None:
        try:
            self._active = self._contains(self.winfo_containing(event.x_root, event.y_root))
        except tk.TclError:
            self._active = False

    def _scroll_lines(self, lines: int, event: tk.Event[tk.Misc]) -> str | None:
        if not self._active:
            return None
        self.canvas.yview_scroll(lines, "units")
        return "break"

    def _on_mousewheel(self, event: tk.Event[tk.Misc]) -> str | None:
        if not self._active:
            return None
        delta = int(getattr(event, "delta", 0))
        if delta:
            self.canvas.yview_scroll(-max(1, abs(delta) // 120) * (1 if delta > 0 else -1), "units")
        return "break"

    def _on_focus_in_global(self, event: tk.Event[tk.Misc]) -> None:
        widget = event.widget
        if self._contains(widget):
            self.after_idle(lambda: self.scroll_to_widget(widget))

    def scroll_to_widget(self, widget: tk.Misc) -> None:
        try:
            top = widget.winfo_rooty() - self.canvas.winfo_rooty()
            bottom = top + widget.winfo_height()
            visible = self.canvas.winfo_height()
            current_top = self.canvas.canvasy(0)
            current_bottom = current_top + visible
            if top < current_top:
                self.canvas.yview_scroll(int((top - current_top) / 20) - 1, "units")
            elif bottom > current_bottom:
                self.canvas.yview_scroll(int((bottom - current_bottom) / 20) + 1, "units")
        except tk.TclError:
            pass

    def _on_scroll(self, first: str, last: str) -> None:
        if float(last) - float(first) < 0.999:
            if not self.scrollbar.winfo_ismapped():
                self.scrollbar.pack(side="right", fill="y")
        elif self.scrollbar.winfo_ismapped():
            self.scrollbar.pack_forget()

    def _update_scrollregion(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        self.after_idle(self._set_scrollregion)

    def _set_scrollregion(self) -> None:
        try:
            canvas_width = max(1, self.canvas.winfo_width())
            canvas_height = max(1, self.canvas.winfo_height())
            width = max(1, canvas_width - self._padding * 2 - 4)
            self.canvas.itemconfigure(self.body_window, width=width)
            content_height = max(1, self.body.winfo_reqheight())
            self.canvas.configure(scrollregion=calculate_scrollregion(
                canvas_width, canvas_height, width, content_height, self._padding,
            ))
        except tk.TclError:
            pass

    def _redraw(self, event: tk.Event[tk.Misc]) -> None:
        self._pending_surface_size = (max(1, event.width), max(1, event.height))
        if self._redraw_job is None:
            self._redraw_job = self.after(16, self._redraw_now)

    def _redraw_now(self) -> None:
        self._redraw_job = None
        width, height = getattr(self, "_pending_surface_size", (1, 1))
        scale = _tk_dpi_scale(self.canvas)
        size_key = (width, height, round(scale, 2))
        if size_key == self._last_surface_size:
            return
        self._last_surface_size = size_key
        style_key = (round(scale, 2), TOKENS.surface, TOKENS.border_soft, TOKENS.shadow)
        if self._surface_parts is None or style_key != self._surface_style_key:
            self.canvas.delete("card-shape")
            self._surface_parts, self._surface_photos = _create_surface_parts(
                self.canvas, fill=TOKENS.surface, outline=TOKENS.border_soft,
                shadow=TOKENS.shadow, dpi_scale=scale,
            )
            self._surface_style_key = style_key
        _update_surface_parts(self.canvas, self._surface_parts, width, height,
                              self._surface_photos, scale)
        self._set_scrollregion()

    def destroy(self) -> None:
        if self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except tk.TclError:
                pass
            self._redraw_job = None
        super().destroy()


class PillButton(tk.Canvas):
    """Keyboard-accessible pill button with native Tk drawing and press feedback."""

    VARIANTS = {
        "primary": (TOKENS.accent, TOKENS.text_on_dark, TOKENS.accent),
        "outline": (TOKENS.surface, TOKENS.accent, TOKENS.accent),
        "danger": (TOKENS.surface, TOKENS.error, TOKENS.error),
        "dark": (TOKENS.house, TOKENS.text_on_dark, TOKENS.house),
        "inverted": (TOKENS.surface, TOKENS.accent, TOKENS.surface),
    }

    def __init__(self, master: tk.Misc, text: str, command: Callable[[], None], *,
                 variant: str = "outline", width: int | None = None, **kwargs: object) -> None:
        self._label = text
        self._command = command
        self._variant = variant
        self._button_state = "normal"
        self._pressed = False
        self._font = tkfont.Font(family=FONT_FAMILY, size=10, weight="bold")
        button_width = width or max(112, self._font.measure(text) + 32)
        self._requested_width = button_width
        self._requested_height = 40
        super().__init__(master, width=button_width, height=40, highlightthickness=0, bd=0,
                         bg=kwargs.pop("bg", TOKENS.surface), takefocus=True, **kwargs)
        self._cap_photos: list[ImageTk.PhotoImage] = []
        self._surface_parts: dict[str, int] | None = None
        self._surface_key: tuple[int, str, str, float] | None = None
        self._draw_job: str | None = None
        self._last_draw_size: tuple[int, int, str, bool] | None = None
        self._draw_now()
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<KeyPress-space>", self._on_key_press)
        self.bind("<KeyRelease-space>", self._on_key_release)
        self.bind("<Return>", lambda _event: self._invoke())
        self.bind("<FocusIn>", lambda _event: self._draw())
        self.bind("<FocusOut>", lambda _event: self._draw())

    def _colors(self) -> tuple[str, str, str]:
        fill, foreground, outline = self.VARIANTS.get(self._variant, self.VARIANTS["outline"])
        if self._button_state == "disabled":
            return TOKENS.ceramic, TOKENS.text_soft, TOKENS.ceramic
        return fill, foreground, outline

    def _current_size(self) -> tuple[int, int]:
        width = int(self.winfo_width())
        height = int(self.winfo_height())
        if width <= 1:
            width = self._requested_width
        if height <= 1:
            height = self._requested_height
        return max(40, width), max(32, height)

    def _on_configure(self, _event: tk.Event[tk.Misc]) -> None:
        self._schedule_draw()

    def _schedule_draw(self) -> None:
        if self._draw_job is None:
            self._draw_job = self.after(16, self._draw_now)

    def _draw(self) -> None:
        # State and focus changes need an immediate visual response; geometry
        # changes are coalesced by _on_configure.
        self._draw_now()

    def _draw_now(self) -> None:
        self._draw_job = None
        width, height = self._current_size()
        fill, foreground, outline = self._colors()
        draw_key = (width, height, self._button_state, self._pressed)
        self._last_draw_size = draw_key
        inset = 2 if self._pressed else 0
        scale = _tk_dpi_scale(self)
        surface_height = max(4, height - inset * 2)
        surface_key = (surface_height, fill, outline, round(scale, 2))
        if self._surface_parts is None or surface_key != self._surface_key:
            self.delete("all")
            left_cap, right_cap = render_pill_caps(surface_height, fill, outline=outline,
                                                   dpi_scale=scale)
            self._cap_photos = [ImageTk.PhotoImage(left_cap), ImageTk.PhotoImage(right_cap)]
            parts: dict[str, int] = {}
            parts["fill"] = self.create_rectangle(0, 0, 0, 0, fill=fill, outline="", tags="surface")
            parts["top"] = self.create_line(0, 0, 0, 0, fill=outline, width=1, tags="surface")
            parts["bottom"] = self.create_line(0, 0, 0, 0, fill=outline, width=1, tags="surface")
            parts["left"] = self.create_image(0, 0, anchor="nw", image=self._cap_photos[0], tags="surface")
            parts["right"] = self.create_image(0, 0, anchor="nw", image=self._cap_photos[1], tags="surface")
            parts["label"] = self.create_text(0, 0, text=self._label, fill=foreground,
                                               font=self._font, tags="label")
            parts["focus"] = self.create_line(0, 0, 0, 0, fill=TOKENS.primary, width=2, tags="focus")
            self._surface_parts = parts
            self._surface_key = surface_key

        parts = self._surface_parts
        x0, y0, x1, y1, cap_width = pill_cap_geometry(width, height, inset)
        self.coords(parts["fill"], x0 + cap_width, y0, max(x0 + cap_width, x1 - cap_width), y1)
        self.coords(parts["top"], x0 + cap_width, y0, max(x0 + cap_width, x1 - cap_width), y0)
        self.coords(parts["bottom"], x0 + cap_width, max(y0, y1 - 1),
                    max(x0 + cap_width, x1 - cap_width), max(y0, y1 - 1))
        self.coords(parts["left"], x0, y0)
        self.coords(parts["right"], x1 - cap_width, y0)
        self.itemconfigure(parts["fill"], fill=fill)
        self.itemconfigure(parts["top"], fill=outline)
        self.itemconfigure(parts["bottom"], fill=outline)
        self.coords(parts["label"], width / 2, height / 2)
        self.itemconfigure(parts["label"], fill=foreground)
        if self.focus_get() is self and self._button_state != "disabled":
            self.coords(parts["focus"], 16, height - 5, max(16, width - 16), height - 5)
            self.itemconfigure(parts["focus"], state="normal")
        else:
            self.itemconfigure(parts["focus"], state="hidden")
        self.tag_lower("surface")

    def _invoke(self) -> None:
        if self._button_state != "disabled":
            self._command()

    def _on_enter(self, _event: tk.Event[tk.Misc]) -> None:
        if self._button_state != "disabled":
            self.configure(cursor="hand2")

    def _on_leave(self, _event: tk.Event[tk.Misc]) -> None:
        self.configure(cursor="")
        self._pressed = False
        self._draw()

    def _on_press(self, _event: tk.Event[tk.Misc]) -> None:
        if self._button_state == "disabled":
            return
        self.focus_set()
        self._pressed = True
        self._draw()

    def _on_release(self, _event: tk.Event[tk.Misc]) -> None:
        was_pressed = self._pressed
        self._pressed = False
        self._draw()
        if was_pressed:
            self._invoke()

    def _on_key_press(self, _event: tk.Event[tk.Misc]) -> None:
        if self._button_state != "disabled":
            self._pressed = True
            self._draw()

    def _on_key_release(self, _event: tk.Event[tk.Misc]) -> None:
        if self._button_state != "disabled":
            self._pressed = False
            self._draw()
            self._invoke()

    def set_state(self, state: str) -> None:
        self._button_state = "disabled" if state == "disabled" else "normal"
        self._draw()

    def set_variant(self, variant: str) -> None:
        if variant not in self.VARIANTS:
            return
        self._variant = variant
        self._surface_parts = None
        self._draw_now()

    def configure(self, cnf: dict[str, object] | None = None, **kwargs: object) -> object:
        state = kwargs.pop("state", None)
        result = super().configure(cnf or {}, **kwargs)
        if state is not None:
            self.set_state(str(state))
        return result

    def destroy(self) -> None:
        if self._draw_job is not None:
            try:
                self.after_cancel(self._draw_job)
            except tk.TclError:
                pass
            self._draw_job = None
        super().destroy()


class FormField(tk.Frame):
    """Labeled entry with focus, valid and invalid semantic states."""

    def __init__(self, master: tk.Misc, label: str, variable: tk.StringVar, *,
                 show: str = "", helper: str = "", **kwargs: object) -> None:
        super().__init__(master, bg=TOKENS.surface, bd=0, highlightthickness=0, **kwargs)
        self.variable = variable
        self._state = "neutral"
        self.label = tk.Label(self, text=label, bg=TOKENS.surface, fg=TOKENS.text,
                              font=(FONT_FAMILY, 10, "bold"), anchor="w")
        self.label.pack(fill="x", pady=(0, TOKENS.space_1))
        self.shell = tk.Frame(self, bg=TOKENS.border, bd=0, highlightthickness=0)
        self.shell.pack(fill="x")
        self.entry = tk.Entry(self.shell, textvariable=variable, show=show, relief="flat", bd=0,
                              highlightthickness=0, bg=TOKENS.surface_muted, fg=TOKENS.text,
                              insertbackground=TOKENS.primary, font=(FONT_FAMILY, 11))
        self.entry.pack(fill="x", padx=1, pady=1, ipady=7)
        self.message = tk.Label(self, text=helper, bg=TOKENS.surface, fg=TOKENS.text_soft,
                                font=(FONT_FAMILY, 9), anchor="w", justify="left")
        if helper:
            self.message.pack(fill="x", pady=(TOKENS.space_1, 0))
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)
        self.entry.bind("<KeyRelease>", self._on_edit)

    def _on_focus_in(self, _event: tk.Event[tk.Misc]) -> None:
        if self._state == "neutral":
            self.shell.configure(bg=TOKENS.focus)

    def _on_focus_out(self, _event: tk.Event[tk.Misc]) -> None:
        self._render_state()

    def _on_edit(self, _event: tk.Event[tk.Misc]) -> None:
        if self._state != "neutral":
            self.set_state("neutral")

    def _render_state(self) -> None:
        if self._state == "valid":
            self.shell.configure(bg=TOKENS.accent)
            self.entry.configure(bg=TOKENS.green_light)
        elif self._state == "invalid":
            self.shell.configure(bg=TOKENS.error)
            self.entry.configure(bg=TOKENS.error_tint)
        else:
            self.shell.configure(bg=TOKENS.border)
            self.entry.configure(bg=TOKENS.surface_muted)

    def set_state(self, state: str, message: str = "") -> None:
        self._state = state if state in {"valid", "invalid", "neutral"} else "neutral"
        self.message.configure(text=message, fg=TOKENS.error if self._state == "invalid" else TOKENS.text_soft)
        if message and not self.message.winfo_ismapped():
            self.message.pack(fill="x", pady=(TOKENS.space_1, 0))
        elif not message and self.message.winfo_ismapped():
            self.message.pack_forget()
        self._render_state()

    def get(self) -> str:
        return self.variable.get()


class StatusBadge(tk.Canvas):
    """Compact pill status indicator."""

    def __init__(self, master: tk.Misc, *, state: str = "ready", **kwargs: object) -> None:
        super().__init__(master, width=92, height=30, highlightthickness=0, bd=0,
                         bg=kwargs.pop("bg", TOKENS.house), **kwargs)
        self._state = state
        self._draw()

    def set(self, state: str) -> None:
        self._state = state if state in STATUS_STYLES else "ready"
        self._draw()

    def _draw(self) -> None:
        label, fill = STATUS_STYLES.get(self._state, STATUS_STYLES["ready"])
        foreground = TOKENS.house if self._state in {"ready", "success", "stopping", "updating"} else TOKENS.text_on_dark
        self.delete("all")
        self.create_oval(4, 10, 10, 16, fill=fill, outline="")
        self.create_text(17, 13, text=label, anchor="w", fill=foreground, font=(FONT_FAMILY, 10, "bold"))
