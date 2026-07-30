from __future__ import annotations

import unittest
from types import SimpleNamespace

from stackup_editor.models import CopperLayer, DielectricLayer, FlexCoreLayer, Stackup
from stackup_editor.rigid_flex_app import RigidFlexEditorWindow


class RigidFlexMasterPreservationTests(unittest.TestCase):
    @staticmethod
    def _master_material_stackup() -> tuple[Stackup, FlexCoreLayer]:
        flex = FlexCoreLayer(
            material_id="flex",
            copper_thickness_top_mm=0.018,
            copper_thickness_bottom_mm=0.018,
        )
        return (
            Stackup(
                mode="rigid",
                layers=[
                    CopperLayer(),
                    DielectricLayer(
                        dielectric_type="etched_core",
                        material_id="top-etched",
                    ),
                    DielectricLayer(
                        dielectric_type="no_flow_prepreg",
                        material_id="top-no-flow",
                    ),
                    CopperLayer(),
                    flex,
                    CopperLayer(),
                    DielectricLayer(
                        dielectric_type="no_flow_prepreg",
                        material_id="bottom-no-flow",
                    ),
                    DielectricLayer(
                        dielectric_type="etched_core",
                        material_id="bottom-etched",
                    ),
                    CopperLayer(),
                ],
            ),
            flex,
        )

    def test_explicit_flex_rebuild_preserves_etched_core_when_no_flow_exists(self) -> None:
        stackup, flex = self._master_material_stackup()
        default_prepreg = DielectricLayer(
            dielectric_type="prepreg",
            material_id="default-pp",
        )
        rigid_editor = SimpleNamespace(
            stackup=stackup,
            _default_dielectric=lambda _kind: default_prepreg,
        )
        harness = SimpleNamespace(
            _ensure_rigid_flex_gap_map=lambda _editor: {0: 1},
            _rigid_core_template_for_slots=lambda _editor: DielectricLayer(
                dielectric_type="core",
                material_id="default-core",
            ),
        )

        rebuilt = RigidFlexEditorWindow._rebuild_rigid_from_explicit_flex_gaps(
            harness,
            rigid_editor,
            {0: flex},
        )
        dielectric_types = [
            layer.dielectric_type
            for layer in rebuilt.layers
            if isinstance(layer, DielectricLayer)
        ]
        material_ids = [
            layer.material_id
            for layer in rebuilt.layers
            if isinstance(layer, DielectricLayer)
        ]

        self.assertEqual(
            dielectric_types,
            ["etched_core", "no_flow_prepreg", "no_flow_prepreg", "etched_core"],
        )
        self.assertEqual(
            material_ids,
            ["top-etched", "top-no-flow", "bottom-no-flow", "bottom-etched"],
        )

    def test_new_four_layer_branch_copies_complete_master_l1_l4_materials(self) -> None:
        master_stackup, flex = self._master_material_stackup()
        flex_stackup = Stackup(
            mode="flex",
            layers=[CopperLayer(), flex, CopperLayer()],
            flex_sandwich_slots=[0],
            flex_slot_capacity=1,
        )
        default_prepreg = DielectricLayer(
            dielectric_type="prepreg",
            material_id="default-pp",
        )
        master_editor = SimpleNamespace(
            stackup=master_stackup,
            _default_dielectric=lambda _kind: default_prepreg,
        )
        flex_editor = SimpleNamespace(
            stackup=flex_stackup,
            zone_display_name="Flex Part 1",
        )
        harness = SimpleNamespace(
            _parent_rigid_for_flex=lambda _editor: master_editor,
            _flex_copper_number_overrides=lambda _parent, _flex: {0: 2, 2: 3},
            _flex_slot_templates=lambda _editor: {0: flex},
            _rigid_core_template_for_slots=lambda _editor: DielectricLayer(
                dielectric_type="core",
                material_id="default-core",
            ),
            _rigid_branch_global_numbers={id(master_editor): [1, 2, 3, 4]},
        )

        branch, _slot_map, global_numbers, _gap_map, _coverage = (
            RigidFlexEditorWindow._build_combined_rigid_branch(
                harness,
                master_editor,
                [flex_editor],
                target_copper_count=4,
            )
        )

        self.assertEqual(global_numbers, [1, 2, 3, 4])
        self.assertEqual(
            [
                layer.dielectric_type
                for layer in branch.layers
                if isinstance(layer, DielectricLayer)
            ],
            ["etched_core", "no_flow_prepreg", "no_flow_prepreg", "etched_core"],
        )
        self.assertEqual(
            [
                layer.material_id
                for layer in branch.layers
                if isinstance(layer, DielectricLayer)
            ],
            ["top-etched", "top-no-flow", "bottom-no-flow", "bottom-etched"],
        )
        self.assertEqual(
            sum(isinstance(layer, FlexCoreLayer) for layer in branch.layers),
            1,
        )


if __name__ == "__main__":
    unittest.main()
