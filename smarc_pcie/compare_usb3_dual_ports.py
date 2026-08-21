#!/usr/bin/env python3
"""Read-only USB3 sheet audit against SMARC_USB3.0_DUAL_PORTS v0.1.0."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import compare_schematic_identity as core
import compare_usb3 as usb


EXPECTED_COMMIT = "7e0358dc08deb6769dbafc4cd6309c77c9b47f1a"
DEFAULT_PACKAGE = (
    "code.diode.computer/spring/registry/"
    "smarc_carrier/SMARC_USB3.0_DUAL_PORTS"
)

# These local references are spelling/numbering deviations from registry paths.
STRICT_CONNECTIVITY_ALIASES = {
    "C612": "C610.R",
    "ES603": "ESD603.D",
    "ES604": "ESD604.D",
    "ES605": "ESD605.D",
}

# The local USB3 TX jumpers use C612 on SSTXP and C609 on SSTXN.  The registry
# calls those same electrical positions C609 and C610, respectively.  This map
# is used only for a secondary functional-topology check.
FUNCTIONAL_CONNECTIVITY_ALIASES = {
    **STRICT_CONNECTIVITY_ALIASES,
    "C609": "C610.R",
    "C612": "C609.R",
}

# Packages stated by loaded registry component documentation but omitted from
# the flattened pcb-bom package field.
DOCUMENTED_PACKAGES = {
    **{f"L{number}.FL": "0805" for number in range(601, 607)},
    "ESD601.D": "0603",
    "ESD602.D": "0603",
    "ESD603.D": "1004-DFN",
    "ESD604.D": "1004-DFN",
    "ESD605.D": "1004-DFN",
    "CN601.J": "692141030100-THT",
}


def parse_local_components(
    schematic: Path, paths_by_reference: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Parse parts, retaining extras and merging repeated multi-unit symbols."""
    root = core.parse_sexpr(schematic.read_text(encoding="utf-8"))
    if not root or root[0] != "kicad_sch":
        raise core.ComparisonError(f"Not a KiCad schematic: {schematic}")

    library_defaults: dict[str, dict[str, str]] = {}
    lib_symbols = core.first_child(root, "lib_symbols")
    if lib_symbols:
        for symbol in core.direct_children(lib_symbols, "symbol"):
            if len(symbol) >= 2 and isinstance(symbol[1], str):
                library_defaults[symbol[1]] = core.property_map(symbol)

    components: dict[str, dict[str, Any]] = {}
    for symbol in core.direct_children(root, "symbol"):
        placed = core.property_map(symbol)
        reference = placed.get("Reference")
        if not reference or reference.startswith("#"):
            continue
        component_path = (
            placed.get("Path")
            or paths_by_reference.get(reference)
            or f"@reference:{reference}"
        )
        lib_id_node = core.first_child(symbol, "lib_id")
        lib_id = lib_id_node[1] if lib_id_node and len(lib_id_node) >= 2 else ""
        defaults = library_defaults.get(lib_id, {})

        def first_value(*names: str) -> str | None:
            for fields in (placed, defaults):
                for name in names:
                    value = fields.get(name)
                    if value:
                        return value
            return None

        record = {
            "path": component_path,
            "designator": reference,
            "lib_id": lib_id,
            "value": first_value("Value"),
            "mpn": first_value("Manufacturer_Part_Number", "MPN"),
            "manufacturer": first_value("Manufacturer_Name", "Manufacturer"),
            "package": first_value("Footprint"),
            "description": placed.get("Description") or "",
            "explicit_path": placed.get("Path"),
            "dnp": core.yes_no(symbol, "dnp", False),
            "in_bom": core.yes_no(symbol, "in_bom", True),
            "on_board": core.yes_no(symbol, "on_board", True),
            "unit_count": 1,
        }
        existing = components.get(component_path)
        if existing is None:
            components[component_path] = record
            continue
        if existing["designator"] != reference:
            raise core.ComparisonError(
                f"Duplicate component path {component_path}: "
                f"{existing['designator']} and {reference}"
            )
        for field in ("value", "mpn", "package", "dnp", "in_bom", "on_board"):
            if existing[field] != record[field]:
                raise core.ComparisonError(
                    f"Multi-unit {reference} disagrees on {field}: "
                    f"{existing[field]!r} vs {record[field]!r}"
                )
        existing["unit_count"] += 1
    return components


def audit_reference_designators(
    local: dict[str, dict[str, Any]], reference_bom: list[dict[str, Any]]
) -> dict[str, Any]:
    differences = []
    expected_paths = {item["path"] for item in reference_bom}
    for component_path in sorted(expected_paths & set(local)):
        expected = usb.source_reference(component_path)
        actual = local[component_path]["designator"]
        if actual != expected:
            differences.append(
                {
                    "path": component_path,
                    "expected_reference": expected,
                    "actual_reference": actual,
                }
            )
    return {
        "identity_source": (
            "KiCad Reference, because USB3.kicad_sch has no placed Path properties"
        ),
        "checked_count": len(expected_paths & set(local)),
        "differences": differences,
    }


