import tempfile
import unittest
from pathlib import Path

import compare_power_supply as power
import compare_schematic_identity as core


class SourceReferenceTests(unittest.TestCase):
    def test_registry_leaf_is_removed(self):
        self.assertEqual(power.source_reference("PC1.C"), "PC1")
        self.assertEqual(power.source_reference("PR12.R"), "PR12")
        self.assertEqual(power.source_reference("PU1"), "PU1")


class DescriptionAuditTests(unittest.TestCase):
    def test_missing_source_footprint_description_is_reported(self):
        local = {"PC1.C": {"description": "", "package": ""}}
        reference = [
            {
                "path": "PC1.C",
                "component_type": "Capacitor",
                "package": "0603",
            }
        ]
        result = power.audit_description_footprints(local, reference)
        self.assertEqual(result["differences"][0]["expected_description"], "C060304")


class InterfaceAlignmentTests(unittest.TestCase):
    def test_unpolarized_passive_is_aligned_by_named_nets(self):
        net_a = core.NetValue(name="A")
        net_b = core.NetValue(name="B")
        local = [
            core.ComponentRecord("PR1.R", "R1", "R", {"P1": net_b, "P2": net_a})
        ]
        reference = [
            core.ComponentRecord("PR1.R", "PR1", "R", {"P1": net_a, "P2": net_b})
        ]
        changes = power.align_local_interfaces(local, reference)
        self.assertEqual(len(changes), 1)
        self.assertIs(local[0].pins["P1"], net_a)
        self.assertIs(local[0].pins["P2"], net_b)

    def test_polymer_cap_pin_names_are_mapped_to_polarity(self):
        positive = core.NetValue(name="VOUT")
        negative = core.NetValue(name="GND")
        local = [
            core.ComponentRecord(
                "PTC21.C", "C43", "C", {"P1": positive, "P2": negative}
            )
        ]
        reference = [
            core.ComponentRecord(
                "PTC21.C", "PTC21", "C", {"POS": positive, "NEG": negative}
            )
        ]
        power.align_local_interfaces(local, reference)
        self.assertEqual(set(local[0].pins), {"POS", "NEG"})


class TemporaryImportCopyTests(unittest.TestCase):
    def test_copy_renames_and_flattens_without_editing_source(self):
        source_text = """(kicad_sch
          (uuid "root-uuid")
          (symbol
            (property "Reference" "PC1")
            (instances
              (project "parent"
                (path "/parent/sheet" (reference "PC1") (unit 1))))))"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.kicad_sch"
            destination = root / "copy.kicad_sch"
            source.write_text(source_text)
            original = power.sha256(source)
            metadata = power.renamed_import_copy(source, destination, {"PC1": "C1"})
            self.assertEqual(power.sha256(source), original)
            copied = destination.read_text()
            self.assertIn('(property "Reference" "C1")', copied)
            self.assertIn('(project "Power_Supply"', copied)
            self.assertIn('(path "/root-uuid"', copied)
            self.assertEqual(metadata["instance_path_replacements"], 1)


if __name__ == "__main__":
    unittest.main()
