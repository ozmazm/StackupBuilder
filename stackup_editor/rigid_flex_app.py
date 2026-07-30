"""rigid_flex_app.py — Rigid-Flex stackup window.

The first rigid part is the master layer-numbering reference. Each Flex Part
can contain several sandwiches, each additional rigid part explicitly selects
its local sandwich span, and a rigid part can own another downstream Flex Part.

Each zone tab reuses the existing StackupEditorWindow wholesale (its central
widget is lifted out and placed into the tab) so the rigid-flex window is
literally "the current view, per zone" rather than a rebuild. Rigid zones
use the standard rigid stackup model, while flex zones now start from a
fixed coverlay + flex-core construction.
"""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from dataclasses import replace
from itertools import combinations
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from stackup_editor.catalog import MaterialCatalog
from stackup_editor.exporter import (
    RigidFlexZoneState,
    export_rigid_flex_xpedition,
    export_rigid_flex_text,
    import_rigid_flex_text,
    stackup_import_mode_warning,
)
from stackup_editor.models import (
    CopperLayer,
    DielectricLayer,
    FlexCoreLayer,
    Stackup,
    build_default_flex_stackup,
    build_flex_stackup_from_templates,
    build_default_rigid_flex_rigid_stackup,
    is_dummy_core_type,
    is_etched_core_type,
    is_no_flow_prepreg_type,
    is_prepreg_dielectric_type,
    preferred_default_flex_core_entry,
    rebuild_rigid_stackup_from_slot_activity,
    rigid_shared_region_bounds,
    rigid_shared_region_bounds_for_capacity,
    rigid_slot_copper_indices,
)
from stackup_editor.qt_app import StackupEditorWindow
from stackup_editor.units import (
    SUPPORTED_UNITS,
    format_compact_thickness,
    format_roughness_um,
    format_total_thickness,
    thickness_unit_for_layer,
    total_unit,
)

logger = logging.getLogger(__name__)

MIN_ZONES = 2


def zone_kind_for_position(position: int) -> str:
    """Return the controlled branching layout kind for a zero-based tab position."""
    return "flex" if position == 1 else "rigid"


class FlexPartTabBar(QTabBar):
    selectionGesture = Signal(int, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ZoneTabBar")
        self.setExpanding(False)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideNone)
        self.setStyleSheet(
            """
            QTabBar#ZoneTabBar::tab {
                padding: 5px 8px;
                margin-right: 2px;
                min-width: 0;
                font-size: 9pt;
            }
            QTabBar#ZoneTabBar[denseTabs="true"]::tab {
                padding: 3px 5px;
                margin-right: 1px;
                font-size: 7pt;
            }
            QTabBar#ZoneTabBar QToolButton {
                min-width: 20px;
                max-width: 20px;
                padding: 0;
                border-radius: 5px;
            }
            """
        )

    def _refresh_tab_density(self) -> None:
        visible_count = sum(self.isTabVisible(index) for index in range(self.count()))
        dense = visible_count >= 7
        if self.property("denseTabs") == dense:
            return
        self.setProperty("denseTabs", dense)
        self.style().unpolish(self)
        self.style().polish(self)
        self.updateGeometry()

    def tabSizeHint(self, index: int) -> QSize:  # type: ignore[override]
        dense = bool(self.property("denseTabs"))
        font = QFont(self.font())
        font.setPointSizeF(7.0 if dense else 9.0)
        font.setBold(True)
        text_width = QFontMetrics(font).horizontalAdvance(self.tabText(index))
        horizontal_padding = 10 if dense else 18
        return QSize(max(38, text_width + horizontal_padding), 25 if dense else 31)

    def minimumTabSizeHint(self, index: int) -> QSize:  # type: ignore[override]
        return self.tabSizeHint(index)

    def tabInserted(self, index: int) -> None:  # type: ignore[override]
        super().tabInserted(index)
        self._refresh_tab_density()

    def tabRemoved(self, index: int) -> None:  # type: ignore[override]
        super().tabRemoved(index)
        self._refresh_tab_density()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        index = self.tabAt(event.position().toPoint())
        if index >= 0:
            self.selectionGesture.emit(
                index,
                bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier),
            )
        super().mousePressEvent(event)


