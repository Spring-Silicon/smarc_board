# Power_Supply registry comparison

`Power_Supply.kicad_sch` was audited read-only against:

- `code.diode.computer/spring/registry/components/Texas_Instruments/TPS51225B`
- release `v0.1.0`
- commit `8be1328adb24ee6556a76847c1f46d91a19aa0f7`
- entrypoint `SMARC_5V_3V3.zen`

The source file SHA-256 before and after the audit was
`67967ee361d79aab353d951070b16c3dd2521ee0f60acd903b95e3d70ad459ab`.

## Result

The result is `different`.

- All 74 reference designators match the source portion of the Zener paths.
- All expected component paths are present; there are no extra components.
- One electrical connection differs: `PQ2.Q:D` is on `GND`, but the registry
  connects it to `P_+5V_3V3_VINFIT`.
- One net name differs: local `P_+3V3_HG_20_R` should be
  `P_+3V3_HG_R` on `PQ2.Q:G` / `PR3.R:P2`.
- `PC5` is `220pF 50V`; the registry value is `2.2nF 50V` (`2200pF`).
- `PTC21`, `PTC23`, `PTC24`, `PTC25`, `PTC26`, and `PTC27` lack the registry
  MPN `EEFCX0J151YR`.
- 22 passive Description fields are empty instead of carrying the expected
  source-footprint annotation.

Missing `C060304` descriptions:

`PC5`, `PC6`, `PC7`, `PC15`, `PC19`, `PC20`, `PC25`, `PC26`, `PC27`, `PC31`,
and `PC35`.

Missing `R0603` descriptions:

`PR1`, `PR9`, `PR10`, `PR11`, `PR12`, `PR13`, `PR14`, `PR15`, `PR16`, `PR17`,
and `PR18`.

The registry does not assert the same source-footprint Description convention
for the three placeholder inductors or the 11 MPN-backed components, so those
14 descriptions are listed as unverified rather than silently accepted.

## Reproduction

```sh
python3 -B compare_power_supply.py \
  Power_Supply.kicad_sch \
  /path/to/registry/components/Texas_Instruments/TPS51225B/SMARC_5V_3V3.zen \
  --output Power_Supply.identity.json
```

The importer cannot consume this hierarchical sheet directly because its
references intentionally use Zener source names (`PC`, `PR`, `PL`, `PU`) and
its instance paths point at a parent project. The audit makes a temporary copy,
renumbers it to KiCad-compatible references, flattens its instance path, and
maps the extracted connectivity back to the original Zener paths. The original
schematic is never written.
