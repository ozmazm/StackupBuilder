from __future__ import annotations

import logging
import math
import sys
import traceback
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QFile, QEventLoop, QMargins, QObject, QPointF, QRectF, QSize, Qt, QSignalBlocker, Signal, QStandardPaths, QSortFilterProxyModel
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
    QAbstractItemView,
)

from stackup_editor.catalog import MaterialCatalog, MaterialEntry
from stackup_editor.exporter import (
    export_stackup_text,
    export_stackup_xpedition,
    import_stackup_text,
    import_stackup_xpedition,
    stackup_import_mode_warning,
)
from stackup_editor.flex_catalog import CoverlayMaterialCatalog, FlexCoreEntry, FlexCoreMaterialCatalog
from stackup_editor.impedance_dialog import CalculateImpedanceDialog
from stackup_editor.impedance_models import ImpedanceWorkspaceState
from stackup_editor.material_comparison import MaterialComparisonDialog
from stackup_editor.models import (
    COPPER_TYPES,
    DIELECTRIC_TYPE_CHOICES,
    FLEX_COPPER_TYPES,
    CopperLayer,
    DielectricLayer,
    DielectricLikeLayer,
    FlexCoreLayer,
    Layer,
    SolderMaskSettings,
    Stackup,
    build_default_stackup,
    build_default_flex_stackup,
    copper_roughness_um,
    copper_type_choice_label,
    catalog_material_type_for_dielectric,
    dielectric_type_display_name,
    is_dielectric_like,
    is_dummy_core_type,
    is_etched_core_type,
    is_no_flow_prepreg_type,
)
from stackup_editor.solver_results_webview import FieldSolverResultsDialog
from stackup_editor.units import (
    SUPPORTED_UNITS,
    UNIT_PRECISION,
    format_compact_thickness,
    format_frequency_ghz,
    format_roughness_um,
    format_stackup_thickness,
    format_total_thickness,
    from_display,
    snap_copper_thickness_mm,
    thickness_unit_for_layer,
    to_display,
)


TABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("layer", "Layer"),
    ("material", "Material"),
    ("thickness", "Thickness"),
    ("roughness", "Roughness (Rq)"),
    ("manufacturer", "Manufacturer"),
    ("dielectric_material", "Dielectric Material"),
    ("construction", "Constructions"),
    ("resin", "Resin"),
    ("dk", "Dk"),
    ("df", "Df"),
    ("frequency", "Freq"),
)
TABLE_COLUMN_INDEX = {
    key: index
    for index, (key, _title) in enumerate(TABLE_COLUMNS)
}

logger = logging.getLogger(__name__)


def populate_copper_type_combo(combo: QComboBox, copper_types: tuple[str, ...]) -> None:
    combo.clear()
    for copper_type in copper_types:
        combo.addItem(copper_type_choice_label(copper_type), copper_type)


def selected_copper_type(combo: QComboBox) -> str:
    data = combo.currentData()
    return str(data).strip() if data is not None else combo.currentText().strip()


def set_selected_copper_type(combo: QComboBox, copper_type: str) -> None:
    index = combo.findData(copper_type)
    if index >= 0:
        combo.setCurrentIndex(index)



