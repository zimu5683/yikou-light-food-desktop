"""Reusable Tkinter design primitives for the Yikou Light Food desktop app.

The values in this module are intentionally kept independent of the business
workflow so the visual system can be tested without creating a Tk root window.
"""
from __future__ import annotations

from dataclasses import dataclass
import tkinter as tk
from tkinter import font as tkfont
from typing import Callable


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

    def _redraw(self, event: tk.Event[tk.Misc]) -> None:
        width, height = max(1, event.width), max(1, event.height)
        self.canvas.delete("card-shape")
        _rounded_rectangle(self.canvas, 2, 2, width - 2, height - 1, TOKENS.radius_card,
                           fill=TOKENS.shadow_soft, outline="")
        _rounded_rectangle(self.canvas, 1, 1, width - 3, height - 3, TOKENS.radius_card,
                           fill=TOKENS.shadow, outline="")
        _rounded_rectangle(self.canvas, 0, 0, width - 4, height - 5, TOKENS.radius_card,
                           fill=TOKENS.surface, outline=TOKENS.border_soft, width=1)
        for item in self.canvas.find_all():
            if item != self.body_window:
                self.canvas.addtag_withtag("card-shape", item)
        self.canvas.tag_lower("card-shape")
        self.canvas.itemconfigure(self.body_window, width=max(1, width - self._padding * 2 - 4),
                                  height=max(1, height - self._padding * 2 - 5))
        self.canvas.coords(self.body_window, self._padding, self._padding)


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
        _rounded_rectangle(self, inset, inset, width - inset - 1, height - inset - 1,
                           height / 2, fill=fill, outline=outline, width=1)
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
