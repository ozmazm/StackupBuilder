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

        def choose_stackup_mode() -> str | None:
            mode_dialog = StackupModeDialog()
            mode_dialog.exec()
            if mode_dialog.chosen_mode is not None:
                logger.info("Stackup mode chosen: %s", mode_dialog.chosen_mode)
            return mode_dialog.chosen_mode

        def create_main_window(mode: str):
            if mode == "rigid_flex":
                from stackup_editor.rigid_flex_app import RigidFlexEditorWindow

                logger.info("Creating rigid-flex main window")
                return RigidFlexEditorWindow(root_path)

            from stackup_editor.qt_app import StackupEditorWindow

            logger.info("Creating rigid main window")
            return StackupEditorWindow(root_path)

        chosen_mode = choose_stackup_mode()
        if chosen_mode is None:
            logger.info("Mode dialog dismissed without a choice; exiting")
            return 0

        window_holder = [create_main_window(chosen_mode)]

        def connect_new_stackup(window) -> None:
            window.newStackupRequested.connect(
                lambda source=window: return_to_mode_chooser(source)
            )

        def return_to_mode_chooser(source_window) -> None:
            app.setQuitOnLastWindowClosed(False)
            source_window.hide()
            replacement_mode = choose_stackup_mode()
            if replacement_mode is None:
                logger.info("Mode dialog dismissed after New; closing application")
                app.setQuitOnLastWindowClosed(True)
                source_window.close()
                return

            replacement = create_main_window(replacement_mode)
            connect_new_stackup(replacement)
            window_holder[0] = replacement
            replacement.showMaximized()
            app.setQuitOnLastWindowClosed(True)
            source_window.close()
            source_window.deleteLater()
            logger.info("Current stackup replaced with a new %s stackup", replacement_mode)

        connect_new_stackup(window_holder[0])
        window_holder[0].showMaximized()
        logger.info("Main window shown maximized")

        exit_code = app.exec()
        logger.warning("QApplication event loop exited with code %s", exit_code)
        return exit_code
    except Exception:
        logger.exception("Application startup failed")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
