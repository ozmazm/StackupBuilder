from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from stackup_editor.material_constructions_help import (
        CONSTRUCTION_CROPS,
        CONSTRUCTION_ROWS,
        FIBER_GLASS_INTRO_PARAGRAPHS,
        FIBER_GLASS_PREVENTION_PARAGRAPH,
        FIBER_GLASS_SELECTION_PARAGRAPH,
    )
    from stackup_editor.qt_app import StackupEditorWindow

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


ROOT = Path(__file__).resolve().parents[1]


class MaterialConstructionsHelpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([]) if QT_AVAILABLE else None

    @unittest.skipUnless(QT_AVAILABLE, "PySide6 is not installed in this test environment")
    def test_help_action_opens_reference_table_and_hover_photos(self) -> None:
        window = StackupEditorWindow(ROOT, defer_initial_refresh=True)
        try:
            self.assertEqual(
                window.material_constructions_action.text(),
                "Material Constructions",
            )
            window.material_constructions_action.trigger()
            self.app.processEvents()

            dialog = window._material_constructions_dialog
            self.assertIsNotNone(dialog)
            self.assertTrue(dialog.isVisible())
            self.assertEqual(
                dialog.performance_image_path.name,
                "material_constructions_performance.png",
            )
            self.assertEqual(
                dialog.construction_atlas_path.name,
                "material_constructions_overview.png",
            )
            self.assertEqual(
                [path.name for path in dialog.guide_image_paths],
                [
                    "fiber_glass_effect.png",
                    "fiber_glass_weave_comparison.png",
                    "fiber_glass_zigzag_routing.png",
                ],
            )
            self.assertFalse(dialog.performance_image.source_pixmap.isNull())
            self.assertFalse(dialog.hover_preview.atlas_pixmap.isNull())
            self.assertTrue(
                all(
                    not image.source_pixmap.isNull()
                    for image in dialog.guide_image_labels
                )
            )
            self.assertEqual(
                [image.image_path.name for image in dialog.guide_image_labels],
                [
                    "fiber_glass_effect.png",
                    "fiber_glass_weave_comparison.png",
                    "material_constructions_performance.png",
                    "fiber_glass_zigzag_routing.png",
                ],
            )
            self.assertEqual(dialog.fiber_glass_heading.text(), "Fiber Glass Effect")
            self.assertEqual(dialog.effect_heading.text(), "Effect of Glass Wave")
            self.assertEqual(
                dialog.prevention_heading.text(),
                "How to Prevent Glass Wave Effect?",
            )
            self.assertEqual(
                [label.text() for label in dialog.guide_text_labels],
                [
                    *FIBER_GLASS_INTRO_PARAGRAPHS,
                    FIBER_GLASS_SELECTION_PARAGRAPH,
                    FIBER_GLASS_PREVENTION_PARAGRAPH,
                ],
            )
            self.assertGreater(
                dialog.guide_scroll_area.verticalScrollBar().maximum(),
                0,
            )
            self.assertGreater(
                dialog.reference_frame.geometry().left(),
                dialog.performance_frame.geometry().left(),
            )
            self.assertLess(dialog.reference_frame.width(), 540)
            self.assertEqual(
                dialog.reference_frame.minimumWidth(),
                dialog.reference_frame.maximumWidth(),
            )

            table = dialog.reference_table
            self.assertEqual(table.rowCount(), 15)
            self.assertEqual(table.columnCount(), 3)
            self.assertEqual(
                [table.horizontalHeaderItem(column).text() for column in range(3)],
                [
                    "Construction",
                    "Wrap Count\n(ends/inch)",
                    "Fill Count\n(ends/inch)",
                ],
            )
            self.assertEqual(
                [table.columnWidth(column) for column in range(3)],
                dialog.column_widths,
            )
            actual_rows = tuple(
                tuple(table.item(row, column).text() for column in range(3))
                for row in range(table.rowCount())
            )
            expected_rows = tuple(
                (construction, str(wrap), str(fill))
                for construction, wrap, fill in CONSTRUCTION_ROWS
            )
            self.assertEqual(actual_rows, expected_rows)
            self.assertEqual(set(CONSTRUCTION_CROPS), {row[0] for row in CONSTRUCTION_ROWS})

            dialog._show_construction_preview(0, 0)
            self.app.processEvents()
            self.assertEqual(dialog.hover_preview.current_construction, "106")
            self.assertTrue(dialog.hover_preview.isVisible())
            self.assertIsNotNone(dialog.hover_preview.pixmap())
            self.assertFalse(dialog.hover_preview.pixmap().isNull())

            dialog._show_construction_preview(0, 1)
            self.assertIsNone(dialog.hover_preview.current_construction)
            dialog.close()
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