def audit_packages(
    local: dict[str, dict[str, Any]], reference_bom: list[dict[str, Any]]
) -> dict[str, Any]:
    reference_by_path = {item["path"]: item for item in reference_bom}
    formal_differences = []
    documented_differences = []
    for component_path in sorted(set(local) & set(reference_by_path)):
        actual = local[component_path].get("package") or ""
        formal = reference_by_path[component_path].get("package")
        documented = DOCUMENTED_PACKAGES.get(component_path)
        if formal:
            normalized = usb.normalized_footprint_size(actual)
            if normalized != formal:
                formal_differences.append(
                    {
                        "path": component_path,
                        "expected_registry_package": formal,
                        "actual_footprint": actual,
                        "actual_normalized_package": normalized,
                    }
                )
        elif documented:
            normalized = usb.normalized_footprint_size(actual)
            if normalized != documented:
                documented_differences.append(
                    {
                        "path": component_path,
                        "expected_documented_package": documented,
                        "actual_footprint": actual,
                        "actual_normalized_package": normalized,
                    }
                )
    return {
        "formal_bom_package_differences": formal_differences,
        "documented_component_package_differences": documented_differences,
        "description_convention": (
            "The registry establishes BOM descriptions, audited separately, but "
            "does not establish project-specific footprint codes such as R0402"
        ),
    }


def audit_descriptions(
    local: dict[str, dict[str, Any]], reference_bom: list[dict[str, Any]]
) -> dict[str, Any]:
    reference_by_path = {item["path"]: item for item in reference_bom}
    differences = []
    for component_path in sorted(set(local) & set(reference_by_path)):
        expected = reference_by_path[component_path].get("description")
        actual = local[component_path].get("description") or ""
        if expected is not None and actual != expected:
            differences.append(
                {
                    "path": component_path,
                    "expected_registry_description": expected,
                    "actual_description": actual,
                }
            )
    return {
        "checked_count": len(set(local) & set(reference_by_path)),
        "differences": differences,
    }


def pin_name_score(
    actual: tuple[str | None, str | None],
    expected: tuple[str | None, str | None],
) -> int:
    return sum(
        left == right
        for left, right in zip(actual, expected)
        if left is not None or right is not None
    )


def remap_pins(
    record: core.ComponentRecord, mapping: dict[str, str]
) -> list[dict[str, Any]]:
    remapped: dict[str, core.NetValue] = {}
    conflicts = []
    for pin, net in record.pins.items():
        target = mapping.get(pin, pin)
        prior = remapped.get(target)
        if prior is not None and prior is not net:
            first = core.normalize_named_net(prior.name)
            second = core.normalize_named_net(net.name)
            if first != second:
                conflicts.append(
                    {
                        "path": record.path,
                        "pin": target,
                        "first": first or "anonymous",
                        "second": second or "anonymous",
                    }
                )
        remapped[target] = net
    record.pins = remapped
    return conflicts


