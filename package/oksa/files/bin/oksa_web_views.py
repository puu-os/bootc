# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from nicegui import events, ui


@dataclass(frozen=True)
class ModelDetails:
    id: str
    name: str
    repo_id: str
    storage: str
    quantization: str
    compatibility: str
    state: str
    calibration_state: str
    calibration_context: str
    calibration_message: str
    profile: str


@dataclass(frozen=True)
class ModelsPresentation:
    rows: list[dict]
    detail: ModelDetails | None
    error: str
    empty_message: str
    service_state: str
    busy: bool
    can_start: bool
    can_restart: bool
    can_remove: bool
    can_calibrate: bool
    can_stop: bool


class ModelsView:
    def __init__(
        self,
        *,
        on_select: Callable[[], Awaitable[None]],
        on_action: Callable[[str], Awaitable[None]],
        on_find: Callable[[], Awaitable[None]],
    ) -> None:
        self.model_id: str | None = None
        self._on_select = on_select
        with ui.column().classes("w-full min-w-0 gap-3"):
            with ui.row().classes("w-full items-center justify-between gap-3"):
                with ui.row().classes("items-center gap-3") as self._list_heading:
                    ui.label("Installed models").classes("text-xl font-semibold")
                    ui.button("Find models", icon="search", on_click=on_find)
                with ui.row().classes("items-center gap-3") as self._navigation:
                    ui.button("Back to Models", icon="arrow_back", on_click=self._back_to_models).props("flat")
                    self._breadcrumb = ui.label("").classes("text-sm text-gray-600 break-words")
                self._navigation.set_visibility(False)
                self._spinner = ui.spinner()
                self._spinner.set_visibility(False)
            self._error = ui.label("").classes("w-full text-negative whitespace-pre-line break-words")
            self._error.set_visibility(False)
            with ui.column().classes("w-full min-w-0 gap-3") as self.list_panel:
                with ui.row().classes("w-full items-center justify-between gap-3 border rounded p-3 bg-gray-50"):
                    with ui.column().classes("min-w-0 gap-1"):
                        ui.label("All model serving").classes("font-medium")
                        self._service_state = ui.label("").classes("text-sm text-gray-600 break-words")
                    self.stop_button = ui.button(
                        "Stop all serving", on_click=lambda: on_action("stop"),
                    ).props("outline")
                self.table = ui.table(
                    columns=[
                        {"name": "name", "field": "name", "label": "Model", "sortable": True, "align": "left"},
                        {"name": "state", "field": "state", "label": "State", "sortable": True, "align": "left"},
                        {"name": "storage_mib", "field": "storage_mib", "label": "On disk", "sortable": True,
                         ":format": "value => value === null ? 'Unavailable' : `${value} MiB`"},
                    ],
                    rows=[],
                    row_key="id",
                    pagination=10,
                ).props("color=primary dense flat bordered").classes("w-full min-w-0")
                self.table.on("rowClick", self._open_model, args=[[], ["id"]])
            with ui.column().classes("w-full min-w-0 gap-4") as self.detail_panel:
                self._detail_name = ui.label("").classes("text-xl font-semibold break-all")
                with ui.row().classes("w-full gap-4 break-words"):
                    self._detail_state = ui.label("")
                    self._detail_storage = ui.label("")
                with ui.row().classes("w-full gap-2"):
                    self.start_button = ui.button("Start", on_click=lambda: on_action("start"))
                    self.restart_button = ui.button("Restart", on_click=lambda: on_action("restart")).props("outline")
                    self.remove_button = ui.button(
                        "Delete", color="negative", on_click=lambda: on_action("remove"),
                    ).props("outline")
                with ui.column().classes("w-full min-w-0 gap-1 break-words"):
                    with ui.row().classes("w-full items-center gap-3"):
                        ui.label("Calibration").classes("text-lg font-medium")
                        self.calibration_button = ui.button(
                            "Calibrate", on_click=lambda: on_action("calibrate"),
                        ).props("outline")
                    self._calibration_state = ui.label("")
                    self._calibration_context = ui.label("").classes("text-sm text-gray-600")
                    self._calibration_message = ui.label("").classes("whitespace-pre-line")
                    self._profile = ui.label("").classes("whitespace-pre-line")
                with ui.expansion("Technical details").classes("w-full"):
                    with ui.column().classes("w-full min-w-0 gap-1 text-sm break-all"):
                        self._detail_id = ui.label("")
                        self._detail_repo = ui.label("")
                        self._detail_quantization = ui.label("")
                        self._detail_compatibility = ui.label("")
            self.detail_panel.set_visibility(False)
        self.controls: list[ui.button] = [
            self.start_button,
            self.restart_button,
            self.remove_button,
            self.calibration_button,
            self.stop_button,
        ]
        for button in self.controls:
            button.disable()

    async def _open_model(self, event: events.GenericEventArguments) -> None:
        model_id = event.args[1]["id"]
        if not isinstance(model_id, str) or not any(row["id"] == model_id for row in self.table.rows):
            return
        self.model_id = model_id
        await self._on_select()

    async def _back_to_models(self) -> None:
        self.model_id = None
        await self._on_select()

    def render(self, presentation: ModelsPresentation) -> None:
        if self.table.rows != presentation.rows:
            self.table.rows = [row.copy() for row in presentation.rows]
            self.table.update()
        if self.model_id is not None and not any(row["id"] == self.model_id for row in self.table.rows):
            self.model_id = None

        self._error.set_text(presentation.error)
        self._error.set_visibility(bool(presentation.error))
        self.table.props["no-data-label"] = presentation.empty_message or "No installed models"
        self._spinner.set_visibility(presentation.busy)
        self._service_state.set_text(presentation.service_state)
        detail = presentation.detail
        if detail is None or detail.id != self.model_id:
            detail = None
        self.start_button.set_enabled(detail is not None and presentation.can_start)
        self.restart_button.set_enabled(detail is not None and presentation.can_restart)
        self.remove_button.set_enabled(detail is not None and presentation.can_remove)
        self.calibration_button.set_enabled(detail is not None and presentation.can_calibrate)
        self.stop_button.set_enabled(self.model_id is None and presentation.can_stop)
        self._list_heading.set_visibility(self.model_id is None)
        self.list_panel.set_visibility(self.model_id is None)
        self._navigation.set_visibility(self.model_id is not None)
        self.detail_panel.set_visibility(detail is not None)
        if detail is not None:
            self._breadcrumb.set_text(f"Models / {detail.name}")
            self._detail_name.set_text(detail.name)
            self._detail_id.set_text(f"Internal ID: {detail.id}")
            self._detail_repo.set_text(f"Repository: {detail.repo_id}")
            self._detail_storage.set_text(f"On disk: {detail.storage}")
            self._detail_quantization.set_text(f"Quantization: {detail.quantization}")
            self._detail_compatibility.set_text(f"Compatibility: {detail.compatibility}")
            self._detail_state.set_text(f"State: {detail.state}")
            self._calibration_state.set_text(f"Status: {detail.calibration_state}")
            self._calibration_context.set_text(detail.calibration_context)
            self._calibration_message.set_text(detail.calibration_message)
            self._profile.set_text(detail.profile)
            self._profile.set_visibility(bool(detail.profile))
