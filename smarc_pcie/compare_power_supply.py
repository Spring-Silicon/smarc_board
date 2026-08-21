#!/usr/bin/env python3
"""Strict Power_Supply.kicad_sch audit against TPS51225B v0.1.0."""

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


EXPECTED_COMMIT = "8be1328adb24ee6556a76847c1f46d91a19aa0f7"
PATH_LEAF = re.compile(r"\.(?:C|R|L|Q)$")
DESCRIPTION_BY_TYPE_AND_PACKAGE = {
    ("Capacitor", "0603"): "C060304",
    ("Capacitor", "0805"): "C080506",
    ("Resistor", "0603"): "R0603",
    ("Resistor", "1206"): "R120603",
}


def source_reference(component_path: str) -> str:
    return PATH_LEAF.sub("", component_path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def renamed_import_copy(
    source: Path, destination: Path, renames: dict[str, str]
) -> dict[str, Any]:
    text = source.read_text(encoding="utf-8")
    root_uuid_match = re.search(r'\(kicad_sch\s+.*?\(uuid\s+"([^"]+)"\)', text, re.DOTALL)
    if root_uuid_match is None:
        raise core.ComparisonError("Cannot find Power_Supply root UUID")
    root_uuid = root_uuid_match.group(1)
    changes: list[dict[str, Any]] = []
    for old, new in sorted(renames.items(), key=lambda item: (-len(item[0]), item[0])):
        if old == new:
            continue
        property_pattern = re.compile(
            r'(\(property\s+"Reference"\s+")' + re.escape(old) + r'(")'
        )
        instance_pattern = re.compile(
            r'(\(reference\s+")' + re.escape(old) + r'(")'
        )
        text, property_count = property_pattern.subn(r"\g<1>" + new + r"\g<2>", text)
        text, instance_count = instance_pattern.subn(r"\g<1>" + new + r"\g<2>", text)
        if property_count != 1 or instance_count < 1:
            raise core.ComparisonError(
                f"Could not safely rename {old} to {new}: "
                f"property={property_count}, instance={instance_count}"
            )
        changes.append(
            {
                "source_reference": old,
                "temporary_reference": new,
                "property_replacements": property_count,
                "instance_replacements": instance_count,
            }
        )
    text, project_count = re.subn(
        r'(\(project\s+")[^"]+("\s*)',
        r"\g<1>Power_Supply\g<2>",
        text,
    )
    text, path_count = re.subn(
        r'(\(path\s+")/[^"]+("\s*)',
        r"\g<1>/" + root_uuid + r"\g<2>",
        text,
    )
    if project_count == 0 or path_count == 0:
        raise core.ComparisonError(
            f"Could not flatten hierarchical instances: projects={project_count}, "
            f"paths={path_count}"
        )
    destination.write_text(text, encoding="utf-8")
    return {
        "temporary_file": str(destination),
        "renames": changes,
        "flattened_root_uuid": root_uuid,
        "project_replacements": project_count,
        "instance_path_replacements": path_count,
    }


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
        "checked_count": len(expected_paths & set(local)),
        "differences": differences,
    }


def audit_description_footprints(
    local: dict[str, dict[str, Any]], reference_bom: list[dict[str, Any]]
) -> dict[str, Any]:
    reference_by_path = {item["path"]: item for item in reference_bom}
    differences = []
    checked = []
    unverified = []
    for component_path in sorted(set(local) & set(reference_by_path)):
        actual = local[component_path]
        expected_component = reference_by_path[component_path]
        component_type = expected_component.get("component_type")
        package = expected_component.get("package")
        expected_description = DESCRIPTION_BY_TYPE_AND_PACKAGE.get(
            (component_type, package)
        )
        if expected_description is None:
            unverified.append(
                {
                    "path": component_path,
                    "component_type": component_type,
                    "package": package,
                    "reason": (
                        "No source-footprint description convention is asserted for "
                        "this MPN component or placeholder inductor"
                    ),
                }
            )
            continue
        checked.append(component_path)
        actual_description = actual.get("description") or ""
        if actual_description != expected_description:
            differences.append(
                {
                    "path": component_path,
                    "expected_description": expected_description,
                    "actual_description": actual_description,
                    "actual_footprint": actual.get("package") or "",
                    "component_type": component_type,
                    "registry_package": package,
                }
            )
    return {
        "checked_count": len(checked),
        "checked_paths": checked,
        "differences": differences,
        "unverified": unverified,
    }


