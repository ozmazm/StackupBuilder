from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QSize, Qt
from PySide6.QtGui import QGuiApplication, QMovie, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stackup_editor.models import (
    COPPER_RQ_BY_TYPE_UM,
    COPPER_RZ_BY_TYPE_UM,
    COPPER_TYPES,
)


class HoverImagePreview(QLabel):
    """A non-interactive image window positioned beside the hovered table item."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(
            parent,
            Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint,
        )
        self.setObjectName("RoughnessHoverPreview")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setContentsMargins(8, 8, 8, 8)
        self._source_cache: dict[Path, QPixmap] = {}
        self.current_key: str | None = None

    def show_image(self, key: str, image_path: Path, anchor: QPoint) -> None:
        source = self._source_cache.get(image_path)
        if source is None:
            source = QPixmap(str(image_path))
            self._source_cache[image_path] = source
        if source.isNull():
            self.hide_preview()
            return

        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        if screen is None:
            self.hide_preview()
            return
        available = screen.availableGeometry()
        maximum = QSize(
            min(760, int(available.width() * 0.58)),
            min(540, int(available.height() * 0.68)),
        )
        pixmap = source.scaled(
            maximum,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(pixmap)
        self.resize(pixmap.width() + 16, pixmap.height() + 16)

        x = anchor.x() + 18
        y = anchor.y() + 18
        if x + self.width() > available.right():
            x = anchor.x() - self.width() - 18
        if y + self.height() > available.bottom():
            y = anchor.y() - self.height() - 18
        x = max(available.left(), min(x, available.right() - self.width()))
        y = max(available.top(), min(y, available.bottom() - self.height()))
        self.move(x, y)
        self.current_key = key
        self.show()

    def hide_preview(self) -> None:
        self.current_key = None
        self.hide()


class CopperRoughnessDialog(QDialog):
    """Animated copper-roughness guide with hover-based reference images."""

    def __init__(
        self,
        gif_path: Path,
        parent: QWidget | None = None,
        *,
        hover_image_paths: dict[str, Path] | None = None,
    ) -> None:
        super().__init__(parent)
        self.gif_path = gif_path
        self.hover_image_paths = hover_image_paths or {
            "RTF": gif_path.parent / "roughness_hover_rtf.png",
            "VLP": gif_path.parent / "roughness_hover_vlp.png",
            "HVLP": gif_path.parent / "roughness_hover_hvlp_ulp.png",
            "ULP": gif_path.parent / "roughness_hover_hvlp_ulp.png",
            "Rz": gif_path.parent / "roughness_hover_rz.png",
            "Rq": gif_path.parent / "roughness_hover_rq.png",
        }
        self.setWindowTitle("Copper Roughness")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(1280, 760)
        self.setMinimumSize(960, 760)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(24, 20, 24, 24)
        root_layout.setSpacing(16)

        title = QLabel("Copper Roughness")
        title.setObjectName("RoughnessHelpTitle")
        subtitle = QLabel(
            "Hover over a copper foil name or the Rz/Rq headers to view its reference image. "
            "Values are shown in micrometres (µm)."
        )
        subtitle.setObjectName("RoughnessHelpSubtitle")
        subtitle.setWordWrap(True)
        root_layout.addWidget(title)
        root_layout.addWidget(subtitle)

        content_layout = QHBoxLayout()
        content_layout.setSpacing(18)
        root_layout.addLayout(content_layout, 1)

        animation_frame = QFrame()
        animation_frame.setObjectName("RoughnessAnimationFrame")
        animation_layout = QVBoxLayout(animation_frame)
        animation_layout.setContentsMargins(10, 10, 10, 10)
        self.animation_label = QLabel()
        self.animation_label.setObjectName("RoughnessAnimation")
        self.animation_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.animation_label.setMinimumSize(480, 300)
        self.animation_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        animation_layout.addWidget(self.animation_label)
        content_layout.addWidget(animation_frame, 3)

        self.reference_frame = QFrame()
        self.reference_frame.setObjectName("RoughnessReferenceFrame")
        self.reference_frame.setMinimumWidth(360)
        self.reference_frame.setMaximumWidth(480)
        reference_layout = QVBoxLayout(self.reference_frame)
        reference_layout.setContentsMargins(14, 16, 14, 14)
        reference_layout.setSpacing(10)

        reference_title = QLabel("Foil profile reference")
        reference_title.setObjectName("RoughnessReferenceTitle")
        reference_layout.addWidget(reference_title)

        self.reference_table = QTableWidget(len(COPPER_TYPES), 3)
        self.reference_table.setObjectName("RoughnessReferenceTable")
        self.reference_table.setHorizontalHeaderLabels(
            ["Copper foil", "Rz (µm)", "Rq (µm)"]
        )
        self.reference_table.verticalHeader().setVisible(False)
        self.reference_table.setAlternatingRowColors(True)
        self.reference_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.reference_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.reference_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.reference_table.setMouseTracking(True)
        self.reference_table.viewport().setMouseTracking(True)
        self.reference_table.viewport().installEventFilter(self)

        self.reference_header = self.reference_table.horizontalHeader()
        self.reference_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.reference_header.setMouseTracking(True)
        self.reference_header.viewport().setMouseTracking(True)
        self.reference_header.viewport().installEventFilter(self)

        self.reference_table.verticalHeader().setDefaultSectionSize(44)
        self.reference_table.setFixedHeight(220)
        for row, copper_type in enumerate(COPPER_TYPES):
            values = (
                copper_type,
                f"{COPPER_RZ_BY_TYPE_UM[copper_type]:.2f}",
                f"{COPPER_RQ_BY_TYPE_UM[copper_type]:.2f}",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.reference_table.setItem(row, column, item)
        reference_layout.addWidget(self.reference_table)

        self.importance_heading = QLabel("Why Surface Roughness Matters")
        self.importance_heading.setObjectName("RoughnessImportanceHeading")
        reference_layout.addWidget(self.importance_heading)

        self.importance_paragraph = QLabel(
            "At high frequencies, current crowds into a thin layer near the conductor "
            "surface—a phenomenon called the skin effect. As frequency rises, skin "
            "depth decreases, so current follows the copper's microscopic peaks and "
            "valleys instead of a smooth path. This increases effective path length "
            "and resistance, raising conductor loss, insertion loss, and signal "
            "attenuation. Smoother copper therefore helps preserve high-speed signal "
            "integrity."
        )
        self.importance_paragraph.setObjectName("RoughnessImportanceParagraph")
        self.importance_paragraph.setWordWrap(True)
        self.importance_paragraph.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        reference_layout.addWidget(self.importance_paragraph)
        content_layout.addWidget(
            self.reference_frame,
            2,
            Qt.AlignmentFlag.AlignTop,
        )

        self.hover_preview = HoverImagePreview(self)

        self.movie = QMovie(str(gif_path))
        self.movie.setCacheMode(QMovie.CacheMode.CacheAll)
        if self.movie.isValid():
            self.movie.jumpToFrame(0)
            self._movie_source_size = self.movie.currentImage().size()
            self.animation_label.setMovie(self.movie)
            self.animation_label.installEventFilter(self)
            self._scale_movie()
            self.movie.start()
        else:
            self._movie_source_size = QSize()
            self.animation_label.setText(
                f"Copper roughness animation could not be loaded:\n{gif_path}"
            )

        self.setStyleSheet(
            """
            QDialog { background: #091521; color: #E7F0F7; }
            QLabel#RoughnessHelpTitle {
                color: #F1F6FA; font-family: Bahnschrift; font-size: 24px; font-weight: 700;
            }
            QLabel#RoughnessHelpSubtitle {
                color: #8FA9BF; font-family: "Segoe UI"; font-size: 10pt;
            }
            QFrame#RoughnessAnimationFrame, QFrame#RoughnessReferenceFrame {
                background: #0F1F2F; border: 1px solid #27445F; border-radius: 8px;
            }
            QLabel#RoughnessAnimation { background: #0B1724; border-radius: 4px; }
            QLabel#RoughnessReferenceTitle {
                color: #D88A36; font-family: Bahnschrift; font-size: 13pt; font-weight: 600;
            }
            QLabel#RoughnessImportanceHeading {
                color: #E7F0F7; font-family: Bahnschrift; font-size: 13pt; font-weight: 600;
            }
            QLabel#RoughnessImportanceParagraph {
                color: #AFC1CF; font-family: "Segoe UI"; font-size: 10pt;
            }
            QTableWidget#RoughnessReferenceTable {
                background: #0C1A28; alternate-background-color: #13283B;
                color: #E7F0F7; border: 1px solid #27445F; gridline-color: #27445F;
                font-family: "Segoe UI"; font-size: 11pt;
            }
            QTableWidget#RoughnessReferenceTable QHeaderView::section {
                background: #20374C; color: #D88A36; border: 0;
                border-right: 1px solid #27445F; padding: 8px; font-weight: 600;
            }
            QLabel#RoughnessHoverPreview {
                background: #0B1724; border: 2px solid #D88A36; border-radius: 8px;
            }
            """
        )

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        if watched is self.animation_label and event.type() == QEvent.Type.Resize:
            self._scale_movie()
        elif watched is self.reference_table.viewport():
            self._handle_table_hover(watched, event)
        elif watched is self.reference_header.viewport():
            self._handle_header_hover(watched, event)
        return super().eventFilter(watched, event)

    def _handle_table_hover(self, viewport: QWidget, event) -> None:
        if event.type() == QEvent.Type.MouseMove:
            point = event.position().toPoint()
            item = self.reference_table.itemAt(point)
            key = item.text() if item is not None and item.column() == 0 else None
            self._show_hover_key(key, viewport.mapToGlobal(point))
        elif event.type() == QEvent.Type.Leave:
            self.hover_preview.hide_preview()

    def _handle_header_hover(self, viewport: QWidget, event) -> None:
        if event.type() == QEvent.Type.MouseMove:
            point = event.position().toPoint()
            section = self.reference_header.logicalIndexAt(point)
            key = {1: "Rz", 2: "Rq"}.get(section)
            self._show_hover_key(key, viewport.mapToGlobal(point))
        elif event.type() == QEvent.Type.Leave:
            self.hover_preview.hide_preview()

    def _show_hover_key(self, key: str | None, anchor: QPoint) -> None:
        image_path = self.hover_image_paths.get(key or "")
        if key is None or image_path is None:
            self.hover_preview.hide_preview()
            return
        self.hover_preview.show_image(key, image_path, anchor)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.hover_preview.hide_preview()
        super().hideEvent(event)

    def _scale_movie(self) -> None:
        if not self._movie_source_size.isValid():
            return
        available = self.animation_label.size() - QSize(4, 4)
        if not available.isValid():
            return
        self.movie.setScaledSize(
            self._movie_source_size.scaled(
                available,
                Qt.AspectRatioMode.KeepAspectRatio,
            )
        )