def align_local_interfaces(
    local_records: list[core.ComponentRecord],
    reference_records: list[core.ComponentRecord],
) -> dict[str, Any]:
    reference_by_path = {record.path: record for record in reference_records}
    changes = []
    coalescing_conflicts = []
    connector_mapping = {
        "D1_NEG": "BOT.D.N",
        "D1_POS": "BOT.D.P",
        "SSRX1_NEG": "BOT.SSRX.N",
        "SSRX1_POS": "BOT.SSRX.P",
        "SSTX1_NEG": "BOT.SSTX.N",
        "SSTX1_POS": "BOT.SSTX.P",
        "D2_NEG": "TOP.D.N",
        "D2_POS": "TOP.D.P",
        "SSRX2_NEG": "TOP.SSRX.N",
        "SSRX2_POS": "TOP.SSRX.P",
        "SSTX2_NEG": "TOP.SSTX.N",
        "SSTX2_POS": "TOP.SSTX.P",
        "VBUS1": "VBUS_BOT",
        "VBUS2": "VBUS_TOP",
        "GND1": "GND",
        "DRAIN1": "GND",
        "GND2": "GND",
        "DRAIN2": "GND",
        "SHIELD": "SHIELD",
    }
    data_esd_mapping = {
        "D1_POS": "CH1_A",
        "NC_10": "CH1_B",
        "D1_NEG": "CH2_A",
        "NC_9": "CH2_B",
        "D2_POS": "CH3_A",
        "NC_7": "CH3_B",
        "D2_NEG": "CH4_A",
        "NC_6": "CH4_B",
        "GND_3": "GND",
        "GND_8": "GND",
    }
    receive_esd_mapping = {
        "D1_NEG": "CH1_A",
        "NC_9": "CH1_B",
        "D1_POS": "CH2_A",
        "NC_10": "CH2_B",
        "D2_NEG": "CH3_A",
        "NC_6": "CH3_B",
        "D2_POS": "CH4_A",
        "NC_7": "CH4_B",
        "GND_3": "GND",
        "GND_8": "GND",
    }
    choke_mapping = {
        "P1": "IN_A",
        "P2": "OUT_A",
        "P3": "OUT_B",
        "P4": "IN_B",
    }
    swapped_choke_mapping = {
        "P1": "IN_B",
        "P2": "OUT_B",
        "P3": "OUT_A",
        "P4": "IN_A",
    }
    for local in local_records:
        reference = reference_by_path.get(local.path)
        if reference is None:
            continue
        mapping: dict[str, str] | None = None
        kind = ""
        if local.path == "CN601.J":
            mapping, kind = connector_mapping, "connector-interface"
        elif local.path in {"ESD603.D", "ESD604.D"}:
            mapping, kind = data_esd_mapping, "data-esd-interface"
        elif local.path == "ESD605.D":
            mapping, kind = receive_esd_mapping, "data-esd-channel-permutation"
        elif local.path in {"ESD601.D", "ESD602.D"}:
            mapping, kind = {"A1": "P2", "A2": "P1"}, "vbus-esd-interface"
        elif local.path.startswith("L") and local.path.endswith(".FL"):
            def mapping_score(candidate: dict[str, str]) -> int:
                return sum(
                    core.normalize_named_net(local.pins[source_pin].name)
                    == core.normalize_named_net(reference.pins[target_pin].name)
                    for source_pin, target_pin in candidate.items()
                    if source_pin in local.pins and target_pin in reference.pins
                )

            mapping = max(
                (choke_mapping, swapped_choke_mapping), key=mapping_score
            )
            kind = (
                "common-mode-choke-interface"
                if mapping is choke_mapping
                else "common-mode-choke-winding-permutation"
            )
        if mapping is not None:
            coalescing_conflicts.extend(remap_pins(local, mapping))
            changes.append({"path": local.path, "kind": kind, "mapping": mapping})
            continue

        if not {"P1", "P2"}.issubset(local.pins) or not {
            "P1",
            "P2",
        }.issubset(reference.pins):
            continue
        local_names = tuple(
            core.normalize_named_net(local.pins[pin].name) for pin in ("P1", "P2")
        )
        reference_names = tuple(
            core.normalize_named_net(reference.pins[pin].name)
            for pin in ("P1", "P2")
        )
        if pin_name_score(local_names[::-1], reference_names) > pin_name_score(
            local_names, reference_names
        ):
            local.pins["P1"], local.pins["P2"] = local.pins["P2"], local.pins["P1"]
            changes.append(
                {
                    "path": local.path,
                    "kind": "unpolarized-passive-swap",
                    "mapping": {"P1": "P2", "P2": "P1"},
                }
            )
    return {"changes": changes, "coalescing_conflicts": coalescing_conflicts}


def connectivity_path_map(
    reference_bom: list[dict[str, Any]], aliases: dict[str, str]
) -> dict[str, str]:
    result = {
        usb.source_reference(item["path"]): item["path"] for item in reference_bom
    }
    result.update(aliases)
    return result


def topology_difference_count(result: dict[str, Any]) -> int:
    return sum(
        len(result[key])
        for key in (
            "missing_from_local",
            "extra_in_local",
            "topology_missing_from_local",
            "topology_extra_in_local",
            "local_conflicts",
            "reference_conflicts",
        )
    )


