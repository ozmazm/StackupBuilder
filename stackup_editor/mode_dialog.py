"""mode_dialog.py — startup chooser between Rigid Stackup and Rigid-Flex Stackup."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


class StackupModeDialog(QDialog):
    """Modal shown once at launch. Sets self.chosen_mode to "rigid" or "rigid_flex"."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("StackUp Editor")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.chosen_mode: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 24)
        layout.setSpacing(18)

        title = QLabel("What kind of stackup do you want to create?")
        title.setWordWrap(True)
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        layout.addWidget(title)

        button_row = QHBoxLayout()
        button_row.setSpacing(12)

        self.rigid_button = QPushButton("Rigid Stackup")
        self.rigid_button.setMinimumHeight(56)
        self.rigid_button.clicked.connect(lambda: self._choose("rigid"))

        self.rigid_flex_button = QPushButton("Rigid Flex Stackup")
        self.rigid_flex_button.setMinimumHeight(56)
        self.rigid_flex_button.clicked.connect(lambda: self._choose("rigid_flex"))

        button_row.addWidget(self.rigid_button)
        button_row.addWidget(self.rigid_flex_button)
        layout.addLayout(button_row)

        for button in (self.rigid_button, self.rigid_flex_button):
            button.setStyleSheet(
                "QPushButton { font-size: 13px; font-weight: 600; border-radius: 8px; }"
            )

        self.rigid_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _choose(self, mode: str) -> None:
        self.chosen_mode = mode
        self.accept()
