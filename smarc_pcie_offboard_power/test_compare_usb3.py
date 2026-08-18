from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import compare_schematic_identity as core
import compare_usb3 as usb3


class Usb3ComparisonTests(unittest.TestCase):
    def test_source_reference_uses_registry_path_prefix(self) -> None:
        self.assertEqual(usb3.source_reference("C501.C"), "C501")
        self.assertEqual(
            usb3.source_reference("TC501.6TPE150MAZB"), "TC501"
        )
        self.assertEqual(usb3.source_reference("U501"), "U501")

    def test_footprint_size_normalization(self) -> None:
        self.assertEqual(usb3.normalized_footprint_size("R0603"), "0603")
        self.assertEqual(usb3.normalized_footprint_size("C060304"), "0603")
        self.assertIsNone(usb3.normalized_footprint_size(""))

    def test_description_and_package_audit_does_not_guess_0402_code(self) -> None:
        local = {
            "C501.C": {
                "package": "",
                "description": "",
            },
            "R501.R": {
                "package": "R0603",
                "description": "",
            },
        }
        reference = [
            {
                "path": "C501.C",
                "component_type": "Capacitor",
                "package": "0402",
            },
            {
                "path": "R501.R",
                "component_type": "Resistor",
                "package": "0402",
            },
        ]
        result = usb3.audit_description_footprints(local, reference)
        self.assertEqual(len(result["package_differences"]), 2)
        self.assertEqual(
            result["description_differences"],
            [
                {
                    "path": "R501.R",
                    "expected_description": "R0603",
                    "actual_description": "",
                    "actual_footprint": "R0603",
                }
            ],
        )
        self.assertEqual(result["unverified"][0]["path"], "C501.C")

    def test_passive_orientation_uses_partial_named_net_match(self) -> None:
        local_ground = core.NetValue("GND")
        local_ilim = core.NetValue("USB2_B_ILIM")
        expected_ground = core.NetValue("GND")
        expected_ilim = core.NetValue("2USB2_B_ILIM")
        local = [
            core.ComponentRecord(
                path="R504.R",
                designator="R504",
                module="Resistor",
                pins={"P1": local_ground, "P2": local_ilim},
            )
        ]
        reference = [
            core.ComponentRecord(
                path="R504.R",
                designator="R504",
                module="Resistor",
                pins={"P1": expected_ilim, "P2": expected_ground},
            )
        ]
        changes = usb3.align_local_interfaces(local, reference)
        self.assertIs(local[0].pins["P1"], local_ilim)
        self.assertIs(local[0].pins["P2"], local_ground)
        self.assertEqual(changes[0]["kind"], "unpolarized-passive-swap")

    def test_restricted_evaluator_accepts_layout_constructor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "layout.zen"
            source.write_text(
                'Layout(name="Example", path="layout/Example")\n',
                encoding="utf-8",
            )
            self.assertEqual(core.evaluate_zen(source), [])


if __name__ == "__main__":
    unittest.main()
