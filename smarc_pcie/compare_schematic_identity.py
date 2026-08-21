#!/usr/bin/env python3
"""Compare a Diode-generated KiCad schematic with a pinned Zener design.

The comparison is intentionally semantic:

* component identity is keyed by KiCad's ``Path`` property;
* BOM data for the reference comes from ``pcb bom``;
* connectivity is evaluated with a deliberately restricted Zener/Python subset;
* KiCad references, UUIDs, placement, graphics, and property ordering are ignored.

The restricted evaluator has no Python builtins, rejects imports and arbitrary
attribute access, and exposes only inert circuit-construction stubs.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


DEFAULT_PACKAGE = (
    "code.diode.computer/spring/registry/components/"
    "Texas_Instruments/TPS51225B"
)
ANONYMOUS_KICAD_NET = re.compile(r"^Net-\(")
REPEATED_MOSFET_PIN = re.compile(r"^(D|S)_\d+$")
VALUE_TOKEN = re.compile(
    r"^(?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+))"
    r"(?P<prefix>[pnumkKM]?)(?P<unit>ohm|[FfHhVvAa%])?$"
)
SI_SCALE = {
    "p": Decimal("1e-12"),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "": Decimal(1),
    "k": Decimal("1e3"),
    "K": Decimal("1e3"),
    "M": Decimal("1e6"),
}


class ComparisonError(RuntimeError):
    pass


@dataclass(eq=False)
class NetValue:
    name: str | None = None
    kind: str = "net"


@dataclass
class ComponentRecord:
    path: str
    designator: str
    module: str
    pins: dict[str, NetValue]
    dnp: bool = False


@dataclass
class EvaluationState:
    path_map: dict[str, str]
    components: list[ComponentRecord] = field(default_factory=list)

    def record_module(self, source: str, kwargs: dict[str, Any]) -> None:
        designator = kwargs.get("name")
        if not isinstance(designator, str):
            raise ComparisonError(f"Module call lacks a string name: {source}")
        path = self.path_map.get(designator)
        if path is None:
            suffix = infer_module_suffix(source)
            path = designator if suffix is None else f"{designator}.{suffix}"
        pins: dict[str, NetValue] = {}

        def collect(prefix: str, value: Any) -> None:
            if isinstance(value, NetValue):
                pins[prefix] = value
            elif isinstance(value, dict):
                for child_name, child_value in value.items():
                    child_prefix = f"{prefix}.{child_name}" if prefix else child_name
                    collect(child_prefix, child_value)

        for pin_name, pin_value in kwargs.items():
            collect(pin_name, pin_value)
        self.components.append(
            ComponentRecord(
                path=path,
                designator=designator,
                module=source,
                pins=pins,
                dnp=bool(kwargs.get("dnp", False)),
            )
        )

    def record_component(self, *args: Any, **kwargs: Any) -> None:
        del args
        designator = kwargs.get("name")
        if not isinstance(designator, str):
            raise ComparisonError("Component() lacks a string name")
        raw_pins = kwargs.get("pins", {})
        if not isinstance(raw_pins, dict):
            raise ComparisonError(f"Component {designator} has non-dict pins")
        pins = {k: v for k, v in raw_pins.items() if isinstance(v, NetValue)}
        self.components.append(
            ComponentRecord(
                path=self.path_map.get(designator, designator),
                designator=designator,
                module="Component",
                pins=pins,
                dnp=bool(kwargs.get("dnp", False)),
            )
        )


class ModuleFactory:
    def __init__(self, source: str, state: EvaluationState):
        self.source = source
        self.state = state

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        if args:
            raise ComparisonError(f"Positional Module arguments are unsupported: {self.source}")
        self.state.record_module(self.source, kwargs)


class ResistanceValue:
    def __init__(self, value: str):
        self.value = value
        self.tolerance: str | None = None

    def with_tolerance(self, tolerance: str) -> "ResistanceValue":
        self.tolerance = tolerance
        return self


def infer_module_suffix(source: str) -> str | None:
    stem = Path(source).stem.lower()
    if "capacitor" in stem or stem.startswith("eefcx"):
        return "C"
    if "resistor" in stem:
        return "R"
    if "inductor" in stem:
        return "L"
    if stem.startswith("bsc090"):
        return "Q"
    return None


def validate_zen_ast(tree: ast.AST, source_path: Path) -> None:
    forbidden = (
        ast.Import,
        ast.ImportFrom,
        ast.ClassDef,
        ast.Lambda,
        ast.With,
        ast.AsyncWith,
        ast.Try,
        ast.Raise,
        ast.Global,
        ast.Nonlocal,
        ast.Await,
        ast.Yield,
        ast.YieldFrom,
    )
    for node in ast.walk(tree):
        if isinstance(node, forbidden):
            raise ComparisonError(
                f"Unsafe/unsupported syntax {type(node).__name__} in {source_path}"
            )
        if isinstance(node, ast.Attribute) and node.attr != "with_tolerance":
            raise ComparisonError(
                f"Unsupported attribute access .{node.attr} in {source_path}"
            )
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ComparisonError(f"Dunder name {node.id} is forbidden in {source_path}")


def evaluate_zen(
    path: Path,
    path_map: dict[str, str] | None = None,
    infer_variable_net_names: bool = False,
) -> list[ComponentRecord]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    validate_zen_ast(tree, path)
    state = EvaluationState(path_map=path_map or {})

    def make_net(*args: Any, **kwargs: Any) -> NetValue:
        del kwargs
        name = args[0] if args and isinstance(args[0], str) else None
        return NetValue(name=name)

    def make_ground(*args: Any, **kwargs: Any) -> NetValue:
        value = make_net(*args, **kwargs)
        value.kind = "ground"
        return value

    def make_nc(*args: Any, **kwargs: Any) -> NetValue:
        del args, kwargs
        return NetValue(kind="not_connected")

    def module(source_name: str) -> ModuleFactory:
        return ModuleFactory(source_name, state)

    def passthrough(value: Any, **kwargs: Any) -> Any:
        del kwargs
        return value

    def inert(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None

    def symbol(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"args": args, **kwargs}

    def interface(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if args:
            raise ComparisonError("Positional interface arguments are unsupported")
        return kwargs

    environment: dict[str, Any] = {
        "__builtins__": {},
        "load": inert,
        "Module": module,
        "Component": state.record_component,
        "Symbol": symbol,
        "DiffPair": interface,
        "Usb3": interface,
        "Net": make_net,
        "Power": make_net,
        "Ground": make_ground,
        "NotConnected": make_nc,
        "io": passthrough,
        "Resistance": ResistanceValue,
        "Board": inert,
        "BoardConfig": inert,
        "Layout": inert,
        "config": inert,
    }
    exec(compile(tree, str(path), "exec"), environment, environment)
    if infer_variable_net_names:
        for variable_name, value in environment.items():
            if (
                isinstance(value, NetValue)
                and value.kind != "not_connected"
                and value.name is None
                and not variable_name.startswith("__")
            ):
                value.name = variable_name
    duplicates = find_duplicates(component.path for component in state.components)
    if duplicates:
        raise ComparisonError(f"Duplicate component paths in {path}: {duplicates}")
    return state.components


def parse_sexpr(text: str) -> list[Any]:
    roots: list[Any] = []
    stack: list[list[Any]] = [roots]
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == "(":
            node: list[Any] = []
            stack[-1].append(node)
            stack.append(node)
            index += 1
            continue
        if char == ")":
            if len(stack) == 1:
                raise ComparisonError("Unbalanced ')' in KiCad schematic")
            stack.pop()
            index += 1
            continue
        if char == '"':
            index += 1
            value: list[str] = []
            while index < length:
                char = text[index]
                if char == '"':
                    index += 1
                    break
                if char == "\\" and index + 1 < length:
                    index += 1
                    escaped = text[index]
                    value.append({"n": "\n", "r": "\r", "t": "\t"}.get(escaped, escaped))
                    index += 1
                    continue
                value.append(char)
                index += 1
            else:
                raise ComparisonError("Unterminated string in KiCad schematic")
            stack[-1].append("".join(value))
            continue
        start = index
        while index < length and not text[index].isspace() and text[index] not in "()":
            index += 1
        stack[-1].append(text[start:index])
    if len(stack) != 1:
        raise ComparisonError("Unbalanced '(' in KiCad schematic")
    if len(roots) != 1 or not isinstance(roots[0], list):
        raise ComparisonError("Expected one KiCad root expression")
    return roots[0]


def direct_children(node: list[Any], name: str) -> list[list[Any]]:
    return [
        child
        for child in node[1:]
        if isinstance(child, list) and child and child[0] == name
    ]


def first_child(node: list[Any], name: str) -> list[Any] | None:
    matches = direct_children(node, name)
    return matches[0] if matches else None


def property_map(node: list[Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for prop in direct_children(node, "property"):
        if len(prop) >= 3 and isinstance(prop[1], str) and isinstance(prop[2], str):
            result[prop[1]] = prop[2]
    return result


def yes_no(node: list[Any], name: str, default: bool) -> bool:
    child = first_child(node, name)
    if child is None or len(child) < 2:
        return default
    return child[1] == "yes"


def parse_kicad_components(
    path: Path,
    path_by_reference: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    root = parse_sexpr(path.read_text(encoding="utf-8"))
    if not root or root[0] != "kicad_sch":
        raise ComparisonError(f"Not a KiCad schematic: {path}")

    library_defaults: dict[str, dict[str, str]] = {}
    lib_symbols = first_child(root, "lib_symbols")
    if lib_symbols:
        for symbol in direct_children(lib_symbols, "symbol"):
            if len(symbol) >= 2 and isinstance(symbol[1], str):
                library_defaults[symbol[1]] = property_map(symbol)

    components: dict[str, dict[str, Any]] = {}
    designators: set[str] = set()
    for symbol in direct_children(root, "symbol"):
        lib_id_node = first_child(symbol, "lib_id")
        if lib_id_node is None or len(lib_id_node) < 2:
            continue
        lib_id = lib_id_node[1]
        placed = property_map(symbol)
        reference = placed.get("Reference")
        component_path = placed.get("Path")
        if component_path is None and reference and path_by_reference:
            component_path = path_by_reference.get(reference)
        if not component_path:
            continue
        if not reference:
            raise ComparisonError(f"Component {component_path} lacks Reference")
        if component_path in components:
            raise ComparisonError(f"Duplicate KiCad Path property: {component_path}")
        if reference in designators:
            raise ComparisonError(f"Duplicate KiCad reference: {reference}")
        designators.add(reference)
        defaults = library_defaults.get(lib_id, {})

        def inherited(name: str) -> str | None:
            value = placed.get(name)
            return value if value else defaults.get(name)

        components[component_path] = {
            "path": component_path,
            "designator": reference,
            "lib_id": lib_id,
            "value": inherited("Value"),
            "mpn": inherited("Manufacturer_Part_Number"),
            "manufacturer": inherited("Manufacturer_Name"),
            "package": inherited("Footprint"),
            "description": placed.get("Description"),
            "explicit_path": placed.get("Path"),
            "dnp": yes_no(symbol, "dnp", False),
            "in_bom": yes_no(symbol, "in_bom", True),
            "on_board": yes_no(symbol, "on_board", True),
        }
    return components


def find_duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def normalize_pin(pin: str) -> str:
    match = REPEATED_MOSFET_PIN.match(pin)
    return match.group(1) if match else pin


def normalize_named_net(name: str | None) -> str | None:
    if name is None or ANONYMOUS_KICAD_NET.match(name):
        return None
    return name[1:] if name.startswith("/") else name


def connectivity_manifest(components: list[ComponentRecord]) -> dict[str, Any]:
    endpoint_to_net: dict[tuple[str, str], NetValue] = {}
    net_members: dict[NetValue, set[tuple[str, str]]] = {}
    conflicts: list[dict[str, str]] = []

    for component in components:
        for raw_pin, net in component.pins.items():
            endpoint = (component.path, normalize_pin(raw_pin))
            prior = endpoint_to_net.get(endpoint)
            if prior is not None and prior is not net:
                prior_name = normalize_named_net(prior.name)
                net_name = normalize_named_net(net.name)
                if prior_name != net_name:
                    conflicts.append(
                        {
                            "endpoint": f"{endpoint[0]}:{endpoint[1]}",
                            "first": prior_name or "anonymous",
                            "second": net_name or "anonymous",
                        }
                    )
                    continue
            endpoint_to_net[endpoint] = net
            net_members.setdefault(net, set()).add(endpoint)

    def endpoint_text(endpoint: tuple[str, str]) -> str:
        return f"{endpoint[0]}:{endpoint[1]}"

    def net_key(net: NetValue) -> str:
        if net.kind == "not_connected":
            return "not_connected"
        named = normalize_named_net(net.name)
        if named is not None:
            return f"named:{named}"
        members = sorted(endpoint_text(item) for item in net_members[net])
        return "anonymous:" + "|".join(members)

    endpoints = {
        endpoint_text(endpoint): net_key(net)
        for endpoint, net in sorted(endpoint_to_net.items())
    }
    nets: dict[str, list[str]] = {}
    for endpoint, net in endpoint_to_net.items():
        nets.setdefault(net_key(net), []).append(endpoint_text(endpoint))
    nets = {key: sorted(value) for key, value in sorted(nets.items())}
    return {
        "endpoints": endpoints,
        "nets": nets,
        "conflicts": conflicts,
    }


def canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def normalize_value(value: str | None) -> Any:
    if value is None:
        return None
    cleaned = (
        value.replace("µ", "u")
        .replace("Ω", "ohm")
        .replace("_", " ")
        .replace("±", "+-")
        .strip()
    )
    tokens = cleaned.split()
    normalized: list[tuple[str, str]] = []
    for token in tokens:
        if token.startswith("+-"):
            token = token[2:]
        match = VALUE_TOKEN.fullmatch(token)
        if not match:
            return re.sub(r"\s+", " ", cleaned).lower()
        try:
            number = Decimal(match.group("number"))
        except InvalidOperation:
            return re.sub(r"\s+", " ", cleaned).lower()
        prefix = match.group("prefix") or ""
        unit = (match.group("unit") or "").lower()
        if unit == "ohm":
            unit = ""
        normalized.append((canonical_decimal(number * SI_SCALE[prefix]), unit))
    return normalized


def values_equivalent(actual: Any, expected: Any) -> bool:
    if actual == expected:
        return True

    def zero_with_optional_tolerance(value: Any) -> bool:
        return (
            isinstance(value, list)
            and bool(value)
            and value[0] == ("0", "")
            and all(unit == "%" for _, unit in value[1:])
        )

    return zero_with_optional_tolerance(actual) and zero_with_optional_tolerance(expected)


def run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def repository_root(reference_zen: Path) -> Path:
    result = run_command(
        ["git", "-C", str(reference_zen.parent), "rev-parse", "--show-toplevel"],
        reference_zen.parent,
    )
    if result.returncode != 0:
        raise ComparisonError(f"Cannot locate registry checkout:\n{result.stdout}")
    return Path(result.stdout.strip()).resolve()


def resolve_reference_bom(
    pcb: str, reference_zen: Path, repo_root: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result = run_command(
        [pcb, "bom", "--offline", "--format", "json", str(reference_zen)],
        repo_root,
    )
    if result.returncode != 0:
        raise ComparisonError(f"pcb bom failed:\n{result.stdout}")
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ComparisonError(f"pcb bom returned invalid JSON: {error}") from error
    slim = [
        {
            "path": item["path"],
            "designator": item.get("designator"),
            "package": item.get("package"),
            "value": item.get("value"),
            "mpn": item.get("mpn"),
            "manufacturer": item.get("manufacturer"),
            "description": item.get("description"),
            "component_type": item.get("component_type"),
            "dnp": item.get("dnp") is True,
            "in_bom": True,
            "on_board": True,
        }
        for item in raw
    ]
    return slim, {"command": " ".join(result.args), "component_count": len(slim)}


def validate_reference(
    pcb: str, reference_zen: Path, repo_root: Path
) -> dict[str, Any]:
    result = run_command(
        [pcb, "build", "--offline", "--diagnostics", "-", str(reference_zen)],
        repo_root,
    )
    if result.returncode != 0:
        raise ComparisonError(f"pcb build failed:\n{result.stdout}")
    return {
        "command": " ".join(result.args),
        "output": result.stdout.strip(),
    }


def import_schematic(pcb: str, schematic: Path, destination: Path) -> dict[str, Any]:
    result = run_command([pcb, "import", str(schematic), str(destination)], schematic.parent)
    generated = destination / f"{schematic.stem}.zen"
    if not generated.is_file():
        raise ComparisonError(
            f"pcb import did not create {generated} (exit {result.returncode}):\n"
            f"{result.stdout[-4000:]}"
        )
    return {
        "generated_zen": str(generated),
        "returncode": result.returncode,
        "warnings": len(re.findall(r"^Warning:", result.stdout, re.MULTILINE)),
        "errors": len(re.findall(r"^Error:", result.stdout, re.MULTILINE)),
        "tail": "\n".join(result.stdout.strip().splitlines()[-12:]),
    }


def compare_components(
    local: dict[str, dict[str, Any]], reference: list[dict[str, Any]]
) -> dict[str, Any]:
    reference_by_path = {item["path"]: item for item in reference}
    duplicate_paths = find_duplicates(item["path"] for item in reference)
    if duplicate_paths:
        raise ComparisonError(f"Duplicate reference BOM paths: {duplicate_paths}")

    local_paths = set(local)
    reference_paths = set(reference_by_path)
    differences: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []

    for component_path in sorted(local_paths & reference_paths):
        actual = local[component_path]
        expected = reference_by_path[component_path]
        checks: list[tuple[str, Any, Any]] = [
            ("dnp", actual["dnp"], expected["dnp"]),
            ("in_bom", actual["in_bom"], expected["in_bom"]),
            ("on_board", actual["on_board"], expected["on_board"]),
        ]
        if expected.get("mpn"):
            checks.append(("mpn", actual.get("mpn"), expected["mpn"]))
        else:
            checks.append(
                (
                    "value",
                    normalize_value(actual.get("value")),
                    normalize_value(expected.get("value")),
                )
            )
        for field_name, actual_value, expected_value in checks:
            equal = (
                values_equivalent(actual_value, expected_value)
                if field_name == "value"
                else actual_value == expected_value
            )
            if not equal:
                differences.append(
                    {
                        "path": component_path,
                        "field": field_name,
                        "expected": expected_value,
                        "actual": actual_value,
                    }
                )
        for field_name in ("manufacturer", "package"):
            actual_value = actual.get(field_name)
            expected_value = expected.get(field_name)
            if actual_value and expected_value and actual_value != expected_value:
                advisories.append(
                    {
                        "path": component_path,
                        "field": field_name,
                        "expected": expected_value,
                        "actual": actual_value,
                    }
                )

    return {
        "local_count": len(local),
        "reference_count": len(reference),
        "missing_from_local": sorted(reference_paths - local_paths),
        "extra_in_local": sorted(local_paths - reference_paths),
        "differences": differences,
        "advisories": advisories,
    }


def compare_connectivity(local: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    local_endpoints = local["endpoints"]
    reference_endpoints = reference["endpoints"]
    local_keys = set(local_endpoints)
    reference_keys = set(reference_endpoints)
    differences = []
    for endpoint in sorted(local_keys & reference_keys):
        if local_endpoints[endpoint] != reference_endpoints[endpoint]:
            differences.append(
                {
                    "endpoint": endpoint,
                    "expected": reference_endpoints[endpoint],
                    "actual": local_endpoints[endpoint],
                }
            )

    def membership_index(manifest: dict[str, Any]) -> dict[tuple[str, ...], str]:
        return {
            tuple(members): net_name
            for net_name, members in manifest["nets"].items()
        }

    local_memberships = membership_index(local)
    reference_memberships = membership_index(reference)
    local_groups = set(local_memberships)
    reference_groups = set(reference_memberships)
    net_name_differences = []
    for members in sorted(local_groups & reference_groups):
        actual = local_memberships[members]
        expected = reference_memberships[members]
        if actual != expected:
            net_name_differences.append(
                {
                    "endpoints": list(members),
                    "expected": expected,
                    "actual": actual,
                }
            )
    return {
        "local_endpoint_count": len(local_endpoints),
        "reference_endpoint_count": len(reference_endpoints),
        "missing_from_local": sorted(reference_keys - local_keys),
        "extra_in_local": sorted(local_keys - reference_keys),
        "differences": differences,
        "topology_missing_from_local": [
            list(members) for members in sorted(reference_groups - local_groups)
        ],
        "topology_extra_in_local": [
            list(members) for members in sorted(local_groups - reference_groups)
        ],
        "net_name_differences": net_name_differences,
        "local_conflicts": local["conflicts"],
        "reference_conflicts": reference["conflicts"],
        "local_nets": local["nets"],
        "reference_nets": reference["nets"],
    }


def command_version(command: str) -> str:
    result = run_command([command, "--version"], Path.cwd())
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "unknown"


def git_revision(repo_root: Path) -> str:
    result = run_command(["git", "rev-parse", "HEAD"], repo_root)
    if result.returncode != 0:
        raise ComparisonError(result.stdout)
    return result.stdout.strip()


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    schematic = args.schematic.resolve()
    reference_zen = args.reference_zen.resolve()
    if not schematic.is_file():
        raise ComparisonError(f"Missing schematic: {schematic}")
    if not reference_zen.is_file():
        raise ComparisonError(f"Missing reference Zener source: {reference_zen}")

    repo_root = repository_root(reference_zen)
    revision = git_revision(repo_root)
    if args.expected_commit and revision != args.expected_commit:
        raise ComparisonError(
            f"Reference checkout is {revision}, expected {args.expected_commit}"
        )

    validation = validate_reference(args.pcb, reference_zen, repo_root)
    reference_bom, bom_metadata = resolve_reference_bom(args.pcb, reference_zen, repo_root)
    local_components = parse_kicad_components(schematic)
    designator_to_path = {
        item["designator"]: component_path
        for component_path, item in local_components.items()
    }

    reference_records = evaluate_zen(reference_zen)
    reference_connectivity = connectivity_manifest(reference_records)

    import_metadata: dict[str, Any]
    if args.imported_zen:
        imported_zen = args.imported_zen.resolve()
        import_metadata = {
            "generated_zen": str(imported_zen),
            "reused": True,
        }
        local_records = evaluate_zen(imported_zen, designator_to_path)
    else:
        with tempfile.TemporaryDirectory(prefix="schematic-identity-") as temp_dir:
            import_metadata = import_schematic(args.pcb, schematic, Path(temp_dir))
            local_records = evaluate_zen(
                Path(import_metadata["generated_zen"]), designator_to_path
            )

    local_connectivity = connectivity_manifest(local_records)
    component_result = compare_components(local_components, reference_bom)
    connectivity_result = compare_connectivity(
        local_connectivity, reference_connectivity
    )

    component_hard_differences = (
        len(component_result["missing_from_local"])
        + len(component_result["extra_in_local"])
        + len(component_result["differences"])
    )
    connectivity_hard_differences = (
        len(connectivity_result["missing_from_local"])
        + len(connectivity_result["extra_in_local"])
        + len(connectivity_result["topology_missing_from_local"])
        + len(connectivity_result["topology_extra_in_local"])
        + len(connectivity_result["net_name_differences"])
        + len(connectivity_result["local_conflicts"])
        + len(connectivity_result["reference_conflicts"])
    )
    identical = component_hard_differences == 0 and connectivity_hard_differences == 0

    return {
        "schema_version": 1,
        "status": "identical" if identical else "different",
        "identical": identical,
        "baseline": {
            "package": args.package,
            "version": args.version,
            "commit": revision,
            "entrypoint": str(reference_zen.relative_to(repo_root)),
            "pcb_version": command_version(args.pcb),
        },
        "target": str(schematic),
        "summary": {
            "component_hard_differences": component_hard_differences,
            "connectivity_hard_differences": connectivity_hard_differences,
            "advisories": len(component_result["advisories"]),
            "bom_identical": component_hard_differences == 0,
            "electrical_topology_identical": not (
                connectivity_result["missing_from_local"]
                or connectivity_result["extra_in_local"]
                or connectivity_result["topology_missing_from_local"]
                or connectivity_result["topology_extra_in_local"]
                or connectivity_result["local_conflicts"]
                or connectivity_result["reference_conflicts"]
            ),
            "named_nets_identical": not connectivity_result[
                "net_name_differences"
            ],
        },
        "components": component_result,
        "connectivity": connectivity_result,
        "normalizations": [
            "KiCad components keyed by Path rather than Reference",
            "one leading KiCad root-scope slash removed from named nets",
            "KiCad Net-(...) names treated as anonymous nets",
            "anonymous nets keyed by sorted component-path/pin endpoints",
            "MOSFET D_<pad>/S_<pad> interfaces coalesced to D/S",
            "SI-equivalent value spellings normalized",
            "UUIDs, ordering, placement, and graphics ignored",
        ],
        "commands": {
            "reference_validation": validation,
            "reference_bom": bom_metadata,
            "local_import": import_metadata,
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schematic", type=Path)
    parser.add_argument("reference_zen", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--imported-zen", type=Path)
    parser.add_argument("--pcb", default="pcb")
    parser.add_argument("--package", default=DEFAULT_PACKAGE)
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--expected-commit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        report = build_report(args)
    except (ComparisonError, OSError, SyntaxError) as error:
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
