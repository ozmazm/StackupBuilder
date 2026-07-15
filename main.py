"""main.py — StackUp Editor entry point."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from stackup_editor.debug_logging import attach_qt_app_logging, configure_debug_logging


def resolve_app_root() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main() -> int:
    root_path = resolve_app_root()
    configure_debug_logging("StackUp Editor", root_path)
    logger = logging.getLogger(__name__)
    logger.info("Starting application entry point")

    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

        app = QApplication.instance() or QApplication(sys.argv)
        attach_qt_app_logging(app)
        app.setStyle("Fusion")
        logger.info("QApplication style set to Fusion")

        from stackup_editor.mode_dialog import StackupModeDialog

        mode_dialog = StackupModeDialog()
        mode_dialog.exec()
        if mode_dialog.chosen_mode is None:
            logger.info("Mode dialog dismissed without a choice; exiting")
            return 0
        logger.info("Stackup mode chosen: %s", mode_dialog.chosen_mode)

        if mode_dialog.chosen_mode == "rigid_flex":
            from stackup_editor.rigid_flex_app import RigidFlexEditorWindow

            logger.info("Creating rigid-flex main window")
            window = RigidFlexEditorWindow(root_path)
        else:
            from stackup_editor.qt_app import StackupEditorWindow

            logger.info("Creating rigid main window")
            window = StackupEditorWindow(root_path)

        window.showMaximized()
        logger.info("Main window shown maximized")

        exit_code = app.exec()
        logger.warning("QApplication event loop exited with code %s", exit_code)
        return exit_code
    except Exception:
        logger.exception("Application startup failed")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