def compare(args: argparse.Namespace) -> dict[str, Any]:
    schematic = args.schematic.resolve()
    reference_zen = args.reference_zen.resolve()
    if not schematic.is_file():
        raise core.ComparisonError(f"Missing schematic: {schematic}")
    if not reference_zen.is_file():
        raise core.ComparisonError(f"Missing reference source: {reference_zen}")

    original_hash = usb.sha256(schematic)
    repo_root = core.repository_root(reference_zen)
    revision = core.git_revision(repo_root)
    if revision != args.expected_commit:
        raise core.ComparisonError(
            f"Reference checkout is {revision}, expected {args.expected_commit}"
        )
    validation = core.validate_reference(args.pcb, reference_zen, repo_root)
    reference_bom, bom_metadata = core.resolve_reference_bom(
        args.pcb, reference_zen, repo_root
    )
    paths_by_reference = usb.path_map(reference_bom)
    local_components = parse_local_components(schematic, paths_by_reference)
    component_result = usb.strict_component_comparison(
        local_components, reference_bom
    )
    designator_result = audit_reference_designators(local_components, reference_bom)
    package_result = audit_packages(local_components, reference_bom)
    description_result = audit_descriptions(local_components, reference_bom)

    reference_records = core.evaluate_zen(reference_zen, path_map=paths_by_reference)
    reference_connectivity = core.connectivity_manifest(reference_records)

    with tempfile.TemporaryDirectory(prefix="usb3-dual-identity-") as temp_name:
        temp_dir = Path(temp_name)
        import_source = temp_dir / schematic.name
        flatten_metadata = usb.flattened_import_copy(schematic, import_source)
        import_output = temp_dir / "imported"
        import_metadata = core.import_schematic(args.pcb, import_source, import_output)
        imported_zen = Path(import_metadata["generated_zen"])
        strict_records = core.evaluate_zen(
            imported_zen,
            path_map=connectivity_path_map(
                reference_bom, STRICT_CONNECTIVITY_ALIASES
            ),
        )
        functional_records = core.evaluate_zen(
            imported_zen,
            path_map=connectivity_path_map(
                reference_bom, FUNCTIONAL_CONNECTIVITY_ALIASES
            ),
        )

    strict_alignment = align_local_interfaces(strict_records, reference_records)
    functional_alignment = align_local_interfaces(
        functional_records, reference_records
    )
    strict_connectivity = core.compare_connectivity(
        core.connectivity_manifest(strict_records), reference_connectivity
    )
    functional_connectivity = core.compare_connectivity(
        core.connectivity_manifest(functional_records), reference_connectivity
    )

    if usb.sha256(schematic) != original_hash:
        raise core.ComparisonError(f"Source schematic changed during audit: {schematic}")

    component_difference_count = sum(
        len(component_result[key])
        for key in ("missing_from_local", "extra_in_local", "differences")
    )
    formal_package_difference_count = len(
        package_result["formal_bom_package_differences"]
    )
    documented_package_difference_count = len(
        package_result["documented_component_package_differences"]
    )
    description_difference_count = len(description_result["differences"])
    strict_topology_count = topology_difference_count(strict_connectivity)
    net_name_difference_count = len(functional_connectivity["net_name_differences"])
    hard_difference_count = (
        component_difference_count
        + len(designator_result["differences"])
        + formal_package_difference_count
        + documented_package_difference_count
        + description_difference_count
        + strict_topology_count
        + net_name_difference_count
    )
    identical = hard_difference_count == 0

    return {
        "schema_version": 1,
        "status": "identical" if identical else "different",
        "identical": identical,
        "baseline": {
            "package": DEFAULT_PACKAGE,
            "version": "0.1.0",
            "commit": revision,
            "entrypoint": str(reference_zen.relative_to(repo_root)),
            "pcb_version": core.command_version(args.pcb),
        },
        "target": str(schematic),
        "target_sha256": original_hash,
        "source_file_unchanged": True,
        "summary": {
            "hard_difference_count": hard_difference_count,
            "component_difference_count": component_difference_count,
            "reference_designator_difference_count": len(
                designator_result["differences"]
            ),
            "formal_package_difference_count": formal_package_difference_count,
            "documented_package_difference_count": (
                documented_package_difference_count
            ),
            "description_difference_count": description_difference_count,
            "strict_topology_difference_count": strict_topology_count,
            "functional_topology_identical": (
                topology_difference_count(functional_connectivity) == 0
            ),
            "net_name_difference_count": net_name_difference_count,
            "all_net_names_present": net_name_difference_count == 0,
        },
        "components": component_result,
        "reference_designators": designator_result,
        "packages_and_descriptions": package_result,
        "descriptions": description_result,
        "connectivity": strict_connectivity,
        "functional_connectivity_after_designator_aliases": functional_connectivity,
        "normalizations": [
            "Registry leaf paths map to their sheet-instance reference prefixes",
            "Repeated CN601 units are merged into one component",
            "Connector, choke, and ESD imported pin names map to registry interfaces",
            "Repeated physical ground pins are coalesced to one module-level GND endpoint",
            "Unpolarized two-pin passive orientation is ignored",
            "One leading KiCad root-scope slash is removed from named nets",
            "A secondary topology result aliases C609/C612 by electrical role",
            "UUIDs, placement, graphics, and property ordering are ignored",
        ],
        "commands": {
            "reference_validation": validation,
            "reference_bom": bom_metadata,
            "temporary_flattening": flatten_metadata,
            "local_import": import_metadata,
            "strict_interface_normalizations": strict_alignment,
            "functional_interface_normalizations": functional_alignment,
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schematic", type=Path)
    parser.add_argument("reference_zen", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pcb", default="pcb")
    parser.add_argument("--expected-commit", default=EXPECTED_COMMIT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        report = compare(args)
    except (core.ComparisonError, OSError, SyntaxError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
        print(f"{report['status']}: wrote {args.output}")
    else:
        print(rendered, end="")
    return 0 if report["identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
