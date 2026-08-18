# SMARC 5 V / 3.3 V schematic identity check

`compare_schematic_identity.py` compares `SMARC_5V_3V3.kicad_sch` with the
released registry reference by electrical/BOM semantics. It uses the KiCad
`Path` property as component identity, so KiCad reference renumbering does not
affect the result.

## Pinned baseline

- Package: `code.diode.computer/spring/registry/components/Texas_Instruments/TPS51225B`
- Version: `v0.1.0`
- Commit: `8be1328adb24ee6556a76847c1f46d91a19aa0f7`
- Entrypoint: `components/Texas_Instruments/TPS51225B/SMARC_5V_3V3.zen`
- Verified compiler: `pcbc 0.4.29`

The baseline was located and verified through the diode-registry MCP. The
installed CLI's direct module download currently fails during sparse checkout
with `invalid repo`, even though authenticated registry search and Git access
succeed. Until that CLI issue is fixed, check out the exact tag into a scratch
directory:

```sh
pcb auth git configure https://code.diode.computer/spring/registry
git clone --depth 1 --single-branch \
  --branch components/Texas_Instruments/TPS51225B/v0.1.0 \
  https://code.diode.computer/spring/registry.git \
  /private/tmp/tps51225b-registry
```

Run the comparison:

```sh
python3 -B compare_schematic_identity.py \
  SMARC_5V_3V3.kicad_sch \
  /private/tmp/tps51225b-registry/components/Texas_Instruments/TPS51225B/SMARC_5V_3V3.zen \
  --expected-commit 8be1328adb24ee6556a76847c1f46d91a19aa0f7 \
  --output SMARC_5V_3V3.identity.json
```

Exit status is `0` for identical, `1` for a completed comparison with hard
differences, and `2` for a tool or input error.

## What is compared

Hard requirements are the component-path set, MPN or normalized value,
DNP/BOM/board state, logical pin endpoints, topology, and named nets. The
comparison ignores UUIDs, KiCad references, ordering, placement, and graphics.
Anonymous nets are compared by their sorted `Path:pin` endpoint sets.

Run the regression tests with:

```sh
python3 -B -m unittest -v test_compare_schematic_identity.py
```

## Current result

The saved `SMARC_5V_3V3.identity.json` report says `different`:

- BOM: identical, 74 of 74 components
- Electrical topology: identical, 171 of 171 logical endpoints
- Named nets: 18 net-name differences

Most differences are registry-named internal nets that are anonymous in the
KiCad file. Two notable explicit-name differences are
`P_3V3_5V_A_PG` versus `P_+3V3_5V_A_PG`, and local `P_3V3_LDO` versus an
anonymous registry net. Therefore the circuit and stuffing match, but the
selected electrical-plus-BOM identity criterion does not pass because exact
named nets were included in that criterion.
