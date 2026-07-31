from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    from stackup_editor.qt_app import StackupEditorWindow

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is not installed in this test environment")
class RigidStackupLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_session_note_pane_is_hidden_in_rigid_editor(self) -> None:
        window = StackupEditorWindow(ROOT, defer_initial_refresh=True)
        try:
            self.assertTrue(window.note_group.isHidden())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
