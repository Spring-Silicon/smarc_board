import tempfile
import unittest
from pathlib import Path

import compare_schematic_identity as compare


class ValueNormalizationTests(unittest.TestCase):
    def test_si_equivalent_values_match(self):
        self.assertEqual(
            compare.normalize_value("0.1uF 25V"),
            compare.normalize_value("100nF 25V"),
        )

    def test_zero_ohm_tolerance_is_equivalent_to_bom_zero(self):
        self.assertTrue(
            compare.values_equivalent(
                compare.normalize_value("0ohm_+-1%"),
                compare.normalize_value("0"),
            )
        )


class ComponentComparisonTests(unittest.TestCase):
    def setUp(self):
        self.local = {
            "PC1.C": {
                "path": "PC1.C",
                "designator": "C1",
                "value": "100nF 25V",
                "mpn": None,
                "manufacturer": None,
                "package": "0603",
                "dnp": False,
                "in_bom": True,
                "on_board": True,
            }
        }
        self.reference = [
            {
                "path": "PC1.C",
                "designator": "C1",
                "value": "0.1uF 25V",
                "mpn": None,
                "manufacturer": None,
                "package": "0603",
                "dnp": False,
                "in_bom": True,
                "on_board": True,
            }
        ]

    def test_equivalent_component_matches(self):
        result = compare.compare_components(self.local, self.reference)
        self.assertEqual(result["differences"], [])

    def test_changed_value_is_detected(self):
        self.local["PC1.C"]["value"] = "220nF 25V"
        result = compare.compare_components(self.local, self.reference)
        self.assertEqual(result["differences"][0]["field"], "value")

    def test_changed_dnp_is_detected(self):
        self.local["PC1.C"]["dnp"] = True
        result = compare.compare_components(self.local, self.reference)
        self.assertEqual(result["differences"][0]["field"], "dnp")

    def test_missing_component_is_detected(self):
        result = compare.compare_components({}, self.reference)
        self.assertEqual(result["missing_from_local"], ["PC1.C"])


class ConnectivityComparisonTests(unittest.TestCase):
    @staticmethod
    def manifest(records):
        return compare.connectivity_manifest(records)

    def test_named_vs_anonymous_preserves_topology_but_fails_name(self):
        local_net = compare.NetValue()
        reference_net = compare.NetValue(name="P_+TEST")
        local = self.manifest(
            [
                compare.ComponentRecord("PR1.R", "R1", "R", {"P1": local_net}),
                compare.ComponentRecord("PU1", "U1", "U", {"EN1": local_net}),
            ]
        )
        reference = self.manifest(
            [
                compare.ComponentRecord("PR1.R", "PR1", "R", {"P1": reference_net}),
                compare.ComponentRecord("PU1", "PU1", "U", {"EN1": reference_net}),
            ]
        )
        result = compare.compare_connectivity(local, reference)
        self.assertEqual(result["topology_missing_from_local"], [])
        self.assertEqual(result["topology_extra_in_local"], [])
        self.assertEqual(len(result["net_name_differences"]), 1)

    def test_moved_pin_changes_topology(self):
        shared = compare.NetValue()
        split_a = compare.NetValue()
        split_b = compare.NetValue()
        reference = self.manifest(
            [
                compare.ComponentRecord("PR1.R", "PR1", "R", {"P1": shared}),
                compare.ComponentRecord("PU1", "PU1", "U", {"EN1": shared}),
            ]
        )
        local = self.manifest(
            [
                compare.ComponentRecord("PR1.R", "R1", "R", {"P1": split_a}),
                compare.ComponentRecord("PU1", "U1", "U", {"EN1": split_b}),
            ]
        )
        result = compare.compare_connectivity(local, reference)
        self.assertTrue(result["topology_missing_from_local"])
        self.assertTrue(result["topology_extra_in_local"])

    def test_mosfet_repeated_pads_coalesce(self):
        drain = compare.NetValue(name="DRAIN")
        manifest = self.manifest(
            [
                compare.ComponentRecord(
                    "PQ1.Q",
                    "PQ1",
                    "Q",
                    {"D_5": drain, "D_6": drain, "D_7": drain, "D_8": drain},
                )
            ]
        )
        self.assertEqual(manifest["endpoints"], {"PQ1.Q:D": "named:DRAIN"})

    def test_nested_module_interfaces_are_flattened(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "nested.zen"
            source.write_text(
                "\n".join(
                    [
                        'P = Net("P")',
                        'N = Net("N")',
                        'Connector = Module("connector.zen")',
                        'Connector(name="J1", BOT=Usb3(D=DiffPair(P=P, N=N)))',
                    ]
                ),
                encoding="utf-8",
            )
            records = compare.evaluate_zen(source)
        self.assertEqual(set(records[0].pins), {"BOT.D.P", "BOT.D.N"})


class KicadParsingTests(unittest.TestCase):
    def test_uuid_and_position_do_not_enter_component_manifest(self):
        before = """(kicad_sch
          (lib_symbols (symbol "diode:R" (property "Value" "10k")))
          (symbol (lib_id "diode:R") (at 1 2 0) (uuid "one")
            (property "Reference" "R1") (property "Value" "10k")
            (property "Path" "PR1.R") (dnp no) (in_bom yes) (on_board yes)))"""
        after = before.replace("(at 1 2 0)", "(at 99 88 90)").replace(
            '(uuid "one")', '(uuid "two")'
        )
        first = compare.parse_sexpr(before)
        second = compare.parse_sexpr(after)
        self.assertNotEqual(first, second)
        # The production extractor deliberately reads no at/uuid fields.
        first_symbol = compare.direct_children(first, "symbol")[0]
        second_symbol = compare.direct_children(second, "symbol")[0]
        self.assertEqual(compare.property_map(first_symbol), compare.property_map(second_symbol))


if __name__ == "__main__":
    unittest.main()
