"""Reusable Tkinter design primitives for the Yikou Light Food desktop app.

The values in this module are intentionally kept independent of the business
workflow so the visual system can be tested without creating a Tk root window.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
import hashlib
import os
import tkinter as tk
from tkinter import font as tkfont
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


_IMAGE_CACHE: dict[tuple[object, ...], Image.Image] = {}


def _color_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(part * 2 for part in value)
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def render_rounded_image(width: int, height: int, radius: int, fill: str,
                         *, outline: str = "", shadow: str = "",
                         scale: int = 4) -> Image.Image:
    """Render a cached, supersampled rounded surface with antialiased edges."""
    width, height = max(1, int(width)), max(1, int(height))
    scale = max(1, int(scale))
    key = (width, height, int(radius), fill, outline, shadow, scale)
    cached = _IMAGE_CACHE.get(key)
    if cached is not None:
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
    return result


def image_cache_key(width: int, height: int, radius: int, fill: str, outline: str = "") -> str:
    """Return a stable debug key for a rendered component image."""
    return hashlib.sha1(f"{width}:{height}:{radius}:{fill}:{outline}".encode()).hexdigest()

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


def _rounded_rectangle(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                       radius: float, *, fill: str, outline: str = "", width: int = 0) -> tuple[int, ...]:
    """Draw a rounded rectangle using Canvas primitives available on all Tk builds."""

    radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    ids = (
        canvas.create_rectangle(x1 + radius, y1, x2 - radius, y2, fill=fill, outline=""),
        canvas.create_rectangle(x1, y1 + radius, x2, y2 - radius, fill=fill, outline=""),
        canvas.create_arc(x1, y1, x1 + 2 * radius, y1 + 2 * radius, start=90, extent=90, fill=fill, outline=""),
        canvas.create_arc(x2 - 2 * radius, y1, x2, y1 + 2 * radius, start=0, extent=90, fill=fill, outline=""),
        canvas.create_arc(x1, y2 - 2 * radius, x1 + 2 * radius, y2, start=180, extent=90, fill=fill, outline=""),
        canvas.create_arc(x2 - 2 * radius, y2 - 2 * radius, x2, y2, start=270, extent=90, fill=fill, outline=""),
    )
    if outline:
        ids += (canvas.create_arc(x1, y1, x1 + 2 * radius, y1 + 2 * radius, start=90, extent=90, outline=outline, width=width),)
    return ids


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
        self.canvas.bind("<Configure>", self._redraw)
        self._photo: ImageTk.PhotoImage | None = None

    def _redraw(self, event: tk.Event[tk.Misc]) -> None:
        width, height = max(1, event.width), max(1, event.height)
        self.canvas.delete("card-shape")
        image = render_rounded_image(width, height, TOKENS.radius_card, TOKENS.surface,
                                     outline=TOKENS.border_soft, shadow=TOKENS.shadow)
        self._photo = ImageTk.PhotoImage(image)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo, tags="card-shape")
        self.canvas.tag_lower("card-shape")
        self.canvas.itemconfigure(self.body_window, width=max(1, width - self._padding * 2 - 4),
                                  height=max(1, height - self._padding * 2 - 5))
        self.canvas.coords(self.body_window, self._padding, self._padding)


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
        self._photo: ImageTk.PhotoImage | None = None
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
            width = max(1, self.canvas.winfo_width() - self._padding * 2 - 4)
            self.canvas.itemconfigure(self.body_window, width=width)
            self.canvas.configure(scrollregion=self.canvas.bbox(self.body_window) or (0, 0, width, 1))
        except tk.TclError:
            pass

    def _redraw(self, event: tk.Event[tk.Misc]) -> None:
        width, height = max(1, event.width), max(1, event.height)
        self.canvas.delete("card-shape")
        image = render_rounded_image(width, height, TOKENS.radius_card, TOKENS.surface,
                                     outline=TOKENS.border_soft, shadow=TOKENS.shadow)
        self._photo = ImageTk.PhotoImage(image)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo, tags="card-shape")
        self.canvas.tag_lower("card-shape")
        self._set_scrollregion()


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
        super().__init__(master, width=button_width, height=40, highlightthickness=0, bd=0,
                         bg=kwargs.pop("bg", TOKENS.surface), takefocus=True, **kwargs)
        self._photo: ImageTk.PhotoImage | None = None
        self._draw()
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

    def _draw(self) -> None:
        width = max(40, int(self.winfo_width() or int(self.cget("width"))))
        height = max(32, int(self.winfo_height() or int(self.cget("height"))))
        self.delete("all")
        fill, foreground, outline = self._colors()
        inset = 2 if self._pressed else 0
        image = render_rounded_image(max(1, width - inset * 2), max(1, height - inset * 2),
                                     height // 2, fill, outline=outline)
        self._photo = ImageTk.PhotoImage(image)
        self.create_image(inset, inset, anchor="nw", image=self._photo, tags="surface")
        self.create_text(width / 2, height / 2, text=self._label, fill=foreground,
                         font=self._font, tags="label")
        if self.focus_get() is self and self._button_state != "disabled":
            self.create_line(16, height - 5, width - 16, height - 5, fill=TOKENS.primary, width=2)

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

    def configure(self, cnf: dict[str, object] | None = None, **kwargs: object) -> object:
        state = kwargs.pop("state", None)
        result = super().configure(cnf or {}, **kwargs)
        if state is not None:
            self.set_state(str(state))
        return result


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
        self.create_oval(4, 10, 10, 16, fill=foreground, outline="")
        self.create_text(17, 13, text=label, anchor="w", fill=foreground, font=(FONT_FAMILY, 10, "bold"))
