from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QCursor, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


CONSTRUCTION_ROWS: tuple[tuple[str, int, int], ...] = (
    ("106", 56, 56),
    ("1035", 66, 68),
    ("1037", 70, 73),
    ("1067", 70, 70),
    ("1078", 54, 54),
    ("1080", 60, 47),
    ("1086", 60, 60),
    ("1506", 46, 45),
    ("1652", 52, 52),
    ("2113", 60, 56),
    ("2116", 60, 58),
    ("2313", 60, 64),
    ("3070", 70, 70),
    ("3313", 61, 62),
    ("7628", 44, 32),
)


# Photo-only regions in the 1509 x 748 source sheet. Keeping one source atlas avoids
# storing fifteen derivative images and preserves the original image resolution.
CONSTRUCTION_CROPS: dict[str, QRect] = {
    "106": QRect(52, 9, 239, 145),
    "1035": QRect(354, 0, 239, 188),
    "1037": QRect(656, 0, 239, 187),
    "1067": QRect(958, 15, 239, 148),
    "1078": QRect(1260, 0, 239, 183),
    "1080": QRect(52, 278, 235, 145),
    "1086": QRect(354, 270, 235, 149),
    "1506": QRect(656, 271, 239, 149),
    "1652": QRect(958, 263, 239, 151),
    "2113": QRect(1261, 276, 235, 145),
    "2116": QRect(52, 523, 239, 149),
    "2313": QRect(354, 519, 238, 148),
    "3070": QRect(658, 516, 236, 150),
    "3313": QRect(958, 521, 239, 149),
    "7628": QRect(1261, 527, 235, 147),
}


FIBER_GLASS_INTRO_PARAGRAPHS: tuple[str, ...] = (
    "Dielectric material between two conductor is not a perfectly solid material.",
    "Typical printed circuit boards are constructed from various woven fiberglass "
    "fabrics strengthened and bound together with epoxy resin.",
    "Combination of those two material determines how signal travels properly.",
    "It does not affect insertion loss directly. However, it causes non-uniform "
    "distribution of Dk & Df over the material. So, it causes impedance mismatches "
    "and increases to RETURN LOSS.",
    "In a differantial pairs, this results in intra-pair skew between the P and N "
    "legs. This is more pronounced for data rates beyond 10 Gb/ps because the amount "
    "of skew due to this effect can often be more than 10 to 15% depending on the "
    "data rate and length of the channel.",
)

FIBER_GLASS_SELECTION_PARAGRAPH = (
    "Because the ratio of fiberglass to epoxy is the primary contributor to the Ɛr "
    "disparity, choose a PCB style with a tighter weave, less epoxy, and greater Ɛr "
    "uniformity across longer trace lengths. Before sending your design out for "
    "fabrication, specify a PCB style that can best accommodate high-speed signals. "
    "For examples of common PCB styles, see Figure below:"
)

FIBER_GLASS_PREVENTION_PARAGRAPH = (
    "When routing for a considerable length, it is oftentimes recommended to do "
    "zigzag routing to mitigate the negative effects of fiber weave on high-speed "
    "differential signals by forcing the traces to be out of alignment with the "
    "fiber weave:\n"
    "– Angle of zigzag can be 1-10 degrees to skew traces relative to weave\n"
    "– Typical value used is 10 degrees to sufficiently skew trace"
)


