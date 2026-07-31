from __future__ import annotations

import unittest
from pathlib import Path

from stackup_editor.catalog import MaterialCatalog


ROOT = Path(__file__).resolve().parents[1]


class IT180ACatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = MaterialCatalog.load(ROOT / "data" / "material_catalog.json")
        cls.entries = [
            entry
            for entry in cls.catalog.entries
            if entry.manufacturer == "ITEQ" and entry.family == "IT-180A"
        ]

    def test_catalog_contains_all_xml_material_rows(self) -> None:
        cores = [entry for entry in self.entries if entry.material_type == "core"]
        prepregs = [entry for entry in self.entries if entry.material_type == "prepreg"]

        self.assertEqual(len(cores), 15)
        self.assertEqual(len(prepregs), 9)
        self.assertEqual({entry.source_pdf for entry in self.entries}, {"LibraryIT180A.xml"})

    def test_core_values_and_mil_conversion_are_preserved(self) -> None:
        entry = next(
            entry
            for entry in self.entries
            if entry.material_type == "core" and entry.construction == "1x1067"
        )

        self.assertAlmostEqual(entry.thickness_mm, 0.0635)
        self.assertAlmostEqual(entry.thickness_in, 0.0025)
        self.assertAlmostEqual(entry.thickness_um, 63.5)
        self.assertEqual(entry.resin_content_pct, 71.0)
        self.assertEqual(entry.sorted_frequencies, [10.0])
        self.assertEqual(entry.dk_at(10.0), 3.6)
        self.assertEqual(entry.df_at(10.0), 0.015)

    def test_prepreg_values_are_preserved(self) -> None:
        entry = next(
            entry
            for entry in self.entries
            if entry.material_type == "prepreg" and entry.construction == "2116"
        )

        self.assertAlmostEqual(entry.thickness_mm, 0.11684)
        self.assertEqual(entry.resin_content_pct, 53.0)
        self.assertEqual(entry.reference_freq_ghz, 10.0)
        self.assertEqual(entry.reference_dk, 3.9)
        self.assertEqual(entry.reference_df, 0.018)


if __name__ == "__main__":
    unittest.main()