class MetricCard(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MetricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        self.caption_label = QLabel(title)
        self.caption_label.setObjectName("MetricCaption")
        self.caption_label.setWordWrap(True)
        self.value_label = QLabel("")
        self.value_label.setObjectName("MetricValue")
        self.value_label.setWordWrap(True)
        layout.addWidget(self.caption_label)
        layout.addWidget(self.value_label)

    def set_value(self, text: str) -> None:
        self.value_label.setText(text)


class NoScrollComboBox(QComboBox):
    """QComboBox that ignores mouse-wheel events so scrolling the editor panel
    never accidentally changes a selection."""

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        event.ignore()


class MaterialConstructionCombo(NoScrollComboBox):
    """Standard construction combo with a separate searchable popup."""

    materialSelected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._popup_width = 0
        self._source_model = QStandardItemModel(self)
        self._proxy_model = QSortFilterProxyModel(self)
        self._proxy_model.setSourceModel(self._source_model)
        self._proxy_model.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy_model.setFilterKeyColumn(0)
        self.setEditable(False)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(12)

        self._popup = QFrame(self, Qt.WindowType.Popup)
        self._popup.setObjectName("ConstructionPickerPopup")
        popup_layout = QVBoxLayout(self._popup)
        popup_layout.setContentsMargins(8, 8, 8, 8)
        popup_layout.setSpacing(6)

        self._popup_search = QLineEdit(self._popup)
        self._popup_search.setPlaceholderText("Filter constructions...")
        self._popup_list = QListView(self._popup)
        self._popup_list.setModel(self._proxy_model)
        self._popup_list.setUniformItemSizes(True)
        popup_layout.addWidget(self._popup_search)
        popup_layout.addWidget(self._popup_list, 1)

        self._popup_search.textChanged.connect(self._filter_materials)
        self._popup_search.returnPressed.connect(self._commit_first_filtered_material)
        self._popup_list.clicked.connect(self._commit_popup_material)
        self._popup_list.activated.connect(self._commit_popup_material)

    def set_materials(
        self,
        items: list[tuple[str, str, str]],
        *,
        selected_id: str | None = None,
    ) -> None:
        self._proxy_model.setFilterFixedString("")
        self._source_model.clear()
        with QSignalBlocker(self):
            self.clear()
        font_metrics = QFontMetrics(self._popup_list.font())
        self._popup_width = 0
        for full_label, material_id, compact_label in items:
            item = QStandardItem(full_label)
            item.setData(material_id, Qt.ItemDataRole.UserRole)
            item.setToolTip(full_label)
            self._source_model.appendRow(item)
            self.addItem(compact_label, material_id)
            self._popup_width = max(
                self._popup_width,
                font_metrics.horizontalAdvance(full_label) + 48,
            )
        self._select_material(selected_id)

    def showPopup(self) -> None:  # type: ignore[override]
        if not self.isEnabled() or self._source_model.rowCount() == 0:
            return
        with QSignalBlocker(self._popup_search):
            self._popup_search.clear()
        self._proxy_model.setFilterFixedString("")
        popup_width = max(self.width(), min(self._popup_width, 760))
        visible_rows = min(max(self._proxy_model.rowCount(), 1), 14)
        row_height = self._popup_list.sizeHintForRow(0)
        if row_height <= 0:
            row_height = 28
        popup_height = self._popup_search.sizeHint().height() + (visible_rows * row_height) + 28
        self._popup.resize(popup_width, popup_height)
        self._popup.move(self.mapToGlobal(self.rect().bottomLeft()))
        self._popup.show()
        self._popup.raise_()
        self._popup_search.setFocus()

    def hidePopup(self) -> None:  # type: ignore[override]
        self._popup.hide()

    def _select_material(self, material_id: str | None) -> None:
        self.setCurrentIndex(self.findData(material_id))

    def _filter_materials(self, text: str) -> None:
        self._proxy_model.setFilterFixedString(text)

    def _commit_first_filtered_material(self) -> None:
        if self._proxy_model.rowCount() > 0:
            self._commit_popup_material(self._proxy_model.index(0, 0))

    def _commit_popup_material(self, index) -> None:
        material_id = index.data(Qt.ItemDataRole.UserRole)
        if material_id is None:
            return
        material_id = str(material_id)
        self._select_material(material_id)
        self.hidePopup()
        self.materialSelected.emit(material_id)


class _WheelBlocker(QObject):
    """Event filter that swallows wheel events on the watched widget."""

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Wheel:
            event.ignore()
            return True
        return super().eventFilter(obj, event)


class LiveStackupWidget(QWidget):
    layerSelected = Signal(int)
    layerContextMenuRequested = Signal(int, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(320, 360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.stackup: Stackup | None = None
        self.catalog: MaterialCatalog | None = None
        self.display_unit = "mm"
        self.selected_index: int | None = None
        self.symmetry_ok = True
        self.symmetry_issues: list[str] = []
        self.copper_number_overrides: dict[int, int] = {}
        self._layer_regions: list[tuple[QRectF, int | None]] = []
        self.palette_map = {
            "preview_bg": "#0b1724",
            "text": "#e7f0f7",
            "text_muted": "#8fa9bf",
            "accent": "#7cd0dd",
            "copper": "#d88a36",
            "danger": "#ff8b70",
        }

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(450, 620)

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(320, 360)

    def set_data(
        self,
        stackup: Stackup,
        catalog: MaterialCatalog,
        *,
        display_unit: str,
        selected_index: int | None,
        symmetry_ok: bool,
        symmetry_issues: list[str],
        copper_number_overrides: dict[int, int] | None = None,
    ) -> None:
        self.stackup = stackup
        self.catalog = catalog
        self.display_unit = display_unit
        self.selected_index = selected_index
        self.symmetry_ok = symmetry_ok
        self.symmetry_issues = symmetry_issues
        self.copper_number_overrides = dict(copper_number_overrides or {})
        self.update()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        point = QPointF(event.position())
        for rect, index in self._layer_regions:
            if rect.contains(point) and index is not None:
                self.layerSelected.emit(index)
                break
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        point = QPointF(event.pos())
        for rect, index in self._layer_regions:
            if rect.contains(point) and index is not None:
                self.layerSelected.emit(index)
                self.layerContextMenuRequested.emit(index, event.globalPos())
                event.accept()
                return
        event.ignore()

    def _blend_hex(self, start_hex: str, end_hex: str, ratio: float) -> QColor:
        ratio = max(0.0, min(1.0, ratio))
        start = QColor(start_hex)
        end = QColor(end_hex)
        red = round(start.red() + ((end.red() - start.red()) * ratio))
        green = round(start.green() + ((end.green() - start.green()) * ratio))
        blue = round(start.blue() + ((end.blue() - start.blue()) * ratio))
        return QColor(red, green, blue)

    def _copper_label(self, index: int) -> str:
        if self.stackup is None:
            return ""
        if index in self.copper_number_overrides:
            return f"L{self.copper_number_overrides[index]}"
        return f"L{self.stackup.copper_layer_number(index)}"

    def _dielectric_material_name(self, layer: DielectricLikeLayer) -> str:
        if self.stackup is None or self.catalog is None:
            return ""
        return self.stackup.dielectric_description(layer, self.catalog)

    def _dielectric_construction_text(self, layer: DielectricLikeLayer) -> str:
        if self.stackup is None or self.catalog is None:
            return ""
        return self.stackup.dielectric_construction(layer, self.catalog) or ""

    def _live_preview_thickness_text(self, thickness_mm: float, *, is_copper: bool) -> str:
        if is_copper:
            return format_compact_thickness(thickness_mm, "oz")
        unit = thickness_unit_for_layer(self.display_unit, is_copper=False)
        return format_compact_thickness(thickness_mm, unit)

    def _is_soldermask_marker(self, layer: CopperLayer | DielectricLikeLayer | str) -> bool:
        return layer == "soldermask_top" or layer == "soldermask_bottom"

    def _is_coverlay_marker(self, layer: CopperLayer | DielectricLikeLayer | str) -> bool:
        return isinstance(layer, str) and layer.startswith("coverlay_")

    def _coverlay_marker_parts(self, marker: str) -> tuple[str, str] | None:
        if not marker.startswith("coverlay_"):
            return None
        remainder = marker[len("coverlay_") :]
        side, _, component = remainder.partition("_")
        if side not in {"top", "bottom"} or component not in {"pi", "adhesive"}:
            return None
        return side, component

    def _center_label(self, layer: CopperLayer | DielectricLikeLayer | str, *, pixel_height: float) -> str:
        if self.stackup is None or self.catalog is None:
            return ""
        if self._is_soldermask_marker(layer):
            primary = "Solder Mask"
            secondary = f"Dk {self.stackup.soldermask.dk:.3f} | Df {self.stackup.soldermask.df:.4f}"
            return f"{primary} | {secondary}"
        if self._is_coverlay_marker(layer):
            if self.stackup.coverlay is None or not isinstance(layer, str):
                return ""
            parts = self._coverlay_marker_parts(layer)
            if parts is None:
                return ""
            _side, component = parts
            primary = f"{self.stackup.coverlay.component_label(component)} {self.stackup.coverlay.family}"
            dk, df = self.stackup.coverlay.component_dk_df(component, self.stackup.coverlay.selected_freq_ghz)
            secondary_parts = []
            if dk is not None:
                secondary_parts.append(f"Dk {dk:.3f}")
            if df is not None:
                secondary_parts.append(f"Df {df:.4f}")
            secondary = " | ".join(secondary_parts)
            return f"{primary} | {secondary}" if secondary else primary
        if isinstance(layer, CopperLayer):
            primary = layer.copper_type
            secondary = format_roughness_um(layer.roughness_um)
            if primary and secondary:
                return f"{primary} | {secondary}"
            return primary or secondary
        dk, df = self.stackup.dielectric_dk_df_or_none(layer, self.catalog)
        primary = (
            "Rigid PP-No Flow"
            if isinstance(layer, DielectricLayer)
            and is_no_flow_prepreg_type(layer.dielectric_type)
            else "Rigid Dummy Core"
            if isinstance(layer, DielectricLayer)
            and is_dummy_core_type(layer.dielectric_type)
            else "Rigid Etched Core"
            if isinstance(layer, DielectricLayer)
            and is_etched_core_type(layer.dielectric_type)
            else self._dielectric_material_name(layer)
        )
        construction = self._dielectric_construction_text(layer)
        if construction:
            primary = f"{primary} | {construction}" if primary else construction
        secondary_parts = []
        if dk is not None:
            secondary_parts.append(f"Dk {dk:.3f}")
        if df is not None:
            secondary_parts.append(f"Df {df:.4f}")
        secondary = " | ".join(secondary_parts)
        if not primary:
            primary = dielectric_type_display_name(layer.dielectric_type)
        if not secondary:
            return primary
        return f"{primary} | {secondary}"

    def _draw_fitted_single_line(
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
        available_width = max(12, int(rect.width()) - 4)
        available_height = max(8, int(rect.height()) - 2)

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

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.fillRect(self.rect(), QColor(self.palette_map["preview_bg"]))
            self._layer_regions.clear()

            if self.stackup is None or self.catalog is None:
                painter.setPen(QColor(self.palette_map["text_muted"]))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No stackup loaded.")
                return

            width = max(320, self.width())
            height = max(360, self.height())
            dense_preview = (len(self.stackup.layers) + 2) >= 20
            grid_color = self._blend_hex(self.palette_map["preview_bg"], self.palette_map["text_muted"], 0.18)
            painter.setPen(QPen(grid_color, 1))
            grid_step = 40 if dense_preview else (46 if width < 420 else 54)
            top_band = 46 if dense_preview else (64 if height < 520 else 74)
            bottom_band = 24 if dense_preview else (38 if height < 520 else 46)
            for x in range(24, width, grid_step):
                painter.drawLine(x, top_band - 4, max(0, x - 42), height - 28)
            for y in range(top_band + 10, height - 22, grid_step):
                painter.drawLine(24, y, width - 24, y)

            title_font = QFont("Bahnschrift", 11 if dense_preview else (14 if width < 430 else 16), QFont.Weight.Bold)
            painter.setFont(title_font)
            painter.setPen(QColor(self.palette_map["text"]))
            painter.drawText(QRectF(26, 16, width - 52, 26), "Live Build")
            total_text = format_total_thickness(self.stackup.total_thickness_mm(self.catalog), self.display_unit)

            if self.stackup.coverlay is not None:
                preview_layers: list[tuple[int | None, CopperLayer | DielectricLikeLayer | str, float]] = [
                    (None, "coverlay_top_pi", self.stackup.coverlay.component_thickness_mm("pi")),
                    (None, "coverlay_top_adhesive", self.stackup.coverlay.component_thickness_mm("adhesive")),
                    *[
                        (index, layer, self.stackup.layer_thickness_mm(layer, self.catalog))
                        for index, layer in enumerate(self.stackup.layers)
                    ],
                    (None, "coverlay_bottom_adhesive", self.stackup.coverlay.component_thickness_mm("adhesive")),
                    (None, "coverlay_bottom_pi", self.stackup.coverlay.component_thickness_mm("pi")),
                ]
            else:
                preview_layers = [
                    (None, "soldermask_top", self.stackup.soldermask.thickness_mm),
                    *[
                        (index, layer, self.stackup.layer_thickness_mm(layer, self.catalog))
                        for index, layer in enumerate(self.stackup.layers)
                    ],
                    (None, "soldermask_bottom", self.stackup.soldermask.thickness_mm),
                ]
            layer_count = max(1, len(preview_layers))
            y0 = top_band + (8 if dense_preview else 14)
            y1 = height - bottom_band
            usable_height = max(80, y1 - y0)
            total_mm = self.stackup.total_thickness_mm(self.catalog) or 1.0
            equal_height = usable_height / layer_count
            proportional = [(thickness_mm / total_mm) * usable_height for _index, _layer, thickness_mm in preview_layers]
            equal_weight = min(0.99, 0.76 + (layer_count / 48))
            blended = [(equal_height * equal_weight) + (prop * (1 - equal_weight)) for prop in proportional]
            scale = usable_height / sum(blended)
            heights = [value * scale for value in blended]

            if layer_count <= 10:
                row_size = 10
                detail_size = 10
            elif layer_count <= 16:
                row_size = 9
                detail_size = 9
            elif layer_count <= 22:
                row_size = 8
                detail_size = 8
            else:
                row_size = 6
                detail_size = 6
            row_font = QFont("Segoe UI", row_size)
            row_bold_font = QFont("Bahnschrift", row_size, QFont.Weight.Bold)
            detail_font = QFont("Segoe UI", detail_size)
            detail_bold_font = QFont("Bahnschrift", detail_size, QFont.Weight.Bold)

            left_gutter = max(50, min(78, round(width * 0.13)))
            right_gutter = max(92, min(148, round(width * 0.2)))
            depth = max(14, min(30, width // 16))
            rise = max(10, min(18, depth // 2))
            x0 = left_gutter
            stack_width = max(170, width - left_gutter - right_gutter - depth - 18)
            x1 = x0 + stack_width

            painter.setPen(QPen(QColor("#35536d"), 1))
            painter.setBrush(QColor("#183247"))
            top_poly = QPainterPath()
            top_poly.moveTo(x0, y0)
            top_poly.lineTo(x1, y0)
            top_poly.lineTo(x1 + depth, y0 - rise)
            top_poly.lineTo(x0 + depth, y0 - rise)
            top_poly.closeSubpath()
            painter.drawPath(top_poly)

            side_poly = QPainterPath()
            side_poly.moveTo(x1, y0)
            side_poly.lineTo(x1 + depth, y0 - rise)
            side_poly.lineTo(x1 + depth, y1 - rise)
            side_poly.lineTo(x1, y1)
            side_poly.closeSubpath()
            painter.setBrush(QColor("#102031"))
            painter.drawPath(side_poly)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(x0 - 4, y0 - 4, stack_width + 8, usable_height + 8))

            y = float(y0)
            for visual, pixel_height in zip(preview_layers, heights):
                index, layer, _thickness_mm = visual
                top = y
                bottom = y + pixel_height
                selected = index is not None and index == self.selected_index

                if self._is_soldermask_marker(layer):
                    fill = QColor("#62866b")
                    outline = QColor("#85b38f")
                    text_color = QColor("#ecf7ee")
                    left_label = ""
                    right_label = self._live_preview_thickness_text(self.stackup.soldermask.thickness_mm, is_copper=False)
                elif self._is_coverlay_marker(layer):
                    if self.stackup.coverlay is not None and isinstance(layer, str):
                        marker_parts = self._coverlay_marker_parts(layer)
                    else:
                        marker_parts = None
                    component = marker_parts[1] if marker_parts is not None else "pi"
                    if component == "pi":
                        fill = QColor("#2e86ff")
                        outline = QColor("#87beff")
                        text_color = QColor("#eff6ff")
                    else:
                        fill = QColor("#8f949c")
                        outline = QColor("#c1c6ce")
                        text_color = QColor("#111418")
                    left_label = ""
                    right_label = self._live_preview_thickness_text(
                        self.stackup.coverlay.component_thickness_mm(component) if self.stackup.coverlay is not None else 0.0,
                        is_copper=False,
                    )
                elif isinstance(layer, CopperLayer):
                    fill = QColor("#d88a36")
                    outline = QColor("#f2bb70")
                    text_color = QColor("#1f1408")
                    left_label = self._copper_label(index)
                    right_label = self._live_preview_thickness_text(layer.thickness_mm, is_copper=True)
                elif isinstance(layer, FlexCoreLayer):
                    fill = QColor("#f0b54a")
                    outline = QColor("#ffd48a")
                    text_color = QColor("#2a1800")
                    left_label = ""
                    thickness_mm = self.stackup.dielectric_thickness_mm(layer, self.catalog)
                    right_label = (
                        self._live_preview_thickness_text(thickness_mm, is_copper=False) if thickness_mm is not None else ""
                    )
                else:
                    if is_dummy_core_type(layer.dielectric_type):
                        fill = QColor("#e67e22")
                        outline = QColor("#ffb05c")
                        text_color = QColor("#211206")
                    elif is_etched_core_type(layer.dielectric_type):
                        fill = QColor("#f39c12")
                        outline = QColor("#ffc35a")
                        text_color = QColor("#211506")
                    elif layer.dielectric_type == "core":
                        fill = QColor("#77a8c8")
                        outline = QColor("#a4d1eb")
                        text_color = QColor("#10202e")
                    elif is_no_flow_prepreg_type(layer.dielectric_type):
                        fill = QColor("#5a321e")
                        outline = QColor("#a66a43")
                        text_color = QColor("#fff2e6")
                    else:
                        fill = QColor("#bfd79a")
                        outline = QColor("#dce9b8")
                        text_color = QColor("#10202e")
                    left_label = ""
                    thickness_mm = self.stackup.dielectric_thickness_mm(layer, self.catalog)
                    right_label = (
                        self._live_preview_thickness_text(thickness_mm, is_copper=False) if thickness_mm is not None else ""
                    )

                side_fill = self._blend_hex(fill.name(), self.palette_map["preview_bg"], 0.38)
                if selected:
                    painter.setPen(QPen(QColor(self.palette_map["accent"]), 2))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(QRectF(x0 - 5, top - 3, stack_width + 10, pixel_height + 6))

                painter.setPen(QPen(outline if not selected else QColor("#e8f6ff"), 2 if selected else 1))
                painter.setBrush(side_fill)
                right_face = QPainterPath()
                right_face.moveTo(x1, top)
                right_face.lineTo(x1 + depth, top - rise)
                right_face.lineTo(x1 + depth, bottom - rise)
                right_face.lineTo(x1, bottom)
                right_face.closeSubpath()
                painter.drawPath(right_face)

                painter.setBrush(fill)
                painter.drawRect(QRectF(x0, top, stack_width, pixel_height))
                self._layer_regions.append((QRectF(x0, top, stack_width, pixel_height), index))

                painter.setPen(QColor(self.palette_map["text"]))
                painter.setFont(row_bold_font)
                if left_label:
                    self._draw_fitted_single_line(
                        painter,
                        QRectF(0, top, x0 - 10, pixel_height),
                        left_label,
                        color=QColor(self.palette_map["text"]),
                        font=row_bold_font,
                        min_point_size=5.0,
                        alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    )

                if pixel_height >= 8:
                    chosen_font = detail_bold_font if selected and pixel_height > 16 else detail_font
                    text_rect = QRectF(
                        x0 + 8,
                        top + 2,
                        max(92, (stack_width * (0.9 if stack_width > 360 else 0.94))),
                        max(14, pixel_height - 4),
                    )
                    self._draw_fitted_single_line(
                        painter,
                        text_rect,
                        self._center_label(layer, pixel_height=pixel_height),
                        color=text_color,
                        font=chosen_font,
                        min_point_size=4.5 if dense_preview else 5.0,
                        alignment=Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
                    )

                if right_label and pixel_height >= 8:
                    self._draw_fitted_single_line(
                        painter,
                        QRectF(x1 + depth + 12, top - (rise / 2), right_gutter - 20, pixel_height + rise),
                        right_label,
                        color=QColor(self.palette_map["text_muted"]),
                        font=row_font,
                        min_point_size=4.5 if dense_preview else 5.0,
                        alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    )

                y = bottom

            painter.setPen(QPen(QColor("#c63b32"), 2, Qt.PenStyle.DashLine))
            symmetry_y = y0 + (usable_height / 2)
            painter.drawLine(x0 - 12, symmetry_y, x1 + depth + 12, symmetry_y)

            footer_color = QColor("#8fe2ac") if self.symmetry_ok else QColor(self.palette_map["danger"])
            footer_text = "No Warning" if self.symmetry_ok else f"Symmetry Warning: {self.symmetry_issues[0]}"
            painter.setPen(QPen(QColor("#27445f"), 1))
            painter.drawLine(18, height - 28, width - 18, height - 28)
            self._draw_fitted_single_line(
                painter,
                QRectF(18, height - 24, width * 0.65, 20),
                footer_text,
                color=footer_color,
                font=QFont("Bahnschrift", 9, QFont.Weight.Bold),
                min_point_size=5.5,
                alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            )
            if width >= 420 and height >= 420 and not dense_preview:
                painter.setPen(QColor(self.palette_map["text_muted"]))
                painter.setFont(QFont("Segoe UI", 8))
                painter.drawText(
                    QRectF(width * 0.45, height - 24, width * 0.5 - 18, 20),
                    Qt.AlignmentFlag.AlignRight,
                    "Click any visible layer to sync the table selection",
                )
        except Exception:
            traceback.print_exc()
            painter.fillRect(self.rect(), QColor(self.palette_map["preview_bg"]))
            painter.setPen(QColor(self.palette_map["danger"]))
            painter.setFont(QFont("Segoe UI", 10))
            painter.drawText(
                self.rect().adjusted(16, 16, -16, -16),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                "Live stackup preview could not be rendered.",
            )
        finally:
            painter.end()


class StackupEditorWindow(QMainWindow):
    newStackupRequested = Signal()
    flexCoreChanged = Signal(object)
    sharedRegionChanged = Signal()
    structureChanged = Signal()
    insertFlexSandwichRequested = Signal()
    removeFlexSandwichRequested = Signal()
    stackupViewChanged = Signal()

    def __init__(
        self,
        root_path: Path,
        *,
        zone_kind: str = "rigid",
        defer_initial_refresh: bool = False,
    ) -> None:
        super().__init__()
        self.root_path = root_path
        self.zone_kind = zone_kind
        self.is_flex_zone = zone_kind == "flex"
        logger.info("Initializing StackupEditorWindow with root_path=%s", root_path)
        self.setWindowTitle("StackUp Editor")
        self.resize(1660, 940)
        self.setMinimumSize(1080, 680)

        catalog_path = root_path / "data" / "material_catalog.json"
        if not catalog_path.exists():
            raise FileNotFoundError(
                f"Material catalog not found at {catalog_path}. Run tools/build_material_catalog.py first."
            )

        self.catalog = MaterialCatalog.load(catalog_path)
        self.flex_core_catalog: FlexCoreMaterialCatalog | None = None
        self.coverlay_catalog: CoverlayMaterialCatalog | None = None
        if self.is_flex_zone:
            self.flex_core_catalog, self.coverlay_catalog = self._load_flex_catalogs()
            self.stackup = build_default_flex_stackup(self.flex_core_catalog, self.coverlay_catalog)
        else:
            self.stackup = build_default_stackup(self.catalog)
        self.display_unit = "mm"
        self.geometry_input_unit = "mm"
        self._ui_loading = False
        self._table_refreshing = False
        self._refresh_deferred = bool(defer_initial_refresh)
        self._deferred_select_meta: tuple[str, int | str] = ("layer", 0)
        self._row_meta: list[tuple[str, int | str]] = []
        self._layer_row_by_index: dict[int, int] = {}
        self._last_solver_result: dict[str, object] | None = None
        self._solver_result_window: FieldSolverResultsDialog | None = None
        self._impedance_dialog: CalculateImpedanceDialog | None = None
        self._material_comparison_dialog: MaterialComparisonDialog | None = None
        self.build_context_menu_handler = None
        self.impedance_workspace = ImpedanceWorkspaceState()
        self._impedance_legacy_migrated = False
        self._ui_scale = 1.0
        self.structure_locked = self.is_flex_zone
        self.minimum_copper_count = 2
        self.copper_number_overrides: dict[int, int] = {}
        self.locked_copper_indices: set[int] = set()
        self.locked_copper_note = "This shared flex copper is editable from the flex zone only."
        self.locked_dielectric_indices: set[int] = set()
        self.no_flow_prepreg_editable = True
        self.no_flow_prepreg_available = False
        self.allowed_copper_removal_indices: set[int] | None = None
        self.protected_structure_bounds: tuple[int, int] | None = None
        self.material_insertion_allowed_indices: set[int] | None = None
        self.zone_display_name = "Flex zone" if self.is_flex_zone else "Rigid zone"
        self.dielectric_row_prefix: str | None = None
        self.dielectric_row_labels: dict[int, str] = {}
        self.coverlay_row_prefix: str | None = None
        self.flex_core_row_labels: dict[int, str] = {}

        self.palette_map = {
            "bg": "#091521",
            "surface": "#0f1f2f",
            "surface_alt": "#13283b",
            "surface_soft": "#183148",
            "border": "#27445f",
            "text": "#e7f0f7",
            "text_muted": "#8fa9bf",
            "accent": "#2d92a0",
            "accent_active": "#256f7a",
            "copper": "#d88a36",
            "copper_active": "#bf7629",
            "success": "#54b071",
            "danger": "#d06145",
            "input": "#0c1a28",
            "tree_bg": "#142535",
            "tree_alt": "#1b3043",
            "tree_head": "#20374c",
            "preview_bg": "#0b1724",
        }

        self._build_ui()
        self._apply_stylesheet()
        if not self._refresh_deferred:
            self._refresh_everything(select_meta=("layer", 0))
        logger.info("StackupEditorWindow ready with %s stackup rows", len(self.stackup.layers))

    def _resolve_catalog_path(self, *relative_candidates: str) -> Path:
        for relative in relative_candidates:
            candidate = self.root_path / relative
            if candidate.exists():
                return candidate
        checked = "\n".join(str(self.root_path / relative) for relative in relative_candidates)
        raise FileNotFoundError(f"Required catalog file was not found. Checked:\n{checked}")

    def _load_flex_catalogs(self) -> tuple[FlexCoreMaterialCatalog, CoverlayMaterialCatalog]:
        flex_core_path = self._resolve_catalog_path(
            "data/flex_core_material_catalog.json",
            "stackup_editor/flex_core_material_catalog.json",
        )
        coverlay_path = self._resolve_catalog_path(
            "data/coverlay_material_catalog.json",
            "stackup_editor/coverlay_material_catalog.json",
        )
        return FlexCoreMaterialCatalog.load(flex_core_path), CoverlayMaterialCatalog.load(coverlay_path)

    def configure_zone_links(
        self,
        *,
        display_copper_numbers: dict[int, int] | None = None,
        locked_copper_indices: set[int] | None = None,
        locked_copper_note: str | None = None,
        locked_dielectric_indices: set[int] | None = None,
        allowed_copper_removal_indices: set[int] | None = None,
        protected_structure_bounds: tuple[int, int] | None = None,
        material_insertion_allowed_indices: set[int] | None = None,
        structure_locked: bool | None = None,
        minimum_copper_count: int | None = None,
        zone_display_name: str | None = None,
        dielectric_row_prefix: str | None = None,
        dielectric_row_labels: dict[int, str] | None = None,
        coverlay_row_prefix: str | None = None,
        flex_core_row_labels: dict[int, str] | None = None,
        no_flow_prepreg_editable: bool | None = None,
        no_flow_prepreg_available: bool | None = None,
    ) -> None:
        self.copper_number_overrides = dict(display_copper_numbers or {})
        self.locked_copper_indices = set(locked_copper_indices or set())
        if locked_copper_note is not None:
            self.locked_copper_note = locked_copper_note
        self.locked_dielectric_indices = set(locked_dielectric_indices or set())
        self.allowed_copper_removal_indices = (
            set(allowed_copper_removal_indices)
            if allowed_copper_removal_indices is not None
            else None
        )
        self.protected_structure_bounds = protected_structure_bounds
        self.material_insertion_allowed_indices = (
            set(material_insertion_allowed_indices)
            if material_insertion_allowed_indices is not None
            else None
        )
        if structure_locked is not None:
            self.structure_locked = structure_locked
        if minimum_copper_count is not None:
            self.minimum_copper_count = max(2, minimum_copper_count)
        if zone_display_name:
            self.zone_display_name = zone_display_name
        if dielectric_row_prefix is not None:
            self.dielectric_row_prefix = dielectric_row_prefix
        if dielectric_row_labels is not None:
            self.dielectric_row_labels = dict(dielectric_row_labels)
        if coverlay_row_prefix is not None:
            self.coverlay_row_prefix = coverlay_row_prefix
        if flex_core_row_labels is not None:
            self.flex_core_row_labels = dict(flex_core_row_labels)
        if no_flow_prepreg_editable is not None:
            self.no_flow_prepreg_editable = no_flow_prepreg_editable
        if no_flow_prepreg_available is not None:
            self.no_flow_prepreg_available = no_flow_prepreg_available
            blocker = QSignalBlocker(self.dielectric_type_combo)
            selected_type = self.dielectric_type_combo.currentText()
            self.dielectric_type_combo.clear()
            self.dielectric_type_combo.addItems(
                [
                    value
                    for _label, value in DIELECTRIC_TYPE_CHOICES
                    if value != "no_flow_prepreg"
                    or (
                        self.no_flow_prepreg_available
                        and self.no_flow_prepreg_editable
                    )
                ]
            )
            if selected_type:
                self.dielectric_type_combo.setCurrentText(selected_type)
            del blocker
        self._refresh_everything(select_meta=self._current_row_meta())

    def replace_stackup(self, stackup: Stackup, *, select_meta: tuple[str, int | str] | None = None) -> None:
        self.stackup = stackup
        self._reset_impedance_workspace()
        self._refresh_everything(select_meta=select_meta or ("layer", 0))

    def _build_ui(self) -> None:
        logger.debug("Building main window UI")
        loaded_window = self._load_main_window_ui()
        if loaded_window.windowTitle():
            self.setWindowTitle(loaded_window.windowTitle())
        self.resize(loaded_window.size())

        central = loaded_window.centralWidget()
        if central is None:
            raise RuntimeError("The UI file does not define a central widget.")
        central.setParent(None)
        loaded_window.deleteLater()
        self.setCentralWidget(central)

        self.main_splitter = self._require_child(QSplitter, "main_splitter")
        self.left_pane = self._require_child(QWidget, "left_pane")
        self.right_pane = self._require_child(QWidget, "right_pane")
        self.metric_total_host = self._require_child(QWidget, "metric_total_host")
        self.metric_copper_host = self._require_child(QWidget, "metric_copper_host")
        self.metric_units_host = self._require_child(QWidget, "metric_units_host")
        self.metric_rows_host = self._require_child(QWidget, "metric_rows_host")
        self.preview_host = self._require_child(QWidget, "preview_host")
        self.add_above_button = self._require_child(QPushButton, "add_above_button")
        self.add_below_button = self._require_child(QPushButton, "add_below_button")
        self.remove_button = self._require_child(QPushButton, "remove_button")
        self.add_material_above_button = self._require_child(QPushButton, "add_material_above_button")
        self.add_material_below_button = self._require_child(QPushButton, "add_material_below_button")
        self.unit_combo = self._require_child(QComboBox, "unit_combo")
        self.copyright_label = self._require_child(QLabel, "copyright_label")
        self.calculate_impedance_button = self._require_child(QPushButton, "calculate_impedance_button")
        self.table = self._require_child(QTableWidget, "table")
        self.detail_title_label = self._require_child(QLabel, "detail_title_label")
        self.preview_title_label = self._require_child(QLabel, "preview_title_label")
        self.preview_subtitle_label = self._require_child(QLabel, "preview_subtitle_label")
        self.editor_stack = self._require_child(QStackedWidget, "editor_stack")
        self.placeholder_page = self._require_child(QWidget, "placeholder_page")
        self.soldermask_page = self._require_child(QWidget, "soldermask_page")
        self.copper_page = self._require_child(QWidget, "copper_page")
        self.dielectric_page = self._require_child(QWidget, "dielectric_page")
        self.copper_type_combo = self._require_child(QComboBox, "copper_type_combo")
        self.copper_thickness_edit = self._require_child(QLineEdit, "copper_thickness_edit")
        self.copper_roughness_label = self._require_child(QLabel, "copper_roughness_label")
        self.apply_copper_button = self._require_child(QPushButton, "apply_copper_button")
        self.apply_sym_copper_button = self._require_child(QPushButton, "apply_sym_copper_button")
        self.dielectric_type_combo = self._require_child(QComboBox, "dielectric_type_combo")
        self.dielectric_manufacturer_combo = self._require_child(QComboBox, "dielectric_manufacturer_combo")
        self.dielectric_family_combo = self._require_child(QComboBox, "dielectric_family_combo")
        self.material_filter_edit = self._require_child(QLineEdit, "material_filter_edit")
        self.dielectric_material_combo = self._require_child(QComboBox, "dielectric_material_combo")
        self.apply_layer_button = self._require_child(QPushButton, "apply_layer_button")
        self.apply_sym_layer_button = self._require_child(QPushButton, "apply_sym_layer_button")
        self.layer_frequency_combo = self._require_child(QComboBox, "layer_frequency_combo")
        self.global_frequency_combo = self._require_child(QComboBox, "global_frequency_combo")
        self.apply_all_frequency_button = self._require_child(QPushButton, "apply_all_frequency_button")
        self.layer_frequency_field_label = self._require_child(QLabel, "layer_frequency_field_label")
        self.global_frequency_field_label = self._require_child(QLabel, "global_frequency_field_label")
        self.readonly_dielectric_card = self._require_child(QFrame, "ReadonlyCard")

        # Frequency selection remains an internal material-catalog detail. The
        # dielectric editor exposes material selection and apply actions only.
        for widget in (
            self.layer_frequency_field_label,
            self.layer_frequency_combo,
            self.global_frequency_field_label,
            self.global_frequency_combo,
            self.apply_all_frequency_button,
            self.readonly_dielectric_card,
        ):
            widget.hide()

        # Install wheel blocker on UI-file-loaded combo boxes (NoScrollComboBox handles
        # the programmatically created ones; these need an event filter instead).
        self._wheel_blocker = _WheelBlocker(self)
        for _combo in (
            self.unit_combo,
            self.copper_type_combo,
            self.dielectric_type_combo,
            self.dielectric_manufacturer_combo,
            self.dielectric_family_combo,
            self.dielectric_material_combo,
            self.layer_frequency_combo,
            self.global_frequency_combo,
        ):
            _combo.installEventFilter(self._wheel_blocker)
        self.readonly_thickness_label = self._require_child(QLabel, "readonly_thickness_label")
        self.readonly_dk_label = self._require_child(QLabel, "readonly_dk_label")
        self.readonly_df_label = self._require_child(QLabel, "readonly_df_label")
        self.readonly_freq_label = self._require_child(QLabel, "readonly_freq_label")
        self.symmetry_badge = self._require_child(QLabel, "symmetry_badge")
        self.note_label = self._require_child(QLabel, "note_label")
        self.editor_scroll = self._require_child(QScrollArea, "editor_scroll")
        self.editor_content = self._require_child(QWidget, "editor_content")
        self.table_group = self._nearest_group_box(self.table)
        self.editor_group = self._nearest_group_box(self.detail_title_label)
        logger.debug("Main window UI loaded and widget references resolved")
        self.snapshot_group = self._nearest_group_box(self.metric_total_host)
        self.preview_group = self._nearest_group_box(self.preview_host)
        self.note_group = self._nearest_group_box(self.note_label)
        self.copyright_frame = self.findChild(QFrame, "frame")
        self.left_content_splitter: QSplitter | None = None
        self.action_rows_layout = self.findChild(QGridLayout, "action_rows")

        self.file_title_label = self._require_child(QLabel, "file_title_label")
        self.file_title_label.setObjectName("ToolbarTitle")
        self.file_subtitle_label = self._require_child(QLabel, "file_subtitle_label")
        self.file_subtitle_label.setObjectName("ToolbarSubtitle")
        self.toolbar_subtitle_label = self._require_child(QLabel, "ToolbarSubtitle")
        self._require_child(QLabel, "unit_label").setObjectName("ToolbarSubtitle")
        self.detail_title_label.setObjectName("SectionTitle")
        self.preview_title_label.setObjectName("SectionTitle")
        self.preview_subtitle_label.setObjectName("SectionSubtitle")
        self._require_child(QLabel, "soldermask_page_label").setObjectName("PlaceholderLabel")
        self.note_label.setObjectName("NoteLabel")
        self.copper_roughness_label.setObjectName("ReadonlyValue")
        self.readonly_thickness_label.setObjectName("ReadonlyValue")
        self.readonly_dk_label.setObjectName("ReadonlyValue")
        self.readonly_df_label.setObjectName("ReadonlyValue")
        self.readonly_freq_label.setObjectName("ReadonlyValue")
        self._require_child(QLabel, "readonly_dk_caption").setObjectName("ReadonlyCaption")
        self._require_child(QLabel, "readonly_df_caption").setObjectName("ReadonlyCaption")
        self._require_child(QLabel, "readonly_freq_caption").setObjectName("ReadonlyCaption")
        self.remove_button.setObjectName("DangerButton")
        for button in (
            self.add_above_button,
            self.add_below_button,
            self.apply_copper_button,
            self.apply_sym_copper_button,
        ):
            button.setObjectName("CopperButton")
        for button in (
            self.calculate_impedance_button,
            self.apply_layer_button,
            self.apply_sym_layer_button,
            self.apply_all_frequency_button,
        ):
            button.setObjectName("PrimaryButton")

        self.main_splitter.setHandleWidth(10)
        self.table.setColumnCount(len(TABLE_COLUMNS))
        self.table.setHorizontalHeaderLabels([title for _key, title in TABLE_COLUMNS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu_requested)
        self.table.itemChanged.connect(self._on_table_item_changed)

        self.unit_combo.clear()
        self.unit_combo.addItems(list(SUPPORTED_UNITS))
        self.unit_combo.setCurrentText(self.display_unit)
        self.unit_combo.currentTextChanged.connect(self._on_display_unit_change)
        if self.is_flex_zone:
            populate_copper_type_combo(
                self.copper_type_combo,
                COPPER_TYPES + FLEX_COPPER_TYPES,
            )
        else:
            populate_copper_type_combo(self.copper_type_combo, COPPER_TYPES)
        self.copper_type_combo.currentTextChanged.connect(self._preview_copper_profile)
        self.dielectric_type_combo.clear()
        if self.is_flex_zone:
            self.dielectric_type_combo.addItems(["flex_core"])
        else:
            self.dielectric_type_combo.addItems(
                [
                    value
                    for _label, value in DIELECTRIC_TYPE_CHOICES
                    if value != "no_flow_prepreg"
                    or self.no_flow_prepreg_available
                ]
            )
        self.dielectric_type_combo.currentTextChanged.connect(self._on_dielectric_type_change)
        self.dielectric_manufacturer_combo.currentTextChanged.connect(self._on_dielectric_manufacturer_change)
        self.dielectric_family_combo.currentTextChanged.connect(self._on_dielectric_family_change)
        self.material_filter_edit.textChanged.connect(self._on_material_filter_change)
        self.dielectric_material_combo.currentIndexChanged.connect(self._on_material_preview_change)
        self.layer_frequency_combo.currentIndexChanged.connect(self._preview_current_material)

        self.add_above_button.clicked.connect(self._add_copper_above)
        self.add_below_button.clicked.connect(self._add_copper_below)
        self.remove_button.clicked.connect(self._remove_selected)
        self.add_material_above_button.clicked.connect(self._add_material_above)
        self.add_material_below_button.clicked.connect(self._add_material_below)
        self.calculate_impedance_button.clicked.connect(self._open_impedance_dialog)
        self.apply_copper_button.clicked.connect(self._apply_copper_edits)
        self.apply_sym_copper_button.clicked.connect(self._apply_symmetric_copper_edits)
        self.apply_layer_button.clicked.connect(self._apply_current_dielectric_settings)
        self.apply_sym_layer_button.clicked.connect(self._apply_symmetric_dielectric_settings)
        self.apply_all_frequency_button.clicked.connect(self._apply_frequency_to_all_dielectrics)

        self._build_file_menu()
        self._build_command_menus()
        self.file_title_label.setText("Analysis")

        self.insert_flex_sandwich_button: QPushButton | None = None
        self.remove_flex_sandwich_button: QPushButton | None = None
        if self.is_flex_zone and self.action_rows_layout is not None:
            self.insert_flex_sandwich_button = QPushButton("Insert Flex Sandwich", self.add_above_button.parentWidget())
            self.insert_flex_sandwich_button.setObjectName("PrimaryButton")
            self.insert_flex_sandwich_button.clicked.connect(self.insertFlexSandwichRequested.emit)
            self.remove_flex_sandwich_button = QPushButton("Remove Flex Sandwich", self.add_above_button.parentWidget())
            self.remove_flex_sandwich_button.setObjectName("SecondaryButton")
            self.remove_flex_sandwich_button.clicked.connect(self.removeFlexSandwichRequested.emit)
            for button in (
                self.add_above_button,
                self.add_below_button,
                self.remove_button,
                self.add_material_above_button,
                self.add_material_below_button,
            ):
                button.hide()
            self.action_rows_layout.addWidget(self.insert_flex_sandwich_button, 0, 0, 1, 2)
            self.action_rows_layout.addWidget(self.remove_flex_sandwich_button, 0, 2, 1, 1)

        self.metric_total_card = MetricCard("Total thickness")
        self.metric_copper_card = MetricCard("Copper layers")
        self.metric_units_card = MetricCard("Display units")
        self.metric_rows_card = MetricCard("Rows")
        self._mount_host_widget(self.metric_total_host, self.metric_total_card)
        self._mount_host_widget(self.metric_copper_host, self.metric_copper_card)
        self._mount_host_widget(self.metric_units_host, self.metric_units_card)
        self._mount_host_widget(self.metric_rows_host, self.metric_rows_card)

        self.preview = LiveStackupWidget()
        self.preview.layerSelected.connect(self._on_preview_layer_selected)
        self.preview.layerContextMenuRequested.connect(self._on_preview_context_menu_requested)
        self._mount_host_widget(self.preview_host, self.preview)
        self.editor_scroll.viewport().setObjectName("EditorScrollViewport")
        for widget in (
            self.editor_content,
            self.editor_stack,
            self.placeholder_page,
            self.soldermask_page,
            self.copper_page,
            self.dielectric_page,
        ):
            widget.setAutoFillBackground(True)

        self.toolbar_subtitle_label.hide()
        self.file_subtitle_label.hide()
        self.snapshot_group.hide()
        if self.copyright_frame is not None:
            self.copyright_frame.hide()
        if not self.is_flex_zone:
            self.note_group.hide()
        # The table itself is now the editor. Keep the legacy widgets alive for
        # compatibility with existing controller code, but remove their panel
        # from the visible layout.
        self.editor_group.hide()
        self._install_symmetry_control()
        self._normalize_loaded_layout()
        self._configure_zone_mode_controls()

        toolbar_card = self.findChild(QWidget, "ToolbarCard")
        if toolbar_card is not None:
            toolbar_card.hide()

    def _install_symmetry_control(self) -> None:
        self.apply_symmetry_checkbox: QCheckBox | None = None
        if self.is_flex_zone:
            return
        left_layout = self.left_pane.layout()
        if left_layout is None:
            return
        insert_index = left_layout.indexOf(self.table_group)
        if insert_index < 0:
            return

        control_bar = QFrame(self.left_pane)
        control_bar.setObjectName("SymmetryControlBar")
        control_layout = QHBoxLayout(control_bar)
        control_layout.setContentsMargins(8, 2, 10, 2)
        control_layout.setSpacing(8)
        control_layout.addStretch(1)

        checkbox = QCheckBox("Apply Symmetry", control_bar)
        checkbox.setObjectName("ApplySymmetryCheck")
        checkbox.setChecked(True)
        checkbox.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        checkbox.setToolTip(
            "Apply copper, dielectric material, and frequency changes to the mirror layer."
        )
        checkbox.toggled.connect(self._on_apply_symmetry_toggled)
        control_layout.addWidget(checkbox)

        left_layout.insertWidget(insert_index, control_bar)
        self.symmetry_control_bar = control_bar
        self.apply_symmetry_checkbox = checkbox

    def _on_apply_symmetry_toggled(self, checked: bool) -> None:
        self._set_note(
            "Table changes will update each mirror layer automatically."
            if checked
            else "Table layers can now be adjusted individually."
        )
        self._update_buttons()

    def _apply_symmetry_enabled(self) -> bool:
        return bool(
            self.apply_symmetry_checkbox is not None
            and self.apply_symmetry_checkbox.isChecked()
        )

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
        self.import_text_action.setStatusTip("Import a stackup from a text file")
        self.import_text_action.triggered.connect(self._import_text)
        self.import_menu.addAction(self.import_text_action)

        self.import_xpedition_action = QAction("Xpedition stackup...", self)
        self.import_xpedition_action.setStatusTip("Import an Xpedition stackup file")
        self.import_xpedition_action.triggered.connect(self._import_xpedition_stackup)
        self.import_menu.addAction(self.import_xpedition_action)

        self.export_text_action = QAction("Stackup text...", self)
        self.export_text_action.setStatusTip("Export the current stackup to a text file")
        self.export_text_action.triggered.connect(self._export_text)
        self.export_menu.addAction(self.export_text_action)

        self.export_xpedition_action = QAction("Xpedition stackup...", self)
        self.export_xpedition_action.setStatusTip("Export the current stackup as an Xpedition file")
        self.export_xpedition_action.triggered.connect(self._export_xpedition_stackup)
        self.export_menu.addAction(self.export_xpedition_action)

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
        self.add_layer_above_action.triggered.connect(self._add_copper_above)
        self.add_layer_below_action = self.build_menu.addAction("Add Layer Below")
        self.add_layer_below_action.triggered.connect(self._add_copper_below)
        self.build_menu.addSeparator()
        self.add_material_above_action = self.build_menu.addAction("Add Material Above")
        self.add_material_above_action.triggered.connect(self._add_material_above)
        self.add_material_below_action = self.build_menu.addAction("Add Material Below")
        self.add_material_below_action.triggered.connect(self._add_material_below)
        self.build_menu.addSeparator()
        self.remove_symmetric_pair_action = self.build_menu.addAction("Remove Symmetric Pair")
        self.remove_symmetric_pair_action.triggered.connect(self._remove_selected)

        self.insert_flex_sandwich_action = QAction("Insert Flex Sandwich", self)
        self.insert_flex_sandwich_action.triggered.connect(self.insertFlexSandwichRequested.emit)
        self.remove_flex_sandwich_action = QAction("Remove Flex Sandwich", self)
        self.remove_flex_sandwich_action.triggered.connect(self.removeFlexSandwichRequested.emit)
        self.flex_menu: QMenu | None = None
        if self.is_flex_zone:
            self.flex_menu = self.menuBar().addMenu("&Flex")
            self.flex_menu.addAction(self.insert_flex_sandwich_action)
            self.flex_menu.addAction(self.remove_flex_sandwich_action)

        self.analysis_menu = self.menuBar().addMenu("&Analysis")
        self.calculate_impedance_action = self.analysis_menu.addAction("Calculate Impedance")
        self.calculate_impedance_action.triggered.connect(self._open_impedance_dialog)

        self.units_menu = self.menuBar().addMenu("&Units")
        self.unit_action_group = QActionGroup(self)
        self.unit_action_group.setExclusive(True)
        self.unit_actions: dict[str, QAction] = {}
        for unit in SUPPORTED_UNITS:
            action = QAction(unit, self, checkable=True)
            action.setData(unit)
            action.setChecked(unit == self.display_unit)
            action.triggered.connect(lambda _checked=False, value=unit: self.unit_combo.setCurrentText(value))
            self.unit_action_group.addAction(action)
            self.units_menu.addAction(action)
            self.unit_actions[unit] = action

        self.help_menu = self.menuBar().addMenu("&Help")
        self.material_comparison_action = self.help_menu.addAction("Material Comparison")
        self.material_comparison_action.setStatusTip(
            "Compare dielectric material families, constructions, Dk, Df, and availability"
        )
        self.material_comparison_action.triggered.connect(self._open_material_comparison)

    def _open_material_comparison(self) -> None:
        if self._material_comparison_dialog is None:
            self._material_comparison_dialog = MaterialComparisonDialog(self.catalog, self)
        self._material_comparison_dialog.show()
        self._material_comparison_dialog.raise_()
        self._material_comparison_dialog.activateWindow()

    def _sync_command_menu_state(self) -> None:
        self.add_layer_above_action.setEnabled(not self.is_flex_zone and self.add_above_button.isEnabled())
        self.add_layer_below_action.setEnabled(not self.is_flex_zone and self.add_below_button.isEnabled())
        self.add_material_above_action.setEnabled(
            not self.is_flex_zone and self.add_material_above_button.isEnabled()
        )
        self.add_material_below_action.setEnabled(
            not self.is_flex_zone and self.add_material_below_button.isEnabled()
        )
        self.remove_symmetric_pair_action.setEnabled(
            not self.is_flex_zone
            and self._apply_symmetry_enabled()
            and self.remove_button.isEnabled()
        )
        self.build_menu.menuAction().setEnabled(not self.is_flex_zone)

        self.insert_flex_sandwich_action.setEnabled(self.is_flex_zone)
        self.remove_flex_sandwich_action.setEnabled(
            self.is_flex_zone and self.stackup.flex_core_count() > 1
        )
        if self.flex_menu is not None:
            self.flex_menu.menuAction().setEnabled(self.is_flex_zone)

        self.calculate_impedance_action.setEnabled(self.calculate_impedance_button.isEnabled())
        current_unit_action = self.unit_actions.get(self.display_unit)
        if current_unit_action is not None:
            current_unit_action.setChecked(True)

    def _context_actions_for_meta(self, meta: tuple[str, int | str]) -> list[QAction]:
        if meta[0] != "layer":
            return []
        index = int(meta[1])
        if index < 0 or index >= len(self.stackup.layers):
            return []
        layer = self.stackup.layers[index]

        if isinstance(layer, FlexCoreLayer):
            if not self.is_flex_zone:
                return []
            actions = [self.insert_flex_sandwich_action]
            if self.remove_flex_sandwich_action.isEnabled():
                actions.append(self.remove_flex_sandwich_action)
            return actions

        if self.is_flex_zone:
            return []
        if isinstance(layer, CopperLayer):
            actions = [self.add_layer_above_action, self.add_layer_below_action]
        elif isinstance(layer, DielectricLayer):
            actions = [self.add_material_above_action, self.add_material_below_action]
        else:
            return []
        if (
            self.remove_symmetric_pair_action.isEnabled()
            or not self._apply_symmetry_enabled()
        ):
            actions.append(self.remove_symmetric_pair_action)
        return actions

    def _show_build_context_menu(self, meta: tuple[str, int | str], global_pos: object) -> None:
        self._select_row_meta(meta)
        self._update_buttons()
        if callable(self.build_context_menu_handler):
            handled = bool(self.build_context_menu_handler(meta, global_pos))
            if handled:
                return

        actions = self._context_actions_for_meta(meta)
        if not actions:
            return
        menu = QMenu(self)
        for position, action in enumerate(actions):
            if position == 2 and action is self.remove_symmetric_pair_action:
                menu.addSeparator()
            menu.addAction(action)
        menu.exec(global_pos)

    def _on_table_context_menu_requested(self, position) -> None:
        row = self.table.rowAt(position.y())
        if row < 0 or row >= len(self._row_meta):
            return
        meta = self._row_meta[row]
        self._show_build_context_menu(meta, self.table.viewport().mapToGlobal(position))

    def _on_preview_context_menu_requested(self, index: int, global_pos: object) -> None:
        self._show_build_context_menu(("layer", index), global_pos)

    def _configure_zone_mode_controls(self) -> None:
        if not self.is_flex_zone:
            return
        self.add_above_button.setEnabled(False)
        self.add_below_button.setEnabled(False)
        self.add_material_above_button.setEnabled(False)
        self.add_material_below_button.setEnabled(False)
        self.remove_button.setEnabled(False)
        self.import_xpedition_action.setEnabled(False)
        self.import_text_action.setEnabled(False)
        self.export_xpedition_action.setEnabled(False)
        self.export_text_action.setEnabled(False)
        self.calculate_impedance_button.setEnabled(True)
        self.apply_all_frequency_button.setEnabled(False)
        self.apply_sym_layer_button.setEnabled(False)
        self.import_xpedition_action.setStatusTip("Flex-zone import is not wired yet.")
        self.import_text_action.setStatusTip("Flex-zone import is not wired yet.")
        self.export_xpedition_action.setStatusTip("Flex-zone export is not wired yet.")
        self.export_text_action.setStatusTip("Flex-zone export is not wired yet.")
        self.calculate_impedance_button.setToolTip(
            "Calculate flex-zone impedance using Flex Core and the combined coverlay construction."
        )
        self._sync_command_menu_state()

    def _load_main_window_ui(self) -> QMainWindow:
        ui_path = self.root_path / "stackup_editor" / "ui" / "stackup_editor_main.ui"
        ui_file = QFile(str(ui_path))
        if not ui_file.open(QFile.OpenModeFlag.ReadOnly):
            raise RuntimeError(f"Could not open UI file:\n{ui_path}")
        try:
            loaded = QUiLoader().load(ui_file, self)
        finally:
            ui_file.close()
        if loaded is None or not isinstance(loaded, QMainWindow):
            raise RuntimeError(f"Could not load a QMainWindow from:\n{ui_path}")
        return loaded

    def _require_child(self, widget_type, object_name: str):
        child = self.findChild(widget_type, object_name)
        if child is None:
            raise RuntimeError(f"Required widget '{object_name}' was not found in the UI file.")
        return child

    def _nearest_group_box(self, widget: QWidget) -> QGroupBox:
        current = widget.parentWidget()
        while current is not None and not isinstance(current, QGroupBox):
            current = current.parentWidget()
        if current is None:
            raise RuntimeError(f"Could not find a parent group box for widget '{widget.objectName()}'.")
        return current

    def _mount_host_widget(self, host: QWidget, child: QWidget) -> None:
        layout = host.layout()
        if layout is None:
            layout = QVBoxLayout(host)
            layout.setContentsMargins(0, 0, 0, 0)
        while layout.count():
            item = layout.takeAt(0)
            old_widget = item.widget()
            if old_widget is not None:
                old_widget.setParent(None)
        layout.addWidget(child)

    def _install_left_pane_splitter(self) -> None:
        left_layout = self.left_pane.layout()
        if left_layout is None:
            return
        if self.left_content_splitter is not None:
            return

        insert_index = left_layout.indexOf(self.table_group)
        if insert_index < 0:
            return

        left_layout.removeWidget(self.table_group)
        left_layout.removeWidget(self.editor_group)

        splitter = QSplitter(Qt.Orientation.Vertical, self.left_pane)
        splitter.setObjectName("left_content_splitter")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)
        splitter.addWidget(self.table_group)
        splitter.addWidget(self.editor_group)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([520, 280])

        left_layout.insertWidget(insert_index, splitter, 1)
        self.left_content_splitter = splitter

    def _normalize_loaded_layout(self) -> None:
        unlimited = QSize(16777215, 16777215)
        for widget in (
            self.left_pane,
            self.right_pane,
            self.table_group,
            self.editor_group,
            self.editor_scroll,
            self.editor_content,
            self.snapshot_group,
            self.preview_group,
            self.note_group,
            self.preview_host,
            self.table,
        ):
            widget.setMaximumSize(unlimited)

        self.left_pane.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.right_pane.setMinimumWidth(360)
        self.right_pane.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.table_group.setMinimumHeight(220)
        self.table_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        if self.left_pane.layout() is not None:
            self.left_pane.layout().setSpacing(12)
        if self.right_pane.layout() is not None:
            self.right_pane.layout().setSpacing(10)

        self.snapshot_group.setMinimumHeight(124)
        self.snapshot_group.setMaximumHeight(148)
        self.snapshot_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        self.editor_group.setMinimumHeight(220)
        self.editor_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.editor_scroll.setMinimumHeight(180)
        self.editor_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.editor_content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.preview_group.setMinimumHeight(300)
        self.preview_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.preview_host.setMinimumHeight(240)
        self.preview_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        if not self.is_flex_zone:
            # Keep the rigid Stackup preview at its minimum width while allowing
            # it to consume all available vertical space.
            self.right_pane.setFixedWidth(340)
            self.preview_group.setSizePolicy(
                QSizePolicy.Policy.Fixed,
                QSizePolicy.Policy.Expanding,
            )
            self.preview_host.setFixedWidth(300)
            self.preview_host.setMinimumHeight(324)
            self.preview_host.setMaximumHeight(16777215)
            self.preview_host.setSizePolicy(
                QSizePolicy.Policy.Fixed,
                QSizePolicy.Policy.Expanding,
            )
            self.preview.setFixedWidth(300)
            self.preview.setMinimumHeight(324)
            self.preview.setMaximumHeight(16777215)
            self.preview.setSizePolicy(
                QSizePolicy.Policy.Fixed,
                QSizePolicy.Policy.Expanding,
            )

        self.note_group.setMinimumHeight(58)
        self.note_group.setMaximumHeight(96)
        self.note_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setStretchFactor(0, 7)
        self.main_splitter.setStretchFactor(1, 3)
        self.main_splitter.setSizes([1120, 480])

        self.setMinimumSize(1080, 680)
        self._update_responsive_metrics(force=True)

    def _wrap_page(self, widget: QWidget) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget)
        layout.addStretch(1)
        return wrap

    def _build_copper_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.addWidget(self._field_label("Copper type"), 0, 0)
        self.copper_type_combo = NoScrollComboBox()
        populate_copper_type_combo(self.copper_type_combo, COPPER_TYPES)
        self.copper_type_combo.currentTextChanged.connect(self._preview_copper_profile)
        grid.addWidget(self.copper_type_combo, 0, 1)
        grid.addWidget(self._field_label("Thickness"), 0, 2)
        self.copper_thickness_edit = QLineEdit()
        grid.addWidget(self.copper_thickness_edit, 0, 3)
        grid.addWidget(self._field_label("Surface roughness"), 0, 4)
        self.copper_roughness_label = QLabel("")
        self.copper_roughness_label.setObjectName("ReadonlyValue")
        grid.addWidget(self.copper_roughness_label, 0, 5)
        layout.addLayout(grid)

        action_row = QHBoxLayout()
        self.apply_copper_button = self._make_button("Apply Copper", self._apply_copper_edits, object_name="CopperButton")
        self.apply_sym_copper_button = self._make_button(
            "Apply Symetrically Copper",
            self._apply_symmetric_copper_edits,
            object_name="CopperButton",
        )
        action_row.addWidget(self.apply_copper_button)
        action_row.addWidget(self.apply_sym_copper_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)
        layout.addStretch(1)
        return page

    def _build_dielectric_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.addWidget(self._field_label("Dielectric type"), 0, 0)
        self.dielectric_type_combo = NoScrollComboBox()
        self.dielectric_type_combo.addItems(
            [
                value
                for _label, value in DIELECTRIC_TYPE_CHOICES
                if value != "no_flow_prepreg"
                or self.no_flow_prepreg_available
            ]
        )
        self.dielectric_type_combo.currentTextChanged.connect(self._on_dielectric_type_change)
        grid.addWidget(self.dielectric_type_combo, 0, 1)
        grid.addWidget(self._field_label("Manufacturer"), 0, 2)
        self.dielectric_manufacturer_combo = NoScrollComboBox()
        self.dielectric_manufacturer_combo.currentTextChanged.connect(self._on_dielectric_manufacturer_change)
        grid.addWidget(self.dielectric_manufacturer_combo, 0, 3)

        grid.addWidget(self._field_label("Family"), 1, 0)
        self.dielectric_family_combo = NoScrollComboBox()
        self.dielectric_family_combo.currentTextChanged.connect(self._on_dielectric_family_change)
        grid.addWidget(self.dielectric_family_combo, 1, 1, 1, 2)
        grid.addWidget(self._field_label("Material filter"), 2, 0)
        self.material_filter_edit = QLineEdit()
        self.material_filter_edit.textChanged.connect(self._on_material_filter_change)
        grid.addWidget(self.material_filter_edit, 2, 1, 1, 2)
        grid.addWidget(self._field_label("Material entry"), 3, 0)
        self.dielectric_material_combo = NoScrollComboBox()
        self.dielectric_material_combo.currentIndexChanged.connect(self._on_material_preview_change)
        grid.addWidget(self.dielectric_material_combo, 3, 1, 1, 3)
        layout.addLayout(grid)

        apply_row = QHBoxLayout()
        self.apply_layer_button = self._make_button("Apply Layer", self._apply_current_dielectric_settings, object_name="PrimaryButton")
        self.apply_sym_layer_button = self._make_button(
            "Symmetrically Apply Layer",
            self._apply_symmetric_dielectric_settings,
            object_name="PrimaryButton",
        )
        apply_row.addWidget(self.apply_layer_button)
        apply_row.addWidget(self.apply_sym_layer_button)
        apply_row.addStretch(1)
        layout.addLayout(apply_row)

        freq_row = QGridLayout()
        freq_row.setHorizontalSpacing(12)
        freq_row.setVerticalSpacing(10)
        freq_row.addWidget(self._field_label("Layer frequency"), 0, 0)
        self.layer_frequency_combo = NoScrollComboBox()
        self.layer_frequency_combo.currentIndexChanged.connect(self._preview_current_material)
        freq_row.addWidget(self.layer_frequency_combo, 0, 1)
        freq_row.addWidget(self._field_label("All dielectrics"), 0, 2)
        self.global_frequency_combo = NoScrollComboBox()
        freq_row.addWidget(self.global_frequency_combo, 0, 3)
        self.apply_all_frequency_button = self._make_button(
            "Apply To All",
            self._apply_frequency_to_all_dielectrics,
            object_name="PrimaryButton",
        )
        freq_row.addWidget(self.apply_all_frequency_button, 0, 4)
        layout.addLayout(freq_row)

        readonly_frame = QFrame()
        readonly_frame.setObjectName("ReadonlyCard")
        readonly_layout = QHBoxLayout(readonly_frame)
        readonly_layout.setContentsMargins(12, 10, 12, 10)
        readonly_layout.setSpacing(18)
        self.readonly_thickness_label = self._readonly_pair(readonly_layout, "Thickness")
        self.readonly_dk_label = self._readonly_pair(readonly_layout, "Dk")
        self.readonly_df_label = self._readonly_pair(readonly_layout, "Df")
        self.readonly_freq_label = self._readonly_pair(readonly_layout, "Frequency")
        readonly_layout.addStretch(1)
        layout.addWidget(readonly_frame)
        layout.addStretch(1)
        return page

    def _readonly_pair(self, layout: QHBoxLayout, label_text: str) -> QLabel:
        wrap = QVBoxLayout()
        wrap.setSpacing(2)
        caption = QLabel(label_text)
        caption.setObjectName("ReadonlyCaption")
        value = QLabel("")
        value.setObjectName("ReadonlyValue")
        wrap.addWidget(caption)
        wrap.addWidget(value)
        layout.addLayout(wrap)
        return value

    def _field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("FieldLabel")
        return label

    def _make_button(self, text: str, handler, *, object_name: str | None = None) -> QPushButton:
        button = QPushButton(text)
        if object_name:
            button.setObjectName(object_name)
        button.clicked.connect(handler)
        return button

    def _apply_stylesheet(self, scale: float | None = None) -> None:
        if scale is None:
            scale = self._ui_scale

        def px(value: float) -> int:
            return max(1, round(value * scale))

        stylesheet = f"""
            QMainWindow, QDialog, QMessageBox, QFileDialog, QInputDialog {{
                background: {self.palette_map['bg']};
            }}
            QWidget {{
                color: {self.palette_map['text']};
                font-family: "Segoe UI";
                font-size: {max(9, round(10 * scale))}pt;
            }}
            QMenuBar {{
                background: {self.palette_map['bg']};
                color: {self.palette_map['text']};
                border-bottom: 1px solid {self.palette_map['border']};
                padding: {px(2)}px {px(6)}px;
            }}
            QMenuBar::item {{
                background: transparent;
                border-radius: {px(5)}px;
                padding: {px(5)}px {px(10)}px;
            }}
            QMenuBar::item:selected,
            QMenuBar::item:pressed {{
                background: {self.palette_map['surface_soft']};
            }}
            QMenu {{
                background: {self.palette_map['surface']};
                color: {self.palette_map['text']};
                border: 1px solid {self.palette_map['border']};
                padding: {px(5)}px;
            }}
            QFrame#ConstructionPickerPopup {{
                background: {self.palette_map['surface']};
                border: 1px solid {self.palette_map['border']};
                border-radius: {px(8)}px;
            }}
            QMenu::item {{
                border-radius: {px(5)}px;
                padding: {px(7)}px {px(28)}px {px(7)}px {px(12)}px;
            }}
            QMenu::item:selected {{
                background: {self.palette_map['accent']};
                color: #f8fcff;
            }}
            QMenu::item:disabled {{
                color: #5f7386;
            }}
            #HeaderCard, #ToolbarCard, QGroupBox, #MetricCard, #ReadonlyCard {{
                background: {self.palette_map['surface']};
                border: 1px solid {self.palette_map['border']};
                border-radius: {px(16)}px;
            }}
            #ToolbarCard {{
                background: {self.palette_map['surface_alt']};
            }}
            QFrame#SymmetryControlBar {{
                background: transparent;
                border: none;
            }}
            QCheckBox#ApplySymmetryCheck {{
                color: {self.palette_map['text']};
                font-family: "Bahnschrift";
                font-weight: 700;
                spacing: {px(8)}px;
                padding: {px(4)}px {px(8)}px;
            }}
            QGroupBox {{
                margin-top: {px(10)}px;
                padding-top: {px(6)}px;
                font-family: "Bahnschrift";
                font-size: {max(10, round(11 * scale))}pt;
                font-weight: 700;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: {px(14)}px;
                padding: 0 {px(4)}px 0 {px(4)}px;
                color: {self.palette_map['text']};
            }}
            #HeaderTitle {{
                font-family: "Bahnschrift";
                font-size: {max(20, round(25 * scale))}pt;
                font-weight: 700;
                color: {self.palette_map['text']};
            }}
            #HeaderSubtitle, #ToolbarSubtitle, #SectionSubtitle, #NoteLabel, #PlaceholderLabel, #MetricCaption, #ReadonlyCaption {{
                color: {self.palette_map['text_muted']};
            }}
            #HeaderBadge, #SymmetryBadgeOk, #SymmetryBadgeWarn {{
                background: {self.palette_map['surface_soft']};
                border: 1px solid {self.palette_map['border']};
                border-radius: {px(12)}px;
                padding: {px(7)}px {px(12)}px;
                font-family: "Bahnschrift";
                font-size: {max(9, round(10 * scale))}pt;
                font-weight: 700;
            }}
            #SymmetryBadgeOk {{
                background: #173928;
                color: #8fe2ac;
                border-color: #214f37;
            }}
            #SymmetryBadgeWarn {{
                background: #3c1b18;
                color: #ff9f8c;
                border-color: #5f2821;
            }}
            #ToolbarTitle, #SectionTitle {{
                font-family: "Bahnschrift";
                font-size: {max(11, round(12 * scale))}pt;
                font-weight: 700;
                color: {self.palette_map['text']};
            }}
            #MetricCaption {{
                font-size: {max(8, round(8 * scale))}pt;
            }}
            #MetricValue {{
                font-family: "Bahnschrift";
                font-size: {max(11, round(13 * scale))}pt;
                font-weight: 700;
                color: {self.palette_map['text']};
            }}
            QLineEdit, QComboBox, QTableWidget, QStackedWidget#editor_stack, QWidget#editor_content,
            QWidget#placeholder_page, QWidget#soldermask_page, QWidget#copper_page, QWidget#dielectric_page,
            QWidget#EditorScrollViewport {{
                background: {self.palette_map['input']};
                border: 1px solid {self.palette_map['border']};
                border-radius: {px(10)}px;
                padding: {px(6)}px {px(8)}px;
                color: {self.palette_map['text']};
                selection-background-color: {self.palette_map['accent']};
            }}
            QWidget#editor_content, QWidget#placeholder_page, QWidget#soldermask_page,
            QWidget#copper_page, QWidget#dielectric_page, QWidget#EditorScrollViewport {{
                background: {self.palette_map['surface']};
            }}
            QComboBox::drop-down {{
                border: none;
                width: {px(26)}px;
            }}
            QComboBox QAbstractItemView {{
                background: {self.palette_map['input']};
                color: {self.palette_map['text']};
                border: 1px solid {self.palette_map['border']};
                border-radius: {px(6)}px;
                selection-background-color: {self.palette_map['accent']};
                selection-color: {self.palette_map['text']};
                outline: none;
            }}
            QComboBox QAbstractItemView::item {{
                padding: {px(5)}px {px(8)}px;
                min-height: {px(22)}px;
                color: {self.palette_map['text']};
                background: transparent;
            }}
            QComboBox QAbstractItemView::item:hover {{
                background: {self.palette_map['surface_soft']};
                color: {self.palette_map['text']};
            }}
            QComboBox QAbstractItemView::item:selected {{
                background: {self.palette_map['accent']};
                color: {self.palette_map['text']};
            }}
            QTabBar::tab {{
                background: {self.palette_map['surface_soft']};
                color: {self.palette_map['text_muted']};
                border: 1px solid {self.palette_map['border']};
                border-bottom: none;
                border-top-left-radius: {px(10)}px;
                border-top-right-radius: {px(10)}px;
                padding: {px(7)}px {px(12)}px;
                margin-right: {px(4)}px;
                font-family: "Bahnschrift";
                font-size: {max(9, round(10 * scale))}pt;
                font-weight: 700;
            }}
            QTabBar::tab:hover {{
                background: {self.palette_map['surface_alt']};
                color: {self.palette_map['text']};
            }}
            QTabBar::tab:selected {{
                background: {self.palette_map['accent']};
                border-color: {self.palette_map['accent']};
                color: #f8fcff;
            }}
            QPushButton {{
                background: {self.palette_map['surface_soft']};
                border: 1px solid {self.palette_map['border']};
                border-radius: {px(11)}px;
                padding: {px(8)}px {px(12)}px;
                min-height: {px(18)}px;
                color: {self.palette_map['text']};
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {self.palette_map['surface_alt']};
            }}
            QPushButton#PrimaryButton {{
                background: {self.palette_map['accent']};
                border-color: {self.palette_map['accent']};
                color: #f8fcff;
            }}
            QPushButton#PrimaryButton:hover {{
                background: {self.palette_map['accent_active']};
            }}
            QPushButton#CopperButton {{
                background: {self.palette_map['copper']};
                border-color: {self.palette_map['copper']};
                color: #1f1408;
            }}
            QPushButton#CopperButton:hover {{
                background: {self.palette_map['copper_active']};
            }}
            QPushButton#DangerButton {{
                background: {self.palette_map['danger']};
                border-color: {self.palette_map['danger']};
                color: #fff6f2;
            }}
            QPushButton:disabled {{
                color: #5f7386;
                background: #152433;
                border-color: #1d3146;
            }}
            QHeaderView::section {{
                background: {self.palette_map['tree_head']};
                color: {self.palette_map['text']};
                border: none;
                padding: {px(8)}px;
                font-family: "Bahnschrift";
                font-weight: 700;
            }}
            QTableWidget {{
                gridline-color: #22374b;
                alternate-background-color: {self.palette_map['tree_alt']};
            }}
            QTableWidget::item:selected {{
                background: {self.palette_map['danger']};
                color: #fff6f2;
            }}
            QTableWidget::item:selected:active {{
                background: {self.palette_map['danger']};
                color: #fff6f2;
            }}
            QListView, QTreeView {{
                background: {self.palette_map['input']};
                color: {self.palette_map['text']};
                border: 1px solid {self.palette_map['border']};
                selection-background-color: {self.palette_map['accent']};
                selection-color: {self.palette_map['text']};
                alternate-background-color: {self.palette_map['tree_alt']};
                outline: none;
            }}
            QToolButton {{
                background: {self.palette_map['surface_soft']};
                border: 1px solid {self.palette_map['border']};
                border-radius: {px(9)}px;
                padding: {px(5)}px {px(8)}px;
                color: {self.palette_map['text']};
            }}
            QToolButton:hover {{
                background: {self.palette_map['surface_alt']};
            }}
            QScrollArea, QScrollArea > QWidget > QWidget {{
                border: none;
                background: transparent;
            }}
            QScrollBar:vertical {{
                background: {self.palette_map['surface']};
                width: {px(14)}px;
                margin: {px(2)}px;
                border-radius: {px(7)}px;
            }}
            QScrollBar::handle:vertical {{
                background: {self.palette_map['accent']};
                min-height: {px(28)}px;
                border-radius: {px(7)}px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {self.palette_map['accent_active']};
            }}
            QScrollBar:horizontal {{
                background: {self.palette_map['surface']};
                height: {px(14)}px;
                margin: {px(2)}px;
                border-radius: {px(7)}px;
            }}
            QScrollBar::handle:horizontal {{
                background: {self.palette_map['accent']};
                min-width: {px(28)}px;
                border-radius: {px(7)}px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: {self.palette_map['accent_active']};
            }}
            QScrollBar::add-line, QScrollBar::sub-line,
            QScrollBar::add-page, QScrollBar::sub-page {{
                background: transparent;
                border: none;
            }}
            QSplitter::handle {{
                background: {self.palette_map['border']};
                border-radius: {px(3)}px;
            }}
            QSplitter::handle:hover {{
                background: {self.palette_map['accent']};
            }}
            QSplitter::handle:pressed {{
                background: {self.palette_map['accent_active']};
            }}
            """
        app = QApplication.instance()
        if app is not None:
            # Applying a QApplication stylesheet re-polishes every widget in
            # every zone. Rigid-flex imports create several identical hidden
            # editors, so avoid repeating the same application-wide work.
            if app.styleSheet() != stylesheet:
                app.setStyleSheet(stylesheet)
        else:
            self.setStyleSheet(stylesheet)

    def _compute_ui_scale(self) -> float:
        width_factor = self.width() / 1660.0
        height_factor = self.height() / 940.0
        return max(0.9, min(1.22, min(width_factor, height_factor)))

    def _update_responsive_metrics(self, *, force: bool = False) -> None:
        scale = self._compute_ui_scale()
        if not force and abs(scale - self._ui_scale) < 0.03:
            return
        self._ui_scale = scale
        self._apply_stylesheet(scale)
        self.table.verticalHeader().setDefaultSectionSize(max(26, round(30 * scale)))
        if hasattr(self, "preview") and self.preview is not None and self.is_flex_zone:
            self.preview.setMinimumSize(max(300, round(320 * scale)), max(260, round(360 * scale)))
        if self.is_flex_zone:
            self.right_pane.setMinimumWidth(max(340, round(360 * scale)))
        else:
            self.right_pane.setFixedWidth(340)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update_responsive_metrics()

    def _run_ui_wait(self, ms: int) -> None:
        loop = QEventLoop(self)
        QApplication.instance().processEvents()
        from PySide6.QtCore import QTimer

        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def _default_dialog_directory(self) -> Path:
        location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        if location:
            return Path(location)
        return Path.home()

    def _default_dialog_path(self, filename: str) -> Path:
        return self._default_dialog_directory() / filename

    def _frequency_label(self, freq_ghz: float) -> str:
        return format_frequency_ghz(freq_ghz)

    def _thickness_text(self, thickness_mm: float, *, is_copper: bool) -> str:
        return format_stackup_thickness(thickness_mm, self.display_unit, is_copper=is_copper)

    def _dielectric_entry(self, layer: DielectricLikeLayer) -> MaterialEntry | None:
        if isinstance(layer, FlexCoreLayer):
            return None
        return self.stackup.dielectric_entry(layer, self.catalog)

    def _dielectric_material_name(self, layer: DielectricLikeLayer) -> str:
        return self.stackup.dielectric_description(layer, self.catalog)

    def _dielectric_manufacturer_text(self, layer: DielectricLikeLayer) -> str:
        return self.stackup.dielectric_manufacturer(layer, self.catalog) or ""

    def _dielectric_construction_text(self, layer: DielectricLikeLayer) -> str:
        return self.stackup.dielectric_construction(layer, self.catalog) or ""

    def _dielectric_resin_text(self, layer: DielectricLikeLayer) -> str:
        if isinstance(layer, FlexCoreLayer):
            return ""
        resin_pct = self.stackup.dielectric_resin_content_pct(layer, self.catalog)
        return f"{resin_pct:.1f}%" if resin_pct is not None else ""

    def _dielectric_thickness_text(self, layer: DielectricLikeLayer) -> str:
        thickness_mm = self.stackup.dielectric_thickness_mm(layer, self.catalog)
        return self._thickness_text(thickness_mm, is_copper=False) if thickness_mm is not None else ""

    def _dielectric_frequency_text(self, layer: DielectricLikeLayer) -> str:
        freq = self.stackup.dielectric_frequency_ghz_or_none(layer, self.catalog)
        return self._frequency_label(freq) if freq is not None else ""

    def _dielectric_dk_df_text(self, layer: DielectricLikeLayer) -> tuple[str, str]:
        dk, df = self.stackup.dielectric_dk_df_or_none(layer, self.catalog)
        return (f"{dk:.3f}" if dk is not None else "", f"{df:.4f}" if df is not None else "")

    def _current_flex_core_entry(self) -> FlexCoreEntry | None:
        if self.flex_core_catalog is None:
            return None
        material_id = self.dielectric_material_combo.currentData()
        if not material_id:
            return None
        return self.flex_core_catalog.get(str(material_id))

    def _current_dielectric_catalog_entry(self) -> MaterialEntry | None:
        material_id = self.dielectric_material_combo.currentData()
        if not material_id:
            return None
        return self.catalog.get(str(material_id))

    def _coverlay_meta_parts(self, meta_key: str) -> tuple[int, str, str] | None:
        if not meta_key.startswith("coverlay_"):
            return None
        remainder = meta_key[len("coverlay_") :]
        parts = remainder.split("_")
        if len(parts) == 2:
            side, component = parts
            if side not in {"top", "bottom"} or component not in {"pi", "adhesive"}:
                return None
            sandwich_index = 0 if side == "top" else max(0, self.stackup.flex_core_count() - 1)
            return sandwich_index, side, component
        if len(parts) == 3 and parts[0].isdigit():
            sandwich_index = int(parts[0])
            side = parts[1]
            component = parts[2]
            if side not in {"top", "bottom"} or component not in {"pi", "adhesive"}:
                return None
            return sandwich_index, side, component
        return None

    def _coverlay_row_values(self, sandwich_slot: int, side: str, component: str) -> tuple[str, ...]:
        if self.stackup.coverlay is None:
            return ("", "", "", "", "", "", "", "", "", "", "")
        if self.coverlay_row_prefix:
            side_name = "Top" if side == "top" else "Bot"
            component_name = "PI" if component == "pi" else "Adhessive"
            layer_name = f"{self.coverlay_row_prefix}{side_name}Coverlay{component_name}"
        elif max(1, self.stackup.flex_core_count()) == 1:
            layer_name = f"{side.title()} Coverlay {'PI' if component == 'pi' else 'Adhesive'}"
        else:
            layer_name = f"Sandwich {sandwich_slot + 1} {side.title()} Coverlay {'PI' if component == 'pi' else 'Adhesive'}"
        material_name = self.stackup.coverlay.component_label(component)
        thickness_mm = self.stackup.coverlay.component_thickness_mm(component)
        freq = self.stackup.coverlay.component_frequency_ghz(component)
        dk, df = self.stackup.coverlay.component_dk_df(component, freq)
        return (
            layer_name,
            material_name,
            self._thickness_text(thickness_mm, is_copper=False),
            "",
            self.stackup.coverlay.manufacturer,
            self.stackup.coverlay.family,
            "",
            "",
            f"{dk:.3f}" if dk is not None else "",
            f"{df:.4f}" if df is not None else "",
            self._frequency_label(freq) if freq is not None else "",
        )

    def _air_gap_row_values(self, gap_index: int) -> tuple[str, ...]:
        layer_name = "Air Gap" if self.stackup.flex_core_count() <= 2 else f"Air Gap {gap_index + 1}"
        return (
            layer_name,
            "Air Gap",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        )

    def _copper_label(self, index: int) -> str:
        if index in self.copper_number_overrides:
            return f"L{self.copper_number_overrides[index]}"
        return f"L{self.stackup.copper_layer_number(index)}"

    def _current_row_meta(self) -> tuple[str, int | str] | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._row_meta):
            return None
        return self._row_meta[row]

    def _selected_index(self) -> int | None:
        meta = self._current_row_meta()
        if meta and meta[0] == "layer":
            return int(meta[1])
        return None

    def _row_for_meta(self, meta: tuple[str, int | str] | None) -> int | None:
        if meta is None:
            return None
        for row, row_meta in enumerate(self._row_meta):
            if row_meta == meta:
                return row
        return None

    def _select_row_meta(self, meta: tuple[str, int | str] | None) -> None:
        row = self._row_for_meta(meta)
        if row is None and self._row_meta:
            row = 1 if len(self._row_meta) > 1 else 0
        if row is None:
            return
        self.table.selectRow(row)
        self.table.setCurrentCell(row, 0)

    def _set_note(self, text: str) -> None:
        self.note_label.setText(text)

    def _on_display_unit_change(self, new_unit: str) -> None:
        if self._ui_loading:
            return
        if new_unit in {"um", "mm", "mil", "inch"}:
            self.geometry_input_unit = new_unit
        self.display_unit = new_unit
        self._refresh_everything(select_meta=self._current_row_meta())

    def _soldermask_row_values(self, position: str, soldermask: SolderMaskSettings) -> tuple[str, ...]:
        if self.dielectric_row_prefix:
            side_name = "Top" if position == "top" else "Bot"
            layer_name = f"{self.dielectric_row_prefix}{side_name}Soldermask"
        else:
            layer_name = "Top Solder Mask" if position == "top" else "Bottom Solder Mask"
        return (
            layer_name,
            "Solder Mask",
            self._thickness_text(soldermask.thickness_mm, is_copper=False),
            "",
            soldermask.manufacturer,
            "",
            "",
            "",
            f"{soldermask.dk:.3f}",
            f"{soldermask.df:.4f}",
            self._frequency_label(soldermask.freq_ghz),
        )

    def _soldermask_edit_value(self, column_key: str) -> str:
        soldermask = self.stackup.soldermask
        if column_key == "thickness":
            unit = thickness_unit_for_layer(self.display_unit, is_copper=False)
            precision = UNIT_PRECISION[unit]
            return f"{to_display(soldermask.thickness_mm, unit):.{precision}f}"
        if column_key == "dk":
            return f"{soldermask.dk:.3f}"
        if column_key == "df":
            return f"{soldermask.df:.4f}"
        return ""

    def _refresh_table(self, *, select_meta: tuple[str, int | str] | None = None) -> None:
        if select_meta is None:
            select_meta = self._current_row_meta()
        self._table_refreshing = True
        self._row_meta.clear()
        self._layer_row_by_index.clear()
        with QSignalBlocker(self.table):
            self.table.setRowCount(0)
            for meta, values, row_type in self._table_rows():
                row = self.table.rowCount()
                self.table.insertRow(row)
                self._row_meta.append(meta)
                if meta[0] == "layer":
                    self._layer_row_by_index[int(meta[1])] = row
                for col, (column_key, _title) in enumerate(TABLE_COLUMNS):
                    item = QTableWidgetItem(values[col])
                    item.setData(Qt.ItemDataRole.UserRole, meta)
                    flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                    if row_type == "soldermask" and column_key in {"thickness", "dk", "df"}:
                        item.setText(self._soldermask_edit_value(column_key))
                        flags |= Qt.ItemFlag.ItemIsEditable
                    elif (
                        row_type == "copper"
                        and column_key == "thickness"
                        and meta[0] == "layer"
                    ):
                        layer_index = int(meta[1])
                        layer = self.stackup.layers[layer_index]
                        locked = (
                            layer_index in self.locked_copper_indices
                            or (
                                self.is_flex_zone
                                and self._adjacent_flex_core(layer_index) is not None
                            )
                        )
                        if isinstance(layer, CopperLayer):
                            unit = thickness_unit_for_layer(
                                self.display_unit,
                                is_copper=True,
                            )
                            precision = UNIT_PRECISION[unit]
                            item.setText(
                                f"{to_display(layer.thickness_mm, unit):.{precision}f}"
                            )
                            if not locked:
                                flags |= Qt.ItemFlag.ItemIsEditable
                    item.setFlags(flags)
                    self._style_table_item(item, row_type)
                    self.table.setItem(row, col, item)
            self._install_table_editors()
            self.table.resizeColumnsToContents()
            self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
            self._select_row_meta(select_meta)
        self._table_refreshing = False

    def _new_table_combo(
        self,
        items: list[tuple[str, object]],
        *,
        selected_data: object | None,
        enabled: bool,
    ) -> NoScrollComboBox:
        combo = NoScrollComboBox(self.table)
        combo.setObjectName("TableCellCombo")
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(8)
        self._populate_combo(combo, items, selected_data=selected_data)
        combo.setEnabled(enabled)
        return combo

    def _rigid_material_choice_label(self, entry: MaterialEntry) -> str:
        classification = entry.classification or entry.style or "Standard"
        return (
            f"{entry.construction} | {entry.thickness_mm:g}mm | "
            f"RC {entry.resin_content_pct:.1f}% | Dk {entry.reference_dk:g} | "
            f"{classification}"
        )

    def _flex_material_choice_label(self, entry: FlexCoreEntry) -> str:
        return (
            f"{entry.construction_label} | {entry.dielectric_thickness_mm:g}mm | "
            f"Dk {entry.reference_dk:g} | {entry.copper_type}"
        )

    def _install_table_editors(self) -> None:
        for layer_index, row in self._layer_row_by_index.items():
            if not 0 <= layer_index < len(self.stackup.layers):
                continue
            layer = self.stackup.layers[layer_index]
            if is_dielectric_like(layer):
                self._install_dielectric_table_editors(row, layer_index, layer)
            elif isinstance(layer, CopperLayer):
                self._install_copper_table_editors(row, layer_index, layer)

    def _install_dielectric_table_editors(
        self,
        row: int,
        layer_index: int,
        layer: DielectricLikeLayer,
    ) -> None:
        inherited_no_flow = (
            isinstance(layer, DielectricLayer)
            and is_no_flow_prepreg_type(layer.dielectric_type)
            and not self.no_flow_prepreg_editable
        )
        locked = layer_index in self.locked_dielectric_indices or inherited_no_flow
        is_flex_core = isinstance(layer, FlexCoreLayer)
        entry: MaterialEntry | FlexCoreEntry | None
        if is_flex_core:
            entry = (
                self.flex_core_catalog.get(layer.material_id)
                if self.flex_core_catalog is not None and layer.material_id
                else None
            )
            manufacturer = layer.manufacturer
            family = layer.family
            material_type = "flex_core"
            material_type_items: list[tuple[str, object]] = [("Flex Core", "flex_core")]
        else:
            entry = self._dielectric_entry(layer)
            manufacturer = self._dielectric_manufacturer_text(layer) or None
            family = (
                entry.family
                if isinstance(entry, MaterialEntry)
                else layer.family_override
            )
            material_type = layer.dielectric_type
            material_type_items = [
                choice
                for choice in DIELECTRIC_TYPE_CHOICES
                if choice[1] != "no_flow_prepreg"
                or (
                    self.no_flow_prepreg_available
                    and self.no_flow_prepreg_editable
                )
                or is_no_flow_prepreg_type(layer.dielectric_type)
            ]

        type_combo = self._new_table_combo(
            material_type_items,
            selected_data=material_type,
            enabled=not locked and not is_flex_core,
        )
        manufacturer_combo = self._new_table_combo([], selected_data=None, enabled=not locked)
        family_combo = self._new_table_combo([], selected_data=None, enabled=not locked)
        construction_combo = MaterialConstructionCombo(self.table)
        construction_combo.setObjectName("ConstructionMaterialPicker")
        construction_combo.setEnabled(not locked)
        frequency_combo = self._new_table_combo([], selected_data=None, enabled=not locked)

        self.table.setCellWidget(row, TABLE_COLUMN_INDEX["material"], type_combo)
        self.table.setCellWidget(row, TABLE_COLUMN_INDEX["manufacturer"], manufacturer_combo)
        self.table.setCellWidget(row, TABLE_COLUMN_INDEX["dielectric_material"], family_combo)
        self.table.setCellWidget(row, TABLE_COLUMN_INDEX["construction"], construction_combo)
        self.table.setCellWidget(row, TABLE_COLUMN_INDEX["frequency"], frequency_combo)

        # A rigid-zone copy of a connected flex core does not own the flex
        # catalog. Show the inherited values as disabled selectors; its Flex
        # Part table remains the single source of truth for editing.
        if is_flex_core and self.flex_core_catalog is None:
            self._populate_combo(
                manufacturer_combo,
                [(layer.manufacturer, layer.manufacturer)] if layer.manufacturer else [],
                selected_data=layer.manufacturer or None,
            )
            self._populate_combo(
                family_combo,
                [(layer.family, layer.family)] if layer.family else [],
                selected_data=layer.family or None,
            )
            material_id = layer.material_id or "__connected_flex_core__"
            construction_combo.set_materials(
                [
                    (
                        (
                            f"{layer.construction} | {layer.dielectric_thickness_mm:g}mm | "
                            f"Dk {layer.reference_dk:g} | {layer.copper_type}"
                        ),
                        material_id,
                        layer.construction,
                    )
                ],
                selected_id=material_id,
            )
            selected_frequency = layer.closest_frequency(layer.selected_freq_ghz)
            self._populate_combo(
                frequency_combo,
                [
                    (self._frequency_label(value), value)
                    for value in layer.sorted_frequencies
                ],
                selected_data=selected_frequency,
            )
            for combo in (
                type_combo,
                manufacturer_combo,
                family_combo,
                construction_combo,
                frequency_combo,
            ):
                combo.setEnabled(False)
            return

        def selected_text(combo: QComboBox) -> str | None:
            text = combo.currentText().strip()
            return text or None

        def available_entries() -> list[MaterialEntry | FlexCoreEntry]:
            selected_manufacturer = selected_text(manufacturer_combo)
            selected_family = selected_text(family_combo)
            if is_flex_core:
                if self.flex_core_catalog is None:
                    return []
                return list(
                    self.flex_core_catalog.filter_entries(
                        manufacturer=selected_manufacturer,
                        family=selected_family,
                    )
                )
            return list(
                self.catalog.filter_entries(
                    material_type=catalog_material_type_for_dielectric(
                        str(type_combo.currentData() or material_type)
                    ),
                    manufacturer=selected_manufacturer,
                    family=selected_family,
                )
            )

        def populate_constructions(*, selected_id: str | None = None) -> None:
            material_items: list[tuple[str, str, str]] = []
            for candidate in available_entries():
                if isinstance(candidate, FlexCoreEntry):
                    material_items.append(
                        (
                            self._flex_material_choice_label(candidate),
                            candidate.id,
                            candidate.construction_label,
                        )
                    )
                elif isinstance(candidate, MaterialEntry):
                    material_items.append(
                        (
                            self._rigid_material_choice_label(candidate),
                            candidate.id,
                            candidate.construction,
                        )
                    )
            construction_combo.set_materials(material_items, selected_id=selected_id)

        def populate_families(*, preserve: str | None = None) -> None:
            selected_manufacturer = selected_text(manufacturer_combo)
            if is_flex_core:
                families = (
                    self.flex_core_catalog.families(manufacturer=selected_manufacturer)
                    if self.flex_core_catalog is not None
                    else ([layer.family] if layer.family else [])
                )
            else:
                families = self.catalog.families(
                    material_type=catalog_material_type_for_dielectric(
                        str(type_combo.currentData() or material_type)
                    ),
                    manufacturer=selected_manufacturer,
                )
            self._populate_combo(
                family_combo,
                [(value, value) for value in families],
                selected_data=preserve if preserve in families else None,
            )

        def populate_manufacturers(*, preserve: str | None = None) -> None:
            if is_flex_core:
                manufacturers = (
                    self.flex_core_catalog.manufacturers()
                    if self.flex_core_catalog is not None
                    else ([layer.manufacturer] if layer.manufacturer else [])
                )
            else:
                manufacturers = self.catalog.manufacturers(
                    material_type=catalog_material_type_for_dielectric(
                        str(type_combo.currentData() or material_type)
                    )
                )
            self._populate_combo(
                manufacturer_combo,
                [(value, value) for value in manufacturers],
                selected_data=preserve if preserve in manufacturers else None,
            )

        populate_manufacturers(preserve=manufacturer)
        populate_families(preserve=family)
        populate_constructions(selected_id=layer.material_id or None)

        if isinstance(entry, (MaterialEntry, FlexCoreEntry)):
            frequencies = entry.sorted_frequencies
            selected_frequency = entry.closest_frequency(layer.selected_freq_ghz)
        elif isinstance(layer, FlexCoreLayer):
            frequencies = layer.sorted_frequencies
            selected_frequency = layer.closest_frequency(layer.selected_freq_ghz)
        else:
            frequencies = (
                [layer.selected_freq_ghz]
                if layer.selected_freq_ghz is not None
                else []
            )
            selected_frequency = layer.selected_freq_ghz
        self._populate_combo(
            frequency_combo,
            [(self._frequency_label(value), value) for value in frequencies],
            selected_data=selected_frequency,
        )

        type_combo.activated.connect(
            lambda _index: (
                populate_manufacturers(),
                populate_families(),
                populate_constructions(),
            )
        )
        manufacturer_combo.activated.connect(
            lambda _index: (
                populate_families(),
                populate_constructions(),
            )
        )
        family_combo.activated.connect(
            lambda _index: populate_constructions()
        )
        construction_combo.materialSelected.connect(
            lambda material_id: self._apply_table_material(
                layer_index,
                material_id,
                str(type_combo.currentData() or material_type),
            )
        )
        frequency_combo.activated.connect(
            lambda _index: self._apply_table_frequency(
                layer_index,
                frequency_combo.currentData(),
            )
        )

    def _install_copper_table_editors(
        self,
        row: int,
        layer_index: int,
        layer: CopperLayer,
    ) -> None:
        locked = (
            layer_index in self.locked_copper_indices
            or (self.is_flex_zone and self._adjacent_flex_core(layer_index) is not None)
        )
        copper_types = COPPER_TYPES + FLEX_COPPER_TYPES if self.is_flex_zone else COPPER_TYPES
        combo = self._new_table_combo(
            [
                (copper_type_choice_label(copper_type), copper_type)
                for copper_type in copper_types
            ],
            selected_data=layer.copper_type,
            enabled=not locked,
        )
        combo.activated.connect(
            lambda _index: self._apply_table_copper_type(
                layer_index,
                str(combo.currentData() or layer.copper_type),
            )
        )
        self.table.setCellWidget(row, TABLE_COLUMN_INDEX["material"], combo)

    def _table_edit_targets(self, layer_index: int) -> list[int] | None:
        if not self._apply_symmetry_enabled():
            return [layer_index]
        mirror_index = self.stackup.mirror_index(layer_index)
        if mirror_index == layer_index:
            return [layer_index]

        source = self.stackup.layers[layer_index]
        mirror = self.stackup.layers[mirror_index]
        compatible = (
            isinstance(source, CopperLayer)
            and isinstance(mirror, CopperLayer)
        ) or (
            isinstance(source, DielectricLayer)
            and isinstance(mirror, DielectricLayer)
        ) or (
            isinstance(source, FlexCoreLayer)
            and isinstance(mirror, FlexCoreLayer)
        )
        if not compatible:
            QMessageBox.information(
                self,
                "Cannot apply symmetry",
                "The selected row does not have a compatible mirror layer.",
            )
            return None

        locked_indices = (
            self.locked_copper_indices
            if isinstance(source, CopperLayer)
            else self.locked_dielectric_indices
        )
        if mirror_index in locked_indices:
            QMessageBox.information(
                self,
                "Cannot apply symmetry",
                "The mirror layer is controlled by a connected flex part.",
            )
            return None
        return [layer_index, mirror_index]

    def _apply_table_material(
        self,
        layer_index: int,
        material_id: str,
        material_type: str,
    ) -> None:
        if not 0 <= layer_index < len(self.stackup.layers):
            return
        current = self.stackup.layers[layer_index]
        if layer_index in self.locked_dielectric_indices or not is_dielectric_like(current):
            return
        if (
            not self.no_flow_prepreg_editable
            and (
                material_type == "no_flow_prepreg"
                or (
                    isinstance(current, DielectricLayer)
                    and is_no_flow_prepreg_type(current.dielectric_type)
                )
            )
        ):
            self._refresh_everything(select_meta=("layer", layer_index))
            return
        if isinstance(current, FlexCoreLayer):
            if self.flex_core_catalog is None:
                return
            entry = self.flex_core_catalog.get(material_id)
            if entry is None:
                return
            updated: DielectricLikeLayer = FlexCoreLayer.from_entry(
                entry,
                selected_freq_ghz=current.selected_freq_ghz,
            )
        else:
            entry = self.catalog.get(material_id)
            if (
                entry is None
                or entry.material_type
                != catalog_material_type_for_dielectric(material_type)
            ):
                return
            updated = DielectricLayer(
                dielectric_type=material_type,
                material_id=entry.id,
                selected_freq_ghz=entry.closest_frequency(current.selected_freq_ghz),
            )
        target_indices = self._table_edit_targets(layer_index)
        if target_indices is None:
            self._refresh_everything(select_meta=("layer", layer_index))
            return
        replacements = {
            target_index: replace(updated)
            for target_index in target_indices
        }
        bonding_message = self.stackup.bonded_core_bonding_message(replacements)
        if bonding_message is not None:
            QMessageBox.information(
                self,
                "Cannot place bonded core",
                bonding_message,
            )
            self._refresh_everything(select_meta=("layer", layer_index))
            return
        if self.stackup.consecutive_core_pair(replacements) is not None:
            QMessageBox.information(
                self,
                "Cannot place core material",
                "Rigid Core and Flex Core materials must be separated by Rigid PP.",
            )
            self._refresh_everything(select_meta=("layer", layer_index))
            return
        for target_index, replacement in replacements.items():
            self.stackup.layers[target_index] = replacement
        if isinstance(updated, FlexCoreLayer):
            for target_index in target_indices:
                self._sync_flex_core_copper_layers(target_index, updated)
            self._set_note(
                "Flex-core material and adjacent copper layers were updated symmetrically."
                if len(target_indices) > 1
                else "Flex-core material and adjacent copper layers were updated from the table."
            )
            self.flexCoreChanged.emit(updated)
            self.sharedRegionChanged.emit()
        else:
            self._set_note(
                "Dielectric material and its mirror layer were updated together."
                if len(target_indices) > 1
                else "Dielectric material was updated from the table."
            )
            if self.is_flex_zone:
                self.sharedRegionChanged.emit()
        self._refresh_everything(select_meta=("layer", layer_index))

    def _apply_table_frequency(self, layer_index: int, frequency: object) -> None:
        if frequency is None or not 0 <= layer_index < len(self.stackup.layers):
            return
        current = self.stackup.layers[layer_index]
        if layer_index in self.locked_dielectric_indices or not is_dielectric_like(current):
            return
        if (
            isinstance(current, DielectricLayer)
            and is_no_flow_prepreg_type(current.dielectric_type)
            and not self.no_flow_prepreg_editable
        ):
            self._refresh_everything(select_meta=("layer", layer_index))
            return
        requested = float(frequency)
        target_indices = self._table_edit_targets(layer_index)
        if target_indices is None:
            self._refresh_everything(select_meta=("layer", layer_index))
            return
        actual_by_index: dict[int, float] = {}
        for target_index in target_indices:
            target_layer = self.stackup.layers[target_index]
            if isinstance(target_layer, FlexCoreLayer):
                actual = target_layer.closest_frequency(requested)
                updated_layer = replace(
                    target_layer,
                    selected_freq_ghz=actual,
                )
            else:
                entry = self._dielectric_entry(target_layer)
                actual = (
                    entry.closest_frequency(requested)
                    if entry is not None
                    else requested
                )
                updated_layer = replace(
                    target_layer,
                    selected_freq_ghz=actual,
                    # Catalog-backed layers must use the Dk/Df values at the
                    # newly selected frequency. Imported fixed overrides would
                    # otherwise mask the frequency-dependent catalog data.
                    dk_override=None if entry is not None else target_layer.dk_override,
                    df_override=None if entry is not None else target_layer.df_override,
                )
            self.stackup.layers[target_index] = updated_layer
            actual_by_index[target_index] = actual
        updated = self.stackup.layers[layer_index]
        if isinstance(updated, FlexCoreLayer):
            self.flexCoreChanged.emit(updated)
            self.sharedRegionChanged.emit()
        selected_actual = actual_by_index[layer_index]
        self._set_note(
            (
                f"Dielectric frequency changed symmetrically to "
                f"{self._frequency_label(selected_actual)}; Dk and Df were updated."
            )
            if len(target_indices) > 1
            else (
                f"Dielectric frequency changed to "
                f"{self._frequency_label(selected_actual)}; Dk and Df were updated."
            )
        )
        self._refresh_everything(select_meta=("layer", layer_index))

    def _apply_table_copper_type(self, layer_index: int, copper_type: str) -> None:
        if not 0 <= layer_index < len(self.stackup.layers):
            return
        current = self.stackup.layers[layer_index]
        if not isinstance(current, CopperLayer):
            return
        if (
            layer_index in self.locked_copper_indices
            or (self.is_flex_zone and self._adjacent_flex_core(layer_index) is not None)
        ):
            return
        target_indices = self._table_edit_targets(layer_index)
        if target_indices is None:
            self._refresh_everything(select_meta=("layer", layer_index))
            return
        for target_index in target_indices:
            target_layer = self.stackup.layers[target_index]
            if not isinstance(target_layer, CopperLayer):
                continue
            updated = replace(target_layer, copper_type=copper_type)
            updated.sync_roughness()
            self.stackup.layers[target_index] = updated
        self._set_note(
            "Copper type and roughness were updated symmetrically."
            if len(target_indices) > 1
            else "Copper type and roughness were updated from the table."
        )
        self._refresh_everything(select_meta=("layer", layer_index))

    def _table_rows(self) -> list[tuple[tuple[str, int | str], tuple[str, ...], str]]:
        rows: list[tuple[tuple[str, int | str], tuple[str, ...], str]] = []
        if self.is_flex_zone and self.stackup.coverlay is not None:
            sandwich_slots = self.stackup.flex_sandwich_slot_ids()
            for sandwich_index, sandwich_slot in enumerate(sandwich_slots):
                rows.append(
                    (
                        ("coverlay", f"coverlay_{sandwich_slot}_top_pi"),
                        self._coverlay_row_values(sandwich_slot, "top", "pi"),
                        "coverlay_pi",
                    )
                )
                rows.append(
                    (
                        ("coverlay", f"coverlay_{sandwich_slot}_top_adhesive"),
                        self._coverlay_row_values(sandwich_slot, "top", "adhesive"),
                        "coverlay_adhesive",
                    )
                )
                flex_start = sandwich_index * 3
                for index in range(flex_start, min(flex_start + 3, len(self.stackup.layers))):
                    layer = self.stackup.layers[index]
                    if isinstance(layer, CopperLayer):
                        values = (
                            self._copper_label(index),
                            layer.copper_type,
                            self._thickness_text(layer.thickness_mm, is_copper=True),
                            format_roughness_um(layer.roughness_um),
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                        )
                        rows.append((("layer", index), values, "copper"))
                    elif isinstance(layer, FlexCoreLayer):
                        dk_text, df_text = self._dielectric_dk_df_text(layer)
                        layer_name = self.flex_core_row_labels.get(index, "Flex Core")
                        values = (
                            layer_name,
                            "Flex Core",
                            self._dielectric_thickness_text(layer),
                            "",
                            self._dielectric_manufacturer_text(layer),
                            self._dielectric_material_name(layer),
                            self._dielectric_construction_text(layer),
                            "",
                            dk_text,
                            df_text,
                            self._dielectric_frequency_text(layer),
                        )
                        rows.append((("layer", index), values, "dielectric"))
                rows.append(
                    (
                        ("coverlay", f"coverlay_{sandwich_slot}_bottom_adhesive"),
                        self._coverlay_row_values(sandwich_slot, "bottom", "adhesive"),
                        "coverlay_adhesive",
                    )
                )
                rows.append(
                    (
                        ("coverlay", f"coverlay_{sandwich_slot}_bottom_pi"),
                        self._coverlay_row_values(sandwich_slot, "bottom", "pi"),
                        "coverlay_pi",
                    )
                )
                if sandwich_index < len(sandwich_slots) - 1:
                    gap_label = f"air_gap_{sandwich_slot}_{sandwich_slots[sandwich_index + 1]}"
                    rows.append((("gap", gap_label), self._air_gap_row_values(sandwich_index), "gap"))
        else:
            rows.append((("soldermask", "top"), self._soldermask_row_values("top", self.stackup.soldermask), "soldermask"))
            dielectric_number = 0
            for index, layer in enumerate(self.stackup.layers):
                if isinstance(layer, CopperLayer):
                    values = (
                        self._copper_label(index),
                        layer.copper_type,
                        self._thickness_text(layer.thickness_mm, is_copper=True),
                        format_roughness_um(layer.roughness_um),
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                    )
                    rows.append((("layer", index), values, "copper"))
                elif isinstance(layer, FlexCoreLayer):
                    dk_text, df_text = self._dielectric_dk_df_text(layer)
                    layer_name = self.flex_core_row_labels.get(index, "Flex Core")
                    values = (
                        layer_name,
                        "Flex Core",
                        self._dielectric_thickness_text(layer),
                        "",
                        self._dielectric_manufacturer_text(layer),
                        self._dielectric_material_name(layer),
                        self._dielectric_construction_text(layer),
                        "",
                        dk_text,
                        df_text,
                        self._dielectric_frequency_text(layer),
                    )
                    rows.append((("layer", index), values, "dielectric"))
                else:
                    dielectric_number += 1
                    dk_text, df_text = self._dielectric_dk_df_text(layer)
                    layer_name = self.dielectric_row_labels.get(
                        index,
                        (
                            f"{self.dielectric_row_prefix}Dielectric{dielectric_number}"
                            if self.dielectric_row_prefix
                            else f"Dielectric {self.stackup.dielectric_layer_number(index)}"
                        ),
                    )
                    values = (
                        layer_name,
                        dielectric_type_display_name(layer.dielectric_type),
                        self._dielectric_thickness_text(layer),
                        "",
                        self._dielectric_manufacturer_text(layer),
                        self._dielectric_material_name(layer),
                        self._dielectric_construction_text(layer),
                        self._dielectric_resin_text(layer),
                        dk_text,
                        df_text,
                        self._dielectric_frequency_text(layer),
                    )
                    rows.append((("layer", index), values, "dielectric"))
            rows.append((("soldermask", "bottom"), self._soldermask_row_values("bottom", self.stackup.soldermask), "soldermask"))
        return rows

    def _style_table_item(self, item: QTableWidgetItem, row_type: str) -> None:
        if row_type == "copper":
            item.setBackground(QColor("#19304a"))
            item.setForeground(QColor("#f7f9fb"))
        elif row_type == "dielectric":
            item.setBackground(QColor("#152a3d"))
            item.setForeground(QColor("#e4edf4"))
        elif row_type == "coverlay_pi":
            item.setBackground(QColor("#194a8b"))
            item.setForeground(QColor("#eef6ff"))
        elif row_type == "coverlay_adhesive":
            item.setBackground(QColor("#59606a"))
            item.setForeground(QColor("#f5f7fa"))
        elif row_type == "gap":
            item.setBackground(QColor("#343941"))
            item.setForeground(QColor("#f0f4f8"))
        else:
            item.setBackground(QColor("#204337"))
            item.setForeground(QColor("#edf8f1"))

    def _on_table_selection_changed(self) -> None:
        if self._table_refreshing or self._ui_loading:
            return
        self._refresh_editor()
        self._update_buttons()
        self._refresh_preview()

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._table_refreshing or self._ui_loading:
            return
        meta = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(meta, tuple) or not meta:
            return
        column_key = TABLE_COLUMNS[item.column()][0]
        if meta[0] == "layer" and column_key == "thickness":
            layer_index = int(meta[1])
            if not 0 <= layer_index < len(self.stackup.layers):
                return
            layer = self.stackup.layers[layer_index]
            locked = (
                layer_index in self.locked_copper_indices
                or (
                    self.is_flex_zone
                    and self._adjacent_flex_core(layer_index) is not None
                )
            )
            if not isinstance(layer, CopperLayer) or locked:
                self._refresh_everything(select_meta=meta)
                return
            try:
                unit = thickness_unit_for_layer(self.display_unit, is_copper=True)
                thickness_mm = from_display(float(item.text().strip()), unit)
                if thickness_mm <= 0:
                    raise ValueError
                target_indices = self._table_edit_targets(layer_index)
                if target_indices is None:
                    self._refresh_everything(select_meta=meta)
                    return
                snapped_thickness = snap_copper_thickness_mm(thickness_mm)
                for target_index in target_indices:
                    target_layer = self.stackup.layers[target_index]
                    if not isinstance(target_layer, CopperLayer):
                        continue
                    updated = replace(
                        target_layer,
                        thickness_mm=snapped_thickness,
                    )
                    updated.sync_roughness()
                    self.stackup.layers[target_index] = updated
                self._set_note(
                    "Copper thickness was updated symmetrically."
                    if len(target_indices) > 1
                    else "Copper thickness was updated from the table."
                )
            except ValueError:
                QMessageBox.warning(
                    self,
                    "Invalid value",
                    "Enter a valid positive copper thickness.",
                )
            self._refresh_everything(select_meta=meta)
            return
        if meta[0] != "soldermask":
            return
        if column_key not in {"thickness", "dk", "df"}:
            return
        selected_meta = meta
        try:
            if column_key == "thickness":
                unit = thickness_unit_for_layer(self.display_unit, is_copper=False)
                thickness_mm = from_display(float(item.text().strip()), unit)
                if thickness_mm <= 0:
                    raise ValueError
                self.stackup.soldermask.thickness_mm = thickness_mm
            elif column_key == "dk":
                dk = float(item.text().strip())
                if dk <= 0:
                    raise ValueError
                self.stackup.soldermask.dk = dk
            elif column_key == "df":
                df = float(item.text().strip())
                if df < 0:
                    raise ValueError
                self.stackup.soldermask.df = df
        except ValueError:
            QMessageBox.warning(self, "Invalid value", "Enter a valid positive number for the selected solder mask field.")
        self._refresh_everything(select_meta=selected_meta)

    def _on_preview_layer_selected(self, index: int) -> None:
        self._select_row_meta(("layer", index))

    def _refresh_editor(self) -> None:
        meta = self._current_row_meta()
        index = self._selected_index()
        if meta is None:
            self.detail_title_label.setText("Select a layer")
            self.editor_stack.setCurrentWidget(self.placeholder_page)
            return
        if meta[0] == "coverlay":
            label = str(meta[1]).replace("coverlay_", "").replace("_", " ").title()
            self.detail_title_label.setText(f"{label} settings")
            self.editor_stack.setCurrentWidget(self.placeholder_page)
            self._set_note("Coverlay rows follow the fixed flex-zone construction for now.")
            return
        if meta[0] == "gap":
            self.detail_title_label.setText("Air Gap")
            self.editor_stack.setCurrentWidget(self.placeholder_page)
            self._set_note("Air-gap rows separate neighboring flex sandwiches.")
            return
        if meta[0] == "soldermask":
            self.detail_title_label.setText("Solder mask settings")
            self.editor_stack.setCurrentWidget(self.soldermask_page)
            return
        if index is None:
            self.detail_title_label.setText("Select a layer")
            self.editor_stack.setCurrentWidget(self.placeholder_page)
            return

        layer = self.stackup.layers[index]
        if isinstance(layer, CopperLayer):
            unit = thickness_unit_for_layer(self.display_unit, is_copper=True)
            precision = UNIT_PRECISION[unit]
            self.detail_title_label.setText(f"Editing {self._copper_label(index)}")
            with QSignalBlocker(self.copper_type_combo):
                set_selected_copper_type(self.copper_type_combo, layer.copper_type)
            with QSignalBlocker(self.copper_thickness_edit):
                self.copper_thickness_edit.setText(f"{to_display(layer.thickness_mm, unit):.{precision}f}")
            self.copper_roughness_label.setText(format_roughness_um(layer.roughness_um))
            locked = index in self.locked_copper_indices or (self.is_flex_zone and self._adjacent_flex_core(index) is not None)
            self._set_copper_editor_locked(locked)
            if locked and not self.is_flex_zone:
                self._set_note(self.locked_copper_note)
            self.editor_stack.setCurrentWidget(self.copper_page)
            return

        entry = self._dielectric_entry(layer) if is_dielectric_like(layer) else None
        selected_freq = self.stackup.dielectric_frequency_ghz_or_none(layer, self.catalog)
        if isinstance(layer, FlexCoreLayer):
            self.detail_title_label.setText("Editing Flex Core")
            prefer_blank = False
            manufacturer = layer.manufacturer
            family = layer.family
            selected_id = layer.material_id
        else:
            self.detail_title_label.setText(
                f"Editing {dielectric_type_display_name(layer.dielectric_type)} dielectric"
            )
            prefer_blank = entry is None
            manufacturer = entry.manufacturer if entry is not None else None
            family = entry.family if entry is not None else None
            selected_id = entry.id if entry is not None else None

        self._ui_loading = True
        try:
            with QSignalBlocker(self.dielectric_type_combo):
                self.dielectric_type_combo.setCurrentText("flex_core" if isinstance(layer, FlexCoreLayer) else layer.dielectric_type)
            material_type = "flex_core" if isinstance(layer, FlexCoreLayer) else layer.dielectric_type
            self._populate_manufacturer_choices(material_type, selected=manufacturer, prefer_blank=prefer_blank)
            self._populate_family_choices(material_type, manufacturer, selected=family, prefer_blank=prefer_blank)
            self._populate_material_choices(
                material_type,
                manufacturer,
                family,
                selected_id=selected_id,
                prefer_blank=prefer_blank,
            )
            if isinstance(layer, FlexCoreLayer):
                current_entry = self._current_flex_core_entry()
                if current_entry is not None and selected_freq is not None:
                    self._populate_layer_frequency_choices(current_entry, selected_freq=selected_freq)
                else:
                    self._populate_combo(self.layer_frequency_combo, [], selected_data=None)
            elif entry is not None and selected_freq is not None:
                self._populate_layer_frequency_choices(entry, selected_freq=selected_freq)
            else:
                self._populate_combo(self.layer_frequency_combo, [], selected_data=None)
            self._populate_global_frequency_choices(selected_freq=selected_freq)
            self._set_readonly_dielectric_details(layer)
        finally:
            self._ui_loading = False
        read_only = index in self.locked_dielectric_indices
        self._set_dielectric_editor_locked(isinstance(layer, FlexCoreLayer), read_only=read_only)
        if read_only:
            self._set_note("This shared flex-core dielectric is editable from the flex zone only.")
        self.editor_stack.setCurrentWidget(self.dielectric_page)

    def _set_copper_editor_locked(self, locked: bool) -> None:
        self.copper_type_combo.setEnabled(not locked)
        self.copper_thickness_edit.setEnabled(not locked)
        self.apply_copper_button.setEnabled(not locked)
        self.apply_sym_copper_button.setEnabled(not locked and not self.is_flex_zone)
        if locked:
            self.copper_type_combo.setToolTip(self.locked_copper_note)
            self.copper_thickness_edit.setToolTip(self.locked_copper_note)
        else:
            self.copper_type_combo.setToolTip("")
            self.copper_thickness_edit.setToolTip("")

    def _set_dielectric_editor_locked(self, flex_core_mode: bool, *, read_only: bool = False) -> None:
        type_enabled = not read_only and not flex_core_mode and not self.is_flex_zone
        shared_enabled = not read_only and not flex_core_mode and not self.is_flex_zone
        edit_enabled = not read_only
        self.dielectric_type_combo.setEnabled(type_enabled)
        self.dielectric_manufacturer_combo.setEnabled(edit_enabled)
        self.dielectric_family_combo.setEnabled(edit_enabled)
        self.material_filter_edit.setEnabled(edit_enabled)
        self.dielectric_material_combo.setEnabled(edit_enabled)
        self.layer_frequency_combo.setEnabled(edit_enabled)
        self.apply_layer_button.setEnabled(edit_enabled)
        self.apply_sym_layer_button.setEnabled(shared_enabled)
        self.global_frequency_combo.setEnabled(not self.is_flex_zone and not read_only)
        self.apply_all_frequency_button.setEnabled(not self.is_flex_zone and not read_only)

    def _set_readonly_material_details(self, entry: MaterialEntry, freq_ghz: float) -> None:
        self.readonly_thickness_label.setText(self._thickness_text(entry.thickness_mm, is_copper=False))
        self.readonly_dk_label.setText(f"{entry.dk_at(freq_ghz):.3f}")
        self.readonly_df_label.setText(f"{entry.df_at(freq_ghz):.4f}")
        self.readonly_freq_label.setText(self._frequency_label(freq_ghz))

    def _set_readonly_dielectric_details(self, layer: DielectricLikeLayer) -> None:
        thickness_mm = self.stackup.dielectric_thickness_mm(layer, self.catalog)
        dk, df = self.stackup.dielectric_dk_df_or_none(layer, self.catalog)
        freq = self.stackup.dielectric_frequency_ghz_or_none(layer, self.catalog)
        self.readonly_thickness_label.setText(self._thickness_text(thickness_mm, is_copper=False) if thickness_mm is not None else "")
        self.readonly_dk_label.setText(f"{dk:.3f}" if dk is not None else "")
        self.readonly_df_label.setText(f"{df:.4f}" if df is not None else "")
        self.readonly_freq_label.setText(self._frequency_label(freq) if freq is not None else "")

    def _adjacent_flex_core(self, copper_index: int) -> FlexCoreLayer | None:
        if copper_index > 0:
            previous = self.stackup.layers[copper_index - 1]
            if isinstance(previous, FlexCoreLayer):
                return previous
        if copper_index + 1 < len(self.stackup.layers):
            following = self.stackup.layers[copper_index + 1]
            if isinstance(following, FlexCoreLayer):
                return following
        return None

    def _sync_flex_core_copper_layers(self, flex_core_index: int, flex_layer: FlexCoreLayer) -> None:
        top_index = flex_core_index - 1
        bottom_index = flex_core_index + 1
        if top_index >= 0 and isinstance(self.stackup.layers[top_index], CopperLayer):
            top_layer = self.stackup.layers[top_index]
            self.stackup.layers[top_index] = replace(
                top_layer,
                thickness_mm=flex_layer.copper_thickness_top_mm,
                copper_type=flex_layer.copper_type,
                roughness_um=copper_roughness_um(flex_layer.copper_type),
            )
        if bottom_index < len(self.stackup.layers) and isinstance(self.stackup.layers[bottom_index], CopperLayer):
            bottom_layer = self.stackup.layers[bottom_index]
            self.stackup.layers[bottom_index] = replace(
                bottom_layer,
                thickness_mm=flex_layer.copper_thickness_bottom_mm,
                copper_type=flex_layer.copper_type,
                roughness_um=copper_roughness_um(flex_layer.copper_type),
            )

    def _update_buttons(self) -> None:
        if self.is_flex_zone:
            self.add_above_button.setEnabled(False)
            self.add_below_button.setEnabled(False)
            self.add_material_above_button.setEnabled(False)
            self.add_material_below_button.setEnabled(False)
            self.remove_button.setEnabled(False)
            if self.insert_flex_sandwich_button is not None:
                self.insert_flex_sandwich_button.setEnabled(True)
            if self.remove_flex_sandwich_button is not None:
                self.remove_flex_sandwich_button.setEnabled(self.stackup.flex_core_count() > 1)
            self._sync_command_menu_state()
            return
        if self.structure_locked:
            index = self._selected_index()
            if index is None:
                self.add_above_button.setEnabled(False)
                self.add_below_button.setEnabled(False)
                self.add_material_above_button.setEnabled(False)
                self.add_material_below_button.setEnabled(False)
                self.remove_button.setEnabled(False)
                self._sync_command_menu_state()
                return
            layer = self.stackup.layers[index]
            if isinstance(layer, CopperLayer):
                self.add_above_button.setEnabled(self._boundary_outside_locked_structure(index))
                self.add_below_button.setEnabled(self._boundary_outside_locked_structure(index + 1))
                self.add_material_above_button.setEnabled(False)
                self.add_material_below_button.setEnabled(False)
                self.remove_button.setEnabled(
                    self._can_remove_symmetric_copper_pair(index)
                    and self._removal_outside_locked_structure(index)
                )
            elif isinstance(layer, DielectricLayer):
                self.add_above_button.setEnabled(False)
                self.add_below_button.setEnabled(False)
                if layer.dielectric_type == "core":
                    can_add_above = False
                    can_add_below = False
                elif self._apply_symmetry_enabled():
                    can_add_above = self._can_insert_material_at(index)
                    can_add_below = can_add_above
                else:
                    can_add_above = self._can_insert_single_material_at(
                        index,
                        index,
                    )
                    can_add_below = self._can_insert_single_material_at(
                        index,
                        index + 1,
                    )
                self.add_material_above_button.setEnabled(can_add_above)
                self.add_material_below_button.setEnabled(can_add_below)
                self.remove_button.setEnabled(self._can_remove_material_at(index))
            else:
                self.add_above_button.setEnabled(False)
                self.add_below_button.setEnabled(False)
                self.add_material_above_button.setEnabled(False)
                self.add_material_below_button.setEnabled(False)
                self.remove_button.setEnabled(False)
            self._sync_command_menu_state()
            return
        index = self._selected_index()
        if index is None:
            self.add_above_button.setEnabled(False)
            self.add_below_button.setEnabled(False)
            self.add_material_above_button.setEnabled(False)
            self.add_material_below_button.setEnabled(False)
            self.remove_button.setEnabled(False)
            self._sync_command_menu_state()
            return
        layer = self.stackup.layers[index]
        if isinstance(layer, CopperLayer):
            self.add_above_button.setEnabled(True)
            self.add_below_button.setEnabled(True)
            self.add_material_above_button.setEnabled(False)
            self.add_material_below_button.setEnabled(False)
            self.remove_button.setEnabled(self._can_remove_symmetric_copper_pair(index))
        elif isinstance(layer, DielectricLayer):
            can_remove, _reason = self.stackup.can_remove_symmetric_dielectric(index)
            can_insert_material = layer.dielectric_type != "core"
            self.add_above_button.setEnabled(False)
            self.add_below_button.setEnabled(False)
            self.add_material_above_button.setEnabled(can_insert_material)
            self.add_material_below_button.setEnabled(can_insert_material)
            self.remove_button.setEnabled(can_remove)
        else:
            self.add_above_button.setEnabled(False)
            self.add_below_button.setEnabled(False)
            self.add_material_above_button.setEnabled(False)
            self.add_material_below_button.setEnabled(False)
            self.remove_button.setEnabled(False)
        self._sync_command_menu_state()

    def _locked_structure_bounds(self) -> tuple[int, int] | None:
        if self.protected_structure_bounds is not None:
            return self.protected_structure_bounds
        locked_indices = self.locked_copper_indices | self.locked_dielectric_indices
        if not locked_indices:
            return None
        return min(locked_indices), max(locked_indices)

    def _can_remove_symmetric_copper_pair(self, index: int) -> bool:
        if not self.stackup.can_remove_copper(index):
            return False
        if (
            self.allowed_copper_removal_indices is not None
            and index not in self.allowed_copper_removal_indices
        ):
            return False
        mirror = self.stackup.mirror_index(index)
        if mirror == index or not isinstance(self.stackup.layers[mirror], CopperLayer):
            return False
        if self.stackup.copper_count() - 2 < self.minimum_copper_count:
            return False
        return self._symmetric_copper_removal_bonding_message(index) is None

    def _symmetric_copper_removal_bonding_message(
        self,
        index: int,
    ) -> str | None:
        candidate = deepcopy(self.stackup)
        try:
            candidate.remove_symmetric_copper_pair(index)
        except (IndexError, ValueError):
            return None
        return candidate.bonded_core_bonding_message()

    def _boundary_outside_locked_structure(self, boundary: int) -> bool:
        bounds = self._locked_structure_bounds()
        if bounds is None:
            return True
        first_locked, last_locked = bounds
        mirror_boundary = len(self.stackup.layers) - boundary
        return all(
            candidate <= first_locked or candidate > last_locked
            for candidate in (boundary, mirror_boundary)
        )

    def _can_insert_material_at(self, index: int) -> bool:
        if self.material_insertion_allowed_indices is not None:
            mirror = self.stackup.mirror_index(index)
            return index in self.material_insertion_allowed_indices and mirror in self.material_insertion_allowed_indices
        return self._boundary_outside_locked_structure(index) or self._boundary_outside_locked_structure(index + 1)

    def _can_insert_single_material_at(self, index: int, boundary: int) -> bool:
        if self.material_insertion_allowed_indices is not None:
            return index in self.material_insertion_allowed_indices
        return self._boundary_outside_locked_structure(boundary)

    def _can_remove_material_at(self, index: int) -> bool:
        allowed, _reason = self.stackup.can_remove_symmetric_dielectric(index)
        if not allowed:
            return False
        mirror = self.stackup.mirror_index(index)
        affected_indices = {index, mirror}
        if self.material_insertion_allowed_indices is not None:
            if not affected_indices.issubset(self.material_insertion_allowed_indices):
                return False
        elif not self._removal_outside_locked_structure(index):
            return False
        return self.stackup.consecutive_core_pair(removed_indices=affected_indices) is None

    def _removal_outside_locked_structure(self, index: int) -> bool:
        if (
            isinstance(self.stackup.layers[index], CopperLayer)
            and self.allowed_copper_removal_indices is not None
        ):
            return index in self.allowed_copper_removal_indices
        bounds = self._locked_structure_bounds()
        if bounds is None:
            return True
        first_locked, last_locked = bounds
        layer = self.stackup.layers[index]
        mirror = self.stackup.mirror_index(index)
        affected_indices: set[int]
        if isinstance(layer, CopperLayer):
            if index < mirror:
                top_start = index if index % 2 == 0 else index - 1
                bottom_start = mirror - 1 if mirror % 2 == 0 else mirror
            else:
                top_start = mirror if mirror % 2 == 0 else mirror - 1
                bottom_start = index - 1 if index % 2 == 0 else index
            affected_indices = set(range(top_start, top_start + 2))
            affected_indices.update(range(bottom_start, bottom_start + 2))
        else:
            affected_indices = {index, mirror}
        return all(candidate < first_locked or candidate > last_locked for candidate in affected_indices)

    def _refresh_preview(self) -> None:
        symmetry_ok, issues = self.stackup.symmetry_report(self.catalog)
        total_text = format_total_thickness(self.stackup.total_thickness_mm(self.catalog), self.display_unit)
        self.preview.set_data(
            self.stackup,
            self.catalog,
            display_unit=self.display_unit,
            selected_index=self._selected_index(),
            symmetry_ok=symmetry_ok,
            symmetry_issues=issues,
            copper_number_overrides=self.copper_number_overrides,
        )
        self.preview_title_label.setText("Board thickness")
        self.preview_subtitle_label.setText(total_text)
        if symmetry_ok:
            self.symmetry_badge.setObjectName("SymmetryBadgeOk")
            self.symmetry_badge.setText("Symmetric")
        else:
            self.symmetry_badge.setObjectName("SymmetryBadgeWarn")
            self.symmetry_badge.setText("Not Symmetric")
        self.style().polish(self.symmetry_badge)

    def _update_summary(self) -> None:
        symmetry_ok, issues = self.stackup.symmetry_report(self.catalog)
        copper_unit = thickness_unit_for_layer(self.display_unit, is_copper=True)
        dielectric_unit = thickness_unit_for_layer(self.display_unit, is_copper=False)
        total_text = format_total_thickness(self.stackup.total_thickness_mm(self.catalog), self.display_unit)
        self.metric_total_card.set_value(total_text)
        self.metric_copper_card.set_value(str(self.stackup.copper_count()))
        self.metric_units_card.set_value(f"{copper_unit} / {dielectric_unit}")
        self.metric_rows_card.set_value(str(len(self._row_meta)))
        if not self.note_label.text().strip():
            if self.is_flex_zone:
                self.note_label.setText("Select a row to review flex copper, flex core, or coverlay data.")
            else:
                self.note_label.setText("Select a row to edit copper or dielectric layers.")
        if symmetry_ok:
            self.preview.symmetry_issues = []
        else:
            self.preview.symmetry_issues = issues

    def _refresh_everything(self, *, select_meta: tuple[str, int | str] | None = None) -> None:
        if self._refresh_deferred:
            if select_meta is not None:
                self._deferred_select_meta = select_meta
            return
        self._refresh_table(select_meta=select_meta)
        self._refresh_editor()
        self._update_buttons()
        self._refresh_preview()
        self._update_summary()
        if self._impedance_dialog is not None and self._impedance_dialog.isVisible():
            self._impedance_dialog.refresh_for_stackup_change()
        if self._solver_result_window is not None and not self._solver_result_window.is_visible():
            self._solver_result_window = None
        if self._last_solver_result is not None and self._solver_result_window is not None:
            self._solver_result_window.load_result(
                self._last_solver_result,
                display_unit=self.display_unit,
                root_path=self.root_path,
            )
        self.stackupViewChanged.emit()

    def finish_deferred_refresh(
        self,
        *,
        select_meta: tuple[str, int | str] | None = None,
    ) -> None:
        """Perform one complete refresh after a batch of model changes."""
        if select_meta is not None:
            self._deferred_select_meta = select_meta
        if not self._refresh_deferred:
            return
        self._refresh_deferred = False
        self._refresh_everything(select_meta=self._deferred_select_meta)

    def _populate_combo(
        self,
        combo: QComboBox,
        items: list[tuple[str, object]],
        *,
        selected_data: object | None = None,
        selected_text: str | None = None,
        allow_blank: bool = False,
    ) -> None:
        blocker = QSignalBlocker(combo)
        combo.clear()
        if allow_blank:
            combo.addItem("", None)
        target_index = -1
        for idx, (label, data) in enumerate(items):
            combo.addItem(label, data)
            combo_row = idx + (1 if allow_blank else 0)
            if selected_data is not None and data == selected_data:
                target_index = combo_row
            if selected_text is not None and label == selected_text:
                target_index = combo_row
        if combo.count():
            if target_index >= 0:
                combo.setCurrentIndex(target_index)
            elif allow_blank:
                combo.setCurrentIndex(0)
            else:
                combo.setCurrentIndex(0)
        else:
            combo.setCurrentIndex(-1)
        del blocker

    def _filtered_material_entries(
        self,
        material_type: str,
        manufacturer: str | None,
        family: str | None,
    ) -> list[MaterialEntry]:
        items = self.catalog.filter_entries(
            material_type=catalog_material_type_for_dielectric(material_type),
            manufacturer=manufacturer or None,
            family=family or None,
        )
        query = self.material_filter_edit.text().strip().lower()
        if not query:
            return items
        tokens = query.split()
        filtered: list[MaterialEntry] = []
        for entry in items:
            haystack = " ".join(
                [
                    entry.display_name,
                    entry.family,
                    entry.variant,
                    entry.manufacturer,
                    entry.construction,
                    entry.style,
                    entry.material_type,
                    entry.classification or "",
                ]
            ).lower()
            if all(token in haystack for token in tokens):
                filtered.append(entry)
        return filtered

    def _filtered_flex_core_entries(
        self,
        manufacturer: str | None,
        family: str | None,
    ) -> list[FlexCoreEntry]:
        if self.flex_core_catalog is None:
            return []
        items = self.flex_core_catalog.filter_entries(
            manufacturer=manufacturer or None,
            family=family or None,
        )
        query = self.material_filter_edit.text().strip().lower()
        if not query:
            return items
        tokens = query.split()
        filtered: list[FlexCoreEntry] = []
        for entry in items:
            haystack = " ".join(
                [
                    entry.display_label,
                    entry.family,
                    entry.variant_code,
                    entry.manufacturer,
                    entry.construction_label,
                    entry.copper_type,
                ]
            ).lower()
            if all(token in haystack for token in tokens):
                filtered.append(entry)
        return filtered

    def _populate_manufacturer_choices(self, material_type: str, *, selected: str | None = None, prefer_blank: bool = False) -> None:
        if material_type == "flex_core":
            manufacturers = self.flex_core_catalog.manufacturers() if self.flex_core_catalog is not None else []
        else:
            manufacturers = self.catalog.manufacturers(
                material_type=catalog_material_type_for_dielectric(material_type)
            )
        items = [(manufacturer, manufacturer) for manufacturer in manufacturers]
        self._populate_combo(
            self.dielectric_manufacturer_combo,
            items,
            selected_data=selected,
            allow_blank=prefer_blank,
        )

    def _populate_family_choices(
        self,
        material_type: str,
        manufacturer: str | None,
        *,
        selected: str | None = None,
        prefer_blank: bool = False,
    ) -> None:
        if material_type == "flex_core":
            families = self.flex_core_catalog.families(manufacturer=manufacturer) if self.flex_core_catalog is not None else []
        else:
            families = self.catalog.families(
                material_type=catalog_material_type_for_dielectric(material_type),
                manufacturer=manufacturer,
            )
        items = [(family, family) for family in families]
        self._populate_combo(self.dielectric_family_combo, items, selected_data=selected, allow_blank=prefer_blank)

    def _populate_material_choices(
        self,
        material_type: str,
        manufacturer: str | None,
        family: str | None,
        *,
        selected_id: str | None = None,
        prefer_blank: bool = False,
    ) -> None:
        if material_type == "flex_core":
            items = self._filtered_flex_core_entries(manufacturer, family)
            labels = [(entry.display_label, entry.id) for entry in items]
        else:
            items = self._filtered_material_entries(material_type, manufacturer, family)
            labels = [(entry.display_name, entry.id) for entry in items]
        self._populate_combo(self.dielectric_material_combo, labels, selected_data=selected_id, allow_blank=prefer_blank)

    def _populate_layer_frequency_choices(self, entry: MaterialEntry | FlexCoreEntry, *, selected_freq: float | None = None) -> None:
        items = [(self._frequency_label(freq), freq) for freq in entry.sorted_frequencies]
        chosen = entry.closest_frequency(selected_freq)
        self._populate_combo(self.layer_frequency_combo, items, selected_data=chosen)

    def _stack_dielectric_frequencies(self) -> list[float]:
        frequencies: set[float] = set()
        for layer in self.stackup.layers:
            if not is_dielectric_like(layer):
                continue
            if isinstance(layer, FlexCoreLayer):
                frequencies.update(layer.sorted_frequencies)
                continue
            entry = self._dielectric_entry(layer)
            if entry is not None:
                frequencies.update(entry.sorted_frequencies)
                continue
            freq = self.stackup.dielectric_frequency_ghz_or_none(layer, self.catalog)
            if freq is not None:
                frequencies.add(freq)
        return sorted(frequencies)

    def _populate_global_frequency_choices(self, *, selected_freq: float | None = None) -> None:
        items = [(self._frequency_label(freq), freq) for freq in self._stack_dielectric_frequencies()]
        self._populate_combo(self.global_frequency_combo, items, selected_data=selected_freq)

    def _selected_layer_frequency(self, entry: MaterialEntry | FlexCoreEntry) -> float:
        selected = self.layer_frequency_combo.currentData()
        return entry.closest_frequency(float(selected) if selected is not None else None)

    def _preview_current_material(self) -> None:
        layer = self._selected_dielectric_layer()
        if isinstance(layer, FlexCoreLayer):
            entry = self._current_flex_core_entry()
            if not entry:
                self.readonly_thickness_label.setText("")
                self.readonly_dk_label.setText("")
                self.readonly_df_label.setText("")
                self.readonly_freq_label.setText("")
                return
            freq_ghz = self._selected_layer_frequency(entry)
            self.readonly_thickness_label.setText(self._thickness_text(entry.dielectric_thickness_mm, is_copper=False))
            self.readonly_dk_label.setText(f"{entry.dk_at(freq_ghz):.3f}")
            self.readonly_df_label.setText(f"{entry.df_at(freq_ghz):.4f}")
            self.readonly_freq_label.setText(self._frequency_label(freq_ghz))
            return
        entry = self._current_dielectric_catalog_entry() if isinstance(layer, DielectricLayer) else None
        if entry is None and isinstance(layer, DielectricLayer):
            entry = self._dielectric_entry(layer)
        if not entry:
            self.readonly_thickness_label.setText("")
            self.readonly_dk_label.setText("")
            self.readonly_df_label.setText("")
            self.readonly_freq_label.setText("")
            return
        freq_ghz = self._selected_layer_frequency(entry)
        self._set_readonly_material_details(entry, freq_ghz)

    def _preview_copper_profile(self, *_args) -> None:
        copper_type = selected_copper_type(self.copper_type_combo)
        if copper_type:
            self.copper_roughness_label.setText(format_roughness_um(copper_roughness_um(copper_type)))
        else:
            self.copper_roughness_label.setText("")

    def _on_dielectric_type_change(self, _text: str) -> None:
        if self._ui_loading:
            return
        self._populate_manufacturer_choices(self.dielectric_type_combo.currentText())
        self._on_dielectric_manufacturer_change()

    def _on_dielectric_manufacturer_change(self, *_args) -> None:
        if self._ui_loading:
            return
        self._populate_family_choices(
            self.dielectric_type_combo.currentText(),
            self.dielectric_manufacturer_combo.currentData(),
        )
        self._on_dielectric_family_change()

    def _on_dielectric_family_change(self, *_args) -> None:
        if self._ui_loading:
            return
        layer = self._selected_dielectric_layer()
        selected_id = layer.material_id if layer is not None else None
        self._populate_material_choices(
            self.dielectric_type_combo.currentText(),
            self._combo_text_or_none(self.dielectric_manufacturer_combo),
            self._combo_text_or_none(self.dielectric_family_combo),
            selected_id=selected_id,
        )
        self._on_material_preview_change()

    def _on_material_filter_change(self) -> None:
        if self._ui_loading:
            return
        layer = self._selected_dielectric_layer()
        if layer is None:
            return
        self._populate_material_choices(
            self.dielectric_type_combo.currentText(),
            self._combo_text_or_none(self.dielectric_manufacturer_combo),
            self._combo_text_or_none(self.dielectric_family_combo),
            selected_id=layer.material_id,
        )
        self._on_material_preview_change()

    def _on_material_preview_change(self, *_args) -> None:
        if self._ui_loading:
            return
        layer = self._selected_dielectric_layer()
        if isinstance(layer, FlexCoreLayer):
            entry = self._current_flex_core_entry()
        elif isinstance(layer, DielectricLayer):
            entry = self._current_dielectric_catalog_entry() or self._dielectric_entry(layer)
        else:
            entry = None
        if not entry:
            self.readonly_thickness_label.setText("")
            self.readonly_dk_label.setText("")
            self.readonly_df_label.setText("")
            self.readonly_freq_label.setText("")
            return
        current_freq = layer.selected_freq_ghz if layer is not None else None
        current_freq = entry.closest_frequency(current_freq)
        self._ui_loading = True
        try:
            self._populate_layer_frequency_choices(entry, selected_freq=current_freq)
        finally:
            self._ui_loading = False
        self._preview_current_material()

    def _combo_text_or_none(self, combo: QComboBox) -> str | None:
        text = combo.currentText().strip()
        return text or None

    def _selected_dielectric_layer(self) -> DielectricLikeLayer | None:
        index = self._selected_index()
        if index is None:
            return None
        layer = self.stackup.layers[index]
        return layer if is_dielectric_like(layer) else None

    def _edited_dielectric_from_controls(self) -> DielectricLikeLayer | None:
        selected_layer = self._selected_dielectric_layer()
        if isinstance(selected_layer, FlexCoreLayer):
            entry = self._current_flex_core_entry()
            if not entry:
                QMessageBox.information(self, "No material selected", "Choose a flex-core material before applying it.")
                return None
            return FlexCoreLayer.from_entry(
                entry,
                selected_freq_ghz=self._selected_layer_frequency(entry),
            )
        entry = self._current_dielectric_catalog_entry()
        if entry is None and isinstance(selected_layer, DielectricLayer):
            entry = self._dielectric_entry(selected_layer)
        if not entry:
            QMessageBox.information(self, "No material selected", "Choose a filtered material entry before applying it.")
            return None
        return DielectricLayer(
            dielectric_type=self.dielectric_type_combo.currentText(),
            material_id=entry.id,
            selected_freq_ghz=self._selected_layer_frequency(entry),
        )

    def _edited_copper_from_controls(self) -> CopperLayer | None:
        unit = thickness_unit_for_layer(self.display_unit, is_copper=True)
        try:
            thickness_mm = from_display(float(self.copper_thickness_edit.text().strip()), unit)
        except ValueError:
            QMessageBox.warning(self, "Invalid number", "Enter a numeric value for copper thickness.")
            return None
        if thickness_mm <= 0:
            QMessageBox.warning(self, "Invalid value", "Copper thickness must be positive.")
            return None
        thickness_mm = snap_copper_thickness_mm(thickness_mm)
        current_layer = None
        index = self._selected_index()
        if index is not None:
            selected = self.stackup.layers[index]
            if isinstance(selected, CopperLayer):
                current_layer = selected
        copper = CopperLayer(
            uid=current_layer.uid if current_layer is not None else "",
            thickness_mm=thickness_mm,
            copper_type=selected_copper_type(self.copper_type_combo),
            trace_width_mm=current_layer.trace_width_mm if current_layer is not None else None,
            trace_spacing_mm=current_layer.trace_spacing_mm if current_layer is not None else None,
            target_impedance_ohm=current_layer.target_impedance_ohm if current_layer is not None else None,
        )
        copper.sync_roughness()
        return copper

    def _apply_current_dielectric_settings(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        layer = self.stackup.layers[index]
        if index in self.locked_dielectric_indices:
            return
        if not is_dielectric_like(layer):
            return
        if (
            isinstance(layer, DielectricLayer)
            and is_no_flow_prepreg_type(layer.dielectric_type)
            and not self.no_flow_prepreg_editable
        ):
            return
        updated_layer = self._edited_dielectric_from_controls()
        if updated_layer is None:
            return
        bonding_message = self.stackup.bonded_core_bonding_message(
            {index: updated_layer}
        )
        if bonding_message is not None:
            QMessageBox.information(
                self,
                "Cannot place bonded core",
                bonding_message,
            )
            return
        if self.stackup.consecutive_core_pair({index: updated_layer}) is not None:
            QMessageBox.information(
                self,
                "Cannot place core material",
                "Rigid Core and Flex Core materials must be separated by Rigid PP.",
            )
            return
        self.stackup.layers[index] = updated_layer
        if isinstance(updated_layer, FlexCoreLayer):
            self._sync_flex_core_copper_layers(index, updated_layer)
            self._set_note("Selected flex-core material updated the locked copper layers automatically.")
            self.flexCoreChanged.emit(updated_layer)
            self.sharedRegionChanged.emit()
        else:
            self._set_note("Selected dielectric layer was updated.")
            if self.is_flex_zone:
                self.sharedRegionChanged.emit()
        self._refresh_everything(select_meta=("layer", index))

    def _apply_symmetric_dielectric_settings(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        layer = self.stackup.layers[index]
        if not isinstance(layer, DielectricLayer):
            return
        updated_layer = self._edited_dielectric_from_controls()
        if updated_layer is None:
            return
        mirror_index = self.stackup.mirror_index(index)
        replacements = {index: updated_layer, mirror_index: updated_layer}
        bonding_message = self.stackup.bonded_core_bonding_message(replacements)
        if bonding_message is not None:
            QMessageBox.information(
                self,
                "Cannot place bonded core",
                bonding_message,
            )
            return
        if self.stackup.consecutive_core_pair(replacements) is not None:
            QMessageBox.information(
                self,
                "Cannot place core material",
                "Rigid Core and Flex Core materials must be separated by Rigid PP.",
            )
            return
        try:
            mirror = self.stackup.apply_symmetric_dielectric(index, dielectric=updated_layer)
        except ValueError as exc:
            QMessageBox.information(self, "Cannot apply symmetrically", str(exc))
            return
        if mirror == index:
            self._set_note("Selected dielectric layer was updated in place at the symmetry center.")
        else:
            self._set_note("Selected dielectric material and its symmetry pair were updated together.")
        self._refresh_everything(select_meta=("layer", index))

    def _apply_frequency_to_all_dielectrics(self) -> None:
        target = self.global_frequency_combo.currentData()
        if target is None:
            QMessageBox.information(self, "No frequency selected", "Choose a global dielectric frequency first.")
            return
        target_freq = float(target)
        applied = self.stackup.apply_frequency_to_all_dielectrics(target_freq, self.catalog)
        adjusted_layers = []
        for index, actual in applied:
            if abs(actual - target_freq) > 1e-9:
                adjusted_layers.append(f"{index + 1}:{self._frequency_label(actual)}")
        if adjusted_layers:
            note = (
                f"Applied {self._frequency_label(target_freq)} to all dielectrics. "
                f"Nearest datasheet frequencies were used for rows {', '.join(adjusted_layers)}."
            )
        else:
            note = f"Applied {self._frequency_label(target_freq)} to all dielectric layers."
        self._set_note(note)
        self._refresh_everything(select_meta=self._current_row_meta())

    def _apply_copper_edits(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        if index in self.locked_copper_indices:
            return
        layer = self.stackup.layers[index]
        if not isinstance(layer, CopperLayer):
            return
        updated_layer = self._edited_copper_from_controls()
        if updated_layer is None:
            return
        self.stackup.layers[index] = updated_layer
        self._set_note("Selected copper layer was updated.")
        self._refresh_everything(select_meta=("layer", index))

    def _apply_symmetric_copper_edits(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        layer = self.stackup.layers[index]
        if not isinstance(layer, CopperLayer):
            return
        updated_layer = self._edited_copper_from_controls()
        if updated_layer is None:
            return
        try:
            self.stackup.apply_symmetric_copper(index, copper=updated_layer)
        except ValueError as exc:
            QMessageBox.information(self, "Cannot apply symmetrically", str(exc))
            return
        self._set_note("Selected copper layer and its symmetry pair were updated together.")
        self._refresh_everything(select_meta=("layer", index))

    def _default_dielectric(self, dielectric_type: str = "prepreg") -> DielectricLayer:
        entry = self.catalog.first_for(
            catalog_material_type_for_dielectric(dielectric_type)
        )
        return DielectricLayer(
            dielectric_type=dielectric_type,
            material_id=entry.id,
            selected_freq_ghz=entry.max_freq_ghz,
        )

    def _add_copper_above(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        if self.structure_locked and not self._boundary_outside_locked_structure(index):
            self._set_note("Add layers outside the shared rigid-flex region.")
            return
        old_len = len(self.stackup.layers)
        boundary = index
        dielectric = self._default_dielectric("prepreg")
        top_index, bottom_index = self.stackup.add_symmetric_layers(boundary, dielectric=dielectric)
        mirror_boundary = old_len - boundary
        selected_index = top_index if boundary <= mirror_boundary else bottom_index
        self._set_note("Symmetric copper and dielectric layers were added automatically.")
        self._refresh_everything(select_meta=("layer", selected_index))
        self.structureChanged.emit()

    def _add_copper_below(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        if self.structure_locked and not self._boundary_outside_locked_structure(index + 1):
            self._set_note("Add layers outside the shared rigid-flex region.")
            return
        old_len = len(self.stackup.layers)
        boundary = index + 1
        dielectric = self._default_dielectric("prepreg")
        top_index, bottom_index = self.stackup.add_symmetric_layers(boundary, dielectric=dielectric)
        mirror_boundary = old_len - boundary
        selected_index = top_index if boundary <= mirror_boundary else bottom_index
        self._set_note("Symmetric copper and dielectric layers were added automatically.")
        self._refresh_everything(select_meta=("layer", selected_index))
        self.structureChanged.emit()

    def _add_material_above(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        layer = self.stackup.layers[index]
        if not isinstance(layer, DielectricLayer):
            return
        if (
            is_no_flow_prepreg_type(layer.dielectric_type)
            and not self.no_flow_prepreg_editable
        ):
            return
        if layer.dielectric_type == "core":
            self._set_note("Material cannot be added directly above or below a Rigid Core.")
            return
        symmetric = self._apply_symmetry_enabled()
        if self.structure_locked:
            can_insert = (
                self._can_insert_material_at(index)
                if symmetric
                else self._can_insert_single_material_at(index, index)
            )
            if not can_insert:
                self._set_note("Rigid material cannot be inserted inside a reserved flex-core span.")
                return
        if not symmetric:
            self.stackup.layers.insert(index, self._default_dielectric("prepreg"))
            self._set_note("One Rigid PP material was added above the selected layer.")
            self._refresh_everything(select_meta=("layer", index))
            self.structureChanged.emit()
            return
        original_is_top_half = index <= self.stackup.mirror_index(index)
        top_index, bottom_index = self.stackup.add_symmetric_dielectrics(
            index,
            dielectric=self._default_dielectric("prepreg"),
        )
        selected_index = top_index if original_is_top_half else bottom_index
        self._set_note("Symmetric Rigid PP materials were added automatically.")
        self._refresh_everything(select_meta=("layer", selected_index))
        self.structureChanged.emit()

    def _add_material_below(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        layer = self.stackup.layers[index]
        if not isinstance(layer, DielectricLayer):
            return
        if layer.dielectric_type == "core":
            self._set_note("Material cannot be added directly above or below a Rigid Core.")
            return
        symmetric = self._apply_symmetry_enabled()
        boundary = index + 1
        if self.structure_locked:
            can_insert = (
                self._can_insert_material_at(index)
                if symmetric
                else self._can_insert_single_material_at(index, boundary)
            )
            if not can_insert:
                self._set_note("Rigid material cannot be inserted inside a reserved flex-core span.")
                return
        if not symmetric:
            self.stackup.layers.insert(boundary, self._default_dielectric("prepreg"))
            self._set_note("One Rigid PP material was added below the selected layer.")
            self._refresh_everything(select_meta=("layer", boundary))
            self.structureChanged.emit()
            return
        original_is_top_half = index <= self.stackup.mirror_index(index)
        top_index, bottom_index = self.stackup.add_symmetric_dielectrics(
            boundary,
            dielectric=self._default_dielectric("prepreg"),
        )
        selected_index = top_index if original_is_top_half else bottom_index
        self._set_note("Symmetric Rigid PP materials were added automatically.")
        self._refresh_everything(select_meta=("layer", selected_index))
        self.structureChanged.emit()

    def _remove_selected(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        if not self._apply_symmetry_enabled():
            self._set_note("Enable Apply Symmetry before removing a symmetric pair.")
            self._sync_command_menu_state()
            return
        original_is_top_half = index <= self.stackup.mirror_index(index)
        layer = self.stackup.layers[index]
        if isinstance(layer, CopperLayer):
            if self.structure_locked and not self._removal_outside_locked_structure(index):
                self._set_note("Shared rigid-flex copper layers cannot be removed from the rigid zone.")
                return
            if not self._can_remove_symmetric_copper_pair(index):
                bonding_message = self._symmetric_copper_removal_bonding_message(
                    index
                )
                if bonding_message is not None:
                    QMessageBox.information(
                        self,
                        "Cannot remove layer",
                        bonding_message,
                    )
                elif self.stackup.copper_count() - 2 < self.minimum_copper_count:
                    self._set_note(
                        "The rigid zone must keep one more symmetric copper pair than its linked flex span."
                    )
                return
            try:
                top_start, bottom_start = self.stackup.remove_symmetric_copper_pair(index)
            except ValueError as exc:
                QMessageBox.information(self, "Cannot remove layer", str(exc))
                return
            next_index = top_start if original_is_top_half else bottom_start
            self._set_note("Selected copper layer was removed together with its symmetric pair.")
        else:
            if self.structure_locked and not self._can_remove_material_at(index):
                self._set_note("At least one Rigid PP must remain between neighboring core materials.")
                return
            try:
                top_start, bottom_start = self.stackup.remove_symmetric_dielectric_pair(index)
            except ValueError as exc:
                QMessageBox.information(self, "Cannot remove material", str(exc))
                return
            next_index = top_start if original_is_top_half else bottom_start
            self._set_note("Selected dielectric material was removed together with its symmetric pair.")
        next_index = max(0, min(next_index, len(self.stackup.layers) - 1))
        self._refresh_everything(select_meta=("layer", next_index))
        self.structureChanged.emit()

    def _reset_impedance_workspace(self) -> None:
        self.impedance_workspace = ImpedanceWorkspaceState()
        self._impedance_legacy_migrated = True
        if self._impedance_dialog is not None:
            self._impedance_dialog._prepare_workspace()
            if self._impedance_dialog.isVisible():
                self._impedance_dialog.refresh_for_stackup_change()

    def _open_impedance_dialog(self) -> None:
        logger.info("Opening impedance dialog")
        if self._impedance_dialog is None:
            dialog_parent = self.left_pane.window()
            self._impedance_dialog = CalculateImpedanceDialog(
                self,
                parent=dialog_parent if dialog_parent is not self else self,
            )
            app = QApplication.instance()
            if app is not None:
                self._impedance_dialog.setStyleSheet(app.styleSheet())
        else:
            self._impedance_dialog.refresh_for_stackup_change()
        self._impedance_dialog.show()
        self._impedance_dialog.raise_()
        self._impedance_dialog.activateWindow()

    def _adjacent_reference_indices(self, copper_index: int) -> tuple[int | None, int | None]:
        if self.is_flex_zone:
            core_above = copper_index - 1
            reference_above = copper_index - 2
            if (
                core_above >= 0
                and reference_above >= 0
                and isinstance(self.stackup.layers[core_above], FlexCoreLayer)
                and isinstance(self.stackup.layers[reference_above], CopperLayer)
            ):
                return reference_above, None

            core_below = copper_index + 1
            reference_below = copper_index + 2
            if (
                core_below < len(self.stackup.layers)
                and reference_below < len(self.stackup.layers)
                and isinstance(self.stackup.layers[core_below], FlexCoreLayer)
                and isinstance(self.stackup.layers[reference_below], CopperLayer)
            ):
                return None, reference_below
            return None, None

        ref_above = None
        for candidate in range(copper_index - 1, -1, -1):
            if isinstance(self.stackup.layers[candidate], CopperLayer):
                ref_above = candidate
                break

        ref_below = None
        for candidate in range(copper_index + 1, len(self.stackup.layers)):
            if isinstance(self.stackup.layers[candidate], CopperLayer):
                ref_below = candidate
                break

        return ref_above, ref_below

    def _restore_window_focus(self, target: QWidget | None) -> None:
        if target is None:
            target = self
        try:
            target.show()
        except Exception:
            pass
        try:
            target.raise_()
            target.activateWindow()
        except Exception:
            pass

    def _on_solver_result_window_closed(self, focus_target: QWidget | None) -> None:
        logger.info("Solver result window closed")
        self._solver_result_window = None
        self._restore_window_focus(focus_target)

    def _show_solver_result_window(self, *, parent: QWidget | None = None, focus_target: QWidget | None = None) -> None:
        if self._last_solver_result is None:
            QMessageBox.information(self, "No result available", "Run Show Report first to open the field solver result window.")
            return
        selected_copper = (self._last_solver_result.get("selected_copper") or {}).get("label", "<unknown>")
        logger.info("Showing solver result window for %s", selected_copper)
        parent_widget = parent or self
        close_focus_target = focus_target or parent_widget
        if (
            self._solver_result_window is None
            or not self._solver_result_window.is_visible()
            or self._solver_result_window.parent_widget() is not parent_widget
        ):
            if self._solver_result_window is not None:
                self._solver_result_window.set_on_close(None)
                self._solver_result_window.close()
            self._solver_result_window = FieldSolverResultsDialog(
                parent=parent_widget,
                on_close=lambda: self._on_solver_result_window_closed(close_focus_target),
            )
        else:
            self._solver_result_window.set_on_close(lambda: self._on_solver_result_window_closed(close_focus_target))
        self._solver_result_window.load_result(
            self._last_solver_result,
            display_unit=self.display_unit,
            root_path=self.root_path,
        )
        self._solver_result_window.show()

    def _clear_solver_result(self) -> None:
        logger.debug("Clearing cached solver result window state")
        self._last_solver_result = None
        if self._solver_result_window is not None:
            self._solver_result_window.set_on_close(None)
            self._solver_result_window.close()
            self._solver_result_window = None

    def _export_text(self) -> None:
        output = export_stackup_text(
            self.stackup,
            self.catalog,
            self.display_unit,
            self.impedance_workspace,
        )
        target, _filter = QFileDialog.getSaveFileName(
            self,
            "Export stackup as text",
            str(self._default_dialog_path("stackup_export.txt")),
            "Text files (*.txt);;All files (*.*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not target:
            return
        Path(target).write_text(output, encoding="utf-8")
        QMessageBox.information(self, "Export complete", f"Stackup exported to:\n{target}")

    def _import_text(self) -> None:
        source, _filter = QFileDialog.getOpenFileName(
            self,
            "Import stackup text",
            str(self._default_dialog_directory()),
            "Text files (*.txt);;All files (*.*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not source:
            return
        try:
            content = Path(source).read_text(encoding="utf-8")
            mode_warning = stackup_import_mode_warning(content, rigid_flex_mode=False)
            if mode_warning is not None:
                QMessageBox.warning(self, "Stackup type mismatch", mode_warning)
                return
            imported_stackup, imported_unit, imported_workspace = import_stackup_text(content, self.catalog)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        self.stackup = imported_stackup
        self._ui_loading = True
        try:
            self.unit_combo.setCurrentText(imported_unit)
        finally:
            self._ui_loading = False
        self.display_unit = imported_unit
        if imported_unit in {"um", "mm", "mil", "inch"}:
            self.geometry_input_unit = imported_unit
        self._clear_solver_result()
        if imported_workspace is None:
            self._reset_impedance_workspace()
        else:
            self.impedance_workspace = imported_workspace
            self._impedance_legacy_migrated = True
        self._set_note(f"Imported stackup from:\n{source}")
        self._refresh_everything(select_meta=("layer", 0))
        QMessageBox.information(self, "Import complete", f"Stackup imported from:\n{source}")

    def _import_xpedition_stackup(self) -> None:
        source, _filter = QFileDialog.getOpenFileName(
            self,
            "Import Xpedition stackup",
            str(self._default_dialog_directory()),
            "Xpedition stackup (*.stk);;All files (*.*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not source:
            return
        try:
            content = Path(source).read_text(encoding="utf-8")
            mode_warning = stackup_import_mode_warning(content, rigid_flex_mode=False)
            if mode_warning is not None:
                QMessageBox.warning(self, "Stackup type mismatch", mode_warning)
                return
            imported_stackup = import_stackup_xpedition(content, self.catalog)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        self.stackup = imported_stackup
        self._clear_solver_result()
        self._reset_impedance_workspace()
        self._set_note(f"Imported Xpedition stackup from:\n{source}")
        self._refresh_everything(select_meta=("layer", 0))
        QMessageBox.information(self, "Import complete", f"Xpedition stackup imported from:\n{source}")

    def _export_xpedition_stackup(self) -> None:
        output = export_stackup_xpedition(self.stackup, self.catalog)
        target, _filter = QFileDialog.getSaveFileName(
            self,
            "Export Xpedition stackup",
            str(self._default_dialog_path("stackup_export.stk")),
            "Xpedition stackup (*.stk);;All files (*.*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not target:
            return
        Path(target).write_text(output, encoding="utf-8")
        QMessageBox.information(self, "Export complete", f"Xpedition stackup exported to:\n{target}")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        logger.warning(
            "Main window closeEvent received accepted=%s visible=%s",
            event.isAccepted(),
            self.isVisible(),
        )
        super().closeEvent(event)
        logger.warning("Main window closeEvent finished accepted=%s", event.isAccepted())


def run_qt_app(root_path: Path) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = StackupEditorWindow(root_path)
    window.showMaximized()
    return app.exec()
