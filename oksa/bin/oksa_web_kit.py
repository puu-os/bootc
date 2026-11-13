# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from collections.abc import Awaitable, Callable
from enum import Enum
import html
from pathlib import Path

from nicegui import ui
from nicegui.events import ClickEventArguments, Handler


class Tone(Enum):
    """Severity of a status, ordered from least to most urgent."""

    OK = (0, "check_circle")
    NEUTRAL = (1, "radio_button_unchecked")
    BUSY = (2, "autorenew")
    WARNING = (3, "warning")
    ERROR = (4, "report")

    def __init__(self, severity: int, icon: str) -> None:
        self.severity = severity
        self.icon = icon


class Emphasis(Enum):
    """How strongly a button competes for attention."""

    PRIMARY = "unelevated color=primary text-color=on-primary"
    DEFAULT = "outline color=primary"
    DANGER = "outline color=negative"


class Meter(ui.element):
    """A reading whose optional bar changes colour as the resource fills up."""

    WARNING_FROM = 0.75
    ERROR_FROM = 0.9

    def __init__(self, label: str) -> None:
        super().__init__("div")
        self.classes("meter")
        with self:
            self._value = ui.label().classes("meter-value")
            self._bar = ui.linear_progress(0.0, show_value=False, size="6px").props("rounded")
            self._bar.props["aria-label"] = label
            self._note = ui.label().classes("meter-note")

    def show(self, value: str, note: str, fraction: float | None = None) -> None:
        self._value.set_text(value)
        self._note.set_text(note)
        self._bar.set_visibility(fraction is not None)
        if fraction is None:
            return
        fraction = min(max(fraction, 0.0), 1.0)
        if fraction >= self.ERROR_FROM:
            color = "negative"
        elif fraction >= self.WARNING_FROM:
            color = "warning"
        else:
            color = "primary"
        self._bar.set_value(fraction)
        self._bar.props(f"color={color}")


class Kit:
    """The visual vocabulary every OKSA page is composed from."""

    STYLESHEET = Path(__file__).with_name("oksa_web.css")

    @staticmethod
    def _icon(name: str) -> str:
        return f'<i class="q-icon notranslate material-icons" aria-hidden="true">{html.escape(name)}</i>'

    @classmethod
    def status(cls, text: object, tone: Tone = Tone.OK) -> str:
        """Markup stating a condition with an icon, a colour and words."""
        return (
            f'<span class="status {tone.name.lower()}">{cls._icon(tone.icon)}'
            f"<span>{html.escape(str(text))}</span></span>"
        )

    @classmethod
    def empty(cls, text: object, *, icon: str = "inbox") -> str:
        """Markup standing in for content that is absent, loading or unreadable."""
        return f'<div class="empty">{cls._icon(icon)}<p>{html.escape(str(text))}</p></div>'

    @classmethod
    def card(cls, title: str, *, icon: str) -> ui.card:
        """A titled surface; enter it to add content below the title."""
        card = ui.card().props("flat bordered").classes("card")
        with card:
            ui.html(f'<h3 class="card-title">{cls._icon(icon)}{html.escape(title)}</h3>', sanitize=False)
        return card

    @staticmethod
    def meter(label: str) -> Meter:
        return Meter(label)

    @staticmethod
    def code_example(copy: Callable[[str], Awaitable[bool]]) -> "CodeExample":
        return CodeExample(copy)

    @staticmethod
    def button(
        text: str,
        on_click: Handler[ClickEventArguments] | None = None,
        *,
        emphasis: Emphasis = Emphasis.DEFAULT,
        icon: str | None = None,
        href: str | None = None,
    ) -> ui.button:
        """An action, or a navigation link when ``href`` is given."""
        button = ui.button(text, on_click=on_click, color=None, icon=icon).props(f"no-caps {emphasis.value}")
        if href is not None:
            button.props["href"] = href
        return button


class CodeExample(ui.element):
    """Alternative snippets for one task, one tab each, copied whole."""

    def __init__(self, copy: Callable[[str], Awaitable[bool]]) -> None:
        super().__init__("div")
        self.classes("code-example")
        self._copy = copy
        self._snippets: dict[str, str] = {}
        with self:
            self._tabs = ui.tabs().props("dense no-caps align=left")
            self._panels = ui.tab_panels(self._tabs)
            with ui.element("div").classes("code-example-actions"):
                Kit.button("Copy example", self._copy_selected, icon="content_copy")
                self._note = ui.label().classes("small").props('aria-live="polite"')
        self.set_visibility(False)

    def show(self, snippets: dict[str, str]) -> None:
        """Replace the snippets; an empty mapping hides the element and drops its content."""
        if snippets == self._snippets:
            return
        self._snippets = dict(snippets)
        self._tabs.clear()
        self._panels.clear()
        with self._tabs:
            for name in snippets:
                ui.tab(name)
        with self._panels:
            for name, code in snippets.items():
                with ui.tab_panel(name):
                    ui.html(f"<pre><code>{html.escape(code)}</code></pre>", sanitize=False)
        self._tabs.set_value(next(iter(snippets), None))
        self._note.set_text("")
        self.set_visibility(bool(snippets))

    async def _copy_selected(self) -> None:
        code = self._snippets.get(str(self._tabs.value))
        if code is None:
            return
        if await self._copy(code):
            self._note.set_text("Copied.")
        else:
            self._note.set_text("The browser refused clipboard access. Select the text and copy it manually.")
