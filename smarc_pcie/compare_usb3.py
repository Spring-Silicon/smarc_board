#!/usr/bin/env python3
"""Strict read-only USB3.kicad_sch audit against Usb3Ocp v0.1.0."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import compare_schematic_identity as core


EXPECTED_COMMIT = "6c8133d9bda9c254cb825103bad53e1ab87fc85e"
DEFAULT_PACKAGE = "code.diode.computer/spring/registry/modules/Usb3Ocp"
PASSIVE_TYPES = {"Capacitor", "Resistor"}
FOOTPRINT_SIZE = re.compile(r"^[CR](\d{4})")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_reference(component_path: str) -> str:
    """Return the source instance name represented by a registry BOM path."""
    return component_path.rsplit(".", 1)[0] if "." in component_path else component_path


def path_map(reference_bom: list[dict[str, Any]]) -> dict[str, str]:
    result = {source_reference(item["path"]): item["path"] for item in reference_bom}
    if len(result) != len(reference_bom):
        raise core.ComparisonError("Registry paths do not have unique source references")
    return result


def parse_local_components(
    schematic: Path, paths_by_reference: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Parse placed parts and accept both KiCad MPN field spellings."""
    components = core.parse_kicad_components(
        schematic, path_by_reference=paths_by_reference
    )
    root = core.parse_sexpr(schematic.read_text(encoding="utf-8"))
    library_defaults: dict[str, dict[str, str]] = {}
    lib_symbols = core.first_child(root, "lib_symbols")
    if lib_symbols:
        for symbol in core.direct_children(lib_symbols, "symbol"):
            if len(symbol) >= 2 and isinstance(symbol[1], str):
                library_defaults[symbol[1]] = core.property_map(symbol)

    by_reference = {item["designator"]: item for item in components.values()}
    for symbol in core.direct_children(root, "symbol"):
        placed = core.property_map(symbol)
        reference = placed.get("Reference")
        local = by_reference.get(reference or "")
        if local is None:
            continue
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

        local["mpn"] = first_value("Manufacturer_Part_Number", "MPN")
        local["manufacturer"] = first_value("Manufacturer_Name", "Manufacturer")
    return components


def strict_component_comparison(
    local: dict[str, dict[str, Any]], reference_bom: list[dict[str, Any]]
) -> dict[str, Any]:
    result = core.compare_components(local, reference_bom)
    reference_by_path = {item["path"]: item for item in reference_bom}
    existing = {(item["path"], item["field"]) for item in result["differences"]}
    for component_path in sorted(set(local) & set(reference_by_path)):
        actual = local[component_path]
        expected = reference_by_path[component_path]
        if expected.get("value") is None or (component_path, "value") in existing:
            continue
        actual_value = core.normalize_value(actual.get("value"))
        expected_value = core.normalize_value(expected.get("value"))
        if not core.values_equivalent(actual_value, expected_value):
            result["differences"].append(
                {
                    "path": component_path,
                    "field": "value",
                    "expected": expected_value,
                    "actual": actual_value,
                }
            )
    result["differences"].sort(key=lambda item: (item["path"], item["field"]))
    return result


def audit_reference_designators(
    local: dict[str, dict[str, Any]], reference_bom: list[dict[str, Any]]
) -> dict[str, Any]:
    differences = []
    expected_paths = {item["path"] for item in reference_bom}
    for component_path in sorted(expected_paths & set(local)):
        expected = source_reference(component_path)
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


def normalized_footprint_size(value: str | None) -> str | None:
    if not value:
        return None
    match = FOOTPRINT_SIZE.match(value)
    return match.group(1) if match else value


