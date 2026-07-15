"""rigid_flex_app.py — Rigid-Flex stackup window.

The zone sequence always starts with a rigid zone and strictly alternates
(rigid, flex, rigid, flex, ...). Because of that, "add zone" and "remove
zone" never need the user to pick a kind: the next zone to add after a
flex zone can only be rigid, and removing always removes the most recently
added zone. Valid sequences this produces match what real rigid-flex
boards look like: Rigid+Flex, Rigid+Flex+Rigid, Rigid+Flex+Rigid+Flex, etc.

Each zone tab reuses the existing StackupEditorWindow wholesale (its central
widget is lifted out and placed into the tab) so the rigid-flex window is
literally "the current view, per zone" rather than a rebuild. Rigid zones
use the standard rigid stackup model, while flex zones now start from a
fixed coverlay + flex-core construction.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from stackup_editor.catalog import MaterialCatalog
from stackup_editor.models import (
    CopperLayer,
    DielectricLayer,
    FlexCoreLayer,
    Stackup,
    build_default_flex_stackup,
    build_flex_stackup_from_templates,
    build_default_rigid_flex_rigid_stackup,
    preferred_default_flex_core_entry,
    rebuild_rigid_stackup_from_slot_activity,
    rigid_shared_region_bounds,
    rigid_shared_region_bounds_for_capacity,
    rigid_slot_copper_indices,
)
from stackup_editor.qt_app import StackupEditorWindow
from stackup_editor.units import (
    format_compact_thickness,
    format_roughness_um,
    format_total_thickness,
    thickness_unit_for_layer,
)

logger = logging.getLogger(__name__)

MIN_ZONES = 2


def zone_kind_for_position(position: int) -> str:
    """Position is 0-indexed. Zones strictly alternate, starting rigid."""
    return "rigid" if position % 2 == 0 else "flex"


class RigidFlexCombinedPreview(QWidget):
    selectionRequested = Signal(int, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(420, 560)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.zone_editors: list[StackupEditorWindow] = []
        self.rigid_zone_indices: list[int] = []
        self.flex_zone_indices: list[int] = []
        self.rigid_editor: StackupEditorWindow | None = None
        self.flex_editor: StackupEditorWindow | None = None
        self.right_rigid_editor: StackupEditorWindow | None = None
        self.left_rigid_index: int | None = None
        self.flex_index: int | None = None
        self.right_rigid_index: int | None = None
        self.active_zone_index = 0
        self._hit_regions: list[tuple[QRectF, int, tuple[str, int | str]]] = []
        self.palette_map = {
            "bg": "#0b1724",
            "grid": "#203344",
            "text": "#edf4fa",
            "muted": "#8fa9bf",
            "accent": "#7cd0dd",
            "danger": "#ff8b70",
            "soldermask": "#0cb34b",
            "soldermask_outline": "#7ee29f",
            "copper": "#ef3f34",
            "copper_outline": "#ff9b8a",
            "rigid_copper": "#caa437",
            "rigid_copper_outline": "#f7dd84",
            "core": "#f8f28f",
            "core_outline": "#fff8bf",
            "prepreg": "#d4cebb",
            "prepreg_outline": "#ece5d1",
            "flex_core": "#8b53d1",
            "flex_core_outline": "#d4b4ff",
            "coverlay": "#2e86ff",
            "coverlay_outline": "#9fc9ff",
            "adhesive": "#8f949c",
            "adhesive_outline": "#c5cad2",
            "connector": "#111418",
        }

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(560, 760)

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(420, 560)

    def set_sources(
        self,
        zone_editors: list[StackupEditorWindow],
        *,
        active_zone_index: int,
    ) -> None:
        self.zone_editors = list(zone_editors)
        self.rigid_zone_indices = [i for i, editor in enumerate(self.zone_editors) if not editor.is_flex_zone]
        self.flex_zone_indices = [i for i, editor in enumerate(self.zone_editors) if editor.is_flex_zone]
        self.left_rigid_index = self.rigid_zone_indices[0] if self.rigid_zone_indices else None
        self.flex_index = self.flex_zone_indices[0] if self.flex_zone_indices else None
        if self.flex_index is not None:
            self.right_rigid_index = next(
                (
                    i
                    for i, editor in enumerate(self.zone_editors[self.flex_index + 1 :], start=self.flex_index + 1)
                    if not editor.is_flex_zone
                ),
                None,
            )
        else:
            self.right_rigid_index = None
        self.rigid_editor = self.zone_editors[self.left_rigid_index] if self.left_rigid_index is not None else None
        self.flex_editor = self.zone_editors[self.flex_index] if self.flex_index is not None else None
        self.right_rigid_editor = (
            self.zone_editors[self.right_rigid_index] if self.right_rigid_index is not None else None
        )
        self.active_zone_index = active_zone_index
        self.update()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        point = QPointF(event.position())
        for rect, zone_index, meta in reversed(self._hit_regions):
            if rect.contains(point):
                self.selectionRequested.emit(zone_index, meta)
                break
        super().mousePressEvent(event)

    def _display_unit(self) -> str:
        if 0 <= self.active_zone_index < len(self.zone_editors):
            return self.zone_editors[self.active_zone_index].display_unit
        if self.rigid_editor is not None:
            return self.rigid_editor.display_unit
        return "mm"

    def _shared_bounds(self, rigid_stackup: Stackup, flex_stackup: Stackup) -> tuple[int, int] | None:
        try:
            return rigid_shared_region_bounds(rigid_stackup, flex_stackup)
        except ValueError:
            return None

    def _rigid_index_for_flex_layer(
        self,
        rigid_stackup: Stackup,
        flex_stackup: Stackup,
        layer_index: int,
    ) -> int:
        slot_id = flex_stackup.flex_slot_for_layer_index(layer_index)
        top_index, bottom_index = rigid_slot_copper_indices(
            rigid_stackup,
            flex_stackup.flex_slot_capacity_or_count(),
            slot_id,
        )
        layer_position = layer_index % 3
        if layer_position == 0:
            return top_index
        if layer_position == 2:
            return bottom_index
        return next(
            (
                index
                for index in range(top_index + 1, bottom_index)
                if isinstance(rigid_stackup.layers[index], FlexCoreLayer)
            ),
            top_index + 1,
        )

    def _thickness_text(self, thickness_mm: float, *, is_copper: bool) -> str:
        if is_copper:
            return format_compact_thickness(thickness_mm, "oz")
        unit = thickness_unit_for_layer(self._display_unit(), is_copper=False)
        return format_compact_thickness(thickness_mm, unit)

    def _block_text(self, primary: str, thickness_mm: float, *, is_copper: bool, pixel_height: float) -> str:
        thickness = self._thickness_text(thickness_mm, is_copper=is_copper)
        if pixel_height < 16:
            return thickness
        if not primary:
            return thickness
        return f"{primary} | {thickness}"

    def _draw_single_line(
        self,
        painter: QPainter,
        rect: QRectF,
        text: str,
        *,
        color: QColor,
        font: QFont,
        min_point_size: float = 5.0,
        alignment: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignCenter,
    ) -> None:
        if not text or rect.width() <= 0 or rect.height() <= 0:
            return
        fitted_font = QFont(font)
        available_width = max(14, int(rect.width()) - 4)
        available_height = max(10, int(rect.height()) - 2)
        while fitted_font.pointSizeF() > min_point_size:
            metrics = QFontMetrics(fitted_font)
            if metrics.horizontalAdvance(text) <= available_width and metrics.height() <= available_height:
                break
            fitted_font.setPointSizeF(fitted_font.pointSizeF() - 0.5)
        metrics = QFontMetrics(fitted_font)
        draw_text = text
        if metrics.horizontalAdvance(draw_text) > available_width:
            draw_text = metrics.elidedText(draw_text, Qt.TextElideMode.ElideRight, available_width)
        painter.setPen(color)
        painter.setFont(fitted_font)
        painter.drawText(rect, alignment | Qt.TextFlag.TextSingleLine, draw_text)

    def _add_hit_region(self, rect: QRectF, zone_index: int, meta: tuple[str, int | str]) -> None:
        if rect.width() > 0 and rect.height() > 0:
            self._hit_regions.append((QRectF(rect), zone_index, meta))

    def _compute_span_layout(
        self,
        canvas_width: float,
    ) -> tuple[float, float, float, dict[int, float]]:
        left_label_width = 48.0
        left_margin = 18.0 + left_label_width
        right_margin = 18.0
        usable_width = max(280.0, canvas_width - left_margin - right_margin)
        rigid_weight = 1.4
        flex_weights = {zone_index: 1.0 for zone_index in self.flex_zone_indices}
        total_weight = (len(self.rigid_zone_indices) * rigid_weight) + sum(flex_weights.values())
        unit_width = usable_width / max(1.0, total_weight)
        rigid_width = max(42.0, unit_width * rigid_weight)
        flex_widths = {zone_index: max(26.0, unit_width * weight) for zone_index, weight in flex_weights.items()}
        return left_label_width, left_margin, rigid_width, flex_widths

    def _structural_layer_weight(self, layer: object) -> float:
        if layer == "soldermask":
            return 0.42
        if isinstance(layer, CopperLayer):
            return 0.72
        if isinstance(layer, (DielectricLayer, FlexCoreLayer)):
            return 1.0
        return 1.0

    def _scaled_heights_from_structure(
        self,
        layers: list[object],
        total_height_px: float,
    ) -> list[float]:
        if not layers:
            return []
        weights = [self._structural_layer_weight(layer) for layer in layers]
        total_weight = sum(weights)
        if total_weight <= 0.0:
            equal = total_height_px / max(1, len(layers))
            return [equal for _ in layers]
        heights = [(weight / total_weight) * total_height_px for weight in weights]
        if heights:
            heights[-1] += total_height_px - sum(heights)
        return heights

    def _flex_gap_component_heights(
        self,
        available_height: float,
        *,
        coverlay_pi_px: float,
        adhesive_px: float,
        minimum_gap_px: float,
    ) -> list[float]:
        """Lay coverlay against its own sandwich and leave unused span as air gap."""
        available_height = max(0.0, available_height)
        component_height = (adhesive_px + coverlay_pi_px) * 2
        if component_height <= available_height:
            return [
                adhesive_px,
                coverlay_pi_px,
                available_height - component_height,
                coverlay_pi_px,
                adhesive_px,
            ]

        weights = [adhesive_px, coverlay_pi_px, minimum_gap_px, coverlay_pi_px, adhesive_px]
        total_weight = sum(weights)
        if total_weight <= 0.0:
            return [0.0] * len(weights)
        scale = available_height / total_weight
        heights = [weight * scale for weight in weights]
        heights[-1] += available_height - sum(heights)
        return heights

    def _zone_selected_meta(self, zone_index: int) -> tuple[str, int | str] | None:
        if zone_index < 0 or zone_index >= len(self.zone_editors):
            return None
        return self.zone_editors[zone_index]._current_row_meta()

    def _zone_selected_layer_index(self, zone_index: int) -> int | None:
        meta = self._zone_selected_meta(zone_index)
        if isinstance(meta, tuple) and len(meta) == 2 and meta[0] == "layer":
            return int(meta[1])
        return None

    def _dielectric_rectangle_text(self, layer: object) -> str:
        if isinstance(layer, FlexCoreLayer):
            return "Flex Core"
        if isinstance(layer, DielectricLayer):
            return "Rigid Core" if layer.dielectric_type == "core" else "Rigid PP"
        return ""

    def _layer_colors(self, layer: object, *, role: str | None = None) -> tuple[QColor, QColor, QColor]:
        if role == "coverlay":
            return (
                QColor(self.palette_map["coverlay"]),
                QColor(self.palette_map["coverlay_outline"]),
                QColor("#eef6ff"),
            )
        if role == "adhesive":
            return (
                QColor(self.palette_map["adhesive"]),
                QColor(self.palette_map["adhesive_outline"]),
                QColor("#111418"),
            )
        if layer == "soldermask":
            return (
                QColor(self.palette_map["soldermask"]),
                QColor(self.palette_map["soldermask_outline"]),
                QColor("#f2fff4"),
            )
        if role == "rigid_copper":
            return (
                QColor(self.palette_map["rigid_copper"]),
                QColor(self.palette_map["rigid_copper_outline"]),
                QColor("#221805"),
            )
        if isinstance(layer, CopperLayer):
            return (
                QColor(self.palette_map["copper"]),
                QColor(self.palette_map["copper_outline"]),
                QColor("#fff6f4"),
            )
        if isinstance(layer, FlexCoreLayer):
            return (
                QColor(self.palette_map["flex_core"]),
                QColor(self.palette_map["flex_core_outline"]),
                QColor("#f7efff"),
            )
        if isinstance(layer, DielectricLayer) and layer.dielectric_type == "core":
            return (
                QColor(self.palette_map["core"]),
                QColor(self.palette_map["core_outline"]),
                QColor("#1f1e12"),
            )
        return (
            QColor(self.palette_map["prepreg"]),
            QColor(self.palette_map["prepreg_outline"]),
            QColor("#1f1d17"),
        )

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.fillRect(self.rect(), QColor(self.palette_map["bg"]))
            self._hit_regions.clear()

            if not self.zone_editors or not self.rigid_zone_indices:
                painter.setPen(QColor(self.palette_map["muted"]))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Rigid-flex preview is not ready.")
                return

            primary_rigid_index = self.rigid_zone_indices[0]
            primary_rigid_editor = self.zone_editors[primary_rigid_index]
            catalog = primary_rigid_editor.catalog
            width = max(420, self.width())
            height = max(560, self.height())

            title_font = QFont("Bahnschrift", 13, QFont.Weight.Bold)
            body_bold_font = QFont("Bahnschrift", 8, QFont.Weight.Bold)
            summary_font = QFont("Segoe UI", 9)

            painter.setPen(QColor(self.palette_map["text"]))
            painter.setFont(title_font)
            painter.drawText(QRectF(18, 12, width - 36, 28), "Rigid-Flex Live Stackup")
            painter.setPen(QColor(self.palette_map["muted"]))
            painter.setFont(summary_font)
            summary = f"Zones {len(self.zone_editors)} | Rigid {len(self.rigid_zone_indices)} | Flex {len(self.flex_zone_indices)}"
            painter.drawText(QRectF(18, 40, width - 36, 22), summary)

            grid_pen = QPen(QColor(self.palette_map["grid"]), 1)
            painter.setPen(grid_pen)
            for x in range(20, width, 48):
                painter.drawLine(x, 76, x - 16, height - 26)
            for y in range(96, height - 18, 48):
                painter.drawLine(18, y, width - 18, y)

            top_margin = 86.0
            bottom_margin = 36.0
            usable_height = max(220.0, height - top_margin - bottom_margin)
            left_label_width, rigid_x0, rigid_width, flex_widths = self._compute_span_layout(width)

            def rigid_flex_contexts_for_zone(
                rigid_zone_index: int,
            ) -> list[tuple[int, StackupEditorWindow, tuple[int, int]]]:
                contexts: list[tuple[int, StackupEditorWindow, tuple[int, int]]] = []
                rigid_editor = self.zone_editors[rigid_zone_index]
                for candidate_index in (rigid_zone_index - 1, rigid_zone_index + 1):
                    if 0 <= candidate_index < len(self.zone_editors):
                        candidate_editor = self.zone_editors[candidate_index]
                        if candidate_editor.is_flex_zone:
                            bounds = self._shared_bounds(rigid_editor.stackup, candidate_editor.stackup)
                            if bounds is not None:
                                contexts.append((candidate_index, candidate_editor, bounds))
                return contexts

            def draw_rigid_stack(
                editor: StackupEditorWindow,
                *,
                zone_index: int,
                x0: float,
                stack_width_px: float,
                selected_index: int | None,
                align_shared_top_to: float | None = None,
                anchor_flex_zone_index: int | None = None,
                show_left_labels: bool = False,
                linked_flex_contexts: list[tuple[int, StackupEditorWindow, tuple[int, int]]] | None = None,
            ) -> tuple[dict[int, QRectF], dict[int, tuple[int, int]]]:
                stack = editor.stackup
                visuals: list[tuple[int | None, object, float]] = [
                    (None, "soldermask", stack.soldermask.thickness_mm),
                    *[
                        (index, layer, stack.layer_thickness_mm(layer, catalog))
                        for index, layer in enumerate(stack.layers)
                    ],
                    (None, "soldermask", stack.soldermask.thickness_mm),
                ]
                heights_local = self._scaled_heights_from_structure(
                    [layer for _index, layer, _thickness in visuals],
                    usable_height,
                )
                y_positions: list[float] = []
                cursor = top_margin
                for value in heights_local:
                    y_positions.append(cursor)
                    cursor += value

                shared_bounds_by_flex: dict[int, tuple[int, int]] = {}
                shared_layer_maps: dict[int, dict[int, int]] = {}
                for flex_zone_index, flex_editor, bounds in linked_flex_contexts or []:
                    shared_bounds_by_flex[flex_zone_index] = bounds
                    shared_layer_maps[flex_zone_index] = {
                        self._rigid_index_for_flex_layer(stack, flex_editor.stackup, flex_layer_index): flex_layer_index
                        for flex_layer_index in range(len(flex_editor.stackup.layers))
                    }
                shift_y = 0.0
                if (
                    align_shared_top_to is not None
                    and anchor_flex_zone_index is not None
                    and anchor_flex_zone_index in shared_bounds_by_flex
                ):
                    anchor_shared_layer_map = shared_layer_maps.get(anchor_flex_zone_index, {})
                    anchor_rigid_index = next(
                        (
                            rigid_index
                            for rigid_index, flex_layer_index in anchor_shared_layer_map.items()
                            if flex_layer_index == 0
                        ),
                        None,
                    )
                    if anchor_rigid_index is None:
                        anchor_bounds = shared_bounds_by_flex[anchor_flex_zone_index]
                        anchor_rigid_index = anchor_bounds[0]
                    shift_y = align_shared_top_to - y_positions[anchor_rigid_index + 1]

                rects_local: dict[int, QRectF] = {}
                for row_no, (visual, pixel_height) in enumerate(zip(visuals, heights_local)):
                    index, layer, _thickness_mm = visual
                    top_y = y_positions[row_no] + shift_y
                    rect = QRectF(x0, top_y, stack_width_px, pixel_height)
                    is_mapped_flex_layer = (
                        index is not None and any(index in shared_layer_map for shared_layer_map in shared_layer_maps.values())
                    )
                    role = "rigid_copper" if isinstance(layer, CopperLayer) and not is_mapped_flex_layer else None
                    fill, outline, _text_color = self._layer_colors(layer, role=role)
                    highlight = (
                        index is not None
                        and selected_index is not None
                        and self.active_zone_index == zone_index
                        and index == selected_index
                    )
                    if index is not None:
                        for flex_zone_index, shared_layer_map in shared_layer_maps.items():
                            selected_flex_index = self._zone_selected_layer_index(flex_zone_index)
                            if self.active_zone_index == flex_zone_index and selected_flex_index is not None:
                                highlight = highlight or (shared_layer_map.get(index) == selected_flex_index)
                    painter.setPen(QPen(QColor(self.palette_map["accent"]) if highlight else outline, 2 if highlight else 1))
                    painter.setBrush(fill)
                    painter.drawRect(rect)

                    dielectric_text = self._dielectric_rectangle_text(layer)
                    if dielectric_text:
                        self._draw_single_line(
                            painter,
                            rect.adjusted(5, 1, -5, -1),
                            dielectric_text,
                            color=_text_color,
                            font=body_bold_font,
                            min_point_size=5.0,
                        )

                    if index is not None:
                        rects_local[index] = QRectF(rect)
                        self._add_hit_region(rect, zone_index, ("layer", index))
                    else:
                        soldermask_pos = "top" if row_no == 0 else "bottom"
                        self._add_hit_region(rect, zone_index, ("soldermask", soldermask_pos))

                    if show_left_labels and isinstance(layer, CopperLayer):
                        self._draw_single_line(
                            painter,
                            QRectF(0, top_y, left_label_width - 6, pixel_height),
                            editor._copper_label(index),
                            color=QColor(self.palette_map["text"]),
                            font=body_bold_font,
                            min_point_size=5.0,
                            alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                        )
                return rects_local, shared_bounds_by_flex

            def draw_flex_segment(
                *,
                flex_zone_index: int,
                flex_editor: StackupEditorWindow,
                left_rigid_zone_index: int,
                left_rigid_editor: StackupEditorWindow,
                left_rigid_rects: dict[int, QRectF],
                left_shared_bounds: tuple[int, int],
                branch_x0: float,
                branch_width: float,
                right_rigid_zone_index: int | None = None,
                right_rigid_editor: StackupEditorWindow | None = None,
            ) -> tuple[dict[int, QRectF], dict[int, tuple[int, int]]] | None:
                flex_stackup = flex_editor.stackup
                if flex_stackup.coverlay is None:
                    return None
                branch_layer_rects = [
                    left_rigid_rects[self._rigid_index_for_flex_layer(left_rigid_editor.stackup, flex_stackup, layer_index)]
                    for layer_index in range(len(flex_stackup.layers))
                    if self._rigid_index_for_flex_layer(left_rigid_editor.stackup, flex_stackup, layer_index) in left_rigid_rects
                ]
                if len(branch_layer_rects) != len(flex_stackup.layers):
                    return None

                top_rect = branch_layer_rects[0]
                bottom_rect = branch_layer_rects[-1]
                copper_rect_heights = [
                    branch_layer_rects[index].height()
                    for index, layer in enumerate(flex_stackup.layers)
                    if isinstance(layer, CopperLayer)
                ]
                reference_copper_px = (
                    sum(copper_rect_heights) / len(copper_rect_heights)
                    if copper_rect_heights
                    else 24.0
                )
                coverlay_pi_px = max(2.0, reference_copper_px * 0.18)
                adhesive_px = max(2.0, reference_copper_px * 0.28)
                minimum_gap_px = max(2.0, reference_copper_px * 0.18)
                sandwich_slots = flex_stackup.flex_sandwich_slot_ids()
                if not sandwich_slots:
                    return None
                first_slot = sandwich_slots[0]
                last_slot = sandwich_slots[-1]

                flex_items: list[tuple[str, int | None, object | None, QRectF, tuple[str, int | str] | None]] = [
                    (
                        "coverlay_top",
                        None,
                        None,
                        QRectF(branch_x0, top_rect.top() - adhesive_px - coverlay_pi_px, branch_width, coverlay_pi_px),
                        ("coverlay", f"coverlay_{first_slot}_top_pi"),
                    ),
                    (
                        "adhesive_top",
                        None,
                        None,
                        QRectF(branch_x0, top_rect.top() - adhesive_px, branch_width, adhesive_px),
                        ("coverlay", f"coverlay_{first_slot}_top_adhesive"),
                    ),
                ]
                for sandwich_index, sandwich_slot in enumerate(sandwich_slots):
                    flex_start = sandwich_index * 3
                    for layer_index in range(flex_start, min(flex_start + 3, len(flex_stackup.layers))):
                        layer = flex_stackup.layers[layer_index]
                        layer_rect = branch_layer_rects[layer_index]
                        flex_items.append(
                            (
                                f"layer_{layer_index}",
                                layer_index,
                                layer,
                                QRectF(branch_x0, layer_rect.top(), branch_width, layer_rect.height()),
                                ("layer", layer_index),
                            )
                        )

                    if sandwich_index < len(sandwich_slots) - 1:
                        next_sandwich_slot = sandwich_slots[sandwich_index + 1]
                        current_bottom_rect = branch_layer_rects[flex_start + 2]
                        next_top_rect = branch_layer_rects[flex_start + 3]
                        gap_top = current_bottom_rect.bottom()
                        gap_bottom = next_top_rect.top()
                        heights = self._flex_gap_component_heights(
                            gap_bottom - gap_top,
                            coverlay_pi_px=coverlay_pi_px,
                            adhesive_px=adhesive_px,
                            minimum_gap_px=minimum_gap_px,
                        )
                        cursor = gap_top
                        gap_roles = [
                            ("adhesive_bottom", ("coverlay", f"coverlay_{sandwich_slot}_bottom_adhesive")),
                            ("coverlay_bottom", ("coverlay", f"coverlay_{sandwich_slot}_bottom_pi")),
                            ("gap", ("gap", f"air_gap_{sandwich_slot}_{next_sandwich_slot}")),
                            ("coverlay_top", ("coverlay", f"coverlay_{next_sandwich_slot}_top_pi")),
                            ("adhesive_top", ("coverlay", f"coverlay_{next_sandwich_slot}_top_adhesive")),
                        ]
                        for (role, meta), gap_height in zip(gap_roles, heights):
                            flex_items.append(
                                (
                                    role,
                                    None,
                                    None,
                                    QRectF(branch_x0, cursor, branch_width, gap_height),
                                    meta,
                                )
                            )
                            cursor += gap_height

                flex_items.extend(
                    [
                        (
                            "adhesive_bottom",
                            None,
                            None,
                            QRectF(branch_x0, bottom_rect.bottom(), branch_width, adhesive_px),
                            ("coverlay", f"coverlay_{last_slot}_bottom_adhesive"),
                        ),
                        (
                            "coverlay_bottom",
                            None,
                            None,
                            QRectF(branch_x0, bottom_rect.bottom() + adhesive_px, branch_width, coverlay_pi_px),
                            ("coverlay", f"coverlay_{last_slot}_bottom_pi"),
                        ),
                    ]
                )

                painter.setPen(QPen(QColor(self.palette_map["connector"]), 2))
                painter.drawLine(branch_x0, top_rect.top(), branch_x0, bottom_rect.bottom())

                selected_flex_meta = self._zone_selected_meta(flex_zone_index)
                selected_flex_index = self._zone_selected_layer_index(flex_zone_index)
                selected_left_rigid_index = self._zone_selected_layer_index(left_rigid_zone_index)
                right_shared_bounds = (
                    self._shared_bounds(right_rigid_editor.stackup, flex_stackup)
                    if right_rigid_editor is not None
                    else None
                )
                selected_right_rigid_index = (
                    self._zone_selected_layer_index(right_rigid_zone_index)
                    if right_rigid_zone_index is not None
                    else None
                )

                def branch_layer_selected(layer_index: int) -> bool:
                    if self.active_zone_index == flex_zone_index and selected_flex_index is not None:
                        return selected_flex_index == layer_index
                    if (
                        self.active_zone_index == left_rigid_zone_index
                        and selected_left_rigid_index is not None
                        and left_shared_bounds[0] <= selected_left_rigid_index <= left_shared_bounds[1]
                    ):
                        return (
                            self._rigid_index_for_flex_layer(left_rigid_editor.stackup, flex_stackup, layer_index) == selected_left_rigid_index
                        )
                    if (
                        right_rigid_zone_index is not None
                        and self.active_zone_index == right_rigid_zone_index
                        and selected_right_rigid_index is not None
                        and right_shared_bounds is not None
                        and right_shared_bounds[0] <= selected_right_rigid_index <= right_shared_bounds[1]
                    ):
                        return (
                            self._rigid_index_for_flex_layer(right_rigid_editor.stackup, flex_stackup, layer_index) == selected_right_rigid_index
                        )
                    return False

                for role, layer_index, layer, rect, meta in flex_items:
                    if role.startswith("coverlay"):
                        fill, outline, _text_color = self._layer_colors(layer, role="coverlay")
                    elif role.startswith("adhesive"):
                        fill, outline, _text_color = self._layer_colors(layer, role="adhesive")
                    elif role.startswith("gap"):
                        fill = QColor("#3b4048")
                        outline = QColor("#8b919a")
                    else:
                        fill, outline, _text_color = self._layer_colors(layer)
                    highlight = False
                    if layer_index is not None:
                        highlight = branch_layer_selected(layer_index)
                    elif self.active_zone_index == flex_zone_index and selected_flex_meta == meta:
                        highlight = True
                    painter.setPen(QPen(QColor(self.palette_map["accent"]) if highlight else outline, 2 if highlight else 1))
                    painter.setBrush(fill)
                    painter.drawRect(rect)
                    dielectric_text = self._dielectric_rectangle_text(layer)
                    if dielectric_text:
                        self._draw_single_line(
                            painter,
                            rect.adjusted(4, 1, -4, -1),
                            dielectric_text,
                            color=_text_color,
                            font=body_bold_font,
                            min_point_size=5.0,
                        )
                    if meta is not None:
                        self._add_hit_region(rect, flex_zone_index, meta)

                if right_rigid_zone_index is None or right_rigid_editor is None:
                    return None

                right_x0 = branch_x0 + branch_width
                return draw_rigid_stack(
                    right_rigid_editor,
                    zone_index=right_rigid_zone_index,
                    x0=right_x0,
                    stack_width_px=rigid_width,
                    selected_index=selected_right_rigid_index,
                    align_shared_top_to=top_rect.top(),
                    anchor_flex_zone_index=flex_zone_index,
                    show_left_labels=False,
                    linked_flex_contexts=rigid_flex_contexts_for_zone(right_rigid_zone_index),
                )

            current_rigid_zone_index = self.rigid_zone_indices[0]
            current_rigid_editor = self.zone_editors[current_rigid_zone_index]
            current_x0 = rigid_x0
            current_rigid_rects, current_shared_bounds_map = draw_rigid_stack(
                current_rigid_editor,
                zone_index=current_rigid_zone_index,
                x0=current_x0,
                stack_width_px=rigid_width,
                selected_index=self._zone_selected_layer_index(current_rigid_zone_index),
                show_left_labels=True,
                linked_flex_contexts=rigid_flex_contexts_for_zone(current_rigid_zone_index),
            )

            zone_cursor = current_rigid_zone_index + 1
            while zone_cursor < len(self.zone_editors):
                zone_editor = self.zone_editors[zone_cursor]
                if zone_editor.is_flex_zone:
                    flex_zone_index = zone_cursor
                    left_shared_bounds = current_shared_bounds_map.get(flex_zone_index)
                    if left_shared_bounds is None:
                        zone_cursor += 1
                        continue
                    next_rigid_zone_index = None
                    next_rigid_editor = None
                    if zone_cursor + 1 < len(self.zone_editors) and not self.zone_editors[zone_cursor + 1].is_flex_zone:
                        next_rigid_zone_index = zone_cursor + 1
                        next_rigid_editor = self.zone_editors[next_rigid_zone_index]
                    drawn_right_rigid = draw_flex_segment(
                        flex_zone_index=flex_zone_index,
                        flex_editor=zone_editor,
                        left_rigid_zone_index=current_rigid_zone_index,
                        left_rigid_editor=current_rigid_editor,
                        left_rigid_rects=current_rigid_rects,
                        left_shared_bounds=left_shared_bounds,
                        branch_x0=current_x0 + rigid_width,
                        branch_width=flex_widths.get(flex_zone_index, max(26.0, rigid_width * 0.45)),
                        right_rigid_zone_index=next_rigid_zone_index,
                        right_rigid_editor=next_rigid_editor,
                    )
                    if next_rigid_zone_index is not None and next_rigid_editor is not None and drawn_right_rigid is not None:
                        current_x0 = current_x0 + rigid_width + flex_widths.get(
                            flex_zone_index,
                            max(26.0, rigid_width * 0.45),
                        )
                        current_rigid_zone_index = next_rigid_zone_index
                        current_rigid_editor = next_rigid_editor
                        current_rigid_rects, current_shared_bounds_map = drawn_right_rigid
                        zone_cursor = next_rigid_zone_index + 1
                    else:
                        zone_cursor += 1
                else:
                    current_x0 += rigid_width
                    current_rigid_zone_index = zone_cursor
                    current_rigid_editor = zone_editor
                    current_rigid_rects, current_shared_bounds_map = draw_rigid_stack(
                        current_rigid_editor,
                        zone_index=current_rigid_zone_index,
                        x0=current_x0,
                        stack_width_px=rigid_width,
                        selected_index=self._zone_selected_layer_index(current_rigid_zone_index),
                        show_left_labels=False,
                        linked_flex_contexts=rigid_flex_contexts_for_zone(current_rigid_zone_index),
                    )
                    zone_cursor += 1
        finally:
            painter.end()


class RigidFlexEditorWindow(QMainWindow):
    def __init__(self, root_path: Path) -> None:
        super().__init__()
        self.root_path = root_path
        self.setWindowTitle("StackUp Editor — Rigid-Flex")
        self.resize(1660, 940)
        self.setMinimumSize(1080, 680)

        self._zone_editors: list[StackupEditorWindow] = []
        self._flex_sync_source: StackupEditorWindow | None = None
        self._flex_sandwich_history: dict[int, list[list[Stackup]]] = {}

        self._build_file_menu()

        central = QWidget()
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(12, 10, 12, 10)
        outer_layout.setSpacing(8)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(10)
        outer_layout.addWidget(self.main_splitter, 1)

        self.tabs = QTabWidget()
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.main_splitter.addWidget(self.tabs)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(8)
        self.preview_title_label = QLabel("Rigid-Flex Overview")
        self.preview_title_label.setStyleSheet("font: 700 14px 'Bahnschrift'; color: #edf4fa;")
        preview_layout.addWidget(self.preview_title_label)
        self.combined_preview = RigidFlexCombinedPreview()
        self.combined_preview.selectionRequested.connect(self._handle_combined_preview_selection)
        preview_layout.addWidget(self.combined_preview, 1)
        self.main_splitter.addWidget(preview_panel)
        self.main_splitter.setSizes([1180, 560])

        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 4, 0)
        corner_layout.setSpacing(4)

        self.add_zone_button = QPushButton("Add Zone")
        self.add_zone_button.setFixedSize(100, 26)
        self.add_zone_button.clicked.connect(self._add_zone)
        corner_layout.addWidget(self.add_zone_button)

        self.remove_zone_button = QPushButton("Remove Zone")
        self.remove_zone_button.setFixedSize(100, 26)
        self.remove_zone_button.setToolTip("Remove last zone")
        self.remove_zone_button.clicked.connect(self._remove_zone)
        corner_layout.addWidget(self.remove_zone_button)

        self.tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        self.tabs.currentChanged.connect(self._on_current_zone_changed)

        self.setCentralWidget(central)

        # Default view: one rigid zone followed by one flex zone.
        self._add_zone()
        self._add_zone()
        self._apply_default_sample_stackup()
        self._sync_file_menu_state()

    def _build_file_menu(self) -> None:
        self.file_menu = self.menuBar().addMenu("&File")
        self.import_menu = self.file_menu.addMenu("&Import")
        self.export_menu = self.file_menu.addMenu("&Export")

        self.import_text_action = QAction("Stackup text...", self)
        self.import_text_action.triggered.connect(
            lambda: self._trigger_current_zone_file_action("import_text_action")
        )
        self.import_menu.addAction(self.import_text_action)

        self.import_xpedition_action = QAction("Xpedition stackup...", self)
        self.import_xpedition_action.triggered.connect(
            lambda: self._trigger_current_zone_file_action("import_xpedition_action")
        )
        self.import_menu.addAction(self.import_xpedition_action)

        self.export_text_action = QAction("Stackup text...", self)
        self.export_text_action.triggered.connect(
            lambda: self._trigger_current_zone_file_action("export_text_action")
        )
        self.export_menu.addAction(self.export_text_action)

        self.export_xpedition_action = QAction("Xpedition stackup...", self)
        self.export_xpedition_action.triggered.connect(
            lambda: self._trigger_current_zone_file_action("export_xpedition_action")
        )
        self.export_menu.addAction(self.export_xpedition_action)

        self._file_actions = {
            "import_text_action": self.import_text_action,
            "import_xpedition_action": self.import_xpedition_action,
            "export_text_action": self.export_text_action,
            "export_xpedition_action": self.export_xpedition_action,
        }
        self._sync_file_menu_state()

    def _current_zone_editor(self) -> StackupEditorWindow | None:
        index = self.tabs.currentIndex() if hasattr(self, "tabs") else -1
        if 0 <= index < len(self._zone_editors):
            return self._zone_editors[index]
        return None

    def _trigger_current_zone_file_action(self, editor_action_name: str) -> None:
        editor = self._current_zone_editor()
        if editor is None:
            return
        editor_action = getattr(editor, editor_action_name)
        if editor_action.isEnabled():
            editor_action.trigger()

    def _sync_file_menu_state(self) -> None:
        editor = self._current_zone_editor()
        for editor_action_name, action in self._file_actions.items():
            if editor is None:
                action.setEnabled(False)
                action.setStatusTip("No stackup zone is selected.")
                continue
            editor_action = getattr(editor, editor_action_name)
            action.setEnabled(editor_action.isEnabled())
            action.setStatusTip(editor_action.statusTip())

    def _on_current_zone_changed(self, _index: int) -> None:
        self._refresh_combined_preview()
        self._sync_file_menu_state()

    def _make_zone_editor(self, kind: str) -> StackupEditorWindow:
        editor = StackupEditorWindow(self.root_path, zone_kind=kind)
        editor.right_pane.hide()
        editor.main_splitter.setSizes([1600, 0])
        editor.stackupViewChanged.connect(self._refresh_combined_preview)
        if kind == "flex":
            editor.sharedRegionChanged.connect(lambda e=editor: self._sync_all_rigid_zones())
            editor.insertFlexSandwichRequested.connect(lambda e=editor: self._insert_flex_sandwich(e))
            editor.removeFlexSandwichRequested.connect(lambda e=editor: self._remove_flex_sandwich(e))
        else:
            editor.structureChanged.connect(lambda e=editor: self._handle_rigid_structure_change(e))
        return editor

    def _handle_rigid_structure_change(self, editor: StackupEditorWindow) -> None:
        self._configure_rigid_zone(
            editor,
            zone_display_name=editor.zone_display_name,
        )
        self._refresh_combined_preview()

    def _add_zone(self) -> None:
        position = len(self._zone_editors)
        kind = zone_kind_for_position(position)
        logger.info("Adding %s zone at position %s", kind, position)

        editor = self._make_zone_editor(kind)
        if position >= MIN_ZONES:
            self._zone_editors.append(editor)
            self._initialize_new_zone_from_template(editor, kind)
            self._zone_editors.pop()
        central = editor.centralWidget()
        central.setParent(None)

        label = self._zone_label(kind, position)
        self.tabs.addTab(central, label)
        self._zone_editors.append(editor)
        self.tabs.setCurrentIndex(self.tabs.count() - 1)

        self._update_zone_controls()
        if position >= MIN_ZONES:
            self._sync_all_rigid_zones()
        self._refresh_combined_preview()

    def _remove_zone(self) -> None:
        if len(self._zone_editors) <= MIN_ZONES:
            return
        index = len(self._zone_editors) - 1
        editor = self._zone_editors.pop()
        if editor.is_flex_zone:
            self._flex_sandwich_history.pop(self._flex_history_key(editor), None)
        widget = self.tabs.widget(index)
        self.tabs.removeTab(index)
        if widget is not None:
            widget.setParent(None)
        editor.deleteLater()
        logger.info("Removed zone at position %s", index)

        self._update_zone_controls()
        if len(self._zone_editors) >= MIN_ZONES:
            self._sync_all_rigid_zones()
        self._refresh_combined_preview()

    def _zone_label(self, kind: str, position: int) -> str:
        same_kind_count = sum(
            1 for existing_position in range(position) if zone_kind_for_position(existing_position) == kind
        ) + 1
        title = "Rigid" if kind == "rigid" else "Flex"
        return f"{title} zone {same_kind_count}"

    def _update_zone_controls(self) -> None:
        zone_count = len(self._zone_editors)
        next_kind = zone_kind_for_position(zone_count)
        self.add_zone_button.setToolTip(f"Add {next_kind} zone")
        self.remove_zone_button.setEnabled(zone_count > MIN_ZONES)

    def _primary_rigid_editor(self) -> StackupEditorWindow | None:
        return next((editor for editor in self._zone_editors if not editor.is_flex_zone), None)

    def _primary_flex_editor(self) -> StackupEditorWindow | None:
        return next((editor for editor in self._zone_editors if editor.is_flex_zone), None)

    def _rigid_editors(self) -> list[StackupEditorWindow]:
        return [editor for editor in self._zone_editors if not editor.is_flex_zone]

    def _zone_index(self, editor: StackupEditorWindow) -> int | None:
        try:
            return self._zone_editors.index(editor)
        except ValueError:
            return None

    def _adjacent_rigid_editors(self, flex_editor: StackupEditorWindow) -> list[StackupEditorWindow]:
        flex_index = self._zone_index(flex_editor)
        if flex_index is None:
            return []
        neighbors: list[StackupEditorWindow] = []
        for candidate_index in (flex_index - 1, flex_index + 1):
            if 0 <= candidate_index < len(self._zone_editors):
                candidate = self._zone_editors[candidate_index]
                if not candidate.is_flex_zone:
                    neighbors.append(candidate)
        return neighbors

    def _adjacent_flex_editors(self, rigid_editor: StackupEditorWindow) -> list[StackupEditorWindow]:
        rigid_index = self._zone_index(rigid_editor)
        if rigid_index is None:
            return []
        neighbors: list[StackupEditorWindow] = []
        for candidate_index in (rigid_index - 1, rigid_index + 1):
            if 0 <= candidate_index < len(self._zone_editors):
                candidate = self._zone_editors[candidate_index]
                if candidate.is_flex_zone:
                    neighbors.append(candidate)
        return neighbors

    def _shared_region_bounds(
        self,
        rigid_editor: StackupEditorWindow,
        flex_editor: StackupEditorWindow,
    ) -> tuple[int, int] | None:
        try:
            return rigid_shared_region_bounds(rigid_editor.stackup, flex_editor.stackup)
        except ValueError:
            return None

    def _locked_shared_indices(
        self,
        rigid_editor: StackupEditorWindow,
        flex_editor: StackupEditorWindow,
    ) -> tuple[set[int], set[int]]:
        bounds = self._shared_region_bounds(rigid_editor, flex_editor)
        if bounds is None:
            return set(), set()
        mapped_flex_indices = {
            self.combined_preview._rigid_index_for_flex_layer(rigid_editor.stackup, flex_editor.stackup, flex_index)
            for flex_index in range(len(flex_editor.stackup.layers))
        }
        locked_copper: set[int] = set()
        locked_dielectric: set[int] = set()
        for index in mapped_flex_indices:
            layer = rigid_editor.stackup.layers[index]
            if isinstance(layer, CopperLayer):
                locked_copper.add(index)
            elif isinstance(layer, FlexCoreLayer):
                locked_dielectric.add(index)
        return locked_copper, locked_dielectric

    def _flex_copper_number_overrides(
        self,
        rigid_editor: StackupEditorWindow,
        flex_editor: StackupEditorWindow,
    ) -> dict[int, int]:
        rigid_total_copper = rigid_editor.stackup.copper_count()
        slot_capacity = flex_editor.stackup.flex_slot_capacity_or_count()
        start_number = ((rigid_total_copper - (slot_capacity * 2)) // 2) + 1
        mapping: dict[int, int] = {}
        for index, layer in enumerate(flex_editor.stackup.layers):
            if isinstance(layer, CopperLayer):
                slot_id = flex_editor.stackup.flex_slot_for_layer_index(index)
                copper_offset = index % 3
                mapping[index] = start_number + (slot_id * 2) + (0 if copper_offset == 0 else 1)
        return mapping

    def _selected_flex_sandwich_slot(self, flex_editor: StackupEditorWindow) -> int | None:
        meta = flex_editor._current_row_meta()
        if not isinstance(meta, tuple) or len(meta) != 2:
            return None
        if meta[0] == "layer":
            return flex_editor.stackup.flex_slot_for_layer_index(int(meta[1]))
        if meta[0] == "coverlay":
            parts = flex_editor._coverlay_meta_parts(str(meta[1]))
            return parts[0] if parts is not None else None
        return None

    def _flex_slot_templates(self, flex_editor: StackupEditorWindow) -> dict[int, FlexCoreLayer]:
        templates: dict[int, FlexCoreLayer] = {}
        for layer_index, layer in enumerate(flex_editor.stackup.layers):
            if isinstance(layer, FlexCoreLayer):
                templates[flex_editor.stackup.flex_slot_for_layer_index(layer_index)] = deepcopy(layer)
        return templates

    def _rigid_core_template_for_slots(self, rigid_editor: StackupEditorWindow) -> DielectricLayer:
        for layer in rigid_editor.stackup.layers:
            if isinstance(layer, DielectricLayer) and layer.dielectric_type == "core":
                return deepcopy(layer)
        return rigid_editor._default_dielectric("core")

    def _prepreg_templates_for_slot_capacity(
        self,
        rigid_editor: StackupEditorWindow,
        slot_capacity: int,
    ) -> tuple[DielectricLayer, DielectricLayer]:
        default_prepreg = rigid_editor._default_dielectric("prepreg")
        try:
            start, end = rigid_shared_region_bounds_for_capacity(rigid_editor.stackup, slot_capacity)
        except ValueError:
            return deepcopy(default_prepreg), deepcopy(default_prepreg)

        boundary_candidate: DielectricLayer | None = None
        bridge_candidate: DielectricLayer | None = None
        layers = rigid_editor.stackup.layers
        scan_start = max(0, start - 1)
        scan_end = min(len(layers) - 1, end + 1)
        for index in range(scan_start, scan_end + 1):
            layer = layers[index]
            if not isinstance(layer, DielectricLayer):
                continue
            if layer.dielectric_type == "prepreg":
                if boundary_candidate is None and index in {start - 1, end + 1}:
                    boundary_candidate = deepcopy(layer)
                if bridge_candidate is None and start <= index <= end:
                    bridge_candidate = deepcopy(layer)

        boundary = boundary_candidate or bridge_candidate or deepcopy(default_prepreg)
        bridge = bridge_candidate or boundary_candidate or deepcopy(default_prepreg)
        return deepcopy(boundary), deepcopy(bridge)

    def _slot_capacity_for_rigid_zone(self, rigid_editor: StackupEditorWindow) -> int:
        adjacent_flexes = self._adjacent_flex_editors(rigid_editor)
        capacities = [editor.stackup.flex_slot_capacity_or_count() for editor in adjacent_flexes if editor.stackup.flex_slot_capacity_or_count() > 0]
        return max(capacities, default=0)

    def _sync_all_rigid_zones(self) -> None:
        for rigid_editor in self._rigid_editors():
            adjacent_flexes = self._adjacent_flex_editors(rigid_editor)
            if not adjacent_flexes:
                continue

            slot_capacity = self._slot_capacity_for_rigid_zone(rigid_editor)
            if slot_capacity <= 0:
                continue

            active_slot_ids: set[int] = set()
            slot_templates: dict[int, FlexCoreLayer] = {}
            for flex_editor in adjacent_flexes:
                active_slot_ids |= flex_editor.stackup.active_flex_slot_ids()
                for slot_id, template in self._flex_slot_templates(flex_editor).items():
                    slot_templates.setdefault(slot_id, deepcopy(template))

            if not active_slot_ids:
                continue

            selected_meta = rigid_editor._current_row_meta() or ("layer", 0)
            outer_boundary_template, bridge_template = self._prepreg_templates_for_slot_capacity(
                rigid_editor,
                slot_capacity,
            )
            new_rigid_stackup = rebuild_rigid_stackup_from_slot_activity(
                rigid_editor.stackup,
                slot_capacity=slot_capacity,
                active_slot_ids=active_slot_ids,
                slot_templates=slot_templates,
                rigid_core_template=self._rigid_core_template_for_slots(rigid_editor),
                bridge_dielectric_template=bridge_template,
                outer_boundary_dielectric_template=outer_boundary_template,
            )
            rigid_editor.replace_stackup(new_rigid_stackup, select_meta=selected_meta)
            self._configure_rigid_zone(
                rigid_editor,
                zone_display_name=rigid_editor.zone_display_name,
            )
            rigid_editor._set_note("Shared rows were rebuilt from adjacent flex-zone activity.")

        for flex_editor in (editor for editor in self._zone_editors if editor.is_flex_zone):
            adjacent_rigids = self._adjacent_rigid_editors(flex_editor)
            self._configure_flex_zone(
                flex_editor,
                rigid_editor=adjacent_rigids[0] if adjacent_rigids else None,
                zone_display_name=flex_editor.zone_display_name,
            )

        self._refresh_combined_preview()

    def _refresh_combined_preview(self) -> None:
        self.combined_preview.set_sources(
            self._zone_editors,
            active_zone_index=self.tabs.currentIndex(),
        )

    def _handle_combined_preview_selection(self, zone_index: int, meta: object) -> None:
        if not isinstance(meta, tuple) or len(meta) != 2:
            return
        if zone_index < 0 or zone_index >= len(self._zone_editors):
            return
        target_editor = self._zone_editors[zone_index]
        self.tabs.setCurrentIndex(zone_index)
        target_editor._refresh_everything(select_meta=meta)

    def _disable_unsupported_zone_actions(self, editor: StackupEditorWindow) -> None:
        editor.import_xpedition_action.setEnabled(False)
        editor.import_text_action.setEnabled(False)
        editor.export_xpedition_action.setEnabled(False)
        editor.export_text_action.setEnabled(False)
        editor.calculate_impedance_button.setEnabled(False)
        editor.import_xpedition_action.setStatusTip("Rigid-flex import is not wired yet.")
        editor.import_text_action.setStatusTip("Rigid-flex import is not wired yet.")
        editor.export_xpedition_action.setStatusTip("Rigid-flex export is not wired yet.")
        editor.export_text_action.setStatusTip("Rigid-flex export is not wired yet.")
        editor.calculate_impedance_button.setToolTip("Rigid-flex impedance workflow is not wired yet.")
        self._sync_file_menu_state()

    def _configure_rigid_zone(
        self,
        editor: StackupEditorWindow,
        *,
        flex_editor: StackupEditorWindow | None = None,
        zone_display_name: str,
    ) -> None:
        flex_sources = [flex_editor] if flex_editor is not None else self._adjacent_flex_editors(editor)
        locked_copper: set[int] = set()
        locked_dielectric: set[int] = set()
        shared_bounds: list[tuple[int, int]] = []
        blocked_material_indices: set[int] = set()
        for flex_source in flex_sources:
            bounds = self._shared_region_bounds(editor, flex_source)
            if bounds is not None:
                shared_bounds.append(bounds)
            source_locked_copper, source_locked_dielectric = self._locked_shared_indices(editor, flex_source)
            locked_copper |= source_locked_copper
            locked_dielectric |= source_locked_dielectric
            if bounds is None:
                continue
            slot_capacity = flex_source.stackup.flex_slot_capacity_or_count()
            for slot_id in range(slot_capacity):
                top_index, bottom_index = rigid_slot_copper_indices(editor.stackup, slot_capacity, slot_id)
                blocked_material_indices.update(range(top_index + 1, bottom_index))
        minimum_copper_count = max(
            max(
                flex_source.stackup.copper_count() + 2,
                flex_source.stackup.flex_slot_capacity_or_count() * 2,
            )
            for flex_source in flex_sources
        ) if flex_sources else 2
        protected_structure_bounds = (
            (min(start for start, _end in shared_bounds), max(end for _start, end in shared_bounds))
            if shared_bounds
            else None
        )
        material_insertion_allowed_indices = {
            index
            for index, layer in enumerate(editor.stackup.layers)
            if isinstance(layer, DielectricLayer) and index not in blocked_material_indices
        }
        editor.configure_zone_links(
            locked_copper_indices=locked_copper,
            locked_dielectric_indices=locked_dielectric,
            protected_structure_bounds=protected_structure_bounds,
            material_insertion_allowed_indices=material_insertion_allowed_indices,
            structure_locked=True,
            minimum_copper_count=minimum_copper_count,
            zone_display_name=zone_display_name,
        )

    def _configure_flex_zone(
        self,
        editor: StackupEditorWindow,
        *,
        rigid_editor: StackupEditorWindow | None = None,
        zone_display_name: str,
    ) -> None:
        rigid_source = rigid_editor or (self._adjacent_rigid_editors(editor)[0] if self._adjacent_rigid_editors(editor) else None)
        copper_number_overrides = (
            self._flex_copper_number_overrides(rigid_source, editor) if rigid_source is not None else {}
        )
        editor.configure_zone_links(
            display_copper_numbers=copper_number_overrides,
            structure_locked=True,
            zone_display_name=zone_display_name,
        )

    def _initialize_new_zone_from_template(self, editor: StackupEditorWindow, kind: str) -> None:
        source = next(
            (existing for existing in self._zone_editors[:-1] if existing.is_flex_zone == (kind == "flex")),
            None,
        )
        if source is None:
            self._disable_unsupported_zone_actions(editor)
            return
        editor.replace_stackup(deepcopy(source.stackup), select_meta=source._current_row_meta() or ("layer", 0))
        editor.configure_zone_links(
            display_copper_numbers=dict(source.copper_number_overrides),
            locked_copper_indices=set(source.locked_copper_indices),
            locked_dielectric_indices=set(source.locked_dielectric_indices),
            protected_structure_bounds=source.protected_structure_bounds,
            material_insertion_allowed_indices=(
                set(source.material_insertion_allowed_indices)
                if source.material_insertion_allowed_indices is not None
                else None
            ),
            structure_locked=source.structure_locked,
            minimum_copper_count=source.minimum_copper_count,
            zone_display_name=source.zone_display_name,
        )
        if kind == "flex":
            source_history = self._flex_sandwich_history.get(self._flex_history_key(source))
            if source_history:
                self._flex_sandwich_history[self._flex_history_key(editor)] = [
                    [deepcopy(stackup) for stackup in snapshot_group]
                    for snapshot_group in source_history
                ]
            else:
                self._flex_sandwich_history.pop(self._flex_history_key(editor), None)
        self._disable_unsupported_zone_actions(editor)

    def _flex_history_key(self, flex_editor: StackupEditorWindow) -> int:
        return id(flex_editor)

    def _record_flex_sandwich_snapshot(self, flex_editor: StackupEditorWindow) -> None:
        adjacent_rigids = self._adjacent_rigid_editors(flex_editor)
        if not adjacent_rigids:
            return
        key = self._flex_history_key(flex_editor)
        snapshot_group = [deepcopy(rigid_editor.stackup) for rigid_editor in adjacent_rigids]
        self._flex_sandwich_history.setdefault(key, []).append(snapshot_group)

    def _restore_flex_sandwich_snapshot(self, flex_editor: StackupEditorWindow) -> bool:
        key = self._flex_history_key(flex_editor)
        history = self._flex_sandwich_history.get(key)
        if not history:
            return False

        snapshot_group = history.pop()
        if history:
            self._flex_sandwich_history[key] = history
        else:
            self._flex_sandwich_history.pop(key, None)

        adjacent_rigids = self._adjacent_rigid_editors(flex_editor)
        if not adjacent_rigids:
            return False

        fallback_stackup = deepcopy(snapshot_group[0]) if snapshot_group else None
        for index, rigid_editor in enumerate(adjacent_rigids):
            snapshot = deepcopy(snapshot_group[index]) if index < len(snapshot_group) else deepcopy(fallback_stackup)
            if snapshot is None:
                continue
            rigid_editor.replace_stackup(snapshot, select_meta=rigid_editor._current_row_meta() or ("layer", 0))
        return True

    def _flex_core_template_for_insertion(self, flex_editor: StackupEditorWindow) -> FlexCoreLayer | None:
        selected_index = flex_editor._selected_index()
        if selected_index is not None:
            selected_layer = flex_editor.stackup.layers[selected_index]
            if isinstance(selected_layer, FlexCoreLayer):
                return deepcopy(selected_layer)
        for layer in flex_editor.stackup.layers:
            if isinstance(layer, FlexCoreLayer):
                return deepcopy(layer)
        return None

    def _bridge_dielectric_template_for_insertion(
        self,
        flex_editor: StackupEditorWindow,
        reference_rigid: StackupEditorWindow,
    ) -> DielectricLayer:
        bounds = self._shared_region_bounds(reference_rigid, flex_editor)
        if bounds is not None:
            for index in range(bounds[0], bounds[1] + 1):
                layer = reference_rigid.stackup.layers[index]
                if isinstance(layer, DielectricLayer):
                    return deepcopy(layer)
            top_neighbor = bounds[0] - 1
            if top_neighbor >= 0:
                layer = reference_rigid.stackup.layers[top_neighbor]
                if isinstance(layer, DielectricLayer):
                    return deepcopy(layer)
            bottom_neighbor = bounds[1] + 1
            if bottom_neighbor < len(reference_rigid.stackup.layers):
                layer = reference_rigid.stackup.layers[bottom_neighbor]
                if isinstance(layer, DielectricLayer):
                    return deepcopy(layer)

        return reference_rigid._default_dielectric("prepreg")

    def _outer_boundary_prepreg_template(
        self,
        flex_editor: StackupEditorWindow,
        reference_rigid: StackupEditorWindow,
    ) -> DielectricLayer:
        bridge = self._bridge_dielectric_template_for_insertion(flex_editor, reference_rigid)
        if bridge.dielectric_type == "prepreg":
            return bridge
        fallback = reference_rigid._default_dielectric("prepreg")
        if bridge.material_id:
            return deepcopy(bridge) if bridge.dielectric_type == "prepreg" else fallback
        return fallback

    def _sync_rigid_zones_from_flex_zone(self, flex_editor: StackupEditorWindow) -> None:
        _ = flex_editor
        self._sync_all_rigid_zones()

    def _insert_flex_sandwich(self, flex_editor: StackupEditorWindow) -> None:
        if not flex_editor.is_flex_zone:
            return
        adjacent_rigids = self._adjacent_rigid_editors(flex_editor)
        if not adjacent_rigids:
            QMessageBox.information(self, "Cannot insert sandwich", "No linked rigid zone was found for this flex zone.")
            return

        reference_rigid = adjacent_rigids[0]
        current_flex_copper = flex_editor.stackup.copper_count()
        target_flex_copper = current_flex_copper + 2
        rigid_total_copper = reference_rigid.stackup.copper_count()
        if target_flex_copper >= rigid_total_copper:
            QMessageBox.information(
                self,
                "Cannot insert sandwich",
                "Flex copper count must stay lower than the rigid copper count. This zone cannot accept another symmetric flex sandwich.",
            )
            return

        answer = QMessageBox.question(
            self,
            "Insert flex sandwich",
            "Inserting a flex sandwich will rebuild the linked rigid stackup symmetrically and may change shared rigid dielectrics from core to prepreg or vice versa.\n\nDo you want to continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        flex_core_template = self._flex_core_template_for_insertion(flex_editor)
        if flex_core_template is None or flex_editor.stackup.coverlay is None:
            QMessageBox.information(self, "Cannot insert sandwich", "The current flex zone does not contain a valid flex-core construction.")
            return

        current_slots = flex_editor.stackup.flex_sandwich_slot_ids()
        current_capacity = flex_editor.stackup.flex_slot_capacity_or_count()
        missing_slots = [slot_id for slot_id in range(current_capacity) if slot_id not in current_slots]
        if missing_slots:
            new_slot = missing_slots[0]
            new_capacity = current_capacity
        else:
            new_slot = current_capacity
            new_capacity = current_capacity + 1
        new_slots = sorted([*current_slots, new_slot])
        inserted_sandwich_index = new_slots.index(new_slot)
        new_flex_stackup = build_flex_stackup_from_templates(
            flex_core_template=flex_core_template,
            coverlay=flex_editor.stackup.coverlay,
            slot_indices=new_slots,
            slot_capacity=new_capacity,
        )
        flex_editor.replace_stackup(new_flex_stackup, select_meta=("layer", inserted_sandwich_index * 3 + 1))
        self._sync_all_rigid_zones()
        flex_editor._set_note("A symmetric flex sandwich was inserted and the linked rigid zone(s) were rebuilt.")

    def _remove_flex_sandwich(self, flex_editor: StackupEditorWindow) -> None:
        if not flex_editor.is_flex_zone:
            return
        current_slots = flex_editor.stackup.flex_sandwich_slot_ids()
        if len(current_slots) <= 1:
            QMessageBox.information(
                self,
                "Cannot remove sandwich",
                "This flex zone already uses the minimum symmetric construction and cannot remove another flex sandwich.",
            )
            return

        adjacent_rigids = self._adjacent_rigid_editors(flex_editor)
        if not adjacent_rigids:
            QMessageBox.information(self, "Cannot remove sandwich", "No linked rigid zone was found for this flex zone.")
            return

        answer = QMessageBox.question(
            self,
            "Remove flex sandwich",
            "Removing a flex sandwich will rebuild the linked rigid stackup symmetrically and restore the previous rigid shell around the flex region.\n\nDo you want to continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        selected_slot = self._selected_flex_sandwich_slot(flex_editor)
        if selected_slot is None or selected_slot not in current_slots:
            QMessageBox.information(
                self,
                "Cannot remove sandwich",
                "Select a row inside the flex sandwich you want to remove first.",
            )
            return

        flex_core_template = self._flex_core_template_for_insertion(flex_editor)
        if flex_core_template is None or flex_editor.stackup.coverlay is None:
            QMessageBox.information(self, "Cannot remove sandwich", "The current flex zone does not contain a valid flex-core construction.")
            return

        new_slots = [slot_id for slot_id in current_slots if slot_id != selected_slot]
        current_capacity = flex_editor.stackup.flex_slot_capacity_or_count()
        fallback_index = 0
        new_flex_stackup = build_flex_stackup_from_templates(
            flex_core_template=flex_core_template,
            coverlay=flex_editor.stackup.coverlay,
            slot_indices=new_slots,
            slot_capacity=current_capacity,
        )
        flex_editor.replace_stackup(new_flex_stackup, select_meta=("layer", fallback_index * 3 + 1))
        self._sync_all_rigid_zones()
        flex_editor._set_note(f"Flex sandwich {selected_slot + 1} was removed and the linked rigid zone(s) were rebuilt.")

    def _apply_default_sample_stackup(self) -> None:
        rigid_editor = self._primary_rigid_editor()
        flex_editor = self._primary_flex_editor()
        if rigid_editor is None or flex_editor is None:
            return
        if flex_editor.flex_core_catalog is None or flex_editor.coverlay_catalog is None:
            return

        flex_entry = preferred_default_flex_core_entry(flex_editor.flex_core_catalog)
        rigid_stackup = build_default_rigid_flex_rigid_stackup(rigid_editor.catalog, flex_entry=flex_entry)
        flex_stackup = build_default_flex_stackup(
            flex_editor.flex_core_catalog,
            flex_editor.coverlay_catalog,
            flex_entry=flex_entry,
        )

        rigid_editor.replace_stackup(rigid_stackup, select_meta=("layer", 0))
        flex_editor.replace_stackup(flex_stackup, select_meta=("layer", 1))

        self._configure_rigid_zone(rigid_editor, flex_editor=flex_editor, zone_display_name="Rigid Part")
        self._configure_flex_zone(flex_editor, rigid_editor=rigid_editor, zone_display_name="Flex Part")

        self.tabs.setTabText(self._zone_editors.index(rigid_editor), "Rigid Part")
        self.tabs.setTabText(self._zone_editors.index(flex_editor), "Flex Part")

        for editor in (rigid_editor, flex_editor):
            self._disable_unsupported_zone_actions(editor)
        self._refresh_combined_preview()
