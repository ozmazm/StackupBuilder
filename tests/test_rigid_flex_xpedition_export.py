from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path

from stackup_editor.catalog import MaterialCatalog
from stackup_editor.exporter import (
    _parse_xpedition_field_line,
    export_rigid_flex_xpedition,
    import_rigid_flex_text,
)
from stackup_editor.models import DielectricLayer


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "Sample Stackups"


def _layer_fields(text: str) -> list[dict[str, str]]:
    return [
        _parse_xpedition_field_line(line)
        for line in text.splitlines()
        if "(LAYER " in line
    ]


class RigidFlexXpeditionExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = MaterialCatalog.load(ROOT / "data" / "material_catalog.json")
        cls.zones = import_rigid_flex_text(
            (SAMPLES / "Xpedition_Example_Export.txt").read_text(encoding="utf-8")
        )
        cls.reference_text = (SAMPLES / "RigidFlex_8L_Rigid_4LFlex.stk").read_text(
            encoding="utf-8"
        )

    def test_export_matches_validated_flattened_layer_sequence(self) -> None:
        generated = _layer_fields(export_rigid_flex_xpedition(self.zones, self.catalog))
        reference = _layer_fields(self.reference_text)

        self.assertEqual(
            [layer["NAME"] for layer in generated],
            [
                "MasterRigidTopSoldermask",
                "L1",
                "MasterRigidDielectric1",
                "FlexPart1TopCoverlayPI",
                "FlexPart1TopCoverlayAdhessive",
                "L2",
                "FlexPart1FlexCore",
                "L3",
                "FlexPart1BotCoverlayAdhessive",
                "FlexPart1BotCoverlayPI",
                "MasterRigidDielectric2",
                "L4",
                "MasterRigidDielectric3",
                "L5",
                "MasterRigidDielectric4",
                "FlexPart2TopCoverlayPI",
                "FlexPart2TopCoverlayAdhessive",
                "L6",
                "FlexPart2FlexCore",
                "L7",
                "FlexPart2BotCoverlayAdhessive",
                "FlexPart2BotCoverlayPI",
                "MasterRigidDielectric5",
                "L8",
                "MasterRigidBotSoldermask",
            ],
        )
        self.assertEqual(len(generated), 25)

        numeric_fields = ("THICKNESS", "ER", "TG", "ER_FREQ")
        classification_fields = ("DESCRIPTION", "TYPE", "PREPREG")
        for actual, expected in zip(generated, reference):
            for field in classification_fields:
                self.assertEqual(actual.get(field), expected.get(field), (actual["NAME"], field))
            if actual["TYPE"] == "SIGNAL":
                self.assertAlmostEqual(
                    float(actual["THICKNESS"]),
                    float(expected["THICKNESS"]),
                    delta=2.1e-9,
                    msg=actual["NAME"],
                )
                self.assertEqual(actual.get("ROUGH_TOP"), expected.get("ROUGH_TOP"))
                self.assertEqual(actual.get("ROUGH_BOT"), expected.get("ROUGH_BOT"))
                continue
            for field in numeric_fields:
                self.assertAlmostEqual(
                    float(actual[field]),
                    float(expected[field]),
                    delta=1e-12,
                    msg=f"{actual['NAME']} {field}",
                )

    def test_export_preserves_correct_material_classifications(self) -> None:
        exported = {
            layer["NAME"]: layer
            for layer in _layer_fields(export_rigid_flex_xpedition(self.zones, self.catalog))
        }

        self.assertEqual(exported["MasterRigidDielectric2"]["TG"], "0.026")
        self.assertEqual(exported["MasterRigidDielectric3"]["PREPREG"], "0")
        self.assertEqual(exported["FlexPart1FlexCore"]["PREPREG"], "2")
        self.assertEqual(exported["FlexPart2FlexCore"]["PREPREG"], "2")
        self.assertEqual(exported["FlexPart1TopCoverlayAdhessive"]["TG"], "0.0026")

    def test_export_flattens_multiple_rigid_parts_into_one_stackup(self) -> None:
        zones = import_rigid_flex_text(
            (SAMPLES / "Multiple_rigid_flex_stackup.txt").read_text(encoding="utf-8")
        )

        generated = _layer_fields(export_rigid_flex_xpedition(zones, self.catalog))

        self.assertEqual(
            [layer["NAME"] for layer in generated],
            [
                "MasterRigidTopSoldermask",
                "RigidPart2TopSoldermask",
                "L1",
                "MasterRigidDielectric1",
                "RigidPart2Dielectric1",
                "FlexPart1TopCoverlayPI",
                "FlexPart1TopCoverlayAdhessive",
                "L2",
                "FlexPart1FlexCore",
                "L3",
                "FlexPart1BotCoverlayAdhessive",
                "FlexPart1BotCoverlayPI",
                "MasterRigidDielectric2",
                "RigidPart2Dielectric2",
                "L4",
                "RigidPart2BotSoldermask",
                "MasterRigidDielectric3",
                "RigidPart3TopSoldermask",
                "L5",
                "MasterRigidDielectric4",
                "RigidPart3Dielectric1",
                "FlexPart2TopCoverlayPI",
                "FlexPart2TopCoverlayAdhessive",
                "L6",
                "FlexPart2FlexCore",
                "L7",
                "FlexPart2BotCoverlayAdhessive",
                "FlexPart2BotCoverlayPI",
                "MasterRigidDielectric5",
                "RigidPart3Dielectric2",
                "L8",
                "RigidPart3BotSoldermask",
                "MasterRigidBotSoldermask",
            ],
        )
        self.assertEqual(len(generated), 33)
        self.assertEqual(
            generated[1]["CONFORMAL"],
            "1",
        )
        self.assertEqual(
            generated[-2]["CONFORMAL"],
            "1",
        )

    def test_export_uses_zone_prefixed_no_flow_names(self) -> None:
        zones = deepcopy(
            import_rigid_flex_text(
                (SAMPLES / "Multiple_rigid_flex_stackup.txt").read_text(
                    encoding="utf-8"
                )
            )
        )
        no_flow = DielectricLayer(
            dielectric_type="no_flow_prepreg",
            material_id="arlon-51n-no-flow-51n0672",
            selected_freq_ghz=1.0,
        )
        zones[0].stackup.layers[9] = no_flow
        zones[4].stackup.layers[1] = deepcopy(no_flow)

        generated = _layer_fields(export_rigid_flex_xpedition(zones, self.catalog))
        matching = [
            layer
            for layer in generated
            if layer["NAME"]
            in {
                "MasterRigidNoFLowDielectric1",
                "RigidPart3NoFLowDielectric1",
            }
        ]

        self.assertEqual(len(matching), 2)
        self.assertEqual(
            {layer["NAME"] for layer in matching},
            {
                "MasterRigidNoFLowDielectric1",
                "RigidPart3NoFLowDielectric1",
            },
        )
        self.assertTrue(all(layer["PREPREG"] == "1" for layer in matching))
        self.assertTrue(all("-No Flow PP" in layer["DESCRIPTION"] for layer in matching))

    def test_export_maps_dummy_core_to_xpedition_core(self) -> None:
        zones = deepcopy(self.zones)
        master = zones[0].stackup
        prepreg_index = next(
            index
            for index, layer in enumerate(master.layers)
            if isinstance(layer, DielectricLayer)
            and layer.dielectric_type == "prepreg"
        )
        prepreg = deepcopy(master.layers[prepreg_index])
        core_entry = self.catalog.first_for("core")
        dummy = DielectricLayer(
            dielectric_type="dummy_core",
            material_id=core_entry.id,
            selected_freq_ghz=core_entry.max_freq_ghz,
        )
        master.layers[prepreg_index : prepreg_index + 1] = [
            deepcopy(prepreg),
            dummy,
            deepcopy(prepreg),
        ]

        generated = _layer_fields(
            export_rigid_flex_xpedition(zones, self.catalog)
        )
        matching = [
            layer
            for layer in generated
            if layer.get("PREPREG") == "0"
            and "-DummyCore" in layer.get("DESCRIPTION", "")
            and core_entry.construction in layer.get("DESCRIPTION", "")
        ]

        self.assertTrue(matching)

    def test_export_maps_etched_core_to_xpedition_core(self) -> None:
        zones = deepcopy(self.zones)
        master = zones[0].stackup
        prepreg_index = next(
            index
            for index, layer in enumerate(master.layers)
            if isinstance(layer, DielectricLayer)
            and layer.dielectric_type == "prepreg"
        )
        prepreg = deepcopy(master.layers[prepreg_index])
        core_entry = self.catalog.first_for("core")
        etched = DielectricLayer(
            dielectric_type="etched_core",
            material_id=core_entry.id,
            selected_freq_ghz=core_entry.max_freq_ghz,
        )
        master.layers[prepreg_index : prepreg_index + 1] = [
            deepcopy(prepreg),
            etched,
        ]

        generated = _layer_fields(
            export_rigid_flex_xpedition(zones, self.catalog)
        )
        matching = [
            layer
            for layer in generated
            if layer.get("PREPREG") == "0"
            and "-EtchedCore" in layer.get("DESCRIPTION", "")
            and core_entry.construction in layer.get("DESCRIPTION", "")
        ]

        self.assertTrue(matching)

if __name__ == "__main__":
    unittest.main()
