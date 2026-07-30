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


class EtchedCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = MaterialCatalog.load(ROOT / "data" / "material_catalog.json")
        cls.core = cls.catalog.first_for("core")
        cls.prepreg = cls.catalog.first_for("prepreg")
        cls.no_flow = cls.catalog.first_for("no_flow_prepreg")

    def dielectric(self, dielectric_type: str) -> DielectricLayer:
        entry = (
            self.core
            if dielectric_type == "etched_core"
            else self.no_flow
            if dielectric_type == "no_flow_prepreg"
            else self.prepreg
        )
        return DielectricLayer(
            dielectric_type=dielectric_type,
            material_id=entry.id,
            selected_freq_ghz=entry.max_freq_ghz,
        )

    def test_etched_core_is_backed_by_core_catalog(self) -> None:
        self.assertIn(("Etched Core", "etched_core"), DIELECTRIC_TYPE_CHOICES)
        self.assertEqual(
            catalog_material_type_for_dielectric("etched_core"),
            "core",
        )

    def test_etched_core_accepts_prepreg_on_either_side(self) -> None:
        before_only = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                self.dielectric("prepreg"),
                self.dielectric("etched_core"),
                CopperLayer(),
            ],
        )
        after_only = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                self.dielectric("etched_core"),
                self.dielectric("no_flow_prepreg"),
                CopperLayer(),
            ],
        )
        both_sides = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                self.dielectric("prepreg"),
                self.dielectric("etched_core"),
                self.dielectric("no_flow_prepreg"),
                CopperLayer(),
            ],
        )

        self.assertIsNone(before_only.etched_core_bonding_violation())
        self.assertIsNone(after_only.etched_core_bonding_violation())
        self.assertIsNone(both_sides.etched_core_bonding_violation())

    def test_etched_core_rejects_missing_prepreg_on_both_sides(self) -> None:
        stackup = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                self.dielectric("etched_core"),
                CopperLayer(),
            ],
        )

        self.assertEqual(
            stackup.etched_core_bonding_violation(),
            (1, "neither"),
        )
        self.assertEqual(
            stackup.bonded_core_bonding_message(),
            "There must be a prepreg before or after Etched Core.",
        )

    def test_text_round_trip_and_xpedition_core_mapping(self) -> None:
        description_core = next(
            entry
            for entry in self.catalog.filter_entries(material_type="core")
            if entry.family == "FR370HR"
            and entry.construction == "4x7628/1x1080"
            and entry.resin_content_pct == 44.0
        )
        stackup = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                self.dielectric("prepreg"),
                DielectricLayer(
                    dielectric_type="etched_core",
                    material_id=description_core.id,
                    selected_freq_ghz=description_core.max_freq_ghz,
                ),
                CopperLayer(),
            ],
        )

        text = export_stackup_text(stackup, self.catalog, "mm")
        imported, _unit, _workspace = import_stackup_text(text, self.catalog)
        xpedition_layers = [
            _parse_xpedition_field_line(line)
            for line in export_stackup_xpedition(
                stackup,
                self.catalog,
            ).splitlines()
            if '(LAYER NAME="DIELECTRIC_' in line
        ]

        self.assertEqual(imported.layers[2].dielectric_type, "etched_core")
        self.assertEqual(xpedition_layers[1]["PREPREG"], "0")
        self.assertEqual(
            xpedition_layers[1]["DESCRIPTION"],
            "FR370HR-EtchedCore 4x7628/1x1080 RC %44",
        )


if __name__ == "__main__":
    unittest.main()
