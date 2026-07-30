from __future__ import annotations

import unittest
from pathlib import Path

from stackup_editor.catalog import MaterialCatalog
from stackup_editor.exporter import export_stackup_xpedition
from stackup_editor.models import (
    DIELECTRIC_TYPE_CHOICES,
    CopperLayer,
    DielectricLayer,
    Stackup,
    is_prepreg_dielectric_type,
)


ROOT = Path(__file__).resolve().parents[1]


class NoFlowPrepregTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = MaterialCatalog.load(ROOT / "data" / "material_catalog.json")
        cls.entries = cls.catalog.filter_entries(material_type="no_flow_prepreg")

    def test_catalog_contains_all_verified_datasheet_constructions(self) -> None:
        self.assertEqual(len(self.entries), 6)
        self.assertEqual(
            {
                (entry.manufacturer, entry.family, entry.variant, entry.construction)
                for entry in self.entries
            },
            {
                ("Arlon", "51N", "51N0672", "106"),
                ("Arlon", "51N", "51N0666", "106"),
                ("Arlon", "51N", "51N8065", "1080"),
                ("Arlon", "51N", "51N8060", "1080"),
                ("TUC", "TU-84P NF", "TU-84P NF", "106"),
                ("TUC", "TU-84P NF", "TU-84P NF", "1080"),
            },
        )

    def test_tuc_frequency_selection_has_datasheet_dk_and_df(self) -> None:
        entry = self.catalog.find("tuc-tu-84p-nf-no-flow-106")
        self.assertEqual(entry.sorted_frequencies, [1.0, 5.0, 10.0])
        self.assertEqual(entry.dk_at(5.0), 4.5)
        self.assertEqual(entry.df_at(10.0), 0.015)

    def test_material_column_exposes_no_flow_prepreg_choice(self) -> None:
        self.assertIn(
            ("No-Flow Prepreg", "no_flow_prepreg"),
            DIELECTRIC_TYPE_CHOICES,
        )

    def test_no_flow_material_obeys_prepreg_structural_and_export_rules(self) -> None:
        entry = self.catalog.find("arlon-51n-no-flow-51n0672")
        layer = DielectricLayer(
            dielectric_type="no_flow_prepreg",
            material_id=entry.id,
            selected_freq_ghz=1.0,
        )
        stackup = Stackup(
            layers=[
                CopperLayer(thickness_mm=0.035),
                layer,
                CopperLayer(thickness_mm=0.035),
            ],
            mode="rigid",
        )

        self.assertTrue(is_prepreg_dielectric_type(layer.dielectric_type))
        self.assertIsNone(stackup.consecutive_core_pair())
        self.assertIn("PREPREG=1", export_stackup_xpedition(stackup, self.catalog))


if __name__ == "__main__":
    unittest.main()
