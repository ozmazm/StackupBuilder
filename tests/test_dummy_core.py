from __future__ import annotations

import unittest
from pathlib import Path

from stackup_editor.catalog import MaterialCatalog
from stackup_editor.exporter import (
    _parse_xpedition_field_line,
    export_stackup_text,
    export_stackup_xpedition,
    import_stackup_text,
)
from stackup_editor.models import (
    DIELECTRIC_TYPE_CHOICES,
    CopperLayer,
    DielectricLayer,
    Stackup,
    catalog_material_type_for_dielectric,
)


ROOT = Path(__file__).resolve().parents[1]


class DummyCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = MaterialCatalog.load(ROOT / "data" / "material_catalog.json")
        cls.core = cls.catalog.first_for("core")
        cls.prepreg = cls.catalog.first_for("prepreg")
        cls.no_flow = cls.catalog.first_for("no_flow_prepreg")

    def test_dummy_core_is_a_material_choice_backed_by_core_catalog(self) -> None:
        self.assertIn(("Dummy Core", "dummy_core"), DIELECTRIC_TYPE_CHOICES)
        self.assertEqual(catalog_material_type_for_dielectric("dummy_core"), "core")
        self.assertTrue(
            self.catalog.filter_entries(
                material_type=catalog_material_type_for_dielectric("dummy_core")
            )
        )

    def test_adjacent_dummy_cores_are_rejected_without_bonding_prepreg(self) -> None:
        stackup = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="prepreg",
                    material_id=self.prepreg.id,
                ),
                DielectricLayer(
                    dielectric_type="dummy_core",
                    material_id=self.core.id,
                ),
                DielectricLayer(
                    dielectric_type="dummy_core",
                    material_id=self.core.id,
                ),
                DielectricLayer(
                    dielectric_type="prepreg",
                    material_id=self.prepreg.id,
                ),
                CopperLayer(),
            ],
        )

        self.assertIsNotNone(stackup.dummy_core_bonding_violation())

    def test_dummy_core_requires_prepreg_before_and_after(self) -> None:
        missing_before = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="dummy_core",
                    material_id=self.core.id,
                ),
                DielectricLayer(
                    dielectric_type="prepreg",
                    material_id=self.prepreg.id,
                ),
                CopperLayer(),
            ],
        )
        missing_after = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="prepreg",
                    material_id=self.prepreg.id,
                ),
                DielectricLayer(
                    dielectric_type="dummy_core",
                    material_id=self.core.id,
                ),
                CopperLayer(),
            ],
        )
        valid = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="prepreg",
                    material_id=self.prepreg.id,
                ),
                DielectricLayer(
                    dielectric_type="dummy_core",
                    material_id=self.core.id,
                ),
                DielectricLayer(
                    dielectric_type="no_flow_prepreg",
                    material_id=self.no_flow.id,
                ),
                CopperLayer(),
            ],
        )

        self.assertEqual(
            missing_before.dummy_core_bonding_violation(),
            (1, "before"),
        )
        self.assertEqual(
            missing_after.dummy_core_bonding_violation(),
            (2, "after"),
        )
        self.assertIsNone(valid.dummy_core_bonding_violation())

    def test_text_export_round_trip_preserves_dummy_core_type(self) -> None:
        stackup = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="prepreg",
                    material_id=self.prepreg.id,
                ),
                DielectricLayer(
                    dielectric_type="dummy_core",
                    material_id=self.core.id,
                    selected_freq_ghz=self.core.max_freq_ghz,
                ),
                DielectricLayer(
                    dielectric_type="prepreg",
                    material_id=self.prepreg.id,
                ),
                CopperLayer(),
            ],
        )

        exported = export_stackup_text(stackup, self.catalog, "mm")
        imported, _unit, _workspace = import_stackup_text(exported, self.catalog)

        self.assertIn("Dummy Core dielectric", exported)
        imported_layer = imported.layers[2]
        self.assertIsInstance(imported_layer, DielectricLayer)
        self.assertEqual(imported_layer.dielectric_type, "dummy_core")
        self.assertEqual(imported_layer.material_id, self.core.id)

    def test_rigid_xpedition_maps_no_flow_to_prepreg_and_dummy_to_core(self) -> None:
        description_core = next(
            entry
            for entry in self.catalog.filter_entries(material_type="core")
            if entry.family == "FR370HR"
            and entry.construction == "4x7628/1x1080"
            and entry.resin_content_pct == 44.0
        )
        description_no_flow = self.catalog.find("arlon-51n-no-flow-51n8065")
        stackup = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="no_flow_prepreg",
                    material_id=description_no_flow.id,
                ),
                DielectricLayer(
                    dielectric_type="dummy_core",
                    material_id=description_core.id,
                ),
                DielectricLayer(
                    dielectric_type="prepreg",
                    material_id=self.prepreg.id,
                ),
                CopperLayer(),
            ],
        )

        dielectric_fields = [
            _parse_xpedition_field_line(line)
            for line in export_stackup_xpedition(stackup, self.catalog).splitlines()
            if '(LAYER NAME="DIELECTRIC_' in line
        ]

        self.assertEqual(
            [fields["PREPREG"] for fields in dielectric_fields],
            ["1", "0", "1"],
        )
        self.assertEqual(
            dielectric_fields[0]["DESCRIPTION"],
            "ARLON 51N-No Flow PP 1080 RC %65",
        )
        self.assertEqual(
            dielectric_fields[1]["DESCRIPTION"],
            "FR370HR-DummyCore 4x7628/1x1080 RC %44",
        )


if __name__ == "__main__":
    unittest.main()
