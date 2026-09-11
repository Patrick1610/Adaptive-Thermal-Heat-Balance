# Adaptive Thermal Heat Balance (ATHB)

## Production architecture and implementation plan

**Planning baseline:** 10 September 2026 — final targeted refinement  
**Product domain:** `athb`  
**Target platform:** Home Assistant Core 2026.9.0 or later, subject to the compatibility tests below  
**Deliverable status:** Architecture specification; no integration code or scaffolding has been created.

Normative terms such as **must**, **must not**, and **shall** define implementation requirements. Scientific findings, engineering decisions, product policies, and physical approximations are distinguished explicitly.

---

## 1. Executive conclusion

Build **Adaptive Thermal Heat Balance (ATHB)** as a local, event-driven Home Assistant integration with:

- One config entry and virtual device per thermal zone.
- One precisely frozen ATHB formulation.
- A small, pure-Python numerical core.
- Shared outdoor-history collection.
- Physically explicit humidity, radiant, surface, and local-air models.
- A deterministic inverse solver.
- A separate comfort-policy layer.
- Capability-driven climate adapters.
- Per-actuator control ownership.
- A single command broker responsible for every climate write.
- Native UI configuration, diagnostics, recovery, and real temperature control in the first completed release.

Use **Option B: an internal implementation**, with pinned `pythermalcomfort` in an isolated development environment as the numerical reference.