class AddRigidPartDialog(QDialog):
    def __init__(
        self,
        *,
        flex_part_labels: list[tuple[int, str]],
        flex_layer_counts: dict[int, int],
        master_layer_count: int,
        suggested_name: str,
        default_selected_flex_indices: set[int] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Rigid Part")
        self.setModal(True)
        self.setMinimumWidth(430)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(suggested_name)
        form.addRow("Rigid part name", self.name_edit)
        self.flex_layer_counts = dict(flex_layer_counts)
        self.master_layer_count = master_layer_count
        self.construction_combo = QComboBox()
        form.addRow("Construction", self.construction_combo)
        layout.addLayout(form)

        layout.addWidget(QLabel("Connected Flex Parts"))
        self.flex_part_checks: dict[int, QCheckBox] = {}
        available_indices = {zone_index for zone_index, _label in flex_part_labels}
        initially_selected = set(default_selected_flex_indices or set()) & available_indices
        if not initially_selected and flex_part_labels:
            initially_selected = {flex_part_labels[0][0]}
        for zone_index, label in flex_part_labels:
            checkbox = QCheckBox(label)
            checkbox.setChecked(zone_index in initially_selected)
            layout.addWidget(checkbox)
            self.flex_part_checks[zone_index] = checkbox

        self.selection_hint = QLabel("")
        self.selection_hint.setWordWrap(True)
        self.selection_hint.setStyleSheet("color: #8fa9bf;")
        layout.addWidget(self.selection_hint)

        topology_note = QLabel(
            "Global copper and flex-sandwich topology is locked while additional rigid parts exist."
        )
        topology_note.setWordWrap(True)
        topology_note.setStyleSheet("color: #8fa9bf;")
        layout.addWidget(topology_note)

        self.validation_label = QLabel("")
        self.validation_label.setStyleSheet("color: #ff8b70;")
        layout.addWidget(self.validation_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.construction_combo.currentIndexChanged.connect(self._update_selection_hint)
        for checkbox in self.flex_part_checks.values():
            checkbox.toggled.connect(self._refresh_construction_options)
        self._refresh_construction_options()
        self._update_selection_hint()

    def _refresh_construction_options(self) -> None:
        previous_count = self.target_copper_count()
        minimum_count = max(4, self.suggested_copper_count())
        if minimum_count % 2:
            minimum_count += 1
        available_counts = list(
            range(minimum_count, self.master_layer_count + 1, 2)
        )

        self.construction_combo.blockSignals(True)
        self.construction_combo.clear()
        for copper_count in available_counts:
            self.construction_combo.addItem(
                f"{copper_count} copper layers",
                copper_count,
            )
        preferred_count = (
            previous_count
            if previous_count in available_counts
            else (available_counts[0] if available_counts else None)
        )
        if preferred_count is not None:
            self.construction_combo.setCurrentIndex(
                available_counts.index(preferred_count)
            )
        self.construction_combo.blockSignals(False)
        self._update_selection_hint()

    def _update_selection_hint(self) -> None:
        target_copper_count = self.target_copper_count()
        if target_copper_count is not None:
            self.selection_hint.setText(
                f"All selected Flex Parts will connect to one {target_copper_count}-layer "
                "rigid construction."
            )
            return
        self.selection_hint.setText(
            "The selected Flex Parts require more copper layers than the Master Rigid Part."
        )

    def _accept_if_valid(self) -> None:
        if not self.name_edit.text().strip():
            self.validation_label.setText("Enter a rigid-part name.")
            return
        if not self.selected_flex_indices():
            self.validation_label.setText("Select at least one Flex Part.")
            return
        if self.target_copper_count() is None:
            self.validation_label.setText(
                "No valid rigid construction is available for the selected Flex Parts."
            )
            return
        self.accept()

    def rigid_name(self) -> str:
        return self.name_edit.text().strip()

    def target_copper_count(self) -> int | None:
        value = self.construction_combo.currentData()
        if not isinstance(value, int):
            return None
        return value

    def selected_flex_indices(self) -> set[int]:
        return {
            zone_index
            for zone_index, checkbox in self.flex_part_checks.items()
            if checkbox.isChecked()
        }

    def suggested_copper_count(self) -> int:
        return sum(
            self.flex_layer_counts.get(zone_index, 0)
            for zone_index in self.selected_flex_indices()
        ) + 2


class RigidFlexCombinedPreview(QWidget):
    selectionRequested = Signal(int, object)
    contextMenuRequested = Signal(int, object, object)
    focusRequested = Signal(int)
    overviewRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(420, 560)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.zone_editors: list[StackupEditorWindow] = []
        self.rigid_zone_indices: list[int] = []
        self.flex_zone_indices: list[int] = []
        self.rigid_editor: StackupEditorWindow | None = None
        self.flex_editor: StackupEditorWindow | None = None
        self.active_zone_index = 0
        self.branch_coverage_by_zone: dict[int, set[int]] = {}
        self.branch_slot_maps_by_zone: dict[int, dict[int, int]] = {}
        self.branch_slot_gaps_by_zone: dict[int, dict[int, int]] = {}
        self.branch_global_numbers_by_zone: dict[int, list[int]] = {}
        self.flex_parent_rigid_by_zone: dict[int, int] = {}
        self.flex_child_rigids_by_zone: dict[int, list[int]] = {}
        self.zone_display_names: dict[int, str] = {}
        self._hit_regions: list[tuple[QRectF, int, tuple[str, int | str]]] = []
        self._card_regions: list[tuple[QRectF, int]] = []
        self._preview_action_regions: list[tuple[QRectF, str, int | None]] = []
        self.focused_rigid_zone_index: int | None = None
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
            "no_flow_prepreg": "#5a321e",
            "no_flow_prepreg_outline": "#a66a43",
            "dummy_core": "#e67e22",
            "dummy_core_outline": "#ffb05c",
            "etched_core": "#f39c12",
            "etched_core_outline": "#ffc35a",
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
        branch_coverage_by_zone: dict[int, set[int]] | None = None,
        branch_slot_maps_by_zone: dict[int, dict[int, int]] | None = None,
        branch_slot_gaps_by_zone: dict[int, dict[int, int]] | None = None,
        branch_global_numbers_by_zone: dict[int, list[int]] | None = None,
        flex_parent_rigid_by_zone: dict[int, int] | None = None,
        flex_child_rigids_by_zone: dict[int, list[int]] | None = None,
        zone_display_names: dict[int, str] | None = None,
    ) -> None:
        self.zone_editors = list(zone_editors)
        self.rigid_zone_indices = [i for i, editor in enumerate(self.zone_editors) if not editor.is_flex_zone]
        self.flex_zone_indices = [i for i, editor in enumerate(self.zone_editors) if editor.is_flex_zone]
        primary_rigid_index = self.rigid_zone_indices[0] if self.rigid_zone_indices else None
        primary_flex_index = self.flex_zone_indices[0] if self.flex_zone_indices else None
        self.rigid_editor = self.zone_editors[primary_rigid_index] if primary_rigid_index is not None else None
        self.flex_editor = self.zone_editors[primary_flex_index] if primary_flex_index is not None else None
        self.active_zone_index = active_zone_index
        self.branch_coverage_by_zone = {
            int(zone_index): set(slot_ids)
            for zone_index, slot_ids in (branch_coverage_by_zone or {}).items()
        }
        self.branch_slot_maps_by_zone = {
            int(zone_index): dict(slot_map)
            for zone_index, slot_map in (branch_slot_maps_by_zone or {}).items()
        }
        self.branch_slot_gaps_by_zone = {
            int(zone_index): dict(slot_gaps)
            for zone_index, slot_gaps in (branch_slot_gaps_by_zone or {}).items()
        }
        self.branch_global_numbers_by_zone = {
            int(zone_index): list(numbers)
            for zone_index, numbers in (branch_global_numbers_by_zone or {}).items()
        }
        self.flex_parent_rigid_by_zone = dict(flex_parent_rigid_by_zone or {})
        self.flex_child_rigids_by_zone = {
            int(zone_index): list(child_indices)
            for zone_index, child_indices in (flex_child_rigids_by_zone or {}).items()
        }
        self.zone_display_names = dict(zone_display_names or {})
        if self.focused_rigid_zone_index not in self.rigid_zone_indices:
            self.focused_rigid_zone_index = None
        self.update()

    def focus_rigid_part(self, zone_index: int) -> None:
        if zone_index not in self.rigid_zone_indices or zone_index == self.rigid_zone_indices[0]:
            return
        self.focused_rigid_zone_index = zone_index
        self.update()

    def show_overview(self) -> None:
        if self.focused_rigid_zone_index is None:
            return
        self.focused_rigid_zone_index = None
        self.update()

    def _action_at(self, point: QPointF) -> tuple[str, int | None] | None:
        for rect, action, zone_index in reversed(self._preview_action_regions):
            if rect.contains(point):
                return action, zone_index
        return None

    def _card_at(self, point: QPointF) -> int | None:
        for rect, zone_index in reversed(self._card_regions):
            if rect.contains(point):
                return zone_index
        return None

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        point = QPointF(event.position())
        action = self._action_at(point)
        if action is not None:
            action_name, zone_index = action
            if action_name == "focus" and zone_index is not None:
                self.focusRequested.emit(zone_index)
            elif action_name == "overview":
                self.overviewRequested.emit()
            event.accept()
            return
        card_zone_index = self._card_at(point)
        if card_zone_index is not None:
            self.selectionRequested.emit(card_zone_index, ("zone", "summary"))
            event.accept()
            return
        for rect, zone_index, meta in reversed(self._hit_regions):
            if rect.contains(point):
                self.selectionRequested.emit(zone_index, meta)
                break
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        point = QPointF(event.position())
        card_zone_index = self._card_at(point)
        if card_zone_index is not None:
            self.focusRequested.emit(card_zone_index)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key.Key_Escape and self.focused_rigid_zone_index is not None:
            self.overviewRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        point = QPointF(event.pos())
        card_zone_index = self._card_at(point)
        if card_zone_index is not None:
            self.selectionRequested.emit(card_zone_index, ("zone", "summary"))
            event.accept()
            return
        for rect, zone_index, meta in reversed(self._hit_regions):
            if rect.contains(point):
                self.selectionRequested.emit(zone_index, meta)
                if meta[0] == "zone":
                    event.accept()
                    return
                self.contextMenuRequested.emit(zone_index, meta, event.globalPos())
                event.accept()
                return
        event.ignore()

    def _display_unit(self) -> str:
        if 0 <= self.active_zone_index < len(self.zone_editors):
            return self.zone_editors[self.active_zone_index].display_unit
        if self.rigid_editor is not None:
            return self.rigid_editor.display_unit
        return "mm"

    def _zone_thickness_summary(self) -> str:
        display_unit = total_unit(self._display_unit())
        rigid_number = 0
        flex_number = 0
        summaries: list[str] = []

        for editor in self.zone_editors:
            if editor.is_flex_zone:
                flex_number += 1
                zone_name = f"Flex {flex_number} total"
            else:
                rigid_number += 1
                zone_name = f"Rigid {rigid_number}"

            thickness_mm = editor.stackup.total_thickness_mm(editor.catalog)
            thickness = format_compact_thickness(thickness_mm, display_unit)
            summaries.append(f"{zone_name}: {thickness}")

        return " | ".join(summaries)

    def _shared_bounds(
        self,
        rigid_stackup: Stackup,
        flex_stackup: Stackup,
        *,
        slot_capacity: int | None = None,
    ) -> tuple[int, int] | None:
        try:
            if slot_capacity is None:
                return rigid_shared_region_bounds(rigid_stackup, flex_stackup)
            return rigid_shared_region_bounds_for_capacity(rigid_stackup, slot_capacity)
        except ValueError:
            return None

    def _rigid_index_for_flex_layer(
        self,
        rigid_stackup: Stackup,
        flex_stackup: Stackup,
        layer_index: int,
        *,
        slot_map: dict[int, int] | None = None,
        covered_slots: set[int] | None = None,
        slot_gaps: dict[int, int] | None = None,
    ) -> int | None:
        global_slot_id = flex_stackup.flex_slot_for_layer_index(layer_index)
        if covered_slots is not None and global_slot_id not in covered_slots:
            return None
        explicit_gap = (slot_gaps or {}).get(global_slot_id)
        if explicit_gap is not None:
            copper_indices = [
                index
                for index, layer in enumerate(rigid_stackup.layers)
                if isinstance(layer, CopperLayer)
            ]
            if not 0 <= explicit_gap < len(copper_indices) - 1:
                return None
            top_index = copper_indices[explicit_gap]
            bottom_index = copper_indices[explicit_gap + 1]
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
        local_slot_id = (slot_map or {}).get(global_slot_id, global_slot_id)
        slot_capacity = max((slot_map or {}).values(), default=-1) + 1
        if slot_capacity <= 0:
            slot_capacity = flex_stackup.flex_slot_capacity_or_count()
        top_index, bottom_index = rigid_slot_copper_indices(
            rigid_stackup,
            slot_capacity,
            local_slot_id,
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
        rigid_depths = self._rigid_visual_depths()
        maximum_flex_depth = max(1, max(rigid_depths.values(), default=0))
        for flex_zone_index in self.flex_zone_indices:
            parent_index = self.flex_parent_rigid_by_zone.get(flex_zone_index)
            if parent_index is not None:
                maximum_flex_depth = max(
                    maximum_flex_depth,
                    rigid_depths.get(parent_index, 0) + 1,
                )

        rigid_column_count = max(rigid_depths.values(), default=0) + 1
        flex_branch_count = max(1, maximum_flex_depth)
        flex_weights = {zone_index: 0.72 for zone_index in self.flex_zone_indices}
        flex_weight = next(iter(flex_weights.values()), 0.72)
        total_weight = (rigid_column_count * rigid_weight) + (flex_branch_count * flex_weight)
        unit_width = usable_width / max(1.0, total_weight)
        rigid_width = max(42.0, unit_width * rigid_weight)
        flex_widths = {zone_index: max(26.0, unit_width * weight) for zone_index, weight in flex_weights.items()}
        return left_label_width, left_margin, rigid_width, flex_widths

    def _rigid_visual_depths(self) -> dict[int, int]:
        if not self.rigid_zone_indices:
            return {}
        primary_rigid_index = self.rigid_zone_indices[0]
        child_rigid_indices = {
            child_index
            for child_indices in self.flex_child_rigids_by_zone.values()
            for child_index in child_indices
        }
        rigid_depths: dict[int, int] = {primary_rigid_index: 0}
        global_lanes = self._branch_lane_assignments()
        for rigid_zone_index in self.rigid_zone_indices[1:]:
            if rigid_zone_index not in child_rigid_indices:
                rigid_depths[rigid_zone_index] = global_lanes.get(rigid_zone_index, 0) + 1

        pending_flex_indices = set(self.flex_zone_indices)
        while pending_flex_indices:
            progressed = False
            for flex_zone_index in list(pending_flex_indices):
                parent_index = self.flex_parent_rigid_by_zone.get(
                    flex_zone_index,
                    primary_rigid_index,
                )
                parent_depth = rigid_depths.get(parent_index)
                if parent_depth is None:
                    continue
                children = self.flex_child_rigids_by_zone.get(flex_zone_index, [])
                if children:
                    lane_assignments = self._branch_lane_assignments(children)
                    for child_index in children:
                        child_depth = parent_depth + lane_assignments.get(child_index, 0) + 1
                        rigid_depths[child_index] = max(
                            rigid_depths.get(child_index, 0),
                            child_depth,
                        )
                pending_flex_indices.remove(flex_zone_index)
                progressed = True
            if not progressed:
                break
        return rigid_depths

    def _branch_lane_assignments(
        self,
        rigid_zone_indices: list[int] | None = None,
    ) -> dict[int, int]:
        """Pack non-overlapping global layer spans into the same visual column."""
        intervals: list[tuple[int, int, int]] = []
        unnumbered: list[int] = []
        target_indices = self.rigid_zone_indices[1:] if rigid_zone_indices is None else rigid_zone_indices
        for zone_index in target_indices:
            numbers = self.branch_global_numbers_by_zone.get(zone_index, [])
            if numbers:
                intervals.append((min(numbers), max(numbers), zone_index))
            else:
                unnumbered.append(zone_index)

        assignments: dict[int, int] = {}
        lane_ends: list[int] = []
        for start, end, zone_index in sorted(intervals):
            lane_index = next(
                (index for index, lane_end in enumerate(lane_ends) if lane_end < start),
                len(lane_ends),
            )
            if lane_index == len(lane_ends):
                lane_ends.append(end)
            else:
                lane_ends[lane_index] = end
            assignments[zone_index] = lane_index

        for zone_index in unnumbered:
            assignments[zone_index] = len(lane_ends)
            lane_ends.append(2**31 - 1)
        return assignments

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
            if is_no_flow_prepreg_type(layer.dielectric_type):
                return "Rigid PP-No Flow"
            if is_dummy_core_type(layer.dielectric_type):
                return "Rigid Dummy Core"
            if is_etched_core_type(layer.dielectric_type):
                return "Rigid Etched Core"
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
        if isinstance(layer, DielectricLayer) and is_dummy_core_type(
            layer.dielectric_type
        ):
            return (
                QColor(self.palette_map["dummy_core"]),
                QColor(self.palette_map["dummy_core_outline"]),
                QColor("#211206"),
            )
        if isinstance(layer, DielectricLayer) and is_etched_core_type(
            layer.dielectric_type
        ):
            return (
                QColor(self.palette_map["etched_core"]),
                QColor(self.palette_map["etched_core_outline"]),
                QColor("#211506"),
            )
        if isinstance(layer, DielectricLayer) and layer.dielectric_type == "core":
            return (
                QColor(self.palette_map["core"]),
                QColor(self.palette_map["core_outline"]),
                QColor("#1f1e12"),
            )
        if isinstance(layer, DielectricLayer) and is_no_flow_prepreg_type(layer.dielectric_type):
            return (
                QColor(self.palette_map["no_flow_prepreg"]),
                QColor(self.palette_map["no_flow_prepreg_outline"]),
                QColor("#fff2e6"),
            )
        return (
            QColor(self.palette_map["prepreg"]),
            QColor(self.palette_map["prepreg_outline"]),
            QColor("#1f1d17"),
        )

    def _linked_flex_indices(self, rigid_zone_index: int) -> list[int]:
        return [
            flex_zone_index
            for flex_zone_index in self.flex_zone_indices
            if self.flex_parent_rigid_by_zone.get(flex_zone_index) == rigid_zone_index
            or rigid_zone_index in self.flex_child_rigids_by_zone.get(flex_zone_index, [])
        ]

    def _rigid_copper_labels(
        self,
        rigid_zone_index: int,
        editor: StackupEditorWindow,
    ) -> dict[int, str]:
        copper_indices = [
            index for index, layer in enumerate(editor.stackup.layers) if isinstance(layer, CopperLayer)
        ]
        global_numbers = self.branch_global_numbers_by_zone.get(rigid_zone_index, [])
        if len(global_numbers) == len(copper_indices):
            return {
                layer_index: f"L{global_number}"
                for layer_index, global_number in zip(copper_indices, global_numbers)
            }
        return {
            layer_index: editor._copper_label(layer_index)
            for layer_index in copper_indices
        }

    def _paint_focused_view(
        self,
        painter: QPainter,
        *,
        width: float,
        height: float,
        title_font: QFont,
        body_bold_font: QFont,
        summary_font: QFont,
    ) -> None:
        zone_index = self.focused_rigid_zone_index
        if zone_index is None or zone_index not in self.rigid_zone_indices:
            return
        editor = self.zone_editors[zone_index]
        stack = editor.stackup
        zone_name = self.zone_display_names.get(zone_index, f"Rigid Part {zone_index + 1}")

        painter.setPen(QColor(self.palette_map["text"]))
        painter.setFont(title_font)
        painter.drawText(QRectF(18, 12, max(120.0, width - 210), 28), f"{zone_name} - Detail View")

        collapse_rect = QRectF(max(18.0, width - 176.0), 12, 158, 28)
        painter.setPen(QPen(QColor(self.palette_map["accent"]), 1))
        painter.setBrush(QColor("#10283c"))
        painter.drawRoundedRect(collapse_rect, 5, 5)
        self._draw_single_line(
            painter,
            collapse_rect.adjusted(8, 2, -8, -2),
            "Shrink to Overview",
            color=QColor(self.palette_map["text"]),
            font=summary_font,
            min_point_size=6.0,
        )
        self._preview_action_regions.append((QRectF(collapse_rect), "overview", None))

        display_unit = total_unit(self._display_unit())
        thickness = format_compact_thickness(stack.total_thickness_mm(editor.catalog), display_unit)
        painter.setPen(QColor(self.palette_map["muted"]))
        painter.setFont(summary_font)
        painter.drawText(QRectF(18, 43, width - 36, 20), f"{stack.copper_count()} layers | {thickness} | Esc returns to overview")

        grid_top = 72.0
        bottom_margin = 30.0
        usable_height = max(260.0, height - grid_top - bottom_margin)
        painter.setPen(QPen(QColor(self.palette_map["grid"]), 1))
        for x in range(22, int(width), 48):
            painter.drawLine(x, int(grid_top), x - 14, int(height - 20))
        for y in range(int(grid_top + 18), int(height - 16), 48):
            painter.drawLine(18, y, int(width - 18), y)

        side_room = max(92.0, min(230.0, width * 0.25))
        stack_width = max(150.0, min(270.0, width - (side_room * 2) - 70.0))
        stack_x = (width - stack_width) / 2.0
        visual_layers: list[tuple[int | None, object]] = [
            (None, "soldermask"),
            *[(index, layer) for index, layer in enumerate(stack.layers)],
            (None, "soldermask"),
        ]
        heights = self._scaled_heights_from_structure(
            [layer for _index, layer in visual_layers],
            usable_height,
        )
        rigid_rects: dict[int, QRectF] = {}
        linked_flex_indices = self._linked_flex_indices(zone_index)
        mapped_flex_layers: dict[int, dict[int, int]] = {}
        mapped_rigid_indices: set[int] = set()
        for flex_zone_index in linked_flex_indices:
            flex_editor = self.zone_editors[flex_zone_index]
            coverage = set(
                self.branch_coverage_by_zone.get(
                    zone_index,
                    flex_editor.stackup.active_flex_slot_ids(),
                )
            )
            slot_map = dict(self.branch_slot_maps_by_zone.get(zone_index, {}))
            mapping: dict[int, int] = {}
            for flex_layer_index in range(len(flex_editor.stackup.layers)):
                try:
                    rigid_layer_index = self._rigid_index_for_flex_layer(
                        stack,
                        flex_editor.stackup,
                        flex_layer_index,
                        slot_map=slot_map,
                        covered_slots=coverage,
                        slot_gaps=self.branch_slot_gaps_by_zone.get(zone_index),
                    )
                except ValueError:
                    rigid_layer_index = None
                if rigid_layer_index is not None:
                    mapping[rigid_layer_index] = flex_layer_index
                    mapped_rigid_indices.add(rigid_layer_index)
            mapped_flex_layers[flex_zone_index] = mapping

        selected_rigid_index = self._zone_selected_layer_index(zone_index)
        copper_labels = self._rigid_copper_labels(zone_index, editor)
        cursor_y = grid_top
        for visual_row, ((layer_index, layer), row_height) in enumerate(zip(visual_layers, heights)):
            rect = QRectF(stack_x, cursor_y, stack_width, row_height)
            role = (
                "rigid_copper"
                if isinstance(layer, CopperLayer) and layer_index not in mapped_rigid_indices
                else None
            )
            fill, outline, text_color = self._layer_colors(layer, role=role)
            highlight = (
                layer_index is not None
                and self.active_zone_index == zone_index
                and selected_rigid_index == layer_index
            )
            if layer_index is not None:
                for flex_zone_index, mapping in mapped_flex_layers.items():
                    if self.active_zone_index != flex_zone_index:
                        continue
                    selected_flex_index = self._zone_selected_layer_index(flex_zone_index)
                    if selected_flex_index is not None and mapping.get(layer_index) == selected_flex_index:
                        highlight = True
            painter.setPen(QPen(QColor(self.palette_map["accent"]) if highlight else outline, 2 if highlight else 1))
            painter.setBrush(fill)
            painter.drawRect(rect)
            dielectric_text = self._dielectric_rectangle_text(layer)
            if dielectric_text:
                self._draw_single_line(
                    painter,
                    rect.adjusted(5, 1, -5, -1),
                    dielectric_text,
                    color=text_color,
                    font=body_bold_font,
                    min_point_size=5.0,
                )
            if layer_index is not None:
                rigid_rects[layer_index] = QRectF(rect)
                self._add_hit_region(rect, zone_index, ("layer", layer_index))
                if isinstance(layer, CopperLayer):
                    self._draw_single_line(
                        painter,
                        QRectF(stack_x - 56, cursor_y, 48, row_height),
                        copper_labels.get(layer_index, ""),
                        color=QColor(self.palette_map["text"]),
                        font=body_bold_font,
                        min_point_size=5.0,
                        alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    )
            else:
                soldermask_position = "top" if visual_row == 0 else "bottom"
                self._add_hit_region(rect, zone_index, ("soldermask", soldermask_position))
            cursor_y += row_height

        self._draw_single_line(
            painter,
            QRectF(stack_x, grid_top - 24, stack_width, 20),
            zone_name,
            color=QColor(self.palette_map["text"]),
            font=body_bold_font,
            min_point_size=6.0,
        )

        def draw_connected_flex(flex_zone_index: int, *, incoming: bool) -> None:
            flex_editor = self.zone_editors[flex_zone_index]
            flex_stackup = flex_editor.stackup
            if flex_stackup.coverlay is None:
                return
            coverage = set(
                self.branch_coverage_by_zone.get(
                    zone_index,
                    flex_stackup.active_flex_slot_ids(),
                )
            )
            slot_map = dict(self.branch_slot_maps_by_zone.get(zone_index, {}))
            flex_width = max(72.0, min(210.0, side_room - 34.0))
            bridge_x = stack_x - flex_width if incoming else stack_x + stack_width
            selected_meta = self._zone_selected_meta(flex_zone_index)
            for slot_id in sorted(flex_stackup.active_flex_slot_ids() & coverage):
                layer_indices = [
                    index
                    for index in range(len(flex_stackup.layers))
                    if flex_stackup.flex_slot_for_layer_index(index) == slot_id
                ]
                mapped: list[tuple[int, int, QRectF]] = []
                for flex_layer_index in layer_indices:
                    try:
                        rigid_layer_index = self._rigid_index_for_flex_layer(
                            stack,
                            flex_stackup,
                            flex_layer_index,
                            slot_map=slot_map,
                            covered_slots=coverage,
                            slot_gaps=self.branch_slot_gaps_by_zone.get(zone_index),
                        )
                    except ValueError:
                        rigid_layer_index = None
                    if rigid_layer_index is None or rigid_layer_index not in rigid_rects:
                        continue
                    mapped.append((flex_layer_index, rigid_layer_index, rigid_rects[rigid_layer_index]))
                if not mapped:
                    continue
                top_y = min(rect.top() for _flex_index, _rigid_index, rect in mapped)
                bottom_y = max(rect.bottom() for _flex_index, _rigid_index, rect in mapped)
                copper_height = sum(
                    rect.height()
                    for flex_layer_index, _rigid_index, rect in mapped
                    if isinstance(flex_stackup.layers[flex_layer_index], CopperLayer)
                ) / max(
                    1,
                    sum(
                        1
                        for flex_layer_index, _rigid_index, _rect in mapped
                        if isinstance(flex_stackup.layers[flex_layer_index], CopperLayer)
                    ),
                )
                coverlay_height = max(2.0, copper_height * 0.20)
                adhesive_height = max(2.0, copper_height * 0.24)
                component_rects: list[tuple[QRectF, str, tuple[str, int | str]]] = [
                    (
                        QRectF(bridge_x, top_y - coverlay_height - adhesive_height, flex_width, coverlay_height),
                        "coverlay",
                        ("coverlay", f"coverlay_{slot_id}_top_pi"),
                    ),
                    (
                        QRectF(bridge_x, top_y - adhesive_height, flex_width, adhesive_height),
                        "adhesive",
                        ("coverlay", f"coverlay_{slot_id}_top_adhesive"),
                    ),
                ]
                for flex_layer_index, _rigid_layer_index, rigid_rect in mapped:
                    component_rects.append(
                        (
                            QRectF(bridge_x, rigid_rect.top(), flex_width, rigid_rect.height()),
                            "layer",
                            ("layer", flex_layer_index),
                        )
                    )
                component_rects.extend(
                    [
                        (
                            QRectF(bridge_x, bottom_y, flex_width, adhesive_height),
                            "adhesive",
                            ("coverlay", f"coverlay_{slot_id}_bottom_adhesive"),
                        ),
                        (
                            QRectF(bridge_x, bottom_y + adhesive_height, flex_width, coverlay_height),
                            "coverlay",
                            ("coverlay", f"coverlay_{slot_id}_bottom_pi"),
                        ),
                    ]
                )
                for component_rect, role, meta in component_rects:
                    if role == "coverlay":
                        fill, outline, _text_color = self._layer_colors(None, role="coverlay")
                    elif role == "adhesive":
                        fill, outline, _text_color = self._layer_colors(None, role="adhesive")
                    else:
                        flex_layer = flex_stackup.layers[int(meta[1])]
                        fill, outline, _text_color = self._layer_colors(flex_layer)
                    highlight = self.active_zone_index == flex_zone_index and selected_meta == meta
                    painter.setPen(QPen(QColor(self.palette_map["accent"]) if highlight else outline, 2 if highlight else 1))
                    painter.setBrush(fill)
                    painter.drawRect(component_rect)
                    self._add_hit_region(component_rect, flex_zone_index, meta)

                copper_mapped = [
                    (flex_layer_index, rigid_layer_index)
                    for flex_layer_index, rigid_layer_index, _rect in mapped
                    if isinstance(flex_stackup.layers[flex_layer_index], CopperLayer)
                ]
                top_label = copper_labels.get(copper_mapped[0][1], "") if copper_mapped else ""
                bottom_label = copper_labels.get(copper_mapped[-1][1], "") if copper_mapped else ""
                port_text = f"{top_label}-{bottom_label}" if top_label and bottom_label else f"Slot {slot_id + 1}"
                port_center_y = (top_y + bottom_y) / 2.0
                port_x = stack_x - 28 if incoming else stack_x + stack_width - 2
                port_rect = QRectF(port_x, port_center_y - 11, 30, 22)
                painter.setPen(QPen(QColor(self.palette_map["accent"]), 1))
                painter.setBrush(QColor("#10283c"))
                painter.drawRoundedRect(port_rect, 4, 4)
                self._draw_single_line(
                    painter,
                    port_rect.adjusted(2, 1, -2, -1),
                    port_text,
                    color=QColor(self.palette_map["text"]),
                    font=body_bold_font,
                    min_point_size=4.5,
                )
                flex_name = self.zone_display_names.get(flex_zone_index, f"Flex Part {flex_zone_index + 1}")
                self._draw_single_line(
                    painter,
                    QRectF(bridge_x, top_y - coverlay_height - adhesive_height - 20, flex_width, 18),
                    f"{flex_name} - {port_text}",
                    color=QColor(self.palette_map["muted"]),
                    font=body_bold_font,
                    min_point_size=5.0,
                )

        for flex_zone_index in linked_flex_indices:
            is_incoming = zone_index in self.flex_child_rigids_by_zone.get(flex_zone_index, [])
            is_outgoing = self.flex_parent_rigid_by_zone.get(flex_zone_index) == zone_index
            if is_incoming:
                draw_connected_flex(flex_zone_index, incoming=True)
            if is_outgoing:
                draw_connected_flex(flex_zone_index, incoming=False)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.fillRect(self.rect(), QColor(self.palette_map["bg"]))
            self._hit_regions.clear()
            self._card_regions.clear()
            self._preview_action_regions.clear()

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

            if self.focused_rigid_zone_index is not None:
                self._paint_focused_view(
                    painter,
                    width=width,
                    height=height,
                    title_font=title_font,
                    body_bold_font=body_bold_font,
                    summary_font=summary_font,
                )
                return

            painter.setPen(QColor(self.palette_map["text"]))
            painter.setFont(title_font)
            painter.drawText(QRectF(18, 12, width - 36, 28), "Rigid-Flex Live Stackup")
            painter.setPen(QColor(self.palette_map["muted"]))
            painter.setFont(summary_font)
            summary = self._zone_thickness_summary()
            summary_flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap
            summary_bounds = QFontMetrics(summary_font).boundingRect(
                0,
                0,
                width - 36,
                1000,
                int(summary_flags),
                summary,
            )
            summary_height = max(22, summary_bounds.height() + 2)
            painter.drawText(QRectF(18, 40, width - 36, summary_height), summary, summary_flags)

            grid_top = int(40 + summary_height + 14)

            grid_pen = QPen(QColor(self.palette_map["grid"]), 1)
            painter.setPen(grid_pen)
            for x in range(20, width, 48):
                painter.drawLine(x, grid_top, x - 16, height - 26)
            for y in range(grid_top + 20, height - 18, 48):
                painter.drawLine(18, y, width - 18, y)

            top_margin = float(grid_top + 10)
            bottom_margin = 36.0
            usable_height = max(220.0, height - top_margin - bottom_margin)
            left_label_width, rigid_x0, rigid_width, flex_widths = self._compute_span_layout(width)
            primary_visual_layers: list[object] = [
                "soldermask",
                *primary_rigid_editor.stackup.layers,
                "soldermask",
            ]
            primary_structure_weight = sum(
                self._structural_layer_weight(layer) for layer in primary_visual_layers
            )
            structure_unit_px = usable_height / max(1.0, primary_structure_weight)
            card_port_centers: dict[tuple[int, int, str], float] = {}
            primary_global_copper_centers: dict[int, float] = {}
            rigid_visual_depths = self._rigid_visual_depths()
            rigid_card_count_by_depth: dict[int, int] = {}
            for rigid_zone_index in self.rigid_zone_indices[1:]:
                depth = rigid_visual_depths.get(rigid_zone_index, 1)
                rigid_card_count_by_depth[depth] = rigid_card_count_by_depth.get(depth, 0) + 1

            def branch_slot_map(rigid_zone_index: int) -> dict[int, int]:
                return dict(self.branch_slot_maps_by_zone.get(rigid_zone_index, {}))

            def branch_coverage(rigid_zone_index: int, flex_editor: StackupEditorWindow) -> set[int]:
                return set(
                    self.branch_coverage_by_zone.get(
                        rigid_zone_index,
                        flex_editor.stackup.active_flex_slot_ids(),
                    )
                )

            def rigid_flex_contexts_for_zone(
                rigid_zone_index: int,
            ) -> list[tuple[int, StackupEditorWindow, tuple[int, int]]]:
                contexts: list[tuple[int, StackupEditorWindow, tuple[int, int]]] = []
                rigid_editor = self.zone_editors[rigid_zone_index]
                linked_flex_indices = [
                    candidate_index
                    for candidate_index in self.flex_zone_indices
                    if self.flex_parent_rigid_by_zone.get(candidate_index) == rigid_zone_index
                    or rigid_zone_index in self.flex_child_rigids_by_zone.get(candidate_index, [])
                ]
                for candidate_index in linked_flex_indices:
                    candidate_editor = self.zone_editors[candidate_index]
                    slot_map = branch_slot_map(rigid_zone_index)
                    capacity = max(slot_map.values(), default=-1) + 1
                    bounds = self._shared_bounds(
                        rigid_editor.stackup,
                        candidate_editor.stackup,
                        slot_capacity=capacity if capacity > 0 else None,
                    )
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
                align_layer_index: int | None = None,
                align_layer_top_to: float | None = None,
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
                heights_local = [
                    self._structural_layer_weight(layer) * structure_unit_px
                    for _index, layer, _thickness in visuals
                ]
                y_positions: list[float] = []
                cursor = top_margin
                for value in heights_local:
                    y_positions.append(cursor)
                    cursor += value

                shared_bounds_by_flex: dict[int, tuple[int, int]] = {}
                shared_layer_maps: dict[int, dict[int, int]] = {}
                for flex_zone_index, flex_editor, bounds in linked_flex_contexts or []:
                    shared_bounds_by_flex[flex_zone_index] = bounds
                    slot_map = branch_slot_map(zone_index)
                    covered_slots = branch_coverage(zone_index, flex_editor)
                    shared_layer_maps[flex_zone_index] = {}
                    for flex_layer_index in range(len(flex_editor.stackup.layers)):
                        rigid_layer_index = self._rigid_index_for_flex_layer(
                            stack,
                            flex_editor.stackup,
                            flex_layer_index,
                            slot_map=slot_map,
                            covered_slots=covered_slots,
                            slot_gaps=self.branch_slot_gaps_by_zone.get(zone_index),
                        )
                        if rigid_layer_index is not None:
                            shared_layer_maps[flex_zone_index][rigid_layer_index] = flex_layer_index
                shift_y = 0.0
                if (
                    align_layer_index is not None
                    and align_layer_top_to is not None
                    and 0 <= align_layer_index < len(stack.layers)
                ):
                    shift_y = align_layer_top_to - y_positions[align_layer_index + 1]
                elif (
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
                if zone_index != self.rigid_zone_indices[0]:
                    for row_no, (visual, pixel_height) in enumerate(zip(visuals, heights_local)):
                        index, _layer, _thickness_mm = visual
                        if index is None:
                            continue
                        rects_local[index] = QRectF(
                            x0,
                            y_positions[row_no] + shift_y,
                            stack_width_px,
                            pixel_height,
                        )

                    port_spans: list[tuple[int, float, float, str]] = []
                    port_reference_centers: dict[int, float] = {}
                    copper_labels = self._rigid_copper_labels(zone_index, editor)
                    rigid_copper_indices = [
                        index
                        for index, layer in enumerate(stack.layers)
                        if isinstance(layer, CopperLayer)
                    ]
                    rigid_global_numbers = self.branch_global_numbers_by_zone.get(
                        zone_index,
                        [],
                    )
                    global_number_by_rigid_index = dict(
                        zip(rigid_copper_indices, rigid_global_numbers)
                    )
                    for flex_zone_index, shared_layer_map in shared_layer_maps.items():
                        mapped_indices = sorted(
                            (
                                rigid_index
                                for rigid_index in shared_layer_map
                                if rigid_index in rects_local
                            ),
                            key=lambda rigid_index: rects_local[rigid_index].top(),
                        )
                        if not mapped_indices:
                            continue
                        mapped_copper_indices = [
                            rigid_index
                            for rigid_index in mapped_indices
                            if isinstance(stack.layers[rigid_index], CopperLayer)
                        ]
                        if mapped_copper_indices:
                            top_label = copper_labels.get(mapped_copper_indices[0], "")
                            bottom_label = copper_labels.get(mapped_copper_indices[-1], "")
                            port_label = (
                                f"{top_label}-{bottom_label}"
                                if top_label and bottom_label and top_label != bottom_label
                                else top_label or bottom_label
                            )
                        else:
                            port_label = self.zone_display_names.get(
                                flex_zone_index,
                                f"Flex {flex_zone_index + 1}",
                            )
                        span_top = min(rects_local[index].top() for index in mapped_indices)
                        span_bottom = max(rects_local[index].bottom() for index in mapped_indices)
                        port_spans.append((flex_zone_index, span_top, span_bottom, port_label))
                        reference_centers = [
                            primary_global_copper_centers[global_number]
                            for rigid_index in mapped_copper_indices
                            if (
                                (global_number := global_number_by_rigid_index.get(rigid_index))
                                in primary_global_copper_centers
                            )
                        ]
                        if reference_centers:
                            port_reference_centers[flex_zone_index] = (
                                min(reference_centers) + max(reference_centers)
                            ) / 2.0

                    incoming_port_spans = [
                        port_span
                        for port_span in port_spans
                        if zone_index
                        in self.flex_child_rigids_by_zone.get(port_span[0], [])
                    ]
                    outgoing_port_spans = [
                        port_span
                        for port_span in port_spans
                        if self.flex_parent_rigid_by_zone.get(port_span[0]) == zone_index
                    ]
                    incoming_port_spans.sort(key=lambda item: ((item[1] + item[2]) / 2.0, item[0]))
                    outgoing_port_spans.sort(key=lambda item: ((item[1] + item[2]) / 2.0, item[0]))
                    row_count = max(1, len(incoming_port_spans), len(outgoing_port_spans))
                    card_height = 100.0 + (row_count * 34.0)
                    anchor_ports = port_spans
                    visual_depth = rigid_visual_depths.get(zone_index, 1)
                    cards_in_visual_zone = rigid_card_count_by_depth.get(visual_depth, 1)
                    if cards_in_visual_zone == 1:
                        anchor_center = top_margin + (usable_height / 2.0)
                    elif anchor_ports:
                        connected_centers = [
                            port_reference_centers.get(
                                flex_zone_index,
                                (span_top + span_bottom) / 2.0,
                            )
                            for flex_zone_index, span_top, span_bottom, _label in anchor_ports
                        ]
                        anchor_center = (
                            min(connected_centers) + max(connected_centers)
                        ) / 2.0
                    elif rects_local:
                        anchor_center = (
                            min(rect.top() for rect in rects_local.values())
                            + max(rect.bottom() for rect in rects_local.values())
                        ) / 2.0
                    else:
                        anchor_center = top_margin + (usable_height / 2.0)
                    card_top = anchor_center - (card_height / 2.0)
                    card_top = max(
                        top_margin + 6.0,
                        min(card_top, height - bottom_margin - card_height),
                    )
                    card_rect = QRectF(
                        x0,
                        card_top,
                        stack_width_px,
                        card_height,
                    )
                    active_card = self.active_zone_index == zone_index
                    card_fill = QColor("#11283b")
                    card_fill.setAlpha(224 if active_card else 190)
                    card_outline = QColor(self.palette_map["accent"])
                    painter.setPen(QPen(card_outline, 2 if active_card else 1))
                    painter.setBrush(card_fill)
                    painter.drawRoundedRect(card_rect, 7, 7)
                    self._card_regions.append((QRectF(card_rect), zone_index))

                    expand_rect = QRectF(card_rect.right() - 31, card_rect.top() + 8, 22, 22)
                    painter.setPen(QPen(card_outline, 1.4))
                    corner = 5.0
                    painter.drawLine(expand_rect.left(), expand_rect.top() + corner, expand_rect.left(), expand_rect.top())
                    painter.drawLine(expand_rect.left(), expand_rect.top(), expand_rect.left() + corner, expand_rect.top())
                    painter.drawLine(expand_rect.right() - corner, expand_rect.top(), expand_rect.right(), expand_rect.top())
                    painter.drawLine(expand_rect.right(), expand_rect.top(), expand_rect.right(), expand_rect.top() + corner)
                    painter.drawLine(expand_rect.left(), expand_rect.bottom() - corner, expand_rect.left(), expand_rect.bottom())
                    painter.drawLine(expand_rect.left(), expand_rect.bottom(), expand_rect.left() + corner, expand_rect.bottom())
                    painter.drawLine(expand_rect.right() - corner, expand_rect.bottom(), expand_rect.right(), expand_rect.bottom())
                    painter.drawLine(expand_rect.right(), expand_rect.bottom() - corner, expand_rect.right(), expand_rect.bottom())
                    self._preview_action_regions.append((QRectF(expand_rect), "focus", zone_index))

                    card_name = self.zone_display_names.get(zone_index, f"Rigid Part {zone_index + 1}")
                    self._draw_single_line(
                        painter,
                        QRectF(card_rect.left() + 12, card_rect.top() + 10, card_rect.width() - 50, 22),
                        card_name,
                        color=QColor(self.palette_map["text"]),
                        font=body_bold_font,
                        min_point_size=6.0,
                        alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    )
                    self._draw_single_line(
                        painter,
                        QRectF(card_rect.left() + 12, card_rect.top() + 34, card_rect.width() - 24, 18),
                        f"{stack.copper_count()} layers",
                        color=QColor(self.palette_map["muted"]),
                        font=summary_font,
                        min_point_size=5.0,
                        alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    )

                    tag_width = max(38.0, min(62.0, stack_width_px * 0.43))
                    for side, side_ports in (
                        ("incoming", incoming_port_spans),
                        ("outgoing", outgoing_port_spans),
                    ):
                        tag_x = (
                            card_rect.left() + 7.0
                            if side == "incoming"
                            else card_rect.right() - tag_width - 7.0
                        )
                        for row_index, (flex_zone_index, _span_top, _span_bottom, port_label) in enumerate(side_ports):
                            port_center = card_rect.top() + 84.0 + (row_index * 34.0)
                            card_port_centers[(zone_index, flex_zone_index, side)] = port_center
                            tag_rect = QRectF(tag_x, port_center - 11.0, tag_width, 22.0)
                            painter.setPen(QPen(card_outline, 1))
                            painter.setBrush(QColor("#0d2031"))
                            painter.drawRoundedRect(tag_rect, 4, 4)
                            self._draw_single_line(
                                painter,
                                tag_rect.adjusted(3, 1, -3, -1),
                                port_label,
                                color=QColor(self.palette_map["accent"]),
                                font=body_bold_font,
                                min_point_size=4.5,
                            )
                    return rects_local, shared_bounds_by_flex

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

            def draw_rigid_part_label(
                zone_index: int,
                x0: float,
                rigid_layer_rects: dict[int, QRectF],
            ) -> None:
                if zone_index != self.rigid_zone_indices[0]:
                    return
                label_top = (
                    min(rect.top() for rect in rigid_layer_rects.values())
                    - (self._structural_layer_weight("soldermask") * structure_unit_px)
                    - 20
                    if rigid_layer_rects
                    else top_margin - 22
                )
                self._draw_single_line(
                    painter,
                    QRectF(x0, label_top, rigid_width, 18),
                    self.zone_display_names.get(zone_index, f"Rigid {zone_index + 1}"),
                    color=QColor(self.palette_map["muted"]),
                    font=body_bold_font,
                    min_point_size=5.0,
                )

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
                covered_slots_override: set[int] | None = None,
                existing_right_drawing: tuple[
                    dict[int, QRectF],
                    dict[int, tuple[int, int]],
                ] | None = None,
            ) -> tuple[dict[int, QRectF], dict[int, tuple[int, int]]] | None:
                flex_stackup = flex_editor.stackup
                if flex_stackup.coverlay is None:
                    return None
                coverage_zone_index = (
                    right_rigid_zone_index
                    if right_rigid_zone_index is not None
                    else left_rigid_zone_index
                )
                covered_slots = (
                    set(covered_slots_override)
                    if covered_slots_override is not None
                    else branch_coverage(coverage_zone_index, flex_editor)
                )
                left_slot_map = branch_slot_map(left_rigid_zone_index)
                left_coverage = branch_coverage(left_rigid_zone_index, flex_editor)
                branch_layer_rects: dict[int, QRectF] = {}
                for layer_index in range(len(flex_stackup.layers)):
                    if flex_stackup.flex_slot_for_layer_index(layer_index) not in covered_slots:
                        continue
                    rigid_layer_index = self._rigid_index_for_flex_layer(
                        left_rigid_editor.stackup,
                        flex_stackup,
                        layer_index,
                        slot_map=left_slot_map,
                        covered_slots=left_coverage,
                        slot_gaps=self.branch_slot_gaps_by_zone.get(left_rigid_zone_index),
                    )
                    if rigid_layer_index is not None and rigid_layer_index in left_rigid_rects:
                        branch_layer_rects[layer_index] = left_rigid_rects[rigid_layer_index]
                selected_layer_indices = sorted(branch_layer_rects)
                if not selected_layer_indices:
                    return None

                top_rect = branch_layer_rects[selected_layer_indices[0]]
                bottom_rect = branch_layer_rects[selected_layer_indices[-1]]
                copper_rect_heights = [
                    branch_layer_rects[index].height()
                    for index in selected_layer_indices
                    if isinstance(flex_stackup.layers[index], CopperLayer)
                ]
                reference_copper_px = (
                    sum(copper_rect_heights) / len(copper_rect_heights)
                    if copper_rect_heights
                    else 24.0
                )
                coverlay_pi_px = max(2.0, reference_copper_px * 0.18)
                adhesive_px = max(2.0, reference_copper_px * 0.28)
                minimum_gap_px = max(2.0, reference_copper_px * 0.18)
                sandwich_slots = [
                    slot_id
                    for slot_id in flex_stackup.flex_sandwich_slot_ids()
                    if slot_id in covered_slots
                ]
                if not sandwich_slots:
                    return None
                first_slot = sandwich_slots[0]
                last_slot = sandwich_slots[-1]

                if len(self.rigid_zone_indices) > 1:
                    right_x0 = branch_x0 + branch_width
                    drawn: tuple[dict[int, QRectF], dict[int, tuple[int, int]]] | None = None
                    if right_rigid_zone_index is not None and right_rigid_editor is not None:
                        selected_right_rigid_index = self._zone_selected_layer_index(right_rigid_zone_index)
                        if existing_right_drawing is not None:
                            drawn = existing_right_drawing
                        else:
                            drawn = draw_rigid_stack(
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

                    copper_labels = self._rigid_copper_labels(
                        left_rigid_zone_index,
                        left_rigid_editor,
                    )
                    mapped_copper_labels: list[str] = []
                    for flex_layer_index in selected_layer_indices:
                        if not isinstance(flex_stackup.layers[flex_layer_index], CopperLayer):
                            continue
                        rigid_layer_index = self._rigid_index_for_flex_layer(
                            left_rigid_editor.stackup,
                            flex_stackup,
                            flex_layer_index,
                            slot_map=left_slot_map,
                            covered_slots=left_coverage,
                            slot_gaps=self.branch_slot_gaps_by_zone.get(left_rigid_zone_index),
                        )
                        if rigid_layer_index is not None:
                            label = copper_labels.get(rigid_layer_index, "")
                            if label:
                                mapped_copper_labels.append(label)
                    if len(mapped_copper_labels) >= 2:
                        port_text = f"{mapped_copper_labels[0]}-{mapped_copper_labels[-1]}"
                    elif mapped_copper_labels:
                        port_text = mapped_copper_labels[0]
                    else:
                        port_text = "Flex"

                    physical_arrow_y = (top_rect.top() + bottom_rect.bottom()) / 2.0
                    source_arrow_y = card_port_centers.get(
                        (left_rigid_zone_index, flex_zone_index, "outgoing"),
                        physical_arrow_y,
                    )
                    target_arrow_y = (
                        card_port_centers.get(
                            (right_rigid_zone_index, flex_zone_index, "incoming"),
                            source_arrow_y,
                        )
                        if right_rigid_zone_index is not None
                        else source_arrow_y
                    )
                    arrow_start_x = branch_x0 + 7.0
                    arrow_tip_x = right_x0 - 2.0
                    arrow_shaft_end = max(arrow_start_x, arrow_tip_x - 12.0)
                    selected_arrow = self.active_zone_index == flex_zone_index
                    arrow_color = QColor(
                        self.palette_map["text"] if selected_arrow else self.palette_map["accent"]
                    )
                    arrow_pen = QPen(arrow_color, 4 if selected_arrow else 3)
                    arrow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                    painter.setPen(arrow_pen)
                    if abs(source_arrow_y - target_arrow_y) < 0.5:
                        painter.drawLine(
                            QPointF(arrow_start_x, source_arrow_y),
                            QPointF(arrow_shaft_end, target_arrow_y),
                        )
                    else:
                        bend_x = (arrow_start_x + arrow_shaft_end) / 2.0
                        painter.drawLine(
                            QPointF(arrow_start_x, source_arrow_y),
                            QPointF(bend_x, source_arrow_y),
                        )
                        painter.drawLine(
                            QPointF(bend_x, source_arrow_y),
                            QPointF(bend_x, target_arrow_y),
                        )
                        painter.drawLine(
                            QPointF(bend_x, target_arrow_y),
                            QPointF(arrow_shaft_end, target_arrow_y),
                        )
                    painter.drawLine(
                        QPointF(arrow_shaft_end, target_arrow_y - 8.0),
                        QPointF(arrow_tip_x, target_arrow_y),
                    )
                    painter.drawLine(
                        QPointF(arrow_shaft_end, target_arrow_y + 8.0),
                        QPointF(arrow_tip_x, target_arrow_y),
                    )
                    flex_name = self.zone_display_names.get(
                        flex_zone_index,
                        f"Flex Part {flex_zone_index + 1}",
                    )
                    label_rect = QRectF(
                        branch_x0,
                        source_arrow_y - 30.0,
                        branch_width,
                        20.0,
                    )
                    self._draw_single_line(
                        painter,
                        label_rect,
                        f"{flex_name} - {port_text}",
                        color=arrow_color,
                        font=body_bold_font,
                        min_point_size=5.0,
                    )
                    self._add_hit_region(
                        QRectF(
                            branch_x0,
                            min(source_arrow_y, target_arrow_y) - 32.0,
                            branch_width,
                            abs(source_arrow_y - target_arrow_y) + 50.0,
                        ),
                        flex_zone_index,
                        ("zone", "summary"),
                    )
                    return drawn

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
                sandwich_layer_indices = {
                    sandwich_slot: [
                        layer_index
                        for layer_index in selected_layer_indices
                        if flex_stackup.flex_slot_for_layer_index(layer_index) == sandwich_slot
                    ]
                    for sandwich_slot in sandwich_slots
                }
                for sandwich_index, sandwich_slot in enumerate(sandwich_slots):
                    current_indices = sandwich_layer_indices[sandwich_slot]
                    for layer_index in current_indices:
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
                        next_indices = sandwich_layer_indices[next_sandwich_slot]
                        current_bottom_rect = branch_layer_rects[current_indices[-1]]
                        next_top_rect = branch_layer_rects[next_indices[0]]
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
                right_slot_map = (
                    branch_slot_map(right_rigid_zone_index)
                    if right_rigid_zone_index is not None
                    else {}
                )
                right_coverage = (
                    branch_coverage(right_rigid_zone_index, flex_editor)
                    if right_rigid_zone_index is not None
                    else set()
                )
                right_capacity = max(right_slot_map.values(), default=-1) + 1
                right_shared_bounds = (
                    self._shared_bounds(
                        right_rigid_editor.stackup,
                        flex_stackup,
                        slot_capacity=right_capacity if right_capacity > 0 else None,
                    )
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
                            self._rigid_index_for_flex_layer(
                                left_rigid_editor.stackup,
                                flex_stackup,
                                layer_index,
                                slot_map=left_slot_map,
                                covered_slots=left_coverage,
                                slot_gaps=self.branch_slot_gaps_by_zone.get(left_rigid_zone_index),
                            ) == selected_left_rigid_index
                        )
                    if (
                        right_rigid_zone_index is not None
                        and self.active_zone_index == right_rigid_zone_index
                        and selected_right_rigid_index is not None
                        and right_shared_bounds is not None
                        and right_shared_bounds[0] <= selected_right_rigid_index <= right_shared_bounds[1]
                    ):
                        return (
                            self._rigid_index_for_flex_layer(
                                right_rigid_editor.stackup,
                                flex_stackup,
                                layer_index,
                                slot_map=right_slot_map,
                                covered_slots=right_coverage,
                                slot_gaps=self.branch_slot_gaps_by_zone.get(right_rigid_zone_index),
                            ) == selected_right_rigid_index
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

                if existing_right_drawing is not None:
                    return existing_right_drawing

                right_x0 = branch_x0 + branch_width
                drawn = draw_rigid_stack(
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
                rigid_layer_rects = drawn[0]
                draw_rigid_part_label(right_rigid_zone_index, right_x0, rigid_layer_rects)
                return drawn

            primary_rigid_zone_index = self.rigid_zone_indices[0]
            primary_rigid_editor = self.zone_editors[primary_rigid_zone_index]
            primary_rigid_rects, primary_shared_bounds_map = draw_rigid_stack(
                primary_rigid_editor,
                zone_index=primary_rigid_zone_index,
                x0=rigid_x0,
                stack_width_px=rigid_width,
                selected_index=self._zone_selected_layer_index(primary_rigid_zone_index),
                show_left_labels=True,
                linked_flex_contexts=rigid_flex_contexts_for_zone(primary_rigid_zone_index),
            )
            primary_global_numbers = self.branch_global_numbers_by_zone.get(
                primary_rigid_zone_index,
                [],
            )
            primary_copper_indices = [
                index
                for index, layer in enumerate(primary_rigid_editor.stackup.layers)
                if isinstance(layer, CopperLayer)
            ]
            primary_global_copper_centers.update(
                {
                    global_number: primary_rigid_rects[layer_index].center().y()
                    for global_number, layer_index in zip(
                        primary_global_numbers,
                        primary_copper_indices,
                    )
                    if layer_index in primary_rigid_rects
                }
            )
            draw_rigid_part_label(primary_rigid_zone_index, rigid_x0, primary_rigid_rects)
            rigid_drawings: dict[
                int,
                tuple[float, dict[int, QRectF], dict[int, tuple[int, int]]],
            ] = {
                primary_rigid_zone_index: (
                    rigid_x0,
                    primary_rigid_rects,
                    primary_shared_bounds_map,
                )
            }

            child_rigid_zone_indices = {
                child_zone_index
                for child_zone_indices in self.flex_child_rigids_by_zone.values()
                for child_zone_index in child_zone_indices
            }
            root_rigid_zone_indices = [
                zone_index
                for zone_index in self.rigid_zone_indices[1:]
                if zone_index not in child_rigid_zone_indices
            ]
            if root_rigid_zone_indices:
                all_lane_assignments = self._branch_lane_assignments()
                branch_gap_width = max(
                    flex_widths.values(),
                    default=max(26.0, rigid_width * 0.45),
                )
                primary_global_numbers = self.branch_global_numbers_by_zone.get(
                    primary_rigid_zone_index,
                    [],
                )
                primary_copper_indices = [
                    index
                    for index, layer in enumerate(primary_rigid_editor.stackup.layers)
                    if isinstance(layer, CopperLayer)
                ]
                primary_index_by_number = dict(
                    zip(primary_global_numbers, primary_copper_indices)
                )
                for root_zone_index in root_rigid_zone_indices:
                    root_editor = self.zone_editors[root_zone_index]
                    root_global_numbers = self.branch_global_numbers_by_zone.get(
                        root_zone_index,
                        [],
                    )
                    root_copper_indices = [
                        index
                        for index, layer in enumerate(root_editor.stackup.layers)
                        if isinstance(layer, CopperLayer)
                    ]
                    root_index_by_number = dict(
                        zip(root_global_numbers, root_copper_indices)
                    )
                    common_number = next(
                        (
                            number
                            for number in root_global_numbers
                            if number in primary_index_by_number
                        ),
                        None,
                    )
                    root_layer_index = (
                        root_index_by_number.get(common_number)
                        if common_number is not None
                        else None
                    )
                    primary_layer_rect = (
                        primary_rigid_rects.get(primary_index_by_number[common_number])
                        if common_number is not None
                        else None
                    )
                    lane_index = all_lane_assignments.get(root_zone_index, 0)
                    root_x0 = (
                        rigid_x0
                        + rigid_width
                        + branch_gap_width
                        + lane_index * (branch_gap_width + rigid_width)
                    )
                    root_drawn = draw_rigid_stack(
                        root_editor,
                        zone_index=root_zone_index,
                        x0=root_x0,
                        stack_width_px=rigid_width,
                        selected_index=self._zone_selected_layer_index(root_zone_index),
                        align_layer_index=root_layer_index,
                        align_layer_top_to=(
                            primary_layer_rect.top()
                            if primary_layer_rect is not None
                            else None
                        ),
                        show_left_labels=False,
                        linked_flex_contexts=rigid_flex_contexts_for_zone(root_zone_index),
                    )
                    rigid_drawings[root_zone_index] = (
                        root_x0,
                        root_drawn[0],
                        root_drawn[1],
                    )
                    draw_rigid_part_label(root_zone_index, root_x0, root_drawn[0])

            pending_flex_indices = list(self.flex_zone_indices)
            while pending_flex_indices:
                progressed = False
                for flex_zone_index in list(pending_flex_indices):
                    parent_zone_index = self.flex_parent_rigid_by_zone.get(
                        flex_zone_index,
                        primary_rigid_zone_index,
                    )
                    parent_drawing = rigid_drawings.get(parent_zone_index)
                    if parent_drawing is None:
                        continue
                    parent_x0, parent_rects, parent_shared_bounds_map = parent_drawing
                    left_shared_bounds = parent_shared_bounds_map.get(flex_zone_index)
                    if left_shared_bounds is None:
                        pending_flex_indices.remove(flex_zone_index)
                        progressed = True
                        continue

                    parent_editor = self.zone_editors[parent_zone_index]
                    flex_editor = self.zone_editors[flex_zone_index]
                    child_zone_indices = self.flex_child_rigids_by_zone.get(flex_zone_index, [])
                    branch_width = flex_widths.get(
                        flex_zone_index,
                        max(26.0, rigid_width * 0.45),
                    )
                    if child_zone_indices:
                        branch_lanes = self._branch_lane_assignments(child_zone_indices)
                        child_covered_slots: set[int] = set()
                        for right_zone_index in child_zone_indices:
                            right_covered_slots = branch_coverage(right_zone_index, flex_editor)
                            child_covered_slots.update(right_covered_slots)
                            lane_index = branch_lanes.get(right_zone_index, 0)
                            branch_x0 = parent_x0 + rigid_width + lane_index * (branch_width + rigid_width)
                            existing_child = rigid_drawings.get(right_zone_index)
                            effective_branch_width = branch_width
                            existing_right_drawing = None
                            if existing_child is not None:
                                existing_x0, existing_rects, existing_bounds = existing_child
                                effective_branch_width = max(10.0, existing_x0 - branch_x0)
                                existing_right_drawing = (existing_rects, existing_bounds)
                            drawn = draw_flex_segment(
                                flex_zone_index=flex_zone_index,
                                flex_editor=flex_editor,
                                left_rigid_zone_index=parent_zone_index,
                                left_rigid_editor=parent_editor,
                                left_rigid_rects=parent_rects,
                                left_shared_bounds=left_shared_bounds,
                                branch_x0=branch_x0,
                                branch_width=effective_branch_width,
                                right_rigid_zone_index=right_zone_index,
                                right_rigid_editor=self.zone_editors[right_zone_index],
                                covered_slots_override=right_covered_slots,
                                existing_right_drawing=existing_right_drawing,
                            )
                            if drawn is not None and existing_child is None:
                                rigid_drawings[right_zone_index] = (
                                    branch_x0 + effective_branch_width,
                                    drawn[0],
                                    drawn[1],
                                )
                        terminal_slots = (
                            flex_editor.stackup.active_flex_slot_ids()
                            - child_covered_slots
                        )
                        if terminal_slots:
                            draw_flex_segment(
                                flex_zone_index=flex_zone_index,
                                flex_editor=flex_editor,
                                left_rigid_zone_index=parent_zone_index,
                                left_rigid_editor=parent_editor,
                                left_rigid_rects=parent_rects,
                                left_shared_bounds=left_shared_bounds,
                                branch_x0=parent_x0 + rigid_width,
                                branch_width=branch_width,
                                covered_slots_override=terminal_slots,
                            )
                    else:
                        draw_flex_segment(
                            flex_zone_index=flex_zone_index,
                            flex_editor=flex_editor,
                            left_rigid_zone_index=parent_zone_index,
                            left_rigid_editor=parent_editor,
                            left_rigid_rects=parent_rects,
                            left_shared_bounds=left_shared_bounds,
                            branch_x0=parent_x0 + rigid_width,
                            branch_width=branch_width,
                        )
                    pending_flex_indices.remove(flex_zone_index)
                    progressed = True
                if not progressed:
                    break
        finally:
            painter.end()


class RigidFlexEditorWindow(QMainWindow):
    newStackupRequested = Signal()

    def __init__(self, root_path: Path) -> None:
        super().__init__()
        self.root_path = root_path
        self.setWindowTitle("StackUp Editor — Rigid-Flex")
        self.resize(1660, 940)
        self.setMinimumSize(1080, 680)

        self._zone_editors: list[StackupEditorWindow] = []
        self._flex_sync_source: StackupEditorWindow | None = None
        self._flex_sandwich_history: dict[int, list[list[Stackup]]] = {}
        self._rigid_branch_coverage: dict[int, set[int] | None] = {}
        self._rigid_branch_slot_maps: dict[int, dict[int, int]] = {}
        self._rigid_branch_global_numbers: dict[int, list[int]] = {}
        self._rigid_flex_gap_by_slot: dict[int, dict[int, int]] = {}
        self._rigid_parent_flexes: dict[int, set[int]] = {}
        self._flex_parent_rigid: dict[int, int] = {}
        self._flex_child_rigids: dict[int, set[int]] = {}
        self._definition_source_by_alias: dict[int, int] = {}
        self._definition_aliases_by_source: dict[int, set[int]] = {}
        self._definition_signal_sources: set[int] = set()
        self._definition_sync_in_progress = False
        self._rigid_sync_in_progress = False
        self._active_visual_editor: StackupEditorWindow | None = None
        self._selected_flex_part_ids: set[int] = set()
        self._preview_selection_in_progress = False
        self._rigid_box_inspection_active = False
        self._overview_splitter_sizes: list[int] | None = None

        self._build_file_menu()

        central = QWidget()
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(12, 10, 12, 10)
        outer_layout.setSpacing(8)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(10)
        outer_layout.addWidget(self.main_splitter, 1)

        zone_panel = QWidget()
        zone_panel_layout = QVBoxLayout(zone_panel)
        zone_panel_layout.setContentsMargins(0, 0, 0, 0)
        zone_panel_layout.setSpacing(2)
        self.zone_toolbar_layout = QHBoxLayout()
        self.zone_toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.zone_toolbar_layout.setSpacing(4)
        self.zone_toolbar_layout.addStretch(1)
        zone_panel_layout.addLayout(self.zone_toolbar_layout)

        self.tabs = QTabWidget()
        self.flex_part_tab_bar = FlexPartTabBar()
        self.tabs.setTabBar(self.flex_part_tab_bar)
        self.flex_part_tab_bar.setExpanding(False)
        self.flex_part_tab_bar.selectionGesture.connect(self._handle_flex_tab_selection_gesture)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        zone_panel_layout.addWidget(self.tabs, 1)
        self.main_splitter.addWidget(zone_panel)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(8)
        self.preview_title_label = QLabel("Rigid-Flex Overview")
        self.preview_title_label.setStyleSheet("font: 700 14px 'Bahnschrift'; color: #edf4fa;")
        preview_layout.addWidget(self.preview_title_label)
        self.combined_preview = RigidFlexCombinedPreview()
        self.combined_preview.selectionRequested.connect(self._handle_combined_preview_selection)
        self.combined_preview.contextMenuRequested.connect(self._handle_combined_preview_context_menu)
        self.combined_preview.focusRequested.connect(self._focus_combined_preview_rigid)
        self.combined_preview.overviewRequested.connect(self._show_combined_preview_overview)
        preview_layout.addWidget(self.combined_preview, 1)
        self.main_splitter.addWidget(preview_panel)
        self.main_splitter.setSizes([1180, 560])

        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 4, 0)
        corner_layout.setSpacing(4)

        self.add_zone_button = QPushButton("Add Rigid Part")
        self.add_zone_button.setFixedSize(122, 26)
        self.add_zone_button.clicked.connect(self._add_rigid_part_interactive)
        corner_layout.addWidget(self.add_zone_button)

        self.add_flex_part_button = QPushButton("Add Flex Part")
        self.add_flex_part_button.setFixedSize(108, 26)
        self.add_flex_part_button.clicked.connect(self._add_flex_part_interactive)
        corner_layout.addWidget(self.add_flex_part_button)

        self.remove_zone_button = QPushButton("Remove Part")
        self.remove_zone_button.setFixedSize(126, 26)
        self.remove_zone_button.setToolTip("Remove the selected additional rigid or Flex Part")
        self.remove_zone_button.clicked.connect(self._remove_zone)
        corner_layout.addWidget(self.remove_zone_button)

        self.zone_toolbar_layout.addWidget(
            corner,
            0,
            Qt.AlignmentFlag.AlignRight,
        )
        self.tabs.currentChanged.connect(self._on_current_zone_changed)
        self.tabs.tabBarClicked.connect(self._on_zone_tab_clicked)

        self.setCentralWidget(central)

        # Default view: one rigid zone followed by one flex zone.
        self._add_zone(kind="rigid")
        self._add_zone(kind="flex")
        self._apply_default_sample_stackup()
        self._active_visual_editor = self._current_zone_editor()
        if self._active_visual_editor is not None and self._active_visual_editor.is_flex_zone:
            self._selected_flex_part_ids = {id(self._active_visual_editor)}
        self._refresh_flex_tab_selection_visuals()
        self._sync_file_menu_state()

    def _build_file_menu(self) -> None:
        self.file_menu = self.menuBar().addMenu("&File")
        self.new_stackup_action = QAction("&New", self)
        self.new_stackup_action.setStatusTip("Create a new stackup")
        self.new_stackup_action.triggered.connect(self._request_new_stackup)
        self.file_menu.addAction(self.new_stackup_action)
        self.file_menu.addSeparator()
        self.import_menu = self.file_menu.addMenu("&Import")
        self.export_menu = self.file_menu.addMenu("&Export")

        self.import_text_action = QAction("Stackup text...", self)
        self.import_text_action.setStatusTip("Import a complete rigid-flex stackup project from text")
        self.import_text_action.triggered.connect(self._import_text)
        self.import_menu.addAction(self.import_text_action)

        self.import_xpedition_action = QAction("Xpedition stackup...", self)
        self.import_xpedition_action.triggered.connect(
            lambda: self._trigger_current_zone_file_action("import_xpedition_action")
        )
        self.import_menu.addAction(self.import_xpedition_action)

        self.export_text_action = QAction("Stackup text...", self)
        self.export_text_action.setStatusTip("Export all rigid-flex zones and impedance profiles to text")
        self.export_text_action.triggered.connect(self._export_text)
        self.export_menu.addAction(self.export_text_action)

        self.export_xpedition_action = QAction("Xpedition stackup...", self)
        self.export_xpedition_action.setStatusTip(
            "Export the complete rigid-flex construction as a flattened Xpedition STK file"
        )
        self.export_xpedition_action.triggered.connect(self._export_xpedition)
        self.export_menu.addAction(self.export_xpedition_action)

        self._file_actions = {
            "import_text_action": self.import_text_action,
            "import_xpedition_action": self.import_xpedition_action,
            "export_text_action": self.export_text_action,
            "export_xpedition_action": self.export_xpedition_action,
        }
        self._build_command_menus()
        self._sync_file_menu_state()

    def _request_new_stackup(self) -> None:
        answer = QMessageBox.warning(
            self,
            "Create new stackup",
            "All the changes will be lost, Do you want to continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.newStackupRequested.emit()

    def _build_command_menus(self) -> None:
        self.build_menu = self.menuBar().addMenu("&Build")
        self.add_layer_above_action = self.build_menu.addAction("Add Layer Above")
        self.add_layer_below_action = self.build_menu.addAction("Add Layer Below")
        self.build_menu.addSeparator()
        self.add_material_above_action = self.build_menu.addAction("Add Material Above")
        self.add_material_below_action = self.build_menu.addAction("Add Material Below")
        self.build_menu.addSeparator()
        self.remove_symmetric_pair_action = self.build_menu.addAction("Remove Symmetric Pair")

        self.flex_menu = self.menuBar().addMenu("&Flex")
        self.insert_flex_sandwich_action = self.flex_menu.addAction("Insert Flex Sandwich")
        self.remove_flex_sandwich_action = self.flex_menu.addAction("Remove Flex Sandwich")

        self.analysis_menu = self.menuBar().addMenu("&Analysis")
        self.calculate_impedance_action = self.analysis_menu.addAction("Calculate Impedance")

        self._command_actions = {
            "add_layer_above_action": self.add_layer_above_action,
            "add_layer_below_action": self.add_layer_below_action,
            "add_material_above_action": self.add_material_above_action,
            "add_material_below_action": self.add_material_below_action,
            "remove_symmetric_pair_action": self.remove_symmetric_pair_action,
            "insert_flex_sandwich_action": self.insert_flex_sandwich_action,
            "remove_flex_sandwich_action": self.remove_flex_sandwich_action,
            "calculate_impedance_action": self.calculate_impedance_action,
        }
        for editor_action_name, action in self._command_actions.items():
            action.triggered.connect(
                lambda _checked=False, name=editor_action_name: self._trigger_current_zone_command(name)
            )

        self.units_menu = self.menuBar().addMenu("&Units")
        self.unit_action_group = QActionGroup(self)
        self.unit_action_group.setExclusive(True)
        self.unit_actions: dict[str, QAction] = {}
        for unit in SUPPORTED_UNITS:
            action = QAction(unit, self, checkable=True)
            action.setData(unit)
            action.triggered.connect(
                lambda _checked=False, value=unit: self._set_current_zone_unit(value)
            )
            self.unit_action_group.addAction(action)
            self.units_menu.addAction(action)
            self.unit_actions[unit] = action
        self._sync_command_menu_state()

    def _current_zone_editor(self) -> StackupEditorWindow | None:
        index = self.tabs.currentIndex() if hasattr(self, "tabs") else -1
        if 0 <= index < len(self._zone_editors):
            return self._zone_editors[index]
        return None

    def _selected_visual_editor(self) -> StackupEditorWindow | None:
        if self._active_visual_editor in self._zone_editors:
            return self._active_visual_editor
        return self._current_zone_editor()

    def _definition_source(self, editor: StackupEditorWindow) -> StackupEditorWindow:
        source_id = self._definition_source_by_alias.get(id(editor))
        source = self._editor_by_id(source_id)
        return source if source is not None else editor

    def _definition_aliases(self, source: StackupEditorWindow) -> list[StackupEditorWindow]:
        canonical = self._definition_source(source)
        alias_ids = self._definition_aliases_by_source.get(id(canonical), set())
        return [
            editor
            for editor in self._zone_editors
            if id(editor) in alias_ids
        ]

    def _bind_zone_definition(
        self,
        alias: StackupEditorWindow,
        source: StackupEditorWindow,
    ) -> None:
        canonical = self._definition_source(source)
        if alias is canonical:
            return
        old_source_id = self._definition_source_by_alias.get(id(alias))
        if old_source_id is not None:
            self._definition_aliases_by_source.get(old_source_id, set()).discard(id(alias))
        self._definition_source_by_alias[id(alias)] = id(canonical)
        self._definition_aliases_by_source.setdefault(id(canonical), set()).add(id(alias))
        alias_index = self._zone_index(alias)
        if alias_index is not None:
            self.tabs.setTabVisible(alias_index, False)
        if id(canonical) not in self._definition_signal_sources:
            canonical.stackupViewChanged.connect(
                lambda source_editor=canonical: self._sync_bound_definition_materials(source_editor)
            )
            self._definition_signal_sources.add(id(canonical))
        self._copy_definition_materials(canonical, alias)

    def _release_zone_definition_for_removal(
        self,
        editor: StackupEditorWindow,
    ) -> None:
        editor_id = id(editor)
        source_id = self._definition_source_by_alias.pop(editor_id, None)
        if source_id is not None:
            self._definition_aliases_by_source.get(source_id, set()).discard(editor_id)
            return

        alias_ids = self._definition_aliases_by_source.pop(editor_id, set())
        aliases = [
            candidate
            for candidate in self._zone_editors
            if id(candidate) in alias_ids and candidate is not editor
        ]
        if not aliases:
            return
        promoted = aliases[0]
        promoted_id = id(promoted)
        self._definition_source_by_alias.pop(promoted_id, None)
        promoted_index = self._zone_index(promoted)
        editor_index = self._zone_index(editor)
        if promoted_index is not None:
            self.tabs.setTabVisible(promoted_index, True)
            if editor_index is not None:
                self.tabs.setTabText(promoted_index, self.tabs.tabText(editor_index))
        remaining_alias_ids = {id(candidate) for candidate in aliases[1:]}
        self._definition_aliases_by_source[promoted_id] = remaining_alias_ids
        for alias_id in remaining_alias_ids:
            self._definition_source_by_alias[alias_id] = promoted_id
        if promoted_id not in self._definition_signal_sources:
            promoted.stackupViewChanged.connect(
                lambda source_editor=promoted: self._sync_bound_definition_materials(source_editor)
            )
            self._definition_signal_sources.add(promoted_id)

    def _copy_definition_materials(
        self,
        source: StackupEditorWindow,
        alias: StackupEditorWindow,
    ) -> None:
        if source.is_flex_zone != alias.is_flex_zone:
            return
        selected_meta = alias._current_row_meta() or ("layer", 0)
        updated = deepcopy(alias.stackup)
        if alias.is_flex_zone:
            source_coppers = [
                layer for layer in source.stackup.layers if isinstance(layer, CopperLayer)
            ]
            source_cores = [
                layer for layer in source.stackup.layers if isinstance(layer, FlexCoreLayer)
            ]
            copper_ordinal = 0
            core_ordinal = 0
            copied_layers: list[object] = []
            for layer in updated.layers:
                if isinstance(layer, CopperLayer) and source_coppers:
                    template = source_coppers[copper_ordinal % len(source_coppers)]
                    copied_layers.append(replace(deepcopy(template), uid=layer.uid))
                    copper_ordinal += 1
                elif isinstance(layer, FlexCoreLayer) and source_cores:
                    template = source_cores[core_ordinal % len(source_cores)]
                    copied_layers.append(deepcopy(template))
                    core_ordinal += 1
                else:
                    copied_layers.append(layer)
            updated.layers = copied_layers
            updated.coverlay = deepcopy(source.stackup.coverlay)
        else:
            source_coppers = [
                layer for layer in source.stackup.layers if isinstance(layer, CopperLayer)
            ]
            alias_copper_count = sum(
                isinstance(layer, CopperLayer) for layer in updated.layers
            )
            if len(source_coppers) != alias_copper_count:
                updated = deepcopy(source.stackup)
                self._rigid_branch_global_numbers[id(alias)] = list(
                    self._rigid_branch_global_numbers.get(
                        id(source),
                        range(1, source.stackup.copper_count() + 1),
                    )
                )
                self._rigid_branch_slot_maps[id(alias)] = dict(
                    self._rigid_branch_slot_maps.get(id(source), {})
                )
                alias.replace_stackup(updated, select_meta=selected_meta)
                return
            dielectric_templates: dict[str, list[DielectricLayer]] = {}
            for layer in source.stackup.layers:
                if isinstance(layer, DielectricLayer):
                    dielectric_templates.setdefault(layer.dielectric_type, []).append(layer)
            dielectric_ordinals: dict[str, int] = {}
            copper_ordinal = 0
            copied_layers = []
            for layer in updated.layers:
                if isinstance(layer, CopperLayer):
                    copied_layers.append(
                        replace(deepcopy(source_coppers[copper_ordinal]), uid=layer.uid)
                    )
                    copper_ordinal += 1
                elif isinstance(layer, DielectricLayer):
                    templates = dielectric_templates.get(layer.dielectric_type, [])
                    if not templates:
                        copied_layers.append(layer)
                        continue
                    ordinal = dielectric_ordinals.get(layer.dielectric_type, 0)
                    copied_layers.append(deepcopy(templates[ordinal % len(templates)]))
                    dielectric_ordinals[layer.dielectric_type] = ordinal + 1
                else:
                    copied_layers.append(layer)
            updated.layers = copied_layers
            updated.soldermask = deepcopy(source.stackup.soldermask)
        alias.replace_stackup(updated, select_meta=selected_meta)

    def _sync_bound_definition_materials(self, source: StackupEditorWindow) -> None:
        if self._definition_sync_in_progress:
            return
        canonical = self._definition_source(source)
        aliases = self._definition_aliases(canonical)
        if not aliases:
            return
        self._definition_sync_in_progress = True
        try:
            for alias in aliases:
                self._copy_definition_materials(canonical, alias)
            if not self._rigid_sync_in_progress:
                self._sync_all_rigid_zones()
        finally:
            self._definition_sync_in_progress = False

    def _is_connected_to_primary_rigid(self, rigid_editor: StackupEditorWindow) -> bool:
        primary = self._primary_rigid_editor()
        if primary is None:
            return False
        pending = [rigid_editor]
        visited: set[int] = set()
        while pending:
            candidate = pending.pop()
            if candidate is primary:
                return True
            if id(candidate) in visited:
                continue
            visited.add(id(candidate))
            for parent_flex in self._parent_flexes_for_rigid(candidate):
                parent_rigid = self._parent_rigid_for_flex(parent_flex)
                if parent_rigid is not None:
                    pending.append(parent_rigid)
        return False

    def _trigger_current_zone_file_action(self, editor_action_name: str) -> None:
        editor = self._current_zone_editor()
        if editor is None:
            return
        editor_action = getattr(editor, editor_action_name)
        if editor_action.isEnabled():
            editor_action.trigger()

    def _trigger_current_zone_command(self, editor_action_name: str) -> None:
        editor = self._current_zone_editor()
        if editor is None:
            return
        editor_action = getattr(editor, editor_action_name)
        if editor_action.isEnabled():
            editor_action.trigger()

    def _set_current_zone_unit(self, unit: str) -> None:
        editor = self._current_zone_editor()
        if editor is not None:
            editor.unit_combo.setCurrentText(unit)
            for alias in self._definition_aliases(editor):
                alias.unit_combo.setCurrentText(unit)

    def _sync_command_menu_state(self) -> None:
        editor = self._current_zone_editor()
        for editor_action_name, action in self._command_actions.items():
            if editor is None:
                action.setEnabled(False)
                continue
            editor_action = getattr(editor, editor_action_name)
            action.setEnabled(editor_action.isEnabled())
            action.setStatusTip(editor_action.statusTip())

        if editor is not None and editor.is_flex_zone and len(self._rigid_editors()) > 1:
            for action in (self.insert_flex_sandwich_action, self.remove_flex_sandwich_action):
                action.setEnabled(False)
                action.setStatusTip("Remove additional rigid parts before changing the global flex topology.")

        rigid_selected = editor is not None and not editor.is_flex_zone
        flex_selected = editor is not None and editor.is_flex_zone
        self.build_menu.menuAction().setEnabled(rigid_selected)
        self.flex_menu.menuAction().setEnabled(flex_selected)
        self.analysis_menu.menuAction().setEnabled(editor is not None)
        self.units_menu.menuAction().setEnabled(editor is not None)
        if editor is not None:
            current_unit_action = self.unit_actions.get(editor.display_unit)
            if current_unit_action is not None:
                current_unit_action.setChecked(True)

    def _sync_file_menu_state(self) -> None:
        has_zones = bool(self._zone_editors)
        self.import_text_action.setEnabled(has_zones)
        self.export_text_action.setEnabled(has_zones)
        self.import_text_action.setStatusTip(
            "Import a complete rigid-flex stackup project from text"
            if has_zones
            else "No rigid-flex project is open."
        )
        self.export_text_action.setStatusTip(
            "Export all rigid-flex zones and impedance profiles to text"
            if has_zones
            else "No rigid-flex project is open."
        )
        self.import_xpedition_action.setEnabled(False)
        self.export_xpedition_action.setEnabled(has_zones)
        self.import_xpedition_action.setStatusTip("Xpedition import is not supported for rigid-flex projects.")
        self.export_xpedition_action.setStatusTip(
            "Export the complete rigid-flex construction as a flattened Xpedition STK file"
            if has_zones
            else "No rigid-flex project is open."
        )

    def _zone_states_for_export(self) -> list[RigidFlexZoneState]:
        zone_index_by_id = {id(editor): index for index, editor in enumerate(self._zone_editors)}
        parent_indices_by_id: dict[int, list[int]] = {}
        for editor in self._zone_editors:
            if editor.is_flex_zone:
                parent_ids = [self._flex_parent_rigid.get(id(editor))]
            else:
                parent_ids = list(self._rigid_parent_flexes.get(id(editor), set()))
            parent_indices_by_id[id(editor)] = sorted(
                parent_index
                for parent_id in parent_ids
                if parent_id is not None
                and (parent_index := zone_index_by_id.get(parent_id)) is not None
            )
        return [
            RigidFlexZoneState(
                kind="flex" if editor.is_flex_zone else "rigid",
                label=self.tabs.tabText(index),
                display_unit=editor.display_unit,
                stackup=editor.stackup,
                impedance_workspace=editor.impedance_workspace,
                flex_slot_coverage=(
                    sorted(self._rigid_branch_coverage.get(id(editor)) or [])
                    if not editor.is_flex_zone and self._rigid_branch_coverage.get(id(editor)) is not None
                    else None
                ),
                flex_slot_map=(
                    dict(self._rigid_branch_slot_maps.get(id(editor), {}))
                    if not editor.is_flex_zone
                    else None
                ),
                global_copper_numbers=(
                    list(self._rigid_branch_global_numbers.get(id(editor), []))
                    if not editor.is_flex_zone
                    else None
                ),
                parent_zone_index=(
                    parent_indices_by_id[id(editor)][0]
                    if parent_indices_by_id[id(editor)]
                    else None
                ),
                parent_zone_indices=parent_indices_by_id[id(editor)],
                definition_zone_index=zone_index_by_id.get(
                    self._definition_source_by_alias.get(id(editor), -1)
                ),
            )
            for index, editor in enumerate(self._zone_editors)
        ]

    def _export_text(self) -> None:
        zones = self._zone_states_for_export()
        try:
            output = export_rigid_flex_text(zones)
        except ValueError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return

        target, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export rigid-flex stackup as text",
            str(self.root_path / "rigid_flex_stackup.txt"),
            "Text files (*.txt);;All files (*.*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not target:
            return
        try:
            Path(target).write_text(output, encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Export complete", f"Rigid-flex stackup exported to:\n{target}")

    def _export_xpedition(self) -> None:
        zones = self._zone_states_for_export()
        master_editor = next((editor for editor in self._zone_editors if not editor.is_flex_zone), None)
        if master_editor is None:
            QMessageBox.warning(
                self,
                "Xpedition export failed",
                "The project has no Master Rigid Part to export.",
            )
            return
        try:
            output = export_rigid_flex_xpedition(zones, master_editor.catalog)
        except ValueError as exc:
            QMessageBox.warning(self, "Xpedition export failed", str(exc))
            return

        target, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export rigid-flex Xpedition stackup",
            str(self.root_path / "rigid_flex_stackup.stk"),
            "Xpedition stackup (*.stk);;All files (*.*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not target:
            return
        try:
            Path(target).write_text(output, encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Xpedition export failed", str(exc))
            return
        QMessageBox.information(self, "Export complete", f"Xpedition stackup exported to:\n{target}")

    def _import_text(self) -> None:
        source, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Import rigid-flex stackup text",
            str(self.root_path),
            "Text files (*.txt);;All files (*.*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not source:
            return
        try:
            content = Path(source).read_text(encoding="utf-8")
            mode_warning = stackup_import_mode_warning(content, rigid_flex_mode=True)
            if mode_warning is not None:
                QMessageBox.warning(self, "Stackup type mismatch", mode_warning)
                return
            zones = import_rigid_flex_text(content)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return

        self._replace_zones_from_import(zones)
        QMessageBox.information(self, "Import complete", f"Rigid-flex stackup imported from:\n{source}")

    def _replace_zones_from_import(self, zones: list[RigidFlexZoneState]) -> None:
        old_editors = self._zone_editors
        self._zone_editors = []
        self._flex_sandwich_history.clear()
        self._rigid_branch_coverage.clear()
        self._rigid_branch_slot_maps.clear()
        self._rigid_branch_global_numbers.clear()
        self._rigid_flex_gap_by_slot.clear()
        self._rigid_parent_flexes.clear()
        self._flex_parent_rigid.clear()
        self._flex_child_rigids.clear()
        self._definition_source_by_alias.clear()
        self._definition_aliases_by_source.clear()
        self._definition_signal_sources.clear()
        self._active_visual_editor = None
        self._selected_flex_part_ids.clear()

        while self.tabs.count():
            widget = self.tabs.widget(0)
            self.tabs.removeTab(0)
            if widget is not None:
                widget.deleteLater()
        for editor in old_editors:
            editor.deleteLater()

        for zone in zones:
            editor = self._make_zone_editor(zone.kind)
            central = editor.centralWidget()
            central.setParent(None)
            self.tabs.addTab(central, zone.label)
            self._zone_editors.append(editor)
            if zone.kind == "rigid":
                self._rigid_branch_coverage[id(editor)] = (
                    set(zone.flex_slot_coverage) if zone.flex_slot_coverage is not None else None
                )
                self._rigid_branch_slot_maps[id(editor)] = dict(zone.flex_slot_map or {})
                self._rigid_branch_global_numbers[id(editor)] = list(
                    zone.global_copper_numbers or range(1, zone.stackup.copper_count() + 1)
                )

            editor.replace_stackup(zone.stackup, select_meta=("layer", 0))
            editor._ui_loading = True
            try:
                editor.unit_combo.setCurrentText(zone.display_unit)
            finally:
                editor._ui_loading = False
            editor.display_unit = zone.display_unit
            if zone.display_unit in {"um", "mm", "mil", "inch"}:
                editor.geometry_input_unit = zone.display_unit
            if zone.impedance_workspace is not None:
                editor.impedance_workspace = zone.impedance_workspace
                editor._impedance_legacy_migrated = True
            editor.zone_display_name = zone.label

        for zone_index, zone in enumerate(zones):
            editor = self._zone_editors[zone_index]
            parent_indices = zone.parent_zone_indices or (
                [zone.parent_zone_index] if zone.parent_zone_index is not None else []
            )
            for parent_index in parent_indices:
                if not 0 <= parent_index < len(self._zone_editors):
                    continue
                parent = self._zone_editors[parent_index]
                if editor.is_flex_zone and not parent.is_flex_zone:
                    self._register_flex_parent(editor, parent)
                elif not editor.is_flex_zone and parent.is_flex_zone:
                    self._register_rigid_parent(editor, parent)

        # The text format already preserves the actual rigid stackup and the
        # flex-slot ids. Reconstruct the exact slot-to-copper-gap association
        # before any legacy slot compaction can reinterpret it as a centered
        # flex region.
        for editor in self._rigid_editors():
            if self._flex_gap_indices(editor):
                self._ensure_rigid_flex_gap_map(editor)

        for editor in self._zone_editors:
            if editor.is_flex_zone:
                self._compact_flex_slot_layout_if_possible(editor)

        self._sync_sub_rigid_copper_from_master()

        for index, editor in enumerate(self._zone_editors):
            label = self.tabs.tabText(index)
            if editor.is_flex_zone:
                self._configure_flex_zone(editor, zone_display_name=label)
            else:
                self._configure_rigid_zone(editor, zone_display_name=label)
            self._disable_unsupported_zone_actions(editor)
            editor._set_note("Imported as part of a rigid-flex stackup project.")

        for zone_index, zone in enumerate(zones):
            definition_index = zone.definition_zone_index
            if (
                definition_index is None
                or not 0 <= definition_index < len(self._zone_editors)
                or definition_index == zone_index
            ):
                continue
            self._bind_zone_definition(
                self._zone_editors[zone_index],
                self._zone_editors[definition_index],
            )

        for editor in list(self._zone_editors):
            if id(editor) in self._definition_source_by_alias:
                continue
            if not editor.is_flex_zone:
                continue
            parent_rigid = self._parent_rigid_for_flex(editor)
            if parent_rigid is None:
                continue
            incoming_source = self._incoming_flex_for_slots(
                parent_rigid,
                editor.stackup.active_flex_slot_ids(),
            )
            if incoming_source is not None and incoming_source is not editor:
                self._bind_zone_definition(editor, incoming_source)

        self.tabs.setCurrentIndex(0)
        self._active_visual_editor = self._zone_editors[0]
        first_flex = next((editor for editor in self._zone_editors if editor.is_flex_zone), None)
        if first_flex is not None:
            self._selected_flex_part_ids = {id(first_flex)}
        self._refresh_flex_tab_selection_visuals()
        self._update_zone_controls()
        self._refresh_combined_preview()
        self._sync_file_menu_state()
        self._sync_command_menu_state()

    def _selected_flex_parts(self) -> list[StackupEditorWindow]:
        valid_ids = {
            id(editor) for editor in self._zone_editors if editor.is_flex_zone
        }
        self._selected_flex_part_ids.intersection_update(valid_ids)
        return [
            editor
            for editor in self._zone_editors
            if editor.is_flex_zone and id(editor) in self._selected_flex_part_ids
        ]

    def _handle_flex_tab_selection_gesture(self, index: int, toggle: bool) -> None:
        if not 0 <= index < len(self._zone_editors):
            return
        editor = self._zone_editors[index]
        if not editor.is_flex_zone:
            self._selected_flex_part_ids.clear()
        elif toggle:
            if id(editor) in self._selected_flex_part_ids:
                self._selected_flex_part_ids.remove(id(editor))
            else:
                self._selected_flex_part_ids.add(id(editor))
        else:
            self._selected_flex_part_ids = {id(editor)}
        self._refresh_flex_tab_selection_visuals()
        self._update_zone_controls()

    def _select_only_flex_part(self, editor: StackupEditorWindow) -> None:
        self._selected_flex_part_ids = {id(editor)} if editor.is_flex_zone else set()
        self._refresh_flex_tab_selection_visuals()

    def _refresh_flex_tab_selection_visuals(self) -> None:
        selected_ids = {id(editor) for editor in self._selected_flex_parts()}
        for index, editor in enumerate(self._zone_editors):
            if not editor.is_flex_zone:
                self.tabs.tabBar().setTabTextColor(index, QColor())
                self.tabs.setTabToolTip(index, "")
                continue
            selected = id(editor) in selected_ids
            self.tabs.tabBar().setTabTextColor(
                index,
                QColor("#70e6f2") if selected else QColor(),
            )
            self.tabs.setTabToolTip(
                index,
                (
                    "Selected for the next rigid part. Ctrl+click to remove from the selection."
                    if selected
                    else "Ctrl+click to add this Flex Part to the rigid-part selection."
                ),
            )

    def _on_current_zone_changed(self, _index: int) -> None:
        if not self._preview_selection_in_progress:
            self._rigid_box_inspection_active = False
            self._active_visual_editor = self._current_zone_editor()
        self._update_zone_controls()
        self._refresh_combined_preview()
        self._sync_file_menu_state()
        self._sync_command_menu_state()

    def _on_zone_tab_clicked(self, index: int) -> None:
        if self._preview_selection_in_progress:
            return
        if not 0 <= index < len(self._zone_editors):
            return
        self._rigid_box_inspection_active = False
        self._active_visual_editor = self._zone_editors[index]
        self._update_zone_controls()
        self._refresh_combined_preview()

    def _make_zone_editor(self, kind: str) -> StackupEditorWindow:
        editor = StackupEditorWindow(self.root_path, zone_kind=kind)
        editor.build_context_menu_handler = (
            lambda meta, global_pos, zone_editor=editor: self._handle_zone_build_context_menu(
                zone_editor,
                meta,
                global_pos,
            )
        )
        editor.right_pane.hide()
        editor.main_splitter.setSizes([1600, 0])
        editor.stackupViewChanged.connect(self._refresh_combined_preview)
        editor.stackupViewChanged.connect(self._sync_command_menu_state)
        if kind == "rigid":
            editor.stackupViewChanged.connect(
                lambda e=editor: self._handle_rigid_stackup_view_change(e)
            )
        editor.table.itemSelectionChanged.connect(self._refresh_combined_preview)
        editor.table.itemSelectionChanged.connect(self._sync_command_menu_state)
        if kind == "flex":
            editor.sharedRegionChanged.connect(lambda e=editor: self._sync_all_rigid_zones())
            editor.insertFlexSandwichRequested.connect(lambda e=editor: self._insert_flex_sandwich(e))
            editor.removeFlexSandwichRequested.connect(lambda e=editor: self._remove_flex_sandwich(e))
        else:
            editor.structureChanged.connect(lambda e=editor: self._handle_rigid_structure_change(e))
        return editor

    def _handle_rigid_structure_change(self, editor: StackupEditorWindow) -> None:
        self._remap_rigid_flex_gaps_after_structure_change(editor)
        if editor is self._primary_rigid_editor():
            self._rigid_branch_global_numbers[id(editor)] = list(
                range(1, editor.stackup.copper_count() + 1)
            )
        self._configure_rigid_zone(
            editor,
            zone_display_name=editor.zone_display_name,
        )
        for flex_editor in self._adjacent_flex_editors(editor):
            self._configure_flex_zone(
                flex_editor,
                rigid_editor=editor,
                zone_display_name=flex_editor.zone_display_name,
            )
        self._refresh_combined_preview()

    def _handle_rigid_stackup_view_change(self, editor: StackupEditorWindow) -> None:
        if self._rigid_sync_in_progress:
            return
        self._rigid_sync_in_progress = True
        try:
            if editor is self._primary_rigid_editor():
                self._sync_sub_rigid_copper_from_master()
            self._refresh_rigid_no_flow_row_labels()
        finally:
            self._rigid_sync_in_progress = False

    @staticmethod
    def _no_flow_templates_by_global_gap(
        stackup: Stackup,
        global_numbers: list[int],
    ) -> dict[tuple[int, int], DielectricLayer]:
        copper_indices = [
            index
            for index, layer in enumerate(stackup.layers)
            if isinstance(layer, CopperLayer)
        ]
        if len(global_numbers) != len(copper_indices):
            return {}
        templates: dict[tuple[int, int], DielectricLayer] = {}
        for position, (top_index, bottom_index) in enumerate(
            zip(copper_indices, copper_indices[1:])
        ):
            template = next(
                (
                    layer
                    for layer in stackup.layers[top_index + 1 : bottom_index]
                    if isinstance(layer, DielectricLayer)
                    and is_no_flow_prepreg_type(layer.dielectric_type)
                ),
                None,
            )
            if template is not None:
                templates[(global_numbers[position], global_numbers[position + 1])] = deepcopy(
                    template
                )
        return templates

    @staticmethod
    def _prepreg_templates_by_global_gap(
        stackup: Stackup,
        global_numbers: list[int],
    ) -> dict[tuple[int, int], list[DielectricLayer]]:
        copper_indices = [
            index
            for index, layer in enumerate(stackup.layers)
            if isinstance(layer, CopperLayer)
        ]
        if len(global_numbers) != len(copper_indices):
            return {}
        templates: dict[tuple[int, int], list[DielectricLayer]] = {}
        for position, (top_index, bottom_index) in enumerate(
            zip(copper_indices, copper_indices[1:])
        ):
            templates[(global_numbers[position], global_numbers[position + 1])] = [
                deepcopy(layer)
                for layer in stackup.layers[top_index + 1 : bottom_index]
                if isinstance(layer, DielectricLayer)
                and is_prepreg_dielectric_type(layer.dielectric_type)
            ]
        return templates

    @staticmethod
    def _apply_no_flow_templates_to_stackup(
        stackup: Stackup,
        global_numbers: list[int],
        templates: dict[tuple[int, int], DielectricLayer],
    ) -> bool:
        copper_indices = [
            index
            for index, layer in enumerate(stackup.layers)
            if isinstance(layer, CopperLayer)
        ]
        if len(global_numbers) != len(copper_indices):
            return False
        changed = False
        for position, (top_index, bottom_index) in enumerate(
            zip(copper_indices, copper_indices[1:])
        ):
            template = templates.get(
                (global_numbers[position], global_numbers[position + 1])
            )
            if template is None:
                continue
            gap_layers = stackup.layers[top_index + 1 : bottom_index]
            if any(isinstance(layer, FlexCoreLayer) for layer in gap_layers):
                continue
            for layer_index in range(top_index + 1, bottom_index):
                target = stackup.layers[layer_index]
                if not isinstance(target, DielectricLayer):
                    continue
                synchronized = deepcopy(template)
                if synchronized != target:
                    stackup.layers[layer_index] = synchronized
                    changed = True
        return changed

    @staticmethod
    def _sync_master_controlled_no_flow_to_stackup(
        stackup: Stackup,
        global_numbers: list[int],
        source_prepreg_templates: dict[
            tuple[int, int],
            list[DielectricLayer],
        ],
        *,
        controlled_no_flow_gaps: set[tuple[int, int]] | None = None,
        valid_no_flow_gaps: set[tuple[int, int]] | None = None,
    ) -> bool:
        copper_indices = [
            index
            for index, layer in enumerate(stackup.layers)
            if isinstance(layer, CopperLayer)
        ]
        if len(global_numbers) != len(copper_indices):
            return False
        target_fallback_pp = next(
            (
                deepcopy(layer)
                for layer in stackup.layers
                if isinstance(layer, DielectricLayer)
                and is_prepreg_dielectric_type(layer.dielectric_type)
                and not is_no_flow_prepreg_type(layer.dielectric_type)
            ),
            None,
        )
        source_fallback_pp = next(
            (
                deepcopy(layer)
                for templates in source_prepreg_templates.values()
                for layer in templates
                if not is_no_flow_prepreg_type(layer.dielectric_type)
            ),
            None,
        )
        changed = False
        # Work from bottom to top so replacing one gap cannot invalidate the
        # indices of a gap that has not yet been visited.
        gap_pairs = list(enumerate(zip(copper_indices, copper_indices[1:])))
        for position, (top_index, bottom_index) in reversed(gap_pairs):
            gap_key = (
                global_numbers[position],
                global_numbers[position + 1],
            )
            controls_gap = (
                controlled_no_flow_gaps is None
                or gap_key in controlled_no_flow_gaps
            )
            source_layers = source_prepreg_templates.get(
                gap_key,
                [],
            ) if controls_gap else []
            gap_layers = stackup.layers[top_index + 1 : bottom_index]
            if any(isinstance(layer, FlexCoreLayer) for layer in gap_layers):
                continue
            source_no_flow = [
                deepcopy(layer)
                for layer in source_layers
                if is_no_flow_prepreg_type(layer.dielectric_type)
            ]
            source_local_pp = next(
                (
                    deepcopy(layer)
                    for layer in source_layers
                    if not is_no_flow_prepreg_type(layer.dielectric_type)
                ),
                None,
            )
            target_has_no_flow = any(
                isinstance(layer, DielectricLayer)
                and is_no_flow_prepreg_type(layer.dielectric_type)
                for layer in gap_layers
            )
            stale_outside_flex_connection = (
                target_has_no_flow
                and valid_no_flow_gaps is not None
                and gap_key not in valid_no_flow_gaps
            )
            if not controls_gap and not stale_outside_flex_connection:
                continue
            if not source_no_flow and not target_has_no_flow:
                continue
            synchronized_gap = [
                deepcopy(layer)
                for layer in gap_layers
                if not (
                    isinstance(layer, DielectricLayer)
                    and is_no_flow_prepreg_type(layer.dielectric_type)
                )
            ]
            ordinary_pp_positions = [
                index
                for index, layer in enumerate(synchronized_gap)
                if isinstance(layer, DielectricLayer)
                and is_prepreg_dielectric_type(layer.dielectric_type)
                and not is_no_flow_prepreg_type(layer.dielectric_type)
            ]
            if (source_no_flow or target_has_no_flow) and not ordinary_pp_positions:
                rigid_pp = (
                    target_fallback_pp
                    or source_local_pp
                    or source_fallback_pp
                    or DielectricLayer(dielectric_type="prepreg")
                )
                synchronized_gap.append(deepcopy(rigid_pp))
                ordinary_pp_positions = [len(synchronized_gap) - 1]
            if source_no_flow:
                source_first_no_flow = next(
                    (
                        index
                        for index, layer in enumerate(source_layers)
                        if is_no_flow_prepreg_type(layer.dielectric_type)
                    ),
                    0,
                )
                source_first_pp = next(
                    (
                        index
                        for index, layer in enumerate(source_layers)
                        if not is_no_flow_prepreg_type(layer.dielectric_type)
                    ),
                    len(source_layers),
                )
                if source_first_no_flow <= source_first_pp:
                    insert_at = ordinary_pp_positions[0]
                else:
                    insert_at = ordinary_pp_positions[-1] + 1
                synchronized_gap[insert_at:insert_at] = source_no_flow
            if synchronized_gap != gap_layers:
                stackup.layers[top_index + 1 : bottom_index] = synchronized_gap
                changed = True
        return changed

    def _no_flow_gaps_adjacent_to_flex(
        self,
        rigid_editor: StackupEditorWindow,
        flex_editor: StackupEditorWindow,
    ) -> set[tuple[int, int]]:
        copper_indices = [
            index
            for index, layer in enumerate(rigid_editor.stackup.layers)
            if isinstance(layer, CopperLayer)
        ]
        global_numbers = self._rigid_branch_global_numbers.get(
            id(rigid_editor),
            [],
        )
        if len(global_numbers) != len(copper_indices):
            return set()
        copper_position_by_index = {
            layer_index: position
            for position, layer_index in enumerate(copper_indices)
        }
        adjacent_gaps: set[tuple[int, int]] = set()
        for global_slot in self._covered_global_slots(rigid_editor, flex_editor):
            pair = self._rigid_pair_for_global_slot(
                rigid_editor,
                flex_editor,
                global_slot,
            )
            if pair is None:
                continue
            top_position = copper_position_by_index.get(pair[0])
            bottom_position = copper_position_by_index.get(pair[1])
            if (
                top_position is None
                or bottom_position is None
                or bottom_position != top_position + 1
            ):
                continue
            if top_position > 0:
                adjacent_gaps.add(
                    (
                        global_numbers[top_position - 1],
                        global_numbers[top_position],
                    )
                )
            if bottom_position + 1 < len(global_numbers):
                adjacent_gaps.add(
                    (
                        global_numbers[bottom_position],
                        global_numbers[bottom_position + 1],
                    )
                )
        return adjacent_gaps

    def _sync_associated_no_flow_materials(
        self,
        source_rigid: StackupEditorWindow,
    ) -> None:
        _ = source_rigid
        master = self._primary_rigid_editor()
        if master is None:
            return
        master_copper_count = master.stackup.copper_count()
        master_numbers = self._rigid_branch_global_numbers.get(
            id(master),
            list(range(1, master_copper_count + 1)),
        )
        templates = self._prepreg_templates_by_global_gap(
            master.stackup,
            master_numbers,
        )
        for child in self._rigid_editors():
            if child is master:
                continue
            valid_gaps = {
                gap
                for flex_editor in self._adjacent_flex_editors(child)
                for gap in self._no_flow_gaps_adjacent_to_flex(
                    child,
                    flex_editor,
                )
            }
            child_numbers = self._rigid_branch_global_numbers.get(
                id(child),
                [],
            )
            updated = deepcopy(child.stackup)
            changed = self._sync_master_controlled_no_flow_to_stackup(
                updated,
                child_numbers,
                templates,
                controlled_no_flow_gaps=valid_gaps,
                valid_no_flow_gaps=valid_gaps,
            )
            if changed:
                selected_meta = child._current_row_meta() or ("layer", 0)
                child.replace_stackup(updated, select_meta=selected_meta)
                self._configure_rigid_zone(
                    child,
                    zone_display_name=child.zone_display_name,
                )

    def _sync_all_associated_no_flow_materials(self) -> None:
        master = self._primary_rigid_editor()
        if master is not None:
            self._sync_associated_no_flow_materials(master)

    def _sync_sub_rigid_copper_from_master(self) -> None:
        master = self._primary_rigid_editor()
        if master is None:
            return
        master_copper_indices = [
            index
            for index, layer in enumerate(master.stackup.layers)
            if isinstance(layer, CopperLayer)
        ]
        master_numbers = self._rigid_branch_global_numbers.get(
            id(master),
            list(range(1, len(master_copper_indices) + 1)),
        )
        if len(master_numbers) != len(master_copper_indices):
            return
        master_copper_by_number = {
            number: master.stackup.layers[index]
            for number, index in zip(master_numbers, master_copper_indices)
        }

        for rigid_editor in self._rigid_editors():
            if rigid_editor is master:
                continue
            copper_indices = [
                index
                for index, layer in enumerate(rigid_editor.stackup.layers)
                if isinstance(layer, CopperLayer)
            ]
            global_numbers = self._rigid_branch_global_numbers.get(id(rigid_editor), [])
            if len(global_numbers) != len(copper_indices):
                continue
            updated = deepcopy(rigid_editor.stackup)
            changed = False
            for global_number, layer_index in zip(global_numbers, copper_indices):
                template = master_copper_by_number.get(global_number)
                target = updated.layers[layer_index]
                if not isinstance(template, CopperLayer) or not isinstance(target, CopperLayer):
                    continue
                synchronized = replace(
                    target,
                    thickness_mm=template.thickness_mm,
                    copper_type=template.copper_type,
                    roughness_um=template.roughness_um,
                )
                if synchronized != target:
                    updated.layers[layer_index] = synchronized
                    changed = True
            if changed:
                selected_meta = rigid_editor._current_row_meta() or ("layer", 0)
                rigid_editor.replace_stackup(updated, select_meta=selected_meta)

    def _sandwich_choice_labels(
        self,
        flex_editor: StackupEditorWindow | None = None,
    ) -> list[tuple[int, str]]:
        flex_editor = flex_editor or self._primary_flex_editor()
        rigid_editor = self._parent_rigid_for_flex(flex_editor) if flex_editor is not None else None
        rigid_editor = rigid_editor or self._primary_rigid_editor()
        if flex_editor is None or rigid_editor is None:
            return []
        copper_numbers = self._flex_copper_number_overrides(rigid_editor, flex_editor)
        labels: list[tuple[int, str]] = []
        for sandwich_index, slot_id in enumerate(flex_editor.stackup.flex_sandwich_slot_ids()):
            top_layer_index = sandwich_index * 3
            bottom_layer_index = top_layer_index + 2
            top_number = copper_numbers.get(top_layer_index)
            bottom_number = copper_numbers.get(bottom_layer_index)
            layer_text = (
                f"L{top_number}-L{bottom_number}"
                if top_number is not None and bottom_number is not None
                else f"slot {slot_id + 1}"
            )
            labels.append((slot_id, f"Flex Sandwich {slot_id + 1} ({layer_text})"))
        return labels

    def _build_minimal_rigid_branch(
        self,
        source_rigid: StackupEditorWindow,
        flex_editor: StackupEditorWindow,
        selected_global_slots: set[int],
        *,
        available_global_slots: set[int] | None = None,
        global_slot_capacity: int | None = None,
        global_slot_templates: dict[int, FlexCoreLayer] | None = None,
        target_copper_count: int | None = None,
    ) -> tuple[Stackup, dict[int, int], list[int]]:
        available_slots = available_global_slots or flex_editor.stackup.active_flex_slot_ids()
        selected_slots = sorted(selected_global_slots & available_slots)
        if not selected_slots:
            raise ValueError("Select at least one active flex sandwich.")

        global_capacity = global_slot_capacity or flex_editor.stackup.flex_slot_capacity_or_count()
        source_copper_indices = [
            index for index, layer in enumerate(source_rigid.stackup.layers) if isinstance(layer, CopperLayer)
        ]
        outer_copper_count = (len(source_copper_indices) - (global_capacity * 2)) // 2
        selected_top_copper = outer_copper_count + (selected_slots[0] * 2)
        selected_bottom_copper = outer_copper_count + (selected_slots[-1] * 2) + 1
        first_copper_number = selected_top_copper - 1
        last_copper_number = selected_bottom_copper + 1
        if (
            outer_copper_count < 1
            or first_copper_number < 0
            or last_copper_number >= len(source_copper_indices)
        ):
            raise ValueError("The selected flex coverage does not leave one rigid copper layer on each side.")

        minimum_copper_count = last_copper_number - first_copper_number + 1
        desired_copper_count = target_copper_count or minimum_copper_count
        if (
            desired_copper_count < minimum_copper_count
            or desired_copper_count > len(source_copper_indices)
            or desired_copper_count % 2 != 0
        ):
            raise ValueError("The selected intermediate rigid-layer count is not structurally valid.")
        extra_copper = desired_copper_count - minimum_copper_count
        first_copper_number -= extra_copper // 2
        last_copper_number = first_copper_number + desired_copper_count - 1
        if first_copper_number < 0:
            last_copper_number -= first_copper_number
            first_copper_number = 0
        if last_copper_number >= len(source_copper_indices):
            shift = last_copper_number - len(source_copper_indices) + 1
            first_copper_number -= shift
            last_copper_number -= shift

        local_selected_top = selected_top_copper - first_copper_number
        local_selected_bottom = selected_bottom_copper - first_copper_number
        selected_slot_span = selected_slots[-1] - selected_slots[0] + 1
        maximum_outer_copper = (desired_copper_count - (selected_slot_span * 2)) // 2
        local_outer_copper = min(
            local_selected_top,
            desired_copper_count - local_selected_bottom - 1,
            maximum_outer_copper,
        )
        while local_outer_copper >= 1 and (local_selected_top - local_outer_copper) % 2:
            local_outer_copper -= 1
        if local_outer_copper < 1:
            raise ValueError("The intermediate span cannot preserve a valid flex-slot position.")
        local_capacity = (desired_copper_count - (local_outer_copper * 2)) // 2
        slot_map: dict[int, int] = {}
        for local_slot in range(local_capacity):
            global_top_copper = first_copper_number + local_outer_copper + (local_slot * 2)
            global_offset = global_top_copper - outer_copper_count
            if global_offset % 2:
                continue
            global_slot = global_offset // 2
            if 0 <= global_slot < global_capacity:
                slot_map[global_slot] = local_slot
        if any(slot_id not in slot_map for slot_id in selected_slots):
            raise ValueError("The intermediate span does not contain every selected flex sandwich.")

        start_index = source_copper_indices[first_copper_number]
        end_index = source_copper_indices[last_copper_number]
        sliced_stackup = Stackup(
            mode="rigid",
            soldermask=deepcopy(source_rigid.stackup.soldermask),
            layers=[deepcopy(layer) for layer in source_rigid.stackup.layers[start_index : end_index + 1]],
        )
        global_templates = global_slot_templates or self._flex_slot_templates(flex_editor)
        local_templates = {
            slot_map[slot_id]: deepcopy(global_templates[slot_id])
            for slot_id in selected_slots
            if slot_id in global_templates
        }
        rebuilt = rebuild_rigid_stackup_from_slot_activity(
            sliced_stackup,
            slot_capacity=local_capacity,
            active_slot_ids={slot_map[slot_id] for slot_id in selected_slots},
            slot_templates=local_templates,
            rigid_core_template=self._rigid_core_template_for_slots(source_rigid),
            bridge_dielectric_template=source_rigid._default_dielectric("prepreg"),
            outer_boundary_dielectric_template=source_rigid._default_dielectric("prepreg"),
        )
        global_numbers = list(range(first_copper_number + 1, last_copper_number + 2))
        return rebuilt, slot_map, global_numbers

    def _build_combined_rigid_branch(
        self,
        source_rigid: StackupEditorWindow,
        parent_flexes: list[StackupEditorWindow],
        *,
        target_copper_count: int | None = None,
    ) -> tuple[Stackup, dict[int, int], list[int], dict[int, int], set[int]]:
        connections: dict[int, tuple[int, int, FlexCoreLayer]] = {}
        for flex_editor in parent_flexes:
            parent_rigid = self._parent_rigid_for_flex(flex_editor) or source_rigid
            number_overrides = self._flex_copper_number_overrides(parent_rigid, flex_editor)
            templates = self._flex_slot_templates(flex_editor)
            for sandwich_index, slot_id in enumerate(flex_editor.stackup.flex_sandwich_slot_ids()):
                top_number = number_overrides.get(sandwich_index * 3)
                bottom_number = number_overrides.get((sandwich_index * 3) + 2)
                template = templates.get(slot_id)
                if top_number is None or bottom_number is None or template is None:
                    raise ValueError(
                        f"{flex_editor.zone_display_name} does not have a complete rigid-layer connection."
                    )
                existing = connections.get(slot_id)
                if existing is not None and existing[:2] != (top_number, bottom_number):
                    raise ValueError(
                        "The selected Flex Parts reuse one slot id on different copper-layer pairs."
                    )
                connections.setdefault(
                    slot_id,
                    (top_number, bottom_number, deepcopy(template)),
                )

        ordered_connections = sorted(
            (
                (slot_id, top_number, bottom_number, template)
                for slot_id, (top_number, bottom_number, template) in connections.items()
            ),
            key=lambda item: (item[1], item[2], item[0]),
        )
        if not ordered_connections:
            raise ValueError("Select at least one connected Flex Part.")
        if any(bottom_number != top_number + 1 for _slot, top_number, bottom_number, _template in ordered_connections):
            raise ValueError("Every selected Flex Part must connect two adjacent global copper layers.")

        selected_pairs = [(top_number, bottom_number) for _slot, top_number, bottom_number, _template in ordered_connections]
        if len(set(selected_pairs)) != len(selected_pairs):
            raise ValueError("Two selected Flex Parts overlap the same copper-layer pair.")
        for (_top_a, bottom_a), (top_b, _bottom_b) in zip(selected_pairs, selected_pairs[1:]):
            if bottom_a >= top_b:
                raise ValueError("Selected Flex Part copper spans overlap.")

        master_copper_count = source_rigid.stackup.copper_count()
        first_flex_layer = ordered_connections[0][1]
        last_flex_layer = ordered_connections[-1][2]
        if first_flex_layer <= 1 or last_flex_layer >= master_copper_count:
            raise ValueError(
                "The selected Flex Parts must leave one rigid copper layer above and below the combined span."
            )

        mandatory_numbers = {
            first_flex_layer - 1,
            last_flex_layer + 1,
            *(
                layer_number
                for _slot, top_number, bottom_number, _template in ordered_connections
                for layer_number in (top_number, bottom_number)
            ),
        }
        minimum_copper_count = len(mandatory_numbers)
        desired_copper_count = target_copper_count or minimum_copper_count
        if (
            desired_copper_count < minimum_copper_count
            or desired_copper_count > master_copper_count
            or desired_copper_count % 2
        ):
            raise ValueError(
                f"This selection requires {minimum_copper_count} to {master_copper_count} even copper layers."
            )

        optional_numbers = [
            number
            for number in range(1, master_copper_count + 1)
            if number not in mandatory_numbers
        ]
        center = (first_flex_layer + last_flex_layer) / 2.0
        optional_numbers.sort(
            key=lambda number: (
                0 if first_flex_layer < number < last_flex_layer else 1,
                abs(number - center),
                number,
            )
        )
        chosen_numbers = sorted(
            mandatory_numbers
            | set(optional_numbers[: desired_copper_count - minimum_copper_count])
        )

        source_copper_indices = [
            index
            for index, layer in enumerate(source_rigid.stackup.layers)
            if isinstance(layer, CopperLayer)
        ]
        source_global_numbers = self._rigid_branch_global_numbers.get(id(source_rigid), [])
        if len(source_global_numbers) != len(source_copper_indices):
            source_global_numbers = list(range(1, len(source_copper_indices) + 1))
        source_copper_by_number = {
            number: deepcopy(source_rigid.stackup.layers[index])
            for number, index in zip(source_global_numbers, source_copper_indices)
        }
        source_gap_layers_by_pair = {
            (source_global_numbers[position], source_global_numbers[position + 1]): [
                deepcopy(layer)
                for layer in source_rigid.stackup.layers[top_index + 1 : bottom_index]
            ]
            for position, (top_index, bottom_index) in enumerate(
                zip(source_copper_indices, source_copper_indices[1:])
            )
        }
        if any(number not in source_copper_by_number for number in chosen_numbers):
            raise ValueError("The master rigid part does not contain all required global copper layers.")

        connection_by_pair = {
            (top_number, bottom_number): (slot_id, template)
            for slot_id, top_number, bottom_number, template in ordered_connections
        }
        copper_by_number = {
            number: deepcopy(source_copper_by_number[number]) for number in chosen_numbers
        }
        for _slot_id, top_number, bottom_number, template in ordered_connections:
            top_copper = copper_by_number[top_number]
            bottom_copper = copper_by_number[bottom_number]
            if isinstance(top_copper, CopperLayer):
                top_copper.thickness_mm = template.copper_thickness_top_mm
                top_copper.copper_type = template.copper_type
                top_copper.sync_roughness()
            if isinstance(bottom_copper, CopperLayer):
                bottom_copper.thickness_mm = template.copper_thickness_bottom_mm
                bottom_copper.copper_type = template.copper_type
                bottom_copper.sync_roughness()

        prepreg = source_rigid._default_dielectric("prepreg")
        rigid_core = self._rigid_core_template_for_slots(source_rigid)
        layers: list[object] = [copper_by_number[chosen_numbers[0]]]
        slot_map: dict[int, int] = {}
        gap_map: dict[int, int] = {}
        for gap_index, (top_number, bottom_number) in enumerate(
            zip(chosen_numbers, chosen_numbers[1:])
        ):
            connection = connection_by_pair.get((top_number, bottom_number))
            if connection is None:
                source_gap = source_gap_layers_by_pair.get(
                    (top_number, bottom_number),
                    [],
                )
                if source_gap and not any(
                    isinstance(layer, FlexCoreLayer) for layer in source_gap
                ):
                    layers.extend(deepcopy(source_gap))
                elif source_gap:
                    # This master flex span was not selected for the new rigid
                    # part, so it becomes a local rigid core.
                    layers.append(deepcopy(rigid_core))
                else:
                    # Nonconsecutive global copper layers have no single master
                    # gap to copy and require a synthesized bonding prepreg.
                    layers.append(deepcopy(prepreg))
            else:
                slot_id, template = connection
                slot_map[slot_id] = len(slot_map)
                gap_map[slot_id] = gap_index
                layers.append(deepcopy(template))
            layers.append(copper_by_number[bottom_number])

        if len(gap_map) != len(ordered_connections):
            raise ValueError("The combined rigid construction could not retain every selected Flex Part.")
        stackup = Stackup(
            mode="rigid",
            soldermask=deepcopy(source_rigid.stackup.soldermask),
            layers=layers,
        )
        return stackup, slot_map, chosen_numbers, gap_map, set(connections)

    @staticmethod
    def _generated_rigid_part_names(base_name: str, count: int) -> list[str]:
        if count <= 1:
            return [base_name]
        stem, separator, suffix = base_name.rpartition(" ")
        if separator and suffix.isdigit():
            start_number = int(suffix)
            prefix = stem
        else:
            start_number = 1
            prefix = base_name
        return [f"{prefix} {start_number + offset}" for offset in range(count)]

    def _add_rigid_part_interactive(self) -> None:
        current_editor = self._selected_visual_editor()
        parent_flexes = self._selected_flex_parts()
        if not parent_flexes and current_editor is not None and current_editor.is_flex_zone:
            parent_flexes = [current_editor]
        if not parent_flexes and (primary_flex := self._primary_flex_editor()) is not None:
            parent_flexes = [primary_flex]
        source_rigid = self._primary_rigid_editor()
        if not parent_flexes or source_rigid is None:
            QMessageBox.information(self, "Cannot add rigid part", "Create a rigid-flex stackup first.")
            return

        flex_part_labels: list[tuple[int, str]] = []
        flex_layer_counts: dict[int, int] = {}
        for index, candidate in enumerate(self._zone_editors):
            if not candidate.is_flex_zone:
                continue
            parent_rigid = self._parent_rigid_for_flex(candidate) or source_rigid
            numbers = self._flex_copper_number_overrides(parent_rigid, candidate)
            spans: list[str] = []
            for sandwich_index, _slot_id in enumerate(candidate.stackup.flex_sandwich_slot_ids()):
                top_number = numbers.get(sandwich_index * 3)
                bottom_number = numbers.get((sandwich_index * 3) + 2)
                if top_number is not None and bottom_number is not None:
                    spans.append(f"L{top_number}-L{bottom_number}")
            span_text = ", ".join(spans) if spans else "unmapped"
            flex_part_labels.append((index, f"{self.tabs.tabText(index)} ({span_text})"))
            flex_layer_counts[index] = candidate.stackup.copper_count()
        master_copper_count = source_rigid.stackup.copper_count()
        selected_flex_indices = {
            index
            for parent_flex in parent_flexes
            if (index := self._zone_index(parent_flex)) is not None
        }
        dialog = AddRigidPartDialog(
            flex_part_labels=flex_part_labels,
            flex_layer_counts=flex_layer_counts,
            master_layer_count=master_copper_count,
            suggested_name=f"Rigid Part {len(self._rigid_editors()) + 1}",
            default_selected_flex_indices=selected_flex_indices,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        target_copper_count = dialog.target_copper_count()
        parent_flexes = [
            self._zone_editors[index]
            for index in sorted(dialog.selected_flex_indices())
            if 0 <= index < len(self._zone_editors)
            and self._zone_editors[index].is_flex_zone
        ]
        if not parent_flexes:
            QMessageBox.warning(self, "Cannot add rigid part", "Select at least one Flex Part.")
            return
        created_editors: list[StackupEditorWindow] = []
        try:
            (
                stackup,
                slot_map,
                global_numbers,
                gap_map,
                coverage,
            ) = self._build_combined_rigid_branch(
                source_rigid,
                parent_flexes,
                target_copper_count=target_copper_count,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot add rigid part", str(exc))
            return

        editor = self._add_zone(kind="rigid", initialize_from_template=False)
        editor.replace_stackup(stackup, select_meta=("layer", 0))
        self._rigid_branch_coverage[id(editor)] = set(coverage)
        self._rigid_branch_slot_maps[id(editor)] = slot_map
        self._rigid_branch_global_numbers[id(editor)] = global_numbers
        self._rigid_flex_gap_by_slot[id(editor)] = gap_map
        for parent_flex in parent_flexes:
            self._register_rigid_parent(editor, parent_flex)
        prepared_parts = [(dialog.rigid_name(), editor)]

        for name, editor in prepared_parts:
            zone_index = self._zone_index(editor)
            if zone_index is not None:
                self.tabs.setTabText(zone_index, name)
            editor.zone_display_name = name
            self._disable_unsupported_zone_actions(editor)
            created_editors.append(editor)
        self._sync_all_rigid_zones()
        if created_editors:
            definition_editor = self._definition_source(created_editors[-1])
            definition_index = self._zone_index(definition_editor)
            if definition_index is not None:
                self.tabs.setCurrentIndex(definition_index)
            self._active_visual_editor = created_editors[-1]
            self._update_zone_controls()
            self._refresh_combined_preview()

    def _add_flex_part_interactive(self) -> None:
        rigid_editor = self._selected_visual_editor()
        if rigid_editor is None or rigid_editor.is_flex_zone:
            QMessageBox.information(
                self,
                "Select a rigid part",
                "Select the rigid part that the new Flex Part should follow.",
            )
            return
        if rigid_editor is self._primary_rigid_editor():
            QMessageBox.information(
                self,
                "Use Insert Flex Sandwich",
                "Each sandwich in the first flex region is created with Insert Flex Sandwich and receives its own Flex Part tab.",
            )
            return

        adjacent_flexes = self._adjacent_flex_editors(rigid_editor)
        selected_slot = self._selected_rigid_flex_slot(rigid_editor)
        if selected_slot is None:
            QMessageBox.information(
                self,
                "Select a Flex Core",
                "Select the Flex Core row for the copper-layer span that should continue into the new Flex Part.",
            )
            return

        incoming_source = self._incoming_flex_for_slots(
            rigid_editor,
            {selected_slot},
        )
        source_flex = incoming_source or next(
            (
                candidate
                for candidate in adjacent_flexes
                if selected_slot in candidate.stackup.active_flex_slot_ids()
                and selected_slot in self._covered_global_slots(rigid_editor, candidate)
            ),
            None,
        )
        if source_flex is None:
            primary_flex = self._primary_flex_editor()
            if (
                primary_flex is not None
                and selected_slot in primary_flex.stackup.active_flex_slot_ids()
            ):
                source_flex = primary_flex
        if source_flex is None or source_flex.stackup.coverlay is None:
            QMessageBox.information(
                self,
                "Cannot add Flex Part",
                "No compatible flex construction was found for the selected Flex Core span.",
            )
            return

        covered_slots = {selected_slot}
        flex_core_template = self._flex_slot_templates(source_flex).get(selected_slot)
        if flex_core_template is None:
            QMessageBox.information(
                self,
                "Cannot add Flex Part",
                "The selected rigid part does not contain a usable flex-core span.",
            )
            return
        duplicate = next(
            (
                candidate
                for candidate in self._zone_editors
                if candidate.is_flex_zone
                and self._parent_rigid_for_flex(candidate) is rigid_editor
                and candidate.stackup.active_flex_slot_ids() == set(covered_slots)
            ),
            None,
        )
        if duplicate is not None:
            QMessageBox.information(
                self,
                "Flex Part already exists",
                "This rigid part already has a flex instance on the selected copper-layer span.",
            )
            return

        new_stackup = build_flex_stackup_from_templates(
            flex_core_template=flex_core_template,
            coverlay=deepcopy(source_flex.stackup.coverlay),
            slot_indices=sorted(covered_slots),
            slot_capacity=source_flex.stackup.flex_slot_capacity_or_count(),
        )
        parent_index = self._zone_index(rigid_editor)
        editor = self._add_zone(
            kind="flex",
            initialize_from_template=False,
            insert_index=(parent_index + 1 if parent_index is not None else None),
        )
        editor.replace_stackup(new_stackup, select_meta=("layer", 1))
        self._register_flex_parent(editor, rigid_editor)
        existing_coverage = self._rigid_branch_coverage.get(id(rigid_editor))
        if existing_coverage is not None:
            self._rigid_branch_coverage[id(rigid_editor)] = set(existing_coverage) | set(covered_slots)
            existing_slot_map = self._rigid_branch_slot_maps.get(id(rigid_editor), {})
            if not existing_slot_map:
                first_slot = min(covered_slots)
                existing_slot_map = {
                    slot_id: slot_id - first_slot for slot_id in sorted(covered_slots)
                }
            self._rigid_branch_slot_maps[id(rigid_editor)] = existing_slot_map
        flex_name = f"Flex Part {len([item for item in self._zone_editors if item.is_flex_zone])}"
        zone_index = self._zone_index(editor)
        if zone_index is not None:
            self.tabs.setTabText(zone_index, flex_name)
        editor.zone_display_name = flex_name
        self._disable_unsupported_zone_actions(editor)
        if incoming_source is not None:
            self._bind_zone_definition(
                editor,
                self._definition_source(incoming_source),
            )
        self._sync_all_rigid_zones()
        definition_editor = self._definition_source(editor)
        definition_index = self._zone_index(definition_editor)
        if definition_index is not None:
            self.tabs.setCurrentIndex(definition_index)
        self._active_visual_editor = editor
        self._update_zone_controls()
        self._refresh_combined_preview()

    def _add_zone(
        self,
        *,
        kind: str | None = None,
        initialize_from_template: bool = True,
        insert_index: int | None = None,
    ) -> StackupEditorWindow:
        position = (
            len(self._zone_editors)
            if insert_index is None
            else max(0, min(insert_index, len(self._zone_editors)))
        )
        kind = kind or zone_kind_for_position(position)
        logger.info("Adding %s zone at position %s", kind, position)

        editor = self._make_zone_editor(kind)
        if position >= MIN_ZONES and initialize_from_template:
            self._zone_editors.append(editor)
            self._initialize_new_zone_from_template(editor, kind)
            self._zone_editors.pop()
        central = editor.centralWidget()
        central.setParent(None)

        label = self._zone_label(kind, position)
        self.tabs.insertTab(position, central, label)
        self._zone_editors.insert(position, editor)
        if kind == "rigid":
            self._rigid_branch_coverage[id(editor)] = None
            self._rigid_branch_slot_maps[id(editor)] = {}
            self._rigid_branch_global_numbers[id(editor)] = list(range(1, editor.stackup.copper_count() + 1))
            self._rigid_flex_gap_by_slot[id(editor)] = {}
        elif len([item for item in self._zone_editors if item.is_flex_zone]) == 1:
            primary_rigid = self._primary_rigid_editor()
            if primary_rigid is not None:
                self._register_flex_parent(editor, primary_rigid)
        self.tabs.setCurrentIndex(position)

        self._refresh_flex_tab_selection_visuals()
        self._update_zone_controls()
        if position >= MIN_ZONES and initialize_from_template:
            self._sync_all_rigid_zones()
        self._refresh_combined_preview()
        self._sync_command_menu_state()
        return editor

    def _remove_zone(self) -> None:
        if len(self._zone_editors) <= MIN_ZONES:
            return
        selected_editor = self._selected_visual_editor()
        index = self._zone_index(selected_editor) if selected_editor is not None else None
        if index is None or index < 0 or index >= len(self._zone_editors):
            return
        editor = self._zone_editors[index]
        if editor is self._primary_rigid_editor():
            return
        if editor.is_flex_zone and not self._can_remove_flex_part(editor):
            connected_rigid_parts = [
                rigid_editor.zone_display_name
                for rigid_id in self._flex_child_rigids.get(id(editor), set())
                if (rigid_editor := self._editor_by_id(rigid_id)) is not None
            ]
            QMessageBox.information(
                self,
                "Cannot remove Flex Part",
                (
                    "Remove the connected rigid part(s) first: "
                    + ", ".join(sorted(connected_rigid_parts))
                    if connected_rigid_parts
                    else "At least one Flex Part must remain in a rigid-flex project."
                ),
            )
            return

        affected_rigid_ids: set[int] = set()
        if editor.is_flex_zone:
            removed_flex_id = id(editor)
            parent_rigid_id = self._flex_parent_rigid.get(removed_flex_id)
            if parent_rigid_id is not None:
                affected_rigid_ids.add(parent_rigid_id)
            for child_id in list(self._flex_child_rigids.get(removed_flex_id, set())):
                affected_rigid_ids.add(child_id)
                parent_ids = self._rigid_parent_flexes.get(child_id, set())
                parent_ids.discard(removed_flex_id)

        self._release_zone_definition_for_removal(editor)
        self._selected_flex_part_ids.discard(id(editor))
        editor = self._zone_editors.pop(index)
        parent_flex_ids = self._rigid_parent_flexes.pop(id(editor), set())
        for parent_flex_id in parent_flex_ids:
            self._flex_child_rigids.get(parent_flex_id, set()).discard(id(editor))
        self._flex_parent_rigid.pop(id(editor), None)
        self._flex_child_rigids.pop(id(editor), None)
        self._rigid_branch_coverage.pop(id(editor), None)
        self._rigid_branch_slot_maps.pop(id(editor), None)
        self._rigid_branch_global_numbers.pop(id(editor), None)
        self._rigid_flex_gap_by_slot.pop(id(editor), None)
        if editor.is_flex_zone:
            self._flex_sandwich_history.pop(self._flex_history_key(editor), None)
        widget = self.tabs.widget(index)
        self.tabs.removeTab(index)
        if widget is not None:
            widget.setParent(None)
        editor.deleteLater()
        self._active_visual_editor = self._current_zone_editor()
        logger.info("Removed zone at position %s", index)

        for rigid_id in affected_rigid_ids:
            rigid_editor = self._editor_by_id(rigid_id)
            if rigid_editor is not None:
                self._recompute_rigid_connection_coverage(rigid_editor)

        self._update_zone_controls()
        if len(self._zone_editors) >= MIN_ZONES:
            self._sync_all_rigid_zones()
        self._refresh_combined_preview()
        self._sync_command_menu_state()

    def _zone_label(self, kind: str, position: int) -> str:
        _ = position
        same_kind_count = sum(
            1 for existing in self._zone_editors if existing.is_flex_zone == (kind == "flex")
        ) + 1
        title = "Rigid" if kind == "rigid" else "Flex"
        return f"{title} zone {same_kind_count}"

    def _update_zone_controls(self) -> None:
        zone_count = len(self._zone_editors)
        current_editor = self._selected_visual_editor()
        current_index = self._zone_index(current_editor) if current_editor is not None else None
        selected_flex_parts = self._selected_flex_parts()
        rigid_part_selected = (
            current_editor is not None
            and not current_editor.is_flex_zone
        )
        inspecting_rigid_box = (
            self._rigid_box_inspection_active
            and rigid_part_selected
            and current_editor is not self._primary_rigid_editor()
        )
        focused_rigid_box = (
            inspecting_rigid_box
            and self.combined_preview.focused_rigid_zone_index is not None
        )
        self.add_zone_button.setEnabled(
            bool(selected_flex_parts) and not rigid_part_selected
        )
        if rigid_part_selected:
            self.add_zone_button.setToolTip(
                "Select one or more Flex Parts before adding another rigid part"
            )
        else:
            self.add_zone_button.setToolTip(
                f"Add one rigid part connected to {len(selected_flex_parts)} selected Flex Part"
                f"{'s' if len(selected_flex_parts) != 1 else ''}. Ctrl+click Flex Part tabs to select multiple."
            )
        can_add_flex_part = (
            current_editor is not None
            and not current_editor.is_flex_zone
            and current_editor is not self._primary_rigid_editor()
        )
        self.add_flex_part_button.setEnabled(can_add_flex_part)
        self.add_flex_part_button.setToolTip(
            "Use Insert Flex Sandwich for the master rigid zone; Add Flex Part is for downstream rigid parts"
        )
        can_remove = False
        if zone_count > MIN_ZONES and current_editor is not None:
            if current_editor.is_flex_zone:
                can_remove = self._can_remove_flex_part(current_editor)
                self.remove_zone_button.setText("Remove Flex Part")
                self.remove_zone_button.setToolTip(
                    "Remove this Flex Part while preserving valid downstream connections"
                )
            else:
                can_remove = (
                    not focused_rigid_box
                    and current_index is not None
                    and current_index >= MIN_ZONES
                    and not any(
                        parent_id == id(current_editor)
                        for parent_id in self._flex_parent_rigid.values()
                    )
                )
                self.remove_zone_button.setText("Remove Rigid Part")
                self.remove_zone_button.setToolTip(
                    "Shrink to the main overview before removing this part"
                    if focused_rigid_box
                    else "Remove the selected additional rigid part"
                )
        else:
            self.remove_zone_button.setText("Remove Part")
        self.remove_zone_button.setEnabled(can_remove)

    def _can_remove_flex_part(self, flex_editor: StackupEditorWindow) -> bool:
        if not flex_editor.is_flex_zone:
            return False
        return (
            len([editor for editor in self._zone_editors if editor.is_flex_zone]) > 1
            and not self._flex_child_rigids.get(id(flex_editor), set())
        )

    def _primary_rigid_editor(self) -> StackupEditorWindow | None:
        return next(
            (
                editor
                for editor in self._zone_editors
                if not editor.is_flex_zone
                and id(editor) not in self._definition_source_by_alias
            ),
            None,
        )

    def _primary_flex_editor(self) -> StackupEditorWindow | None:
        return next(
            (
                editor
                for editor in self._zone_editors
                if editor.is_flex_zone
                and id(editor) not in self._definition_source_by_alias
            ),
            None,
        )

    def _rigid_editors(self) -> list[StackupEditorWindow]:
        return [editor for editor in self._zone_editors if not editor.is_flex_zone]

    def _editor_by_id(self, editor_id: int | None) -> StackupEditorWindow | None:
        if editor_id is None:
            return None
        return next((editor for editor in self._zone_editors if id(editor) == editor_id), None)

    def _register_flex_parent(
        self,
        flex_editor: StackupEditorWindow,
        rigid_editor: StackupEditorWindow,
    ) -> None:
        self._flex_parent_rigid[id(flex_editor)] = id(rigid_editor)
        self._flex_child_rigids.setdefault(id(flex_editor), set())

    def _register_rigid_parent(
        self,
        rigid_editor: StackupEditorWindow,
        flex_editor: StackupEditorWindow,
    ) -> None:
        self._rigid_parent_flexes.setdefault(id(rigid_editor), set()).add(id(flex_editor))
        self._flex_child_rigids.setdefault(id(flex_editor), set()).add(id(rigid_editor))

    def _parent_rigid_for_flex(self, flex_editor: StackupEditorWindow) -> StackupEditorWindow | None:
        return self._editor_by_id(self._flex_parent_rigid.get(id(flex_editor)))

    def _parent_flex_for_rigid(self, rigid_editor: StackupEditorWindow) -> StackupEditorWindow | None:
        return next(iter(self._parent_flexes_for_rigid(rigid_editor)), None)

    def _parent_flexes_for_rigid(self, rigid_editor: StackupEditorWindow) -> list[StackupEditorWindow]:
        parent_ids = self._rigid_parent_flexes.get(id(rigid_editor), set())
        return [
            editor
            for editor in self._zone_editors
            if editor.is_flex_zone and id(editor) in parent_ids
        ]

    def _incoming_flex_for_slots(
        self,
        rigid_editor: StackupEditorWindow,
        slot_ids: set[int],
    ) -> StackupEditorWindow | None:
        if not slot_ids:
            return None
        return next(
            (
                flex_editor
                for flex_editor in self._parent_flexes_for_rigid(rigid_editor)
                if slot_ids <= flex_editor.stackup.active_flex_slot_ids()
                and slot_ids <= self._covered_global_slots(rigid_editor, flex_editor)
            ),
            None,
        )

    def _parallel_flex_editors(self, flex_editor: StackupEditorWindow) -> list[StackupEditorWindow]:
        parent_rigid = self._parent_rigid_for_flex(flex_editor)
        if parent_rigid is None:
            return [flex_editor]
        upstream_signature = frozenset(self._rigid_parent_flexes.get(id(parent_rigid), set()))
        parallel: list[StackupEditorWindow] = []
        for candidate in self._zone_editors:
            if not candidate.is_flex_zone:
                continue
            candidate_parent = self._parent_rigid_for_flex(candidate)
            if candidate_parent is None:
                continue
            candidate_signature = frozenset(
                self._rigid_parent_flexes.get(id(candidate_parent), set())
            )
            if candidate_signature == upstream_signature:
                parallel.append(candidate)
        return parallel or [flex_editor]

    def _zone_index(self, editor: StackupEditorWindow) -> int | None:
        try:
            return self._zone_editors.index(editor)
        except ValueError:
            return None

    def _covered_global_slots(
        self,
        rigid_editor: StackupEditorWindow,
        flex_editor: StackupEditorWindow,
    ) -> set[int]:
        active_slots = flex_editor.stackup.active_flex_slot_ids()
        coverage = self._rigid_branch_coverage.get(id(rigid_editor))
        return set(active_slots) if coverage is None else set(coverage) & set(active_slots)

    def _branch_slot_map(
        self,
        rigid_editor: StackupEditorWindow,
        flex_editor: StackupEditorWindow,
    ) -> dict[int, int]:
        if self._rigid_branch_coverage.get(id(rigid_editor)) is None:
            return {
                slot_id: slot_id
                for slot_id in range(flex_editor.stackup.flex_slot_capacity_or_count())
            }
        stored = self._rigid_branch_slot_maps.get(id(rigid_editor)) or {}
        if stored:
            return dict(stored)
        return {
            slot_id: slot_id
            for slot_id in range(flex_editor.stackup.flex_slot_capacity_or_count())
        }

    def _branch_slot_capacity(
        self,
        rigid_editor: StackupEditorWindow,
        flex_editor: StackupEditorWindow,
    ) -> int:
        slot_map = self._branch_slot_map(rigid_editor, flex_editor)
        return max(slot_map.values(), default=-1) + 1

    @staticmethod
    def _copper_gap_for_layer_index(stackup: Stackup, layer_index: int) -> int | None:
        copper_indices = [
            index for index, layer in enumerate(stackup.layers) if isinstance(layer, CopperLayer)
        ]
        for gap_index, (top_index, bottom_index) in enumerate(zip(copper_indices, copper_indices[1:])):
            if top_index < layer_index < bottom_index:
                return gap_index
        return None

    @staticmethod
    def _gap_layer_indices(stackup: Stackup, gap_index: int) -> list[int]:
        copper_indices = [
            index for index, layer in enumerate(stackup.layers) if isinstance(layer, CopperLayer)
        ]
        if not 0 <= gap_index < len(copper_indices) - 1:
            return []
        return list(range(copper_indices[gap_index] + 1, copper_indices[gap_index + 1]))

    def _flex_gap_indices(self, rigid_editor: StackupEditorWindow) -> list[int]:
        gaps: list[int] = []
        for layer_index, layer in enumerate(rigid_editor.stackup.layers):
            if not isinstance(layer, FlexCoreLayer):
                continue
            gap_index = self._copper_gap_for_layer_index(rigid_editor.stackup, layer_index)
            if gap_index is not None and gap_index not in gaps:
                gaps.append(gap_index)
        return sorted(gaps)

    def _ensure_rigid_flex_gap_map(
        self,
        rigid_editor: StackupEditorWindow,
    ) -> dict[int, int]:
        mapping = self._rigid_flex_gap_by_slot.setdefault(id(rigid_editor), {})
        active_slots = sorted(
            {
                slot_id
                for flex_editor in self._adjacent_flex_editors(rigid_editor)
                for slot_id in flex_editor.stackup.active_flex_slot_ids()
            }
        )
        available_gaps = [
            gap_index
            for gap_index in self._flex_gap_indices(rigid_editor)
            if gap_index not in mapping.values()
        ]
        for slot_id, gap_index in zip(
            [slot_id for slot_id in active_slots if slot_id not in mapping],
            available_gaps,
        ):
            mapping[slot_id] = gap_index
        return mapping

    def _remap_rigid_flex_gaps_after_structure_change(
        self,
        rigid_editor: StackupEditorWindow,
    ) -> None:
        """Keep flex slots attached to their physical Flex Core after copper edits."""
        previous = dict(self._rigid_flex_gap_by_slot.get(id(rigid_editor), {}))
        active_slots = {
            slot_id
            for flex_editor in self._adjacent_flex_editors(rigid_editor)
            for slot_id in self._covered_global_slots(rigid_editor, flex_editor)
        }
        physical_gaps = self._flex_gap_indices(rigid_editor)
        if not active_slots and not physical_gaps:
            self._rigid_flex_gap_by_slot[id(rigid_editor)] = {}
            return
        if len(active_slots) != len(physical_gaps):
            # Do not invent a connection if the edited model is already
            # inconsistent. The subsequent structural validation can then
            # report the mismatch without moving an existing sandwich.
            return

        ordered_slots = sorted(
            active_slots,
            key=lambda slot_id: (previous.get(slot_id, len(rigid_editor.stackup.layers)), slot_id),
        )
        self._rigid_flex_gap_by_slot[id(rigid_editor)] = dict(
            zip(ordered_slots, physical_gaps)
        )

    def _rigid_pair_for_global_slot(
        self,
        rigid_editor: StackupEditorWindow,
        flex_editor: StackupEditorWindow,
        global_slot: int,
    ) -> tuple[int, int] | None:
        gap_index = self._ensure_rigid_flex_gap_map(rigid_editor).get(global_slot)
        copper_indices = [
            index
            for index, layer in enumerate(rigid_editor.stackup.layers)
            if isinstance(layer, CopperLayer)
        ]
        if gap_index is not None and 0 <= gap_index < len(copper_indices) - 1:
            return copper_indices[gap_index], copper_indices[gap_index + 1]
        local_slot = self._branch_slot_map(rigid_editor, flex_editor).get(global_slot, global_slot)
        try:
            return rigid_slot_copper_indices(
                rigid_editor.stackup,
                self._branch_slot_capacity(rigid_editor, flex_editor),
                local_slot,
            )
        except ValueError:
            return None

    def _gap_contains_prepreg(
        self,
        rigid_editor: StackupEditorWindow,
        gap_index: int,
    ) -> bool:
        return any(
            isinstance(rigid_editor.stackup.layers[index], DielectricLayer)
            and is_prepreg_dielectric_type(
                rigid_editor.stackup.layers[index].dielectric_type
            )
            for index in self._gap_layer_indices(rigid_editor.stackup, gap_index)
        )

    def _flex_insertion_eligibility(
        self,
        rigid_editor: StackupEditorWindow,
        layer_index: int,
    ) -> tuple[bool, str, int | None]:
        if rigid_editor.is_flex_zone or not 0 <= layer_index < len(rigid_editor.stackup.layers):
            return False, "Select a rigid dielectric material.", None
        layer = rigid_editor.stackup.layers[layer_index]
        if not isinstance(layer, DielectricLayer):
            return False, "Select a rigid core or prepreg material between two copper layers.", None
        gap_index = self._copper_gap_for_layer_index(rigid_editor.stackup, layer_index)
        if gap_index is None:
            return False, "The selected material is not between two copper layers.", None

        flex_gaps = self._flex_gap_indices(rigid_editor)
        if gap_index in flex_gaps:
            return False, "This copper pair already contains a flex core.", gap_index
        if any(abs(existing_gap - gap_index) == 1 for existing_gap in flex_gaps):
            return (
                False,
                "Two flex cores cannot be adjacent. Keep at least one prepreg gap between them.",
                gap_index,
            )
        for existing_gap in flex_gaps:
            first_gap, last_gap = sorted((existing_gap, gap_index))
            if not any(
                self._gap_contains_prepreg(rigid_editor, intermediate_gap)
                for intermediate_gap in range(first_gap + 1, last_gap)
            ):
                return (
                    False,
                    "At least one prepreg must remain between the existing and new flex cores.",
                    gap_index,
                )

        new_flex_copper_count = (len(flex_gaps) + 1) * 2
        if new_flex_copper_count >= rigid_editor.stackup.copper_count():
            return (
                False,
                "Connected flex copper layers must remain fewer than the rigid copper layers.",
                gap_index,
            )
        return True, "", gap_index

    def _rebuild_rigid_from_explicit_flex_gaps(
        self,
        rigid_editor: StackupEditorWindow,
        active_templates: dict[int, FlexCoreLayer],
    ) -> Stackup:
        source = rigid_editor.stackup
        copper_indices = [
            index for index, layer in enumerate(source.layers) if isinstance(layer, CopperLayer)
        ]
        if len(copper_indices) < 2:
            return deepcopy(source)

        gap_by_slot = self._ensure_rigid_flex_gap_map(rigid_editor)
        slot_by_gap = {
            gap_index: slot_id
            for slot_id, gap_index in gap_by_slot.items()
            if slot_id in active_templates
        }
        rigid_core = self._rigid_core_template_for_slots(rigid_editor)
        prepreg = rigid_editor._default_dielectric("prepreg")
        copper_layers = [deepcopy(source.layers[index]) for index in copper_indices]
        gap_layers: list[list[object]] = []

        for gap_index, (top_index, bottom_index) in enumerate(zip(copper_indices, copper_indices[1:])):
            slot_id = slot_by_gap.get(gap_index)
            template = active_templates.get(slot_id) if slot_id is not None else None
            existing = [deepcopy(layer) for layer in source.layers[top_index + 1 : bottom_index]]
            if template is not None:
                gap_layers.append([deepcopy(template)])
                top_copper = copper_layers[gap_index]
                bottom_copper = copper_layers[gap_index + 1]
                if isinstance(top_copper, CopperLayer):
                    top_copper.thickness_mm = template.copper_thickness_top_mm
                    top_copper.copper_type = template.copper_type
                    top_copper.sync_roughness()
                if isinstance(bottom_copper, CopperLayer):
                    bottom_copper.thickness_mm = template.copper_thickness_bottom_mm
                    bottom_copper.copper_type = template.copper_type
                    bottom_copper.sync_roughness()
            elif any(isinstance(layer, FlexCoreLayer) for layer in existing):
                gap_layers.append([deepcopy(rigid_core)])
            else:
                gap_layers.append(existing or [deepcopy(prepreg)])

        # A flex core must be laminated against rigid prepreg on both sides.
        # Preserve the complete existing construction when the gap already
        # contains ordinary or No-Flow prepreg. Only a gap with no prepreg at
        # all needs its non-prepreg materials converted to default Rigid PP.
        active_flex_gaps = set(slot_by_gap)
        for flex_gap in active_flex_gaps:
            for neighboring_gap in (flex_gap - 1, flex_gap + 1):
                if not 0 <= neighboring_gap < len(gap_layers):
                    continue
                materials = gap_layers[neighboring_gap]
                if any(isinstance(layer, FlexCoreLayer) for layer in materials):
                    continue
                if any(
                    isinstance(layer, DielectricLayer)
                    and is_prepreg_dielectric_type(layer.dielectric_type)
                    for layer in materials
                ):
                    continue
                converted_materials = [
                    (
                        deepcopy(layer)
                        if isinstance(layer, DielectricLayer)
                        and is_prepreg_dielectric_type(layer.dielectric_type)
                        else deepcopy(prepreg)
                    )
                    for layer in materials
                ]
                gap_layers[neighboring_gap] = converted_materials or [deepcopy(prepreg)]

        previous_core_kind: str | None = None
        for gap_index, materials in enumerate(gap_layers):
            current_core_kind = next(
                (
                    layer.dielectric_type
                    for layer in materials
                    if isinstance(layer, DielectricLayer)
                    and layer.dielectric_type == "core"
                ),
                None,
            )
            if any(isinstance(layer, FlexCoreLayer) for layer in materials):
                current_core_kind = "flex_core"
            if (
                previous_core_kind is not None
                and current_core_kind is not None
            ):
                gap_layers[gap_index] = [deepcopy(prepreg)]
                current_core_kind = None
            previous_core_kind = current_core_kind

        rebuilt_layers: list[object] = [
            deepcopy(layer) for layer in source.layers[: copper_indices[0]]
        ]
        rebuilt_layers.append(copper_layers[0])
        for gap_index, materials in enumerate(gap_layers):
            rebuilt_layers.extend(materials)
            rebuilt_layers.append(copper_layers[gap_index + 1])
        rebuilt_layers.extend(
            deepcopy(layer) for layer in source.layers[copper_indices[-1] + 1 :]
        )
        return Stackup(
            mode="rigid",
            soldermask=deepcopy(source.soldermask),
            layers=rebuilt_layers,
        )

    def _branch_global_number_overrides(self, rigid_editor: StackupEditorWindow) -> dict[int, int]:
        global_numbers = self._rigid_branch_global_numbers.get(id(rigid_editor), [])
        copper_indices = [
            index for index, layer in enumerate(rigid_editor.stackup.layers) if isinstance(layer, CopperLayer)
        ]
        if len(global_numbers) != len(copper_indices):
            return {}
        return {index: number for index, number in zip(copper_indices, global_numbers)}

    def _adjacent_rigid_editors(self, flex_editor: StackupEditorWindow) -> list[StackupEditorWindow]:
        adjacent: list[StackupEditorWindow] = []
        parent = self._parent_rigid_for_flex(flex_editor)
        if parent is not None:
            adjacent.append(parent)
        child_ids = self._flex_child_rigids.get(id(flex_editor), set())
        adjacent.extend(
            editor
            for editor in self._zone_editors
            if not editor.is_flex_zone and id(editor) in child_ids and editor not in adjacent
        )
        return adjacent

    def _adjacent_flex_editors(self, rigid_editor: StackupEditorWindow) -> list[StackupEditorWindow]:
        if rigid_editor not in self._zone_editors:
            return []
        adjacent: list[StackupEditorWindow] = []
        adjacent.extend(self._parent_flexes_for_rigid(rigid_editor))
        adjacent.extend(
            editor
            for editor in self._zone_editors
            if editor.is_flex_zone
            and self._flex_parent_rigid.get(id(editor)) == id(rigid_editor)
            and editor not in adjacent
        )
        return adjacent

    def _recompute_rigid_connection_coverage(self, rigid_editor: StackupEditorWindow) -> None:
        adjacent_flexes = self._adjacent_flex_editors(rigid_editor)
        connected_slots = {
            slot_id
            for flex_editor in adjacent_flexes
            for slot_id in flex_editor.stackup.active_flex_slot_ids()
        }
        previous_coverage = self._rigid_branch_coverage.get(id(rigid_editor))
        if previous_coverage is None:
            new_coverage = connected_slots
        else:
            new_coverage = set(previous_coverage) & connected_slots
        self._rigid_branch_coverage[id(rigid_editor)] = set(new_coverage)

    def _shared_region_bounds(
        self,
        rigid_editor: StackupEditorWindow,
        flex_editor: StackupEditorWindow,
    ) -> tuple[int, int] | None:
        try:
            return rigid_shared_region_bounds_for_capacity(
                rigid_editor.stackup,
                self._branch_slot_capacity(rigid_editor, flex_editor),
            )
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
        locked_copper: set[int] = set()
        locked_dielectric: set[int] = set()
        for global_slot in self._covered_global_slots(rigid_editor, flex_editor):
            pair = self._rigid_pair_for_global_slot(
                rigid_editor,
                flex_editor,
                global_slot,
            )
            if pair is None:
                continue
            top_index, bottom_index = pair
            locked_copper.update((top_index, bottom_index))
            core_index = next(
                (
                    index
                    for index in range(top_index + 1, bottom_index)
                    if isinstance(rigid_editor.stackup.layers[index], FlexCoreLayer)
                ),
                None,
            )
            if core_index is not None:
                locked_dielectric.add(core_index)
        return locked_copper, locked_dielectric

    def _flex_copper_number_overrides(
        self,
        rigid_editor: StackupEditorWindow,
        flex_editor: StackupEditorWindow,
    ) -> dict[int, int]:
        mapping: dict[int, int] = {}
        rigid_number_overrides = self._branch_global_number_overrides(rigid_editor)
        for index, layer in enumerate(flex_editor.stackup.layers):
            if not isinstance(layer, CopperLayer):
                continue
            global_slot = flex_editor.stackup.flex_slot_for_layer_index(index)
            pair = self._rigid_pair_for_global_slot(
                rigid_editor,
                flex_editor,
                global_slot,
            )
            if pair is None:
                continue
            top_index, bottom_index = pair
            rigid_index = top_index if index % 3 == 0 else bottom_index
            global_number = rigid_number_overrides.get(rigid_index)
            if global_number is not None:
                mapping[index] = global_number

        if mapping:
            return mapping

        rigid_total_copper = rigid_editor.stackup.copper_count()
        slot_capacity = flex_editor.stackup.flex_slot_capacity_or_count()
        start_number = ((rigid_total_copper - (slot_capacity * 2)) // 2) + 1
        for index, layer in enumerate(flex_editor.stackup.layers):
            if isinstance(layer, CopperLayer):
                slot_id = flex_editor.stackup.flex_slot_for_layer_index(index)
                mapping[index] = start_number + (slot_id * 2) + (0 if index % 3 == 0 else 1)
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

    def _selected_rigid_flex_slot(
        self,
        rigid_editor: StackupEditorWindow,
    ) -> int | None:
        meta = rigid_editor._current_row_meta()
        if not isinstance(meta, tuple) or len(meta) != 2 or meta[0] != "layer":
            return None
        layer_index = int(meta[1])
        if not 0 <= layer_index < len(rigid_editor.stackup.layers):
            return None
        if not isinstance(rigid_editor.stackup.layers[layer_index], FlexCoreLayer):
            return None
        gap_index = self._copper_gap_for_layer_index(rigid_editor.stackup, layer_index)
        if gap_index is None:
            return None
        return next(
            (
                slot_id
                for slot_id, mapped_gap in self._ensure_rigid_flex_gap_map(rigid_editor).items()
                if mapped_gap == gap_index
            ),
            None,
        )

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
            if is_prepreg_dielectric_type(layer.dielectric_type):
                if boundary_candidate is None and index in {start - 1, end + 1}:
                    boundary_candidate = deepcopy(layer)
                if bridge_candidate is None and start <= index <= end:
                    bridge_candidate = deepcopy(layer)

        boundary = boundary_candidate or bridge_candidate or deepcopy(default_prepreg)
        bridge = bridge_candidate or boundary_candidate or deepcopy(default_prepreg)
        return deepcopy(boundary), deepcopy(bridge)

    def _slot_capacity_for_rigid_zone(self, rigid_editor: StackupEditorWindow) -> int:
        adjacent_flexes = self._adjacent_flex_editors(rigid_editor)
        capacities = [
            self._branch_slot_capacity(rigid_editor, editor)
            for editor in adjacent_flexes
            if self._branch_slot_capacity(rigid_editor, editor) > 0
        ]
        return max(capacities, default=0)

    def _compact_flex_slot_layout_if_possible(self, flex_editor: StackupEditorWindow) -> bool:
        if any(
            self._rigid_flex_gap_by_slot.get(id(rigid_editor))
            for rigid_editor in self._adjacent_rigid_editors(flex_editor)
        ):
            # Explicit material-selected gaps are physical placement data.
            # Renumbering a flex slot must never move one of those sandwiches.
            return False
        parent_rigid = self._parent_rigid_for_flex(flex_editor)
        if parent_rigid is not None and any(
            candidate is not flex_editor
            and candidate.is_flex_zone
            and self._parent_rigid_for_flex(candidate) is parent_rigid
            for candidate in self._zone_editors
        ):
            # Slot ids are global positions shared by independent Flex Part
            # definitions. Compacting one sibling alone would collapse two
            # physical sandwiches onto the same copper pair.
            return False
        current_slots = flex_editor.stackup.flex_sandwich_slot_ids()
        current_capacity = flex_editor.stackup.flex_slot_capacity_or_count()
        sandwich_count = len(current_slots)
        if sandwich_count <= 0 or current_capacity <= sandwich_count:
            return False

        adjacent_rigids = self._adjacent_rigid_editors(flex_editor)
        if not adjacent_rigids:
            return False
        reference_rigids = [
            editor
            for editor in adjacent_rigids
            if self._rigid_branch_coverage.get(id(editor)) is None
        ]
        if not reference_rigids:
            return False

        for candidate_capacity in range(sandwich_count, current_capacity):
            for candidate_slots in combinations(range(candidate_capacity), sandwich_count):
                preserves_physical_pairs = True
                for rigid_editor in reference_rigids:
                    for old_slot, candidate_slot in zip(current_slots, candidate_slots):
                        try:
                            old_top_index, old_bottom_index = rigid_slot_copper_indices(
                                rigid_editor.stackup,
                                current_capacity,
                                old_slot,
                            )
                            new_top_index, new_bottom_index = rigid_slot_copper_indices(
                                rigid_editor.stackup,
                                candidate_capacity,
                                candidate_slot,
                            )
                        except (IndexError, ValueError):
                            preserves_physical_pairs = False
                            break

                        old_top = rigid_editor.stackup.layers[old_top_index]
                        old_bottom = rigid_editor.stackup.layers[old_bottom_index]
                        new_top = rigid_editor.stackup.layers[new_top_index]
                        new_bottom = rigid_editor.stackup.layers[new_bottom_index]
                        if not (
                            isinstance(old_top, CopperLayer)
                            and isinstance(old_bottom, CopperLayer)
                            and isinstance(new_top, CopperLayer)
                            and isinstance(new_bottom, CopperLayer)
                            and old_top.uid == new_top.uid
                            and old_bottom.uid == new_bottom.uid
                        ):
                            preserves_physical_pairs = False
                            break
                    if not preserves_physical_pairs:
                        break

                if preserves_physical_pairs:
                    slot_remap = dict(zip(current_slots, candidate_slots))
                    flex_editor.stackup.flex_sandwich_slots = list(candidate_slots)
                    flex_editor.stackup.flex_slot_capacity = candidate_capacity
                    for rigid_editor in adjacent_rigids:
                        coverage = self._rigid_branch_coverage.get(id(rigid_editor))
                        if coverage is None:
                            self._rigid_branch_slot_maps[id(rigid_editor)] = {
                                slot_id: slot_id for slot_id in range(candidate_capacity)
                            }
                            continue
                        self._rigid_branch_coverage[id(rigid_editor)] = {
                            slot_remap[slot_id]
                            for slot_id in coverage
                            if slot_id in slot_remap
                        }
                        old_slot_map = self._rigid_branch_slot_maps.get(id(rigid_editor), {})
                        self._rigid_branch_slot_maps[id(rigid_editor)] = {
                            slot_remap[global_slot]: local_slot
                            for global_slot, local_slot in old_slot_map.items()
                            if global_slot in slot_remap
                        }
                    return True
        return False

    def _sync_all_rigid_zones(self) -> None:
        if self._rigid_sync_in_progress:
            return
        self._rigid_sync_in_progress = True
        try:
            self._sync_all_rigid_zones_impl()
        finally:
            self._rigid_sync_in_progress = False

    def _sync_all_rigid_zones_impl(self) -> None:
        for rigid_editor in self._rigid_editors():
            adjacent_flexes = self._adjacent_flex_editors(rigid_editor)
            explicit_gap_map = self._rigid_flex_gap_by_slot.get(id(rigid_editor), {})
            if explicit_gap_map:
                active_templates: dict[int, FlexCoreLayer] = {}
                for flex_editor in adjacent_flexes:
                    templates = self._flex_slot_templates(flex_editor)
                    for slot_id in self._covered_global_slots(rigid_editor, flex_editor):
                        template = templates.get(slot_id)
                        if template is not None and slot_id in explicit_gap_map:
                            active_templates.setdefault(slot_id, deepcopy(template))
                selected_meta = rigid_editor._current_row_meta() or ("layer", 0)
                rebuilt = self._rebuild_rigid_from_explicit_flex_gaps(
                    rigid_editor,
                    active_templates,
                )
                rigid_editor.replace_stackup(rebuilt, select_meta=selected_meta)
                self._configure_rigid_zone(
                    rigid_editor,
                    zone_display_name=rigid_editor.zone_display_name,
                )
                continue
            if not adjacent_flexes:
                selected_meta = rigid_editor._current_row_meta() or ("layer", 0)
                local_capacity = max(
                    self._rigid_branch_slot_maps.get(id(rigid_editor), {}).values(),
                    default=-1,
                ) + 1
                if local_capacity > 0:
                    outer_template, bridge_template = self._prepreg_templates_for_slot_capacity(
                        rigid_editor,
                        local_capacity,
                    )
                    rigidized = rebuild_rigid_stackup_from_slot_activity(
                        rigid_editor.stackup,
                        slot_capacity=local_capacity,
                        active_slot_ids=set(),
                        slot_templates={},
                        rigid_core_template=self._rigid_core_template_for_slots(rigid_editor),
                        bridge_dielectric_template=bridge_template,
                        outer_boundary_dielectric_template=outer_template,
                    )
                else:
                    rigidized = deepcopy(rigid_editor.stackup)
                    rigid_core = self._rigid_core_template_for_slots(rigid_editor)
                    rigidized.layers = [
                        deepcopy(rigid_core) if isinstance(layer, FlexCoreLayer) else layer
                        for layer in rigidized.layers
                    ]
                rigid_editor.replace_stackup(rigidized, select_meta=selected_meta)
                continue

            if self._rigid_branch_coverage.get(id(rigid_editor)) is None:
                primary_flex = adjacent_flexes[0]
                self._rigid_branch_slot_maps[id(rigid_editor)] = {
                    slot_id: slot_id
                    for slot_id in range(primary_flex.stackup.flex_slot_capacity_or_count())
                }

            slot_capacity = self._slot_capacity_for_rigid_zone(rigid_editor)
            if slot_capacity <= 0:
                continue

            active_slot_ids: set[int] = set()
            slot_templates: dict[int, FlexCoreLayer] = {}
            for flex_editor in adjacent_flexes:
                slot_map = self._branch_slot_map(rigid_editor, flex_editor)
                global_templates = self._flex_slot_templates(flex_editor)
                for global_slot in self._covered_global_slots(rigid_editor, flex_editor):
                    local_slot = slot_map.get(global_slot)
                    template = global_templates.get(global_slot)
                    if local_slot is None or template is None:
                        continue
                    active_slot_ids.add(local_slot)
                    slot_templates.setdefault(local_slot, deepcopy(template))

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

        flex_editors = [editor for editor in self._zone_editors if editor.is_flex_zone]
        for flex_editor in flex_editors:
            self._compact_flex_slot_layout_if_possible(flex_editor)

        self._sync_sub_rigid_copper_from_master()

        for rigid_editor in self._rigid_editors():
            self._configure_rigid_zone(
                rigid_editor,
                zone_display_name=rigid_editor.zone_display_name,
            )

        for flex_editor in flex_editors:
            adjacent_rigids = self._adjacent_rigid_editors(flex_editor)
            self._configure_flex_zone(
                flex_editor,
                rigid_editor=adjacent_rigids[0] if adjacent_rigids else None,
                zone_display_name=flex_editor.zone_display_name,
            )

        self._refresh_combined_preview()

    def _refresh_combined_preview(self) -> None:
        flex_editor = self._primary_flex_editor()
        coverage_by_zone: dict[int, set[int]] = {}
        slot_maps_by_zone: dict[int, dict[int, int]] = {}
        if flex_editor is not None:
            for zone_index, editor in enumerate(self._zone_editors):
                if editor.is_flex_zone:
                    continue
                stored_coverage = self._rigid_branch_coverage.get(id(editor))
                coverage_by_zone[zone_index] = (
                    {
                        slot_id
                        for adjacent_flex in self._adjacent_flex_editors(editor)
                        for slot_id in adjacent_flex.stackup.active_flex_slot_ids()
                    }
                    if stored_coverage is None
                    else set(stored_coverage)
                )
                slot_maps_by_zone[zone_index] = self._branch_slot_map(editor, flex_editor)
        zone_index_by_id = {id(editor): index for index, editor in enumerate(self._zone_editors)}
        flex_parent_by_zone: dict[int, int] = {}
        flex_children_by_zone: dict[int, list[int]] = {}
        for zone_index, editor in enumerate(self._zone_editors):
            if not editor.is_flex_zone:
                continue
            parent_index = zone_index_by_id.get(self._flex_parent_rigid.get(id(editor)))
            if parent_index is not None:
                flex_parent_by_zone[zone_index] = parent_index
            flex_children_by_zone[zone_index] = [
                child_index
                for child_id in self._flex_child_rigids.get(id(editor), set())
                if (child_index := zone_index_by_id.get(child_id)) is not None
            ]
        active_visual_index = (
            self._zone_index(self._active_visual_editor)
            if self._active_visual_editor is not None
            else None
        )
        self.combined_preview.set_sources(
            self._zone_editors,
            active_zone_index=(
                active_visual_index
                if active_visual_index is not None
                else self.tabs.currentIndex()
            ),
            branch_coverage_by_zone=coverage_by_zone,
            branch_slot_maps_by_zone=slot_maps_by_zone,
            branch_slot_gaps_by_zone={
                zone_index: dict(self._rigid_flex_gap_by_slot.get(id(editor), {}))
                for zone_index, editor in enumerate(self._zone_editors)
                if not editor.is_flex_zone
            },
            branch_global_numbers_by_zone={
                zone_index: list(self._rigid_branch_global_numbers.get(id(editor), []))
                for zone_index, editor in enumerate(self._zone_editors)
                if not editor.is_flex_zone
            },
            flex_parent_rigid_by_zone=flex_parent_by_zone,
            flex_child_rigids_by_zone=flex_children_by_zone,
            zone_display_names={
                zone_index: self.tabs.tabText(zone_index)
                for zone_index in range(self.tabs.count())
            },
        )

    def _handle_combined_preview_selection(self, zone_index: int, meta: object) -> None:
        if not isinstance(meta, tuple) or len(meta) != 2:
            return
        if zone_index < 0 or zone_index >= len(self._zone_editors):
            return
        target_editor = self._zone_editors[zone_index]
        definition_editor = self._definition_source(target_editor)
        definition_index = self._zone_index(definition_editor)
        self._preview_selection_in_progress = True
        try:
            if definition_index is not None:
                self.tabs.setCurrentIndex(definition_index)
        finally:
            self._preview_selection_in_progress = False
        self._active_visual_editor = target_editor
        self._rigid_box_inspection_active = (
            not target_editor.is_flex_zone
            and target_editor is not self._primary_rigid_editor()
        )
        self._select_only_flex_part(target_editor)
        if meta[0] != "zone":
            definition_editor._refresh_everything(select_meta=meta)
        self._update_zone_controls()
        self._refresh_combined_preview()

    def _focus_combined_preview_rigid(self, zone_index: int) -> None:
        if zone_index < 0 or zone_index >= len(self._zone_editors):
            return
        target_editor = self._zone_editors[zone_index]
        if target_editor.is_flex_zone or target_editor is self._primary_rigid_editor():
            return
        definition_editor = self._definition_source(target_editor)
        definition_index = self._zone_index(definition_editor)
        self._preview_selection_in_progress = True
        try:
            if definition_index is not None:
                self.tabs.setCurrentIndex(definition_index)
        finally:
            self._preview_selection_in_progress = False
        self._active_visual_editor = target_editor
        self._rigid_box_inspection_active = True
        if self.combined_preview.focused_rigid_zone_index is None:
            self._overview_splitter_sizes = self.main_splitter.sizes()
        self.combined_preview.focus_rigid_part(zone_index)
        total_width = max(1, sum(self.main_splitter.sizes()))
        preview_width = max(560, int(total_width * 0.46))
        self.main_splitter.setSizes([max(520, total_width - preview_width), preview_width])
        self._update_zone_controls()
        self._refresh_combined_preview()

    def _show_combined_preview_overview(self) -> None:
        self.combined_preview.show_overview()
        if self._overview_splitter_sizes:
            self.main_splitter.setSizes(self._overview_splitter_sizes)
        self._overview_splitter_sizes = None
        self._update_zone_controls()
        self._refresh_combined_preview()

    def _handle_combined_preview_context_menu(
        self,
        zone_index: int,
        meta: object,
        global_pos: object,
    ) -> None:
        if not isinstance(meta, tuple) or len(meta) != 2:
            return
        if zone_index < 0 or zone_index >= len(self._zone_editors):
            return
        self._handle_combined_preview_selection(zone_index, meta)
        self._zone_editors[zone_index]._show_build_context_menu(meta, global_pos)

    def _matching_flex_core_index(
        self,
        rigid_editor: StackupEditorWindow,
        flex_editor: StackupEditorWindow,
        rigid_layer_index: int,
    ) -> int | None:
        matching_global_slot: int | None = None
        for global_slot in self._covered_global_slots(rigid_editor, flex_editor):
            pair = self._rigid_pair_for_global_slot(
                rigid_editor,
                flex_editor,
                global_slot,
            )
            if pair is None:
                continue
            top_index, bottom_index = pair
            if top_index < rigid_layer_index < bottom_index:
                matching_global_slot = global_slot
                break
        if matching_global_slot is None:
            return None
        for flex_layer_index, layer in enumerate(flex_editor.stackup.layers):
            if (
                isinstance(layer, FlexCoreLayer)
                and flex_editor.stackup.flex_slot_for_layer_index(flex_layer_index) == matching_global_slot
            ):
                return flex_layer_index
        return None

    def _add_flex_sandwich_context_actions(
        self,
        menu: QMenu,
        flex_editor: StackupEditorWindow,
        *,
        selected_flex_core_index: int | None,
    ) -> None:
        if selected_flex_core_index is not None:
            flex_editor._select_row_meta(("layer", selected_flex_core_index))
            flex_editor._update_buttons()

        insert_action = menu.addAction("Insert Flex Sandwich")
        insert_action.setEnabled(
            selected_flex_core_index is None
            and flex_editor.insert_flex_sandwich_action.isEnabled()
        )
        if selected_flex_core_index is not None:
            insert_action.setToolTip("Select a rigid core or prepreg row to choose the new flex location.")
        insert_action.triggered.connect(
            lambda _checked=False, editor=flex_editor: self._insert_flex_sandwich(editor)
        )
        if (
            selected_flex_core_index is not None
            and flex_editor.remove_flex_sandwich_action.isEnabled()
        ):
            remove_action = menu.addAction("Remove Flex Sandwich")
            remove_action.triggered.connect(
                lambda _checked=False, editor=flex_editor: self._remove_flex_sandwich(editor)
            )

    def _handle_zone_build_context_menu(
        self,
        editor: StackupEditorWindow,
        meta: tuple[str, int | str],
        global_pos: object,
    ) -> bool:
        zone_index = self._zone_index(editor)
        if zone_index is not None:
            self.tabs.setCurrentIndex(zone_index)
        if meta[0] != "layer":
            return False
        layer_index = int(meta[1])
        if layer_index < 0 or layer_index >= len(editor.stackup.layers):
            return False
        layer = editor.stackup.layers[layer_index]
        if editor.is_flex_zone:
            return False

        if isinstance(layer, DielectricLayer):
            allowed, reason, _gap_index = self._flex_insertion_eligibility(editor, layer_index)
            menu = QMenu(self)
            for action in editor._context_actions_for_meta(meta):
                menu.addAction(action)
            if menu.actions():
                menu.addSeparator()
            insert_action = menu.addAction("Insert Flex Sandwich")
            insert_action.setEnabled(allowed)
            if reason:
                insert_action.setToolTip(reason)
                insert_action.setStatusTip(reason)
            insert_action.triggered.connect(
                lambda _checked=False, rigid=editor, index=layer_index: self._insert_flex_sandwich_at_rigid_material(
                    rigid,
                    index,
                )
            )
            menu.exec(global_pos)
            return True

        if not isinstance(layer, FlexCoreLayer):
            return False

        flex_targets = self._adjacent_flex_editors(editor)
        if not flex_targets:
            return True
        menu = QMenu(self)
        if len(flex_targets) == 1:
            target = flex_targets[0]
            self._add_flex_sandwich_context_actions(
                menu,
                target,
                selected_flex_core_index=self._matching_flex_core_index(editor, target, layer_index),
            )
        else:
            for target in flex_targets:
                target_index = self._zone_index(target)
                target_label = self.tabs.tabText(target_index) if target_index is not None else target.zone_display_name
                submenu = menu.addMenu(target_label)
                self._add_flex_sandwich_context_actions(
                    submenu,
                    target,
                    selected_flex_core_index=self._matching_flex_core_index(editor, target, layer_index),
                )
        if menu.actions():
            menu.exec(global_pos)
        return True

    def _disable_unsupported_zone_actions(self, editor: StackupEditorWindow) -> None:
        editor.import_xpedition_action.setEnabled(False)
        editor.import_text_action.setEnabled(False)
        editor.export_xpedition_action.setEnabled(False)
        editor.export_text_action.setEnabled(False)
        editor.calculate_impedance_button.setEnabled(True)
        editor.import_xpedition_action.setStatusTip("Use the rigid-flex window File menu for project import.")
        editor.import_text_action.setStatusTip("Use the rigid-flex window File menu for project import.")
        editor.export_xpedition_action.setStatusTip("Use the rigid-flex window File menu for project export.")
        editor.export_text_action.setStatusTip("Use the rigid-flex window File menu for project export.")
        editor.calculate_impedance_button.setToolTip(
            "Calculate flex-zone impedance using Flex Core and the combined coverlay construction."
            if editor.is_flex_zone
            else "Calculate impedance for this rigid zone."
        )
        editor._sync_command_menu_state()
        self._sync_file_menu_state()
        self._sync_command_menu_state()

    def _allowed_rigid_copper_removal_indices(
        self,
        rigid_editor: StackupEditorWindow,
        flex_sources: list[StackupEditorWindow],
        *,
        minimum_copper_count: int,
    ) -> set[int]:
        allowed: set[int] = set()
        rigid_stackup = rigid_editor.stackup
        if rigid_stackup.copper_count() - 2 < minimum_copper_count:
            return allowed

        for candidate_index, candidate_layer in enumerate(rigid_stackup.layers):
            if not isinstance(candidate_layer, CopperLayer):
                continue
            simulated = deepcopy(rigid_stackup)
            try:
                simulated.remove_symmetric_copper_pair(candidate_index)
            except (IndexError, ValueError):
                continue

            preserves_all_slots = True
            for flex_source in flex_sources:
                capacity = self._branch_slot_capacity(rigid_editor, flex_source)
                slot_map = self._branch_slot_map(rigid_editor, flex_source)
                for global_slot in self._covered_global_slots(rigid_editor, flex_source):
                    slot_id = slot_map.get(global_slot)
                    if slot_id is None:
                        continue
                    try:
                        old_top_index, old_bottom_index = rigid_slot_copper_indices(
                            rigid_stackup,
                            capacity,
                            slot_id,
                        )
                        new_top_index, new_bottom_index = rigid_slot_copper_indices(
                            simulated,
                            capacity,
                            slot_id,
                        )
                    except (IndexError, ValueError):
                        preserves_all_slots = False
                        break

                    old_top = rigid_stackup.layers[old_top_index]
                    old_bottom = rigid_stackup.layers[old_bottom_index]
                    new_top = simulated.layers[new_top_index]
                    new_bottom = simulated.layers[new_bottom_index]
                    if not (
                        isinstance(old_top, CopperLayer)
                        and isinstance(old_bottom, CopperLayer)
                        and isinstance(new_top, CopperLayer)
                        and isinstance(new_bottom, CopperLayer)
                        and old_top.uid == new_top.uid
                        and old_bottom.uid == new_bottom.uid
                    ):
                        preserves_all_slots = False
                        break
                if not preserves_all_slots:
                    break

            if preserves_all_slots:
                allowed.add(candidate_index)
        return allowed

    def _flex_part_core_name(
        self,
        flex_editor: StackupEditorWindow,
    ) -> str | None:
        canonical_flexes: list[StackupEditorWindow] = []
        seen_source_ids: set[int] = set()
        for candidate in self._zone_editors:
            if not candidate.is_flex_zone:
                continue
            source = self._definition_source(candidate)
            if id(source) in seen_source_ids:
                continue
            seen_source_ids.add(id(source))
            canonical_flexes.append(source)
        flex_number_by_source_id = {
            id(source): number
            for number, source in enumerate(canonical_flexes, start=1)
        }
        source = self._definition_source(flex_editor)
        flex_number = flex_number_by_source_id.get(id(source))
        return f"FlexPart{flex_number}FlexCore" if flex_number is not None else None

    def _flex_zone_core_row_labels(
        self,
        flex_editor: StackupEditorWindow,
    ) -> dict[int, str]:
        core_name = self._flex_part_core_name(flex_editor)
        if core_name is None:
            return {}
        return {
            index: core_name
            for index, layer in enumerate(flex_editor.stackup.layers)
            if isinstance(layer, FlexCoreLayer)
        }

    def _rigid_flex_core_row_labels(
        self,
        rigid_editor: StackupEditorWindow,
    ) -> dict[int, str]:

        labels: dict[int, str] = {}
        for flex_editor in self._adjacent_flex_editors(rigid_editor):
            core_name = self._flex_part_core_name(flex_editor)
            if core_name is None:
                continue
            for global_slot in self._covered_global_slots(rigid_editor, flex_editor):
                pair = self._rigid_pair_for_global_slot(
                    rigid_editor,
                    flex_editor,
                    global_slot,
                )
                if pair is None:
                    continue
                top_index, bottom_index = pair
                core_index = next(
                    (
                        index
                        for index in range(top_index + 1, bottom_index)
                        if isinstance(rigid_editor.stackup.layers[index], FlexCoreLayer)
                    ),
                    None,
                )
                if core_index is not None:
                    labels.setdefault(core_index, core_name)
        return labels

    def _rigid_no_flow_row_labels(
        self,
        rigid_editor: StackupEditorWindow,
    ) -> dict[int, str]:
        if rigid_editor is self._primary_rigid_editor():
            prefix = "MasterRigid"
        else:
            label = getattr(rigid_editor, "zone_display_name", "")
            label_match = re.search(r"rigid\s*part\s*(\d+)", label, re.IGNORECASE)
            if label_match is not None:
                prefix = f"RigidPart{int(label_match.group(1))}"
            else:
                additional_rigids = [
                    editor
                    for editor in self._rigid_editors()
                    if editor is not self._primary_rigid_editor()
                ]
                prefix = f"RigidPart{additional_rigids.index(rigid_editor) + 2}"

        labels: dict[int, str] = {}
        no_flow_number = 0
        for layer_index, layer in enumerate(rigid_editor.stackup.layers):
            if not (
                isinstance(layer, DielectricLayer)
                and is_no_flow_prepreg_type(layer.dielectric_type)
            ):
                continue
            no_flow_number += 1
            labels[layer_index] = f"{prefix}NoFLowDielectric{no_flow_number}"
        return labels

    def _refresh_rigid_no_flow_row_labels(self) -> None:
        for rigid_editor in self._rigid_editors():
            labels = self._rigid_no_flow_row_labels(rigid_editor)
            if labels == rigid_editor.dielectric_row_labels:
                continue
            selected_meta = rigid_editor._current_row_meta()
            rigid_editor.dielectric_row_labels = labels
            rigid_editor._refresh_everything(select_meta=selected_meta)

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
            for global_slot in self._covered_global_slots(editor, flex_source):
                pair = self._rigid_pair_for_global_slot(editor, flex_source, global_slot)
                if pair is None:
                    continue
                top_index, bottom_index = pair
                blocked_material_indices.update(range(top_index + 1, bottom_index))
        minimum_copper_count = max(
            max(
                len(self._covered_global_slots(editor, flex_source)) * 2 + 2,
                self._branch_slot_capacity(editor, flex_source) * 2,
            )
            for flex_source in flex_sources
        ) if flex_sources else 2
        protected_structure_bounds = (
            (min(start for start, _end in shared_bounds), max(end for _start, end in shared_bounds))
            if shared_bounds
            else None
        )
        is_primary_rigid = editor is self._primary_rigid_editor()
        if not is_primary_rigid:
            locked_copper = {
                index
                for index, layer in enumerate(editor.stackup.layers)
                if isinstance(layer, CopperLayer)
            }
        additional_rigid_editors = [
            rigid_editor
            for rigid_editor in self._rigid_editors()
            if rigid_editor is not self._primary_rigid_editor()
        ]
        dielectric_row_prefix = (
            "MasterRigid"
            if is_primary_rigid
            else f"RigidPart{additional_rigid_editors.index(editor) + 2}"
        )
        copper_topology_locked = len(self._rigid_editors()) > 1
        if is_primary_rigid and not copper_topology_locked:
            allowed_copper_removal_indices = self._allowed_rigid_copper_removal_indices(
                editor,
                flex_sources,
                minimum_copper_count=minimum_copper_count,
            )
        else:
            # Once branches exist, every rigid part is a view of the same global
            # copper numbering. Freeze copper topology until the extra branches are
            # removed; dielectric construction may still differ by rigid part.
            allowed_copper_removal_indices = set()
            protected_structure_bounds = (-1, len(editor.stackup.layers))
        material_insertion_allowed_indices = {
            index
            for index, layer in enumerate(editor.stackup.layers)
            if isinstance(layer, DielectricLayer) and index not in blocked_material_indices
        }
        editor.configure_zone_links(
            display_copper_numbers=self._branch_global_number_overrides(editor),
            locked_copper_indices=locked_copper,
            locked_copper_note=(
                "Copper thickness, type, and roughness are inherited from the corresponding Master Rigid layer."
                if not is_primary_rigid
                else "This shared flex copper is editable from the flex zone only."
            ),
            locked_dielectric_indices=locked_dielectric,
            allowed_copper_removal_indices=allowed_copper_removal_indices,
            protected_structure_bounds=protected_structure_bounds,
            material_insertion_allowed_indices=material_insertion_allowed_indices,
            structure_locked=True,
            minimum_copper_count=minimum_copper_count,
            zone_display_name=zone_display_name,
            dielectric_row_prefix=dielectric_row_prefix,
            dielectric_row_labels=self._rigid_no_flow_row_labels(editor),
            flex_core_row_labels=self._rigid_flex_core_row_labels(editor),
            no_flow_prepreg_editable=True,
            no_flow_prepreg_available=True,
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
        core_name = self._flex_part_core_name(editor)
        coverlay_row_prefix = (
            core_name[:-len("FlexCore")]
            if core_name is not None and core_name.endswith("FlexCore")
            else None
        )
        editor.configure_zone_links(
            display_copper_numbers=copper_number_overrides,
            structure_locked=True,
            zone_display_name=zone_display_name,
            coverlay_row_prefix=coverlay_row_prefix,
            flex_core_row_labels=self._flex_zone_core_row_labels(editor),
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
            allowed_copper_removal_indices=(
                set(source.allowed_copper_removal_indices)
                if source.allowed_copper_removal_indices is not None
                else None
            ),
            protected_structure_bounds=source.protected_structure_bounds,
            material_insertion_allowed_indices=(
                set(source.material_insertion_allowed_indices)
                if source.material_insertion_allowed_indices is not None
                else None
            ),
            structure_locked=source.structure_locked,
            minimum_copper_count=source.minimum_copper_count,
            zone_display_name=source.zone_display_name,
            dielectric_row_labels=dict(source.dielectric_row_labels),
            no_flow_prepreg_editable=source.no_flow_prepreg_editable,
            no_flow_prepreg_available=source.no_flow_prepreg_available,
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
        if is_prepreg_dielectric_type(bridge.dielectric_type):
            return bridge
        fallback = reference_rigid._default_dielectric("prepreg")
        if bridge.material_id:
            return (
                deepcopy(bridge)
                if is_prepreg_dielectric_type(bridge.dielectric_type)
                else fallback
            )
        return fallback

    def _sync_rigid_zones_from_flex_zone(self, flex_editor: StackupEditorWindow) -> None:
        _ = flex_editor
        self._sync_all_rigid_zones()

    def _insert_flex_sandwich(self, flex_editor: StackupEditorWindow) -> None:
        parent_rigid = self._parent_rigid_for_flex(flex_editor)
        selected_visual = self._selected_visual_editor()
        if selected_visual is not None and not selected_visual.is_flex_zone:
            parent_rigid = selected_visual
        if parent_rigid is None:
            QMessageBox.information(
                self,
                "Cannot insert sandwich",
                "Select a rigid core or prepreg row in a connected rigid part.",
            )
            return

        first_reason = "No eligible rigid dielectric material was found."
        for layer_index, layer in enumerate(parent_rigid.stackup.layers):
            if not isinstance(layer, DielectricLayer):
                continue
            allowed, reason, _gap_index = self._flex_insertion_eligibility(
                parent_rigid,
                layer_index,
            )
            if allowed:
                self._insert_flex_sandwich_at_rigid_material(
                    parent_rigid,
                    layer_index,
                    source_flex=flex_editor,
                )
                return
            if reason:
                first_reason = reason
        QMessageBox.information(self, "Cannot insert sandwich", first_reason)

    def _insert_flex_sandwich_at_rigid_material(
        self,
        parent_rigid: StackupEditorWindow,
        layer_index: int,
        *,
        source_flex: StackupEditorWindow | None = None,
    ) -> None:
        allowed, reason, gap_index = self._flex_insertion_eligibility(
            parent_rigid,
            layer_index,
        )
        if not allowed or gap_index is None:
            QMessageBox.information(
                self,
                "Cannot insert flex sandwich",
                reason or "The selected material cannot be converted to a flex core.",
            )
            return

        adjacent_flexes = self._adjacent_flex_editors(parent_rigid)
        if source_flex is None:
            source_flex = next(
                (
                    candidate
                    for candidate in adjacent_flexes
                    if self._flex_parent_rigid.get(id(candidate)) == id(parent_rigid)
                ),
                None,
            )
        source_flex = source_flex or next(iter(adjacent_flexes), None) or self._primary_flex_editor()
        if source_flex is None:
            QMessageBox.information(
                self,
                "Cannot insert flex sandwich",
                "No Flex Part material definition is available for the new sandwich.",
            )
            return
        flex_core_template = self._flex_core_template_for_insertion(source_flex)
        if flex_core_template is None or source_flex.stackup.coverlay is None:
            QMessageBox.information(
                self,
                "Cannot insert flex sandwich",
                "The source Flex Part does not contain a valid flex-core and coverlay construction.",
            )
            return

        top_layer = gap_index + 1
        bottom_layer = gap_index + 2
        answer = QMessageBox.question(
            self,
            "Insert flex sandwich",
            (
                f"Convert the selected rigid dielectric between L{top_layer} and L{bottom_layer} "
                "to Flex Core and create a new Flex Part?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        used_slots = {
            slot_id
            for candidate in adjacent_flexes
            for slot_id in candidate.stackup.active_flex_slot_ids()
        }
        new_slot = next(slot_id for slot_id in range(len(used_slots) + 1) if slot_id not in used_slots)
        new_capacity = max(
            new_slot + 1,
            *(candidate.stackup.flex_slot_capacity_or_count() for candidate in adjacent_flexes),
        )
        self._ensure_rigid_flex_gap_map(parent_rigid)[new_slot] = gap_index

        outgoing_siblings = [
            candidate
            for candidate in self._zone_editors
            if candidate.is_flex_zone
            and self._flex_parent_rigid.get(id(candidate)) == id(parent_rigid)
        ]
        self._definition_sync_in_progress = True
        try:
            for candidate in outgoing_siblings:
                candidate_template = self._flex_core_template_for_insertion(candidate)
                if candidate_template is None or candidate.stackup.coverlay is None:
                    continue
                candidate.replace_stackup(
                    build_flex_stackup_from_templates(
                        flex_core_template=candidate_template,
                        coverlay=deepcopy(candidate.stackup.coverlay),
                        slot_indices=candidate.stackup.flex_sandwich_slot_ids(),
                        slot_capacity=new_capacity,
                    ),
                    select_meta=candidate._current_row_meta() or ("layer", 1),
                )
        finally:
            self._definition_sync_in_progress = False

        new_flex_stackup = build_flex_stackup_from_templates(
            flex_core_template=deepcopy(flex_core_template),
            coverlay=deepcopy(source_flex.stackup.coverlay),
            slot_indices=[new_slot],
            slot_capacity=new_capacity,
        )
        sibling_indices = [
            index
            for candidate in outgoing_siblings
            if (index := self._zone_index(candidate)) is not None
        ]
        insert_after = max(sibling_indices, default=self._zone_index(parent_rigid) or 0)
        new_flex_editor = self._add_zone(
            kind="flex",
            initialize_from_template=False,
            insert_index=insert_after + 1,
        )
        new_flex_editor.replace_stackup(new_flex_stackup, select_meta=("layer", 1))
        self._register_flex_parent(new_flex_editor, parent_rigid)
        flex_name = f"Flex Part {len([item for item in self._zone_editors if item.is_flex_zone])}"
        new_flex_index = self._zone_index(new_flex_editor)
        if new_flex_index is not None:
            self.tabs.setTabText(new_flex_index, flex_name)
        new_flex_editor.zone_display_name = flex_name
        self._disable_unsupported_zone_actions(new_flex_editor)

        coverage = self._rigid_branch_coverage.get(id(parent_rigid))
        if coverage is not None:
            coverage.add(new_slot)
        slot_map = self._rigid_branch_slot_maps.setdefault(id(parent_rigid), {})
        slot_map.setdefault(new_slot, new_slot)
        self._sync_all_rigid_zones()
        definition_editor = self._definition_source(new_flex_editor)
        definition_index = self._zone_index(definition_editor)
        if definition_index is not None:
            self.tabs.setCurrentIndex(definition_index)
        self._active_visual_editor = new_flex_editor
        self._update_zone_controls()
        self._refresh_combined_preview()
        definition_editor._set_note(
            f"Flex sandwich L{top_layer}-L{bottom_layer} was inserted from the selected rigid material."
        )

    def _insert_flex_sandwich_legacy(self, flex_editor: StackupEditorWindow) -> None:
        if not flex_editor.is_flex_zone:
            return
        selected_visual = self._selected_visual_editor()
        if (
            selected_visual is not None
            and selected_visual.is_flex_zone
            and self._definition_source(selected_visual) is self._definition_source(flex_editor)
        ):
            flex_editor = selected_visual

        parent_rigid = self._parent_rigid_for_flex(flex_editor)
        if parent_rigid is None:
            QMessageBox.information(self, "Cannot insert sandwich", "No linked rigid zone was found for this flex zone.")
            return

        sibling_flexes = [
            candidate
            for candidate in self._zone_editors
            if candidate.is_flex_zone
            and self._parent_rigid_for_flex(candidate) is parent_rigid
        ]
        used_slots = {
            slot_id
            for candidate in sibling_flexes
            for slot_id in candidate.stackup.active_flex_slot_ids()
        }
        maximum_sandwiches = max(1, (parent_rigid.stackup.copper_count() - 2) // 2)
        available_slots = [
            slot_id for slot_id in range(maximum_sandwiches) if slot_id not in used_slots
        ]
        if not available_slots:
            QMessageBox.information(
                self,
                "Cannot insert sandwich",
                "No unused flex-sandwich position remains inside the connected rigid part.",
            )
            return

        new_slot = available_slots[0]
        incoming_source = self._incoming_flex_for_slots(
            parent_rigid,
            {new_slot},
        )
        answer = QMessageBox.question(
            self,
            "Insert flex sandwich",
            (
                "The new master flex sandwich will receive its own independent Flex Part tab."
                if parent_rigid is self._primary_rigid_editor()
                else (
                    "The new flex sandwich continues the matching incoming Flex Part definition."
                    if incoming_source is not None
                    else "The new flex sandwich has no matching incoming span and will receive its own independent Flex Part tab."
                )
            )
            + "\n\nDo you want to continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        flex_core_template = self._flex_core_template_for_insertion(flex_editor)
        if flex_core_template is None or flex_editor.stackup.coverlay is None:
            QMessageBox.information(self, "Cannot insert sandwich", "The current flex zone does not contain a valid flex-core construction.")
            return

        new_capacity = max(
            new_slot + 1,
            *(candidate.stackup.flex_slot_capacity_or_count() for candidate in sibling_flexes),
        )
        self._definition_sync_in_progress = True
        try:
            for candidate in sibling_flexes:
                candidate_template = self._flex_core_template_for_insertion(candidate)
                if candidate_template is None or candidate.stackup.coverlay is None:
                    continue
                candidate.replace_stackup(
                    build_flex_stackup_from_templates(
                        flex_core_template=candidate_template,
                        coverlay=deepcopy(candidate.stackup.coverlay),
                        slot_indices=candidate.stackup.flex_sandwich_slot_ids(),
                        slot_capacity=new_capacity,
                    ),
                    select_meta=candidate._current_row_meta() or ("layer", 1),
                )
        finally:
            self._definition_sync_in_progress = False

        new_flex_stackup = build_flex_stackup_from_templates(
            flex_core_template=flex_core_template,
            coverlay=deepcopy(flex_editor.stackup.coverlay),
            slot_indices=[new_slot],
            slot_capacity=new_capacity,
        )
        sibling_indices = [
            index
            for candidate in sibling_flexes
            if (index := self._zone_index(candidate)) is not None
        ]
        insert_after = max(sibling_indices, default=self._zone_index(flex_editor) or 0)
        new_flex_editor = self._add_zone(
            kind="flex",
            initialize_from_template=False,
            insert_index=(insert_after + 1 if insert_after is not None else None),
        )
        new_flex_editor.replace_stackup(new_flex_stackup, select_meta=("layer", 1))
        self._register_flex_parent(new_flex_editor, parent_rigid)
        flex_name = f"Flex Part {len([item for item in self._zone_editors if item.is_flex_zone])}"
        new_flex_index = self._zone_index(new_flex_editor)
        if new_flex_index is not None:
            self.tabs.setTabText(new_flex_index, flex_name)
        new_flex_editor.zone_display_name = flex_name
        self._disable_unsupported_zone_actions(new_flex_editor)
        bound_to_existing_definition = incoming_source is not None
        if incoming_source is not None:
            self._bind_zone_definition(
                new_flex_editor,
                self._definition_source(incoming_source),
            )

        coverage = self._rigid_branch_coverage.get(id(parent_rigid))
        if coverage is not None:
            coverage.add(new_slot)
        slot_map = self._rigid_branch_slot_maps.get(id(parent_rigid), {})
        if self._rigid_branch_coverage.get(id(parent_rigid)) is None:
            slot_map = {slot_id: slot_id for slot_id in range(new_capacity)}
        else:
            slot_map.setdefault(new_slot, max(slot_map.values(), default=-1) + 1)
        self._rigid_branch_slot_maps[id(parent_rigid)] = slot_map
        self._sync_all_rigid_zones()
        definition_editor = self._definition_source(new_flex_editor)
        definition_index = self._zone_index(definition_editor)
        if definition_index is not None:
            self.tabs.setCurrentIndex(definition_index)
        self._active_visual_editor = new_flex_editor
        self._update_zone_controls()
        self._refresh_combined_preview()
        definition_editor._set_note(
            (
                "A new flex-sandwich instance was added to the live stackup and bound to this Flex Part definition."
                if bound_to_existing_definition
                else "A new independent Flex Part definition was created for the inserted master flex sandwich."
            )
        )

    def _remove_flex_sandwich(self, flex_editor: StackupEditorWindow) -> None:
        if not flex_editor.is_flex_zone:
            return
        selected_visual = self._selected_visual_editor()
        if (
            selected_visual is not None
            and selected_visual.is_flex_zone
            and self._definition_source(selected_visual) is self._definition_source(flex_editor)
        ):
            flex_editor = selected_visual
        current_slots = flex_editor.stackup.flex_sandwich_slot_ids()
        if len(current_slots) <= 1:
            connected_rigid_parts = [
                rigid_editor.zone_display_name
                for rigid_id in self._flex_child_rigids.get(id(flex_editor), set())
                if (rigid_editor := self._editor_by_id(rigid_id)) is not None
            ]
            if connected_rigid_parts:
                QMessageBox.information(
                    self,
                    "Cannot remove connected flex sandwich",
                    "This flex sandwich is connected to: "
                    + ", ".join(sorted(connected_rigid_parts))
                    + ". Remove the connected rigid part before removing this flex sandwich.",
                )
                return
            if len([editor for editor in self._zone_editors if editor.is_flex_zone]) > 1:
                answer = QMessageBox.question(
                    self,
                    "Remove flex sandwich",
                    "Remove this flex-sandwich instance from the live stackup? The shared Flex Part material definition will remain available to its other instances.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    self._active_visual_editor = flex_editor
                    self._remove_zone()
                return
            QMessageBox.information(
                self,
                "Cannot remove sandwich",
                "The rigid-flex project must keep at least one flex-sandwich instance.",
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

        connected_rigid_parts: list[str] = []
        for rigid_id in self._flex_child_rigids.get(id(flex_editor), set()):
            rigid_editor = self._editor_by_id(rigid_id)
            if rigid_editor is None:
                continue
            coverage = self._rigid_branch_coverage.get(rigid_id)
            connected_slots = set(current_slots) if coverage is None else set(coverage)
            if selected_slot in connected_slots:
                connected_rigid_parts.append(rigid_editor.zone_display_name)
        if connected_rigid_parts:
            QMessageBox.information(
                self,
                "Cannot remove connected flex sandwich",
                "This flex sandwich is connected to: "
                + ", ".join(sorted(connected_rigid_parts))
                + ". Remove the connected rigid part before removing this flex sandwich.",
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
        affected_rigids = self._adjacent_rigid_editors(flex_editor)
        flex_editor.replace_stackup(new_flex_stackup, select_meta=("layer", fallback_index * 3 + 1))

        remaining_slots = set(new_slots)
        child_rigid_ids = set(self._flex_child_rigids.get(id(flex_editor), set()))
        for rigid_id in child_rigid_ids:
            rigid_editor = self._editor_by_id(rigid_id)
            if rigid_editor is None:
                continue
            coverage = self._rigid_branch_coverage.get(rigid_id)
            if coverage is not None and not (set(coverage) & remaining_slots):
                self._flex_child_rigids.get(id(flex_editor), set()).discard(rigid_id)
                self._rigid_parent_flexes.get(rigid_id, set()).discard(id(flex_editor))

        for rigid_editor in affected_rigids:
            coverage = self._rigid_branch_coverage.get(id(rigid_editor))
            if coverage is None:
                continue
            connected_slots = {
                slot_id
                for adjacent_flex in self._adjacent_flex_editors(rigid_editor)
                for slot_id in adjacent_flex.stackup.active_flex_slot_ids()
            }
            coverage.intersection_update(connected_slots)
        self._sync_all_rigid_zones()
        flex_editor._set_note(
            f"Flex sandwich {selected_slot + 1} was removed. Any rigid part that lost its flex connection was rebuilt with Rigid Core."
        )

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
        self._rigid_branch_coverage[id(rigid_editor)] = None
        self._rigid_branch_slot_maps[id(rigid_editor)] = {
            slot_id: slot_id for slot_id in range(flex_stackup.flex_slot_capacity_or_count())
        }
        self._rigid_branch_global_numbers[id(rigid_editor)] = list(
            range(1, rigid_stackup.copper_count() + 1)
        )

        self._configure_rigid_zone(
            rigid_editor,
            flex_editor=flex_editor,
            zone_display_name="Master Rigid Part",
        )
        self._configure_flex_zone(flex_editor, rigid_editor=rigid_editor, zone_display_name="Flex Part")

        self.tabs.setTabText(self._zone_editors.index(rigid_editor), "Master Rigid Part")
        self.tabs.setTabText(self._zone_editors.index(flex_editor), "Flex Part")

        for editor in (rigid_editor, flex_editor):
            self._disable_unsupported_zone_actions(editor)
        self._refresh_combined_preview()
