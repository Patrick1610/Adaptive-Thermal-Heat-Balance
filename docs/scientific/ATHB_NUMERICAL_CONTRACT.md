# ATHB numerical contract, version 1

## Frozen identity

- Formulation identifier: `athb_2022_ptc_4_4_2`.
- Numerical contract version: `1`.
- Reference implementation: `pythermalcomfort==4.4.2`, repository commit `2597e88fed10fec2f40d49759ed9b74e10ce9f89`.
- Arithmetic: scalar IEEE-754 binary64 operations through Python `float`.
- Production dependency boundary: Python standard library only. NumPy, SciPy and Numba are oracle-only dependencies.

This contract implements the 2022 standard ATHB formulation, not the 2015 formulation, the extended ATHBx formulation, a standalone PMV/PPD model, or another selectable comfort model.

## Inputs and tagged clothing

The forward model accepts dry-bulb air temperature `tdb_c`, mean radiant temperature `tr_c`, relative air speed `relative_air_speed_m_s`, relative humidity `rh_pct`, original metabolic rate `met`, outdoor running mean `running_mean_c`, and either tagged automatic clothing or tagged fixed clothing.

Booleans are never numbers for this contract. Every numeric input must be a finite `int` or `float` other than `bool`. An integer outside the finite binary64 conversion range returns the same typed `non_finite` failure as infinity; conversion never leaks `OverflowError`. Automatic clothing is an explicit tag and cannot be selected with `False`, zero, `None`, or another false-like sentinel.

When ambient speed is converted to relative speed, use the original, unadapted activity value:

\[
v_r=\begin{cases}
v,&m\leq1\\
\operatorname{round}_3(v+0.3(m-1)),&m>1
\end{cases}
\]

Direct relative-speed input bypasses this conversion exactly once. The three-decimal rounding applies only to the moving branch.

For every three-decimal result, `round_3` is the pinned NumPy `around(x, 3)`
binary64 operation, reproduced without a production NumPy dependency: multiply
the absolute binary64 value by `1000.0`, round that scaled value to the nearest
integer with ties to even, divide by `1000.0`, and restore the original sign
(including negative zero). It is intentionally not Python `round(x, 3)`, whose
decimal-correction path can choose a different adjacent value. Boundary anchors
include `round_3(0.6165) = 0.616` for relative air speed and
`round_3(-1.1865) = -1.186` for a public vote.

The boundary suite also freezes the valid upstream binary64 straddle at
`tdb_c = tr_c = 14.005708558508`, direct `vr = 0.1`, `RH = 50`, original
`met = 1.2`, `R = 20`, and automatic clothing. Its reconstructed thermal load
is `-55.793504353973056`, unrounded vote is `-1.2854999999999959`, and public
vote is `-1.285`.

## Adaptation and clothing

Physiological adaptation uses `58.2` exactly:

\[
m_a=m-\frac{0.234R}{58.2}
\]

Tagged automatic clothing uses:

\[
clo_a=10^{-0.17168-0.000485R+0.08176m_a-0.00527Rm_a}
\]

Tagged fixed clothing uses its declared `clo` value unchanged. Both modes retain adapted metabolism and the same ATHB transfer function. No dynamic-clothing correction is added.

## Scalar heat-balance kernel

The heat-balance load uses `58.15` exactly, distinct from the adaptation constant:

\[
M=58.15m_a,\quad I_{cl}=0.155clo_a,\quad W=0
\]

\[
f_{cl}=\begin{cases}
1+1.29I_{cl},&I_{cl}\leq0.078\\
1.05+0.645I_{cl},&I_{cl}>0.078
\end{cases}
\]

Vapor pressure inside this frozen kernel is:

\[
p_a=10RH\exp\left(16.6536-\frac{4030.183}{T_a+235}\right)
\]

The clothing-surface iteration preserves the pinned initialization and ordering:

1. `hcf = 12.1 * sqrt(vr)` and initial `hc = hcf`.
2. `taa = tdb + 273` and `tra = tr + 273`; the legacy `273` constants are intentional.
3. `t_cla = taa + (35.5 - tdb) / (3.5 * (6.45 * I_cl + 0.1))`.
4. Initialize `xn = t_cla / 100`, `xf = t_cla / 50`, and convergence epsilon `0.00015`.
5. On each update, average `xf`, calculate natural convection as `2.38 * abs(100 * xf - taa) ** 0.25`, select the larger of natural and forced convection, then update `xn` with the pinned balance equation. Every integer fourth power in that equation and the post-convergence radiation term is evaluated as square-then-square (`q = x * x; q * q`), matching the pinned Numba binary64 lowering rather than CPython's distinct `float ** 4` path.
6. Convergence requires `abs(xn - xf) <= 0.00015`. At most 150 updates are permitted. Exhaustion produces typed `heat_balance_non_convergence`; no plausible value is fabricated.

