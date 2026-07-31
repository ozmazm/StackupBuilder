from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from stackup_editor.catalog import MaterialCatalog
from stackup_editor.material_comparison_model import (
    RADAR_AXES,
    RADAR_AXIS_HELP,
    FamilySummary,
    build_family_summaries,
    normalized_profiles,
)

try:
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from stackup_editor.material_comparison import MaterialComparisonDialog
    from stackup_editor.qt_app import StackupEditorWindow
    from stackup_editor.rigid_flex_app import RigidFlexEditorWindow

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


ROOT = Path(__file__).resolve().parents[1]


class MaterialComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([]) if QT_AVAILABLE else None
        cls.catalog = MaterialCatalog.load(ROOT / "data" / "material_catalog.json")

    def test_family_summary_uses_consistent_frequency_and_type(self) -> None:
        summaries = build_family_summaries(
            self.catalog,
            manufacturer="Isola",
            material_type="core",
            frequency_ghz=10.0,
            search="FR406",
        )

        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary.family, "FR406")
        self.assertEqual(summary.entry_count, 22)
        self.assertEqual(summary.construction_count, 20)
        self.assertEqual(summary.frequency_ghz, 10.0)
        self.assertAlmostEqual(summary.average_dk, 3.9754545454545456)
        self.assertAlmostEqual(summary.average_df, 0.016772727272727272)
        self.assertAlmostEqual(summary.min_thickness_mm, 0.064)
        self.assertAlmostEqual(summary.max_thickness_mm, 0.889)

    def test_normalized_profile_plots_average_dk_and_df_directly(self) -> None:
        common = {
            "manufacturer": "Example",
            "material_type": "core",
            "frequency_ghz": 10.0,
            "entry_count": 1,
            "construction_count": 1,
            "min_thickness_mm": 0.1,
            "max_thickness_mm": 0.2,
            "min_resin_pct": 45.0,
            "max_resin_pct": 55.0,
            "max_frequency_ghz": 10.0,
        }
        lower_values = FamilySummary(
            family="Lower values", average_dk=3.5, average_df=0.004, **common
        )
        higher_values = FamilySummary(
            family="Higher values", average_dk=4.5, average_df=0.020, **common
        )

        profiles = normalized_profiles([lower_values, higher_values])

        self.assertEqual(RADAR_AXES[:2], ("Average Dk", "Average Df"))
        self.assertGreater(profiles[higher_values.key][0], profiles[lower_values.key][0])
        self.assertGreater(profiles[higher_values.key][1], profiles[lower_values.key][1])
        self.assertTrue(
            all(0.18 <= score <= 1.0 for profile in profiles.values() for score in profile)
        )

    def test_every_radar_axis_has_hover_help(self) -> None:
        self.assertEqual(set(RADAR_AXIS_HELP), set(RADAR_AXES))
        self.assertIn("Lower Df means lower dielectric loss", RADAR_AXIS_HELP["Average Df"])
        self.assertIn("not mechanical flexibility", RADAR_AXIS_HELP["Resin flexibility"])

    def test_arlon_is_excluded_from_material_comparison(self) -> None:
        summaries = build_family_summaries(
            self.catalog,
            frequency_ghz=None,
        )

        self.assertNotIn("arlon", {summary.manufacturer.casefold() for summary in summaries})

    def test_no_flow_prepregs_are_excluded_from_material_comparison(self) -> None:
        summaries = build_family_summaries(self.catalog, frequency_ghz=None)
        no_flow_summaries = build_family_summaries(
            self.catalog,
            material_type="no_flow_prepreg",
            frequency_ghz=None,
        )

        self.assertNotIn("no_flow_prepreg", {summary.material_type for summary in summaries})
        self.assertEqual(no_flow_summaries, [])

    def test_requested_megtron_families_are_combined_for_comparison(self) -> None:
        summaries = build_family_summaries(
            self.catalog,
            manufacturer="Panasonic",
            frequency_ghz=None,
        )
        by_family = {summary.family: summary for summary in summaries}
        expected_groups = {
            "Megtron6 (G)": {"Megtron6 R-5670(G)", "Megtron6 R-5775(G)"},
            "Megtron6 (K)": {"Megtron6 R-5670(K)", "Megtron6 R-5775(K)"},
            "Megtron6 (N)": {"Megtron6 R-5670(N)", "Megtron6 R-5775(N)"},
            "Megtron7 (N)": {"Megtron7 R-5680(N)", "Megtron7 R-5785(N)"},
            "Megtron7 (YN)": {"Megtron7 R-568Y(N)", "Megtron7 R-578Y(N)"},
            "Megtron8 (YN)": {"Megtron8 R-569Y(N)", "Megtron8 R-579Y(N)"},
            "Megtron7 (YU)": {"Megtron8 R-569Y(U)", "Megtron8 R-579Y(U)"},
        }

        for display_family, source_families in expected_groups.items():
            self.assertIn(display_family, by_family)
            self.assertEqual(set(by_family[display_family].catalog_families), source_families)
            self.assertGreater(by_family[display_family].entry_count, 0)

        source_names = set().union(*expected_groups.values())
        self.assertTrue(source_names.isdisjoint(by_family))

    def test_megtron_group_display_name_is_searchable(self) -> None:
        summaries = build_family_summaries(
            self.catalog,
            manufacturer="Panasonic",
            frequency_ghz=None,
            search="Megtron7 (YN)",
        )

        self.assertEqual([summary.family for summary in summaries], ["Megtron7 (YN)"])

    @unittest.skipUnless(QT_AVAILABLE, "PySide6 is not installed in this test environment")
    def test_dialog_filters_and_populates_raw_construction_table(self) -> None:
        dialog = MaterialComparisonDialog(self.catalog)
        try:
            self.assertEqual(dialog.type_combo.currentText(), "All laminate types")
            self.assertIsNone(dialog.type_combo.currentData())
            self.assertEqual(dialog.frequency_combo.currentText(), "Catalog reference")
            self.assertIsNone(dialog.frequency_combo.currentData())
            material_types = {
                dialog.type_combo.itemData(index)
                for index in range(dialog.type_combo.count())
            }
            self.assertNotIn("no_flow_prepreg", material_types)
            manufacturers = {
                str(dialog.manufacturer_combo.itemData(index)).casefold()
                for index in range(dialog.manufacturer_combo.count())
                if dialog.manufacturer_combo.itemData(index) is not None
            }
            self.assertNotIn("arlon", manufacturers)
            self.assertGreater(dialog.family_list.count(), 0)
            self.assertGreater(len(dialog._selected_keys), 0)
            active = dialog._active_summary()
            self.assertIsNotNone(active)
            self.assertGreater(dialog.detail_table.rowCount(), 0)
            self.assertIn("families", dialog.match_count_label.text())
            dialog.type_combo.setCurrentIndex(1)
            dialog.frequency_combo.setCurrentIndex(dialog.frequency_combo.count() - 1)
            dialog._reset_filters()
            self.assertEqual(dialog.type_combo.currentText(), "All laminate types")
            self.assertIsNone(dialog.type_combo.currentData())
            self.assertEqual(dialog.frequency_combo.currentText(), "Catalog reference")
            self.assertIsNone(dialog.frequency_combo.currentData())
        finally:
            dialog.close()

    @unittest.skipUnless(QT_AVAILABLE, "PySide6 is not installed in this test environment")
    def test_radar_axis_help_appears_only_while_hovered(self) -> None:
        dialog = MaterialComparisonDialog(self.catalog)
        try:
            dialog.show()
            self.app.processEvents()
            average_dk_rect = dialog.radar._axis_label_hits["Average Dk"]
            QTest.mouseMove(dialog.radar, average_dk_rect.center().toPoint())
            self.app.processEvents()
            self.assertEqual(dialog.radar._hovered_axis, "Average Dk")

            QTest.mouseMove(dialog.radar, QPoint(1, 1))
            self.app.processEvents()
            self.assertIsNone(dialog.radar._hovered_axis)
        finally:
            dialog.close()

    @unittest.skipUnless(QT_AVAILABLE, "PySide6 is not installed in this test environment")
    def test_help_menu_exposes_material_comparison(self) -> None:
        window = StackupEditorWindow(ROOT, defer_initial_refresh=True)
        try:
            self.assertEqual(window.help_menu.title(), "&Help")
            self.assertEqual(window.material_comparison_action.text(), "Material Comparison")
            self.assertTrue(window.material_comparison_action.isEnabled())
            window.material_comparison_action.trigger()
            self.app.processEvents()
            self.assertIsNotNone(window._material_comparison_dialog)
            self.assertTrue(window._material_comparison_dialog.isVisible())
            window._material_comparison_dialog.close()
        finally:
            window.close()

    @unittest.skipUnless(QT_AVAILABLE, "PySide6 is not installed in this test environment")
    def test_rigid_flex_session_does_not_expose_help_menu(self) -> None:
        window = RigidFlexEditorWindow(ROOT)
        try:
            menu_titles = {action.text() for action in window.menuBar().actions()}
            self.assertNotIn("&Help", menu_titles)
            self.assertFalse(hasattr(window, "material_comparison_action"))
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
