from __future__ import annotations

import unittest
from pathlib import Path

from stackup_editor.catalog import MaterialCatalog


ROOT = Path(__file__).resolve().parents[1]


class FR406CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = MaterialCatalog.load(ROOT / "data" / "material_catalog.json")
        cls.entries = cls.catalog.filter_entries(manufacturer="Isola", family="FR406")

    def test_catalog_contains_all_official_table_rows(self) -> None:
        cores = [entry for entry in self.entries if entry.material_type == "core"]
        prepregs = [entry for entry in self.entries if entry.material_type == "prepreg"]

        self.assertEqual(len(cores), 22)
        self.assertEqual(len(prepregs), 8)
        self.assertEqual(
            {entry.source_pdf for entry in self.entries},
            {"fr406-laminate-and-prepreg__Dk_Df_Tables.pdf"},
        )

    def test_core_frequency_values_are_preserved(self) -> None:
        entry = next(
            entry
            for entry in self.entries
            if entry.material_type == "core"
            and entry.construction == "1x1080"
            and entry.classification == "Standard"
        )

        self.assertEqual(entry.sorted_frequencies, [0.1, 0.5, 1.0, 2.0, 5.0, 10.0])
        self.assertEqual(entry.dk_at(0.1), 3.83)
        self.assertEqual(entry.dk_at(10.0), 3.72)
        self.assertEqual(entry.df_at(0.1), 0.014)
        self.assertEqual(entry.df_at(10.0), 0.019)
        self.assertEqual(entry.resin_content_pct, 58.0)
        self.assertAlmostEqual(entry.thickness_mm, 0.064)

    def test_current_four_mil_alternate_is_used(self) -> None:
        alternate = next(
            entry
            for entry in self.entries
            if entry.material_type == "core"
            and entry.classification == "Alternate"
            and entry.thickness_in == 0.004
        )

        self.assertEqual(alternate.construction, "106/1080")
        self.assertEqual(alternate.resin_content_pct, 59.0)
        self.assertFalse(any(entry.construction == "1x3070" for entry in self.entries))

    def test_revision_d_prepreg_values_are_preserved(self) -> None:
        entry = next(
            entry
            for entry in self.entries
            if entry.material_type == "prepreg"
            and entry.construction == "7628"
            and entry.classification == "Alternate"
            and entry.resin_content_pct == 47.0
        )

        self.assertAlmostEqual(entry.thickness_in, 0.0084)
        self.assertEqual(entry.reference_freq_ghz, 10.0)
        self.assertEqual(entry.reference_dk, 3.97)
        self.assertEqual(entry.reference_df, 0.017)
        self.assertFalse(
            any(
                candidate.material_type == "prepreg"
                and candidate.construction == "2116"
                and candidate.resin_content_pct == 62.0
                for candidate in self.entries
            )
        )


if __name__ == "__main__":
    unittest.main()