After convergence:

\[
\begin{aligned}
E_d&=0.00305(5733-6.99M-p_a)\\
E_{sw}&=\max(0,0.42(M-58.15))\\
E_{re}&=0.000017M(5867-p_a)\\
C_{re}&=0.0014M(34-T_a)\\
R_{cl}&=3.96f_{cl}\left[\left(\frac{T_{cl}+273}{100}\right)^4-\left(\frac{T_r+273}{100}\right)^4\right]\\
C_{cl}&=f_{cl}h_c(T_{cl}-T_a)\\
L&=M-E_d-E_{sw}-E_{re}-C_{re}-R_{cl}-C_{cl}
\end{aligned}
\]

The pinned heat-balance source first multiplies `L` by its PMV transfer
coefficient. The ATHB source then divides that intermediate PMV by its own
coefficient before applying the ATHB equation. Production preserves that
binary64 multiply/divide order (including the source's distinct multiplication
association in the two coefficient expressions) and returns the reconstructed,
unrounded thermal load `L`. It does not expose standalone PMV, PPD or
cooling-effect APIs.

## ATHB transfer function and output

The unrounded sensation vote is:

\[
\begin{aligned}
S={}&1.484+0.0276L-0.9602m_a-0.0342R\\
&+0.0002264LR+0.018696m_aR-0.0002909Lm_aR
\end{aligned}
\]

The internal result retains `S` without rounding for later inverse solving. The public companion value uses the `round_3` binary64 operation defined above. Neither result is clipped to `[-3,+3]`.

The inspected `comf::calcATHBstandard` source is not interchangeable with this formulation: it adds a separate `L * m_a` term and repeats the `L * R` term. Those terms are deliberately absent here, matching the selected reference and the research model structure.

## Moisture and radiant candidate semantics

Inverse calculations preserve the starting vapor pressure. A measured RH source remains
`measured`; an explicitly configured fixed RH remains `declared`. Candidate RH is derived with
the ASHRAE/PsychroLib saturation-pressure equations and is never held constant. A candidate that
exceeds saturation by more than `1e-6` percentage point returns
`moisture_limited_no_solution`; only smaller floating-point overshoot is normalized to 100%.
The two descriptive outer comfort-range sensors are the sole exception: if their requested vote
lies beyond that saturation boundary, their display-only inverse continues at 100% RH. These
completed outer limits are never consumed by control eligibility, policy or climate commands;
heating and cooling control roots retain `moisture_limited_no_solution`.
Dew/frost point is the bisection inverse of the same water/ice equations. Zero RH returns the
explicit `dry_limit`, not an invented finite temperature. Optional humidity-ratio diagnostics
require an explicit total pressure; ATHB does not manufacture an atmospheric-pressure reading.

This physical candidate transformation deliberately differs slightly from the frozen ATHB
heat-balance kernel's historical vapor-pressure approximation. The kernel formula above remains
unchanged under numerical contract version 1 so pinned forward conformance is preserved.

Radiant inputs remain physically tagged. The default uniform environment estimates MRT from air
temperature and moves it with candidate air. A direct measured MRT and a current globe-derived
MRT are held constant for the snapshot. A surface composite uses effective occupant view factors
and fourth-power Kelvin combination; measured/modelled surfaces remain fixed while an air
background moves with the candidate, or an explicit measured background remains fixed. Missing
surface data falls back visibly to the uniform estimate without redistributing view factor.
Modelled surfaces retain `estimated` provenance and are never relabelled as air or direct MRT.

## Inverse and mapped-location contract

The production inverse solves the requested sensation votes directly. Efficient, Balanced and
Comfort apply fixed inward fractions 0.30, 0.50 and 0.70 to the configured outer votes before
temperature solving. Thermal neutral remains a separate zero-vote reference. The five roots are
attempted in stable order with one reused 33-point monotonicity scan, bracketed bisection, a
0.005 K maximum final bracket width, a 0.002 maximum sensation residual, at most 48 iterations
per root and one 3,000-ATHB-evaluation budget shared across the zone snapshot. Search intervals
are intersected with the local-air and constant-vapor-pressure saturation domains. Every root
retains its own success or typed failure.

Actuation consumes those diagnostics directionally: heating requires the heating-control root,
cooling requires the cooling-control root, and a range requires both in the required order and
with its applicable gap. Failure of unrelated outer or neutral roots cannot suppress a valid
directional decision.

