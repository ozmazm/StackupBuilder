from __future__ import annotations

import unittest

from stackup_editor.exporter import stackup_import_mode_warning


class StackupImportModeGuardTests(unittest.TestCase):
    def test_rigid_file_is_accepted_only_by_rigid_editor(self) -> None:
        content = "PCB Stackup Export\nL1\nDielectric 1\nL2"

        self.assertIsNone(stackup_import_mode_warning(content, rigid_flex_mode=False))
        self.assertEqual(
            stackup_import_mode_warning(content, rigid_flex_mode=True),
            "Selected stackup is Rigid, not suitable for Rigid Flex stackup.",
        )

    def test_rigid_flex_file_is_accepted_only_by_rigid_flex_editor(self) -> None:
        content = "PCB RigidFlex Stackup Export\nTYPE=FLEX\nFlexPart1FlexCore"

        self.assertIsNone(stackup_import_mode_warning(content, rigid_flex_mode=True))
        self.assertEqual(
            stackup_import_mode_warning(content, rigid_flex_mode=False),
            "Selected stackup is Rigid Flex, not suitable for Rigid stackup.",
        )

    def test_flex_detection_is_case_insensitive_and_matches_compound_names(self) -> None:
        self.assertIsNotNone(
            stackup_import_mode_warning("layer_name=FLEXPART2FLEXCORE", rigid_flex_mode=False)
        )


if __name__ == "__main__":
    unittest.main()