class AspectRatioImageLabel(QLabel):
    """A responsive image that preserves its natural aspect ratio."""

    def __init__(self, image_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.image_path = image_path
        self.source_pixmap = QPixmap(str(image_path))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        if self.source_pixmap.isNull():
            self.setText(f"Material construction image could not be loaded:\n{image_path}")

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt naming
        return not self.source_pixmap.isNull()

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        if self.source_pixmap.isNull() or self.source_pixmap.width() <= 0:
            return 220
        return max(
            180,
            round(width * self.source_pixmap.height() / self.source_pixmap.width()),
        )

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        width = 720
        return QSize(width, self.heightForWidth(width))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if self.source_pixmap.isNull():
            return
        self.setPixmap(
            self.source_pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


class ConstructionHoverPreview(QLabel):
    """Non-interactive popup showing one construction cropped from the source atlas."""

    def __init__(self, atlas_path: Path, parent: QWidget) -> None:
        super().__init__(
            parent,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.atlas_path = atlas_path
        self.atlas_pixmap = QPixmap(str(atlas_path))
        self.current_construction: str | None = None
        self.setObjectName("ConstructionHoverPreview")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setContentsMargins(8, 8, 8, 8)
        self.setStyleSheet(
            "QLabel#ConstructionHoverPreview {"
            " background: #07111B; border: 2px solid #D88A3D; border-radius: 7px;"
            "}"
        )

    def show_construction(self, construction: str, anchor: QPoint) -> None:
        crop_rect = CONSTRUCTION_CROPS.get(construction)
        if crop_rect is None or self.atlas_pixmap.isNull():
            self.hide_preview()
            return

        crop = self.atlas_pixmap.copy(crop_rect.intersected(self.atlas_pixmap.rect()))
        if crop.isNull():
            self.hide_preview()
            return

        scaled = crop.scaled(
            QSize(560, 360),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.current_construction = construction
        self.setPixmap(scaled)
        self.adjustSize()
        self._move_near(anchor)
        self.show()
        self.raise_()

    def hide_preview(self) -> None:
        self.current_construction = None
        self.hide()

    def _move_near(self, anchor: QPoint) -> None:
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        if screen is None:
            self.move(anchor + QPoint(18, 18))
            return

        bounds = screen.availableGeometry()
        x = anchor.x() + 18
        y = anchor.y() + 18
        if x + self.width() > bounds.right():
            x = anchor.x() - self.width() - 18
        if y + self.height() > bounds.bottom():
            y = anchor.y() - self.height() - 18
        x = max(bounds.left(), min(x, bounds.right() - self.width() + 1))
        y = max(bounds.top(), min(y, bounds.bottom() - self.height() + 1))
        self.move(x, y)


class MaterialConstructionsDialog(QDialog):
    """Glass-weave reference table with contextual construction photographs."""

    def __init__(
        self,
        performance_image_path: Path,
        construction_atlas_path: Path,
        guide_image_paths: tuple[Path, Path, Path],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.performance_image_path = performance_image_path
        self.construction_atlas_path = construction_atlas_path
        self.guide_image_paths = guide_image_paths
        self.setWindowTitle("Material Constructions")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(1280, 760)
        self.setMinimumSize(960, 600)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(24, 20, 24, 24)
        root_layout.setSpacing(12)

        title = QLabel("Material Constructions")
        title.setObjectName("MaterialConstructionsTitle")
        subtitle = QLabel(
            "Compare measured transmission response and hover over a construction "
            "number to inspect its glass-weave pattern."
        )
        subtitle.setObjectName("MaterialConstructionsSubtitle")
        subtitle.setWordWrap(True)
        root_layout.addWidget(title)
        root_layout.addWidget(subtitle)

        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(0, 4, 0, 0)
        content_layout.setSpacing(16)
        root_layout.addLayout(content_layout, 1)

        self.performance_frame = QFrame()
        self.performance_frame.setObjectName("MaterialPerformanceFrame")
        performance_layout = QVBoxLayout(self.performance_frame)
        performance_layout.setContentsMargins(6, 6, 6, 6)
        performance_layout.setSpacing(0)

        self.guide_scroll_area = QScrollArea()
        self.guide_scroll_area.setObjectName("MaterialGuideScroll")
        self.guide_scroll_area.setWidgetResizable(True)
        self.guide_scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.guide_scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self.guide_content = QWidget()
        self.guide_content.setObjectName("MaterialGuideContent")
        guide_layout = QVBoxLayout(self.guide_content)
        guide_layout.setContentsMargins(12, 12, 12, 12)
        guide_layout.setSpacing(10)

        self.guide_sequence: list[QWidget] = []
        self.guide_text_labels: list[QLabel] = []
        self.guide_image_labels: list[AspectRatioImageLabel] = []

        def add_heading(text: str, object_name: str) -> QLabel:
            label = QLabel(text)
            label.setObjectName(object_name)
            label.setWordWrap(True)
            guide_layout.addWidget(label)
            self.guide_sequence.append(label)
            return label

        def add_paragraph(text: str) -> QLabel:
            label = QLabel(text)
            label.setObjectName("GuideBody")
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            guide_layout.addWidget(label)
            self.guide_sequence.append(label)
            self.guide_text_labels.append(label)
            return label

        def add_image(image_path: Path, object_name: str) -> AspectRatioImageLabel:
            label = AspectRatioImageLabel(image_path)
            label.setObjectName(object_name)
            guide_layout.addWidget(label)
            self.guide_sequence.append(label)
            self.guide_image_labels.append(label)
            return label

        self.fiber_glass_heading = add_heading("Fiber Glass Effect", "GuideHeading")
        for paragraph in FIBER_GLASS_INTRO_PARAGRAPHS:
            add_paragraph(paragraph)

        add_image(guide_image_paths[0], "MaterialGuideImage")
        add_paragraph(FIBER_GLASS_SELECTION_PARAGRAPH)
        add_image(guide_image_paths[1], "MaterialGuideImage")

        guide_layout.addSpacing(6)
        self.effect_heading = add_heading("Effect of Glass Wave", "GuideSubheading")
        self.performance_image = add_image(
            performance_image_path,
            "MaterialPerformanceImage",
        )

        guide_layout.addSpacing(6)
        self.prevention_heading = add_heading(
            "How to Prevent Glass Wave Effect?",
            "GuideSubheading",
        )
        add_paragraph(FIBER_GLASS_PREVENTION_PARAGRAPH)
        add_image(guide_image_paths[2], "MaterialGuideImage")
        guide_layout.addStretch(1)

        self.guide_scroll_area.setWidget(self.guide_content)
        performance_layout.addWidget(self.guide_scroll_area)
        content_layout.addWidget(self.performance_frame, 3)

        self.reference_frame = QFrame()
        self.reference_frame.setObjectName("MaterialConstructionReferenceFrame")
        reference_layout = QVBoxLayout(self.reference_frame)
        reference_layout.setContentsMargins(12, 12, 12, 12)
        reference_layout.setSpacing(8)

        reference_title = QLabel("Construction reference")
        reference_title.setObjectName("SectionTitle")
        reference_layout.addWidget(reference_title)

        reference_hint = QLabel("Hover over a construction number to view its weave.")
        reference_hint.setObjectName("ReferenceHint")
        reference_hint.setWordWrap(True)
        reference_layout.addWidget(reference_hint)

        self.reference_table = QTableWidget(len(CONSTRUCTION_ROWS), 3)
        self.reference_table.setObjectName("MaterialConstructionTable")
        self.reference_table.setHorizontalHeaderLabels(
            [
                "Construction",
                "Wrap Count\n(ends/inch)",
                "Fill Count\n(ends/inch)",
            ]
        )
        self.reference_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.reference_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.reference_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.reference_table.setMouseTracking(True)
        self.reference_table.viewport().setMouseTracking(True)
        self.reference_table.setShowGrid(False)
        self.reference_table.setAlternatingRowColors(True)
        self.reference_table.verticalHeader().setVisible(False)
        self.reference_table.verticalHeader().setDefaultSectionSize(34)
        self.reference_table.horizontalHeader().setMinimumHeight(44)
        self.reference_table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        self.reference_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Fixed
        )

        for row_index, (construction, wrap_count, fill_count) in enumerate(
            CONSTRUCTION_ROWS
        ):
            values = (construction, str(wrap_count), str(fill_count))
            for column_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column_index == 0:
                    item.setData(Qt.ItemDataRole.UserRole, construction)
                    item.setToolTip(f"View {construction} weave photograph")
                self.reference_table.setItem(row_index, column_index, item)

        reference_layout.addWidget(self.reference_table, 1)
        content_layout.addWidget(self.reference_frame, 2)

        self.hover_preview = ConstructionHoverPreview(construction_atlas_path, self)
        self.reference_table.cellEntered.connect(self._show_construction_preview)
        self.reference_table.viewport().installEventFilter(self)

        self.setStyleSheet(
            """
            QDialog { background: #091521; color: #E7F0F7; }
            QLabel#MaterialConstructionsTitle {
                color: #F1F6FA; font-family: Bahnschrift; font-size: 24px; font-weight: 700;
            }
            QLabel#MaterialConstructionsSubtitle {
                color: #8FA9BF; font-family: "Segoe UI"; font-size: 10pt;
            }
            QFrame#MaterialPerformanceFrame,
            QFrame#MaterialConstructionReferenceFrame {
                background: #0F1F2F; border: 1px solid #27445F; border-radius: 8px;
            }
            QLabel#SectionTitle {
                color: #F1F6FA; font-family: Bahnschrift; font-size: 14px; font-weight: 600;
            }
            QLabel#ReferenceHint {
                color: #8FA9BF; font-family: "Segoe UI"; font-size: 9pt;
            }
            QScrollArea#MaterialGuideScroll,
            QWidget#MaterialGuideContent {
                background: transparent; border: 0;
            }
            QLabel#GuideHeading {
                color: #F1F6FA; font-family: Bahnschrift; font-size: 18px; font-weight: 700;
                padding-bottom: 2px;
            }
            QLabel#GuideSubheading {
                color: #D7E8F4; font-family: Bahnschrift; font-size: 14px; font-weight: 600;
                padding-top: 4px;
            }
            QLabel#GuideBody {
                color: #C6D5E1; font-family: "Segoe UI"; font-size: 10pt;
                line-height: 1.35;
            }
            QLabel#MaterialGuideImage,
            QLabel#MaterialPerformanceImage {
                background: #FFFFFF; border: 1px solid #27445F; border-radius: 5px;
            }
            QTableWidget#MaterialConstructionTable {
                background: #0B1926; alternate-background-color: #102437;
                color: #DDE9F2; border: 1px solid #27445F; border-radius: 5px;
                font-family: "Segoe UI"; font-size: 9pt; outline: 0;
            }
            QTableWidget#MaterialConstructionTable::item {
                border-bottom: 1px solid #1E3B51; padding: 5px;
            }
            QTableWidget#MaterialConstructionTable::item:hover {
                background: #1D4258; color: #FFFFFF;
            }
            QHeaderView::section {
                background: #17364B; color: #F1F6FA; border: 0;
                border-right: 1px solid #274F68; padding: 7px 5px;
                font-family: "Segoe UI"; font-size: 8.5pt; font-weight: 600;
            }
            QScrollBar:vertical {
                background: #0C1A28; width: 10px; margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #33566F; min-height: 28px; border-radius: 5px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0; background: transparent;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
            """
        )
        self._fit_reference_section_to_text()

    def _fit_reference_section_to_text(self) -> None:
        """Size each column and the right panel from their widest visible text."""
        table_font = self.reference_table.fontMetrics()
        header_font = self.reference_table.horizontalHeader().fontMetrics()
        self.column_widths: list[int] = []

        for column in range(self.reference_table.columnCount()):
            header_text = self.reference_table.horizontalHeaderItem(column).text()
            header_width = max(
                header_font.horizontalAdvance(line) for line in header_text.splitlines()
            )
            cell_width = max(
                table_font.horizontalAdvance(self.reference_table.item(row, column).text())
                for row in range(self.reference_table.rowCount())
            )
            column_width = max(header_width, cell_width) + 24
            self.reference_table.setColumnWidth(column, column_width)
            self.column_widths.append(column_width)

        scrollbar_width = self.reference_table.style().pixelMetric(
            QStyle.PixelMetric.PM_ScrollBarExtent,
            None,
            self.reference_table,
        )
        table_width = (
            sum(self.column_widths)
            + scrollbar_width
            + 2 * self.reference_table.frameWidth()
        )
        panel_margins = 24
        panel_width = table_width + panel_margins
        self.reference_frame.setFixedWidth(panel_width)

    def _show_construction_preview(self, row: int, column: int) -> None:
        if column != 0:
            self.hover_preview.hide_preview()
            return
        item = self.reference_table.item(row, column)
        if item is None:
            self.hover_preview.hide_preview()
            return
        construction = item.data(Qt.ItemDataRole.UserRole)
        self.hover_preview.show_construction(str(construction), QCursor.pos())

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        if watched is self.reference_table.viewport() and event.type() == QEvent.Type.Leave:
            self.hover_preview.hide_preview()
        return super().eventFilter(watched, event)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.hover_preview.hide_preview()
        super().hideEvent(event)
