from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path

from stackup_editor.exporter import import_rigid_flex_text
from stackup_editor.models import CopperLayer, DielectricLayer, Stackup
from stackup_editor.rigid_flex_app import RigidFlexEditorWindow


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "Sample Stackups"


class _EditorStub:
    is_flex_zone = False

    def __init__(self, stackup) -> None:
        self.stackup = stackup
        self.replace_count = 0

    def _current_row_meta(self):
        return ("layer", 0)

    def replace_stackup(self, stackup, *, select_meta=None) -> None:
        _ = select_meta
        self.stackup = stackup
        self.replace_count += 1


class _WindowHarness:
    def __init__(self, master: _EditorStub, branch: _EditorStub) -> None:
        self._zone_editors = [master, branch]
        self._rigid_branch_global_numbers = {
            id(master): list(range(1, 9)),
            id(branch): [5, 6, 7, 8],
        }

    def _primary_rigid_editor(self):
        return self._zone_editors[0]

    def _rigid_editors(self):
        return self._zone_editors

class RigidFlexCopperSynchronizationTests(unittest.TestCase):
    def test_sub_rigid_copper_inherits_master_profile_by_global_number(self) -> None:
        zones = import_rigid_flex_text(
            (SAMPLES / "Xpedition_Example_Export.txt").read_text(encoding="utf-8")
        )
        master_stackup = deepcopy(zones[0].stackup)
        branch_stackup = deepcopy(zones[0].stackup)
        branch_stackup.layers = branch_stackup.layers[8:15]

        branch_coppers = [
            layer for layer in branch_stackup.layers if isinstance(layer, CopperLayer)
        ]
        for ordinal, layer in enumerate(branch_coppers, start=1):
            layer.thickness_mm = 0.01 * ordinal
            layer.copper_type = "ULP"
            layer.roughness_um = 0.99
            layer.trace_width_mm = 0.123

        master = _EditorStub(master_stackup)
        branch = _EditorStub(branch_stackup)
        harness = _WindowHarness(master, branch)

        RigidFlexEditorWindow._sync_sub_rigid_copper_from_master(harness)

        master_coppers = [
            layer for layer in master.stackup.layers if isinstance(layer, CopperLayer)
        ]
        synchronized = [
            layer for layer in branch.stackup.layers if isinstance(layer, CopperLayer)
        ]
        for global_number, actual in zip([5, 6, 7, 8], synchronized):
            expected = master_coppers[global_number - 1]
            self.assertEqual(actual.thickness_mm, expected.thickness_mm)
            self.assertEqual(actual.copper_type, expected.copper_type)
            self.assertEqual(actual.roughness_um, expected.roughness_um)
            self.assertEqual(actual.trace_width_mm, 0.123)
        self.assertEqual(branch.replace_count, 1)

    def test_no_flow_prepreg_is_carried_by_matching_global_copper_gap(self) -> None:
        source = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="no_flow_prepreg",
                    material_id="arlon-51n-no-flow-51n0672",
                    selected_freq_ghz=1.0,
                ),
                CopperLayer(),
                DielectricLayer(dielectric_type="prepreg", material_id="regular-pp"),
                CopperLayer(),
            ],
        )
        target = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                DielectricLayer(dielectric_type="prepreg", material_id="target-pp"),
                CopperLayer(),
                DielectricLayer(dielectric_type="prepreg", material_id="target-pp"),
                CopperLayer(),
            ],
        )

        templates = RigidFlexEditorWindow._no_flow_templates_by_global_gap(
            source,
            [2, 3, 4],
        )
        changed = RigidFlexEditorWindow._apply_no_flow_templates_to_stackup(
            target,
            [2, 3, 4],
            templates,
        )

        self.assertTrue(changed)
        first_gap = target.layers[1]
        second_gap = target.layers[3]
        self.assertIsInstance(first_gap, DielectricLayer)
        self.assertEqual(first_gap.dielectric_type, "no_flow_prepreg")
        self.assertEqual(first_gap.material_id, "arlon-51n-no-flow-51n0672")
        self.assertIsInstance(second_gap, DielectricLayer)
        self.assertEqual(second_gap.material_id, "target-pp")

    def test_master_can_restore_inherited_no_flow_gap_to_standard_prepreg(self) -> None:
        source = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="prepreg",
                    material_id="master-standard-pp",
                    selected_freq_ghz=10.0,
                ),
                CopperLayer(),
            ],
        )
        target = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="no_flow_prepreg",
                    material_id="arlon-51n-no-flow-51n0672",
                    selected_freq_ghz=1.0,
                ),
                CopperLayer(),
            ],
        )

        templates = RigidFlexEditorWindow._prepreg_templates_by_global_gap(
            source,
            [6, 7],
        )
        changed = RigidFlexEditorWindow._sync_master_controlled_no_flow_to_stackup(
            target,
            [6, 7],
            templates,
        )

        self.assertTrue(changed)
        restored = target.layers[1]
        self.assertIsInstance(restored, DielectricLayer)
        self.assertEqual(restored.dielectric_type, "prepreg")
        self.assertEqual(restored.material_id, "master-standard-pp")
        self.assertEqual(restored.selected_freq_ghz, 10.0)

    def test_no_flow_sync_preserves_rigid_parts_local_prepreg(self) -> None:
        source = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="no_flow_prepreg",
                    material_id="master-no-flow",
                ),
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="prepreg",
                    material_id="master-regular-pp",
                ),
                CopperLayer(),
            ],
        )
        target = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="prepreg",
                    material_id="rigid-part-local-pp",
                ),
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="prepreg",
                    material_id="other-local-pp",
                ),
                CopperLayer(),
            ],
        )

        templates = RigidFlexEditorWindow._prepreg_templates_by_global_gap(
            source,
            [1, 2, 3],
        )
        changed = RigidFlexEditorWindow._sync_master_controlled_no_flow_to_stackup(
            target,
            [1, 2, 3],
            templates,
        )

        self.assertTrue(changed)
        first_gap = target.layers[1:3]
        self.assertEqual(
            [
                layer.material_id
                for layer in first_gap
                if isinstance(layer, DielectricLayer)
            ],
            ["master-no-flow", "rigid-part-local-pp"],
        )

    def test_no_flow_sync_repairs_legacy_gap_that_lost_its_rigid_pp(self) -> None:
        source = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="no_flow_prepreg",
                    material_id="master-no-flow",
                ),
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="prepreg",
                    material_id="master-regular-pp",
                ),
                CopperLayer(),
            ],
        )
        target = Stackup(
            mode="rigid",
            layers=[
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="no_flow_prepreg",
                    material_id="stale-no-flow",
                ),
                CopperLayer(),
                DielectricLayer(
                    dielectric_type="prepreg",
                    material_id="rigid-part-fallback-pp",
                ),
                CopperLayer(),
            ],
        )

        templates = RigidFlexEditorWindow._prepreg_templates_by_global_gap(
            source,
            [1, 2, 3],
        )
        changed = RigidFlexEditorWindow._sync_master_controlled_no_flow_to_stackup(
            target,
            [1, 2, 3],
            templates,
        )

        self.assertTrue(changed)
        repaired_gap = [
            layer
            for layer in target.layers[1:3]
            if isinstance(layer, DielectricLayer)
        ]
        self.assertEqual(
            [layer.material_id for layer in repaired_gap],
            ["master-no-flow", "rigid-part-fallback-pp"],
        )
        self.assertEqual(
            [layer.dielectric_type for layer in repaired_gap],
            ["no_flow_prepreg", "prepreg"],
        )

    def test_no_flow_sync_removes_material_outside_connected_flex_neighbors(self) -> None:
        source_layers = []
        target_layers = []
        for gap_number in range(5):
            source_layers.append(CopperLayer())
            target_layers.append(CopperLayer())
            no_flow = DielectricLayer(
                dielectric_type="no_flow_prepreg",
                material_id=f"master-no-flow-{gap_number}",
            )
            rigid_pp = DielectricLayer(
                dielectric_type="prepreg",
                material_id=f"rigid-part-pp-{gap_number}",
            )
            source_layers.extend((deepcopy(no_flow), deepcopy(rigid_pp)))
            target_layers.extend((deepcopy(no_flow), deepcopy(rigid_pp)))
        source_layers.append(CopperLayer())
        target_layers.append(CopperLayer())
        source = Stackup(mode="rigid", layers=source_layers)
        target = Stackup(mode="rigid", layers=target_layers)
        global_numbers = [3, 4, 5, 6, 7, 8]
        connected_neighbor_gaps = {(5, 6), (7, 8)}

        templates = RigidFlexEditorWindow._prepreg_templates_by_global_gap(
            source,
            global_numbers,
        )
        changed = RigidFlexEditorWindow._sync_master_controlled_no_flow_to_stackup(
            target,
            global_numbers,
            templates,
            controlled_no_flow_gaps=connected_neighbor_gaps,
            valid_no_flow_gaps=connected_neighbor_gaps,
        )

        self.assertTrue(changed)
        copper_indices = [
            index
            for index, layer in enumerate(target.layers)
            if isinstance(layer, CopperLayer)
        ]
        types_by_gap = {}
        for position, (top_index, bottom_index) in enumerate(
            zip(copper_indices, copper_indices[1:])
        ):
            gap = (global_numbers[position], global_numbers[position + 1])
            types_by_gap[gap] = [
                layer.dielectric_type
                for layer in target.layers[top_index + 1 : bottom_index]
                if isinstance(layer, DielectricLayer)
            ]

        self.assertEqual(types_by_gap[(3, 4)], ["prepreg"])
        self.assertEqual(
            types_by_gap[(5, 6)],
            ["no_flow_prepreg", "prepreg"],
        )
        self.assertEqual(
            types_by_gap[(7, 8)],
            ["no_flow_prepreg", "prepreg"],
        )

    def test_each_rigid_part_has_its_own_prefixed_no_flow_layer_name(self) -> None:
        zones = import_rigid_flex_text(
            (SAMPLES / "Multiple_rigid_flex_stackup.txt").read_text(encoding="utf-8")
        )
        master_stackup = deepcopy(zones[0].stackup)
        branch_stackup = deepcopy(zones[4].stackup)
        master_stackup.layers[9] = DielectricLayer(
            dielectric_type="no_flow_prepreg",
            material_id="arlon-51n-no-flow-51n0672",
            selected_freq_ghz=1.0,
        )
        branch_stackup.layers[1] = deepcopy(master_stackup.layers[9])

        master = _EditorStub(master_stackup)
        branch = _EditorStub(branch_stackup)
        harness = _WindowHarness(master, branch)

        master_labels = RigidFlexEditorWindow._rigid_no_flow_row_labels(
            harness,
            master,
        )
        branch_labels = RigidFlexEditorWindow._rigid_no_flow_row_labels(
            harness,
            branch,
        )

        self.assertEqual(master_labels[9], "MasterRigidNoFLowDielectric1")
        self.assertEqual(branch_labels[1], "RigidPart2NoFLowDielectric1")


if __name__ == "__main__":
    unittest.main()