def audit_description_footprints(
    local: dict[str, dict[str, Any]], reference_bom: list[dict[str, Any]]
) -> dict[str, Any]:
    """Check formal package alignment and literal Description/Footprint agreement.

    Usb3Ocp specifies generic passive package 0402 but does not define the
    project's encoded capacitor Description spelling (for example C060304).
    Such a spelling is therefore reported as unverified rather than guessed.
    """
    reference_by_path = {item["path"]: item for item in reference_bom}
    package_differences = []
    description_differences = []
    unverified = []
    for component_path in sorted(set(local) & set(reference_by_path)):
        actual = local[component_path]
        expected = reference_by_path[component_path]
        component_type = expected.get("component_type")
        registry_package = expected.get("package")
        if component_type not in PASSIVE_TYPES or not registry_package:
            unverified.append(
                {
                    "path": component_path,
                    "reason": "Registry BOM has no generic passive package-to-Description assertion",
                }
            )
            continue

        actual_footprint = actual.get("package") or ""
        actual_description = actual.get("description") or ""
        actual_size = normalized_footprint_size(actual_footprint)
        if actual_size != registry_package:
            package_differences.append(
                {
                    "path": component_path,
                    "component_type": component_type,
                    "expected_registry_package": registry_package,
                    "actual_footprint": actual_footprint,
                    "actual_normalized_package": actual_size,
                }
            )

        if actual_footprint:
            if actual_description != actual_footprint:
                description_differences.append(
                    {
                        "path": component_path,
                        "expected_description": actual_footprint,
                        "actual_description": actual_description,
                        "actual_footprint": actual_footprint,
                    }
                )
        else:
            unverified.append(
                {
                    "path": component_path,
                    "component_type": component_type,
                    "registry_package": registry_package,
                    "actual_description": actual_description,
                    "reason": (
                        "Footprint is empty and Usb3Ocp does not establish the local "
                        "0402 Description-code spelling"
                    ),
                }
            )
    return {
        "package_differences": package_differences,
        "description_differences": description_differences,
        "unverified": unverified,
    }


def flattened_import_copy(source: Path, destination: Path) -> dict[str, Any]:
    text = source.read_text(encoding="utf-8")
    root_uuid_match = re.search(
        r'\(kicad_sch\s+.*?\(uuid\s+"([^"]+)"\)', text, re.DOTALL
    )
    if root_uuid_match is None:
        raise core.ComparisonError("Cannot find USB3 root UUID")
    root_uuid = root_uuid_match.group(1)
    text, project_count = re.subn(
        r'(\(project\s+")[^"]+("\s*)', r"\g<1>USB3\g<2>", text
    )
    text, path_count = re.subn(
        r'(\(path\s+")/[^"]+("\s*)', r"\g<1>/" + root_uuid + r"\g<2>", text
    )
    if project_count == 0 or path_count == 0:
        raise core.ComparisonError(
            f"Could not flatten hierarchical instances: projects={project_count}, "
            f"paths={path_count}"
        )
    destination.write_text(text, encoding="utf-8")
    return {
        "temporary_file": str(destination),
        "flattened_root_uuid": root_uuid,
        "project_replacements": project_count,
        "instance_path_replacements": path_count,
    }


def align_local_interfaces(
    local_records: list[core.ComponentRecord],
    reference_records: list[core.ComponentRecord],
) -> list[dict[str, Any]]:
    reference_by_path = {record.path: record for record in reference_records}
    changes = []
    for local in local_records:
        reference = reference_by_path.get(local.path)
        if reference is None:
            continue
        if local.path == "TC501.6TPE150MAZB":
            local.pins = {
                ("POS" if pin == "P1" else "NEG" if pin == "P2" else pin): net
                for pin, net in local.pins.items()
            }
            changes.append(
                {
                    "path": local.path,
                    "kind": "polarized-interface",
                    "mapping": {"P1": "POS", "P2": "NEG"},
                }
            )
            continue
        if local.path == "U501":
            mapping = {"N_FAULT": "~{FAULT}", "PAD": "EP"}
            local.pins = {mapping.get(pin, pin): net for pin, net in local.pins.items()}
            changes.append(
                {"path": local.path, "kind": "symbol-pin-alias", "mapping": mapping}
            )
            continue
        if not {"P1", "P2"}.issubset(local.pins) or not {
            "P1",
            "P2",
        }.issubset(reference.pins):
            continue
        local_names = (
            core.normalize_named_net(local.pins["P1"].name),
            core.normalize_named_net(local.pins["P2"].name),
        )
        reference_names = (
            core.normalize_named_net(reference.pins["P1"].name),
            core.normalize_named_net(reference.pins["P2"].name),
        )
        direct_score = sum(
            actual == expected
            for actual, expected in zip(local_names, reference_names)
            if actual is not None or expected is not None
        )
        reverse_score = sum(
            actual == expected
            for actual, expected in zip(local_names[::-1], reference_names)
            if actual is not None or expected is not None
        )
        if reverse_score > direct_score:
            local.pins["P1"], local.pins["P2"] = local.pins["P2"], local.pins["P1"]
            changes.append(
                {
                    "path": local.path,
                    "kind": "unpolarized-passive-swap",
                    "mapping": {"P1": "P2", "P2": "P1"},
                }
            )
    return changes


