from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from stackup_editor.catalog import MaterialCatalog, MaterialEntry
from stackup_editor.material_comparison_model import (
    RADAR_AXES,
    RADAR_AXIS_HELP,
    FamilySummary,
    build_family_summaries,
    entry_values,
    frequency_label,
    manufacturer_is_comparable,
    normalized_profiles,
)


_MAX_PLOTTED_FAMILIES = 7
_SERIES_COLORS = (
    "#35B8C8",
    "#E69A3B",
    "#63C78D",
    "#D76BD7",
    "#7E9CFF",
    "#E66F61",
    "#B6D45A",
)


class MaterialRadarWidget(QWidget):
    familySelected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(520, 520)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self._all_summaries: list[FamilySummary] = []
        self._selected_keys: list[str] = []
        self._active_key: str | None = None
        self._legend_hits: dict[str, QRectF] = {}
        self._polygon_hits: dict[str, QPainterPath] = {}
        self._axis_label_hits: dict[str, QRectF] = {}
        self._hovered_axis: str | None = None

    def set_data(
        self,
        summaries: list[FamilySummary],
        selected_keys: list[str],
        active_key: str | None,
    ) -> None:
        self._all_summaries = list(summaries)
        self._selected_keys = list(selected_keys)
        self._active_key = active_key
        self._hide_axis_help()
        self.update()

    def _summary_map(self) -> dict[str, FamilySummary]:
        return {summary.key: summary for summary in self._all_summaries}

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#0B1926"))
        self._legend_hits.clear()
        self._polygon_hits.clear()
        self._axis_label_hits.clear()

        summary_map = self._summary_map()
        selected = [summary_map[key] for key in self._selected_keys if key in summary_map]
        if not selected:
            painter.setPen(QColor("#8FA9BF"))
            painter.setFont(QFont("Segoe UI", 11))
            painter.drawText(
                self.rect().adjusted(40, 40, -40, -40),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                "Select one or more material families to draw their engineering fingerprints.",
            )
            painter.end()
            return

        width = self.width()
        height = self.height()
        legend_height = 72 + (24 * math.ceil(len(selected) / 2))
        chart_rect = QRectF(28, 24, width - 56, max(250, height - legend_height - 36))
        center = QPointF(chart_rect.center().x(), chart_rect.center().y() + 4)
        radius = max(90.0, min(chart_rect.width(), chart_rect.height()) * 0.34)
        axis_count = len(RADAR_AXES)

        painter.setPen(QPen(QColor("#29465D"), 1.0))
        for ring in range(1, 6):
            ring_path = QPainterPath()
            ring_radius = radius * ring / 5
            for index in range(axis_count):
                angle = (-math.pi / 2) + (2 * math.pi * index / axis_count)
                point = QPointF(
                    center.x() + math.cos(angle) * ring_radius,
                    center.y() + math.sin(angle) * ring_radius,
                )
                if index == 0:
                    ring_path.moveTo(point)
                else:
                    ring_path.lineTo(point)
            ring_path.closeSubpath()
            painter.drawPath(ring_path)

        label_font = QFont("Bahnschrift", 9, QFont.Weight.DemiBold)
        painter.setFont(label_font)
        for index, label in enumerate(RADAR_AXES):
            angle = (-math.pi / 2) + (2 * math.pi * index / axis_count)
            endpoint = QPointF(
                center.x() + math.cos(angle) * radius,
                center.y() + math.sin(angle) * radius,
            )
            painter.setPen(QPen(QColor("#33566F"), 1.2))
            painter.drawLine(center, endpoint)
            label_center = QPointF(
                center.x() + math.cos(angle) * (radius + 36),
                center.y() + math.sin(angle) * (radius + 30),
            )
            label_rect = QRectF(label_center.x() - 66, label_center.y() - 18, 132, 36)
            self._axis_label_hits[label] = label_rect.adjusted(-6, -5, 6, 5)
            if label == self._hovered_axis:
                painter.fillRect(label_rect.adjusted(-4, -2, 4, 2), QColor("#163149"))
            painter.setPen(QColor("#B8CCDA"))
            painter.drawText(
                label_rect,
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                label,
            )

        profiles = normalized_profiles(self._all_summaries)
        color_index_by_key = {summary.key: index for index, summary in enumerate(selected)}
        paint_order = [summary for summary in selected if summary.key != self._active_key]
        paint_order.extend(summary for summary in selected if summary.key == self._active_key)
        for summary in paint_order:
            series_index = color_index_by_key[summary.key]
            scores = profiles.get(summary.key)
            if scores is None:
                continue
            color = QColor(_SERIES_COLORS[series_index % len(_SERIES_COLORS)])
            path = QPainterPath()
            points: list[QPointF] = []
            for index, score in enumerate(scores):
                angle = (-math.pi / 2) + (2 * math.pi * index / axis_count)
                point = QPointF(
                    center.x() + math.cos(angle) * radius * score,
                    center.y() + math.sin(angle) * radius * score,
                )
                points.append(point)
                if index == 0:
                    path.moveTo(point)
                else:
                    path.lineTo(point)
            path.closeSubpath()
            self._polygon_hits[summary.key] = path
            fill = QColor(color)
            fill.setAlpha(38 if summary.key == self._active_key else 12)
            painter.fillPath(path, fill)
            painter.setPen(
                QPen(
                    color,
                    3.4 if summary.key == self._active_key else 2.0,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.drawPath(path)
            if summary.key == self._active_key:
                painter.setBrush(color)
                painter.setPen(Qt.PenStyle.NoPen)
                for point in points:
                    painter.drawEllipse(point, 4.0, 4.0)

        legend_top = height - legend_height + 18
        legend_column_width = max(210.0, (width - 52) / 2)
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        for series_index, summary in enumerate(selected):
            column = series_index % 2
            row = series_index // 2
            item_rect = QRectF(
                26 + (column * legend_column_width),
                legend_top + (row * 24),
                legend_column_width - 10,
                21,
            )
            self._legend_hits[summary.key] = item_rect
            if summary.key == self._active_key:
                painter.fillRect(item_rect.adjusted(-5, -1, 0, 1), QColor("#163149"))
            color = QColor(_SERIES_COLORS[series_index % len(_SERIES_COLORS)])
            painter.fillRect(QRectF(item_rect.left(), item_rect.top() + 4, 11, 11), color)
            painter.setPen(QColor("#E6EEF4") if summary.key == self._active_key else QColor("#A9BFCE"))
            painter.drawText(
                item_rect.adjusted(18, 0, -2, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"{summary.manufacturer} · {summary.family}",
            )
        painter.end()

    def _hide_axis_help(self) -> None:
        if self._hovered_axis is None:
            return
        self._hovered_axis = None
        self.unsetCursor()
        QToolTip.hideText()
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        position = event.position()
        hovered_axis = next(
            (
                label
                for label, rect in self._axis_label_hits.items()
                if rect.contains(position)
            ),
            None,
        )
        if hovered_axis is None:
            self._hide_axis_help()
        elif hovered_axis != self._hovered_axis:
            self._hovered_axis = hovered_axis
            self.setCursor(Qt.CursorShape.WhatsThisCursor)
            QToolTip.showText(
                event.globalPosition().toPoint(),
                f"<b>{hovered_axis}</b><br>{RADAR_AXIS_HELP[hovered_axis]}",
                self,
                self._axis_label_hits[hovered_axis].toRect(),
            )
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._hide_axis_help()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        position = event.position()
        for key, rect in self._legend_hits.items():
            if rect.contains(position):
                self.familySelected.emit(key)
                return
        for key in reversed(self._selected_keys):
            path = self._polygon_hits.get(key)
            if path is not None and path.contains(position):
                self.familySelected.emit(key)
                return
        super().mousePressEvent(event)


class MaterialComparisonDialog(QDialog):
    def __init__(self, catalog: MaterialCatalog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self._summaries: list[FamilySummary] = []
        self._selected_keys: set[str] = set()
        self._active_key: str | None = None
        self._updating_family_list = False

        self.setWindowTitle("Material Comparison")
        self.setModal(False)
        self.resize(1480, 860)
        self.setMinimumSize(1120, 680)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._build_ui()
        self._apply_style()
        self._populate_filters()
        self._refresh_summaries(reset_selection=True)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(14)

        header = QHBoxLayout()
        title_wrap = QVBoxLayout()
        title_wrap.setSpacing(2)
        title = QLabel("Material fingerprints")
        title.setObjectName("ComparisonTitle")
        subtitle = QLabel(
            "Compare catalog families at one frequency. Shapes are normalized; construction values stay exact."
        )
        subtitle.setObjectName("ComparisonSubtitle")
        title_wrap.addWidget(title)
        title_wrap.addWidget(subtitle)
        header.addLayout(title_wrap, 1)
        self.match_count_label = QLabel()
        self.match_count_label.setObjectName("MatchCount")
        header.addWidget(self.match_count_label)
        outer.addLayout(header)

        filter_frame = QFrame()
        filter_frame.setObjectName("FilterRail")
        filters = QGridLayout(filter_frame)
        filters.setContentsMargins(14, 11, 14, 11)
        filters.setHorizontalSpacing(12)
        filters.setVerticalSpacing(4)
        self.manufacturer_combo = QComboBox()
        self.type_combo = QComboBox()
        self.frequency_combo = QComboBox()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search manufacturer or family")
        reset_button = QPushButton("Reset filters")
        reset_button.setObjectName("QuietButton")
        for column, (label_text, widget) in enumerate(
            (
                ("Manufacturer", self.manufacturer_combo),
                ("Material type", self.type_combo),
                ("Frequency", self.frequency_combo),
                ("Find a family", self.search_edit),
            )
        ):
            label = QLabel(label_text)
            label.setObjectName("FilterLabel")
            filters.addWidget(label, 0, column)
            filters.addWidget(widget, 1, column)
        filters.addWidget(reset_button, 1, 4)
        filters.setColumnStretch(3, 1)
        outer.addWidget(filter_frame)

        content_splitter = QSplitter(Qt.Orientation.Horizontal)
        content_splitter.setChildrenCollapsible(False)
        content_splitter.setHandleWidth(8)

        family_panel = QFrame()
        family_panel.setObjectName("ComparisonPanel")
        family_layout = QVBoxLayout(family_panel)
        family_layout.setContentsMargins(14, 14, 14, 14)
        family_layout.setSpacing(8)
        family_title = QLabel("Families on graph")
        family_title.setObjectName("PanelTitle")
        family_note = QLabel(f"Choose up to {_MAX_PLOTTED_FAMILIES}. Click a plotted shape to inspect it.")
        family_note.setObjectName("PanelNote")
        family_note.setWordWrap(True)
        self.family_list = QListWidget()
        self.family_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.family_list.setAlternatingRowColors(False)
        family_layout.addWidget(family_title)
        family_layout.addWidget(family_note)
        family_layout.addWidget(self.family_list, 1)
        content_splitter.addWidget(family_panel)

        chart_panel = QFrame()
        chart_panel.setObjectName("ComparisonPanel")
        chart_layout = QVBoxLayout(chart_panel)
        chart_layout.setContentsMargins(8, 8, 8, 8)
        self.radar = MaterialRadarWidget()
        chart_layout.addWidget(self.radar, 1)
        content_splitter.addWidget(chart_panel)

        detail_panel = QFrame()
        detail_panel.setObjectName("ComparisonPanel")
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(14, 14, 14, 14)
        detail_layout.setSpacing(8)
        self.detail_title = QLabel("Select a family")
        self.detail_title.setObjectName("PanelTitle")
        self.detail_summary = QLabel("Raw construction values will appear here.")
        self.detail_summary.setObjectName("PanelNote")
        self.detail_summary.setWordWrap(True)
        self.detail_table = QTableWidget()
        self.detail_table.setColumnCount(8)
        self.detail_table.setHorizontalHeaderLabels(
            ["Construction", "Type", "Thickness", "Resin", "Frequency", "Dk", "Df", "Class"]
        )
        self.detail_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.detail_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.detail_table.setAlternatingRowColors(False)
        self.detail_table.verticalHeader().setVisible(False)
        self.detail_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.detail_table.horizontalHeader().setStretchLastSection(True)
        detail_layout.addWidget(self.detail_title)
        detail_layout.addWidget(self.detail_summary)
        detail_layout.addWidget(self.detail_table, 1)
        content_splitter.addWidget(detail_panel)
        content_splitter.setSizes([270, 670, 480])
        content_splitter.setStretchFactor(0, 0)
        content_splitter.setStretchFactor(1, 1)
        content_splitter.setStretchFactor(2, 1)
        outer.addWidget(content_splitter, 1)

        footer = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setObjectName("StatusText")
        footer.addWidget(self.status_label, 1)
        close_button = QPushButton("Close")
        close_button.setObjectName("PrimaryButton")
        close_button.clicked.connect(self.close)
        footer.addWidget(close_button)
        outer.addLayout(footer)

        self.manufacturer_combo.currentIndexChanged.connect(self._filters_changed)
        self.type_combo.currentIndexChanged.connect(self._filters_changed)
        self.frequency_combo.currentIndexChanged.connect(self._filters_changed)
        self.search_edit.textChanged.connect(self._filters_changed)
        reset_button.clicked.connect(self._reset_filters)
        self.family_list.itemChanged.connect(self._family_check_changed)
        self.family_list.itemSelectionChanged.connect(self._family_selection_changed)
        self.radar.familySelected.connect(self._activate_family)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QDialog { background: #08141F; color: #E6EEF4; }
            QWidget { color: #D9E5ED; font: 10pt "Segoe UI"; }
            QLabel#ComparisonTitle { color: #F0F6FA; font: 700 23pt "Bahnschrift"; }
            QLabel#ComparisonSubtitle, QLabel#PanelNote, QLabel#StatusText {
                color: #8FA9BF; font: 9pt "Segoe UI";
            }
            QLabel#MatchCount {
                color: #8ED9E2; background: #10283A; border: 1px solid #28536A;
                border-radius: 12px; padding: 6px 12px; font: 700 9pt "Bahnschrift";
            }
            QLabel#PanelTitle { color: #EDF4F8; font: 700 12pt "Bahnschrift"; }
            QLabel#FilterLabel { color: #7794A9; font: 700 8pt "Segoe UI"; }
            QFrame#FilterRail, QFrame#ComparisonPanel {
                background: #0F2130; border: 1px solid #27445F; border-radius: 8px;
            }
            QComboBox, QLineEdit {
                background: #0A1926; color: #E6EEF4; border: 1px solid #31516A;
                border-radius: 5px; min-height: 29px; padding: 2px 8px;
            }
            QComboBox:focus, QLineEdit:focus { border: 1px solid #35B8C8; }
            QComboBox QAbstractItemView {
                background: #102231; color: #E6EEF4; selection-background-color: #1A5263;
                border: 1px solid #31516A;
            }
            QListWidget, QTableWidget {
                background: #0A1926; alternate-background-color: #0D1D2B;
                border: 1px solid #233F55; border-radius: 5px; gridline-color: #1D384C;
                selection-background-color: #17485B; selection-color: #FFFFFF;
            }
            QListWidget::item { padding: 7px 5px; border-bottom: 1px solid #172F41; }
            QListWidget::item:hover { background: #112D40; }
            QHeaderView::section {
                background: #173044; color: #AFC5D3; border: none;
                border-right: 1px solid #28465C; padding: 6px; font: 700 8pt "Segoe UI";
            }
            QPushButton {
                min-height: 30px; padding: 3px 14px; border-radius: 5px;
                border: 1px solid #31516A; background: #132B3D; color: #DDEAF1;
                font: 600 9pt "Segoe UI";
            }
            QPushButton:hover { background: #193A50; border-color: #3B718B; }
            QPushButton#PrimaryButton { background: #237C89; border-color: #35A8B6; color: #FFFFFF; }
            QPushButton#PrimaryButton:hover { background: #2C919F; }
            QPushButton#QuietButton { background: transparent; }
            QSplitter::handle { background: #08141F; }
            QScrollBar:vertical { background: #0A1926; width: 10px; margin: 0; }
            QScrollBar::handle:vertical { background: #31516A; border-radius: 4px; min-height: 28px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            """
        )

    def _populate_filters(self) -> None:
        self.manufacturer_combo.addItem("All manufacturers", None)
        for manufacturer in sorted(
            {
                entry.manufacturer
                for entry in self.catalog.entries
                if manufacturer_is_comparable(entry.manufacturer)
            }
        ):
            self.manufacturer_combo.addItem(manufacturer, manufacturer)

        self.type_combo.addItem("All laminate types", None)
        self.type_combo.addItem("Core", "core")
        self.type_combo.addItem("Prepreg", "prepreg")
        self.type_combo.setCurrentIndex(0)

        frequencies = sorted(
            {
                frequency
                for entry in self.catalog.entries
                for frequency in entry.sorted_frequencies
            }
        )
        self.frequency_combo.addItem("Catalog reference", None)
        for frequency in frequencies:
            self.frequency_combo.addItem(frequency_label(frequency), frequency)
        self.frequency_combo.setCurrentIndex(0)

    def _filters_changed(self, *_args) -> None:
        self._refresh_summaries(reset_selection=False)

    def _reset_filters(self) -> None:
        self.manufacturer_combo.setCurrentIndex(0)
        self.type_combo.setCurrentIndex(0)
        self.frequency_combo.setCurrentIndex(0)
        self.search_edit.clear()
        self._refresh_summaries(reset_selection=True)

    def _preferred_default_keys(self) -> list[str]:
        preferred_families = (
            "FR406",
            "IT-180A",
            "I-Tera MT40",
            "Megtron6 R-5775(N)",
            "Tachyon 100G",
            "ThunderClad 3+",
        )
        by_family = {summary.family: summary.key for summary in self._summaries}
        preferred = [by_family[family] for family in preferred_families if family in by_family]
        if len(preferred) < 4:
            preferred.extend(
                summary.key
                for summary in self._summaries
                if summary.key not in preferred
            )
        return preferred[: min(5, _MAX_PLOTTED_FAMILIES)]

    def _refresh_summaries(self, *, reset_selection: bool) -> None:
        self._summaries = build_family_summaries(
            self.catalog,
            manufacturer=self.manufacturer_combo.currentData(),
            material_type=self.type_combo.currentData(),
            frequency_ghz=self.frequency_combo.currentData(),
            search=self.search_edit.text(),
        )
        available_keys = {summary.key for summary in self._summaries}
        if reset_selection:
            self._selected_keys = set()
        else:
            self._selected_keys.intersection_update(available_keys)
        if not self._selected_keys and self._summaries:
            self._selected_keys.update(self._preferred_default_keys())
        if self._active_key not in self._selected_keys:
            self._active_key = next(
                (summary.key for summary in self._summaries if summary.key in self._selected_keys),
                None,
            )

        self._updating_family_list = True
        self.family_list.clear()
        for summary in self._summaries:
            item = QListWidgetItem(
                f"{summary.manufacturer} · {summary.family}  ({summary.entry_count})"
            )
            item.setData(Qt.ItemDataRole.UserRole, summary.key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if summary.key in self._selected_keys else Qt.CheckState.Unchecked
            )
            self.family_list.addItem(item)
            if summary.key == self._active_key:
                item.setSelected(True)
        self._updating_family_list = False
        self._sync_view()

    def _family_check_changed(self, item: QListWidgetItem) -> None:
        if self._updating_family_list:
            return
        key = str(item.data(Qt.ItemDataRole.UserRole))
        checked = item.checkState() == Qt.CheckState.Checked
        if checked and key not in self._selected_keys and len(self._selected_keys) >= _MAX_PLOTTED_FAMILIES:
            self._updating_family_list = True
            item.setCheckState(Qt.CheckState.Unchecked)
            self._updating_family_list = False
            self.status_label.setText(
                f"The graph is limited to {_MAX_PLOTTED_FAMILIES} families for readable comparisons."
            )
            return
        if checked:
            self._selected_keys.add(key)
            self._active_key = key
            item.setSelected(True)
        else:
            self._selected_keys.discard(key)
            if self._active_key == key:
                self._active_key = next(iter(self._selected_keys), None)
        self._sync_view()

    def _family_selection_changed(self) -> None:
        if self._updating_family_list:
            return
        selected_items = self.family_list.selectedItems()
        if not selected_items:
            return
        key = str(selected_items[0].data(Qt.ItemDataRole.UserRole))
        if key in self._selected_keys:
            self._active_key = key
            self._sync_view()

    def _activate_family(self, key: str) -> None:
        if key not in self._selected_keys:
            return
        self._active_key = key
        self._updating_family_list = True
        for index in range(self.family_list.count()):
            item = self.family_list.item(index)
            item.setSelected(str(item.data(Qt.ItemDataRole.UserRole)) == key)
        self._updating_family_list = False
        self._sync_view()

    def _selected_keys_in_display_order(self) -> list[str]:
        return [summary.key for summary in self._summaries if summary.key in self._selected_keys]

    def _sync_view(self) -> None:
        selected_keys = self._selected_keys_in_display_order()
        self.radar.set_data(self._summaries, selected_keys, self._active_key)
        self.match_count_label.setText(f"{len(self._summaries)} families · {len(selected_keys)} plotted")
        self.status_label.setText(
            "Outward means higher Dk, lower Df, broader availability, or thinner minimum construction."
        )
        self._refresh_detail_table()

    def _active_summary(self) -> FamilySummary | None:
        return next((summary for summary in self._summaries if summary.key == self._active_key), None)

    def _active_entries(self, summary: FamilySummary) -> list[MaterialEntry]:
        material_type = self.type_combo.currentData()
        frequency = self.frequency_combo.currentData()
        entries = []
        for entry in self.catalog.entries:
            if entry.manufacturer != summary.manufacturer or entry.family != summary.family:
                continue
            if material_type and entry.material_type != material_type:
                continue
            if entry_values(entry, frequency) is None:
                continue
            entries.append(entry)
        return sorted(entries, key=lambda entry: (entry.material_type, entry.thickness_mm, entry.construction))

    def _refresh_detail_table(self) -> None:
        summary = self._active_summary()
        if summary is None:
            self.detail_title.setText("Select a family")
            self.detail_summary.setText("Raw construction values will appear here.")
            self.detail_table.setRowCount(0)
            return
        self.detail_title.setText(f"{summary.manufacturer} · {summary.family}")
        self.detail_summary.setText(
            f"Average Dk {summary.average_dk:.3f} · average Df {summary.average_df:.4f} · "
            f"{summary.construction_count} constructions at {frequency_label(summary.frequency_ghz)}"
        )
        entries = self._active_entries(summary)
        frequency = self.frequency_combo.currentData()
        self.detail_table.setSortingEnabled(False)
        self.detail_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            values = entry_values(entry, frequency)
            if values is None:
                continue
            dk, df = values
            actual_frequency = entry.reference_freq_ghz if frequency is None else frequency
            row_values = (
                entry.construction,
                entry.material_type.replace("_", " ").title(),
                f"{entry.thickness_mm:.4f} mm",
                f"{entry.resin_content_pct:.1f}%",
                frequency_label(actual_frequency),
                f"{dk:.3f}",
                f"{df:.4f}",
                entry.classification or "—",
            )
            for column, value in enumerate(row_values):
                item = QTableWidgetItem(value)
                if column in {2, 3, 5, 6}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.detail_table.setItem(row, column, item)
        self.detail_table.setSortingEnabled(True)