Critical local-air locations preserve primary vapor pressure unless they have an explicit local
RH source. Their signed room-minus-local delta initializes from the first valid report, uses the
specified 600-second exponential filter, requires ten minutes and three valid reports for
control eligibility, rejects raw magnitude above 6 K and bounds the effective mapped delta to
plus or minus 3 K. Each location solves the same five selected-strategy votes in primary room
coordinates. Surface calibration separately requires a supplied or measured `f_Rsi`; it produces
an `estimated` steady-state surface, finite uncapped surface RH diagnostics, a display value
capped at 100%, and no independent heating demand.

## Outdoor-history contract

Outdoor adaptation uses eligible summaries from the seven previous local calendar days with
fixed finite normalized weights `alpha^(k-1)`; absent dates retain their age and are never
compressed. Raw reports are integrated as piecewise-constant observations only until the next
report, an invalid/unavailable event, or the configured maximum hold. Intervals split at
localized midnight converted to UTC, so 23-, 24- and 25-hour days retain their actual duration.
Daily eligibility requires at least 90% temporal coverage. Complete, partial, diagnostic-only
and unavailable histories remain distinct typed qualities; no instantaneous outdoor value or
invented shutdown coverage substitutes for missing history.

Source validation precedes history integration and preserves measured versus declared
provenance. It rejects unknown, Boolean, nonfinite, unconvertible, stale and out-of-range states;
large jumps enter the specified three-report quarantine. Registry identity retains lineage over
an entity rename, while replacement or generation-bound unregistered identity does not.
Versioned history storage rejects incompatible, duplicate, overlapping, impossible or nonfinite
records and preserves the corrupt payload for diagnosis. Recorder bootstrap is generation- and
deadline-bound, limited to eight calendar days plus the hold lookback and 100,000 records, and
uses the same integration path as live collection.

## Product-policy separation

Raw comfort and control roots are immutable model outputs. Product policy first selects the
worst eligible same-vote critical demand, applies the single aggregate 2 K/comfort-edge cap and
discards conflicting ranged contributions back to the primary control band. It then applies the
resolved occupancy setback and independent Boost mode and, for ordinary environmental updates only, the 0.5 K per
ten-minute room-target slew. Static actuator calibration and explicit user bounds follow in
actuator coordinates and are reported as limitations; none of these transformations are
relabelled as sensation roots. Fixed fallback is a separate typed result with no invented ATHB
calculation and requires valid primary air and selected RH inputs.

## Engineering envelope

Inputs are rejected before evaluation when outside this closed envelope, except where an open bound is stated:

| Quantity | Engineering envelope |
| --- | ---: |
| Candidate air temperature | 5 to 40 degrees Celsius |
| Mean radiant temperature | 0 to 50 degrees Celsius |
| Relative air speed | 0 to 2 metres per second |
| Relative humidity | 0 to 100 percent |
| Original metabolic rate | 0.8 to 2.0 met |
| Running mean | -30 to 45 degrees Celsius |
| Fixed clothing | 0.1 to 2.0 clo |
| Computed automatic clothing | greater than 0 and at most 3.0 clo |
| Adapted metabolic rate | greater than 0 met |

An envelope violation returns typed `outside_engineering_domain`. Boolean input returns `boolean_input`; a non-numeric value returns `non_numeric`; and NaN, infinity, or a numeric integer that cannot be represented as finite binary64 returns `non_finite`. These failures occur before heat-balance evaluation. The `numerical_status` tag is fixed by the concrete immutable result type and is not a constructor argument: `NumericalFailure` is always `failure`, while `HeatBalanceSuccess` and `AthbSuccess` are always `success`.

## Applicability classification

A successful numerical result can carry multiple immutable applicability reasons:

- `limited_evidence` when air temperature or radiant temperature lies outside 14 to 27 degrees Celsius, or running mean lies outside 16 to 30 degrees Celsius.
- `extrapolated` when any explicit research-filter envelope is exceeded: air/radiant temperature 12.6 to 38.5 degrees Celsius, RH 16.9 to 87.7 percent, relative air speed at most 1.9 metres per second, running mean -2.7 to 41.3 degrees Celsius, radiant-minus-air difference -7.4 to +9.2 kelvin, or clothing 0.1 to 2.0 clo.

These labels do not change, clamp, or invalidate a value inside the engineering envelope. Marginal research filters do not claim validity for every combination within their rectangle.

## Conformance thresholds

- Public three-decimal oracle output: absolute error at most `0.00051` vote.
- Unrounded reference output under equivalent arithmetic ordering: absolute error at most `1e-7` vote.
- Displayed current-sensation anchors: absolute error at most `1e-6` vote.
- Typed failures must match exactly.

Normal tests consume checked-in fixtures read-only. Only the isolated oracle driver may generate them, and it must never import `custom_components.athb.core`.