def compare(args: argparse.Namespace) -> dict[str, Any]:
    schematic = args.schematic.resolve()
    reference_zen = args.reference_zen.resolve()
    if not schematic.is_file():
        raise core.ComparisonError(f"Missing schematic: {schematic}")
    if not reference_zen.is_file():
        raise core.ComparisonError(f"Missing reference source: {reference_zen}")

    original_hash = sha256(schematic)
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
    paths_by_reference = path_map(reference_bom)
    local_components = parse_local_components(schematic, paths_by_reference)
    component_result = strict_component_comparison(local_components, reference_bom)
    designator_result = audit_reference_designators(local_components, reference_bom)
    description_result = audit_description_footprints(local_components, reference_bom)

    reference_records = core.evaluate_zen(reference_zen, path_map=paths_by_reference)
    reference_connectivity = core.connectivity_manifest(reference_records)

    with tempfile.TemporaryDirectory(prefix="usb3-identity-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        import_source = temp_dir / schematic.name
        flatten_metadata = flattened_import_copy(schematic, import_source)
        import_output = temp_dir / "imported"
        import_metadata = core.import_schematic(args.pcb, import_source, import_output)
        imported_zen = Path(import_metadata["generated_zen"])
        local_records = core.evaluate_zen(
            imported_zen,
            path_map={source_reference(item["path"]): item["path"] for item in reference_bom},
        )

    interface_normalizations = align_local_interfaces(local_records, reference_records)
    local_connectivity = core.connectivity_manifest(local_records)
    connectivity_result = core.compare_connectivity(
        local_connectivity, reference_connectivity
    )

    if sha256(schematic) != original_hash:
        raise core.ComparisonError(f"Source schematic changed during audit: {schematic}")

    component_difference_count = (
        len(component_result["missing_from_local"])
        + len(component_result["extra_in_local"])
        + len(component_result["differences"])
    )
    description_footprint_difference_count = (
        len(description_result["package_differences"])
        + len(description_result["description_differences"])
    )
    topology_difference_count = (
        len(connectivity_result["missing_from_local"])
        + len(connectivity_result["extra_in_local"])
        + len(connectivity_result["topology_missing_from_local"])
        + len(connectivity_result["topology_extra_in_local"])
        + len(connectivity_result["local_conflicts"])
        + len(connectivity_result["reference_conflicts"])
    )
    net_name_difference_count = len(connectivity_result["net_name_differences"])
    hard_difference_count = (
        component_difference_count
        + len(designator_result["differences"])
        + description_footprint_difference_count
        + topology_difference_count
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
            "bom_identical": component_difference_count == 0,
            "reference_designators_identical": not designator_result["differences"],
            "description_footprints_identical": (
                description_footprint_difference_count == 0
            ),
            "electrical_topology_identical": topology_difference_count == 0,
            "all_net_names_present": net_name_difference_count == 0,
            "component_difference_count": component_difference_count,
            "reference_designator_difference_count": len(
                designator_result["differences"]
            ),
            "description_footprint_difference_count": (
                description_footprint_difference_count
            ),
            "topology_difference_count": topology_difference_count,
            "net_name_difference_count": net_name_difference_count,
        },
        "components": component_result,
        "reference_designators": designator_result,
        "description_footprints": description_result,
        "connectivity": connectivity_result,
        "normalizations": [
            "Registry leaf paths such as C501.C map to source reference C501",
            "TC501.6TPE150MAZB maps to source reference TC501",
            "U501 N_FAULT/PAD map to registry ~{FAULT}/EP",
            "TC501 P1/P2 map to registry POS/NEG",
            "Unpolarized two-pin passive orientation is ignored",
            "One leading KiCad root-scope slash is removed from named nets",
            "SI-equivalent values and source underscore/ohm/tolerance spellings are normalized",
            "UUIDs, ordering, placement, and graphics are ignored",
            "No 0402 Description-code spelling is guessed without a registry/source assertion",
        ],
        "commands": {
            "reference_validation": validation,
            "reference_bom": bom_metadata,
            "temporary_flattening": flatten_metadata,
            "local_import": import_metadata,
            "interface_normalizations": interface_normalizations,
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
