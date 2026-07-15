from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stackup_editor.field_solver_bridge import (
    FieldSolverBridgeError,
    build_solver_request,
    find_width_for_impedance,
    run_solver_request,
)
from stackup_editor.impedance_models import (
    ImpedanceLayerEntry,
    ImpedanceProfile,
    ImpedanceSectionState,
    SectionKind,
    copper_index_for_uid,
    copper_stackup_entries,
    deviation_percent,
    format_profile_name,
    migrate_legacy_copper_impedance,
    mirror_copper_uid,
    mirrored_reference_uids,
    sync_workspace_with_stackup,
)
from stackup_editor.impedance_table_export import ImpedanceTableRow, export_impedance_table_xlsx
from stackup_editor.models import CopperLayer
from stackup_editor.units import SUPPORTED_UNITS, UNIT_PRECISION, from_display, to_display

if TYPE_CHECKING:
    from stackup_editor.qt_app import StackupEditorWindow

logger = logging.getLogger(__name__)


class NoScrollComboBox(QComboBox):
    def wheelEvent(self, event) -> None:  # type: ignore[override]
        event.ignore()


class ImpedanceSectionPanel(QWidget):
    COL_LAYER = 0
    COL_REF_ABOVE = 1
    COL_REF_BELOW = 2
    COL_WIDTH = 3

    def __init__(
        self,
        title: str,
        *,
        section_kind: SectionKind,
        get_host: Callable[[], StackupEditorWindow],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._section_kind = section_kind
        self._get_host = get_host
        self._is_differential = section_kind == "differential"
        self._refreshing = False
        self._selected_row = 0
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        title_label = QLabel(title)
        title_label.setObjectName("SectionTitle")
        root.addWidget(title_label)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.add_button = QPushButton("+ Add")
        self.add_button.setObjectName("PrimaryButton")
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("DangerButton")
        toolbar.addWidget(self.add_button)
        toolbar.addWidget(self.delete_button)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel("Display unit"))
        self.unit_combo = NoScrollComboBox()
        self.unit_combo.addItems(list(SUPPORTED_UNITS))
        self.unit_combo.setFixedWidth(72)
        toolbar.addWidget(self.unit_combo)
        toolbar.addSpacing(12)
        target_caption = "Target Z0 (Ω)" if not self._is_differential else "Target Zdiff (Ω)"
        toolbar.addWidget(QLabel(target_caption))
        self.target_impedance_edit = QLineEdit()
        self.target_impedance_edit.setPlaceholderText("e.g. 50")
        self.target_impedance_edit.setFixedWidth(64)
        self.target_impedance_edit.setToolTip(
            "Profile target impedance.\n"
            "Used for Deviation % on every row.\n"
            + ("Also used by the W button to solve trace width." if not self._is_differential else "")
        )
        toolbar.addWidget(self.target_impedance_edit)
        root.addLayout(toolbar)

        self.tab_bar = QTabBar()
        self.tab_bar.setMovable(True)
        self.tab_bar.setExpanding(False)
        self.tab_bar.setDrawBase(False)
        root.addWidget(self.tab_bar)

        self.table = QTableWidget()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft)
        self._apply_compact_table_style()
        root.addWidget(self.table, stretch=1)

        self.add_button.clicked.connect(self._add_profile)
        self.delete_button.clicked.connect(self._delete_profile)
        self.unit_combo.currentTextChanged.connect(self._on_unit_changed)
        self.target_impedance_edit.editingFinished.connect(self._save_target_from_edit)
        self.tab_bar.currentChanged.connect(self._on_tab_changed)
        self.tab_bar.tabBarDoubleClicked.connect(self._rename_profile_tab)
        self.table.itemSelectionChanged.connect(self._on_row_selection_changed)

        self._configure_columns()

    def _apply_compact_table_style(self) -> None:
        compact_font = QFont(self.font())
        compact_font.setPointSize(8)
        self.table.setFont(compact_font)
        self.table.setStyleSheet(
            """
            QTableWidget { gridline-color: #27445f; }
            QTableWidget QLineEdit, QTableWidget QComboBox {
                font-size: 8pt;
                padding: 0px 2px;
                min-height: 18px;
            }
            QTableWidget QPushButton {
                font-size: 8pt;
                padding: 1px 4px;
                min-width: 0px;
                min-height: 20px;
                max-height: 22px;
            }
            QHeaderView::section {
                font-size: 8pt;
                padding: 2px 4px;
            }
            """
        )

    def section_state(self) -> ImpedanceSectionState:
        host = self._get_host()
        if self._section_kind == "single_ended":
            return host.impedance_workspace.single_ended
        return host.impedance_workspace.differential

    def _configure_columns(self) -> None:
        if self._is_differential:
            headers = [
                "Layer",
                "Top Reference",
                "Bottom Reference",
                "Width",
                "Gap",
                "Zdiff",
                "Dev %",
                "Show Report",
            ]
        else:
            headers = [
                "Layer",
                "Top Reference",
                "Bottom Reference",
                "Width",
                "Z0",
                "Dev %",
                "Show Report",
                "Calculate W",
            ]
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(headers)
        self._apply_column_layout()

    def _apply_column_layout(self) -> None:
        header = self.table.horizontalHeader()
        fixed = QHeaderView.ResizeMode.Fixed
        stretch = QHeaderView.ResizeMode.Stretch

        self.table.setColumnWidth(0, 55)
        header.setSectionResizeMode(0, fixed)

        self.table.setColumnWidth(1, 110)
        header.setSectionResizeMode(1, fixed)
        self.table.setColumnWidth(2, 110)
        header.setSectionResizeMode(2, fixed)

        header.setSectionResizeMode(3, stretch)

        if self._is_differential:
            self.table.setColumnWidth(4, 85)
            header.setSectionResizeMode(4, fixed)
            self.table.setColumnWidth(5, 54)
            header.setSectionResizeMode(5, fixed)
            self.table.setColumnWidth(6, 50)
            header.setSectionResizeMode(6, fixed)
            self.table.setColumnWidth(7, 110)
            header.setSectionResizeMode(7, fixed)
        else:
            self.table.setColumnWidth(4, 54)
            header.setSectionResizeMode(4, fixed)
            self.table.setColumnWidth(5, 50)
            header.setSectionResizeMode(5, fixed)
            self.table.setColumnWidth(6, 110)
            header.setSectionResizeMode(6, fixed)
            self.table.setColumnWidth(7, 84)
            header.setSectionResizeMode(7, fixed)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self.table.rowCount() > 0:
            self._apply_column_layout()

    def reload(self) -> None:
        state = self.section_state()
        with QSignalBlocker(self.unit_combo):
            self.unit_combo.setCurrentText(state.display_unit)
        self._rebuild_tabs(preserve_index=state.active_profile_index)
        self._refresh_table()

    def _rebuild_tabs(self, *, preserve_index: int) -> None:
        state = self.section_state()
        with QSignalBlocker(self.tab_bar):
            while self.tab_bar.count():
                self.tab_bar.removeTab(self.tab_bar.count() - 1)
            for profile in state.profiles:
                self.tab_bar.addTab(profile.name)
            if state.profiles:
                index = max(0, min(preserve_index, len(state.profiles) - 1))
                self.tab_bar.setCurrentIndex(index)
                state.active_profile_index = index
        self._sync_target_edit_from_profile()

    def _active_profile(self) -> ImpedanceProfile:
        state = self.section_state()
        state.active_profile_index = self.tab_bar.currentIndex()
        return state.active_profile()

    def _sync_target_edit_from_profile(self) -> None:
        profile = self._active_profile()
        with QSignalBlocker(self.target_impedance_edit):
            if profile.target_impedance_ohm is None or profile.target_impedance_ohm <= 0:
                self.target_impedance_edit.clear()
            else:
                self.target_impedance_edit.setText(f"{profile.target_impedance_ohm:g}")

    def _save_target_from_edit(self) -> None:
        profile = self._active_profile()
        text = self.target_impedance_edit.text().strip()
        if not text:
            profile.target_impedance_ohm = None
        else:
            try:
                value = float(text)
            except ValueError:
                return
            if value <= 0:
                return
            profile.target_impedance_ohm = value
        self._refresh_table()

    def _on_tab_changed(self, index: int) -> None:
        if index < 0 or self._refreshing:
            return
        state = self.section_state()
        state.active_profile_index = index
        self._sync_target_edit_from_profile()
        self._refresh_table()

    def _on_row_selection_changed(self) -> None:
        self._selected_row = max(0, self.table.currentRow())

    def _add_profile(self) -> None:
        host = self._get_host()
        sync_workspace_with_stackup(host.impedance_workspace, host.stackup)
        default_target = "50" if not self._is_differential else "85"
        target_text, target_ok = QInputDialog.getText(
            self,
            "New impedance profile",
            "Target impedance (Ω):",
            text=default_target,
        )
        if not target_ok:
            return
        try:
            target_ohm = float(target_text.strip())
        except ValueError:
            QMessageBox.warning(self, "Invalid target", "Target impedance must be a positive number.")
            return
        if target_ohm <= 0:
            QMessageBox.warning(self, "Invalid target", "Target impedance must be a positive number.")
            return

        name = format_profile_name(
            "",
            section_kind=self._section_kind,
            target_ohm=target_ohm,
        )

        state = self.section_state()
        profile = ImpedanceProfile(name=name, target_impedance_ohm=target_ohm)
        for _index, layer in copper_stackup_entries(host.stackup):
            profile.layers[layer.uid] = ImpedanceLayerEntry()
        state.profiles.append(profile)
        state.active_profile_index = len(state.profiles) - 1
        self._rebuild_tabs(preserve_index=state.active_profile_index)
        self._sync_target_edit_from_profile()
        self._refresh_table()

    def _delete_profile(self) -> None:
        state = self.section_state()
        if len(state.profiles) <= 1:
            QMessageBox.information(self, "Cannot delete", "At least one impedance profile must remain.")
            return
        profile = self._active_profile()
        answer = QMessageBox.question(
            self,
            "Delete profile",
            f"Delete profile {profile.name!r}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        index = state.active_profile_index
        del state.profiles[index]
        state.active_profile_index = max(0, index - 1)
        self._rebuild_tabs(preserve_index=state.active_profile_index)
        self._refresh_table()

    def _rename_profile_tab(self, index: int) -> None:
        state = self.section_state()
        if index < 0 or index >= len(state.profiles):
            return
        profile = state.profiles[index]
        name, ok = QInputDialog.getText(self, "Rename profile", "Profile name:", text=profile.name)
        if not ok:
            return
        profile.name = format_profile_name(
            name,
            section_kind=self._section_kind,
            target_ohm=profile.target_impedance_ohm,
        )
        self.tab_bar.setTabText(index, profile.name)

    def _on_unit_changed(self, new_unit: str) -> None:
        if not new_unit or self._refreshing:
            return
        state = self.section_state()
        if state.display_unit == new_unit:
            return
        self._save_table_to_profile()
        state.display_unit = new_unit
        self._refresh_table()

    def _format_geometry(self, value_mm: float | None) -> str:
        if value_mm is None or value_mm <= 0:
            return ""
        unit = self.section_state().display_unit
        precision = UNIT_PRECISION[unit]
        return f"{to_display(value_mm, unit):.{precision}f}"

    def _parse_geometry(self, text: str) -> float | None:
        stripped = text.strip()
        if not stripped:
            return None
        unit = self.section_state().display_unit
        value_mm = from_display(float(stripped), unit)
        if value_mm <= 0:
            return None
        return value_mm

    def _copper_ref_label(self, host: StackupEditorWindow, index: int) -> str:
        number = host.stackup.copper_layer_number(index)
        return f"{number} - {host._copper_label(index)}"

    def _ref_options(
        self,
        host: StackupEditorWindow,
        copper_index: int,
        *,
        above: bool,
    ) -> list[tuple[str, str | None]]:
        options: list[tuple[str, str | None]] = [("Auto", None)]
        for index, layer in copper_stackup_entries(host.stackup):
            if index == copper_index:
                continue
            if above and index < copper_index:
                options.append((self._copper_ref_label(host, index), layer.uid))
            if not above and index > copper_index:
                options.append((self._copper_ref_label(host, index), layer.uid))
        return options

    def _set_combo_value(self, combo: QComboBox, value: str | None) -> None:
        with QSignalBlocker(combo):
            for row in range(combo.count()):
                if combo.itemData(row) == value:
                    combo.setCurrentIndex(row)
                    return
            combo.setCurrentIndex(0)

    def _combo_value(self, combo: QComboBox) -> str | None:
        value = combo.currentData()
        return str(value) if value is not None else None

    def _save_table_to_profile(self) -> None:
        if self._refreshing or self.table.rowCount() == 0:
            return
        profile = self._active_profile()

        for row in range(self.table.rowCount()):
            copper_uid_item = self.table.item(row, self.COL_LAYER)
            if copper_uid_item is None:
                continue
            copper_uid = copper_uid_item.data(Qt.ItemDataRole.UserRole)
            if copper_uid is None:
                continue
            entry = profile.layers.setdefault(str(copper_uid), ImpedanceLayerEntry())

            ref_above = self.table.cellWidget(row, self.COL_REF_ABOVE)
            ref_below = self.table.cellWidget(row, self.COL_REF_BELOW)
            width_edit = self.table.cellWidget(row, self.COL_WIDTH)
            if isinstance(ref_above, QComboBox):
                entry.ref_above_uid = self._combo_value(ref_above)
            if isinstance(ref_below, QComboBox):
                entry.ref_below_uid = self._combo_value(ref_below)
            if isinstance(width_edit, QLineEdit):
                try:
                    entry.width_mm = self._parse_geometry(width_edit.text())
                except ValueError:
                    entry.width_mm = None
            if self._is_differential:
                gap_edit = self.table.cellWidget(row, 4)
                if isinstance(gap_edit, QLineEdit):
                    try:
                        entry.spacing_mm = self._parse_geometry(gap_edit.text())
                    except ValueError:
                        entry.spacing_mm = None

    def _result_columns(self) -> tuple[int, int]:
        if self._is_differential:
            return 5, 6
        return 4, 5

    def _update_row_result_display(self, row: int, entry: ImpedanceLayerEntry) -> None:
        impedance_col, dev_col = self._result_columns()
        z_item = self.table.item(row, impedance_col)
        if z_item is None:
            z_item = QTableWidgetItem()
            z_item.setFlags(z_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, impedance_col, z_item)
        z_item.setText(f"{entry.calculated_impedance_ohm:.2f}" if entry.calculated_impedance_ohm is not None else "")

        dev_item = self.table.item(row, dev_col)
        if dev_item is None:
            dev_item = QTableWidgetItem()
            dev_item.setFlags(dev_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, dev_col, dev_item)
        target_ohm = self._active_profile().target_impedance_ohm
        dev = deviation_percent(entry.calculated_impedance_ohm, target_ohm)
        dev_item.setText(f"{dev:.2f}%" if dev is not None else "")

    def _row_for_copper_uid(self, copper_uid: str) -> int:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, self.COL_LAYER)
            if item is None:
                continue
            value = item.data(Qt.ItemDataRole.UserRole)
            if value is not None and str(value) == copper_uid:
                return row
        return -1

    def _sync_row_widgets_from_entry(self, row: int, entry: ImpedanceLayerEntry) -> None:
        ref_above = self.table.cellWidget(row, self.COL_REF_ABOVE)
        if isinstance(ref_above, QComboBox):
            with QSignalBlocker(ref_above):
                self._set_combo_value(ref_above, entry.ref_above_uid)

        ref_below = self.table.cellWidget(row, self.COL_REF_BELOW)
        if isinstance(ref_below, QComboBox):
            with QSignalBlocker(ref_below):
                self._set_combo_value(ref_below, entry.ref_below_uid)

        width_edit = self.table.cellWidget(row, self.COL_WIDTH)
        if isinstance(width_edit, QLineEdit):
            with QSignalBlocker(width_edit):
                width_edit.setText(self._format_geometry(entry.width_mm))

        if self._is_differential:
            gap_edit = self.table.cellWidget(row, 4)
            if isinstance(gap_edit, QLineEdit):
                with QSignalBlocker(gap_edit):
                    gap_edit.setText(self._format_geometry(entry.spacing_mm))

    def _refresh_row_from_entry(self, copper_uid: str, entry: ImpedanceLayerEntry, *, sync_inputs: bool) -> None:
        row = self._row_for_copper_uid(copper_uid)
        if row < 0:
            return
        if sync_inputs:
            self._sync_row_widgets_from_entry(row, entry)
        self._update_row_result_display(row, entry)

    def _auto_calculate_row(self, row: int) -> None:
        if self._refreshing:
            return
        self._calculate_row(row, interactive=False, show_report=False)

    def _refresh_table(self) -> None:
        host = self._get_host()
        sync_workspace_with_stackup(host.impedance_workspace, host.stackup)
        profile = self._active_profile()
        copper_entries = copper_stackup_entries(host.stackup)
        self._refreshing = True
        try:
            self.table.setRowCount(len(copper_entries))
            target_ohm = profile.target_impedance_ohm
            unit = self.section_state().display_unit

            for row, (copper_index, copper_layer) in enumerate(copper_entries):
                entry = profile.layers.setdefault(copper_layer.uid, ImpedanceLayerEntry())

                layer_item = QTableWidgetItem(host._copper_label(copper_index))
                layer_item.setData(Qt.ItemDataRole.UserRole, copper_layer.uid)
                layer_item.setFlags(layer_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, self.COL_LAYER, layer_item)

                ref_above = NoScrollComboBox()
                for text, data in self._ref_options(host, copper_index, above=True):
                    ref_above.addItem(text, data)
                self._set_combo_value(ref_above, entry.ref_above_uid)
                ref_above.currentIndexChanged.connect(lambda _index, r=row: self._auto_calculate_row(r))
                self.table.setCellWidget(row, self.COL_REF_ABOVE, ref_above)

                ref_below = NoScrollComboBox()
                for text, data in self._ref_options(host, copper_index, above=False):
                    ref_below.addItem(text, data)
                self._set_combo_value(ref_below, entry.ref_below_uid)
                ref_below.currentIndexChanged.connect(lambda _index, r=row: self._auto_calculate_row(r))
                self.table.setCellWidget(row, self.COL_REF_BELOW, ref_below)

                width_edit = QLineEdit()
                width_edit.setPlaceholderText(unit)
                width_edit.setText(self._format_geometry(entry.width_mm))
                width_edit.editingFinished.connect(lambda r=row: self._auto_calculate_row(r))
                self.table.setCellWidget(row, self.COL_WIDTH, width_edit)

                if self._is_differential:
                    gap_edit = QLineEdit()
                    gap_edit.setPlaceholderText(unit)
                    gap_edit.setText(self._format_geometry(entry.spacing_mm))
                    gap_edit.editingFinished.connect(lambda r=row: self._auto_calculate_row(r))
                    self.table.setCellWidget(row, 4, gap_edit)
                    impedance_col = 5
                    dev_col = 6
                    calc_col = 7
                else:
                    impedance_col = 4
                    dev_col = 5
                    calc_col = 6
                    w_col = 7

                z_text = f"{entry.calculated_impedance_ohm:.2f}" if entry.calculated_impedance_ohm is not None else ""
                z_item = QTableWidgetItem(z_text)
                z_item.setFlags(z_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, impedance_col, z_item)

                dev = deviation_percent(entry.calculated_impedance_ohm, target_ohm)
                dev_item = QTableWidgetItem(f"{dev:.2f}%" if dev is not None else "")
                dev_item.setFlags(dev_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, dev_col, dev_item)

                calc_btn = QPushButton("Show Report")
                calc_btn.setObjectName("PrimaryButton")
                calc_btn.setToolTip("Run the solver for this row and open the detailed report")
                calc_btn.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                calc_btn.clicked.connect(
                    lambda _checked=False, r=row: self._calculate_row(r, interactive=True, show_report=True)
                )
                self.table.setCellWidget(row, calc_col, calc_btn)

                if not self._is_differential:
                    calc_w_btn = QPushButton("Calculate W")
                    calc_w_btn.setObjectName("PrimaryButton")
                    calc_w_btn.setToolTip(
                        "Solve trace width to match the profile Target Z0 shown in the toolbar above"
                    )
                    calc_w_btn.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                    calc_w_btn.clicked.connect(lambda _checked=False, r=row: self._calculate_width_row(r))
                    self.table.setCellWidget(row, w_col, calc_w_btn)

                self.table.setRowHeight(row, 28)

            self._apply_column_layout()

            if copper_entries:
                select_row = min(self._selected_row, len(copper_entries) - 1)
                self.table.selectRow(select_row)
        finally:
            self._refreshing = False

    def _row_copper_uid(self, row: int) -> str | None:
        item = self.table.item(row, self.COL_LAYER)
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return str(value) if value is not None else None

    def _resolved_ref_indices(
        self,
        host: StackupEditorWindow,
        entry: ImpedanceLayerEntry,
    ) -> tuple[int | None, int | None]:
        return (
            copper_index_for_uid(host.stackup, entry.ref_above_uid),
            copper_index_for_uid(host.stackup, entry.ref_below_uid),
        )

    def _resolved_ref_uids(
        self,
        host: StackupEditorWindow,
        copper_index: int,
        entry: ImpedanceLayerEntry,
    ) -> tuple[str | None, str | None]:
        ref_above_index, ref_below_index = self._resolved_ref_indices(host, entry)
        if ref_above_index is None or ref_below_index is None:
            auto_above, auto_below = host._adjacent_reference_indices(copper_index)
            if ref_above_index is None:
                ref_above_index = auto_above
            if ref_below_index is None:
                ref_below_index = auto_below

        ref_above_uid = None
        if ref_above_index is not None:
            ref_above_layer = host.stackup.layers[ref_above_index]
            if isinstance(ref_above_layer, CopperLayer):
                ref_above_uid = ref_above_layer.uid

        ref_below_uid = None
        if ref_below_index is not None:
            ref_below_layer = host.stackup.layers[ref_below_index]
            if isinstance(ref_below_layer, CopperLayer):
                ref_below_uid = ref_below_layer.uid

        return ref_above_uid, ref_below_uid

    def _is_stackup_symmetric(self, host: StackupEditorWindow) -> bool:
        symmetry_ok, _issues = host.stackup.symmetry_report(host.catalog)
        return symmetry_ok

    def _apply_symmetric_result(
        self,
        host: StackupEditorWindow,
        profile: ImpedanceProfile,
        copper_uid: str,
        *,
        width_mm: float | None,
        spacing_mm: float | None,
        calculated_impedance_ohm: float | None,
        ref_above_uid: str | None,
        ref_below_uid: str | None,
    ) -> None:
        if not self._is_stackup_symmetric(host):
            return

        mirror_uid = mirror_copper_uid(host.stackup, copper_uid)
        if not mirror_uid or mirror_uid == copper_uid:
            return

        mirror_entry = profile.layers.setdefault(mirror_uid, ImpedanceLayerEntry())
        mirror_entry.width_mm = width_mm
        if self._is_differential:
            mirror_entry.spacing_mm = spacing_mm
        mirror_entry.calculated_impedance_ohm = calculated_impedance_ohm
        mirror_above_uid, mirror_below_uid = mirrored_reference_uids(
            host.stackup,
            ref_above_uid,
            ref_below_uid,
        )
        mirror_entry.ref_above_uid = mirror_above_uid
        mirror_entry.ref_below_uid = mirror_below_uid

    def _calculate_row(self, row: int, *, interactive: bool = True, show_report: bool = True) -> bool:
        host = self._get_host()
        self._save_table_to_profile()
        copper_uid = self._row_copper_uid(row)
        if copper_uid is None:
            return False
        copper_index = copper_index_for_uid(host.stackup, copper_uid)
        if copper_index is None:
            return False
        profile = self._active_profile()
        entry = profile.layers.setdefault(copper_uid, ImpedanceLayerEntry())
        if entry.width_mm is None or entry.width_mm <= 0:
            entry.calculated_impedance_ohm = None
            self._update_row_result_display(row, entry)
            if interactive:
                QMessageBox.warning(self, "Invalid width", "Enter a positive trace width before calculating.")
            return False
        spacing_mm = 0.0
        if self._is_differential:
            if entry.spacing_mm is None or entry.spacing_mm <= 0:
                entry.calculated_impedance_ohm = None
                self._update_row_result_display(row, entry)
                if interactive:
                    QMessageBox.warning(
                        self,
                        "Invalid spacing",
                        "Enter a positive trace gap before calculating differential impedance.",
                    )
                return False
            spacing_mm = entry.spacing_mm
        ref_above, ref_below = self._resolved_ref_indices(host, entry)
        if ref_above is None or ref_below is None:
            auto_above, auto_below = host._adjacent_reference_indices(copper_index)
            if ref_above is None:
                ref_above = auto_above
            if ref_below is None:
                ref_below = auto_below
        ref_above_uid, ref_below_uid = self._resolved_ref_uids(host, copper_index, entry)
        entry.ref_above_uid = ref_above_uid
        entry.ref_below_uid = ref_below_uid
        logger.info(
            "Running impedance calculation row=%s layer=%s width_mm=%s spacing_mm=%s refs=(%s,%s) show_report=%s",
            row,
            host._copper_label(copper_index),
            entry.width_mm,
            spacing_mm,
            ref_above,
            ref_below,
            show_report,
        )
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            request = build_solver_request(
                host.stackup,
                host.catalog,
                copper_index,
                trace_width_mm=entry.width_mm,
                trace_spacing_mm=spacing_mm,
                ref_above_index=ref_above,
                ref_below_index=ref_below,
            )
            result = run_solver_request(host.root_path, request)
        except FieldSolverBridgeError as exc:
            logger.warning(
                "Impedance calculation failed row=%s layer=%s: %s",
                row,
                host._copper_label(copper_index),
                exc,
            )
            entry.calculated_impedance_ohm = None
            self._update_row_result_display(row, entry)
            if interactive:
                QMessageBox.warning(self, "Calculation failed", str(exc))
            return False
        finally:
            QApplication.restoreOverrideCursor()

        solved = result["solved"]
        result["plot_request_base"] = copy.deepcopy(request)
        result["target_impedance_ohm"] = profile.target_impedance_ohm
        entry.calculated_impedance_ohm = float(
            solved["z_diff_ohm" if self._is_differential else "z0_ohm"]
        )
        logger.info(
            "Impedance calculation succeeded row=%s layer=%s result_ohm=%.4f",
            row,
            host._copper_label(copper_index),
            entry.calculated_impedance_ohm,
        )
        mirror_uid = mirror_copper_uid(host.stackup, copper_uid)
        self._apply_symmetric_result(
            host,
            profile,
            copper_uid,
            width_mm=entry.width_mm,
            spacing_mm=entry.spacing_mm,
            calculated_impedance_ohm=entry.calculated_impedance_ohm,
            ref_above_uid=entry.ref_above_uid,
            ref_below_uid=entry.ref_below_uid,
        )
        host._last_solver_result = result
        self._refresh_row_from_entry(copper_uid, entry, sync_inputs=True)
        if mirror_uid:
            mirror_entry = profile.layers.get(mirror_uid)
            if mirror_entry is not None:
                self._refresh_row_from_entry(mirror_uid, mirror_entry, sync_inputs=True)
        if show_report:
            host._show_solver_result_window(parent=self.window(), focus_target=self.window())
        return True

    def _calculate_width_row(self, row: int) -> None:
        if self._is_differential:
            return
        host = self._get_host()
        self._save_table_to_profile()
        copper_uid = self._row_copper_uid(row)
        if copper_uid is None:
            return
        copper_index = copper_index_for_uid(host.stackup, copper_uid)
        if copper_index is None:
            return
        profile = self._active_profile()
        target = profile.target_impedance_ohm
        if target is None or target <= 0:
            QMessageBox.warning(
                self,
                "No target impedance",
                "Enter a Target Z0 (Ω) value in the toolbar for this profile before using the W button.",
            )
            return
        entry = profile.layers.setdefault(copper_uid, ImpedanceLayerEntry())
        ref_above, ref_below = self._resolved_ref_indices(host, entry)
        if ref_above is None or ref_below is None:
            auto_above, auto_below = host._adjacent_reference_indices(copper_index)
            if ref_above is None:
                ref_above = auto_above
            if ref_below is None:
                ref_below = auto_below
        ref_above_uid, ref_below_uid = self._resolved_ref_uids(host, copper_index, entry)
        entry.ref_above_uid = ref_above_uid
        entry.ref_below_uid = ref_below_uid
        logger.info(
            "Running width solve row=%s layer=%s target_ohm=%s refs=(%s,%s)",
            row,
            host._copper_label(copper_index),
            target,
            ref_above,
            ref_below,
        )
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            found_width_mm, achieved_z0 = find_width_for_impedance(
                host.root_path,
                host.stackup,
                host.catalog,
                copper_index,
                target,
                ref_above_index=ref_above,
                ref_below_index=ref_below,
            )
        except FieldSolverBridgeError as exc:
            logger.warning(
                "Width solve failed row=%s layer=%s: %s",
                row,
                host._copper_label(copper_index),
                exc,
            )
            QMessageBox.warning(self, "Solve Width failed", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        entry.width_mm = found_width_mm
        entry.calculated_impedance_ohm = achieved_z0
        logger.info(
            "Width solve succeeded row=%s layer=%s width_mm=%.6f achieved_ohm=%.4f",
            row,
            host._copper_label(copper_index),
            found_width_mm,
            achieved_z0,
        )
        self._apply_symmetric_result(
            host,
            profile,
            copper_uid,
            width_mm=entry.width_mm,
            spacing_mm=entry.spacing_mm,
            calculated_impedance_ohm=entry.calculated_impedance_ohm,
            ref_above_uid=entry.ref_above_uid,
            ref_below_uid=entry.ref_below_uid,
        )
        self._sync_target_edit_from_profile()
        self._refresh_table()

    def build_export_rows(self) -> tuple[list[ImpedanceTableRow], list[str]]:
        host = self._get_host()
        self._save_table_to_profile()
        rows: list[ImpedanceTableRow] = []
        warnings: list[str] = []
        state = self.section_state()
        tl_prefix = "DIFF" if self._is_differential else "SE"

        for profile in state.profiles:
            for copper_index, copper_layer in copper_stackup_entries(host.stackup):
                entry = profile.layers.get(copper_layer.uid)
                if entry is None:
                    continue
                if entry.width_mm is None or entry.width_mm <= 0:
                    continue
                spacing_mm = entry.spacing_mm or 0.0 if self._is_differential else 0.0
                if self._is_differential and spacing_mm <= 0:
                    continue
                ref_above, ref_below = self._resolved_ref_indices(host, entry)
                if ref_above is None or ref_below is None:
                    auto_above, auto_below = host._adjacent_reference_indices(copper_index)
                    if ref_above is None:
                        ref_above = auto_above
                    if ref_below is None:
                        ref_below = auto_below
                calculated = entry.calculated_impedance_ohm
                if calculated is None:
                    try:
                        request = build_solver_request(
                            host.stackup,
                            host.catalog,
                            copper_index,
                            trace_width_mm=entry.width_mm,
                            trace_spacing_mm=spacing_mm,
                            ref_above_index=ref_above,
                            ref_below_index=ref_below,
                        )
                        result = run_solver_request(host.root_path, request)
                        solved = result["solved"]
                        calculated = float(solved["z_diff_ohm" if self._is_differential else "z0_ohm"])
                    except (FieldSolverBridgeError, KeyError, TypeError, ValueError) as exc:
                        warnings.append(f"{profile.name} / {host._copper_label(copper_index)}: {exc}")

                rows.append(
                    ImpedanceTableRow(
                        tl_type=f"{tl_prefix} - {profile.name}",
                        trace_layer=host._copper_label(copper_index),
                        trace_width=self._export_length_text(entry.width_mm),
                        trace_gap=self._export_length_text(spacing_mm) if spacing_mm > 0 else "",
                        reference_above=host._copper_label(ref_above) if ref_above is not None else "",
                        reference_below=host._copper_label(ref_below) if ref_below is not None else "",
                        calculated_impedance_ohm=calculated,
                        target_impedance_ohm=profile.target_impedance_ohm,
                    )
                )
        return rows, warnings

    def _export_length_text(self, value_mm: float | None) -> str:
        if value_mm is None or value_mm <= 0:
            return ""
        return f"{to_display(value_mm, 'mil'):.2f}mil"


class CalculateImpedanceDialog(QDialog):
    def __init__(self, host: StackupEditorWindow, parent: QWidget | None = None) -> None:
        super().__init__(parent or host)
        self._host = host
        self.setWindowTitle("Calculate Impedance")
        self.resize(1480, 760)
        self.setMinimumSize(1100, 560)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.single_ended_panel = ImpedanceSectionPanel(
            "Single-Ended Impedance",
            section_kind="single_ended",
            get_host=lambda: self._host,
        )
        self.differential_panel = ImpedanceSectionPanel(
            "Differential Impedance",
            section_kind="differential",
            get_host=lambda: self._host,
        )
        splitter.addWidget(self.single_ended_panel)
        splitter.addWidget(self.differential_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, stretch=1)

        footer = QHBoxLayout()
        self.export_button = QPushButton("Export Impedance Table")
        self.export_button.setObjectName("PrimaryButton")
        footer.addStretch(1)
        footer.addWidget(self.export_button)
        layout.addLayout(footer)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.export_button.clicked.connect(self._export_impedance_table)
        self._prepare_workspace()
        self.single_ended_panel.reload()
        self.differential_panel.reload()

    def _prepare_workspace(self) -> None:
        sync_workspace_with_stackup(self._host.impedance_workspace, self._host.stackup)
        if not self._host._impedance_legacy_migrated:
            migrate_legacy_copper_impedance(self._host.impedance_workspace, self._host.stackup)
            self._host._impedance_legacy_migrated = True

    def refresh_for_stackup_change(self) -> None:
        sync_workspace_with_stackup(self._host.impedance_workspace, self._host.stackup)
        self.single_ended_panel.reload()
        self.differential_panel.reload()

    def _export_impedance_table(self) -> None:
        se_rows, se_warnings = self.single_ended_panel.build_export_rows()
        diff_rows, diff_warnings = self.differential_panel.build_export_rows()
        rows = se_rows + diff_rows
        warnings = se_warnings + diff_warnings
        if not rows:
            QMessageBox.information(
                self,
                "No impedance rows",
                "Enter trace width on at least one layer in any profile before exporting.",
            )
            return

        target, _filter = QFileDialog.getSaveFileName(
            self,
            "Export impedance table",
            str(self._host._default_dialog_path("impedance_table.xlsx")),
            "Excel workbook (*.xlsx);;All files (*.*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not target:
            return

        template_path = self._host.root_path / "TransmissionLineTemp.xlsx"
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            export_impedance_table_xlsx(template_path, Path(target), rows)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        message = f"Impedance table exported to:\n{target}"
        if warnings:
            preview = "\n".join(warnings[:5])
            if len(warnings) > 5:
                preview += f"\n... and {len(warnings) - 5} more row warning(s)."
            message += f"\n\nRows with blank calculated impedance:\n{preview}"
        QMessageBox.information(self, "Export complete", message)
