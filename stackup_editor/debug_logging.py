from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import sys
import threading
from pathlib import Path

_CONFIGURED = False
_QT_MESSAGE_HANDLER_INSTALLED = False


def configure_debug_logging(app_name: str, root_path: Path | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = os.environ.get("STACKUP_EDITOR_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    enable_console_logging = os.environ.get("STACKUP_EDITOR_ENABLE_CONSOLE_LOG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in list(root_logger.handlers):
        if getattr(handler, "_stackup_editor_debug_handler", False):
            root_logger.removeHandler(handler)
    if enable_console_logging and not any(
        getattr(handler, "_stackup_editor_debug_handler", False) for handler in root_logger.handlers
    ):
        handler = logging.StreamHandler(sys.stderr)
        handler._stackup_editor_debug_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s | %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
        )
        root_logger.addHandler(handler)

    logging.captureWarnings(True)
    _enable_faulthandler()
    _install_exception_hooks()
    _install_qt_message_handler()
    atexit.register(_log_process_exit)

    logger = logging.getLogger(__name__)
    logger.info("%s debug logging initialized", app_name)
    if root_path is not None:
        logger.info("Application root resolved to %s", root_path)
    logger.info("Python executable: %s", sys.executable)
    logger.info("Python version: %s", sys.version.replace("\n", " "))

    _CONFIGURED = True


def attach_qt_app_logging(app) -> None:
    logger = logging.getLogger("stackup_editor.app")
    logger.info("QApplication created: %s", app)

    try:
        app.aboutToQuit.connect(
            lambda: logger.warning("QApplication.aboutToQuit emitted")
        )
    except Exception:
        logger.exception("Failed to attach aboutToQuit logger")

    try:
        app.lastWindowClosed.connect(
            lambda: logger.warning("QApplication.lastWindowClosed emitted")
        )
    except Exception:
        logger.exception("Failed to attach lastWindowClosed logger")

    try:
        app.applicationStateChanged.connect(
            lambda state: logger.info("Application state changed to %s", _enum_name(state))
        )
    except Exception:
        logger.exception("Failed to attach applicationStateChanged logger")


def _enable_faulthandler() -> None:
    logger = logging.getLogger(__name__)
    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
        logger.info("faulthandler enabled for native crash tracebacks")
    except Exception:
        logger.exception("Could not enable faulthandler")


def _install_exception_hooks() -> None:
    crash_logger = logging.getLogger("stackup_editor.crash")
    original_excepthook = sys.excepthook

    def handle_main_exception(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            crash_logger.warning("KeyboardInterrupt reached sys.excepthook")
        else:
            crash_logger.critical(
                "Unhandled exception on the main thread",
                exc_info=(exc_type, exc_value, exc_traceback),
            )
        try:
            original_excepthook(exc_type, exc_value, exc_traceback)
        except Exception:
            pass

    sys.excepthook = handle_main_exception

    if hasattr(threading, "excepthook"):
        original_threading_hook = threading.excepthook

        def handle_thread_exception(args) -> None:
            crash_logger.critical(
                "Unhandled exception on thread %s",
                getattr(args.thread, "name", "<unknown>"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            try:
                original_threading_hook(args)
            except Exception:
                pass

        threading.excepthook = handle_thread_exception

    if hasattr(sys, "unraisablehook"):
        original_unraisablehook = sys.unraisablehook

        def handle_unraisable(unraisable) -> None:
            crash_logger.error(
                "Unraisable exception from %r: %s",
                getattr(unraisable, "object", None),
                getattr(unraisable, "err_msg", "") or "<no message>",
                exc_info=(
                    getattr(unraisable, "exc_type", None),
                    getattr(unraisable, "exc_value", None),
                    getattr(unraisable, "exc_traceback", None),
                ),
            )
            try:
                original_unraisablehook(unraisable)
            except Exception:
                pass

        sys.unraisablehook = handle_unraisable


def _install_qt_message_handler() -> None:
    global _QT_MESSAGE_HANDLER_INSTALLED
    if _QT_MESSAGE_HANDLER_INSTALLED:
        return

    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        return

    def handle_qt_message(message_type, context, message) -> None:
        if message_type == QtMsgType.QtDebugMsg:
            level = logging.DEBUG
        elif message_type == QtMsgType.QtInfoMsg:
            level = logging.INFO
        elif message_type == QtMsgType.QtWarningMsg:
            level = logging.WARNING
        elif message_type == QtMsgType.QtCriticalMsg:
            level = logging.ERROR
        else:
            level = logging.CRITICAL

        category = getattr(context, "category", "") or "qt"
        file_name = getattr(context, "file", "") or ""
        line = getattr(context, "line", 0) or 0
        location = f"{file_name}:{line}" if file_name else "unknown"
        logging.getLogger(f"qt.{category}").log(level, "%s [%s]", message, location)

    qInstallMessageHandler(handle_qt_message)
    _QT_MESSAGE_HANDLER_INSTALLED = True
    logging.getLogger(__name__).info("Qt message handler installed")


def _log_process_exit() -> None:
    logging.getLogger(__name__).warning("Python process is exiting")


def _enum_name(value: object) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    try:
        return str(int(value))
    except Exception:
        return repr(value)
