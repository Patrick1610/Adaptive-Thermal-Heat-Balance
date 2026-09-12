# ATHB scientific model

## Runtime formulation

The integration implements only the Adaptive Thermal Heat Balance formulation published in 2022.
The exact coefficients, equations, numerical domains, binary64 behavior and source hashes are
frozen in [ATHB_NUMERICAL_CONTRACT.md](scientific/ATHB_NUMERICAL_CONTRACT.md). The whitepaper's
`comf` wording discrepancy is documented rather than blended into a second formulation.

For every coherent snapshot ATHB computes the current sensation and attempts five named roots:
lower comfort, heating control, thermal neutral, cooling control and upper comfort. Efficient,
Balanced and Comfort use fixed inward fractions 0.30, 0.50 and 0.70 between each configured outer
sensation vote and zero. This operation is in sensation space. Heating needs the heating-control
root, cooling needs the cooling-control root, and a ranged target needs both; failure of unrelated
roots remains diagnostic and does not block a valid directional decision.

The outer comfort band and inner control band are distinct. Thermal neutral is published as a
reference and is not substituted as the normal actuator target.

## Moisture and provenance

Inverse candidates preserve the starting vapor pressure. Candidate RH is recomputed from that
vapor pressure and candidate temperature. A saturated or physically impossible candidate fails
explicitly; RH is not clipped to conceal saturation. Dew/frost point and optional humidity-ratio
diagnostics use the same psychrometric state.

Measured observations carry `measured` provenance. Direct configuration values such as RH,
activity and ambient air speed carry `declared` provenance and do not receive fabricated sensor
timestamps. Uniform MRT, automatic clothing and internally modelled surfaces carry `estimated`
provenance.

## Radiant and local models

Air temperature, mean radiant temperature and surface temperature are separate physical types.
The user-facing Standard room model estimates MRT from air. The Mold Indicator route reads its
calculated critical-point temperature for surface-risk diagnostics only; that coldest point is not
substituted for occupant-weighted room MRT. The frozen core retains separately tested direct-MRT,
globe and surface-composite primitives for numerical conformance, but the normal wizard does not
ask Home Assistant users for those specialist inputs.

Critical local-air locations run the same selected sensation votes as the primary location. They
require stable warm-up, reject raw deltas above 6 K and have an aggregate influence cap of 2 K.
Surface estimates cannot be substituted for local-air measurements.

## Limitations

ATHB exposes applicability and extrapolation labels rather than a confidence percentage. This
software does not guarantee building response, comfort, mould prevention or frost protection and
does not replace calibrated environmental measurements.