The canonical repository is **[Patrick1610/Adaptive-Thermal-Heat-Balance](https://github.com/Patrick1610/Adaptive-Thermal-Heat-Balance)**. Its mandatory local working copy is **`/Volumes/Storage/Adaptive Thermal Heat Balance`**. The `.git` repository in that exact directory must use the canonical GitHub repository as `origin`; the build agent must not create or use a second clone elsewhere. These are the owner-specified project identities, not a claim that local Git setup has been performed by this document refinement. The later build prompt supplies operational Git instructions.

This document is the single technical implementation specification and supersedes the supplied architecture plan. The whitepaper and old Adaptive Climate blueprint remain reference material. Where their instructions differ, the requirements in this plan govern implementation.

The initial product has three comfort strategies: **Efficient**, **Balanced** (default), and **Comfort**. Each solves heating and cooling targets inward from the relevant comfort boundary in sensation space. Thermal neutral remains the zero-vote reference. The normal ranged request is the control band between the two strategy roots.

Software completion is proven entirely by repository-based numerical, policy/state-machine, Virtual Installation Validation, and Home Assistant API-contract tests. All production target-writing paths must be implemented and exercised through controlled service doubles. No external HA deployment, physical equipment, or owner-performed test is a completion dependency. Passing the complete Definition of Done permits the status **`SOFTWARE_COMPLETE`**.

### Corrections to the whitepaper

| Whitepaper assumption or ambiguity | Required decision |
| --- | --- |
| “ATHB” identifies an unambiguous numerical implementation | Freeze the 2022 standard formulation as implemented by `pythermalcomfort 4.4.2`; document the discovered discrepancy with `comf`. |
| A PMV-related internal module conflicts with “ATHB only” | ATHB requires Fanger heat-balance calculations internally. These are an implementation component, not a second selectable comfort model. |
| A Mold Detector critical-point temperature can be mapped to a local-air location | A helper-derived surface estimate is not measured local air. It belongs to surface-risk modelling unless measurement provenance establishes otherwise. |
| Candidate RH can simply be clamped to 100% | Material supersaturation invalidates the constant-moisture candidate. Clamping would silently introduce condensation and moisture removal. |
| Seven days and `alpha=0.8` are inherently mandated by ATHB | They are the selected outdoor-exposure estimator, consistent with the referenced utility, not universal ATHB scientific validity criteria. |
| Managed heating should be the primary mode | The initial product owns temperature targets only. It does not turn equipment on or change HVAC modes automatically. |
| Climate targets should be converted to the device’s native unit before a service call | Home Assistant’s `climate.set_temperature` service accepts the configured HA temperature unit and performs device conversion itself. |
| Entity precision determines actuator step size | Display precision and accepted setpoint increments are different. Capability discovery and acknowledgement must handle that distinction. |
| Fixed fallback is inherently safe | Fallback is an explicit bounded policy. It requires valid fresh primary temperature, eligible measured or explicitly declared RH, available equipment, and control ownership. |
| Thermal neutral is the normal conditioning target | Thermal neutral is the zero-vote reference; the chosen comfort strategy defines separate heating and cooling control roots. |
| An RH entity is always necessary | A measured source is recommended; an explicit fixed RH declaration in configuration is supported without a helper. |
| Completion depends on a household trial | The full software control path is qualified with deterministic virtual installations and API-contract tests inside the repository. |

The HA unit behavior is verified in the [2026.9.0 climate service implementation](https://github.com/home-assistant/core/blob/2026.9.0/homeassistant/components/climate/__init__.py).

### Principal risks

1. **Empirical extrapolation:** Dutch winter conditions can fall outside the research dataset’s outdoor-temperature envelope.
2. **Model-to-building approximation:** An instantaneous comfort calculation is not a prediction of wall warming, moisture removal, or equipment response.
3. **Actuator uncertainty:** HA feature flags do not prove that every advertised target works in every HVAC mode.
4. **Competing controllers:** HA provides no universal exclusive-control lock over climate entities.
5. **Measurement provenance:** Fresh HA state reports do not necessarily prove fresh physical measurements.

These risks must remain visible in diagnostics and must constrain control. They must not be hidden behind a numerical “confidence percentage.”

---

## 2. Scientific model specification

### 2.1 Selected formulation and provenance

**Decision:** Implement the **2022 standard ATHB formulation**, excluding the extended building-type/cooling-strategy formulation, with coefficients and numerical behavior pinned to `pythermalcomfort 4.4.2`.

The 2015 formulation, 2022 standard formulation, and extended `ATHBx` formulation are not interchangeable. The published 2022 standard model uses a thermal-load transfer function without a separate two-way `load × adapted_met` term. The original research analysis also specifies that structure. [2022 publication](https://onlinelibrary.wiley.com/doi/full/10.1111/ina.13018), [author’s analysis](https://raw.githubusercontent.com/marcelschweiker/ATHBv2/main/analysis_ATHB_v2.r).

**Discovered discrepancy:** The inspected `comf::calcATHBstandard` source includes an additional `load × adapted_met` term and repeats a `load × running_mean` term. Do not merge those terms into the selected formula or use that function as an interchangeable golden oracle. No author-confirmed erratum was established during this investigation. The research model structure supports selecting the `pythermalcomfort` term structure. [Inspected `comf` source](https://raw.githubusercontent.com/marcelschweiker/comf/master/R/calcATHBstandard.R).

Freeze this numerical identity:

| Item | Reference |
| --- | --- |
| Oracle package | `pythermalcomfort==4.4.2` |
| Oracle repository commit | `2597e88fed10fec2f40d49759ed9b74e10ce9f89` |
| Research analysis commit | `8cb4e7eabe25bc6dbbafa554a045ceacd56a865b` |
| Internal formulation identifier | `athb_2022_ptc_4_4_2` |
| Numerical contract version | `1` |

The production implementation must not fetch source code or model coefficients at runtime.

### 2.2 Inputs and output

Pure numerical input:

```text
tdb_c
tr_c
relative_air_speed_m_s
rh_pct
met
running_mean_c
clothing = automatic | fixed(clo)
```

Output:

```text
sensation_vote
adapted_met
effective_clo
thermal_load_w_m2
numerical_status
applicability_reasons
```

All numerical values use binary64 floats. Boolean values must not be accepted as numerical inputs. Automatic clothing uses a tagged configuration value, not a false-like numerical sentinel.

The sensation output is continuous. Do not clip it to `−3…+3`. Values outside that interval remain numerical outputs with appropriate applicability diagnostics.

### 2.3 ATHB equations

Let:

- `R` = outdoor running mean in °C.
- `m` = configured metabolic rate in met.
- `m_a` = adapted metabolic rate.
- `L` = adapted thermal load in W/m².

Physiological adaptation:

\[
m_a=m-\frac{0.234R}{58.2}
\]

Automatic clothing:

\[
clo_a=10^{-0.17168-0.000485R+0.08176m_a-0.00527Rm_a}
\]

For fixed clothing, use the configured `clo` directly; retain physiological adaptation and the ATHB transfer function.

Thermal sensation:

\[
\begin{aligned}
S={}&1.484+0.0276L-0.9602m_a-0.0342R\\
&+0.0002264LR+0.018696m_aR-0.0002909Lm_aR
\end{aligned}
\]

These are the selected implementation’s coefficients. Its public result is rounded to three decimals; ATHB shall retain the unrounded value internally for inverse solving. [Pinned ATHB implementation](https://raw.githubusercontent.com/pythermalcomfort/pythermalcomfort/v4.4.2/pythermalcomfort/models/pmv_athb.py).

### 2.4 Required heat-balance kernel

Implement only the heat-load subset needed by ATHB.

Use:

\[
M=58.15m_a,\qquad I_{cl}=0.155clo_a,\qquad W=0
\]

\[
f_{cl}=
\begin{cases}
1+1.29I_{cl},&I_{cl}\leq0.078\\
1.05+0.645I_{cl},&I_{cl}>0.078
\end{cases}
\]

The kernel’s vapor-pressure approximation is:

\[
p_a=10\,RH\exp\left(16.6536-\frac{4030.183}{T_a+235}\right)
\]

After solving clothing-surface temperature `T_cl`, calculate:

\[
\begin{aligned}
E_d&=0.00305(5733-6.99M-p_a)\\
E_{sw}&=\max(0,0.42(M-58.15))\\
E_{re}&=0.000017M(5867-p_a)\\
C_{re}&=0.0014M(34-T_a)\\
R_{cl}&=3.96f_{cl}\left[\left(\frac{T_{cl}+273}{100}\right)^4-
\left(\frac{T_r+273}{100}\right)^4\right]\\
C_{cl}&=f_{cl}h_c(T_{cl}-T_a)\\
L&=M-E_d-E_{sw}-E_{re}-C_{re}-R_{cl}-C_{cl}
\end{aligned}
\]

Use the pinned kernel’s clothing iteration, initialization, convection selection, and convergence criterion. Preserve its legacy `273` constants and the distinction between `58.2` in adaptation and `58.15` in heat-load conversion. Changing these would change the numerical contract. Cap clothing iteration at 150 updates; failure returns a typed numerical error. [Pinned heat-balance kernel](https://github.com/pythermalcomfort/pythermalcomfort/blob/v4.4.2/pythermalcomfort/models/_pmv_ppd_optimized.py).

No standalone PMV API, PMV entity, PPD calculation, cooling-effect model, or alternative comfort-model selector belongs in production.

### 2.5 Air speed

For measured or configured ambient air speed:

\[
v_r=
\begin{cases}
v,&m\leq1\\
v+0.3(m-1),&m>1
\end{cases}
\]

Use the **original activity value**, not physiologically adapted metabolism, for body-movement air speed. Match the reference utility’s three-decimal rounding in the second branch.

Advanced direct-relative-speed input bypasses this conversion exactly once. Globe calculations require ambient speed; relative speed is not a substitute. [Reference utilities](https://pythermalcomfort.readthedocs.io/en/latest/_modules/pythermalcomfort/utilities.html).

Do not additionally apply dynamic-clothing corrections to the selected automatic clothing formula.

### 2.6 Applicability and engineering domains

Separate three concepts.

**Research evidence:** Performance is strongest around indoor air/radiant temperatures of 14–27 °C and outdoor running means of 16–30 °C. Higher velocities and extreme activity/clothing values have less supporting data. These are evidence-strength observations, not hard physical boundaries. [Publication, applicability discussion](https://onlinelibrary.wiley.com/doi/full/10.1111/ina.13018).

**Research data filtering:** The author’s analysis retained approximately:

| Variable | Explicit filtering envelope |
| --- | ---: |
| Air temperature | 12.6–38.5 °C |
| Radiant temperature | 12.6–38.5 °C |
| RH | 16.9–87.7% |
| Air velocity | At most 1.9 m/s |
| Outdoor running mean | −2.7–41.3 °C |
| Radiant minus air temperature | −7.4…+9.2 K |
| Clothing | 0.1–2.0 clo |
| Metabolic rate | At most 2.6 met |

These marginal filters do not establish validity for every combination inside the rectangle. [Original research filtering](https://raw.githubusercontent.com/marcelschweiker/ATHBv2/main/analysis_ATHB_v2.r).

**Selected engineering evaluation envelope:**

| Input | Accepted numerical envelope |
| --- | ---: |
| Candidate air temperature | 5–40 °C |
| MRT | 0–50 °C |
| RH | 0–100% |
| Ambient or relative air speed | 0–2 m/s |
| Configured met | 0.8–2.0 |
| Fixed clothing | 0.1–2.0 clo |
| Computed automatic clothing | Greater than zero, at most 3.0 clo |
| Running mean | −30–45 °C |
| Adapted met | Greater than zero |

Outside the engineering envelope: return `outside_engineering_domain`; do not evaluate uncontrolled extrapolations.

Within the engineering envelope:

- Add `limited_evidence` outside the stronger research ranges.
- Add `extrapolated` outside the corresponding research filtering envelope.
- Evaluate applicability for current conditions and solved candidate conditions.
- Do not clamp outdoor running mean, clothing, or votes to conceal extrapolation.

**Default product policy:** Permit bounded extrapolated control, visibly labelled. An advanced `reject_extrapolation` option sends such decisions through the fixed-fallback path instead. This choice allows Dutch winter operation without pretending winter accuracy is established.

### 2.7 Runtime dependency decision

| Criterion | Runtime `pythermalcomfort` | Internal required subset |
| --- | --- | --- |
| Numerical reference | Direct upstream behavior | Must prove conformance |
| Dependency footprint | NumPy, SciPy, Numba and packaging dependencies | Python standard library |
| Installation risk | Scientific wheels and dependency constraints | Ordinary HA Python environment |
| Startup | Scientific imports; possible JIT work | Small deterministic imports |
| Solver precision | Public ATHB output is rounded | Unrounded internal result |
| Upgrade control | Upstream package changes can affect behavior | Explicit numerical contract upgrades |
| Maintenance | Upstream owns algorithms | Project owns a small frozen kernel and tests |

Version 4.4.2 declares NumPy `<2.3`, SciPy and Numba dependencies. Keep its oracle environment separate from HA’s Python environment. [Package metadata](https://pypi.org/project/pythermalcomfort/4.4.2/).

**Recommendation:** Internal implementation, without NumPy, SciPy or Numba in the production manifest. Preserve applicable notices when adapting permissively licensed source. Use the research R code for scientific comparison, not as production source to copy.

---

## 3. Psychrometric specification

### 3.1 Selected physical model

**Approximation:** During an inverse calculation, preserve vapor pressure.

At approximately constant atmospheric pressure, constant vapor pressure, constant humidity ratio, and constant dew point describe the same moisture constraint. Constant water mass per cubic metre does not remain equivalent as air density changes.

This is an instantaneous residential heating/cooling approximation. It does not predict ventilation moisture exchange, occupants’ moisture generation, or cooling-coil dehumidification.

For measured RH, use the current eligible observation. For fixed RH, use the explicit configured declaration at the current air temperature. The declaration defines the starting moisture state; it does not keep candidate RH constant. Preserve `declared` provenance through the derived moisture state.

Calculate:

\[
p_v=\frac{RH_0}{100}p_{ws}(T_0)
\]

For candidate temperature `x`:

\[
RH(x)=100\frac{p_v}{p_{ws}(x)}
\]

If pressure is available for diagnostics:

\[
w=0.621945\frac{p_v}{p-p_v}
\]

Pressure is not required for control because vapor pressure suffices. Do not silently create an atmospheric-pressure measurement.

### 3.2 Saturation-pressure equations

Use the ASHRAE-form equations implemented by PsychroLib, in SI units. With `K=T+273.15`:

For `T>0.01 °C`:

\[
\ln p_{ws}=
-\frac{5800.2206}{K}+1.3914993
-0.048640239K
+4.1764768\times10^{-5}K^2
-1.4452093\times10^{-8}K^3
+6.5459673\ln K
\]

For `T<=0.01 °C`:

\[
\ln p_{ws}=
-\frac{5674.5359}{K}+6.3925247
-0.009677843K
+6.2215701\times10^{-7}K^2
+2.0747825\times10^{-9}K^3
-9.484024\times10^{-13}K^4
+4.1635019\ln K
\]

Return pressure in Pa. Use the triple-point branch boundary, not an arbitrary discontinuity at zero. [PsychroLib implementation](https://psychrometrics.github.io/psychrolib/_modules/psychrolib.html).

Dew/frost point is the inverse of the same saturation function:

- Bracket from −100 °C to current dry-bulb temperature.
- Use bisection.
- Temperature tolerance: 0.001 K.
- Maximum iterations: 64.
- At zero RH, return `None` with `dry_limit`, not negative infinity.
- If the root is below the supported bracket, return `below_psychrometric_domain`.

### 3.3 Saturation and impossible states

- Measured or declared RH outside `[0,100]` is invalid; do not clamp it.
- Candidate RH exceeding 100% by more than `1e-6` percentage points is outside the constant-moisture model.
- Floating-point overshoot within that tolerance may be normalized to 100%.
- Restrict inverse-solver brackets to temperatures at or above the applicable dew point.
- Do not cross saturation by assuming condensation occurs.
- If saturation removes a required root, return `moisture_limited_no_solution`.
- Do not switch silently to constant RH.

The product shall ship one inverse humidity assumption: constant vapor pressure. A constant-RH option is excluded from the initial product because the integration does not own or verify humidity regulation.

### 3.4 Relationship to the ATHB kernel

The psychrometric module supplies physically transformed candidate RH. The frozen ATHB kernel retains its own vapor-pressure approximation for reference conformance.

Document this small approximation mismatch. Do not modify the heat-balance kernel to use a different vapor-pressure formula under the same numerical version.

### 3.5 Required checks

Examples:

- At 20 °C and 50% RH, `p_v≈1169.40 Pa`.
- Warming that air to 25 °C gives approximately **36.90% RH**.
- At a 12 °C surface, the corresponding equilibrium RH is approximately **83.37%**.

Test water/ice transition continuity, RH endpoints, inverse round trips, high humidity, and invalid pressure where optional humidity-ratio diagnostics are calculated.

---

## 4. Thermal and radiant model

### 4.1 Explicit input types

Use separate types:

```text
MeasuredAirTemperature
MeasuredRelativeHumidity
DeclaredRelativeHumidity
MeasuredMeanRadiantTemperature
MeasuredGlobeTemperature
MeasuredSurfaceTemperature
ModelledSurfaceTemperature
DerivedMeanRadiantTemperature
CriticalAirLocation
```

The configuration flow must ask what the source represents. It must not infer physical meaning from an entity name.

### 4.2 Radiant modes

| Mode | Current MRT | Candidate MRT during inverse solving |
| --- | --- | --- |
| Uniform environment | Primary/local air temperature | Moves with candidate air temperature |
| Direct MRT measurement | Valid measured MRT | Held constant for the snapshot |
| Globe-derived MRT | Derived from current globe, air and ambient speed | Hold the derived MRT constant |
| Surface composite | Fourth-power surface/background combination | Recompute using fixed measured surfaces and the specified background model |

**Default:** Uniform environment, labelled `estimated_uniform_radiant_environment`.

A globe is an instrument used to estimate the current radiant field. Do not keep its measured temperature fixed while repeatedly deriving a different radiant field from hypothetical room temperatures.

### 4.3 Globe mode

Support an explicit ISO 7726-style estimate:

\[
h_n=1.4\left(\frac{|T_g-T_a|}{D}\right)^{0.25}
\]

\[
h_f=6.3\frac{v^{0.6}}{D^{0.4}},\qquad h=\max(h_n,h_f)
\]

\[
T_r=
\left[(T_g+273.15)^4+\frac{h(T_g-T_a)}
{\epsilon\,5.67\times10^{-8}}\right]^{1/4}-273.15
\]

Configuration:

- Globe diameter: default 0.15 m; supported 0.04–0.15 m.
- Emissivity: default 0.95; supported 0.8–1.0.
- Ambient speed required.
- No direct solar exposure assumed.
- Nonpositive fourth-root radicand: invalid.
- Small globes receive an additional measurement-quality warning.

Specify `standard="ISO"` in reference tests; the upstream utility’s default is a different mixed-convection model. [Globe reference utility](https://pythermalcomfort.readthedocs.io/en/latest/_modules/pythermalcomfort/utilities.html).

### 4.4 Surface-composite MRT

For configured effective view factors:

\[
T_r=
\left[
\sum_iF_i(T_{s,i}+273.15)^4+
\left(1-\sum_iF_i\right)(T_b+273.15)^4
\right]^{1/4}-273.15
\]

Requirements:

- Each `F_i` is greater than zero and at most one.
- Sum of configured factors must not exceed one.
- Factors represent the occupant’s radiant exposure, not merely wall-area fractions.
- Background defaults to candidate primary air temperature.
- An optional measured background MRT is held constant.
- Missing configured surface data falls back to the declared uniform approximation, with a reason; do not silently redistribute its view factor.
- Maximum eight surface contributions per radiant model.
- This is a diffuse-environment approximation, not a full radiosity solution.

### 4.5 Internal surface and “critical-point” calculations

With valid indoor temperature and RH, ATHB can derive:

- Vapor pressure.
- Dew/frost point.
- Temperature corresponding to a selected surface-RH threshold.
- Surface RH when surface temperature is measured or explicitly modelled.

It cannot determine the coldest surface temperature from indoor/outdoor temperature alone.

For an optional building-specific model, configure:

\[
f_{Rsi}=\frac{T_s-T_o}{T_i-T_o}
\]

Then estimate:

\[
T_s=T_o+f_{Rsi}(T_i-T_o)
\]

Requirements:

- `f_Rsi` must be supplied or calculated from a user-entered calibration measurement.
- No fabricated default calibration factor.
- Require `0<f_Rsi<=1`.
- Calibration requires an indoor/outdoor difference of at least 5 K and a surface temperature between them.
- Use current outdoor temperature, not the adaptation running mean.
- Label output as a steady-state surface estimate with `estimated` provenance; add `modelled_surface` when this estimate contributes to MRT.
- Do not infer short-term surface response during weather transients.

The Home Assistant Mold Indicator similarly requires building-specific calibration; its critical point refers to a cold surface. [Mold Indicator documentation](https://www.home-assistant.io/integrations/mold_indicator/).

For surface diagnostics:

\[
RH_s=100\frac{p_v}{p_{ws}(T_s)}
\]

- Default high-surface-humidity threshold: 80%, explicitly a diagnostic policy.
- `RH_s ≥100%` indicates predicted saturation/condensation conditions.
- Preserve a supersaturation flag; display surface RH capped at 100%, retaining the uncapped finite ratio in diagnostics.
- Do not claim mold growth, exposure duration, material susceptibility, or health outcomes from one threshold crossing.
- These calculations do not independently raise heating demand.
- A modelled surface may influence MRT only when explicitly assigned a view factor. Compute its current estimate once per snapshot and hold that surface temperature fixed during the inverse solve; the declared radiant background retains its candidate behavior. This avoids treating a steady-state surface estimate as a dynamic building-response prediction.

No Mold Detector or template helper is required.

### 4.6 Critical local-air locations

Each configured critical-air location receives a separate ATHB evaluation.

It may provide:

- Local air temperature: required.
- Local RH: optional.
- Direct local MRT: optional.
- Control eligibility: monitoring, heating, cooling, or both.

Defaults:

- Same zone activity, clothing configuration, comfort boundaries and selected comfort strategy.
- Shared vapor pressure when no local RH exists.
- Local uniform MRT when the zone uses uniform radiant modelling.
- Otherwise the zone radiant field, labelled as shared.
- Maximum eight critical-air locations.

Using shared vapor pressure means local RH is recalculated at the local temperature. Do not reuse the primary RH percentage unchanged.

### 4.7 Mapping local conditions to a room target

For location `i`:

\[
\Delta_i=T_p-T_i
\]

Initialize the delta filter from the first valid sample of a new/recovered eligibility window, then filter with an exponential time constant of 10 minutes:

\[
\hat\Delta_{new}=\hat\Delta_{old}
+\left(1-e^{-\Delta t/600}\right)(\Delta_i-\hat\Delta_{old})
\]

For candidate primary temperature `x`:

\[
T_i(x)=x-\hat\Delta_i
\]

This is a short-horizon spatial approximation, not a building-response model.

Eligibility:

- All contributing samples must be valid and fresh.
- New/recovered locations require 10 minutes of valid observation and at least three reports before influencing control.
- Reject a raw primary/local difference exceeding 6 K for control; retain it diagnostically.
- Bound the effective delta to ±3 K.
- A heating location contributes only additional heating demand.
- A cooling location contributes only additional cooling demand.
- Monitoring-only locations never influence commands.

Maximum influence:

- Aggregate heating uplift: at most 2 K above the primary-location target.
- Aggregate cooling reduction: at most 2 K below the primary-location target.
- Additionally constrain critical influence by the primary comfort interval.
- Apply the cap after selecting the most demanding location; caps do not accumulate across sensors.
- Do not use weighting: eligibility, worst-demand selection, and explicit caps are more explainable.

Each location solves the same five sensation votes as the primary location. Its mapped heating root solves the selected heating control vote; its mapped cooling root solves the selected cooling control vote. A cold location is never assigned a different comfort strategy or required to reach thermal neutral.

A capped location generates `critical_demand_limited`; incompatible joint requirements generate `critical_locations_conflict`. Diagnostics identify its raw strategy demand, applied contribution and governing location. The exact policy formulas are in §7.

---

## 5. Outdoor adaptation and history

### 5.1 Running-mean algorithm

Use seven previous **complete local calendar days**:

\[
R_d=
\frac{\sum_{k=1}^{7}a_k\alpha^{k-1}\bar T_{d-k}}
{\sum_{k=1}^{7}a_k\alpha^{k-1}}
\]

Where:

- Default `alpha=0.8`.
- Advanced range: 0.6–0.9.
- `a_k=1` only for an eligible daily mean.
- Missing days retain their calendar-age position; never compress older observations into newer weights.

Keep unrounded values internally. Round only for entity display and comparison with the reference utility’s rounded public output.

The finite normalized estimator is an engineering choice. Do not substitute the infinite recursive form without changing the history-algorithm version.

### 5.2 Time-weighted daily means

Treat a valid observation as piecewise constant until the earliest of:

- The next observation.
- An explicit unavailable/invalid event.
- The maximum permitted observation age.

Default maximum outdoor hold: two hours.

Accumulate:

```text
integral_c_seconds
covered_seconds
day_start_utc
day_end_utc
observations
largest_uncovered_gap_seconds
provenance
```

Daily mean:

\[
\bar T=\frac{\int_{covered}T(t)\,dt}
{covered\ seconds}
\]

Daily eligibility requires at least 90% temporal coverage.

Coverage measures the integration’s documented observation-hold assumption; it is not proof that a physical sensor remained healthy throughout the interval.

### 5.3 History quality

| Condition | Result |
| --- | --- |
| Seven eligible days | `complete_history` |
| At least three eligible days, represented exponential weight ≥60%, and an eligible day among yesterday/the day before | `partial_history`; adaptive operation allowed |
| One or two eligible days, or insufficient represented weight | Diagnostic estimate only; fixed fallback for control |
| No eligible history | Running mean unavailable; fixed fallback if otherwise eligible |
| Outdoor source unavailable today but prior-day criteria still satisfied | Continue using history; report current-source outage |
| History no longer satisfies the criteria | Hold, then fallback |

Expose separately:

- Number of eligible days.
- Per-day temporal coverage.
- Fraction of seven-day exponential weight represented.
- Date of most recent eligible day.
- Bootstrap provenance.

### 5.4 Recorder bootstrap

Use a dedicated `RecorderHistoryReader` adapter.

For the 2026.9 baseline:

- Use `homeassistant.components.recorder.history.get_significant_states`.
- Execute through Recorder’s executor.
- Request full attributes.
- Set `significant_changes_only=False`.
- Set `minimal_response=False`.
- Use actual state timestamps; do not treat a synthetic start-state timestamp as a new observation.
- Query from the earliest required day minus the maximum hold interval.
- Use the same integration and validation algorithm as live collection.

These are exported Recorder history APIs, but compatibility must still be tested for each supported HA release. [Recorder history source](https://github.com/home-assistant/core/blob/2026.9.0/homeassistant/components/recorder/history/__init__.py).

Limits:

- One bootstrap operation per shared source.
- At most eight calendar days plus the two-hour lookback.
- Process day-sized queries sequentially.
- Overall result deadline: 30 seconds.
- Maximum accepted records: 100,000 per source.
- Discard results arriving after source-generation change or deadline.
- Do not query Recorder in steady-state operation.

Do not substitute long-term statistics when their aggregation or coverage semantics cannot reproduce the selected daily mean. Recorder absence is supported.

### 5.5 Shared collection and lifecycle

Use one `OutdoorHistorySource` per source identity, temperature attribute, timezone, and collection-policy fingerprint.

- Multiple zones share raw daily summaries.
- Different `alpha` values reuse those summaries.
- Use reference counting for listeners.
- Keep 35 days of summaries.
- Remove unused persisted source records after 35 days.
- A changed outdoor source begins a new history lineage.
- An entity rename with the same entity-registry identity retains lineage.
- Entity replacement, unknown identity reuse, or timezone change does not silently reuse history.

### 5.6 DST, restart, and corruption

- Store timestamps in UTC.
- Determine day boundaries using the configured HA timezone.
- Calculate day length from consecutive localized midnights: 23, 24, or 25 hours.
- Split intervals exactly at day boundaries.
- Never assume 86,400 seconds per calendar day.
- Persist the current accumulator every five minutes while dirty, and at rollover/unload.
- Reconstruct downtime from Recorder where possible; otherwise mark uncovered time.
- Do not fill a long shutdown interval with the last outdoor value.
- Reject duplicate dates, overlapping intervals, impossible coverage, nonfinite values, and incompatible schemas.
- Preserve corrupt storage for diagnosis; recover from valid summaries or Recorder without claiming full coverage.

---

## 6. Inverse solver specification

### 6.1 Required roots and terminology

For each valid location, solve these **five** roots for the selected comfort strategy:

| Output | Contract key | Sensation vote |
| --- | --- | ---: |
| Lower comfort boundary | `lower_comfort` | `comfort_vote_lower`, default −0.50 |
| Heating control target | `heating_control` | `lower_vote + f × (0 − lower_vote)` |
| Thermal neutral | `thermal_neutral` | 0.00 |
| Cooling control target | `cooling_control` | `upper_vote + f × (0 − upper_vote)` |
| Upper comfort boundary | `upper_comfort` | `comfort_vote_upper`, default +0.50 |

The **comfort band** spans the lower and upper comfort boundaries. The **control band** spans the heating and cooling control targets. These terms describe the raw solved bands; subsequent profile, critical-location, bound, calibration and grid effects are identified separately.

The outer sensation boundaries are product policy, not universal scientific ATHB comfort limits, satisfaction percentages or standards categories. Thermal neutral is a model reference and is not the default heating or cooling target.

Use the fixed strategy fractions in §7 to calculate the actual requested votes from the configured boundaries. Solve those votes directly through the ATHB candidate function. Do not interpolate temperatures between boundary and neutral roots. Normal calculation requires five roots for the selected strategy, not seven roots for all strategies; cross-strategy comparisons belong in tests or explicit previews.

### 6.2 Candidate function

For each location:

\[
f_q(x)=ATHB(T_i(x),T_{r,i}(x),v_{r,i},RH_i(x),m,R,clo)-q
\]

Freeze within one solve:

- Input snapshot and source provenance.
- Running mean.
- Adapted metabolism and clothing.
- Ambient/relative speed.
- Moisture state.
- Filtered local deltas.
- Measured and snapshot-modelled radiant/surface values.
- Selected comfort strategy, its fraction, configured outer votes, and all five requested votes.
- Configuration generation.

Candidate-dependent quantities are air temperature, transformed RH, and declared moving radiant-background contributions. Mapped critical roots use the primary room-temperature coordinate `x`; also retain the corresponding local candidate temperature for diagnostics.

### 6.3 Algorithm

Use **bracketed bisection**.

1. Begin with primary room search interval `[5,40] °C`.
2. Intersect it with local-air, radiant, and psychrometric domain constraints.
3. Evaluate 33 uniformly distributed points across the remaining interval and reuse those evaluations for all five roots.
4. Reject nonfinite evaluations or a decrease exceeding `1e-4` sensation units between adjacent samples.
5. Locate the unique adjacent sign-changing interval for each requested root; an exact sampled root may form a degenerate bracket.
6. Bisect each bracket. A merely small endpoint residual does not waive the temperature-width requirement.
7. Return success only when bracket width is at most `0.005 K` and final sensation residual is at most `0.002`.
8. Re-evaluate the forward sensation at the selected root as the final solver check.
9. Maximum 48 bisection iterations per root.
10. Maximum **3,000 ATHB evaluations per zone snapshot**, including current sensations, all locations and final checks. This accommodates five roots at up to nine locations; exceeding the budget remains a typed failure. Psychrometric domain searches do not invoke ATHB.

The preliminary sampling is a bracket and monotonicity check, not the temperature solver. It cannot mathematically prove global monotonicity; property tests must exercise the supported domain and candidate models. Iterate locations and roots in stable contract order so budget behavior is reproducible.

### 6.4 Failure results

Return typed results:

```text
success
below_search_domain
above_search_domain
moisture_limited_no_solution
no_bracket
multiple_brackets
non_monotonic
non_finite
heat_balance_non_convergence
iteration_limit
evaluation_budget_exceeded
outside_engineering_domain
```

Rules:

- Never return a search endpoint as a solved root merely because the true root lies outside the search interval.
- Preserve valid individual roots diagnostically, including when another required root fails.
- Primary adaptive control requires a complete, ordered five-root result for the selected strategy, even for a scalar actuator.
- An incomplete critical-location solution excludes that location from control.
- A primary solver failure enters hold/fallback.
- Keep raw roots separate from user and device limits.
- Store typed root failures rather than invented numerical placeholders.

> **Normative clarification:** The complete-five-root actuation requirement above is superseded by `ATHB_BUILD_CLARIFICATIONS.md` §2. ATHB should attempt all five roots for observability, but heating-only, cooling-only and ranged actuation have directional root requirements.

---

## 7. Product policy

### 7.1 Comfort strategy, profiles and precedence

Comfort strategy and profile are independent controls. **Comfort** is a strategy label; `comfort` is the occupied profile that applies the selected strategy without eco setback or boost.

| Comfort strategy | Stable key | Inward fraction `f` | Default heating vote | Default cooling vote | User explanation |
| --- | --- | ---: | ---: | ---: | --- |
| Efficient | `efficient` | 0.30 | −0.35 | +0.35 | Controls closer to the comfort boundary and prioritizes reduced conditioning. |
| Balanced | `balanced` | 0.50 | −0.25 | +0.25 | Controls halfway between the comfort boundary and thermal neutral in sensation space. |
| Comfort | `comfort` | 0.70 | −0.15 | +0.15 | Controls closer to thermal neutral and provides more comfort reserve. |

**Balanced is the default.** Fractions are product-defined presets, not editable tuning parameters. For advanced outer boundaries `l < 0 < u`:

\[
q_H=l+f(0-l),\qquad q_C=u+f(0-u)
\]

The default numerical votes in the table are examples of these formulas, not hardcoded alternatives to them. For example, boundaries `−0.60/+0.40` and Balanced produce `−0.30/+0.20`. The fraction is applied before solving temperatures.

Strategy expresses desired thermal-comfort position. It is independent of heating-system or emitter type, thermal inertia, expected overshoot, PID/TPI, modulation and minimum runtime. No such strategy selector or dynamic compensation is included. ATHB does not deliberately target thermal neutral in anticipation of equipment overshoot.

Provide profiles `auto`, `comfort`, `eco`, and `boost`.

Priority:

1. Disabled control or ownership inhibition.
2. Invalid mandatory actuation data.
3. Manual override.
4. Fallback when adaptive calculation is unavailable.
5. Explicit profile.
6. `auto` occupancy resolution.

`auto` resolves as follows:

- Configured occupancy/schedule entity `on` → `comfort`.
- `off` → `eco`.
- No entity → `comfort`.
- Unknown/unavailable → retain the last resolved profile for 30 minutes, then `comfort` with `occupancy_unknown`.

Schedules remain Home Assistant schedule helpers or automations. ATHB does not implement a scheduling engine. No dedicated legacy boost input is included; normal HA automations can select the native boost profile. Profile changes never change the selected comfort strategy or its sensation votes.

### 7.2 Directional baseline and critical-location targets

Let:

- `L`, `H₀`, `N`, `C₀`, `U` be the primary lower comfort, heating control, thermal neutral, cooling control and upper comfort roots.
- `Hᵢ`, `Cᵢ` be mapped critical-location roots at the **same `q_H` and `q_C`**, in primary room-temperature coordinates.
- `I = 2 K` be maximum aggregate critical influence.
- `m_b = min(0.25 K, (U − L)/4)` be the retained primary comfort-edge guard for critical influence only.

Primary baselines:

\[
H_0=\operatorname{root}_{primary}(q_H),\qquad
C_0=\operatorname{root}_{primary}(q_C)
\]

Heating-only `comfort` target:

\[
H_{cap}=\max(H_0,\min(H_0+I,U-m_b))
\]

\[
H=\min\left(\max(H_0,\max_{i\in heating}H_i),H_{cap}\right)
\]

Cooling-only `comfort` target:

\[
C_{floor}=\min(C_0,\max(C_0-I,L+m_b))
\]

\[
C=\max\left(\min(C_0,\min_{i\in cooling}C_i),C_{floor}\right)
\]

An empty eligible set contributes only the primary baseline. The guards cannot move a baseline outward; if a narrow configured comfort band makes an edge guard inconsistent with that baseline, retain the baseline and report `critical_constraints_inconsistent`. Caps apply once after worst-demand selection. They do not add across locations.

Report the primary as governing when no critical demand strictly changes the target. Otherwise name the location supplying the maximum heating/minimum cooling demand, even when its contribution is capped; resolve equal demands by stable location UUID. Publish requested and applied contributions separately. A capped demand receives `critical_demand_limited`. A rejected pathological location is ineligible, with `critical_delta_outlier`, rather than a source of extreme demand.

ATHB publishes the desired steady-state target whenever calculation is eligible, including while current sensation is inside the comfort band. There is no trigger that waits for the outer comfort boundary. Downstream climate control remains responsible for actual hysteresis, modulation and actuator timing. ATHB's command suppression limits repeated setpoint traffic; it is not an on/off thermostat algorithm.

### 7.3 Ranged conditioning and the control band

The normal primary active range is **`[H₀, C₀]`**, the solved **control band**. The wider `[L,U]` remains the comfort band. Do not use outer roots plus margins as the default range.

Use the `H` and `C` from §7.2 when eligible critical locations contribute. Require a gap `g` of at least 1 K, or a larger applicable configured/device requirement. If critical contributions conflict (`C − H < g`), discard both critical contributions, use `[H₀,C₀]`, identify the primary as governing, and report `critical_locations_conflict` plus the excluded demands. Never swap endpoints or enlarge critical influence.

If even the primary control band cannot meet `g`, suspend ranged/coordinated adaptive writes with `control_band_too_narrow`. Do not widen to the comfort boundaries or substitute thermal neutral. Later bounds and inward grid normalization must also preserve the gap; otherwise return `no_legal_range`. A scalar actuator in a zone without an opposing/ranged actuator need not satisfy a two-endpoint gap.

### 7.4 Eco

Defaults: heating setback 2 K; cooling setback 2 K.

```text
heating: H − heating_setback
cooling: C + cooling_setback
range: [H − heating_setback, C + cooling_setback]
```

Eco transforms the selected strategy's critical-adjusted targets and can deliberately leave the comfort band. Label it as policy; raw sensation roots remain unchanged.

### 7.5 Boost

Defaults: temperature delta 1 K; duration 60 minutes.

```text
heating: H + boost_delta
cooling: C − boost_delta
range: [H + b, C − b], where b = min(boost_delta, (C − H − g)/2)
```

The base range must already be valid. Limit `b` to a nonnegative value. If narrowing is limited or impossible, report `boost_limited` and retain the largest valid narrowing. Bounds and grid feasibility still apply afterward.

Boost is an explicit temporary profile transformation, not inferred equipment overshoot. Expiry restores the previous non-boost profile. Re-selecting boost restarts its duration; restart does not extend expiry. The selected comfort strategy stays unchanged.

### 7.6 Bounds and calibration

Default user command limits:

```text
minimum_control_temperature = 18 °C
maximum_control_temperature = 26 °C
```

These are conservative product defaults shown during setup, not medically safe limits for every household.

Order:

1. Calculate the five requested sensation votes and solve the five raw roots.
2. Select the directional strategy root(s).
3. Apply bounded critical-location contributions and coordinated-range conflict handling.
4. Apply profile transformation.
5. Apply environmental slew limits.
6. Add per-actuator fixed calibration offset.
7. Apply user command bounds.
8. Apply device bounds and inward grid normalization.
9. Check cross-actuator conflicts in room-reference coordinates.
10. Check ownership and freshness.
11. Dispatch only through the command broker.

The requested room target is the result through step 5, before actuator calibration. Preserve pre-slew and pre-bound values in the trace. User/device clamps are explicit policy limitations (`user_bound_applied`, `device_bound_applied`), not solved comfort temperatures. Bounds apply to the actual Celsius-equivalent command, including calibration.

Per-actuator calibration is static, default zero, explicitly configured within −3…+3 K. Do not derive automatic dynamic correction from radiator-mounted sensors. It adjusts actuator coordinates, not sensation votes.

### 7.7 Multiple actuators in one zone

Allow up to eight targets.

- Targets operating in the same direction receive corresponding per-actuator normalized targets.
- Simultaneously controllable heating and cooling targets use the coordinated **control-band** policy.
- A ranged actuator also puts the zone into this coordinated policy.
- Compare targets in the common room-reference coordinate, subtracting configured static offsets from actuator values.
- A manual or unavailable opposing actuator prevents writes that conflict with its observed setting.
- If an opposing target cannot be established, suspend affected coordinated writes with `opposing_target_unknown`.
- If known normalized targets violate the common required gap, suppress affected writes with `cross_actuator_conflict`.

This prevents contradictory ATHB requests. It does not claim physical interlocking of unrelated equipment.

### 7.8 Fallback policy

Default fixed fallback:

```text
heating = 18 °C
cooling = 26 °C
range = [18 °C, 26 °C]
```

Configuration validates these against user limits and the intended target shape/direction.

Fallback:

- Uses no fabricated ATHB result and is separate from comfort-strategy roots.
- Does not apply eco or boost.
- Requires valid fresh primary air temperature and eligible primary RH: a valid fresh measurement or a valid explicit fixed declaration.
- Requires available equipment, known capability mapping and ownership.
- Uses ordinary calibration, bounds, inward normalization and the command broker.
- Does not change HVAC mode.
- Is inhibited for cooling if its final room-equivalent normalized target lies below the current dew-point constraint.
- Can be replaced by advanced `fallback_policy=no_write`.

Fallback is available for missing adaptation history, rejected extrapolation or numerical/model failure. Missing/invalid primary temperature or absence of any valid selected RH source stops all writes, including fallback. A failed measured-RH source is not silently replaced with a fixed assumption; changing to a fixed declaration is an explicit configuration action.

---

## 8. Climate-control specification

### 8.1 Ownership scope

The initial product writes only:

```text
climate.set_temperature
```

With either:

```text
temperature
```

or:

```text
target_temp_low
target_temp_high
```

Never include `hvac_mode` in these calls.

ATHB does not call:

- `set_hvac_mode`
- `turn_on` or `turn_off`
- `set_humidity`
- Fan, preset, swing, or horizontal-swing services

The user or another explicitly designated HA automation controls whether equipment is in heat, cool, or another HVAC mode.

### 8.2 Capability/HVAC matrix

Feature flags are necessary but not sufficient: they are entity-level capabilities, not a universal per-mode contract. HA distinguishes `auto` from `heat_cool`; its canonical `auto` semantics concern device-controlled schedules or similar behavior. [HA mode and feature definitions](https://raw.githubusercontent.com/home-assistant/core/2026.9.0/homeassistant/components/climate/const.py).

| Current mode | Required capability/configuration | ATHB behavior |
| --- | --- | --- |
| `off` | Any | Suspend writes; preserve off |
| `heat` | `TARGET_TEMPERATURE` | Write single heating target |
| `heat` with range only | No single-target support | Unsupported; no improvised range write |
| `cool` | `TARGET_TEMPERATURE` | Write single cooling target |
| `cool` with range only | No single-target support | Unsupported |
| `heat_cool` | `TARGET_TEMPERATURE_RANGE` | Write both range endpoints atomically |
| `heat_cool` with single target only | Inconsistent/inadequate contract | Unsupported |
| `auto`, default | Any flags | Suspend; mode semantics unconfirmed |
| `auto`, explicitly declared single heating | `TARGET_TEMPERATURE`; confirmed adjustable in auto | Apply heating policy |
| `auto`, explicitly declared single cooling | Same, cooling semantics | Apply cooling policy |
| `auto`, bidirectional single target without a heating/cooling mapping | `TARGET_TEMPERATURE` | Unsupported; one scalar cannot express the two strategy targets |
| `auto`, explicitly declared range | `TARGET_TEMPERATURE_RANGE`; confirmed adjustable in auto | Apply ranged policy |
| `dry` | Temperature/humidity flags irrelevant | Suspend temperature control |
| `fan_only` | Any | Suspend temperature control |
| Unknown/unavailable | Any | Suspend; discard queued commands |
| Unrecognized mode | Any | Unsupported; report reason |

When both target flags exist, use the selected mode mapping; never send both scalar and ranged fields.

A bidirectional single-target auto mapping cannot express the selected control band. Suspend with `unsupported_auto_mapping` for all profiles unless the user declares a supported directional or ranged mapping. Do not infer direction from current room temperature or `hvac_action`, and do not substitute thermal neutral. Unmapped auto uses `unsupported_auto_mapping`; off uses `hvac_off`; other unsupported modes use `unsupported_hvac_mode`.

### 8.3 Capability snapshot

Read:

- Current mode and advertised modes.
- Supported features using HA enums.
- Scalar/range target values.
- `min_temp`, `max_temp`.
- `target_temp_step`.
- Availability and restoration state.
- Existing preset and current target for external-change detection.
- HA configured temperature unit.

Target humidity, fan and swing capabilities may appear in diagnostics but are not control inputs.

Do not access another integration’s private entity object to obtain hidden properties.

### 8.4 Units and grids

- Scientific calculations and policy use °C.
- Convert absolute temperatures to the HA service unit once.
- Convert temperature differences without adding an offset.
- State target temperatures and min/max values are interpreted in the HA unit.
- Use the state attribute `target_temp_step`, not the Python property name.
- Do not assume an exposed `precision` attribute exists or defines accepted increments.

The baseline HA implementation passes through the advertised step while converting displayed absolute temperatures. Therefore some mixed-unit integrations require explicit step-unit correction.

Per-actuator advanced options:

```text
step_override
step_unit = HA unit | °C | °F
grid_origin_override
minimum_range_gap
```

Defaults:

- Advertised step, interpreted in HA units.
- Grid origin at advertised minimum.
- If no step is advertised: proposed client step 0.5 °C or 1 °F, clearly labelled as an assumption.
- Enablement requires observable setpoint feedback; mismatches suspend control instead of guessing a new device grid.

### 8.5 Normalization contract

First apply user and device bounds to the continuous actuator-coordinate request and record each clamp. Convert once to HA service units, including step differences correctly. Let `G = {o + k·s}` be the legal grid within the intersection of user/device bounds, with configured/advertised origin `o` and positive step `s`.

Use **inward directional normalization**:

\[
Q_H(t)=\min\{g\in G:g\geq t\},\qquad
Q_C(t)=\max\{g\in G:g\leq t\}
\]

- Heating uses the smallest legal grid value at or above the bounded request.
- Cooling uses the largest legal grid value at or below the bounded request.
- An already legal value is unchanged.
- No legal value on the required side → `no_legal_inward_target`; no command.
- Do not select an outward value because it is nearer or because the inward grid point is unavailable.
- The rule applies equally to strategy, eco, boost and fallback requests. Explicit bound clamping is recorded before normalization; rounding must not add an unreported outward movement.

For a range, calculate `[Q_H(low), Q_C(high)]` and require the applicable minimum gap in common temperature-difference units. This pair maximizes the remaining gap among all inward choices. If it fails, no more inward pair can succeed: return `no_legal_range`. Do not widen outward, minimize distance to a different pair, swap endpoints or use the outer comfort band.

Use integer grid indices with a documented floating-point tolerance of `1e-9` HA temperature units for representational equality at a grid point; bounds checks use the same tolerance and retain the exact canonical grid value. This is numerical equality handling, not a temperature deadband. Limit the grid to 2,000 points; a finer unsupported grid requires configuration correction.

Example: a heating request of 19.362709 °C on a 1 °C grid becomes **20 °C**, although 19 °C is nearer. A cooling request of 23.481038 °C on a 2 °C grid with origin 18 °C becomes **22 °C**, although 24 °C is nearer. A range on that grid becomes `[20,22] °C`. With a 4 °C step and the same origin, both inward endpoints become 22 °C and the range is suppressed. These examples concern representation of a steady-state target, not predictions of emitter response.

### 8.6 Anti-chatter defaults

| Control | Default |
| --- | ---: |
| Environmental debounce | 2 seconds |
| Maximum debounce delay during continuous updates | 10 seconds |
| Quantization release hysteresis | 0.1 K, as defined below |
| Minimum meaningful change | Greater of one grid step and 0.2 K |
| Ordinary minimum command interval | 60 seconds per actuator |
| Hard minimum interval for explicit policy changes | 10 seconds |
| Environmental target slew | 0.5 K per 10 minutes |
| Acknowledgement deadline | 30 seconds |
| Service-call deadline | 15 seconds |
| Pending command count | One per actuator |
| Queued future command count | One latest intent per actuator |

The computed target is always published independently of command suppression. These controls limit setpoint updates; they do not switch equipment or wait for current sensation to cross a comfort boundary.

Use directional release hysteresis compatible with §8.5. For an ordinary environmental update that would lower a previously acknowledged heating target, release to the newly normalized grid value only when the continuous bounded request is at least 0.1 K below that new grid value. For an update that would raise a cooling target, require the request at least 0.1 K above that new grid value. A change demanding more conditioning does not wait for this extra release margin. For ranges, apply both endpoint tests and suppress the atomic pair if a changing endpoint fails its release test. Never construct a hybrid pair from old and new endpoints.

After normalization, an unchanged acknowledged request produces `target_unchanged` and no write. A smaller-than-meaningful change produces `below_minimum_change`; a failed release test produces `quantization_hysteresis`; a rate-limited current intent produces `command_interval` while the one-slot latest queue waits. Evaluate these reasons in this order after eligibility checks. Startup has no acknowledged target to supply hysteresis/slew history; never treat the observed setpoint as a persisted ATHB command.

Explicit enable/resume/profile/**comfort-strategy** transitions bypass environmental slew, release hysteresis and the ordinary interval, but retain the hard interval, grid validity, meaningful-change suppression and all ownership checks. Strategy changes create a new configuration/input generation and invalidate old roots and queued intents immediately. Disabling and external manual intervention inhibit dispatch immediately without debounce.

---

## 9. Control ownership state machine

### 9.1 Separate ownership from data health

Maintain orthogonal state:

```text
Ownership:
  DISABLED
  OWNED
  MANUAL_OVERRIDE
  RECONCILING
  COMMAND_FAULT

Data readiness:
  READY
  DEGRADED_READY
  HOLD_LAST_GOOD
  FALLBACK_READY
  INVALID

Target readiness:
  AVAILABLE_SUPPORTED
  SUSPENDED_MODE
  UNAVAILABLE
  INCOMPATIBLE
```

The user-facing control status is derived from these states. Avoid one enum whose transitions mix unrelated causes.

Ownership is per actuator. The zone switch gates all its actuators.

### 9.2 Transitions

| Current state/event | New ownership state | Required behavior |
| --- | --- | --- |
| New setup | `DISABLED` | Calculate previews; no climate writes |
| User enables control | `RECONCILING` | Validate data, target identity, capabilities and stored state |
| Reconciliation succeeds | `OWNED` | Compute a fresh intent |
| User disables | `DISABLED` | Close dispatch gate immediately; discard queued intent |
| External target change | `MANUAL_OVERRIDE` | Inhibit that actuator |
| External HVAC-mode change | `MANUAL_OVERRIDE` | Respect mode and setpoint; cancel pending future intent |
| External preset change | `MANUAL_OVERRIDE` | Respect the user/device policy change |
| Override expires | `RECONCILING` | Fresh validation before resuming |
| Resume button | `RECONCILING` | Clear override; do not bypass invalid data |
| Target unavailable | Ownership retained separately | Inhibit; reconcile on return |
| Target replaced | `DISABLED` for that target | Require explicit configuration/enablement |
| Command rejected or outcome uncertain | `COMMAND_FAULT` | No automatic resend loop |
| Clean restart with matching state | `RECONCILING` | Restore intent to operate, not a command |
| Unclean shutdown or unresolved persisted command | `RECONCILING` with resume required | Do not assume control continuity |

> **Normative clarification:** The external-HVAC-mode and preset rows above are superseded where applicable by `ATHB_BUILD_CLARIFICATIONS.md` §1. External HVAC-mode changes update readiness/reconciliation and do not by themselves create a manual temperature-target override.

Default override duration: two hours. Advanced options permit 15 minutes–24 hours or “until resumed.”

Each new external intervention restarts the expiry. Ordinary temperature/current-action updates do not.

### 9.3 Command identity

Every proposed command carries:

```text
command_id
target_registry_identity
entry_generation
input_generation
capability_generation
ownership_revision
expected_pre_command_target
requested_normalized_target
created_at
expires_at
HA context_id
```

Before dispatch:

1. Create a fresh HA `Context`.
2. Register pending correlation before calling the service.
3. Persist the command intent.
4. Recheck all generations and ownership after persistence.
5. Dispatch only if still current.

Use context ID and parent-ID correlation for attributable feedback.

### 9.4 Recognizing external changes

Observe both:

- Climate state changes.
- Relevant external service-call events.

For external service targeting, use HA’s target-selection helpers so entity, device, area, floor, and label targeting are handled consistently. Do not maintain a separate incomplete target resolver. [HA target helpers](https://raw.githubusercontent.com/home-assistant/core/2026.9.0/homeassistant/helpers/target.py).

Rules:

- A known external setpoint/mode/preset service call preempts ATHB even before feedback arrives, subject to the refined ownership classification in `ATHB_BUILD_CLARIFICATIONS.md`.
- A context-matching state update is an ATHB acknowledgement candidate.
- Contextless feedback matching the single pending expected value may be accepted as inferred acknowledgement, provided no external revision occurred.
- A contextless contradictory target change is external/ambiguous and causes override.
- An external call requesting the same numeric value still expresses external ownership intent when it targets the controlled temperature target.
- Matching an old command value is not, by itself, proof of ATHB origin.

HA cannot make “check ownership and command another integration” globally atomic. A dispatched command may already be in flight when a manual change occurs. ATHB must stop subsequent writes, expose the race, and avoid claiming perfect origin detection.

### 9.5 Acknowledgements and failures

- Service completion is not proof of device acceptance.
- Accept acknowledgement only when the observed target matches the normalized intent within the stricter of one-quarter step and configured feedback-resolution tolerance.
- Own-context feedback with a different value is `coerced_or_rejected`.
- Unexpected external target feedback causes manual override.
- Timeout produces `command_outcome_unknown`.
- Do not automatically resend after an uncertain outcome.
- A late matching acknowledgement may resolve the fault only if identity/generation still match and no external intervention occurred.
- Persistent rejection requires explicit resume or a relevant configuration/capability change.

Retries are limited to rebuilding an intent that was cancelled **before dispatch**. There is no blind timed retry after a possibly executed write.

---

## 10. Failure and recovery state machine

### 10.1 Input validation

Every entity-backed measurement source must pass:

- Entity/attribute exists.
- State is not unknown or unavailable.
- Numeric parsing yields a finite non-Boolean value.
- Unit is declared and convertible.
- Value satisfies physical/source limits.
- Required freshness evidence is present.
- Jump/outlier checks pass.

Source screening defaults:

| Source | Screening range | Freshness |
| --- | ---: | ---: |
| Primary/local air | −20…60 °C | 30 minutes |
| Measured RH | 0–100% | 30 minutes |
| Direct MRT | −20…80 °C | 30 minutes |
| Surface/globe | −30…100 °C | 30 minutes |
| Outdoor | −60…60 °C | 2 hours |
| Air speed | 0–2 m/s | 30 minutes |

These screening ranges precede the narrower model-evaluation envelope.

Fixed configuration values are a distinct source type. `rh_source` is a tagged choice: `measured(entity_identity)` or `declared(value_pct)`. Direct declarations require finite non-Boolean values in `[0,100]`, no HA entity, and provenance exactly `declared`. Their validity is tied to the current validated configuration generation; they have no fabricated observation timestamp or sensor-freshness expiry. The actual primary temperature must still be measured/observed and fresh. Fixed air speed, met, fixed clothing and other explicitly supported fixed quantities follow the same declaration principle and their own domains. A helper that explicitly represents a user declaration retains declared provenance; its existence never proves a physical measurement.

No RH mode/value is invented for an absent configuration. A measured source that fails remains invalid; a fixed assumption is used only after explicit selection of declared mode. There is no automatic RH-mode switch or numeric fallback to 50%.

Jump quarantine:

- Indoor/local air: more than 3 K within five minutes.
- RH: more than 25 percentage points within five minutes.
- Outdoor: more than 8 K within five minutes.
- Radiant/surface/globe: more than 10 K within one minute.

A quarantined change requires three mutually consistent reports spanning at least 60 seconds. Consistency means within 1 K, or five RH percentage points. Otherwise the source remains invalid.

### 10.2 Failure behavior

| Failure | Immediate action | After hold/recovery |
| --- | --- | --- |
| Primary air invalid/stale | Stop all writes; `HOLD_LAST_GOOD` for explanation only | After 15 minutes: `INVALID`; no fixed fallback |
| Selected primary RH missing/invalid, or measured RH stale | Same; an explicit valid declared mode is eligible without a sensor | Same; no implicit RH substitution |
| No usable outdoor history | Adaptive result unavailable | Fixed fallback if actuation inputs valid |
| Outdoor unavailable but usable history remains | Continue adaptive calculation | Quality reason; reassess at rollover |
| Configured MRT unavailable | Use declared uniform approximation | `DEGRADED_READY`; normal slew applies |
| Globe/surface radiant derivation invalid | Same declared approximation | Never relabel as measured MRT |
| Optional local critical source invalid | Remove its control eligibility | Re-entry requires the eligibility warm-up |
| Configured air-speed sensor invalid | Do not silently replace with fixed speed | Hold, then fixed fallback |
| Occupancy unavailable | Retain resolved profile for 30 minutes | Then comfort with explicit reason |
| Primary inverse solver fails | No new adaptive write; hold for 15 minutes | Fixed fallback if eligible, subject to directional-root clarification |
| Extrapolation rejected by policy | Same | Fixed fallback |
| Target unavailable | No writes; cancel queued intent | Reconcile on return |
| Unsupported HVAC mode | No writes | Reconcile after an eligible mode is observed |
| Internal unexpected exception | Inhibit affected decision/actuator | Log once; recompute on fresh event; persistent fault raises repair |
| Storage ownership verification fails | Inhibit new writes | Repair required; no assumption that persistence succeeded |
| History storage fails | Keep valid in-memory history, visibly degraded | Restart must rebuild; ownership-storage failure still blocks writes |

**Hold means no climate write.** It does not mean repeatedly restoring an old setpoint. The underlying climate keeps its existing state.

### 10.3 Recovery

- Primary input recovery requires two valid reports at least 30 seconds apart.
- A fixed configured declaration is validated directly and labelled `declared`; no two-report sensor recovery rule applies to it. An explicit change of declaration creates a new configuration generation and triggers normal reconciliation.
- Recompute from a new coherent snapshot.
- Never replay a persisted comfort result as current data.
- Recovery does not clear a manual override.
- Recovered critical locations complete their longer eligibility warm-up.
- Recovered targets reconcile their current setpoint before accepting control.

### 10.4 Quality representation

Use categorical facts:

```text
measured
declared
estimated
partial_history
limited_evidence
extrapolated
stale
invalid
actuator_unconfirmed
```

Store multiple reasons. Maintain separate fields for:

- Numerical validity.
- Data provenance.
- Scientific applicability.
- Control eligibility.

A uniform-MRT approximation can be operationally eligible without being a measured radiant environment.

---

## 11. Home Assistant architecture

### 11.1 Integration structure

One config entry per zone is preferred over an installation entry with subentries:

- Independent reload and override lifecycle.
- Natural room/device organization.
- No account or connection resource requiring a parent entry.
- Shared registries still eliminate duplicate environmental collection.

HA subentries exist, but do not improve this product enough to justify a shared parent failure domain.

Use typed `ConfigEntry.runtime_data` for zone runtime state and domain-level storage only for shared registries. [HA runtime-data guidance](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/runtime-data/).

### 11.2 Proposed package structure

```text
custom_components/athb/
├── __init__.py
├── manifest.json
├── const.py
├── config_flow.py
├── config_schema.py
├── runtime.py
├── controller.py
├── entity.py
├── sensor.py
├── binary_sensor.py
├── switch.py
├── select.py
├── button.py
├── diagnostics.py
├── repairs.py
├── strings.json
├── translations/
│   ├── en.json
│   └── nl.json
├── core/
│   ├── contracts.py
│   ├── validation.py
│   ├── heat_balance.py
│   ├── athb.py
│   ├── psychrometrics.py
│   ├── radiant.py
│   ├── locations.py
│   ├── inverse.py
│   ├── history_math.py
│   ├── policy.py
│   └── ownership.py
└── adapters/
    ├── sources.py
    ├── outdoor_history.py
    ├── recorder.py
    ├── climate.py
    ├── broker.py
    └── storage.py

tests/
├── numerical/
├── history/
├── policy/
├── control/
├── virtual_installations/
│   ├── test_scenarios.py
│   ├── schema.py
│   ├── runner.py
│   └── conftest.py
├── home_assistant/
└── fixtures/
    ├── reference/
    └── virtual_installations/

docs/
├── architecture.md
├── scientific-model.md
├── operations.md
└── validation.md
```

This is a proposed structure, not scaffolding to generate during planning.

### 11.3 Responsibilities

| Component | Responsibility |
| --- | --- |
| `SourceRegistry` | Shared source listeners, unit extraction, reports and provenance |
| `InputValidator` | Pure validation and quarantine decisions |
| `OutdoorHistoryRegistry` | Shared source acquisition and daily persistence |
| `RecorderHistoryReader` | Version-tested historical bootstrap |
| `Psychrometrics` | Vapor pressure, dew point and candidate RH |
| `RadiantModel` | Direct/derived MRT and explicit candidate behavior |
| `LocationAssembler` | Primary/critical environments and eligibility |
| `AthbEngine` | Selected forward formulation only |
| `InverseSolver` | Deterministic roots and typed failures |
| `ComfortPolicy` | Profiles, governing location, bounds and coordination |
| `ClimateAdapter` | HA capability and unit contract |
| `OwnershipReducer` | Pure ownership transitions |
| `CommandBroker` | Sole climate-writing path |
| `StorageRepository` | Versioned persistence and verification |
| `ZoneController` | Snapshot orchestration and publication |
| Entity platforms | Presentation and explicit user actions |

### 11.4 Core contracts

Use immutable dataclasses and tagged result types:

```text
Observation
  source_identity, value, unit, observed_at, received_at,
  provenance (measured | declared | estimated), validity, reasons

EnvironmentalSnapshot
  generation, primary, critical_locations, radiant_model,
  moisture_states, activity, clothing, air_speed,
  running_mean, history_quality, input_expiries

RootSet
  lower_comfort, heating_control, thermal_neutral, cooling_control, upper_comfort
  each: requested_vote, mapped_room_temperature_c or typed_failure,
        local_candidate_temperature_c, residual, bracket_width, evaluation_count

LocationResult
  location_id, current_vote, RootSet, applicability, eligibility

PolicyDecision
  snapshot_generation, comfort_strategy, inward_fraction, heating_control_vote,
  cooling_control_vote, profile, governing_locations,
  primary_five_roots, critical_adjusted_targets, transformed_targets, limitations

NormalizedIntent
  target_identity, scalar_or_range, HA_unit,
  capability_generation, ownership_revision, expiry

CommandOutcome
  command_id, dispatch_status, acknowledgement_status, reason

ZoneSnapshot
  sequence, calculation, policy, actuator_states, quality
```

Pure core modules must not import Home Assistant or invoke services, storage, wall clocks, or global mutable configuration.

### 11.5 HA lifecycle and manifest

- `config_flow: true`.
- `integration_type: helper`.
- `iot_class: calculated`.
- Empty third-party production requirements.
- Versioned custom-integration manifest.
- Required climate component; Recorder is optional and loaded through an isolated adapter.
- Forward the five entity platforms (sensor, binary sensor, switch, select and button) in one setup call.
- Use `async_unload_platforms` and registered unload callbacks.
- Do not fail config-entry setup merely because source entities are temporarily unavailable.
- Use `OptionsFlowWithReload` where appropriate; the normal strategy select follows the lightweight runtime clarification.
- Support `async_migrate_entry` from the first released schema onward.
- No YAML setup, import flow, authentication, or reauthentication.
- No custom climate entity wrapping the target.
- No custom service is necessary initially: standard switch/select/button actions cover control.

Manifest and flow behavior follow the [HA manifest](https://developers.home-assistant.io/docs/creating_integration_manifest/), [config-flow](https://developers.home-assistant.io/docs/core/integration/config_flow/), and [options-flow](https://developers.home-assistant.io/docs/core/integration/options_flow/) contracts.

Use the canonical repository identity in §1 for project metadata. Packaging does not authorize a push, publication or release; those remain separate explicitly authorized actions. Do not create another remote repository.

---

## 12. Config-flow specification

### 12.1 Normal setup

| Step | Fields | Behavior |
| --- | --- | --- |
| Zone | Name; optional area | Generate immutable zone UUID |
| Environment | Primary temperature; RH source mode; measured RH entity **or direct fixed RH value**; outdoor source | Recommend measured RH; explain declarations and units |
| Climate targets | One or more climates | Show discovered modes, target types, bounds and step |
| Comfort | Activity preset; **Comfort strategy**: Efficient, Balanced, Comfort | Balanced selected by default; explain position in sensation space |
| Control | Occupancy source, command limits, fallback values | Explain target-only ownership |
| Review | Input quality, all five votes and solved temperatures, target capability summary | Save with control disabled |

Temporarily unavailable entities may be saved for monitoring, but activation enforces runtime eligibility. If preview calculation fails, show its typed reason; do not insert illustrative temperatures as real results.

### 12.2 Defaults and validation

| Field | Default | Validation |
| --- | --- | --- |
| Primary temperature | Required | `sensor` or temperature-declared `input_number`; declared unit and source provenance |
| Primary RH source mode | Required; measured recommended | Tagged measured entity or fixed declared value; exactly one active alternative |
| Measured RH | Required in measured mode | Appropriate entity with percent semantics and explicit source provenance |
| Fixed RH declaration | No implicit value | Direct finite non-Boolean 0–100%; required only in declared mode; no helper |
| Outdoor source | Required | Temperature sensor/helper or `weather.temperature` with unit |
| Targets | Required | 1–8 distinct climate entities |
| Occupancy | None | Binary sensor, input Boolean or schedule entity |
| Met | Fixed 1.1 | Declared; 0.8–2.0 |
| Clothing | Automatic | Fixed declared alternative 0.1–2.0 clo |
| Ambient air speed | Fixed 0.1 m/s | Declared directly; 0–2 m/s; measured alternative supported |
| Relative-speed mode | Off | Explicit advanced declaration |
| Comfort strategy | Balanced | Exactly `efficient`, `balanced`, `comfort`; fixed fractions 0.30/0.50/0.70 |
| Lower comfort boundary vote | −0.50 | Advanced; −1.0…−0.05 |
| Upper comfort boundary vote | +0.50 | Advanced; +0.05…+1.0 |
| MRT | Uniform approximation | Direct, globe or surface alternatives |
| Critical locations | None | At most eight; physical type required |
| Heating/cooling setback | 2 K each | 0–5 K |
| Boost delta | 1 K | 0–3 K |
| Boost duration | 60 minutes | 5–180 minutes |
| Command limits | 18–26 °C | Ordered; within engineering configuration range 5–35 °C |
| Fallback | Fixed 18/26 °C | Valid within configured command bounds |
| Manual override | 120 minutes | 15–1,440 minutes or until resumed |
| Running-mean alpha | 0.8 | 0.6–0.9 |
| Reject extrapolation | False | Explicit advanced policy |
| Calibration offset | 0 K | −3…+3 K per target |
| Auto-mode mapping | Suspended | Explicit supported directional or ranged mapping required |
| Control enabled | False | Separate deliberate activation |

The outer votes must straddle zero. Strategy formulas keep the heating vote strictly between lower and zero, and cooling strictly between zero and upper. Do not expose a personal thermal bias or editable inward fraction. A narrow configured band may be unusable for a ranged actuator; preview the actual gap failure rather than silently changing the votes.

### 12.3 Advanced configuration

Expose grouped options for:

- Outer comfort boundaries and extrapolation policy.
- Radiant measurement type and properties.
- Local-air locations and eligibility.
- Surface-risk calibration and view factors.
- Air-speed source mode and measurement semantics.
- Per-measured-source freshness limits.
- Per-target grid, unit correction, calibration and minimum range gap.
- Override duration and fallback policy.
- Debounce, minimum meaningful change, minimum command interval and slew.

Keep numerical-kernel constants, strategy fractions, root tolerances, iteration caps and evaluation budgets fixed by implementation version. They are not user tuning controls. Allow measured-source freshness intervals from five minutes to six hours and explain the observation-hold assumption. Fixed configuration declarations do not use a freshness timer.

> **Normative UX clarification:** Radiant/MRT/cold-surface options use progressive disclosure per `ATHB_BUILD_CLARIFICATIONS.md` §4. The normal setup path remains simple and does not expose view-factor complexity unless the user explicitly chooses an advanced surface/radiant model.

### 12.4 Reconfiguration and strategy changes

- Required source modes/identities, direct RH declaration and target changes use reconfigure.
- Optional behavior changes use options.
- Store the selected comfort strategy authoritatively in `ConfigEntry.options.comfort_strategy`, defaulting to Balanced on initial creation.
- The strategy select updates that same authoritative value through one serialized controller configuration-update path. **Per `ATHB_BUILD_CLARIFICATIONS.md`, a normal strategy-select change must not unload/reload the config entry.**
- Changes create a new configuration generation and invalidate calculations and queued commands.
- Entity renames follow registry identity; replacement entities require explicit selection.
- Changing the outdoor source starts/reuses the matching new history lineage.
- Do not delete/recreate zone entities for ordinary options changes.
- Config-flow validation produces previews and never writes to climates.

### 12.5 Duplicate control prevention

A target climate identity may belong to only one enabled ATHB zone.

Enforce through config-flow duplicate detection, a domain-level runtime lease and a final broker lease check.

Reject direct self-references to ATHB output entities as mandatory environmental inputs. Arbitrary external template dependency cycles cannot be fully discovered; document that limitation.

---

## 13. Entity model

### 13.1 Enabled by default

Per zone:

| Entity | State |
| --- | --- |
| Thermal sensation sensor | Current primary ATHB vote |
| Heating control target sensor | Primary raw root at the selected heating control vote |
| Thermal neutral sensor | Primary raw root at vote zero |
| Cooling control target sensor | Primary raw root at the selected cooling control vote |
| Comfort status sensor | `cold`, `comfortable`, `warm`, `mixed`, `unknown` |
| Control status sensor | Derived operational state |
| Outdoor running-mean sensor | Current usable or explicitly partial running mean |
| Adaptive control switch | User control intent |
| Comfort strategy select | **Efficient**, **Balanced**, **Comfort**; Balanced default |
| Profile select | `auto`, `comfort`, `eco`, `boost` |
| Resume control button | Resume eligible overridden/faulted targets |

Per target:

- Effective target-temperature sensor for scalar control.
- Effective target-low and target-high sensors for ranged control.
- Only the applicable shape is available.

“Effective target” means the current normalized ATHB request, not a guarantee that hardware reached it. Acknowledgement is reported separately.

For several targets, do not publish one “actual effective target” that hides different device values.

### 13.2 Disabled by default

- Lower/upper comfort boundaries.
- Effective MRT.
- Adapted metabolism and clothing.
- Relative speed.
- Vapor pressure and dew point.
- Surface temperature/RH risk.
- Per-critical-location sensation and mapped demand.
- History coverage.
- Per-actuator control status.
- Last acknowledged ATHB target.
- Solver evaluation count.

### 13.3 State, attributes, diagnostics, and logs

**Entity state:** One stable scalar or categorical value.

**Small attributes:** Selected comfort strategy, effective profile, root sensation vote where applicable, compact quality reasons, governing location label and current target role. Heating/cooling control-root sensors are primary raw model roots; per-actuator effective-target sensors show the transformed and normalized request.

**Diagnostics only:** Raw observations, timestamps, per-location root details, command contexts, full capability snapshots, history rows, decision traces.

**Logs:** Transitions and actionable failures.

Do not add a changing timestamp, full trace, or growing command history to every sensor update.

Temperature sensors use appropriate units and device classes. Thermal sensation has no invented HA device class. Only declare measurement state classes where meaningful.

Entity IDs use immutable identifiers:

```text
zone_uuid + entity_key
zone_uuid + target_uuid + entity_key
zone_uuid + location_uuid + entity_key
```

Names and source entity IDs are not unique-ID components.

On invalid thermal data, current numerical entities become unavailable. Last-known-good values remain identifiable in diagnostics rather than masquerading as current measurements.

---

## 14. Persistence model

### 14.1 Configuration

`ConfigEntry.data` stores:

- Zone UUID.
- Required source identities and selections, including the tagged primary RH source with either its measured identity or its direct fixed declared value.
- Target UUIDs and identities.

`ConfigEntry.options` stores:

- Selected comfort strategy, outer comfort boundaries, radiant, local-location and control settings.
- Advanced validation/stability configuration.
- Fixed fallback values.

Do not duplicate these as authoritative runtime configuration.

### 14.2 Outdoor storage

Per shared source:

```text
schema_version
algorithm_version
source_uuid
source_identity
source_generation
timezone
collection_policy_fingerprint
daily_summaries[maximum 35]
current_day_accumulator
last_valid_observation
last_integrated_utc
storage_generation
```

Each daily summary includes bounds, integral, coverage, maximum gap, and provenance.

### 14.3 Zone recovery storage

Persist:

```text
schema_version
zone_uuid
configuration_fingerprint
storage_generation
clean_shutdown
control_enabled_intent
selected_profile
previous_non_boost_profile
boost_expiry_utc
last_good_calculation_summary
last_good_input_timestamps
per_target:
  target_uuid
  target_identity
  override_expiry_utc or until_resumed
  override_reason
  last_observed_target_fingerprint
  last_command_id
  last_command_payload
  last_command_context_id
  dispatch/acknowledgement status
  command timestamps
```

Last-good calculations are explanatory recovery data, never executable commands.

Do not persist listeners, locks, tasks, monotonic timestamps, caches, or a replay queue.

### 14.4 Atomicity and failure detection

Use HA `Store` for versioned storage, with one writer lock per store.

For control-critical writes:

- Await saving.
- Read the serialized store back through the executor.
- Verify the expected storage generation and payload identity.
- Inhibit dispatch if verification fails.

This verification is necessary because the inspected HA storage implementation can log write errors internally without propagating them from the public save call.

Persist pending intent before dispatch. Persist disable/override/profile changes promptly. Strategy changes update the authoritative config-entry option and configuration fingerprint; stale persisted recovery summaries cannot restore an old strategy. Coalesce noncritical diagnostic saves.

At startup:

1. Load and validate state.
2. Detect unresolved command or unclean prior shutdown.
3. Persist the new run as unclean before enabling dispatch.
4. Reconcile live target state.
5. Mark clean only during successful controlled unload/shutdown.

A corrupted control store requires explicit resume after recovery. A corrupted history store may rebuild independently.

### 14.5 Removal

- Unloading a zone releases listeners and leases without deleting its history.
- Removing a config entry removes its zone recovery store.
- Shared outdoor data remains while referenced.
- No cleanup may delete unrelated Recorder or helper data.

---

## 15. Event and concurrency model

### 15.1 Subscriptions

Use indexed listeners for:

- Environmental `state_changed`.
- Relevant unchanged-state reports for freshness.
- Outdoor reports and availability.
- Occupancy/schedule state.
- Climate target, mode, preset and capability changes.
- Relevant external climate service calls.
- Entity-registry renames/removals.
- HA startup, shutdown and configuration-unit/timezone changes.

Use `State.last_reported` where appropriate instead of interpreting a long-unchanged temperature as automatically stale. Report freshness still concerns HA observations, not independently proven physical sampling. [HA reporting timestamps](https://developers.home-assistant.io/blog/2024/03/20/state_reported_timestamp/).

### 15.2 Scheduled work

Timers are allowed only for:

- Input freshness expiry.
- Daily-history rollover.
- Persistence checkpoints while dirty.
- Debounce deadlines.
- Manual override and boost expiry.
- Broker rate-limit release.
- Acknowledgement deadlines.
- Recovery/quarantine eligibility deadlines.

No ten-minute climate polling loop.

### 15.3 Calculation orchestration

Use a push-only `ZoneController` with coherent snapshot publication.

- No polling `DataUpdateCoordinator`.
- Each event invalidates the relevant generation.
- Capture source state and timestamps into an immutable snapshot before numerical work.
- Permit one numerical job per zone and at most two globally.
- Use HA’s executor for numerical batches.
- If a job is running, retain only the latest pending snapshot request.
- A completed stale-generation calculation may be retained for debug counters but must not publish as current or command equipment.

Cache only:

- Adapted met/clothing for identical model inputs.
- Moisture state for identical temperature/RH inputs.
- Running means by day set and alpha.
- Repeated evaluations inside one solve.

Do not round inputs to manufacture cache hits.

### 15.4 Command sequencing

Per-actuator broker:

1. Receive immutable normalized intent.
2. Coalesce older unsent intent.
3. Apply hysteresis and rate limits.
4. Persist pending intent.
5. Acquire a short dispatch lock.
6. Recheck source expiry, target state, all generations, ownership and lease.
7. Register acknowledgement correlation.
8. Call `climate.set_temperature`.
9. Release dispatch lock.
10. Resolve feedback asynchronously.

Do not hold the dispatch lock while waiting for device acknowledgement. External overrides must remain processable.

Any material source/capability/ownership change invalidates queued commands. Freshness expiry can inhibit dispatch without waiting for a new calculation.

### 15.5 Unload

- Close the dispatch gate first.
- Increment runtime generation.
- Unsubscribe every callback.
- Cancel timers and queued intents.
- Await bounded completion of integration-owned service tasks.
- Discard late executor results.
- Release leases and shared-source references.
- Persist final state.

Cancellation of an HA task cannot guarantee that already transmitted equipment commands are recalled. Diagnostics must retain unresolved outcomes.

### 15.6 Repository performance and load qualification

Use deterministic operation counts, fake clocks and controlled executor completions as required performance evidence. The full nine-location, five-root case has a hard cap of **3,000 ATHB evaluations per zone snapshot**. At most two numerical jobs run globally, one runs per zone, and each zone retains at most one latest pending calculation request. Per target, there is at most one dispatched pending command and one queued future intent.

Required repository load simulations:

- Forty zones, each with a primary and eight critical locations, share one outdoor source. Assert one collector, one bootstrap lineage, 40 listener references and no duplicate daily integration.
- Drive 10,000 deterministically ordered environmental events over ten seconds of virtual time, then quiesce. Assert bounded job/queue counts, at most the latest generation published per zone, and zero stale command dispatches.
- Complete numerical jobs through the controlled executor, draining runnable tasks without wall-clock sleeps. Within the configured debounce and rate-limit deadlines, every eligible latest intent is either dispatched or has the exact blocking/suppression reason and next timer. No task remains runnable without making progress; cap the test drain at 100,000 scheduler turns and fail on non-settlement.
- Simulate 30 minutes of stable reports after initial acknowledgement. Expect zero additional climate calls, finite caches, 35 retained daily summaries per source and at most 20 decision traces per zone.
- Instrument HA API-contract tests so the event loop never executes the numerical kernel, Recorder queries or storage readback directly; these use the declared executors/adapters.

These are repository acceptance criteria, independent of a particular processor or an external HA installation. Optional developer-host timing measurements may be recorded as informational performance data; they are not a hardware qualification gate or a claim about household response. Optimize scheduling and caching if the required budgets fail without loosening numerical or ownership guarantees.

---

## 16. Diagnostics and observability

### 16.1 Decision trace

Keep the latest coherent decision plus a bounded in-memory ring of 20 material decisions.

Each trace contains:

```text
decision identifier and generation
raw/normalized input values
source provenance, validity and expiry
history window and running mean
radiant mode and assumptions
moisture state
adapted met and clothing
primary sensation
critical-location eligibility and demands
governing location
selected comfort strategy, inward fraction and all five requested sensation votes
lower comfort / heating control / thermal neutral / cooling control / upper comfort roots
raw comfort band and raw control band
profile transformation, distinct from comfort strategy
critical influence limits and same-vote mapped roots
slew and bound applications
requested room target
per-target normalized target
ownership and availability
dispatch/acknowledgement outcome
suppression or fallback reason
```

Answer “Why this temperature?” through this ordered trace, without requiring source-code inspection.

### 16.2 Privacy

Downloaded diagnostics must:

- Pseudonymize entity, device, area and location identifiers consistently.
- Remove user IDs, context ancestry identifying users, coordinates and external URLs.
- Omit raw occupancy histories.
- Include only relevant current environmental values and bounded control evidence.
- Exclude arbitrary source attributes.
- Remain local unless the user explicitly shares the file.

Use HA diagnostics redaction helpers and dedicated tests. [HA diagnostics guidance](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics/).

### 16.3 Logging

| Level | Content |
| --- | --- |
| Debug | Detailed calculations, normalization, correlation and suppression |
| Info | Enabled/disabled, override start/end, fallback entry/recovery |
| Warning | Persistent unavailable mandatory source, capped pathological critical location, repeated incompatibility |
| Error | Unexpected internal failure or control-storage failure |

Log a failure on transition, not every sensor event. Log recovery once.

Create repair issues for:

- Removed source/target.
- Duplicate target ownership.
- Corrupt control storage.
- Persistent target rejection.
- Incompatible auto-mode mapping.
- Missing history after 24 hours.
- Mandatory input unavailable for more than one hour.

Ordinary short outages and expected cold-weather extrapolation should not generate repetitive repairs.

---

## 17. Test architecture and Virtual Installation Validation Suite

All required tests run from the repository. They use controlled observations, clocks, executors, storage and climate service endpoints. They require no external running Home Assistant installation, HA UI installation, access to a household or physical actuator. Production still contains the complete `climate.set_temperature` integration path; tests intercept that path at the service/API boundary.

### 17.1 Four distinct validation layers

| Layer | Purpose | Required evidence |
| --- | --- | --- |
| **A — Numerical unit/golden tests** | Validate the selected forward ATHB, pinned-reference conformance, psychrometrics, radiation and all five inverse roots | Independent reference vectors, residuals, domain/failure tests and property checks |
| **B — Pure policy/state-machine tests** | Validate comfort strategies, critical influence, profiles, bounds, normalization, ownership and recovery | Hand-verifiable policy expectations, deterministic transitions and exact suppression reasons |
| **C — Virtual Installation Validation** | Validate a complete configured zone from observations/history to a climate command or suppression | The canonical VI-001–VI-030 library, full result assertions and decision reports |
| **D — Home Assistant API-contract tests** | Validate software compatibility with HA's configuration, entity, service, Recorder and storage interfaces | HA Python test helpers or fake/mocked `hass`, fake climates, mocked services/history and config-flow tests |

Layer D may create HA Python objects in the pytest process. It does not start an external HA server, use a real installation or require installation through a UI. Tests for flows invoke their Python flow APIs and inspect form/result contracts. Describe this evidence as **repository-based Home Assistant API-contract testing**.

Layer C invokes the actual implementation components in this order: validated configuration and source observations/history → immutable environmental snapshot → ATHB forward evaluation and inverse roots for the selected strategy → mapped critical-location influence → profile policy → slew/calibration/bounds → climate normalization → ownership/preflight → sole command broker → captured exact service call or suppression. Only external boundaries (clock, source delivery, Recorder, storage I/O, executor scheduling and climate service/feedback) are controlled test doubles. Do not mock the numerical engine, policy, ownership reducer or broker, or feed expected golden results into the production calculation path.

### 17.2 Reference isolation and fixture provenance

Use separate environments:

1. Repository HA API-contract tests on the supported HA/Python matrix.
2. Oracle-generation tests with pinned `pythermalcomfort==4.4.2` and a compatible, fully locked scientific dependency set.

The HA 2026.9.0 baseline declares Python `>=3.14.2`; do not force the oracle's scientific stack into that environment. Production has no NumPy/SciPy/Numba dependency.

Check in reference fixtures containing package/transitive versions, repository revision, input vectors, rounded public outputs, unrounded reference outputs where applicable, source hashes, generation command and generation timestamp. Record these pinned source SHA-256 values:

```text
_pmv_ppd_optimized.py
dd3c1f3d7ffacea65978cb080a1b676dbadc143eeb8121196dfa6b9b8a9d2518

pmv_athb.py
6ea3044b9ab0a229e3f47603d64c3070b707fb31271a9a92ff4fff0c92ec5c9d
```

For unrounded inverse goldens, use an isolated reference driver over the pinned upstream forward kernel, removing only its final public rounding and recording that transformation. Use independent numerical/psychrometric reference tooling, not the production `athb.core` implementation, to obtain the expected roots. Layer A separately proves the candidate psychrometric and radiant transformations. Reference generation is an explicit reviewed developer action; normal tests only read the checked-in results. Never regenerate fixtures automatically to make a failing test pass.

### 17.3 Numerical conformance and properties

Required forward cases include automatic/fixed clothing, metabolic/convection branches, zero air speed, RH endpoints, cold/warm running means, engineering boundaries, divergent MRT, Boolean/nonfinite rejection and heat-balance nonconvergence.

Tolerances:

- Rounded public ATHB oracle: absolute error ≤0.00051 vote.
- Unrounded pinned kernel under equivalent arithmetic: target absolute error ≤`1e-7` vote.
- Platform deviations exceeding that target require explanation and a versioned test decision, not silent tolerance inflation.
- Utility comparisons respect public-output rounding.
- Inverse goldens: absolute temperature error ≤0.01 K, in addition to the solver's ≤0.005 K bracket-width and ≤0.002 vote residual requirements.

Retain the discrepancy regression showing that the inspected `comf` transfer function is not interchangeable with the selected formulation.

Inverse properties include ordered successfully solved roots where the monotonic complete solution exists; actual strategy-vote residuals; changed asymmetric outer boundaries; fixed versus moving MRT; signed local deltas; saturation/no-bracket behavior; no endpoint masquerading as a root; deterministic multiple-bracket rejection; all iteration/evaluation limits. Increasing requested sensation cannot decrease the root within an accepted monotonic interval.

Test that Efficient/Balanced/Comfort fractions are applied in sensation space. A test must fail an implementation that uses arithmetic temperature midpoints, hardcoded default votes under changed outer boundaries, or a different vote for a critical location.

> **Normative clarification:** Tests must also cover valid directional actuation when one or more unrelated roots fail, per `ATHB_BUILD_CLARIFICATIONS.md` §2.

### 17.4 Inspectable numerical anchors

The following are **planning reference probes**, not claims that the future integration or its suites already pass. The forward kernel was taken from the hash-pinned upstream sources, with final ATHB rounding removed; tighter bracketed reference solves were used for these anchors. The build must preserve these expectations in reviewed checked-in goldens with provenance.

Unless stated otherwise: primary air 20 °C, primary RH 50%, `R = 5 °C`, declared ambient speed 0.1 m/s, met 1.1, relative speed 0.13 m/s, automatic clothing, uniform radiant environment and constant vapor pressure. The starting vapor pressure is 1169.401850 Pa. Temperature entries below are in primary room coordinates; `L/H/N/C/U` have the five meanings in §6.

| Golden key | Conditions/strategy | Current primary sensation | L °C | H °C | N °C | C °C | U °C |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `G_BASE_B` | Baseline / Balanced | −0.173088 | 17.281547 | 19.362709 | 21.429145 | 23.481038 | 25.518575 |
| `G_BASE_E` | Baseline / Efficient | −0.173088 | 17.281547 | 18.532022 | 21.429145 | 24.297765 | 25.518575 |
| `G_BASE_C` | Baseline / Comfort | −0.173088 | 17.281547 | 20.191040 | 21.429145 | 22.662014 | 25.518575 |
| `G_NEAR_B` | Air 19.5 °C, RH 51.5759274364%, same vapor pressure / Balanced | −0.233445 | 17.281547 | 19.362709 | 21.429145 | 23.481038 | 25.518575 |
| `G_BELOW_B` | Air 16 °C, RH 64.3079856685%, same vapor pressure / Balanced | −0.653070 | 17.281547 | 19.362709 | 21.429145 | 23.481038 | 25.518575 |
| `G_SURFACE16_B` | Fixed surface 16 °C, view factor 0.25, moving uniform background / Balanced | −0.228480 | 17.447275 | 19.798354 | 22.133705 | 24.453465 | 26.757772 |
| `G_SURFACE14_B` | Snapshot-modelled surface 14 °C, view factor 0.25, moving uniform background / Balanced | −0.255340 | 17.701332 | 20.050001 | 22.382966 | 24.700363 | 27.002331 |
| `G_WINTER_B` | Running mean −10 °C / Balanced | +0.206715 | 13.702541 | 15.944270 | 18.170614 | 20.381760 | 22.577898 |
| `G_HUMID_B` | Air 28 °C, RH 80%, R 25 °C / Balanced | +0.563948 | moisture-limited | moisture-limited | moisture-limited | 25.660781 | 27.525210 |
| `G_WIDE_B` | Baseline environment, outer votes −1.00/+1.00 / Balanced | −0.173088 | 13.074783 | 17.281547 | 21.429145 | 25.518575 | 29.551316 |

For a baseline critical location 2 K colder with shared vapor pressure and local uniform MRT, its current **local** vote is −0.413897. Its five mapped Balanced roots are exactly `G_BASE_B` roots +2 K: heating 21.362709 °C, thermal neutral 23.429145 °C. This shift is hand-verifiable from `T_local(x)=x−2` and does not require the location to reach zero vote. The primary vote and primary roots remain `G_BASE_B`.

The canonical uncapped local-air scenario uses a 1.5 K delta so numerical solver tolerance cannot blur its distinction from the 2 K cap. Its local current vote is −0.353849; its mapped five roots equal `G_BASE_B` +1.5 K. The capped 3 K-delta case has local current vote −0.533684 and mapped five roots equal `G_BASE_B` +3 K. Reference fixtures store all five values explicitly as well as these hand-verifiable relationships.

The numerical expectations in the table are compared within `1e-6` vote for the displayed current sensations and 0.01 K for roots; full-precision fixture values use Layer A's stricter conformance tolerance. Typed failures must match exactly.

Use `G_WIDE_B` as an explicit interpolation regression in Layers A/B. Balanced requests votes −0.50/+0.50 with these configured outer boundaries. Arithmetic temperature midpoints would instead produce 17.251964/25.490230 °C, errors of approximately 0.029582/0.028344 K, exceeding the 0.01 K golden tolerance. The test must reject those midpoint results. This case supplements the default-boundary examples, where interpolation error can be smaller than the accepted inverse-temperature tolerance.

Additional retained numerical checks:

| Scenario | Expected result |
| --- | --- |
| Forward: `tdb=25`, `tr=25`, `vr=0.1`, `RH=50`, `met=1.2`, `R=20`, automatic clothing | Unrounded vote ≈0.2062518; public result 0.206 |
| Baseline with measured MRT 16 °C | Heating root ≈22.249377 °C; thermal neutral ≈26.139879 °C before limits |
| Baseline moisture heated to 25 °C | RH ≈36.90% |
| Same moisture at a 12 °C surface | Surface RH ≈83.37% |

For the retained wider outdoor-temperature matrix, base current air/RH and Balanced give:

| Running mean °C | L °C | H °C | N °C | C °C | U °C |
| ---: | ---: | ---: | ---: | ---: | ---: |
| −20 | 10.98 | 13.35 | 15.70 | 18.03 | 20.35 |
| −10 | 13.70 | 15.94 | 18.17 | 20.38 | 22.58 |
| 0 | 16.14 | 18.27 | 20.39 | 22.49 | 24.58 |
| 5 | 17.28 | 19.36 | 21.43 | 23.48 | 25.52 |
| 10 | 18.38 | 20.42 | 22.43 | 24.44 | 26.43 |
| 20 | 20.47 | 22.41 | 24.33 | 26.25 | 28.14 |
| 30 | 22.54 | 24.39 | 26.23 | 28.05 | 29.86 |

These raw roots may lie outside user limits; the test must assert both the root and the separate bound application. The comfort band may span approximately eight degrees. Neither that width nor an equipment assumption changes the strategy fraction.

### 17.5 Virtual installation fixture contract

Use versioned **JSON fixtures**, validated against a checked-in JSON Schema and loaded into typed immutable test objects. Keep scenario data under `tests/fixtures/virtual_installations/` and independent numerical goldens under `tests/fixtures/reference/`. Use deterministic JSON output with sorted keys for review.

A scenario may inherit one named baseline and explicitly override fields. Expand it to one complete validated object before execution. Object merge rules are explicit; arrays replace whole arrays, absent fields inherit, explicit `null` remains null, and unknown fields fail validation. Expected numerical references resolve only from checked-in golden files; they never call production code or an oracle at test runtime. Report the fully expanded input and expectation on failure so inheritance cannot hide missing truth.

Every expanded scenario requires:

| Group | Required fields and semantics |
| --- | --- |
| Identity | `schema_version`, unique `scenario_id`, `name`, purpose, baseline/variant identity, fixed UTC clock origin, configured IANA timezone, deterministic random seed |
| Zone configuration | `zone_id`; primary temperature source identity/unit/provenance; tagged primary RH source (`measured` identity or `declared` numeric value); outdoor identity/attribute; seven-day history settings, alpha, coverage and hold limits; met; automatic/fixed clothing; measured/declared ambient or relative speed; radiant mode; surfaces/view factors/background/calibration; critical locations/types/control eligibility/delta policy; lower/upper comfort votes; comfort strategy; occupancy mapping and profile; eco values; boost delta/duration; user command limits; fallback mode/values; extrapolation policy; per-target calibration and grid/gap options; debounce, rate and override settings |
| Environmental state | Source observations with values, units, `observed_at`, `received_at`, availability, validity and provenance; current indoor temperature and measured RH when selected; current outdoor observation; MRT/globe/surface/local-air observations as configured; raw outdoor history records or explicit persisted summaries with coverage and provenance; pre-roll reports for delta filtering/quarantine; input-expiry expectations. A direct declaration has `observed_at=null`, `received_at=null`, and its validated configuration generation rather than a fabricated sensor timestamp |
| Target capabilities | Stable target identity and entity ID; initial HVAC mode; advertised modes; supported HA feature names; explicit scalar/range shape; min/max/step and their units; grid origin; HA service unit and fake backend native unit; current scalar/range target; availability/restored flag; preset; explicit auto mapping; feedback resolution; readback/coercion/delay/rejection behavior |
| Runtime/control | Zone enabled state; per-target ownership, target/data readiness and revisions; manual override reason/expiry; selected and previous profile; boost expiry; previous requested room target and acknowledged command; unresolved command/context if any; clean/unclean persisted state; configuration/input/capability generations; pending and queued intent; timer deadlines and source leases |
| Event timeline | Ordered `(virtual_time, sequence, event)` actions for source report/unavailability, configuration/strategy/profile change, external service/state feedback, fake time advance, controlled executor completion, simulated storage save/read/corruption, load/unload/restart, and target capability changes. Equal timestamps use explicit sequence order |
| Expected numerical result | Current primary sensation or typed failure; requested votes; primary roots with units/tolerances or typed failures; participating critical-location votes/mapped roots or exclusion reasons; history result/coverage; applicability and provenance; exact golden key/hash |
| Expected policy result | Selected comfort strategy/fraction, selected and resolved profile, comfort/control bands, governing heating/cooling locations, raw/applied critical influence, pre-slew and requested room targets, profile/fallback status, calibration/bound effects |
| Expected broker result | Per-target bounded continuous and normalized scalar/range request, legal-grid index, emitted boolean, ordered exact service payloads and contexts, suppression reason(s), acknowledgement result, ending ownership/revisions, queue/timer state, degraded/fallback reasons and transition sequence |
| Expected execution evidence | Collector/bootstrap/listener counts where relevant, maximum running jobs/queued requests/pending commands, evaluation budget, discarded generations, total service count, termination conditions and virtual elapsed time |

No mandatory expectation may be omitted merely because a test runner can calculate it. For invalid data, explicitly expect typed unavailable outputs and no adaptive room target; if a valid numerical preview is suppressed only by ownership/mode, assert that preview as well. Exact emitted payload lists are empty when no command is allowed. Current observations and declarations retain their distinct provenance through diagnostics.

Observation fixtures may carry a `raw_state` JSON primitive so invalid sensor strings, nulls or Booleans reach the input validator. Encode nonfinite raw sensor values as strings such as `"NaN"`, not invalid JSON numeric tokens. Validated numeric expectations and configuration declarations remain finite and strictly typed. Config-flow error cases carry raw form submissions in a separate event payload; fixture-schema validity must not depend on the submitted configuration being accepted by the product.

### 17.6 Canonical baseline and expectation notation

The following baseline makes the scenario library concrete. It is stored in the repository as `living_room_base.json` and expanded into every applicable fixture:

```text
clock = 2026-09-10T10:00:00Z; timezone = Europe/Amsterdam; seed = 0
zone_id = living_room
primary = sensor.living_room_temperature, 20 °C, measured, available
RH = measured(sensor.living_room_rh), 50%, available
outdoor = sensor.outdoor_temperature, 5 °C, measured, available
current observation timestamps = clock, configuration/input/capability generation = 1
history = hourly reports of 5 °C across each local day 2026-09-03 through 2026-09-09,
          plus the observation needed at the first day boundary; complete raw attributes
          and timestamps; 100% coverage in each day; current outdoor observation at clock
history alpha = 0.8, maximum hold = 2 h, daily coverage threshold = 90%
met = declared(1.1); clothing = automatic; ambient speed = declared(0.1 m/s)
radiant = uniform; surfaces = []; critical locations = []
comfort boundaries = [-0.50, +0.50]; comfort strategy = balanced
selected profile = comfort; occupancy = absent; resolved profile = comfort
eco = heating 2 K / cooling 2 K; boost = 1 K for 60 min, inactive
user command bounds = [18,26] °C; fixed fallback = heat 18 / cool 26 °C
reject_extrapolation = false; calibration = 0 K; minimum range gap = 1 K
target = climate.living_room; mode = heat; modes = [off, heat]
features = [TARGET_TEMPERATURE]; shape = scalar
target min/max = [16,30] °C; step = 0.5 °C; origin = 16 °C
HA unit = °C; fake backend native unit = °C
current setpoint = 18 °C; available, not restored; preset = none
auto mapping = absent; feedback tolerance = 0.01 K
readback = exact own-context acknowledgement in the same deterministic service cycle
enabled = true; ownership = OWNED following successful simulated reconciliation
ownership_revision = 1; control/history storage = valid and writable
last acknowledged/requested/pending/queued command = none; no prior command interval
override = none; boost expiry = none; stale timers = none
debounce/rate/slew settings = §8.6 defaults; initial calculation has no slew history
events = submit complete configuration and observations, calculate after 2 s debounce,
         complete controlled numerical/storage/service work, process acknowledgement
```

The virtual history adapter feeds those raw reports through the production history integration path. It must produce seven exact daily means of 5 °C, `R = 5 °C`, `complete_history`, 100% represented weight and exactly one shared collector. Fixtures that start from persisted summaries identify that different bootstrap path explicitly.

Baseline expectations: numerical `G_BASE_B`; votes `[-0.50,−0.25,0,+0.25,+0.50]`; requested heating room target 19.362709 °C; normalized 19.5 °C; primary governing; one acknowledged command; ending `OWNED/DEGRADED_READY/AVAILABLE_SUPPORTED`; no suppression or fallback. The quality-reason set is exactly `{estimated_uniform_radiant_environment, limited_evidence}`. History quality is a separate field `complete_history`. Input provenance is measured for the temperature/RH/outdoor observations, declared for met/ambient speed, and estimated for uniform MRT/automatic clothing; these are distinct fields rather than extra interchangeable reason codes.

The following shorthand is an exact payload convention for scenario tables, not permission for loose assertions:

```json
{
  "domain": "climate",
  "service": "set_temperature",
  "service_data": {"entity_id": "climate.living_room", "temperature": 19.5}
}
```

`heat(t)` and `cool(t)` both mean that exact service envelope with `temperature=t` and the fixture's declared target entity ID. Direction comes from the capability mapping, never a service `hvac_mode` field. `range(h,c)` means the same envelope with exactly `entity_id`, `target_temp_low=h` and `target_temp_high=c`. The deterministic context factory supplies `ctx-<scenario-id>-<command-index>`; assert the context passed to HA separately. No fan/preset/mode/on/off/humidity/swing call is allowed in any scenario.

### 17.7 Required canonical virtual installations

The following scenario library is mandatory. Every unchanged field inherits the baseline; each actual fixture must expand to a complete explicit expectation object before execution.

| ID and scenario | Required essence and expected result |
| --- | --- |
| **VI-001 — Dutch living room / Balanced** | Baseline. `G_BASE_B`; heating vote −0.25; requested 19.362709 °C; primary governing; exact `heat(19.5)` once and acknowledged. Proves target publication while already inside the outer comfort band. |
| **VI-002 — Same room / Efficient** | Strategy only changes to Efficient. `G_BASE_E`; heating vote −0.35; requested 18.532022 °C; exact `heat(19.0)` once. |
| **VI-003 — Same room / Comfort** | Strategy only changes to Comfort. `G_BASE_C`; heating vote −0.15; requested 20.191040 °C; exact `heat(20.5)` once. |
| **VI-004 — Room around Balanced target** | Air 19.5 °C with moisture-equivalent RH and previous acknowledged 19.5 target. `G_NEAR_B`; zero additional commands for 30 minutes virtual time; `target_unchanged`. |
| **VI-005 — Room below comfort band** | Initial air 16 °C with moisture-equivalent RH. `G_BELOW_B`; heating target remains the −0.25 strategy root 19.362709 °C, exact `heat(19.5)` once; no neutral/boundary substitution. |
| **VI-006 — Cold critical-air location** | Eligible local air 1.5 K colder, same strategy vote and valid warm-up. Local mapped roots are primary +1.5 K. Governing local heating request 20.862709 °C; exact `heat(21.0)` once. |
| **VI-007 — Pathological location/influence cap** | >6 K raw delta is excluded; stable 3 K delta produces capped 2 K aggregate uplift with `critical_demand_limited`; no accumulating demand. |
| **VI-008 — Cold measured surface** | Measured 16 °C surface, view factor 0.25. `G_SURFACE16_B`; request 19.798354 °C; exact `heat(20.0)`. Surface is not local air. |
| **VI-009 — Internally modelled surface** | `f_Rsi=0.6`, outdoor 5 °C, primary 20 °C → estimated 14 °C surface. `G_SURFACE14_B`; request 20.050001 °C; exact `heat(20.5)`; estimated/modelled provenance and no helper lookup. |
| **VI-010 — Direct fixed RH declaration** | RH configured directly as declared 50%, no RH entity. Numerical result and command match VI-001; provenance remains `declared`; no helper/entity lookup. |
| **VI-011 — Missing optional MRT** | Direct MRT source unavailable, uniform approximation allowed. `G_BASE_B`; `heat(19.5)` once with degraded radiant reason. |
| **VI-012 — Invalid mandatory air** | Unavailable/stale/nonfinite/Boolean/malformed primary temperature variants. No executable target and no commands; typed failure and hold→invalid behavior. |
| **VI-013 — Invalid/missing RH** | Measured mode with missing/stale/invalid RH and no declared alternative. No numerical executable target and zero calls; config form without a valid RH mode/value fails. |
| **VI-014 — Partial/insufficient history** | Three newest eligible days allow partial adaptive operation; two days are insufficient and use fixed fallback if otherwise eligible. |
| **VI-015 — Cold-winter extrapolation** | Seven days/current outdoor −10 °C. `G_WINTER_B`. Allow mode computes/bounds adaptive target; reject mode follows hold→fallback with distinct trace. |
| **VI-016 — Humid warm room and saturation** | Air 28 °C, RH 80%, `R=25`, cooling-only. `G_HUMID_B`: heating/neutral roots may be moisture-limited while cooling-control root is valid at 25.660781 °C. **Per `ATHB_BUILD_CLARIFICATIONS.md`, cooling-only control must use the valid cooling root rather than fallback merely because unrelated roots failed.** With a 0.5 °C inward cooling grid, expect 25.5 °C if otherwise eligible. A ranged actuator lacking its heating endpoint remains ineligible. |
| **VI-017 — Cooling-only strategy targets** | Balanced/Efficient/Comfort scalar cooling variants verify +0.25/+0.35/+0.15 roots and inward normalization. |
| **VI-018 — Ranged heat/cool** | Balanced range uses `[H,C]`, not `[L,U]`; baseline request `[19.362709,23.481038]` → exact inward normalized range. |
| **VI-019 — Explicit mapped auto range** | Explicit supported ranged-auto mapping behaves like ranged policy; no mode service call. |
| **VI-020 — Unsupported auto semantics** | Valid numerical preview may remain, but zero writes and exact `unsupported_auto_mapping`; never choose a neutral scalar substitute. |
| **VI-021 — Climate off** | Numerical preview remains; zero writes, no turn-on/mode change; readiness suspended. External off does not automatically create a manual target override. |
| **VI-022 — Fahrenheit and one conversion** | Verify HA-unit service semantics, legal grid and no double conversion. |
| **VI-023 — Coarse-grid inward normalization** | Heating always rounds inward/up, cooling inward/down; ranged failure does not widen to comfort band. |
| **VI-024 — Manual external setpoint change** | External temperature target change enters `MANUAL_OVERRIDE` and suppresses further ATHB writes until reconciliation/resume. |
| **VI-025 — ATHB-originated acknowledgement** | Own-context and permissible contextless exact pending feedback acknowledge without override or duplicate write. |
| **VI-026 — Obsolete delayed acknowledgement** | Obsolete feedback cannot restore ownership or overwrite current command state. |
| **VI-027 — Restart recovery simulation** | Clean/unclean persisted-state scenarios verify reconcile-before-command and no replay of persisted calculation/command. |
| **VI-028 — Multiple actuators/conflicts** | Coordinated directional outputs respect room-reference gap and manual opposing targets; conflicting writes are suppressed. |
| **VI-029 — Zones sharing outdoor source** | Multiple zones share exactly one logical collector/bootstrap lineage and release references cleanly. |
| **VI-030 — Event storm/stale work** | 10,000 deterministic source events, stale work invalidation, bounded jobs/queues and final strategy generation only; no stale command dispatch. Strategy change itself is lightweight and does not reload the config entry. |

For VI-001/002/003/017 assert the cross-strategy ordering on **raw roots**:

```text
Heating: Efficient < Balanced < Comfort < Thermal neutral
Cooling: Thermal neutral < Comfort < Balanced < Efficient
```

Actuator grid rounding and hard bounds may collapse different strategy requests to the same legal payload; they must not change those raw model expectations. Assert normalized ordering separately where the fixture's grid permits distinct outputs.

The scenario table is the minimum library, not a substitute for complete expanded expectations. Invalid-primary scenarios compare typed failures and a full empty command sequence. Modelled-surface/history expectations must be explicit fields.

### 17.8 Pure policy, history, capability and race coverage

In addition to the canonical installations, retain focused tests for:

- All three strategy fractions with default and asymmetric outer votes; no personal-bias input; strategy-change invalidation/persistence; continuous target publication inside the comfort band.
- Directional root eligibility, including irrelevant-root failure for scalar heat/cool and required paired roots for ranged control.
- Critical warm-up boundaries, ±3 K filtered limit, raw >6 K rejection, aggregate 2 K cap, heating/cooling eligibility, tie-breaking, monitoring-only exclusion and ranged conflicts.
- `auto` occupancy resolution, eco setbacks, scalar/ranged boost limiting/expiry, fixed fallback/no-write mode, user bounds and calibration ordering.
- Inward normalization, exact grid points, mixed units, no legal inward target/range, gap conversion and directional release hysteresis.
- Measured versus directly declared RH; absent/invalid declarations; zero RH; declared air speed; changing source modes; stale observations; no helper dependency; no fabricated freshness or automatic RH substitution.
- Time-weighted unequal sampling, unchanged reports, gaps, availability boundaries, exact maximum hold, 23/25-hour DST days, midnight splits, source/timezone changes, synthetic start states, bootstrap/event overlap, partial weighted positions, corrupt history and downtime.
- Every climate matrix row, supported/unmapped auto, Fahrenheit/native-unit differences, missing/misleading step, coarse readback, invalid capabilities, range rejection, coercion, preset changes and target replacement.
- External HVAC-mode changes as readiness/reconciliation events rather than automatic manual target overrides; external temperature-target interventions still override.
- Feedback before service return; manual calls before/during dispatch; contextless ambiguity; obsolete feedback; capability/input expiry; reload/unload during persistence/service dispatch; unresolved commands; concurrent target leases and opposing targets.
- Every failure-table row, failed control-store save/readback, corruption/schema migration, optional source recovery, duplicate control prevention, bound-respecting fallback and cooling dew-point checks after calibration/normalization.

All time advances, restarts, sensor faults and device responses above are test-environment simulations. Runtime repair thresholds are exercised by advancing the fake clock.

### 17.9 Repository-local technical demonstration

Provide this repository command in the development documentation:

```bash
python -m pytest tests/virtual_installations -v --athb-report=artifacts/virtual-installations.md
```

Implement `--athb-report` in the test suite's pytest plugin/conftest using existing scenario results. It is a test report, not a standalone application or simulator UI. It creates a concise human-readable Markdown report, a machine-readable JSON companion and an assertion-summary artifact. Normal exit status is zero only if every mandatory scenario and variant passes. Missing fixtures, skipped mandatory variants or absent expected fields fail qualification. Tests never fetch new golden truth from the network.

Each scenario report includes input/provenance summary, running mean/history quality, current sensation, attempted roots and typed failures, raw comfort/control bands where available, governing location, selected strategy/profile, requested room target, applied limitations, normalized actuator target, exact command or suppression reason, ownership transition and PASS/FAIL with failed assertions.

Representative expected excerpt:

```text
Scenario: VI-001 / Dutch living room / Balanced
Running mean: 5.00 °C; complete history, 7 days, 100% represented weight
Indoor: 20.00 °C (measured); RH: 50.00% (measured)
Current ATHB sensation: -0.173088
Lower comfort boundary: -0.50 -> 17.281547 °C
Heating control target: -0.25 -> 19.362709 °C
Thermal neutral:         0.00 -> 21.429145 °C
Cooling control target: +0.25 -> 23.481038 °C
Upper comfort boundary: +0.50 -> 25.518575 °C
Comfort band: 17.281547 to 25.518575 °C
Control band: 19.362709 to 23.481038 °C
Profile: comfort; governing heating location: primary
Requested room target: 19.362709 °C
Effective actuator target: 19.5 °C
Command: climate.set_temperature(entity_id=climate.living_room, temperature=19.5)
Ownership: OWNED; acknowledgement: acknowledged
Quality: estimated_uniform_radiant_environment, limited_evidence
Result: PASS only when every expected assertion actually passes
```

### 17.10 Full repository qualification

Run Layers A–D, every VI scenario/variant, event/race/storage simulations, config/reconfigure/options tests, climate capability matrix, diagnostics/redaction, translations, all safety invariants, deterministic §15.6 load budgets, static analysis, type/lint checks and packaging validation in CI. Test minimum HA 2026.9.0 and the current explicitly supported HA version using pinned CI environments; avoid an unbounded claim of compatibility with every future release.

Independent final software architecture, numerical and safety review examines the same immutable candidate and its evidence. Findings are resolved and affected checks rerun before completion. The review scope is software and reference conformance; no external installation evidence is deferred to an owner or agent.

---

## 18. Safety invariants

The implementation must enforce these invariants through core validation and the final broker boundary. Each invariant has named repository tests and coverage in the qualification report.

1. **No write without valid fresh primary temperature and eligible RH.** RH is either a valid fresh selected observation or a valid explicit fixed declaration; a declaration never fabricates sensor freshness.
2. **No adaptive write without the root(s) relevant to the current target direction/shape.** ATHB attempts all semantic roots for observability where possible, but unrelated root failure must not block an otherwise complete directional decision. Missing roots remain typed diagnostic failures.
3. **Fallback is explicitly labelled policy and never presented as ATHB output.** It cannot bypass invalid mandatory actuation data, ownership or mode checks.
4. **No NaN or infinity reaches entity state, persistence or service data.** Invalid quantities use typed absence/failure.
5. **Every command satisfies user bounds and advertised device bounds**, including calibration and unit conversion.
6. **Every command lies on the selected legal actuator grid.** Normalization rounds heating inward upward and cooling inward downward from the bounded request; inability to do so suppresses the command.
7. **Every commanded range is ordered and meets minimum separation.** An infeasible control band is not widened to comfort boundaries or silently swapped.
8. **No command changes HVAC mode or unrelated climate functionality.** Off and unsupported modes suspend target writes.
9. **No write while disabled, overridden, unavailable, incompatible or awaiting required reconciliation.**
10. **No stale-generation command is dispatched**, including after a comfort-strategy change.
11. **Only the command broker may call climate services.**
12. **Only one ATHB zone may hold a target's runtime lease.**
13. **At most one command is pending and one future intent is queued per target.** Numerical work obeys its own one-per-zone/two-global limits.
14. **Timeouts cannot create an automatic resend storm.**
15. **Critical-location influence is bounded in aggregate.** Each eligible location uses the same selected heating/cooling sensation vote as the primary.
16. **Modelled surface temperature is never substituted for local air or measured MRT.** Its configured radiant contribution and estimated provenance are explicit.
17. **Missing sensor data never becomes a fabricated measurement.** Direct fixed declarations remain labelled `declared` throughout their derivations and diagnostics.
18. **Saturation is not silently crossed by clipping candidate RH.** Fixed starting RH declarations still use constant-vapor-pressure inverse psychrometrics.
19. **Restart never replays persisted commands without reconciliation.** Obsolete acknowledgements cannot restore ownership or overwrite a newer command state.
20. **Every inhibited or limited decision has a visible reason**, separate from numerical validity and source provenance.
21. **Known conflicting heating/cooling requests within a zone are not dispatched.** Compare normalized values in room-reference coordinates.
22. **Stopping ATHB does not silently switch equipment off.**
23. **The default conditioning targets are the selected strategy roots.** Thermal neutral is a reference; the default ranged request is the control band. Normal target publication does not wait for an outer comfort-boundary crossing.
24. **Strategy votes come from configured outer boundaries and fixed inward fractions.** No temperature interpolation, personal thermal bias, emitter-type adjustment or predicted overshoot changes them.
25. **External HVAC-mode changes do not automatically become manual temperature-target overrides.** They update target readiness and reconciliation; external target changes retain manual-override semantics.
26. **A normal comfort-strategy change does not reload the entire config entry.** It invalidates/recalculates the relevant generation through the lightweight runtime path.

These invariants constrain software requests. They do not guarantee actual room temperature, equipment response, frost protection or absence of manual intervention. The specified proof is deterministic repository evidence for the software's behavior.

---

## 19. Development phases

All phases belong to one full production implementation. Completing the numerical core or read-only entities alone is not product completion. Each phase's verification is repository-local.

| Phase | Objective and modules | Dependencies | Required tests | Completion gate |
| --- | --- | --- | --- | --- |
| 1. Numerical contract | Freeze provenance, equations, oracle environment and fixtures; core contracts/heat balance/ATHB | None | Forward goldens, input/domain failures | Exact model identified; conformance passes |
| 2. Moisture and radiation | Psychrometrics, radiant models, direct declarations | Phase 1 contracts | Saturation, dew point, globe, surface and declared-RH vectors | Candidate physical semantics and provenance proven |
| 3. Inverse and locations | Five semantic root attempts, strategy-vote derivation, mapped critical locations and directional actuation eligibility | Phases 1–2 | Root matrix for every strategy, directional-root failures, local mapping/warm-up, failure limits | Deterministic roots and typed failures |
| 4. Shared environmental history | History math, sources, Recorder/storage adapters | Phase 1 contracts | Coverage, DST, bootstrap, simulated restart and sharing | Reproducible running mean without helper dependencies |
| 5. Product policy | Comfort strategies, profiles, caps, bounds and coordination | Phases 3–4 | Same-vote critical influence, control band, eco/boost/fallback | Exact directional target semantics for every profile |
| 6. Climate compatibility | Climate adapter, units and inward normalization | Phase 5 | Complete capability/unit/grid matrix | Exact supported payload or suppression reason |
| 7. Ownership and broker | Ownership reducer, sole broker, recovery journal | Phases 5–6 | Manual-target intervention, HVAC-mode readiness, acknowledgement, storage and race simulations | Every write-boundary invariant passes |
| 8. Native HA configuration | Config/reconfigure/options, runtime, entities, lightweight strategy select | Phases 4–7 | Repository HA flow/API-contract and load/unload tests | Complete configuration and production service path exercised through test doubles |
| 9. Observability and documentation | Diagnostics, repairs, translations, fixture schema and scenario reports | Phases 1–8 | Redaction, transition logs, schema validation, documentation examples | Every target decision and test expectation inspectable |
| 10. Integrated software qualification | VI-001–VI-030, full quality/compatibility/load suites, final independent review and packaging | Phases 1–9 | All Layers A–D, variants, failure matrix and deterministic load budgets | Entire Definition of Done passes; `SOFTWARE_COMPLETE` |

The future implementation workflow should use the applicable multi-session phase execution and independent review procedure available to the build environment. This document does not activate controllers or require household-dependent testing.

---

## 20. Definition of Done

The build agent may return **`SOFTWARE_COMPLETE`** when every item below is satisfied entirely inside the repository. All mandatory checks must pass; skipped or missing mandatory VI variants are failures, not deferred work. The release implements the full supported target-control path, with technical correctness demonstrated by the specified tests.

### Scientific and numerical

- Frozen ATHB 2022 formulation, numerical contract and source provenance implemented and documented.
- `comf` discrepancy recorded without merging variants.
- Pinned-reference numerical conformance, psychrometrics, radiant calculations and inverse-root tests pass.
- Efficient/Balanced/Comfort use actual sensation roots, fixed fractions and configurable outer boundaries correctly.
- Directional actuation remains valid when unrelated roots fail, exactly as specified in `ATHB_BUILD_CLARIFICATIONS.md`.
- Dutch winter, critical-air, declared-RH and humid-summer cases have explicit golden-backed outcomes.
- Applicability limitations, typed failures and measured/declared/estimated provenance remain visible.
- Runtime uses the small pure-Python core and exposes no alternative comfort model.

### Policy, control and recovery

- Heating, cooling and range policies use the selected strategy targets; raw comfort/control bands remain distinct where their component roots are available.
- Critical influence, eco, boost, occupancy resolution, calibration, bounds and fallback tests pass.
- Production `climate.set_temperature` calls exist solely in the command-broker path and are verified with exact payload assertions against controlled HA service doubles.
- Entire climate-capability matrix passes, including unsupported/off suppression and inward normalization.
- Ownership, manual **target** override, HVAC-mode readiness/reconciliation, disable/resume, acknowledgements and clean/unclean restart simulations pass.
- Event ordering, stale work, bounded queues, storage failure/readback and conflict simulations pass.
- Strategy select updates are lightweight and do not unload/reload the config entry.
- Every safety invariant has passing coverage; numerical failure branches and all safety-critical state-machine transitions are explicitly tested.
- Stable inputs cause zero redundant calls during 30 minutes of **virtual** time.

### Virtual Installation Validation and HA API contracts

- All canonical VI-001–VI-030 scenarios and every mandatory variant pass with explicit expected sensations, attempted/successful roots, policy decisions, payloads/suppressions, provenance and ownership results.
- Repository-local demonstration command succeeds and produces inspectable Markdown/JSON evidence.
- Config, reconfigure, options and strategy-select tests pass through repository HA Python APIs.
- Direct fixed RH requires no helper; invalid/missing selected RH cannot trigger a fabricated value or fallback write.
- Normal default configuration works without MRT/view-factor/cold-surface expertise; optional advanced radiant paths are separately tested.
- Zone/entity identity survives reconfiguration; disabled-first setup and duplicate target prevention pass.
- Recorder absence/first-start missing history, shared collectors and simulated recovery pass.
- Load/unload removes listeners, timers, tasks and leases as specified.
- Minimum-version and current-explicitly-supported-version CI environments pass.

### Engineering and packaging

- At least 95% total test coverage, with complete required safety-transition coverage as above.
- Static typing, lint, formatting and all repository quality checks pass.
- Deterministic operation/load simulations in §15.6 pass without hardware-specific qualification dependencies.
- Versioned config/storage schemas and migration tests exist.
- Diagnostics/redaction, repair issue behavior and log-transition tests pass.
- English and Dutch translations and labels are complete; the normal comfort strategy choices are exactly Efficient, Balanced and Comfort.
- Runtime manifest has no NumPy/SciPy/Numba requirements.
- Installable archive is built and its contents/import paths/manifest/version validated in repository tests or an isolated temporary extraction; this verification does not install it into HA.
- HACS-compatible structure and validation pass; publication remains a separate authorized action. [HACS integration requirements](https://www.hacs.xyz/docs/publish/integration/).
- No formal HA Quality Scale certification is claimed for the custom integration.
- Independent final architecture, numerical and safety review covers the immutable candidate and test evidence; actionable findings are resolved and affected checks/review repeated on the final candidate.

### Documentation and closure

Complete README and focused project documentation for the scientific model, source types/provenance, direct RH declaration, MRT/globe/surface/local-air distinctions, progressive-disclosure radiant setup, history quality, comfort strategies and semantic roots, profile transformations, climate-mode support, normalization, bounds, extrapolation, fallback, manual override, diagnostics, troubleshooting, supported HA versions and repository test commands.

Ordinary installation/configuration instructions may be provided for future users. Development completion does not depend on executing those instructions, modifying an existing controller or operating any household equipment. Document software capabilities and scientific/measurement limitations without claiming proven building performance, mold prediction or frost protection.

The old Adaptive Climate blueprint is reference material only. No importer, mapping engine, migration wizard, compatibility input, blueprint parser or legacy-specific runtime component is required.

The closure record identifies the candidate revision/tree or artifact hashes, supported-version test matrix, Layer A–D results, all VI scenario/variant results, coverage, load-operation evidence, packaging checks and independent review outcome. It records `SOFTWARE_COMPLETE` only when these repository requirements pass; there is no outstanding owner test, external-installation evidence or observation-period gate.

---

## 21. Ordered implementation task list

Use the canonical working copy and remote identity from §1. Each task's tests and evidence remain in that repository; none depends on access to the owner's Home Assistant environment. The task list describes future implementation, not work performed during this document refinement.

| ID | Task | Dependencies |
| --- | --- | --- |
| **ATHB-001** | Confirm the canonical repository and mandatory working-copy identity under the build prompt's Git instructions; preserve supplied reference documents and register this revised architecture plus `ATHB_BUILD_CLARIFICATIONS.md` as the technical baseline. | — |
| **ATHB-002** | Record immutable scientific references, coefficients, source hashes, the `comf` discrepancy and numerical contract version. | 001 |
| **ATHB-003** | Create the isolated pinned oracle environment; generate and review forward/root golden fixtures, including the strategy anchors in §17.4. | 002 |
| **ATHB-004** | Implement immutable contracts, named root results with per-root typed failure, tagged measured/declared sources, domain validation and applicability classification. | 002 |
| **ATHB-005** | Implement the scalar heat-load kernel and unrounded selected ATHB transfer function; pass conformance tests. | 003–004 |
| **ATHB-006** | Implement saturation pressure, vapor pressure, dew/frost point and constant-vapor-pressure candidate RH, including fixed starting RH declarations and saturation rejection. | 004 |
| **ATHB-007** | Implement uniform/direct/globe/surface-composite MRT with explicit candidate behavior and provenance. | 004, 006 |
| **ATHB-008** | Implement strategy-fraction vote derivation and the bounded bisection solver, residual/width checks, per-root typed failures and directional eligibility within the evaluation budget. | 005–007 |
| **ATHB-009** | Implement critical-air typing, shared/local moisture, delta filtering, warm-up, same-strategy mapped roots and aggregate influence protection. | 008 |
| **ATHB-010** | Implement internal calibrated surface estimates and surface-RH diagnostics without helper dependencies; support the documented Mold Indicator-style calibration input/conversion without treating it as local air/MRT. | 006–007 |
| **ATHB-011** | Implement shared source identity, declaration handling, unit conversion, freshness, quarantine and listener reference counting. | 004 |
| **ATHB-012** | Implement time-weighted daily summaries, calendar weighting, coverage and DST behavior. | 004, 011 |
| **ATHB-013** | Implement Recorder bootstrap, persisted history, source-lineage changes and corrupt-history recovery with fake history/storage tests. | 012 |
| **ATHB-014** | Implement strategy-based directional/control-band policy, critical caps/conflicts, comfort/auto/eco/boost, fixed fallback, bounds and cross-actuator coordination. | 009–010, 013 |
| **ATHB-015** | Implement the complete climate-capability matrix, HA-unit handling, inward grid normalization and range feasibility. | 014 |
| **ATHB-016** | Implement pure per-target ownership transitions, distinguishing external target intervention from external HVAC-mode readiness/reconciliation, plus deterministic acknowledgement classification. | 004, 015 |
| **ATHB-017** | Implement versioned control recovery storage, verified critical writes and simulated clean/unclean restart reconciliation. | 013, 016 |
| **ATHB-018** | Implement the sole broker, generation preflight, coalescing, directional release hysteresis, slew/rate limits and acknowledgement handling. | 015–017 |
| **ATHB-019** | Pass complete ownership/race/failure tests before connecting platform actions to the production service-dispatch path. | 018 |
| **ATHB-020** | Implement zone orchestration, executor limits, coherent snapshots, timers and unload cleanup. | 011–019 |
| **ATHB-021** | Implement config/reconfigure/options flows, direct fixed RH, progressive-disclosure radiant configuration, comfort-strategy selection, previews/typed root failures, duplicate-target prevention and activation validation. | 020 |
| **ATHB-022** | Implement sensor/binary-sensor/switch/select/button platforms, lightweight strategy-select persistence/update and stable identities; distinguish raw roots from effective actuator requests. | 020–021 |
| **ATHB-023** | Implement decision traces with strategy/roots/failures, redacted diagnostics, transition logging and repair issues. | 020–022 |
| **ATHB-024** | Complete English/Dutch translations and scientific, configuration, operation, ordinary installation and repository-validation documentation. | 002, 021–023 |
| **ATHB-025** | Implement the versioned virtual-installation fixture schema, deterministic full-path runner, all VI-001–VI-030 fixtures/variants, golden expectation links and human-readable demonstration report. | 003–004, 014, 018, 020–024 |
| **ATHB-026** | Complete integrated Virtual Installation Validation qualification, exact command/suppression assertions, directional-root scenarios, forty-zone event/load simulations and all safety-invariant mappings. | 025 |
| **ATHB-027** | Run the full repository numerical, policy/state-machine, HA API-contract, config/options, capability, race/storage, privacy, static-analysis and packaging/manifest/HACS compatibility suite on the release candidate. | 026 |
| **ATHB-028** | Perform independent final software architecture, numerical and safety review of the immutable candidate and evidence; resolve findings, rerun affected checks and obtain review of the final candidate. | 027 |
| **ATHB-029** | Produce the validated local distribution and final software documentation/evidence, verify their identity against the reviewed candidate and close with `SOFTWARE_COMPLETE` when every Definition-of-Done item passes. Publication is separate. If candidate content changes, repeat affected checks and final review before closure. | 028 |

---

## Appendix A — Detailed Virtual Installation expectations

This appendix preserves the detailed scenario expectations from the final targeted refinement. Where §17.7 is a shorter summary, this appendix supplies the additional normative detail. `ATHB_BUILD_CLARIFICATIONS.md` remains authoritative over any conflicting scenario wording, especially directional root eligibility and HVAC-mode ownership.

### VI-001 — Dutch living room / Balanced

Baseline as specified in §17.6. The room is already inside the outer comfort band.

Expected: `G_BASE_B`; heating vote −0.25; requested 19.362709 °C; primary governing; `heat(19.5)` exactly once, acknowledged; no suppression. This proves normal target publication without waiting for a boundary crossing.

### VI-002 — Same room / Efficient

Change only comfort strategy to `efficient`.

Expected: `G_BASE_E`; heating vote −0.35; requested 18.532022 °C; `heat(19.0)` once; primary governing.

### VI-003 — Same room / Comfort

Change only comfort strategy to `comfort`.

Expected: `G_BASE_C`; heating vote −0.15; requested 20.191040 °C; `heat(20.5)` once; primary governing.

### VI-004 — Room around the Balanced target

Air 19.5 °C and RH 51.5759274364%; acknowledged ATHB target 19.5 °C and last requested room target 19.362709 °C from a valid prior generation. Send unchanged fresh reports each minute for 30 minutes of virtual time.

Expected: `G_NEAR_B`; current vote −0.233445; roots unchanged; normalized request 19.5 °C; zero additional commands; `target_unchanged`; ownership stays `OWNED`; no queue/timer growth.

### VI-005 — Room below the comfort band

Initial air 16 °C and RH 64.3079856685%, preserving baseline vapor pressure. These are initial observations, not a jump from 20 °C.

Expected: `G_BELOW_B`; current vote −0.653070 below −0.50; heating vote still −0.25 and requested target 19.362709 °C; `heat(19.5)` once. No zero-vote or boundary-root substitution.

### VI-006 — Cold critical-air location

Add eligible heating location `cold_seat`, measured air 18.5 °C, shared vapor pressure, local uniform MRT. Supply constant valid primary/local pre-roll at −600, −300 and 0 s so filtered delta is 1.5 K and warm-up is satisfied.

Expected: primary `G_BASE_B`; local current vote −0.353849; local mapped roots equal baseline +1.5 K; local heating vote −0.25; governing `cold_seat`; requested 20.862709 °C, aggregate uplift 1.5 K; `heat(21.0)` once. Local neutral 22.929145 °C is not the demanded root. No limiting reason because the requested uplift is below the cap.

### VI-007 — Pathological location and influence cap

Variant A: eligible-duration local reports at 12 °C, raw delta 8 K. Variant B: local reports at 17 °C with stable filtered delta 3 K, using the same pre-roll times.

Expected A: primary `G_BASE_B`, critical control calculation excluded with `critical_delta_outlier`; primary governing and `heat(19.5)`.

Expected B: primary `G_BASE_B`; local current vote −0.533684 and mapped roots equal baseline +3 K; raw heating demand 22.362709 °C, applied 21.362709 °C; `cold_seat` governing, `critical_demand_limited`, `heat(21.5)`. No sensor can accumulate further uplift.

### VI-008 — Cold measured surface

Add measured surface 16 °C with effective view factor 0.25 and uniform moving background; no critical-air locations.

Expected: `G_SURFACE16_B`; heating root/request 19.798354 °C; primary governing; `heat(20.0)` once. Radiant mode is surface composite; remove uniform-fallback reason, retain `limited_evidence`; measured surface is not local air and creates no separate critical demand.

### VI-009 — Internally modelled surface

No helper or measured surface entity. Declare calibrated `f_Rsi=0.6`, current outdoor 5 °C, primary 20 °C, view factor 0.25 and uniform moving background.

Expected: surface estimate `5 + 0.6×(20−5) = 14 °C`, provenance `estimated`; freeze that estimate within the solve. `G_SURFACE14_B`; request 20.050001 °C; `heat(20.5)` once; primary governing; replace uniform-fallback reason with `modelled_surface`; retain `limited_evidence`. Internal surface RH is 73.148458% (±0.001 percentage points), below the 80% diagnostic threshold, without any helper lookup.

### VI-010 — Direct fixed RH declaration

Select `rh_source={kind:declared,value_pct:50}`; remove the RH entity and all RH observations from the fixture.

Expected: `G_BASE_B`; roots and `heat(19.5)` match VI-001 exactly. RH/moisture provenance is `declared`; RH has no sensor timestamp/freshness timer. Assert no RH entity/helper registration or lookup. No measured-RH label appears in the result.

### VI-011 — Missing optional MRT

Select a direct MRT source but mark it unavailable; the declared uniform approximation is allowed.

Expected: `G_BASE_B`; `heat(19.5)` once; primary governing; `DEGRADED_READY`; baseline quality plus `radiant_source_unavailable`. Provenance says estimated uniform MRT, never measured MRT.

### VI-012 — Invalid mandatory air temperature

Parameterize unavailable, stale (>30 min), nonfinite, Boolean and malformed primary observations. Keep RH and history valid.

Expected: no current primary sensation or usable primary roots; typed `primary_temperature_invalid` for malformed/nonfinite/Boolean/unavailable or `primary_temperature_stale` for stale data. No requested/normalized executable target, no governing location; empty payload list, including after 15 min; explanation-only hold then `INVALID`. Ownership remains separate.

### VI-013 — Invalid/missing RH without declaration

Measured mode, with RH missing/unavailable, stale (>30 min), or 101%; no declared alternative. Also test absent RH mode through config-flow validation.

Expected runtime variants: typed `primary_rh_invalid` or `primary_rh_stale`; no primary numerical result, no executable target, zero calls immediately and after hold, no fallback. Configuration variant: form error `rh_source_required`, no created entry. No invented 50% value or sensor helper.

### VI-014 — Partial and insufficient outdoor history

Variant A: retain only the three newest complete 5 °C days. Variant B: retain only the two newest complete days; set initial climate target to 21 °C to expose the fallback command.

Expected A: `R=5`, represented weight `2.44/3.951424 ≈0.617499`, `partial_history`, three eligible days; `G_BASE_B`, `heat(19.5)` once, quality adds `partial_history`.

Expected B: diagnostic estimate 5 °C is ineligible for adaptive solving/control; primary current sensation and roots unavailable for an eligible decision with `history_insufficient`; `FALLBACK_READY`, requested fallback 18 °C, `heat(18.0)` once, primary governing not applicable. No adaptive result fabricated from the two-day estimate.

### VI-015 — Cold-winter extrapolation

All seven days and current outdoor are −10 °C; initial target 21 °C. Variant A: allow extrapolation. Variant B: enable `reject_extrapolation`.

Expected: `G_WINTER_B` in the numerical preview; quality adds `extrapolated`, retaining `limited_evidence`.

A: raw heating request 15.944270 °C, user bound applies to 18 °C, `heat(18.0)`, `user_bound_applied`, no fallback.

B: immediate hold/no command with `extrapolation_rejected`; after 15 min virtual time, valid inputs permit fixed fallback request 18 °C and `heat(18.0)`. Record distinct adaptive versus fallback paths even though payloads coincide.

### VI-016 — Humid warm room and saturation

Air 28 °C, RH 80%, seven days/current outdoor 25 °C, cooling scalar target initially 28 °C; other limits remain baseline.

Numerical anchor: `G_HUMID_B`; lower/heating/neutral roots each `moisture_limited_no_solution`; cooling/upper roots remain visible at 25.660781/27.525210 °C.

**Normative actuation expectation is superseded by `ATHB_BUILD_CLARIFICATIONS.md` §2:** for a cooling-only actuator, the valid cooling-control root is sufficient for adaptive cooling if all other mandatory inputs/ownership/capabilities are valid. With a 0.5 °C inward cooling grid, expect 25.5 °C. Do not fall back merely because unrelated heating/neutral roots are moisture-limited. For ranged heat/cool, the absent heating endpoint makes the adaptive range ineligible.

Dew point remains approximately 24.225293 °C (±0.001 K). Never clamp candidate RH to manufacture missing roots.

### VI-017 — Cooling-only strategy targets

Mode `cool`, scalar capability and current target 26 °C. Mandatory variants for all three comfort strategies.

Expected:

- Balanced: `G_BASE_B`, cooling vote +0.25, request 23.481038 °C, `cool(23.0)`.
- Efficient: `G_BASE_E`, +0.35, request 24.297765 °C, `cool(24.0)`.
- Comfort: `G_BASE_C`, +0.15, request 22.662014 °C, `cool(22.5)`.

One acknowledged call per variant; primary governing.

### VI-018 — Ranged heat/cool

Mode `heat_cool`, range feature, current range `[18,26] °C`, gap 1 K; Balanced.

Expected: `G_BASE_B`; requested control band `[19.362709,23.481038] °C`; normalized `range(19.5,23.0)` in one call. Comfort band `[17.281547,25.518575] °C` remains separate diagnostics.

### VI-019 — Explicit mapped auto range

Mode `auto`, range capability, explicitly declared adjustable ranged mapping; current range `[18,26] °C`.

Expected: same roots, requested control band and exact `range(19.5,23.0)` as VI-018. No mode field or mode service; mapping and range support are both required.

### VI-020 — Unsupported auto semantics

Auto mode with flags but no explicit mapping. Also run bidirectional scalar mapping without a declared direction.

Expected: `G_BASE_B` preview remains; no executable intent or call; `unsupported_auto_mapping`; target readiness `SUSPENDED_MODE`; ownership retained separately. Never choose a neutral scalar target.

### VI-021 — Climate off

Initial mode `off`, otherwise baseline, enabled control intent retained.

Expected: `G_BASE_B` preview remains; `hvac_off`; no executable intent and zero climate services, including turn-on or mode change; `SUSPENDED_MODE`. Per `ATHB_BUILD_CLARIFICATIONS.md`, the mode change itself is not automatically a long-lived manual temperature-target override.

### VI-022 — Fahrenheit and one conversion

HA unit °F; fake backend native unit °C. State bounds `[60.8,86] °F`; explicit step 1 °F and origin override 64 °F; current target 66 °F; baseline user limits remain `[18,26] °C`.

Expected: `G_BASE_B`; room request 19.362709 °C → 66.852876 °F; legal inward result **67 °F**; exact service `heat(67.0)`. Fake HA climate service converts once to backend **19.444444 °C**; readback 67 °F acknowledges. Assert grid index 3, user/device bounds and no second ATHB conversion to device units.

### VI-023 — Coarse-grid inward normalization

Step 2 °C, origin override 18 °C. Run heat, cool and ranged Balanced variants. Also run ranged step 4 °C, same origin.

Expected: base roots unchanged. Heat request 19.362709 → `heat(20.0)`. Cool 23.481038 → `cool(22.0)`. Range → `range(20.0,22.0)`. Add normalization reason `grid_inward_adjustment` when movement is nonzero. The nearer outward alternatives 18/24 are forbidden. Step-4 range has inward pair `[22,22]`, hence zero calls and `no_legal_range`; never widen to `[18,26]`.

### VI-024 — Manual external setpoint change

After baseline acknowledgement, inject an external user-context `set_temperature(21 °C)` and its feedback; recompute with unchanged environment.

Expected: exactly one ATHB call total (the initial 19.5 °C); external call recorded separately. Ownership `OWNED → MANUAL_OVERRIDE`, expiry two hours after intervention, revision increments; all new ATHB target calls suppressed with `manual_override`, queued intent empty. `G_BASE_B` preview remains.

### VI-025 — ATHB-originated acknowledgement

Deliver baseline own-context feedback before the fake service coroutine returns; variant uses contextless exact pending-value feedback with no intervening external revision.

Expected: one `heat(19.5)` call; `OWNED` retained; pending journal resolves `acknowledged` or `inferred_acknowledged` respectively. No override, retry or second write; matching command/context/generation recorded.

### VI-026 — Obsolete delayed acknowledgement

Start with baseline numerical environment, an obsolete pending-command journal from revision 1, and current `MANUAL_OVERRIDE` revision 2 after an external target 21 °C. Deliver old-context feedback for 19.5 °C.

Expected: zero new ATHB calls; `obsolete_acknowledgement` event classification; suppression `manual_override`. Current ownership revision, override expiry and active command/policy state are unchanged. Old evidence may be annotated but cannot resolve current state. Older-timestamp feedback cannot replace the current observation; a fresh physical readback may update observation diagnostics without regaining ownership or acknowledging a newer command.

### VI-027 — Restart recovery state simulation

Reconstruct runtime from in-memory/temporary Store fixtures.

A: clean state, enabled, matching current/acknowledged 19.5 °C.  
B: unclean state with unresolved prior command and observed 18 °C.  
C: explicit resume after B.

Use fresh baseline observations; deliberately store an unrelated historical calculation to prove it is not executed.

Expected A: `RECONCILING → OWNED`, recomputed `G_BASE_B`, zero calls, `target_unchanged`.

Expected B: fresh preview allowed but remain `RECONCILING`, `reconciliation_required`, no replay/call.

Expected C: fresh reconciliation and a new command identity emit `heat(19.5)` once. Simulated expiry variants also prove boost/override deadlines are not extended across downtime.

### VI-028 — Multiple actuators and conflicts

Heating `climate.heat_a` and cooling `climate.cool_b`, initial 18/26 °C, both owned; heating calibration +0.5 K, cooling 0 K, same 0.5 °C grids. Conflict variant starts with cooling manually overridden at 20 °C.

Expected normal: `G_BASE_B`, primary governs both; heating calibrated request 19.862709 → `heat_a(20.0)` and `cool_b(23.0)` exactly once each. Room-equivalent normalized targets 19.5/23.0 meet the 1 K gap.

Expected conflict: heat would leave only 0.5 K to observed cooling; zero calls, `cross_actuator_conflict` for heat and `manual_override` for cool; the manual cooling target is never overwritten.

### VI-029 — Zones sharing an outdoor source

Two distinct zone IDs, identical baseline environments/strategies, distinct climate targets, exactly the same outdoor identity/attribute/timezone/collection policy. Start both before bootstrap completes.

Expected: both produce `G_BASE_B` and their own exact `heat(19.5)` call; one collector, one bootstrap operation, two references, seven daily summaries integrated once. Unload first zone: one reference remains; unload second: no listener/lease leaks. Distinct alpha variant reuses same raw summaries without a second collector.

### VI-030 — Event storm and stale work

Hold a baseline numerical completion and one old unsent 19.5 °C intent. A previously acknowledged 18 °C command at clock origin makes that intent rate-limited until +60 s; it is not a new service call in this timeline. Issue 10,000 deterministic source events over 10 s, then select Comfort as the final configuration generation. Final environment is exactly baseline. Release controlled work and advance required fake deadlines; the explicit strategy change retains the 10-second hard interval and bypasses environmental slew/ordinary interval.

Expected: final `G_BASE_C`, heating vote −0.15 and target 20.191040 °C; only the latest generation publishes and emits `heat(20.5)` once during the timeline. Obsolete calculation/intent counters increment with `stale_generation`; at most one running job and one latest calculation request for this zone, globally at most two, and one pending/one queued command per actuator. No older 19.5 °C intent is dispatched; all work settles within the §15.6 operation budget. The strategy change itself is a lightweight runtime transition and does not unload/reload the config entry.

### Cross-strategy assertions

For VI-001/002/003/017 assert on raw roots:

```text
Heating: Efficient < Balanced < Comfort < Thermal neutral
Cooling: Thermal neutral < Comfort < Balanced < Efficient
```

Actuator grid rounding and hard bounds may collapse different strategy requests to the same legal payload; they must not alter the raw model ordering.
