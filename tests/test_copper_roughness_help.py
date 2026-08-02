from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from stackup_editor.models import (
    COPPER_RQ_BY_TYPE_UM,
    COPPER_RZ_BY_TYPE_UM,
    CopperLayer,
    copper_roughness_um,
)

try:
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from stackup_editor.qt_app import StackupEditorWindow

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


ROOT = Path(__file__).resolve().parents[1]


class CopperRoughnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([]) if QT_AVAILABLE else None

    def test_reference_values_match_copper_foil_table(self) -> None:
        self.assertEqual(
            COPPER_RZ_BY_TYPE_UM,
            {"RTF": 4.21, "VLP": 3.86, "HVLP": 1.80, "ULP": 1.09},
        )
        self.assertEqual(
            COPPER_RQ_BY_TYPE_UM,
            {"RTF": 0.48, "VLP": 0.50, "HVLP": 0.22, "ULP": 0.12},
        )

    def test_stackup_copper_uses_corrected_rq_values(self) -> None:
        for copper_type, expected_rq in COPPER_RQ_BY_TYPE_UM.items():
            with self.subTest(copper_type=copper_type):
                self.assertEqual(copper_roughness_um(copper_type), expected_rq)
                self.assertEqual(CopperLayer(copper_type=copper_type).roughness_um, expected_rq)

        self.assertEqual(copper_roughness_um("ED"), 0.48)
        self.assertEqual(copper_roughness_um("RA"), 0.50)

    @unittest.skipUnless(QT_AVAILABLE, "PySide6 is not installed in this test environment")
    def test_help_menu_opens_copper_roughness_guide(self) -> None:
        window = StackupEditorWindow(ROOT, defer_initial_refresh=True)
        try:
            self.assertEqual(window.copper_roughness_action.text(), "Copper Roughness")
            window.copper_roughness_action.trigger()
            self.app.processEvents()

            dialog = window._copper_roughness_dialog
            self.assertIsNotNone(dialog)
            self.assertTrue(dialog.isVisible())
            self.assertEqual(dialog.height(), 760)
            self.assertTrue(dialog.movie.isValid())
            self.assertFalse(hasattr(dialog, "calculation_labels"))
            self.assertEqual(dialog.reference_frame.layout().count(), 4)
            self.assertEqual(
                dialog.importance_heading.text(),
                "Why Surface Roughness Matters",
            )
            self.assertIn("skin effect", dialog.importance_paragraph.text())
            self.assertIn("skin depth decreases", dialog.importance_paragraph.text())
            self.assertGreater(
                dialog.importance_heading.font().pointSizeF(),
                dialog.importance_paragraph.font().pointSizeF(),
            )
            self.assertEqual(dialog.reference_table.rowCount(), 4)
            self.assertEqual(
                [
                    dialog.reference_table.horizontalHeaderItem(column).text()
                    for column in range(dialog.reference_table.columnCount())
                ],
                ["Copper foil", "Rz (µm)", "Rq (µm)"],
            )
            displayed = {
                dialog.reference_table.item(row, 0).text(): (
                    dialog.reference_table.item(row, 1).text(),
                    dialog.reference_table.item(row, 2).text(),
                )
                for row in range(dialog.reference_table.rowCount())
            }
            self.assertEqual(
                displayed,
                {
                    "RTF": ("4.21", "0.48"),
                    "VLP": ("3.86", "0.50"),
                    "HVLP": ("1.80", "0.22"),
                    "ULP": ("1.09", "0.12"),
                },
            )

            expected_hover_files = {
                "RTF": "roughness_hover_rtf.png",
                "VLP": "roughness_hover_vlp.png",
                "HVLP": "roughness_hover_hvlp_ulp.png",
                "ULP": "roughness_hover_hvlp_ulp.png",
                "Rz": "roughness_hover_rz.png",
                "Rq": "roughness_hover_rq.png",
            }
            self.assertEqual(
                {
                    key: path.name
                    for key, path in dialog.hover_image_paths.items()
                },
                expected_hover_files,
            )

            for row, key in enumerate(("RTF", "VLP", "HVLP", "ULP")):
                item = dialog.reference_table.item(row, 0)
                QTest.mouseMove(
                    dialog.reference_table.viewport(),
                    dialog.reference_table.visualItemRect(item).center(),
                )
                self.app.processEvents()
                self.assertEqual(dialog.hover_preview.current_key, key)
                self.assertTrue(dialog.hover_preview.isVisible())

            for column, key in ((1, "Rz"), (2, "Rq")):
                x = (
                    dialog.reference_header.sectionViewportPosition(column)
                    + dialog.reference_header.sectionSize(column) // 2
                )
                QTest.mouseMove(
                    dialog.reference_header.viewport(),
                    QPoint(x, dialog.reference_header.height() // 2),
                )
                self.app.processEvents()
                self.assertEqual(dialog.hover_preview.current_key, key)
                self.assertTrue(dialog.hover_preview.isVisible())

            dialog.hover_preview.hide_preview()
            dialog.close()
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