def align_local_interfaces(
    local_records: list[core.ComponentRecord],
    reference_records: list[core.ComponentRecord],
) -> list[dict[str, Any]]:
    reference_by_path = {record.path: record for record in reference_records}
    changes: list[dict[str, Any]] = []
    for local in local_records:
        reference = reference_by_path.get(local.path)
        if reference is None:
            continue
        if local.path.startswith("PTC") and {"P1", "P2"}.issubset(local.pins):
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
        if not PATH_LEAF.search(local.path) or local.path.endswith(".Q"):
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
        if local_names != reference_names and local_names[::-1] == reference_names:
            local.pins["P1"], local.pins["P2"] = (
                local.pins["P2"],
                local.pins["P1"],
            )
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
    path_by_source_reference = {
        source_reference(item["path"]): item["path"] for item in reference_bom
    }
    if len(path_by_source_reference) != len(reference_bom):
        raise core.ComparisonError("Registry paths do not have unique source references")

    local_components = core.parse_kicad_components(
        schematic, path_by_reference=path_by_source_reference
    )
    component_result = core.compare_components(local_components, reference_bom)
    designator_result = audit_reference_designators(local_components, reference_bom)
    description_result = audit_description_footprints(local_components, reference_bom)

    reference_records = core.evaluate_zen(
        reference_zen, infer_variable_net_names=True
    )
    reference_connectivity = core.connectivity_manifest(reference_records)

    designator_renames = {
        source_reference(item["path"]): item["designator"]
        for item in reference_bom
    }
    imported_designator_to_path = {
        item["designator"]: item["path"] for item in reference_bom
    }
    with tempfile.TemporaryDirectory(prefix="power-supply-identity-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        import_source = temp_dir / schematic.name
        rename_metadata = renamed_import_copy(
            schematic, import_source, designator_renames
        )
        import_output = temp_dir / "imported"
        import_metadata = core.import_schematic(args.pcb, import_source, import_output)
        imported_zen = Path(import_metadata["generated_zen"])
        local_records = core.evaluate_zen(
            imported_zen, path_map=imported_designator_to_path
        )

    interface_normalizations = align_local_interfaces(
        local_records, reference_records
    )

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
        + len(description_result["differences"])
        + topology_difference_count
        + net_name_difference_count
    )
    identical = hard_difference_count == 0

    return {
        "schema_version": 1,
        "status": "identical" if identical else "different",
        "identical": identical,
        "baseline": {
            "package": core.DEFAULT_PACKAGE,
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
            "description_footprints_identical": not description_result["differences"],
            "electrical_topology_identical": topology_difference_count == 0,
            "all_net_names_present": net_name_difference_count == 0,
            "component_difference_count": component_difference_count,
            "reference_designator_difference_count": len(
                designator_result["differences"]
            ),
            "description_footprint_difference_count": len(
                description_result["differences"]
            ),
            "topology_difference_count": topology_difference_count,
            "net_name_difference_count": net_name_difference_count,
        },
        "components": component_result,
        "reference_designators": designator_result,
        "description_footprints": description_result,
        "connectivity": connectivity_result,
        "normalizations": [
            "Registry leaf paths PC1.C/PR1.R/PL1.L/PQ1.Q map to references PC1/PR1/PL1/PQ1",
            "A temporary import copy is renumbered to KiCad-compatible C/R/L/U references",
            "The original Power_Supply.kicad_sch is never written",
            "Zener Net/Power variables without explicit constructor names use their variable names",
            "One leading KiCad root-scope slash is removed from named nets",
            "MOSFET D_<pad>/S_<pad> interfaces are coalesced to D/S",
            "SI-equivalent values and source underscore/ohm/tolerance spellings are normalized",
            "UUIDs, ordering, placement, and graphics are ignored",
        ],
        "commands": {
            "reference_validation": validation,
            "reference_bom": bom_metadata,
            "temporary_reference_renames": rename_metadata,
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
